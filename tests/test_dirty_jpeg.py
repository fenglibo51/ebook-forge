"""脏头 JPEG 回归测试。

真实漫画包里存在前面带几十字节垃圾的 JPEG（下载工具/打包器留下的），
SOI(FFD8) 不在偏移 0。探测失败会让 PDF 里 /Width 写成 0，
MuPDF 宽容照样渲染，严格的阅读器直接报错 —— 2026-11-22 实测踩到。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest  # noqa: E402

from bookforge import convert                        # noqa: E402
from bookforge.formats import png                    # noqa: E402


def fake_jpeg(width: int, height: int, prefix: bytes = b"") -> bytes:
    """只构造到 SOF 为止的 JPEG 头 —— 够 image_size 解析，不需要真能解码。"""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width)
           + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return prefix + b"\xff\xd8" + app0 + sof + b"\xff\xd9"


class TestDirtyJPEG(unittest.TestCase):
    def test_prefix_27_bytes_detected(self):
        data = fake_jpeg(1280, 1900, prefix=bytes(range(27)))
        self.assertEqual(png.image_size(data), (1280, 1900, "jpeg"))

    def test_clean_jpeg_still_ok(self):
        self.assertEqual(png.image_size(fake_jpeg(600, 800)), (600, 800, "jpeg"))

    def test_pdf_width_not_zero(self):
        """脏头 JPEG 进 PDF 后 /Width 必须是真实值，不能是 0"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.cbz"
            with zipfile.ZipFile(src, "w") as zf:
                zf.writestr("001.jpg", fake_jpeg(1280, 1900, prefix=bytes(range(27))))
            out = Path(d) / "a.pdf"
            convert(src, "pdf", out=out, dpi=300)
            data = out.read_bytes()
            self.assertIn(b"/Width 1280 /Height 1900", data)
            self.assertNotIn(b"/Width 0 ", data)
            # 1280px @300dpi = 307.2pt
            self.assertIn(b"/MediaBox [0 0 307.2000 456.0000]", data)

    def test_garbage_not_mistaken_for_image(self):
        """纯垃圾字节 + 偶然出现的 FFD8 不该被当成有尺寸的 JPEG"""
        junk = b"\x00" * 100 + b"\xff\xd8" + b"\x00" * 100
        self.assertEqual(png.image_size(junk)[:2], (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
