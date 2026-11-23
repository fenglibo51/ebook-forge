"""P1 回归测试：PDF → CBZ 不依赖 poppler。

覆盖：
    · 对象扫描 / 页面树 / 链式 Filter（FlateDecode + DCTDecode）
    · PDF → ComicBook 的字节一致性（自己写的 PDF 自己读回来）
    · 零拷贝 PNG 路径（PDF Predictor 15 直接当 PNG 的 IDAT）
    · CCITT G4 往返（编码器只用于测试，不是产品功能）
    · 判据：文字版拒绝 / 覆盖率不足拒绝 / 碎片过滤
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge import convert                                  # noqa: E402
from bookforge.formats import cbz, png                         # noqa: E402
from bookforge.pdf._g4enc import encode_bitmap                 # noqa: E402
from bookforge.pdf.ccitt import decode_g4                      # noqa: E402
from bookforge.pdf.reader import PDFDocument, dict_get, dict_name, dict_ref  # noqa: E402
from bookforge.pdf.to_comic import PDFComicError, pdf_to_comic  # noqa: E402


def make_png(w: int, h: int, seed: int = 0) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes(((x * 7 + seed) & 0xFF, (y * 5 + seed) & 0xFF,
                          ((x + y) * 3 + seed) & 0xFF))

    def ck(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr)
            + ck(b"IDAT", zlib.compress(bytes(raw), 6)) + ck(b"IEND", b""))


def build_cbz(path: Path, pages: list[bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for i, d in enumerate(pages):
            zf.writestr(f"{i + 1:03d}.png", d, compress_type=zipfile.ZIP_STORED)
    return path


class TestDictParse(unittest.TestCase):
    def test_ref_not_truncated(self):
        """`/Root 1 0 R` 必须在第一个空格处截断的错误版本会返回 b'1'"""
        self.assertEqual(dict_get(b"<< /Root 12 0 R /Size 5 >>", "Root"), b"12 0 R")
        self.assertEqual(dict_ref(b"<< /Root 12 0 R >>", "Root"), 12)

    def test_name_value(self):
        self.assertEqual(dict_name(b"<< /ColorSpace /DeviceRGB /Width 10 >>", "ColorSpace"),
                         "DeviceRGB")

    def test_nested_dict(self):
        raw = b"<< /XObject << /Im0 4 0 R >> /ProcSet [/PDF /ImageB] >>"
        sub = dict_get(raw, "XObject")
        self.assertTrue(sub.startswith(b"<<"))
        self.assertEqual(dict_ref(sub, "Im0"), 4)


class TestPDFRoundTrip(unittest.TestCase):
    def test_own_pdf_read_back_identical(self):
        """自造 PDF → reader 读回 → 像素一致（PNG 走零拷贝 IDAT 路径）"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            pages = [make_png(40, 60, seed=i) for i in range(4)]
            build_cbz(src, pages)
            pdf = Path(d) / "a.pdf"
            convert(src, "pdf", out=pdf)

            doc = PDFDocument(pdf)
            self.assertEqual(len(doc.page_objects()), 4)
            self.assertEqual(len(doc.all_images()), 4)

            # min_side=10：测试图小，正式阈值 200 见 test_small_fragments_filtered
            book = pdf_to_comic(pdf, min_side=10)
            self.assertEqual(book.page_count, 4)
            for out_page, ref in zip(book.pages, pages):
                self.assertEqual(png.decode(out_page.data)[0], png.decode(ref)[0])

    def test_zero_copy_png_is_byte_equal_idat(self):
        """零拷贝路径：输出 PNG 的 IDAT == PDF 里的 Flate 流"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            build_cbz(src, [make_png(30, 40, seed=7)])
            pdf = Path(d) / "a.pdf"
            convert(src, "pdf", out=pdf)

            doc = PDFDocument(pdf)
            img = doc.all_images()[0]
            self.assertEqual(img.filters, ["FlateDecode"])
            self.assertEqual(img.decode_parms.get("Predictor"), 15)

            book = pdf_to_comic(pdf, min_side=10)
            self.assertEqual(png.idat_bytes(book.pages[0].data), img.stream)

    def test_small_fragments_filtered(self):
        """小于 200px 的碎片不算整页图。

        P6 之后行为分两种：
          兜底开启（默认）→ 走渲染，mode=render，不报错
          兜底关闭        → 明确报错，不静默少页
        """
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            build_cbz(src, [make_png(300, 400, seed=1), make_png(40, 40, seed=2)])
            pdf = Path(d) / "a.pdf"
            convert(src, "pdf", out=pdf)
            doc = PDFDocument(pdf)
            self.assertEqual(len(doc.all_images()), 2)      # 两张图都在 PDF 里

            import bookforge.pdf.to_comic as tc
            old = tc.RENDER_FALLBACK
            try:
                # 兜底开启 → 渲染，页数完整
                tc.RENDER_FALLBACK = True
                book = pdf_to_comic(pdf)
                self.assertEqual(book.page_count, 2)
                self.assertEqual(book.mode, "render")

                # 兜底关闭 → 报错
                tc.RENDER_FALLBACK = False
                with self.assertRaises(PDFComicError) as ctx:
                    pdf_to_comic(pdf)
                self.assertIn("1/2", str(ctx.exception))
            finally:
                tc.RENDER_FALLBACK = old

            # 放宽碎片阈值 → 无损提取，不走渲染
            book = pdf_to_comic(pdf, min_side=10)
            self.assertEqual(book.page_count, 2)
            self.assertEqual(book.mode, "")


class TestReject(unittest.TestCase):
    def _minimal_text_pdf(self, path: Path) -> Path:
        """手写一个只有文字页、没有任何图像的 PDF（不依赖 xref，我们的扫描器能读）"""
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length 44 >>\nstream\nBT /F1 24 Tf 72 700 Td (hello novel) Tj ET\nendstream",
        ]
        out = bytearray(b"%PDF-1.4\n")
        for i, body in enumerate(objs, 1):
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        out += b"trailer\n<< /Root 1 0 R /Size 6 >>\n%%EOF\n"
        path.write_bytes(bytes(out))
        return path

    def test_text_pdf_rejected(self):
        """矢量/文字版 PDF 不许被当漫画处理（更不能拍成图片）"""
        with tempfile.TemporaryDirectory() as d:
            pdf = self._minimal_text_pdf(Path(d) / "n.pdf")
            doc = PDFDocument(pdf)
            self.assertEqual(len(doc.page_objects()), 1)
            self.assertEqual(doc.all_images(), [])
            with self.assertRaises(PDFComicError):
                pdf_to_comic(pdf)


class TestCCITT(unittest.TestCase):
    def _roundtrip(self, bitmap, columns):
        enc = encode_bitmap(bitmap, columns)
        dec = decode_g4(enc, columns, len(bitmap))
        rowbytes = (columns + 7) // 8
        ref = bytearray(rowbytes * len(bitmap))
        for y, line in enumerate(bitmap):
            for x, v in enumerate(line):
                if v:
                    ref[y * rowbytes + (x >> 3)] |= 0x80 >> (x & 7)
        self.assertEqual(dec[:len(ref)], bytes(ref))

    def test_patterns(self):
        W = 64
        self._roundtrip([[0] * W] * 4, W)                                    # 全白
        self._roundtrip([[1] * W] * 4, W)                                    # 全黑
        self._roundtrip([[0] * 32 + [1] * 32] * 4, W)                        # 左右半
        self._roundtrip([[1 if (x // 4) % 2 else 0 for x in range(W)] for _ in range(4)], W)
        self._roundtrip([[1 if x % 7 == 0 else 0 for x in range(256)] for _ in range(10)], 256)

    def test_non_byte_aligned_width(self):
        self._roundtrip([[1 if x < 20 else 0 for x in range(37)] for _ in range(5)], 37)

    def test_makeup_codes(self):
        """宽度超过 1728 必须走 makeup 码"""
        self._roundtrip([[1 if x > 1000 else 0 for x in range(2000)] for _ in range(3)], 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
