"""CBZ（漫画图片 zip）读写。

CBZ = ZIP，所以读就是解 zip、写就是打包。关键是**图片字节原样进出**：
不解码、不重压、不改名以外的任何东西。

两个必须做对的地方：

1. 页序 —— 漫画包常见 1.jpg / 2.jpg / 10.jpg 这种命名，
   按字符串排会把 10 排在 2 前面。用自然排序。
2. 垃圾条目 —— __MACOSX/、.DS_Store、Thumbs.db 混在里面会被当成页，
   必须过滤，否则 CBZ 里会多出几张黑图。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..model import IMAGE_EXTS, ComicBook, Page, normalize_fmt
from . import png

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "comicinfo.xml"}

# 图片已压缩，再 deflate 一遍只是浪费时间；PNG/BMP 这种反而能省点空间
_STORED_EXTS = {".jpg", ".jpeg", ".jp2", ".jpx", ".gif", ".webp"}


def natural_key(s: str):
    """1.jpg / 2.jpg / 10.jpg → 按数字大小排"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


def _is_junk(name: str) -> bool:
    low = name.lower()
    if low.startswith("__macosx/"):
        return True
    base = low.rsplit("/", 1)[-1]
    if base in JUNK_NAMES:
        return True
    if base.startswith("._"):
        return True
    return False


def _read_comicinfo(zf: zipfile.ZipFile) -> dict:
    """ComicInfo.xml → metadata。读不出就返回空 dict，不影响转换。"""
    target = None
    for n in zf.namelist():
        if n.lower().endswith("comicinfo.xml"):
            target = n
            break
    if not target:
        return {}
    try:
        root = ET.fromstring(zf.read(target))
    except Exception:
        return {}
    mapping = {"Title": "title", "Series": "series", "Number": "number",
               "Writer": "author", "Publisher": "publisher", "LanguageISO": "language"}
    meta = {}
    for tag, key in mapping.items():
        el = root.find(tag)
        if el is not None and el.text:
            meta[key] = el.text.strip()
    return meta


def read(path: str | Path) -> ComicBook:
    """CBZ / ZIP → ComicBook（图片原字节）"""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist()
                 if not n.endswith("/") and not _is_junk(n)
                 and Path(n).suffix.lower() in IMAGE_EXTS]
        names.sort(key=natural_key)

        pages: list[Page] = []
        for n in names:
            data = zf.read(n)
            w, h, fmt = png.image_size(data)
            pages.append(Page(
                data=data,
                fmt=fmt or normalize_fmt(Path(n).suffix),
                width=w, height=h, name=n,
            ))
        meta = _read_comicinfo(zf)

    return ComicBook(pages=pages, metadata=meta, source_format="cbz")


def write(book: ComicBook, path: str | Path, *, keep_names: bool = False) -> Path:
    """ComicBook → CBZ。

    keep_names=False（默认）会把页面重命名成 00001.jpg 保证阅读顺序。
    重命名只改条目名，**图片字节不动**。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    width = max(5, len(str(len(book.pages))))

    with zipfile.ZipFile(path, "w") as zf:
        for i, page in enumerate(book.pages, 1):
            ext = "." + page.fmt.replace("jpeg", "jpg")
            if keep_names and page.name:
                name = page.name
            else:
                name = f"{i:0{width}d}{ext}"
            comp = (zipfile.ZIP_STORED if ext.lower() in _STORED_EXTS
                    else zipfile.ZIP_DEFLATED)
            zf.writestr(name, page.data, compress_type=comp)

        info = _comicinfo_xml(book)
        if info:
            zf.writestr("ComicInfo.xml", info.encode("utf-8"),
                        compress_type=zipfile.ZIP_DEFLATED)
    return path


def _comicinfo_xml(book: ComicBook) -> str:
    m = book.metadata or {}
    if not any(m.get(k) for k in ("title", "series", "author", "number")):
        return ""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
    for tag, key in (("Title", "title"), ("Series", "series"), ("Number", "number"),
                     ("Writer", "author"), ("Publisher", "publisher"),
                     ("LanguageISO", "language")):
        if m.get(key):
            parts.append(f"  <{tag}>{esc(m[key])}</{tag}>")
    parts.append(f"  <PageCount>{len(book.pages)}</PageCount>")
    parts.append("</ComicInfo>")
    return "\n".join(parts)
