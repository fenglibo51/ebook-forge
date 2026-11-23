"""BookForge —— 电子书 / 漫画格式转换引擎（纯标准库自研）。

不含 Calibre、不依赖 poppler / unar，也不调用任何外部二进制。

    from bookforge import convert
    r = convert("漫画.cbz", "pdf")
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .formats import cbz, epub, png
from .naming import display_stem
from .model import ComicBook
from .pdf.writer import A4, A5, PDFWriter

# 版本号只有一处：bookforge/__init__.py 的 __version__。
# 这里曾经还有一行 __version__ = "0.1.0"，没人引用、也没跟着发版更新，
# 只会让人 `import bookforge.convert` 时看到个对不上的数字。删了。

COMIC_INPUTS = {".cbz", ".zip", ".cbr", ".cb7", ".cbc"}
# 漫画源（→ cbz / zip / epub / pdf）
P0_INPUTS = {".cbz", ".zip", ".epub", ".pdf"}
P0_TARGETS = {"cbz", "zip", "epub", "pdf"}
# 文字书（→ txt / fb2 / docx / epub / mobi / pdf）
TEXT_INPUTS = {".txt", ".fb2", ".docx", ".epub", ".mobi"}
TEXT_TARGETS = {"txt", "fb2", "docx", "epub", "mobi", "pdf"}


def _epub_is_comic(path: Path) -> bool:
    """epub 是漫画还是小说？判据同 P0：页数 + 图片占体积比。"""
    try:
        comic = epub.read(path)
        return epub.looks_like_comic(comic, path.stat().st_size)
    except Exception:
        return False        # 读不出图（纯文字 epub）→ 不是漫画


def _mobi_is_comic(path: Path) -> bool:
    """mobi 里图片够多就是漫画 mobi。"""
    try:
        from .formats import mobi as mobi_mod
        mobi_mod.to_comic(path)
        return True
    except Exception:
        return False


def read_text_book(path: str | Path):
    """文字源 → TextBook。epub 走文字解析（不是漫画的取图路径）。"""
    from .formats import docx as docx_mod, fb2, mobi as mobi_mod, txt_io
    from .model import TextBook

    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".txt":
        return txt_io.read(path)
    if ext == ".fb2":
        return fb2.read(path)
    if ext == ".docx":
        return docx_mod.read(path)
    if ext == ".epub":
        return epub.read_text(path)
    if ext == ".mobi":
        return mobi_mod.read(path)
    raise ConvertError(f"文字路径不支持源格式 {ext}")


def write_text_book(book, target: str, out: Path, *,
                    font: str = "", page_size: str = "a5",
                    style: dict | None = None) -> tuple[Path, str]:
    """TextBook → 目标容器。返回 (输出路径, 提示)。"""
    from .formats import docx as docx_mod, fb2, mobi as mobi_mod, txt_io

    out.parent.mkdir(parents=True, exist_ok=True)
    if target == "txt":
        out.write_bytes(txt_io.to_bytes(book))
        return out, ""
    if target == "fb2":
        out.write_bytes(fb2.to_bytes(book))
        return out, ""
    if target == "docx":
        data, skipped = docx_mod.to_bytes(book)
        out.write_bytes(data)
        note = f"docx 输出暂不支持嵌图，跳过 {skipped} 张图片" if skipped else ""
        return out, note
    if target == "mobi":
        out.write_bytes(mobi_mod.to_bytes(book))
        return out, ""
    if target == "pdf":
        from .model import PageSpec
        from .pdf.textpdf import TextPDFWriter, find_font
        spec = PageSpec.a5() if page_size.lower() != "a4" else PageSpec.from_mm(210, 297)
        writer = TextPDFWriter(find_font(font))
        data = writer.build(book, spec=spec, style=style,
                            title=(book.metadata or {}).get("title", ""))
        out.write_bytes(data)
        return out, "排版 L1：单栏分页 / 章节另起页 / 页码 / 首行缩进 2 字符"
    if target == "epub":
        epub.write_text(book, out,
                        title=(book.metadata or {}).get("title", ""),
                        author=(book.metadata or {}).get("author", ""))
        return out, ""
    raise ConvertError(f"文字路径不支持目标格式 {target}")


class ConvertError(Exception):
    pass


def read_comic(path: str | Path) -> ComicBook:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".cbz", ".zip"):
        return cbz.read(path)
    if ext == ".epub":
        return epub.read(path)
    if ext == ".pdf":
        from .pdf.to_comic import pdf_to_comic
        return pdf_to_comic(path)
    if ext == ".mobi":
        from .formats import mobi as mobi_mod
        return mobi_mod.to_comic(path)
    if ext in (".cbr", ".cb7", ".cbc"):
        if ext == ".cbr":
            raise ConvertError("CBR（RAR）不支持：RAR5 算法闭源，纯 Python 无实现")
        return cbz.read_cb7(path)
    raise ConvertError(f"不支持的源格式 {ext}")


def convert(src: str | Path, target: str, *, out: str | Path | None = None,
            dpi: float = 300.0, fit: str = "native",
            page_size: str = "a5", font: str = "",
            src_name: str = "") -> dict:
    """转换单个漫画文件。

    target: cbz / zip / epub / pdf
    fit:    native（页面跟图片走，按 dpi 换算）或 fit（等比缩放进 page_size）
    dpi:    只在 fit=native 时决定页面物理尺寸；默认 300（漫画扫描件主流）
    """
    src = Path(src)
    # 磁盘上是随机 file_id（Web 上传），用户看到的是原始名。
    # 默认书名 / 内嵌标题兜底一律用这个，绝不把存储 ID 写进书里。
    title_fallback = display_stem(src, src_name)
    target = target.lower().lstrip(".")
    if not src.is_file():
        raise ConvertError(f"文件不存在：{src}")
    if target not in P0_TARGETS and target not in TEXT_TARGETS:
        raise ConvertError(f"不支持目标格式 {target}")

    out_path = Path(out) if out else src.with_suffix("." + target)
    if out_path.suffix.lower().lstrip(".") != target:
        out_path = out_path.with_suffix("." + target)

    # ── 文字书路径：txt / fb2 / docx / epub ──────────────────────────────
    text_src = src.suffix.lower() in (".txt", ".fb2", ".docx")
    if text_src and target not in TEXT_TARGETS:
        raise ConvertError(
            f"{src.suffix} 是文字书，只能转 {'/'.join(sorted(TEXT_TARGETS))}；"
            f"转 {target} 需要排版引擎（P4）或本身不适用")

    want_text = text_src
    # epub / mobi 作为源时要分辨漫画还是小说：
    #   目标是文字类（txt/fb2/docx/pdf/epub）且源不是漫画 → 走文字路径
    #   漫画 epub/mobi → cbz/pdf 等 → 走图片路径
    # 早先只判断了 epub→epub，导致小说 epub → pdf 被当漫画处理（正文全丢）。
    if src.suffix.lower() in (".epub", ".mobi") and target in ("txt", "fb2", "docx", "pdf", "epub"):
        is_comic = (_epub_is_comic(src) if src.suffix.lower() == ".epub"
                    else _mobi_is_comic(src))
        want_text = not is_comic

    if want_text:
        book = read_text_book(src)
        # 源文件没带标题就用文件名兜底（txt 几乎没有内嵌标题）
        if not (book.metadata or {}).get("title"):
            book.metadata = dict(book.metadata or {})
            book.metadata["title"] = title_fallback
        out_path, note = write_text_book(book, target, out_path,
                                         font=font, page_size=page_size)
        return {"ok": True, "out": str(out_path),
                "pages": len(book.chapters), "mode": "text", "note": note}

    # zip ↔ cbz：容器相同，直接复制，连非图片条目一起保住（真·字节无损）
    if target in ("cbz", "zip") and src.suffix.lower() in (".cbz", ".zip"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_path)
        return {"ok": True, "out": str(out_path), "pages": None,
                "mode": "copy", "note": "容器等价，直接复制（含 ComicInfo 等非图片条目）"}

    book = read_comic(src)
    if not book.pages:
        raise ConvertError("源文件里没有图片页")
    # 读取器用 path.stem 兜底了书名，这里换成用户看到的原始名
    meta = dict(book.metadata or {})
    if not meta.get("title") or meta.get("title") == src.stem:
        meta["title"] = title_fallback
        book.metadata = meta

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
            "mode": book.mode or mode, "note": book.note or note}
