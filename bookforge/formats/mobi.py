"""MOBI 读写（PalmDB + PalmDOC / PalmDOC 压缩）。

MOBI 本质是 Palm 数据库：78 字节库头 + 记录头表 + 一堆记录。
记录 0 装的是 PalmDOC 头 + MOBI 头（+ EXTH 元数据），
记录 1..N 是正文，按 4096 字节分块、用 PalmDOC 算法压缩，
图片记录排在 first_image_index 之后。

只做 MOBI（KF8/AZW3 明确报错）。压缩只支持 None 和 PalmDOC，
遇到 HUFF/CDIC 明确报错 —— 不猜。

读 → TextBook（HTML 里的段落/标题/图片）
写 ← TextBook（HTML 拼装 → PalmDOC 分块，compression=1 不压缩，最稳）
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..model import Chapter, ImageBlock, Paragraph, TextBook

PALMDOC_HEADER_LEN = 16
RECORD_SIZE = 4096

# compression
COMP_NONE = 1
COMP_PALMDOC = 2
COMP_HUFF = 17480


class MOBIError(ValueError):
    pass


# ── PalmDOC 解压 / 压缩 ───────────────────────────────────────────────────

def decompress_palmdoc(data: bytes) -> bytes:
    """PalmDOC (LZ77 变体) 解压。"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0:
            out.append(0)
        elif c <= 8:
            out += data[i:i + c]
            i += c
        elif c <= 0x7F:
            out.append(c)
        elif c <= 0xBF:
            if i >= n:
                break
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x07FF
            length = (pair & 0x07) + 3
            if distance == 0 or distance > len(out):
                raise MOBIError("PalmDOC 压缩数据损坏（回溯距离越界）")
            start = len(out) - distance
            for k in range(length):
                out.append(out[start + k])
        else:
            out.append(0x20)
            out.append(c ^ 0x80)
    return bytes(out)


def compress_palmdoc(data: bytes) -> bytes:
    """PalmDOC 压缩。

    实现最直接的那种：优先用 2 字节的距离/长度对（能省就省），
    否则原样输出、短的连续字面量用 1~8 的前缀块打包。
    不做最优匹配搜索 —— mobi 主要是给阅读器读，体积不是第一优先，
    而且生成端的正确性比压缩率重要得多。
    """
    out = bytearray()
    i = 0
    n = len(data)
    lit_start = 0

    def flush_literal(end: int) -> None:
        nonlocal lit_start
        while lit_start < end:
            chunk = min(8, end - lit_start)
            out.append(chunk)
            # ⚠️ 这里必须是 out.extend(...)，不能用 out += ...
            # 闭包里的增强赋值会把 out 变成本地变量 → UnboundLocalError
            out.extend(data[lit_start:lit_start + chunk])
            lit_start += chunk

    while i < n:
        c = data[i]
        # 1) 空格 + 大写字母 的经典组合
        if (c == 0x20 and i + 1 < n and 0x40 <= data[i + 1] <= 0x7F):
            flush_literal(i)
            out.append(data[i + 1] ^ 0x80)
            i += 2
            lit_start = i
            continue

        # 2) 在最近 2047 字节里找 ≥3 字节的匹配
        best_len = 0
        best_dist = 0
        window = max(0, i - 2047)
        limit = min(n - i, 10)
        if limit >= 3:
            for dist in range(1, i - window + 1):
                start = i - dist
                ln = 0
                while ln < limit and data[start + ln] == data[i + ln]:
                    ln += 1
                if ln > best_len:
                    best_len = ln
                    best_dist = dist
                    if ln == limit:
                        break
        if best_len >= 3:
            flush_literal(i)
            pair = ((best_dist & 0x07FF) << 3) | ((best_len - 3) & 0x07)
            # ⚠️ 距离/长度对的首字节必须落在 0x80~0xBF。
            # 写成 0xC0 会落进「空格+字符」的范围，解压端按另一套语义解，
            # 结果是垃圾数据（实测：200 个空格压出来变成 '@\x0f' 之类）。
            out.append(0x80 | ((pair >> 8) & 0x3F))
            out.append(pair & 0xFF)
            i += best_len
            lit_start = i
            continue

        i += 1

    flush_literal(n)
    return bytes(out)


