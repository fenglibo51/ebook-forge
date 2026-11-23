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

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..model import IMAGE_EXTS, ComicBook, Page, normalize_fmt
from . import limits, png

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "comicinfo.xml"}

# 解压炸弹防护：用 zipinfo.file_size（压缩包里声明的解压后大小）预判，
# 不需要真解压就能拦住「几百 KB 解出几十 GB」的包。
# 限额统一由 formats/limits.py 管，这里保留别名方便老代码/环境变量兼容
MAX_TOTAL_BYTES = limits.MAX_TOTAL_BYTES
MAX_PAGE_BYTES = limits.MAX_ENTRY_BYTES


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
    # 元数据文件也可能是个几 GB 的炸弹。check_zip 已经查过一遍，
    # 但那道闸在校验顺序被改动时会失效 —— 高风险路径自己再卡一次。
    try:
        info = zf.getinfo(target)
        if info.file_size > limits.MAX_ENTRY_BYTES:
            return {}
        blob = zf.read(target)
        if len(blob) > limits.MAX_ENTRY_BYTES:
            return {}
        root = ET.fromstring(blob)
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
        try:
            limits.check_zip(zf)          # 条目数 + 单项 + 累计声明大小
        except limits.LimitError as e:
            raise ValueError(str(e)) from e
        names = [n for n in zf.namelist()
                 if not n.endswith("/") and not _is_junk(n)
                 and Path(n).suffix.lower() in IMAGE_EXTS]
        # 重名条目（zip 规范允许）只取一次，避免同一张图重复成两页
        names = list(dict.fromkeys(names))
        names.sort(key=natural_key)

        total = sum(zf.getinfo(n).file_size for n in names)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(
                f"压缩包解压后约 {total / 2**30:.1f} GB，超过上限 "
                f"{MAX_TOTAL_BYTES / 2**30:.0f} GB（BOOKFORGE_MAX_TOTAL_MB 可调）")

        pages: list[Page] = []
        for n in names:
            info = zf.getinfo(n)
            if info.file_size > MAX_PAGE_BYTES:
                raise ValueError(
                    f"单页 {n} 解压后 {info.file_size / 2**20:.0f} MB，超过上限 "
                    f"{MAX_PAGE_BYTES / 2**20:.0f} MB")
            data = zf.read(n)
            if len(data) > MAX_PAGE_BYTES:
                raise ValueError(
                    f"单页 {n} 实际解压 {len(data) / 2**20:.0f} MB，"
                    f"超过上限 {MAX_PAGE_BYTES / 2**20:.0f} MB")
            w, h, fmt = png.image_size(data)
            pages.append(Page(
                data=data,
                fmt=fmt or normalize_fmt(Path(n).suffix),
                width=w, height=h, name=n,
            ))
        meta = _read_comicinfo(zf)

    return ComicBook(pages=pages, metadata=meta, source_format="cbz")


def _safe_entry_name(name: str, fallback: str) -> str:
    """keep_names 时消毒条目名：去掉绝对路径和 ..，避免解压时逃出目标目录。"""
    parts = [p for p in name.replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    clean = "/".join(parts)
    return clean or fallback


def read_cb7(path: str | Path) -> ComicBook:
    """CB7 / CBC → ComicBook。7z 里可能是图片，也可能嵌着 cbz（CBC 集合）。"""
    from . import sevenz

    path = Path(path)
    # 一个包一次转换只建一份预算：外层 7z 解出多少先扣掉，内层 ZIP 只能用剩下的。
    # 早先是 read_all() 自己按完整额度解一遍、内层再从零拿一份 —— 2 层嵌套就能
    # 吃掉 2 倍内存，跟「跨层共享」的意图正好相反。
    budget = limits.Budget()
    files = sevenz.read_all(path, budget=budget)

    # CBC 里可能嵌着 cbz/zip：递归一层
    nested = [n for n in files if Path(n).suffix.lower() in (".cbz", ".zip")]
    if nested and not any(Path(n).suffix.lower() in IMAGE_EXTS for n in files):
        import io
        merged: list[Page] = []
        for n in sorted(nested, key=natural_key):
            try:
                with zipfile.ZipFile(io.BytesIO(files[n])) as zf:
                    try:
                        limits.check_zip(zf, budget)
                    except limits.LimitError as e:
                        raise ValueError(f"内层 {n}：{e}") from e
                    inner = [x for x in zf.namelist()
                             if not x.endswith("/") and not _is_junk(x)
                             and Path(x).suffix.lower() in IMAGE_EXTS]
                    inner.sort(key=natural_key)
                    for x in inner:
                        data = zf.read(x)
                        # 头部声明会撒谎，读出来按实际字节再卡一次
                        if len(data) > limits.MAX_ENTRY_BYTES:
                            raise ValueError(
                                f"内层 {n} 的 {x} 实际解压 "
                                f"{len(data) / 2**20:.0f} MB，超过单文件上限 "
                                f"{limits.MAX_ENTRY_BYTES / 2**20:.0f} MB")
                        w, h, fmt = png.image_size(data)
                        merged.append(Page(data=data,
                                           fmt=fmt or normalize_fmt(Path(x).suffix),
                                           width=w, height=h, name=x))
            except zipfile.BadZipFile:
                continue
        if merged:
            return ComicBook(pages=merged, metadata={"title": path.stem},
                             source_format=path.suffix.lstrip("."))

    names = [n for n in files
             if not _is_junk(n) and Path(n).suffix.lower() in IMAGE_EXTS]
    names.sort(key=natural_key)
    pages: list[Page] = []
    for n in names:
        data = files[n]
        w, h, fmt = png.image_size(data)
        pages.append(Page(data=data, fmt=fmt or normalize_fmt(Path(n).suffix),
                          width=w, height=h, name=n))
    return ComicBook(pages=pages, metadata={"title": path.stem},
                     source_format=path.suffix.lstrip("."))


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
                name = _safe_entry_name(page.name, f"{i:0{width}d}{ext}")
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
