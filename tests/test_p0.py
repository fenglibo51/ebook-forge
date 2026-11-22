"""P0 冒烟 + 无损单测（零依赖，python3 -m unittest 直接跑）。

测试里自己手写 PNG（不复用被测模块），避免「用被测代码生成测试数据」的循环验证。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
import zlib
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookforge import ConvertError, convert
from bookforge.formats import cbz, epub, png   # noqa: E402
from bookforge.pdf import extract              # noqa: E402


# ── 测试专用的最小 PNG 编码器（独立实现，不用 bookforge.png）──────────────
def make_png(width: int, height: int, seed: int = 0, channels: int = 3) -> bytes:
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter None
        for x in range(width):
            if channels == 3:
                raw += bytes(((x * 7 + seed) & 0xFF,
                              (y * 5 + seed) & 0xFF,
                              ((x + y) * 3 + seed) & 0xFF))
            elif channels == 1:
                raw.append((x * 3 + y * 11 + seed) & 0xFF)
            else:
                raise ValueError("测试只造 1 或 3 通道")

    def chunk(t: bytes, b: bytes) -> bytes:
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))

    color = {1: 0, 3: 2}[channels]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def make_cbz(path: Path, pages: list[bytes], names: list[str] | None = None) -> Path:
    names = names or [f"{i + 1:05d}.png" for i in range(len(pages))]
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in zip(names, pages):
            zf.writestr(n, data, compress_type=zipfile.ZIP_STORED)
    return path


class TestPNG(unittest.TestCase):
    def test_roundtrip_rgb(self):
        src = make_png(31, 17, seed=3)
        info = png.probe(src)
        self.assertEqual((info.width, info.height), (31, 17))
        px, inf, ch = png.decode(src)
        self.assertEqual(ch, 3)
        self.assertEqual(len(px), 31 * 17 * 3)
        again = png.encode(px, 31, 17, 3)
        self.assertEqual(md5(px), md5(png.decode(again)[0]))

    def test_passthrough_flag(self):
        self.assertTrue(png.probe(make_png(8, 8)).passthrough_ok)

    def test_size_probe(self):
        self.assertEqual(png.image_size(make_png(12, 9))[:2], (12, 9))


def md5(b: bytes) -> str:
    import hashlib
    return hashlib.md5(b).hexdigest()


class TestCBZ(unittest.TestCase):
    def test_natural_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.cbz"
            pages = [make_png(4, 4, seed=i) for i in range(3)]
            make_cbz(p, pages, names=["10.png", "2.png", "1.png"])
            book = cbz.read(p)
            self.assertEqual([x.name for x in book.pages], ["1.png", "2.png", "10.png"])

    def test_junk_filtered(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.cbz"
            make_cbz(p, [make_png(4, 4, 1), make_png(4, 4, 2)],
                     names=["a/1.png", "a/2.png"])
            with zipfile.ZipFile(p, "a") as zf:
                zf.writestr("__MACOSX/a/._1.png", b"junk")
                zf.writestr("a/.DS_Store", b"junk")
            book = cbz.read(p)
            self.assertEqual(book.page_count, 2)

    def test_write_bytes_identical(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            pages = [make_png(9, 7, seed=i) for i in range(4)]
            make_cbz(src, pages)
            book = cbz.read(src)
            out = Path(d) / "b.cbz"
            cbz.write(book, out)
            got = [x.data for x in cbz.read(out).pages]
            self.assertEqual([md5(x) for x in pages], [md5(x) for x in got])


class TestEPUB(unittest.TestCase):
    def test_write_then_read_lossless(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            pages = [make_png(10, 12, seed=i) for i in range(5)]
            make_cbz(src, pages)
            book = cbz.read(src)

            ep = Path(d) / "a.epub"
            epub.write(book, ep, title="测试本")
            back = epub.read(ep)
            self.assertEqual(back.page_count, 5)
            self.assertEqual([md5(x) for x in pages],
                             [md5(p.data) for p in back.pages])

    def test_mimetype_first_and_stored(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            make_cbz(src, [make_png(6, 6)] * 3)
            ep = Path(d) / "a.epub"
            epub.write(cbz.read(src), ep)
            with zipfile.ZipFile(ep) as zf:
                first = zf.infolist()[0]
                self.assertEqual(first.filename, "mimetype")
                self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zf.read("mimetype"), b"application/epub+zip")

    def test_novel_rejected_as_comic(self):
        """只有 1 张封面图的 epub 不该被当成漫画"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "novel.epub"
            book_text = "x" * 40000
            with zipfile.ZipFile(src, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr("META-INF/container.xml",
                            '<?xml version="1.0"?><container><rootfiles>'
                            '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
                zf.writestr("OEBPS/content.opf",
                            '<package><manifest>'
                            '<item id="c" href="cover.png"/>'
                            '<item id="t" href="t.xhtml"/>'
                            '</manifest><spine><itemref idref="t"/></spine></package>')
                zf.writestr("OEBPS/t.xhtml", f"<html><body><p>{book_text}</p></body></html>")
                zf.writestr("OEBPS/cover.png", make_png(20, 20))
            book = epub.read(src)
            self.assertEqual(book.page_count, 1)
            self.assertFalse(epub.looks_like_comic(book, src.stat().st_size))
            with self.assertRaises(ConvertError):
                convert(src, "cbz", out=Path(d) / "x.cbz")


class TestPDF(unittest.TestCase):
    def test_png_passthrough_pixels(self):
        """PNG 走 Predictor 15 直通，抽回来后像素必须一致"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            pages = [make_png(37, 23, seed=i) for i in range(3)]
            make_cbz(src, pages)
            out = Path(d) / "a.pdf"
            r = convert(src, "pdf", out=out)
            self.assertTrue(r["ok"])
            self.assertEqual(r["pages"], 3)

            imgs = extract.extract_images(str(out))
            self.assertEqual(len(imgs), 3)
            for img, src_bytes in zip(imgs, pages):
                self.assertEqual(img.filters, ["FlateDecode"])
                self.assertEqual(img.predictor, 15)
                rebuilt = png.encode(img.data, img.width, img.height, 3)
                self.assertEqual(md5(png.decode(rebuilt)[0]),
                                 md5(png.decode(src_bytes)[0]))

    def test_pdf_structure(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            make_cbz(src, [make_png(8, 8)] * 2)
            out = Path(d) / "a.pdf"
            convert(src, "pdf", out=out)
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.7"))
            self.assertIn(b"/Type /Catalog", data)
            self.assertIn(b"/Count 2", data)
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))
            # xref 偏移必须指到对象头
            import re
            m = re.search(rb"startxref\s+(\d+)", data)
            off = int(m.group(1))
            self.assertTrue(data[off:off + 4] == b"xref")

    def test_page_spec_native_dpi(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            make_cbz(src, [make_png(300, 450)])     # 300×450 px @300dpi = 72×108 pt
            out = Path(d) / "a.pdf"
            convert(src, "pdf", out=out, dpi=300)
            data = out.read_bytes()
            self.assertIn(b"/MediaBox [0 0 72.0000 108.0000]", data)

    def test_zip_copy_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.zip"
            make_cbz(src, [make_png(5, 5, 1), make_png(5, 5, 2)])
            out = Path(d) / "b.cbz"
            r = convert(src, "cbz", out=out)
            self.assertEqual(r["mode"], "copy")
            self.assertEqual(md5(src.read_bytes()), md5(out.read_bytes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
