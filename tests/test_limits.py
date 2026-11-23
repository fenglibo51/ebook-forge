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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge import convert                       # noqa: E402
from bookforge.formats import cbz                   # noqa: E402
from bookforge.formats import limits                # noqa: E402


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
    """解压炸弹防护。

    ⚠️ 一律「缩小阈值 + 小数据」来测，不造 300MB / 900MB 的真实数组。
    早先那版在 CI 里真的分配过 GB 级内存，跑一次就把 runner 压得半死，
    而且测的是「能不能分配」而不是「限额逻辑对不对」。
    """

    def setUp(self):
        self._entry = limits.MAX_ENTRY_BYTES
        self._total = limits.MAX_TOTAL_BYTES

    def tearDown(self):
        limits.MAX_ENTRY_BYTES = self._entry
        limits.MAX_TOTAL_BYTES = self._total

    def test_single_page_over_limit(self):
        """单页声明超上限 → 读取入口就拒，不用真解压。"""
        limits.MAX_ENTRY_BYTES = 1 << 20                     # 1MB
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bomb.cbz"
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("001.png", make_png(8, 8))
                zf.writestr("002.png", b"\x00" * (2 << 20))     # 声明 2MB（压缩后很小）
            with self.assertRaisesRegex(ValueError, "超过单文件上限"):
                convert(p, "pdf", out=Path(d) / "o.pdf")

    def test_total_over_limit(self):
        """累计超上限 → 拒。单项不超，走的是总量那道闸。"""
        limits.MAX_ENTRY_BYTES = 1 << 20                     # 单项 1MB
        limits.MAX_TOTAL_BYTES = 1 << 20                     # 总量也 1MB
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bomb2.cbz"
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                for i in range(4):
                    zf.writestr(f"{i}.png", b"\x00" * (400 << 10))   # 400KB × 4
            with self.assertRaises(ValueError) as ctx:
                convert(p, "pdf", out=Path(d) / "o.pdf")
            self.assertIn("上限", str(ctx.exception))

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
