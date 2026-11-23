"""DOCX 读写。

DOCX = ZIP + 一堆 XML。真正要读的只有两个：

    word/document.xml          正文（w:body → w:p 段落，w:r/w:t 文字）
    word/_rels/document.xml.rels  图片引用（rIdN → media/imageN.png）

图片在段落里以 <w:drawing><a:blip r:embed="rIdN"> 出现，要经 rels 换成实际文件。

读：支持段落 / 标题样式 / 图片。
写：生成最小可用 docx（纯文字 + 标题样式）。**不写图片** —— 嵌入图片要拼
    完整的 drawing XML（图形框架 + 变换），P2 先不做，遇到图片会在结果里说明。
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ..model import Chapter, ImageBlock, Paragraph, TextBook

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_HEADING_RE = re.compile(r"(?:heading|标题|標題)\s*([1-9])?", re.I)
_MEDIA_FMT = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}


class DOCXError(ValueError):
    pass


def _read_rels(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        xml = zf.read("word/_rels/document.xml.rels")
    except KeyError:
        return {}
    root = ET.fromstring(xml)
    out = {}
    for rel in root:
        rid = rel.get("Id") or ""
        target = rel.get("Target") or ""
        if not target or target.lower().startswith(("http://", "https://", "mailto:")):
            continue
        out[rid] = "word/" + target.lstrip("/").replace("../", "")
    return out


def read(path) -> TextBook:
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise DOCXError("docx 文件损坏（不是有效的 zip）")

    with zf:
        try:
            doc_xml = zf.read("word/document.xml")
        except KeyError:
            raise DOCXError("docx 里找不到 word/document.xml")
        rels = _read_rels(zf)
        root = ET.fromstring(doc_xml)
        body = root.find(W + "body")
        if body is None:
            raise DOCXError("docx 的 document.xml 里没有 body")

        chapters: list[Chapter] = []
        cur = Chapter(title="")

        def flush() -> None:
            nonlocal cur
            if cur.blocks or cur.title:
                chapters.append(cur)
            cur = Chapter(title="")

        for p in body.iter(W + "p"):
            style = ""
            ppr = p.find(W + "pPr")
            if ppr is not None:
                ps = ppr.find(W + "pStyle")
                if ps is not None:
                    style = ps.get(W + "val") or ""

            text = "".join(t.text or "" for t in p.iter(W + "t")).strip()

            # 图片：w:drawing → a:blip r:embed
            for blip in p.iter(A + "blip"):
                rid = blip.get(R + "embed") or ""
                target = rels.get(rid)
                if not target:
                    continue
                try:
                    data = zf.read(target)
                except KeyError:
                    continue
                ext = target.rsplit(".", 1)[-1].lower()
                cur.blocks.append(ImageBlock(data=data,
                                             fmt=_MEDIA_FMT.get(ext, "jpeg")))

            if not text:
                continue

            if _HEADING_RE.match(style) or style.lower() in ("title", "subtitle"):
                flush()
                cur.title = text
            else:
                cur.blocks.append(Paragraph(text=text))

        flush()

    if not chapters:
        raise DOCXError("docx 里没有可提取的正文")
    return TextBook(chapters=chapters, metadata={}, source_format="docx")


# ── 写 ────────────────────────────────────────────────────────────────────

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Default Extension="jpeg" ContentType="image/jpeg"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_RELS_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
"""

_NS_DECL = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"')

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# 96 dpi 下 1 像素 = 9525 EMU（Word 的绘图单位）
_EMU_PER_PX = 9525
# 页面可用宽度：A4 11906 twips - 左右边距 1134*2，换算成 EMU 约 6 英寸
_MAX_W_EMU = 5486400

# 能直接嵌进 docx 的图片格式（content-types 里声明过的）
_EMBEDDABLE = {"png", "jpg", "jpeg"}


def _drawing_xml(rid: str, idx: int, w: int, h: int, name: str) -> str:
    """图片的 DrawingML 片段。"""
    cx = w * _EMU_PER_PX if w else _MAX_W_EMU
    cy = h * _EMU_PER_PX if h else _MAX_W_EMU
    if cx > _MAX_W_EMU:                      # 太宽就等比缩到版心宽度
        cy = int(cy * _MAX_W_EMU / cx)
        cx = _MAX_W_EMU
    return (
        '<w:p><w:r><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="{idx}" name="{_esc(name)}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{idx}" name="{_esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/>'
        '<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic>'
        '</wp:inline></w:drawing></w:r></w:p>')


def to_bytes(book: TextBook) -> tuple[bytes, int]:
    """TextBook → docx 字节。返回 (字节, 被跳过的图片数)。

    手拼 XML 而不是用 ElementTree —— Word 对命名空间前缀敏感，
    ET 输出的 ns0: 前缀虽然合法，但部分阅读器（WPS 旧版）会不认。

    图片现在能嵌了：PNG / JPEG 原字节放进 word/media/，document.xml 里用
    DrawingML 引用。超出可嵌范围的格式（webp / gif 等）才计入 skipped。
    """
    paras: list[str] = []
    skipped = 0
    media: list[tuple[str, bytes]] = []          # (filename, data)
    rels: list[str] = []

    for ch in book.chapters:
        if ch.title:
            paras.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{_esc(ch.title)}</w:t></w:r></w:p>')
        for block in ch.blocks:
            if isinstance(block, Paragraph):
                if block.text.strip():
                    paras.append(
                        f'<w:p><w:r><w:t xml:space="preserve">{_esc(block.text)}</w:t></w:r></w:p>')
            elif isinstance(block, ImageBlock):
                fmt = (block.fmt or "").lower()
                if fmt == "jpg":
                    fmt = "jpeg"
                if fmt not in _EMBEDDABLE or not block.data:
                    skipped += 1
                    continue
                idx = len(media) + 1
                ext = "png" if fmt == "png" else "jpg"
                fname = f"image{idx}.{ext}"
                media.append((fname, block.data))
                rid = f"rIdImg{idx}"
                rels.append(
                    f'<Relationship Id="{rid}" '
                    f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="media/{fname}"/>')
                paras.append(_drawing_xml(rid, idx, block.width or 0,
                                          block.height or 0, fname))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document {_NS_DECL}>\n<w:body>\n'
        + "\n".join(paras)
        + '\n<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>\n'
        '</w:body>\n</w:document>\n')

    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/document.xml", document)
        if rels:
            zf.writestr("word/_rels/document.xml.rels", _RELS_HEAD + "\n".join(rels) + "\n</Relationships>\n")
        for fname, data in media:
            zf.writestr(f"word/media/{fname}", data)      # 原字节，不重压
    return buf.getvalue(), skipped
