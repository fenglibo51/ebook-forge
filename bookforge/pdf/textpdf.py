"""文字书排版（L1 → L2）+ PDF 输出。

L2 在 L1 之上补了「中文排版细节」：

    行首禁则      。，、）」』等不落行首（拉回上一行）
    行尾禁则      （「《 等开括号不落行尾（推到下一行）
    标点挤压      行末标点、连续标点按半角宽度排
    中英混排间距  中文与英文/数字之间加 1/4 em 空隙
    孤行控制      标题后面至少跟 2 行正文；段落末行不单独成页

不做：脚注（EPUB 的脚注结构各家不同，收益低风险高）、多栏、图文环绕。

PDF 侧用 Type0 + CIDFontType2（Identity-H 编码 + CIDToGIDMap /Identity），
字体做子集化 —— 不然每本书都要背 10MB 的 Noto Sans SC。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..model import (DEFAULT_TEXT_STYLE, Chapter, ImageBlock, PageSpec,
                     Paragraph, TextBook)
from .fonts import TrueTypeFont

# 默认字体：按顺序找，谁在就用谁
DEFAULT_FONT_CANDIDATES = (
    # Docker 镜像里随包带的那份
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
    # 项目自带（本地开发 / 直接跑源码）
    str(Path(__file__).resolve().parent.parent.parent / "assets/fonts/NotoSansSC-Regular.ttf"),
    # 容器里 hermes 的自定义字体目录
    "/home/hermeswebui/.hermes/home/.fonts/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

# 行首禁则：这些字符不能出现在一行开头
NO_LINE_START = set("，。、；：？！）】》」』〕〉》”’…—～·%,.;:?!)]}>")
# 行尾禁则：这些字符不能出现在一行末尾
NO_LINE_END = set("（【《「『〔〈“‘([{<")
# 可以压缩到半角宽度的标点
SQUEEZABLE = set("，。、；：？！）】》」』〕〉”’…—～·%,.;:?!)]}>（【《「『〔〈“‘([{<")


class LayoutError(ValueError):
    pass


@dataclass
class Line:
    text: str
    x: float
    y: float
    size: float
    is_title: bool = False
    gaps: dict[int, float] = field(default_factory=dict)     # 第 i 个字前额外空隙
    squeeze: dict[int, float] = field(default_factory=dict)  # 第 i 个字宽度缩放（0.5 = 半角）
    line_scale: float = 1.0                                 # 整行横向缩放（标点压完还超时用）

    @property
    def width(self) -> float:
        """实际排版宽度（含空隙、扣压缩）"""
        return self._w if hasattr(self, "_w") else 0.0


@dataclass
class Page:
    lines: list[Line] = field(default_factory=list)
    images: list[tuple[bytes, str, float, float, float, float]] = field(default_factory=list)
    #             data, fmt,  x, y, 显示宽, 显示高


def find_font(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise LayoutError(f"字体不存在：{explicit}")
        return p
    for cand in DEFAULT_FONT_CANDIDATES:
        p = Path(cand)
        if p.exists():
            return p
    raise LayoutError(
        "找不到可嵌入的中文字体。请把 NotoSansSC-Regular.ttf 放到 "
        "~/.fonts/ 下，或用 --font 指定路径")


# ── 断行 ──────────────────────────────────────────────────────────────────

_EN_WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
               "-_./:;@#&+=%'\"()[]{}")


def _tokenize(text: str) -> list[str]:
    """切成「排在一起不可分开」的单元：中文单字、英文单词、空格"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in _EN_WORD:
            buf.append(ch)
        elif ch == " ":
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(" ")
        else:
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _width(font: TrueTypeFont, text: str, size: float) -> float:
    """文字在给定字号下的宽度（pt）"""
    units = font.text_width(text)
    return units * size / font.units_per_em


def _char_width(font: TrueTypeFont, ch: str, size: float) -> float:
    return font.advance(font.gid(ch)) * size / font.units_per_em


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF)


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "+-_/@#&")