# ── PalmDB ────────────────────────────────────────────────────────────────

@dataclass
class PalmDB:
    name: str
    type: bytes
    creator: bytes
    records: list[bytes] = field(default_factory=list)


def read_palmdb(path: str | Path) -> PalmDB:
    data = Path(path).read_bytes()
    if len(data) < 78:
        raise MOBIError("文件太小，不是 PalmDB")
    name = data[:32].split(b"\x00")[0].decode("latin-1", "replace")
    db_type = data[60:64]
    creator = data[64:68]
    num_records = struct.unpack(">H", data[76:78])[0]
    if num_records <= 0 or num_records > 100000:
        raise MOBIError(f"PalmDB 记录数异常：{num_records}")

    offsets = []
    pos = 78
    for _ in range(num_records):
        if pos + 8 > len(data):
            raise MOBIError("PalmDB 记录头被截断")
        off = struct.unpack(">I", data[pos:pos + 4])[0]
        offsets.append(off)
        pos += 8
    offsets.append(len(data))

    records = []
    for i in range(num_records):
        s, e = offsets[i], offsets[i + 1]
        if s > e or e > len(data):
            raise MOBIError("PalmDB 记录偏移非法")
        records.append(data[s:e])
    return PalmDB(name=name, type=db_type, creator=creator, records=records)


# ── MOBI 头 ───────────────────────────────────────────────────────────────

@dataclass
class MobiHeader:
    compression: int
    text_length: int
    text_records: int
    encryption: int
    encoding: int
    version: int
    first_image: int
    title_offset: int
    exth_flags: int
    metadata: dict = field(default_factory=dict)


def parse_header(db: PalmDB) -> MobiHeader:
    if not db.records:
        raise MOBIError("PalmDB 没有记录")
    r0 = db.records[0]
    if len(r0) < PALMDOC_HEADER_LEN + 8:
        raise MOBIError("记录 0 太小，缺 PalmDOC/MOBI 头")
    compression, _, text_length, text_records, _, encryption, _ = struct.unpack(
        ">HHIHHHH", r0[:16])
    if r0[16:20] != b"MOBI":
        raise MOBIError("记录 0 里找不到 MOBI 标识（可能是纯 PalmDOC）")
    header_len = struct.unpack(">I", r0[20:24])[0]
    mobi_type = struct.unpack(">I", r0[24:28])[0]
    encoding = struct.unpack(">I", r0[28:32])[0]
    version = struct.unpack(">I", r0[36:40])[0]

    def u32(off: int, default: int = 0) -> int:
        if off + 4 > len(r0):
            return default
        return struct.unpack(">I", r0[off:off + 4])[0]

    first_image = u32(108, 0xFFFFFFFF)
    if first_image == 0xFFFFFFFF:
        first_image = 0
    exth_flags = u32(128, 0)
    title_offset = u32(84, 0)

    if version >= 8:
        raise MOBIError(
            f"这是 KF8/AZW3（MOBI version {version}），暂不支持。"
            "AZW3 的碎片化 HTML + FDST/INDX 表还没实现")

    meta: dict = {}
    # EXTH
    if exth_flags & 0x40:
        exth_off = 16 + header_len
        if exth_off + 12 <= len(r0) and r0[exth_off:exth_off + 4] == b"EXTH":
            count = struct.unpack(">I", r0[exth_off + 8:exth_off + 12])[0]
            p = exth_off + 12
            for _ in range(count):
                if p + 8 > len(r0):
                    break
                rtype, rlen = struct.unpack(">II", r0[p:p + 8])
                body = r0[p + 8:p + rlen]
                if rtype == 100 and body:            # author
                    meta["author"] = body.decode("utf-8", "replace")
                elif rtype == 503:                   # updated title
                    meta["title"] = body.decode("utf-8", "replace")
                elif rtype == 524:                   # language
                    meta["language"] = body.decode("utf-8", "replace")
                p += rlen

    # 全名：header 里存 offset + length（相对记录 0），名字区只放正文
    # 记录 0 偏移 84 = 名字 offset，88 = 名字长度；换算到 MOBI 头内是 68 / 72
    title_offset = u32(84, 0)
    title_len = u32(88, 0)
    if title_offset and title_len and title_offset + title_len <= len(r0):
        raw = r0[title_offset:title_offset + title_len]
        enc = "utf-8" if encoding == 65001 else "cp1252"
        if raw:
            meta.setdefault("title", raw.decode(enc, "replace"))

    return MobiHeader(compression=compression, text_length=text_length,
                      text_records=text_records, encryption=encryption,
                      encoding=encoding, version=version,
                      first_image=first_image, title_offset=title_offset,
                      exth_flags=exth_flags, metadata=meta)


# ── 读：MOBI → TextBook ───────────────────────────────────────────────────

def _html_to_blocks(html: str) -> list:
    """MOBI 的正文是 HTML 片段，粗粒度转成段落/标题/图片。"""
    blocks: list = []
    # 图片：<img recindex="00003"> （MOBI 用记录号引用图片）
    img_re = re.compile(r'<img[^>]*recindex\s*=\s*"?(\d+)"?[^>]*>', re.I)
    # 先把图片挖出来当占位符，避免被段落切碎
    placeholders: dict[str, str] = {}

    def stash(m: re.Match) -> str:
        key = f"\x00IMG{m.group(1)}\x00"
        placeholders[key] = m.group(1)
        return key

    html = img_re.sub(stash, html)
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
    html = re.sub(r"</(p|div|h[1-6]|li|blockquote)>", "\n", html, flags=re.I)
    html = re.sub(r"<h[1-6][^>]*>", "\n\x01", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)

    for line in html.split("\n"):
        line = line.strip()
        if not line:
            continue
        is_head = line.startswith("\x01")
        line = line.replace("\x01", "").strip()
        if not line:
            continue
        # 还原图片占位
        parts = re.split(r"(\x00IMG\d+\x00)", line)
        for part in parts:
            m = re.fullmatch(r"\x00IMG(\d+)\x00", part)
            if m:
                blocks.append(("img", int(m.group(1))))
            elif part.strip():
                blocks.append(("h" if is_head else "p", part.strip()))
    return blocks


def read(path: str | Path) -> TextBook:
    db = read_palmdb(path)
    hdr = parse_header(db)
    if hdr.encryption != 0:
        raise MOBIError("这个 mobi 有 DRM 加密，不支持")
    if hdr.compression == COMP_HUFF:
        raise MOBIError("这个 mobi 用 HUFF/CDIC 压缩，暂不支持（P3 只做 PalmDOC）")
    if hdr.compression not in (COMP_NONE, COMP_PALMDOC):
        raise MOBIError(f"未知的 mobi 压缩方式：{hdr.compression}")

    enc = "utf-8" if hdr.encoding == 65001 else "cp1252"
    parts: list[bytes] = []
    for i in range(1, min(hdr.text_records + 1, len(db.records))):
        rec = db.records[i]
        if hdr.compression == COMP_PALMDOC:
            rec = decompress_palmdoc(rec)
        elif rec[:2] == b"\x00\x00":       # 未压缩但带尾部标记的记录
            rec = rec[2:]
        parts.append(rec)
    text = b"".join(parts)[:hdr.text_length or None].decode(enc, "replace")

    blocks = _html_to_blocks(text)

    # 图片记录：first_image 之后的记录，按 recindex 映射
    images: dict[int, bytes] = {}
    for idx in range(hdr.first_image, len(db.records)):
        data = db.records[idx]
        if len(data) < 12:
            continue
        w, h, kind = _image_size(data)
        if not w or not h:
            continue
        images[idx - hdr.first_image + 1] = data    # recindex 从 1 开始

    chapters: list[Chapter] = []
    cur = Chapter(title="")
    for kind, value in blocks:
        if kind == "h":
            if cur.blocks or cur.title:
                chapters.append(cur)
            cur = Chapter(title=str(value))
        elif kind == "img":
            data = images.get(int(value))
            if data:
                w, h, fmt = _image_size(data)
                cur.blocks.append(ImageBlock(data=data, fmt=fmt, width=w, height=h))
        else:
            cur.blocks.append(Paragraph(text=str(value)))
    if cur.blocks or cur.title:
        chapters.append(cur)
    if not chapters:
        raise MOBIError("mobi 里没有可提取的正文")

    meta = dict(hdr.metadata)
    meta.setdefault("title", db.name)
    return TextBook(chapters=chapters, metadata=meta, source_format="mobi")


def to_comic(path: str | Path):
    """MOBI → ComicBook（漫画 mobi：图片就是页）。没有图就说不像漫画。"""
    from ..model import ComicBook, Page

    book = read(path)
    imgs = [b for ch in book.chapters for b in ch.blocks if isinstance(b, ImageBlock)]
    if len(imgs) < 2:
        raise MOBIError(
            f"这个 mobi 只有 {len(imgs)} 张图，不像漫画；"
            "小说 mobi 请转 txt / epub / fb2")
    pages = [Page(data=b.data, fmt=b.fmt, width=b.width, height=b.height)
             for b in imgs]
    return ComicBook(pages=pages, metadata=book.metadata, source_format="mobi")


def _image_size(data: bytes) -> tuple[int, int, str]:
    from . import png
    return png.image_size(data)


# ── 写：TextBook → MOBI ───────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _build_html(book: TextBook) -> tuple[bytes, list[bytes]]:
    """TextBook → (正文 HTML 字节, 图片字节列表)。图片用 recindex 引用。

    ⚠️ 必须是**完整 HTML 文档**（<html><head><guide></guide></head><body>…）。
    只写 <h1>/<p> 片段时，MuPDF 这类严格实现只解析出第一段，
    实测「转出来只有标题」。真实 mobi 的正文开头就是这个结构。
    """
    out: list[str] = ["<html><head><guide></guide></head><body>"]
    images: list[bytes] = []
    for ch in book.chapters:
        if ch.title:
            out.append(f"<h1>{_esc(ch.title)}</h1>")
        for block in ch.blocks:
            if isinstance(block, Paragraph):
                if block.text.strip():
                    out.append(f"<p>{_esc(block.text)}</p>")
            elif isinstance(block, ImageBlock):
                images.append(block.data)
                w, h, _ = _image_size(block.data)
                dims = f' width="{w}" height="{h}"' if w and h else ""
                out.append(f'<p><img recindex="{len(images):05d}"{dims}/></p>'
                           "<mbp:pagebreak/>")
    out.append("</body></html>")
    return "".join(out).encode("utf-8"), images


def to_bytes(book: TextBook) -> bytes:
    """TextBook → MOBI（compression=1 不压缩，最稳；PalmDOC 压缩在读取端已实现）。"""
    html, images = _build_html(book)
    title = (book.metadata or {}).get("title") or "未命名"
    author = (book.metadata or {}).get("author") or ""

    # 正文按 4096 切块（不切断 UTF-8 多字节字符）
    text_records: list[bytes] = []
    pos = 0
    while pos < len(html):
        end = min(pos + RECORD_SIZE, len(html))
        while end < len(html) and (html[end] & 0xC0) == 0x80:
            end -= 1
        if end <= pos:
            end = min(pos + RECORD_SIZE, len(html))
        text_records.append(html[pos:end])
        pos = end
    if not text_records:
        text_records = [b""]

    first_image = 1 + len(text_records)          # 记录 0 是头
    image_records = images

    # ── 记录 0：PalmDOC 头 + MOBI 头 + EXTH + 全名 ──
    exth_items: list[tuple[int, bytes]] = []
    if author:
        exth_items.append((100, author.encode("utf-8")))
    exth_items.append((503, title.encode("utf-8")))
    exth_items.append((524, b"zh"))
    exth_body = b"".join(struct.pack(">II", t, 8 + len(b)) + b for t, b in exth_items)
    exth = b"EXTH" + struct.pack(">II", 12 + len(exth_body), len(exth_items)) + exth_body
    exth = exth + b"\x00" * ((4 - len(exth) % 4) % 4)
    exth_flags = 0x40

    mobi_len = 232
    full_name = title.encode("utf-8")
    title_off = PALMDOC_HEADER_LEN + mobi_len + len(exth)

    r0 = bytearray()
    r0 += struct.pack(">HHIHHHH", COMP_NONE, 0, len(html), len(text_records),
                      RECORD_SIZE, 0, 0)
    mobi = bytearray(mobi_len)
    mobi[0:4] = b"MOBI"
    struct.pack_into(">I", mobi, 4, mobi_len)
    struct.pack_into(">I", mobi, 8, 2)                 # mobi type: book
    struct.pack_into(">I", mobi, 12, 65001)            # UTF-8
    struct.pack_into(">I", mobi, 16, 0x12345678)       # unique id
    struct.pack_into(">I", mobi, 20, 6)                # file version
    # 字段偏移对照真实 mobi 实测（记录 0 偏移 → mobi 数组偏移要 -16）：
    #   84 全名 offset / 88 全名长度 / 92 locale / 104 最后内容记录 / 108 首图记录
    struct.pack_into(">I", mobi, 68, title_off)        # full name offset(84)
    struct.pack_into(">I", mobi, 72, len(full_name))   # full name length(88)
    struct.pack_into(">I", mobi, 76, 0x0804)           # locale: zh-CN(92)
    struct.pack_into(">I", mobi, 88, len(text_records))  # last content index(104)
    struct.pack_into(">I", mobi, 92, first_image)      # first image index(108)
    struct.pack_into(">I", mobi, 112, exth_flags)      # EXTH flags(128)
    r0 += mobi
    r0 += exth
    r0 += full_name
    r0 += b"\x00" * ((4 - len(r0) % 4) % 4)

    records = [bytes(r0)] + text_records + image_records

    # ── PalmDB 容器 ──
    # ⚠️ PalmDB 的库名是 32 字节单字节（Latin-1/ASCII），塞不下中文。
    # 中文写 UTF-8 进去，读出来是「æªå½å」这种乱码。
    # 真正的标题走 EXTH + full name（UTF-8），这里只放 ASCII 兜底名。
    safe = "".join(c for c in title if 32 <= ord(c) < 127).strip()[:31] or "book"
    name = safe.encode("ascii") + b"\x00"
    name = name + b"\x00" * (32 - len(name))
    head = bytearray()
    head += name
    head += struct.pack(">HH", 0, 0)                   # attributes, version
    head += struct.pack(">III", 0, 0, 0)               # created, modified, backup
    head += struct.pack(">I", 0)                       # modnum
    head += struct.pack(">I", 0)                       # appinfo
    head += struct.pack(">I", 0)                       # sortinfo
    head += b"BOOK"                                    # type
    head += b"MOBI"                                    # creator
    head += struct.pack(">I", 0)                       # uniqueIDseed
    head += struct.pack(">I", 0)                       # nextRecordList
    head += struct.pack(">H", len(records))
    assert len(head) == 78, len(head)

    offset = 78 + 8 * len(records) + 2                  # +2 是 2 字节对齐填充
    table = bytearray()
    body = bytearray()
    for i, rec in enumerate(records):
        table += struct.pack(">I", offset + len(body))
        table += struct.pack(">I", (i << 24) & 0xFFFFFF00)
        body += rec
    out = bytes(head) + bytes(table) + b"\x00\x00" + bytes(body)
    return out
