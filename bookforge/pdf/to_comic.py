"""PDF → ComicBook（图片型漫画 PDF）。

判据三条，缺一条就会误伤：

    ① 碎片过滤      min(宽,高) < 200 的是扫描切边/空白条，不是页
    ② 覆盖率        按「有整页图的页数 / 总页数」算，不是按图片张数
                    （一张页切出十几个窄条会把分子灌水）
    ③ 蒙版占比      stencil（/ImageMask）抽出来是反相的，不能当页面用
                    占比过半 = 蒙版型 PDF，只能渲染

处置：
    覆盖率 100%           → 直接输出，无损
    覆盖率 90%~100%       → 报错（缺页不静默少页），提示 P6 渲染
    覆盖率 < 90% 或蒙版型  → 报错（P6 渲染未实现）
    完全无图              → 判定文字/矢量版，直接拒绝，不许当漫画处理
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from ..model import ComicBook, Page
from .images import to_image_bytes
from .reader import PDFDocument

MIN_SIDE = 200          # 小于这个边长的是碎片
MIN_COVER = 0.90        # 整页图覆盖率下限

# 渲染兜底（有损）：只有「没有无损通路」的 PDF 才走这条路
RENDER_DPI = int(os.environ.get("BOOKFORGE_RENDER_DPI", "200"))
if RENDER_DPI not in (150, 200, 300):
    RENDER_DPI = 200
RENDER_FALLBACK = os.environ.get("BOOKFORGE_RENDER_FALLBACK", "true").strip().lower() \
    not in ("0", "false", "no", "off")


class PDFComicError(ValueError):
    pass


def _render_pages(doc: PDFDocument, out_pages: int, dpi: int) -> list[Page]:
    """逐页渲染成 PNG。有损，但这是蒙版/损坏 PDF 唯一的出图办法。"""
    from .render import render_page_to_png

    scale = dpi / 72.0
    pages: list[Page] = []
    for i in range(out_pages):
        data = render_page_to_png(doc, i, scale=scale)
        from ..formats import png as pngmod
        w, h, _ = pngmod.image_size(data)
        pages.append(Page(data=data, fmt="png", width=w, height=h,
                          name=f"{i + 1:05d}"))
    return pages


def pdf_to_comic(path: str | Path, *, min_side: int = MIN_SIDE,
                 min_cover: float = MIN_COVER) -> ComicBook:
    doc = PDFDocument(path)
    page_objs = doc.page_objects()
    pages_n = len(page_objs)
    if pages_n == 0:
        raise PDFComicError("这个 PDF 里找不到页对象（可能已损坏或加密）")

    all_imgs = doc.all_images()
    stencil = [i for i in all_imgs if i.is_stencil]
    real = [i for i in all_imgs if not i.is_stencil and i.min_side >= min_side]

    covered_pages = {i.page_index for i in real}
    cover = len(covered_pages) / pages_n
    mask_heavy = bool(all_imgs) and len(stencil) > len(all_imgs) * 0.5

    if not all_imgs and not mask_heavy:
        raise PDFComicError(
            "这个 PDF 里提取不到图片，可能是矢量/文字版 PDF。"
            "本功能只支持图片型漫画 PDF —— 小说类请转 EPUB / TXT 等格式")

    if mask_heavy:
        if not RENDER_FALLBACK:
            raise PDFComicError(
                f"这个 PDF 的页面由图像蒙版构成（{len(stencil)}/{len(all_imgs)} 条），"
                "没有无损提取通路；渲染兜底已关闭（BOOKFORGE_RENDER_FALLBACK=false）")
        pages = _render_pages(doc, pages_n, RENDER_DPI)
        return ComicBook(pages=pages, metadata={"title": Path(path).stem},
                         source_format="pdf",
                         mode="render",
                         note=(f"已用渲染模式转换（{RENDER_DPI}dpi，有损）："
                               f"页面是图像蒙版结构（{len(stencil)}/{len(all_imgs)} 条），"
                               "没有无损通路"))

    if cover < min_cover:
        if not RENDER_FALLBACK:
            raise PDFComicError(
                f"只有 {len(covered_pages)}/{pages_n} 页能提取到整页图"
                f"（{cover:.0%}），渲染兜底已关闭")
        pages = _render_pages(doc, pages_n, RENDER_DPI)
        return ComicBook(pages=pages, metadata={"title": Path(path).stem},
                         source_format="pdf",
                         mode="render",
                         note=(f"已用渲染模式转换（{RENDER_DPI}dpi，有损）："
                               f"只提取到 {len(covered_pages)}/{pages_n} 页整页图"))

    # 每页取面积最大的一张当整页图（一页里附带的小图是装饰/水印）
    best: dict[int, object] = {}
    for im in real:
        cur = best.get(im.page_index)
        if cur is None or im.width * im.height > cur.width * cur.height:
            best[im.page_index] = im

    missing = [i + 1 for i in range(pages_n) if i not in best]
    if missing:
        head = ", ".join(str(x) for x in missing[:8])
        more = f" 等 {len(missing)} 页" if len(missing) > 8 else ""
        raise PDFComicError(
            f"第 {head}{more} 提取不到整页图。不静默少页 —— "
            "这类 PDF 需要渲染模式（P6，尚未实现）")

    pages: list[Page] = []
    for i in range(pages_n):
        im = best[i]
        try:
            data, ext = to_image_bytes(im)
        except Exception as e:
            raise PDFComicError(
                f"第 {i + 1} 页图片解码失败（PDF 可能已损坏，或用了不支持的编码）：{e}")
        pages.append(Page(data=data, fmt=ext, width=im.width,
                          height=im.height, name=f"{i + 1:05d}"))

    meta = {"title": Path(path).stem}
    return ComicBook(pages=pages, metadata=meta, source_format="pdf")