def line_width(font: TrueTypeFont, ln: "Line", size: float) -> float:
    """一行的实际宽度（含空隙、扣压缩）"""
    w = 0.0
    for i, ch in enumerate(ln.text):
        w += ln.gaps.get(i, 0.0)
        cw = _char_width(font, ch, size)
        if i in ln.squeeze:
            cw *= ln.squeeze[i]
        w += cw
    return w


def apply_kinsoku(font: TrueTypeFont, lines: list["Line"], size: float) -> None:
    """行首行尾禁则：标点该往哪边挪就挪，不留在错误的位置。"""
    for _ in range(4):
        changed = False
        # 行首禁则：把禁则标点拉回上一行
        for i in range(1, len(lines)):
            ln = lines[i]
            while ln.text and ln.text[0] in NO_LINE_START:
                lines[i - 1].text += ln.text[0]
                ln.text = ln.text[1:]
                ln.gaps = {k - 1: v for k, v in ln.gaps.items() if k >= 1}
                ln.squeeze = {k - 1: v for k, v in ln.squeeze.items() if k >= 1}
                changed = True
        # 行尾禁则：把开括号/开引号推到下一行
        for i in range(len(lines) - 1):
            ln = lines[i]
            while ln.text and ln.text[-1] in NO_LINE_END:
                ch = ln.text[-1]
                ln.text = ln.text[:-1]
                nxt = lines[i + 1]
                nxt.text = ch + nxt.text
                nxt.gaps = {k + 1: v for k, v in nxt.gaps.items()}
                nxt.squeeze = {k + 1: v for k, v in nxt.squeeze.items()}
                changed = True
        if not changed:
            break


def squeeze_overflow(font: TrueTypeFont, lines: list["Line"],
                     avail: float, size: float) -> None:
    """禁则把标点挪过来之后行会变宽，先把可压缩的标点收成半角。

    标点压完还超（连续好几个标点被拉到同一行），再整行横向微缩 ——
    上限 8%，再超就让它悬挂（专业排版允许标点探出版心）。
    """
    for ln in lines:
        w = line_width(font, ln, size)
        if w <= avail + 0.01:
            continue
        for i in range(len(ln.text) - 1, -1, -1):
            if w <= avail + 0.01:
                break
            ch = ln.text[i]
            if ch in SQUEEZABLE and i not in ln.squeeze:
                w -= _char_width(font, ch, size) * 0.5
                ln.squeeze[i] = 0.5
        if w > avail + 0.01:
            ln.line_scale = max(avail / w, 0.92)


def mark_cjk_ascii_gaps(font: TrueTypeFont, lines: list["Line"], size: float) -> None:
    """中英混排：中文和英文/数字交界处加 1/4 em 空隙。"""
    gap = size * 0.25
    for ln in lines:
        t = ln.text
        for i in range(1, len(t)):
            a, b = t[i - 1], t[i]
            if (_is_cjk(a) and _is_word_char(b)) or (_is_word_char(a) and _is_cjk(b)):
                ln.gaps[i] = gap


def break_lines(font: TrueTypeFont, text: str, avail: float, size: float,
                indent: float = 0.0) -> list[str]:
    """按可用宽度断行。中文逐字、英文逐词。"""
    tokens = _tokenize(text)
    lines: list[str] = []
    cur: list[str] = []
    cur_w = 0.0
    first = True

    def limit() -> float:
        return avail - (indent if first else 0.0)

    for tok in tokens:
        w = _width(font, tok, size)
        if tok == " " and not cur:
            continue                       # 行首不留空格
        if cur_w + w <= limit() or not cur:
            cur.append(tok)
            cur_w += w
        else:
            lines.append("".join(cur).rstrip())
            cur = [] if tok == " " else [tok]
            cur_w = 0.0 if tok == " " else w
            first = False
            # 单个 token 比整行还宽（超长英文单词）：硬拆
            while cur and _width(font, "".join(cur), size) > limit():
                s = "".join(cur)
                n = len(s)
                while n > 1 and _width(font, s[:n], size) > limit():
                    n -= 1
                lines.append(s[:n])
                cur = [s[n:]]
    if cur:
        lines.append("".join(cur).rstrip())
    return [ln for ln in lines if ln]


