"""P0 审核发现的问题 —— 固化成回归测试。

这几条都是「不测就发现不了、上线就会被咬」的：
    ① 解压炸弹：几百 KB 的包声称赞解出几百 GB
    ② keep_names 路径遍历：条目名写成 ../../evil.jpg，解压时逃出目标目录
    ③ 重名条目：zip 规范允许同名，会重复成两页
    ④ PNG 直通的无损校验层次错：以前比的是「重建的 PNG」，必然不等 → 误报有损
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookforge import convert                       # noqa: E402
from bookforge.formats import cbz                   # noqa: E402


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


class TestUnzipBomb(unittest.TestCase):
    def test_single_page_over_limit(self):
        """单页解压后超上限 → 读的时候就拒，且不用真解压（0.0s）"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bomb.cbz"
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("001.png", make_png(8, 8))
                zf.writestr("002.png", b"\x00" * 300_000_000)
            with self.assertRaisesRegex(ValueError, "超过上限"):
                convert(p, "pdf", out=Path(d) / "o.pdf")

    def test_total_over_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bomb2.cbz"
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                for i in range(6):
                    zf.writestr(f"{i}.png", b"\x00" * 900_000_000)
            with self.assertRaises(ValueError):
                convert(p, "pdf", out=Path(d) / "o.pdf")

    def test_zip_to_cbz_copies_without_extracting(self):
        """zip→cbz 是容器等价复制，不解压，所以大包也照过（正确行为）"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.cbz"
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("001.png", make_png(8, 8))
            r = convert(p, "cbz", out=Path(d) / "out.cbz")
            self.assertEqual(r["mode"], "copy")


class TestEntryNameSafety(unittest.TestCase):
    def test_keep_names_strips_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.cbz"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("a.png", make_png(8, 8))
            book = cbz.read(p)
            book.pages[0].name = "../../evil.jpg"
            out = Path(d) / "o.cbz"
            cbz.write(book, out, keep_names=True)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            self.assertTrue(all(".." not in n for n in names), names)
            self.assertTrue(all(not n.startswith("/") for n in names), names)

    def test_duplicate_entries_deduped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dup.cbz"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("001.png", make_png(8, 8, seed=1))
                with self.assertWarns(UserWarning):
                    zf.writestr("001.png", make_png(8, 8, seed=2))
            self.assertEqual(cbz.read(p).page_count, 1)


class TestVerifyLayers(unittest.TestCase):
    def test_png_passthrough_reports_lossless(self):
        """PNG 走 IDAT 直通，verify 必须判无损（以前比重建 PNG，必然误报有损）"""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "png.cbz"
            with zipfile.ZipFile(src, "w") as zf:
                for i in range(3):
                    zf.writestr(f"{i:03d}.png", make_png(60, 80, seed=i))
            out = Path(d) / "png.pdf"
            convert(src, "pdf", out=out)

            script = Path(__file__).resolve().parent.parent / "scripts" / "verify_lossless.py"
            r = subprocess.run([sys.executable, str(script), str(src), str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("png-idat", r.stdout)
            self.assertIn("无损", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
