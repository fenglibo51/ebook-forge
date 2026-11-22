"""BookForge —— 电子书 / 漫画格式转换引擎（纯标准库自研）。

不含 Calibre、不依赖 poppler / unar，也不调用任何外部二进制。

    from bookforge import convert
    r = convert("漫画.cbz", "pdf")
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .formats import cbz, epub, png
from .model import ComicBook
from .pdf.writer import A4, A5, PDFWriter

__version__ = "0.1.0"

COMIC_INPUTS = {".cbz", ".zip", ".cbr", ".cb7", ".cbc"}
# P0 支持的源（cbr/cb7/cbc 在 P2 接 7z / RAR 处理）
P0_INPUTS = {".cbz", ".zip", ".epub"}
P0_TARGETS = {"cbz", "zip", "epub", "pdf"}


class ConvertError(Exception):
    pass


def read_comic(path: str | Path) -> ComicBook:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".cbz", ".zip"):
        return cbz.read(path)
    if ext == ".epub":
        return epub.read(path)
    if ext in (".cbr", ".cb7", ".cbc"):
        raise ConvertError(f"{ext} 还没实现（P2 接 7z / RAR）")
    raise ConvertError(f"不支持的源格式 {ext}")


def convert(src: str | Path, target: str, *, out: str | Path | None = None,
            dpi: float = 300.0, fit: str = "native",
            page_size: str = "a5") -> dict:
    """转换单个漫画文件。

    target: cbz / zip / epub / pdf
    fit:    native（页面跟图片走，按 dpi 换算）或 fit（等比缩放进 page_size）
    dpi:    只在 fit=native 时决定页面物理尺寸；默认 300（漫画扫描件主流）
    """
    src = Path(src)
    target = target.lower().lstrip(".")
    if not src.is_file():
        raise ConvertError(f"文件不存在：{src}")
    if target not in P0_TARGETS:
        raise ConvertError(f"P0 还不支持目标格式 {target}")

    out_path = Path(out) if out else src.with_suffix("." + target)
    if out_path.suffix.lower().lstrip(".") != target:
        out_path = out_path.with_suffix("." + target)

    # zip ↔ cbz：容器相同，直接复制，连非图片条目一起保住（真·字节无损）
    if target in ("cbz", "zip") and src.suffix.lower() in (".cbz", ".zip"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_path)
        return {"ok": True, "out": str(out_path), "pages": None,
                "mode": "copy", "note": "容器等价，直接复制（含 ComicInfo 等非图片条目）"}

    book = read_comic(src)
    if not book.pages:
        raise ConvertError("源文件里没有图片页")

    note = ""
    if src.suffix.lower() == ".epub" and target in ("cbz", "zip", "pdf"):
        total = src.stat().st_size
        if not epub.looks_like_comic(book, total):
            raise ConvertError(
                f"这本 epub 不像漫画（图片 {book.page_count} 张、"
                f"约占体积 {sum(p.size for p in book.pages) / total:.0%}）。"
                "小说类书籍不走 CBZ/漫画路径")

    if target in ("cbz", "zip"):
        cbz.write(book, out_path)
        mode = "extract"
    elif target == "epub":
        epub.write(book, out_path,
                   title=(book.metadata or {}).get("title", ""),
                   author=(book.metadata or {}).get("author", ""))
        mode = "embed"
    else:
        size = A4 if page_size.lower() == "a4" else A5
        writer = PDFWriter(dpi=dpi)
        data = writer.build(book, fit=fit, page_size=size,
                            title=(book.metadata or {}).get("title", ""),
                            author=(book.metadata or {}).get("author", ""))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        mode = "embed"
        if fit == "native":
            note = f"页面按 {dpi:g}dpi 换算，图片原字节直嵌"

    return {"ok": True, "out": str(out_path), "pages": book.page_count,
            "mode": mode, "note": note}