# ── 排版 ──────────────────────────────────────────────────────────────────

def layout_book(book: TextBook, font: TrueTypeFont, *,
                spec: PageSpec | None = None,
                style: dict | None = None) -> list[Page]:
    spec = spec or book.page_spec or PageSpec.a5()
    st = dict(DEFAULT_TEXT_STYLE)
    if style:
        st.update(style)
    size = st["font_size"]
    line_h = size * st["line_height"]
    indent = st["para_indent"]
    title_size = size * 1.6
    title_line_h = title_size * 1.3
    avail = spec.width - spec.margin_left - spec.margin_right
    top = spec.height - spec.margin_top
    bottom = spec.margin_bottom + size * 2          # 底部留出页码空间

    pages: list[Page] = [Page()]
    y = top

    def new_page() -> None:
        nonlocal y
        pages.append(Page())
        y = top

    def ensure(height: float) -> None:
        nonlocal y
        if y - height < bottom:
            new_page()

    for ch in book.chapters:
        has_body = any((isinstance(b, Paragraph) and b.text.strip())
                       or isinstance(b, ImageBlock) for b in ch.blocks)
        # 没有实际内容（只有标题、或段落全是空白）的章节不占页 ——
        # 否则封面页/版权页会在 PDF 里留下一堆「只有页码」的空页（实测 704 页里有 3 张）。
        if not has_body:
            continue

        # 章节另起页（L1 的规矩）
        if pages[-1].lines or pages[-1].images:
            new_page()

        if ch.title:
            # 孤行控制：标题后面至少要跟 2 行正文，否则标题孤零零挂在页尾
            ensure(title_line_h + line_h * 2)
            y -= title_line_h
            pages[-1].lines.append(Line(text=ch.title, x=spec.margin_left,
                                        y=y, size=title_size, is_title=True))
            y -= line_h * 0.6

        for block in ch.blocks:
            if isinstance(block, ImageBlock):
                w, h = block.width or 0, block.height or 0
                if not w or not h:
                    from ..formats import png as pngmod
                    w, h, _ = pngmod.image_size(block.data)
                if not w or not h:
                    w, h = int(avail), int(avail * 1.414)     # 探测不出就按 1:√2 兜底
                # 图内不调 ensure()：它会把「半行间距」也算进去，导致明明放得下
                # 却又换页 —— 实测每张整页图后面都多出一张只有页码的空页。
                avail_h = y - bottom
                scale = min(avail / w, avail_h / h)
                full_h = top - bottom
                if scale * h < full_h * 0.5:        # 缩得只剩半页，不如另起一页
                    new_page()
                    avail_h = full_h
                    scale = min(avail / w, avail_h / h)
                dw, dh = w * scale, h * scale
                y -= dh
                x = spec.margin_left + (avail - dw) / 2
                pages[-1].images.append((block.data, block.fmt, x, y, dw, dh))
                y -= line_h * 0.5
                continue

            text = block.text.strip()
            if not text:
                continue
            raw = break_lines(font, text, avail, size, indent)
            lines = [Line(text=t, x=0.0, y=0.0, size=size) for t in raw]
            # L2 顺序很重要：先把中英间距加上，再算宽度挤压 ——
            # 反过来的话间距会把行重新撑宽，挤压白做（实测 697 页里 33 行超宽）。
            apply_kinsoku(font, lines, size)
            mark_cjk_ascii_gaps(font, lines, size)
            squeeze_overflow(font, lines, avail, size)
            for i, ln in enumerate(lines):
                if not ln.text:
                    continue
                ensure(line_h)
                y -= line_h
                ln.x = spec.margin_left + (indent if i == 0 else 0.0)
                ln.y = y
                pages[-1].lines.append(ln)

    # 去掉末尾空页
    while len(pages) > 1 and not pages[-1].lines and not pages[-1].images:
        pages.pop()
    return pages


# ── PDF 输出 ──────────────────────────────────────────────────────────────

def _pdf_str(s: str) -> str:
    if all(32 <= ord(c) < 127 for c in s):
        return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _build_tounicode(mapping: dict[int, int]) -> bytes:
    """CID → Unicode 的 ToUnicode CMap（复制粘贴、搜索都靠它）"""
    lines = ["/CIDInit /ProcSet findresource begin",
             "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    items = sorted((cid, cp) for cp, cid in mapping.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for cid, cp in chunk:
            utf16 = chr(cp).encode("utf-16-be").hex().upper()
            lines.append(f"<{cid:04X}> <{utf16}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("latin-1")


class TextPDFWriter:
    """TextBook → PDF。字体子集化 + Type0 输出。"""

    def __init__(self, font_path: str | Path):
        self.font = TrueTypeFont.load(font_path)
        self.font_name = Path(font_path).stem.replace(" ", "")

    def build(self, book: TextBook, *, spec: PageSpec | None = None,
              style: dict | None = None, title: str = "") -> bytes:
        spec = spec or book.page_spec or PageSpec.a5()
        pages = layout_book(book, self.font, spec=spec, style=style)
        if not pages:
            raise LayoutError("没有可输出的内容")

        # 收集全书用到的字符
        chars: set[str] = set()
        for pg in pages:
            for ln in pg.lines:
                chars.update(ln.text)
        # ⚠️ 页码是排版后加的，字符也要算进去 —— 漏了它页码会显示成空框
        chars.update("- 0123456789")
        if not chars:
            chars.add(" ")
        subset, char2gid = self.font.subset(chars)
        # PDF 里 CID == 子集内 GID（CIDToGIDMap /Identity）
        cp2cid = {c: char2gid[c] for c in chars if c in char2gid}
        used_cids = sorted(set(cp2cid.values()))

        objs: dict[int, bytes] = {}
        nxt = 1

        def alloc() -> int:
            nonlocal nxt
            i = nxt
            nxt += 1
            return i

        def put(num: int, body: bytes) -> None:
            objs[num] = body

        def stream(num: int, dict_body: str, data: bytes) -> None:
            objs[num] = (f"{num} 0 obj\n<< {dict_body} /Length {len(data)} >>\nstream\n"
                         .encode() + data + b"\nendstream\nendobj\n")

        catalog = alloc()
        pages_obj = alloc()
        type0 = alloc()
        cidfont = alloc()
        descriptor = alloc()
        fontfile = alloc()
        tounicode = alloc()

        # 字体文件（子集 TTF，FlateDecode 压一下）
        import zlib
        stream(fontfile, f"/Length1 {len(subset)} /Filter /FlateDecode",
               zlib.compress(subset, 9))

        bbox = self.font.bbox
        put(descriptor, (f"{descriptor} 0 obj\n<< /Type /FontDescriptor "
                         f"/FontName /{self.font_name} /Flags 4 "
                         f"/FontBBox [{bbox[0]} {bbox[1]} {bbox[2]} {bbox[3]}] "
                         f"/ItalicAngle 0 /Ascent {self.font.ascender} "
                         f"/Descent {self.font.descender} "
                         f"/CapHeight {int(self.font.ascender * 0.7)} /StemV 80 "
                         f"/FontFile2 {fontfile} 0 R >>\nendobj\n").encode())

        # W 数组：每 8 个一组 [start [w1 w2 ...]]
        # ⚠️ 宽度必须用「该字符在原字体里的 advance」，不能用子集 GID 去查原字体 ——
        # 子集 GID 是重编号后的，拿它当原字体索引会查到完全不相干的字形宽度，
        # 渲染器按错误宽度推进，整行字就叠在一起（实测视觉模型报「字严重重叠」）。
        cid_adv = {cid: self.font.advance(self.font.gid(c))
                   for c, cid in cp2cid.items()}
        w_parts = []
        for i in range(0, len(used_cids), 8):
            chunk = used_cids[i:i + 8]
            widths = " ".join(str(min(cid_adv.get(c, 1000), 0xFFFF)) for c in chunk)
            w_parts.append(f"{chunk[0]} [{widths}]")
        put(cidfont, (f"{cidfont} 0 obj\n<< /Type /Font /Subtype /CIDFontType2 "
                      f"/BaseFont /{self.font_name} "
                      f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
                      f"/Supplement 0 >> "
                      f"/FontDescriptor {descriptor} 0 R "
                      f"/CIDToGIDMap /Identity /DW 1000 "
                      f"/W [{' '.join(w_parts)}] >>\nendobj\n").encode())

        tou = _build_tounicode({ord(c): cid for c, cid in cp2cid.items()})
        stream(tounicode, "", tou)

        put(type0, (f"{type0} 0 obj\n<< /Type /Font /Subtype /Type0 "
                    f"/BaseFont /{self.font_name} /Encoding /Identity-H "
                    f"/DescendantFonts [{cidfont} 0 R] "
                    f"/ToUnicode {tounicode} 0 R >>\nendobj\n").encode())

        page_ids = []
        for idx, pg in enumerate(pages, 1):
            page_id = alloc()
            content_id = alloc()
            page_ids.append(page_id)

            ops = ["BT"]
            upem = self.font.units_per_em
            for ln in pg.lines:
                ops.append(f"/F1 {ln.size:.2f} Tf")
                if not ln.gaps and not ln.squeeze and ln.line_scale == 1.0:
                    # 快路径：整行一次 Tj（绝大多数行都走这里）
                    cids = "".join(f"{cp2cid.get(ch, 0):04X}" for ch in ln.text)
                    ops.append(f"1 0 0 1 {ln.x:.2f} {ln.y:.2f} Tm")
                    ops.append(f"<{cids}> Tj")
                    continue
                # 慢路径：有中英间距 / 标点挤压 / 整行微缩，逐字定位
                if ln.line_scale != 1.0:
                    ops.append(f"{ln.line_scale * 100:.1f} Tz")
                cx = ln.x
                for i, ch in enumerate(ln.text):
                    cx += ln.gaps.get(i, 0.0)
                    cw = self.font.advance(self.font.gid(ch)) * ln.size / upem
                    sc = ln.squeeze.get(i, 1.0)
                    ops.append(f"1 0 0 1 {cx:.2f} {ln.y:.2f} Tm")
                    if sc != 1.0:
                        ops.append(f"{sc * 100:.1f} Tz")
                    ops.append(f"<{cp2cid.get(ch, 0):04X}> Tj")
                    if sc != 1.0:
                        ops.append(f"{ln.line_scale * 100:.1f} Tz"
                                   if ln.line_scale != 1.0 else "100 Tz")
                    cx += cw * sc
                if ln.line_scale != 1.0:
                    ops.append("100 Tz")
            ops.append("ET")

            # 页码
            if len(pages) > 1:
                label = f"- {idx} -"
                size = 9.0
                w = _width(self.font, label, size)
                cids = "".join(f"{cp2cid.get(ch, 0):04X}" for ch in label)
                ops.append("BT")
                ops.append(f"/F1 {size:.2f} Tf")
                ops.append(f"1 0 0 1 {(spec.width - w) / 2:.2f} "
                           f"{spec.margin_bottom * 0.5:.2f} Tm")
                ops.append(f"<{cids}> Tj")
                ops.append("ET")

            page_images: list[int] = []
            for data, fmt, x, y, dw, dh in pg.images:
                img_id = alloc()
                page_images.append(img_id)
                # ⚠️ PDF 里 /Width /Height 必须是**图片像素尺寸**，不是显示尺寸。
                # 写成显示尺寸会让解码器按错误尺寸解 JPEG，图直接画不出来（实测封面丢失）。
                from ..formats import png as pngmod
                iw, ih, _ifmt = pngmod.image_size(data)
                if not iw or not ih:
                    iw, ih = int(dw), int(dh)
                if fmt == "jpeg":
                    from .writer import _jpeg_components
                    comps = _jpeg_components(data)
                    cs = {1: "/DeviceGray", 4: "/DeviceCMYK"}.get(comps, "/DeviceRGB")
                    extra = " /Decode [1 0 1 0 1 0 1 0]" if comps == 4 else ""
                    stream(img_id, f"/Type /XObject /Subtype /Image /Width {iw} "
                                    f"/Height {ih} /ColorSpace {cs} "
                                    f"/BitsPerComponent 8 /Filter /DCTDecode{extra}", data)
                    ops.append(f"q {dw:.2f} 0 0 {dh:.2f} {x:.2f} {y:.2f} cm /Im{img_id} Do Q")
                else:
                    info = pngmod.probe(data)
                    if info.passthrough_ok:
                        body = pngmod.idat_bytes(data)
                        stream(img_id, f"/Type /XObject /Subtype /Image /Width {info.width} "
                                        f"/Height {info.height} "
                                        f"/ColorSpace /{info.colorspace} "
                                        f"/BitsPerComponent 8 /Filter /FlateDecode "
                                        f"/DecodeParms << /Predictor 15 "
                                        f"/Colors {info.channels} /BitsPerComponent 8 "
                                        f"/Columns {info.width} >>", body)
                    else:
                        px, inf, ch = pngmod.decode(data)
                        cs = "/DeviceGray" if ch in (1, 2) else "/DeviceRGB"
                        if ch == 2:
                            px = bytes(px[i] for i in range(0, len(px), 2))
                        elif ch == 4:
                            px = bytes(b for i in range(0, len(px), 4) for b in px[i:i + 3])
                        stream(img_id, f"/Type /XObject /Subtype /Image /Width {inf.width} "
                                        f"/Height {inf.height} /ColorSpace {cs} "
                                        f"/BitsPerComponent 8 /Filter /FlateDecode",
                               zlib.compress(px, 6))
                    ops.append(f"q {dw:.2f} 0 0 {dh:.2f} {x:.2f} {y:.2f} cm /Im{img_id} Do Q")

            content = "\n".join(ops).encode("latin-1")
            stream(content_id, "", content)

            xobj = ""
            if page_images:
                entries = " ".join(f"/Im{i} {i} 0 R" for i in page_images)
                xobj = f"/XObject << {entries} >> "
            put(page_id, (f"{page_id} 0 obj\n<< /Type /Page /Parent {pages_obj} 0 R "
                          f"/MediaBox [0 0 {spec.width:.2f} {spec.height:.2f}] "
                          f"/Resources << /Font << /F1 {type0} 0 R >> {xobj}>> "
                          f"/Contents {content_id} 0 R >>\nendobj\n").encode())

        put(pages_obj, (f"{pages_obj} 0 obj\n<< /Type /Pages /Count {len(page_ids)} "
                        f"/Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] >>\nendobj\n").encode())
        put(catalog, f"{catalog} 0 obj\n<< /Type /Catalog /Pages {pages_obj} 0 R >>\nendobj\n".encode())

        info_id = 0
        if title:
            info_id = alloc()
            put(info_id, f"{info_id} 0 obj\n<< /Title {_pdf_str(title)} "
                         f"/Producer (BookForge) >>\nendobj\n".encode())

        return self._serialize(objs, nxt, catalog, info_id)

    def _serialize(self, objs: dict[int, bytes], size: int,
                   root: int, info: int) -> bytes:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for num in sorted(objs):
            offsets[num] = len(out)
            out += objs[num]
        xref_at = len(out)
        out += f"xref\n0 {size}\n".encode()
        out += b"0000000000 65535 f \n"
        for num in range(1, size):
            out += f"{offsets.get(num, 0):010d} 00000 n \n".encode()
        trailer = f"<< /Size {size} /Root {root} 0 R"
        if info:
            trailer += f" /Info {info} 0 R"
        trailer += " >>"
        out += f"trailer\n{trailer}\nstartxref\n{xref_at}\n%%EOF\n".encode()
        return bytes(out)
