"""P6 回归测试：极简渲染器（蒙版 PDF 兜底，有损）。

测试样本自己造：用 `_g4enc` 编一张 1bit 位图，拼成「页面 = 一张 ImageMask」的
最小 PDF —— 不依赖 104MB 的真实样本，也不依赖任何外部渲染器。

判据全部是几何/像素统计，不看感觉：
    方向      上半页和下半页的暗像素占比要对应得上（翻转会互换）
    内容      渲染结果与源位图的暗像素分布一致
    边界      文字不渲染（明确能力边界，不是 bug）
    开关      BOOKFORGE_RENDER_FALLBACK=false 时必须报错而不是静默渲染
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge.pdf._g4enc import encode_bitmap          # noqa: E402
from bookforge.pdf.reader import PDFDocument            # noqa: E402
from bookforge.pdf.render import PageRenderer           # noqa: E402


def make_stencil_pdf(bitmap: list[list[int]], columns: int, rows: int,
                     mask_aspect: bool = True) -> bytes:
    """拼一个最小 PDF：1 页，内容流是 `q cm /Im0 Do Q`，Im0 是 CCITT 蒙版。"""
    g4 = encode_bitmap(bitmap, columns)
    # 页面尺寸按位图比例给（单位 pt）
    page_w = 400.0
    page_h = page_w * rows / columns if mask_aspect else page_w * 1.414

    # 图片铺满整页
    content = f"q {page_w} 0 0 {page_h} 0 0 cm /Im0 Do Q\n".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
         f"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>").encode(),
        None,      # 内容流，下面填
        None,      # 蒙版图，下面填
    ]
    out = bytearray(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        if i == 4:
            out += f"{i} 0 obj\n<< /Length {len(content)} >>\nstream\n".encode()
            out += content + b"\nendstream\nendobj\n"
        elif i == 5:
            d = (f"<< /Type /XObject /Subtype /Image /Width {columns} "
                 f"/Height {rows} /ImageMask true "
                 f"/Filter /CCITTFaxDecode "
                 f"/DecodeParms << /K -1 /Columns {columns} /Rows {rows} >> "
                 f"/Length {len(g4)} >>").encode()
            out += f"{i} 0 obj\n".encode() + d + b"\nstream\n" + g4 + b"\nendstream\nendobj\n"
        else:
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R /Size 6 >>\n%%EOF\n"
    return bytes(out)


def dark_ratio(buf: bytes, y0: int, y1: int, w: int) -> float:
    """位图 [y0, y1) 行的暗像素占比"""

    n = dark = 0
    for y in range(y0, y1):
        base = y * w * 3
        for x in range(w):
            if buf[base + x * 3] < 128:
                dark += 1
            n += 1
    return dark / max(1, n)


class TestRenderStencil(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 上半黑、下半白 —— 位置错了一眼就能看出来
        W, H = 64, 64
        cls.bitmap = [[1 if y < H // 2 else 0 for x in range(W)] for y in range(H)]
        cls.W, cls.H = W, H
        cls.pdf_bytes = make_stencil_pdf(cls.bitmap, W, H)
        cls.tmp = Path(tempfile.mkdtemp()) / "mask.pdf"
        cls.tmp.write_bytes(cls.pdf_bytes)

    def test_pdf_readable(self):
        doc = PDFDocument(self.tmp)
        self.assertEqual(len(doc.page_objects()), 1)
        self.assertEqual(len(doc.all_images()), 1)

    def test_render_orientation(self):
        """上半页该黑、下半页该白；翻转会互换"""
        doc = PDFDocument(self.tmp)
        bmp = PageRenderer(doc, scale=1.0).render(0)
        top = dark_ratio(bytes(bmp.buf), 0, bmp.h // 2, bmp.w)
        bottom = dark_ratio(bytes(bmp.buf), bmp.h // 2, bmp.h, bmp.w)
        self.assertGreater(top, 0.8, f"上半页应该几乎全黑，实测 {top:.0%}")
        self.assertLess(bottom, 0.2, f"下半页应该几乎全白，实测 {bottom:.0%}")

    def test_render_resolution_scales(self):
        doc = PDFDocument(self.tmp)
        a = PageRenderer(doc, scale=1.0).render(0)
        b = PageRenderer(doc, scale=2.0).render(0)
        self.assertAlmostEqual(b.w / a.w, 2.0, delta=0.1)
        self.assertAlmostEqual(b.h / a.h, 2.0, delta=0.1)

    def test_fill_color_applies(self):
        """蒙版用当前填充色上色；默认黑色"""
        doc = PDFDocument(self.tmp)
        bmp = PageRenderer(doc, scale=1.0).render(0)
        buf = bytes(bmp.buf)
        # 上半页第一个像素应该接近黑
        self.assertLess(buf[0], 60)


class TestRenderFallback(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _mask_heavy_pdf(self) -> Path:
        W, H = 64, 64
        bitmap = [[1 if (x // 8 + y // 8) % 2 else 0 for x in range(W)] for y in range(H)]
        p = self.d / "m.pdf"
        p.write_bytes(make_stencil_pdf(bitmap, W, H))
        return p

    def test_fallback_renders_and_flags_lossy(self):
        """蒙版型 PDF 走渲染，结果必须带 mode=render 和「有损」说明"""
        from bookforge.pdf.to_comic import pdf_to_comic
        book = pdf_to_comic(self._mask_heavy_pdf())
        self.assertEqual(book.page_count, 1)
        self.assertEqual(book.mode, "render")
        self.assertIn("有损", book.note)
        self.assertEqual(book.pages[0].fmt, "png")

    def test_fallback_disabled_raises(self):
        """关掉兜底就必须明确报错，绝不悄悄渲染"""
        import bookforge.pdf.to_comic as tc
        old = tc.RENDER_FALLBACK
        try:
            tc.RENDER_FALLBACK = False
            from bookforge.pdf.to_comic import PDFComicError, pdf_to_comic
            with self.assertRaises(PDFComicError) as ctx:
                pdf_to_comic(self._mask_heavy_pdf())
            self.assertIn("蒙版", str(ctx.exception))
        finally:
            tc.RENDER_FALLBACK = old

    def test_text_pdf_still_rejected(self):
        """渲染兜底不能让文字版 PDF 混进来（那等于把小说拍成图）"""
        from bookforge.pdf.to_comic import PDFComicError, pdf_to_comic
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>",
        ]
        out = bytearray(b"%PDF-1.4\n")
        for i, body in enumerate(objs, 1):
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        out += b"trailer\n<< /Root 1 0 R /Size 4 >>\n%%EOF\n"
        p = self.d / "t.pdf"
        p.write_bytes(bytes(out))
        with self.assertRaises(PDFComicError):
            pdf_to_comic(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
