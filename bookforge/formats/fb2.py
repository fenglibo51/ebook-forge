"""FB2（FictionBook 2.0）读写。

FB2 就是单个 XML：正文在 <body> 的 <section> 树里，图片以 base64 塞在
<binary> 元素里。俄罗斯/东欧电子书主流，国内资源站也常见。

读：递归 <section> → Chapter（嵌套章节各成一段），<p> → Paragraph，
    <image l:href="#id"> → ImageBlock（从 <binary> 查 base64）。
写：生成最小合法 FB2（description + body + binary），图片同样 base64 内嵌。
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET

from ..model import Chapter, ImageBlock, Paragraph, TextBook

NS = "{http://www.gribuser.ru/xml/fictionbook/2.0}"
XLINK = "{http://www.w3.org/1999/xlink}href"

_MEDIA_EXT = {"image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif",
              "image/webp": "webp"}


class FB2Error(ValueError):
    pass


def _text_of(el: ET.Element) -> str:
    """把 <p> 里的文字拼起来（<strong>/<emphasis> 等内联标签只取文字）"""
    return "".join(el.itertext()).strip()


def _clean(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def read(path) -> TextBook:
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise FB2Error(f"FB2 解析失败（XML 不合法）：{e}")

    # 图片：<binary id="..." content-type="...">base64</binary>
    binaries: dict[str, ImageBlock] = {}
    for b in root.iter(NS + "binary"):
        bid = b.get("id") or ""
        ctype = (b.get("content-type") or "image/jpeg").lower()
        try:
            data = base64.b64decode((b.text or "").strip())
        except Exception:
            continue
        if data:
            binaries[bid] = ImageBlock(data=data, fmt=_MEDIA_EXT.get(ctype, "jpeg"))

    meta: dict = {}
    ti = root.find(f"{NS}description/{NS}title-info")
    if ti is not None:
        bt = ti.find(NS + "book-title")
        if bt is not None and bt.text:
            meta["title"] = _clean(bt.text)
        author = ti.find(NS + "author")
        if author is not None:
            parts = [(author.findtext(NS + k) or "").strip()
                     for k in ("first-name", "middle-name", "last-name", "nickname")]
            name = " ".join(p for p in parts if p)
            if name:
                meta["author"] = name
        lang = ti.find(NS + "lang")
        if lang is not None and lang.text:
            meta["language"] = lang.text.strip()

    chapters: list[Chapter] = []

    def walk_section(sec: ET.Element, depth: int, fallback: str) -> None:
        title_el = sec.find(NS + "title")
        title = ""
        if title_el is not None:
            title = _clean(" ".join(_text_of(p) for p in title_el.findall(NS + "p")))
        ch = Chapter(title=title or fallback)

        for child in sec:
            tag = child.tag
            if tag == NS + "title":
                continue
            if tag == NS + "p":
                text = _text_of(child)
                if text:
                    ch.blocks.append(Paragraph(
                        text=text,
                        bold=child.find(NS + "strong") is not None,
                        italic=child.find(NS + "emphasis") is not None))
            elif tag == NS + "empty-line":
                continue
            elif tag == NS + "image":
                href = (child.get(XLINK) or "").lstrip("#")
                img = binaries.get(href)
                if img:
                    ch.blocks.append(img)
            elif tag == NS + "section":
                walk_section(child, depth + 1, fallback=f"第 {len(chapters) + 1} 节")

        if ch.blocks or ch.title:
            chapters.append(ch)

    for body in root.findall(NS + "body"):
        # body 自己的 <title> 是书名，不当章节标题
        for sec in body.findall(NS + "section"):
            walk_section(sec, 0, fallback=f"第 {len(chapters) + 1} 章")

    if not chapters:
        raise FB2Error("FB2 里没有可提取的正文（找不到 <section> 内容）")
    return TextBook(chapters=chapters, metadata=meta, source_format="fb2")


def to_bytes(book: TextBook) -> bytes:
    """TextBook → FB2。图片转 base64 内嵌。"""
    ET.register_namespace("", NS.strip("{}"))
    ET.register_namespace("l", "http://www.w3.org/1999/xlink")

    root = ET.Element(f"{NS}FictionBook")
    desc = ET.SubElement(root, f"{NS}description")
    ti = ET.SubElement(desc, f"{NS}title-info")
    title = (book.metadata or {}).get("title") or ""
    ET.SubElement(ti, f"{NS}book-title").text = title or "未命名"
    author = (book.metadata or {}).get("author") or ""
    if author:
        a = ET.SubElement(ti, f"{NS}author")
        ET.SubElement(a, f"{NS}nickname").text = author
    ET.SubElement(ti, f"{NS}lang").text = (book.metadata or {}).get("language") or "zh"

    body = ET.SubElement(root, f"{NS}body")
    if title:
        t = ET.SubElement(body, f"{NS}title")
        ET.SubElement(t, f"{NS}p").text = title

    binaries: list[tuple[str, ImageBlock]] = []
    img_index = [0]

    def emit_section(ch: Chapter) -> ET.Element:
        sec = ET.SubElement(body, f"{NS}section")
        if ch.title:
            t = ET.SubElement(sec, f"{NS}title")
            ET.SubElement(t, f"{NS}p").text = ch.title
        for block in ch.blocks:
            if isinstance(block, Paragraph):
                if block.text.strip():
                    ET.SubElement(sec, f"{NS}p").text = block.text
            elif isinstance(block, ImageBlock):
                img_index[0] += 1
                bid = f"img{img_index[0]}"
                binaries.append((bid, block))
                ET.SubElement(sec, f"{NS}image", {f"{XLINK}": f"#{bid}"})
        return sec

    for ch in book.chapters:
        emit_section(ch)

    ctype = {"jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp"}
    for bid, img in binaries:
        b = ET.SubElement(root, f"{NS}binary",
                          {"id": bid, "content-type": ctype.get(img.fmt, "image/jpeg")})
        b.text = base64.b64encode(img.data).decode("ascii")

    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml
