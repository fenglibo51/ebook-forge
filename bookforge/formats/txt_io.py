"""纯文本读写：编码探测 + 章节识别 + 分段。

中文 txt 的坑基本都在这里：

    GBK / GB18030 编码     国标 txt 主流，直接按 utf-8 读会整个乱码
    BOM                     UTF-8 BOM 会让第一行多出「\ufeff」
    CRLF / CR               老 Windows / 老 Mac 换行
    章节标记不统一           「第一章」「第 1 章」「Chapter 1」「楔子」「番外」
    用空行分段              中文小说几乎不用缩进标记段落边界

判定顺序：先看 BOM，再逐个编码试解（utf-8 → gb18030 → big5 → shift_jis），
全都失败才退回 latin-1（不丢字节，但可能显示乱码）。
"""

from __future__ import annotations

import re

from ..model import Chapter, Paragraph, TextBook

# 章节标题：整行只有标题，别把正文里出现的「第三章」当标题（要求整行匹配）
_CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百千零〇两]+\s*[章节節回卷篇部集话話]"
    r"(?:[\s:：、.·-]+.{0,30})?"          # 「第一章 风起」这种后缀
    r"|Chapter\s+\d+(?:[\s:：.-]+.{0,40})?"
    r"|序章|序言|楔子|引子|前言|后记|尾聲|尾声|終章|终章"
    r"|番外(?:篇)?(?:[\s:：、.·-]*.{0,20})?"
    r")\s*$",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"[ \t\u3000]+")


def detect_encoding(data: bytes) -> str:
    """返回可以直接用的 codec 名。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") and len(data) > 2 and data[2:4] != b"\x00\x00":
        return "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be"
    for enc in ("utf-8", "gb18030", "big5", "shift_jis"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def decode_text(data: bytes) -> tuple[str, str]:
    """→ (正文, 实际使用的编码名)"""
    enc = detect_encoding(data)
    text = data.decode(enc, "replace")
    # 有些文件是「BOM + BOM + 内容」（二次编码过），utf-8-sig 只吃掉第一个，
    # 剩下的 \ufeff 会让第一行匹配不上章节正则 —— 统一削掉。
    text = text.lstrip("\ufeff")
    # 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, enc


def split_chapters(text: str) -> list[Chapter]:
    """按章节标记切，识别不出就当一本单章（不硬切，避免把正文切碎）。"""
    lines = text.split("\n")
    chapters: list[Chapter] = []
    cur = Chapter(title="")
    buf: list[str] = []

    def flush_para() -> None:
        if not buf:
            return
        para = "".join(buf).strip()
        if para:
            cur.blocks.append(Paragraph(text=para))
        buf.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_para()
            continue
        if _CHAPTER_RE.match(stripped):
            flush_para()
            if cur.blocks or cur.title:
                chapters.append(cur)
            cur = Chapter(title=stripped)
            continue
        # 中文小说惯例：**每段都首行缩进**，所以缩进开头 = 新段落；
        # 不缩进的续行 = 同一段被硬换行拆开了，拼回去。
        # 早先写反了（把缩进当续行），结果两段被粘成一段。
        if line[:1] in (" ", "\t", "\u3000", "\u00a0"):
            flush_para()
            buf.append(stripped)
        else:
            buf.append(stripped)

    flush_para()
    if cur.blocks or cur.title:
        chapters.append(cur)
    if not chapters:
        chapters = [Chapter(title="")]
    return chapters


def read(path) -> TextBook:
    with open(path, "rb") as fh:
        data = fh.read()
    text, enc = decode_text(data)
    return TextBook(chapters=split_chapters(text),
                    metadata={"encoding": enc},
                    source_format="txt")


def to_bytes(book: TextBook, *, indent: bool = True) -> bytes:
    """TextBook → 纯文本。章节标题占一行，段落之间空一行。

    中文小说惯例是每段首行缩进 2 字符。读的时候 `strip()` 把缩进吃掉了，
    写的时候必须补回来，否则 txt → txt 会静默把原文的缩进丢干净
    （实测 28 字节变 26 字节，看着像"只是覆盖"，实际内容也变了）。
    纯英文段落不加缩进，避免英文书多出奇怪的首行空格。
    """
    out: list[str] = []
    for ch in book.chapters:
        if ch.title:
            out.append(ch.title)
            out.append("")
        for block in ch.blocks:
            para = getattr(block, "text", None)
            if not para:
                continue
            text = para
            if indent and _needs_indent(para):
                text = "\u3000\u3000" + para
            out.append(text)
            out.append("")
    return "\n".join(out).rstrip("\n").encode("utf-8") + b"\n"


def _needs_indent(para: str) -> bool:
    """中文段落才加缩进 —— 看开头几个字里有没有汉字。"""
    head = para[:10]
    return any("\u4e00" <= c <= "\u9fff" for c in head)
