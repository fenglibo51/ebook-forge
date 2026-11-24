"""v1.1.2 复审（基准 6553851）的 1 个 P1 + 2 个 P2 回归测试。

  P1-01  PNG 的 IHDR 能覆盖全局限额 —— 文件自己给自己定预算
  P2-01  inflate 没查 d.eof，截断的 zlib 流被当合法
  P2-02  PDF 渲染把 LimitError 当「损坏内容」吞掉，会渲染空白页还报成功

这三条都是上一轮修 P1-01（无界解压）时我自己引入或没做干净的。
教训：修一个通用的保护函数，要把「谁来定参数」想清楚 ——
让不可信输入决定自己的预算，等于没设预算。
"""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _testenv                                            # noqa: E402,F401

from bookforge.formats import limits, png                  # noqa: E402

MB = 1024 * 1024
CH = {0: 1, 2: 3, 4: 2, 6: 4}


def _chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def build_png(w: int, h: int, payload: bytes, color_type: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", payload) + _chunk(b"IEND", b""))


def make_png(w: int, h: int, ct: int = 2):
    ch = CH[ct]
    px = bytes([200, 100, 50, 255][:ch]) * w
    raw = (b"\x00" + px) * h
    return build_png(w, h, zlib.compress(raw, 9), ct), raw


class PngBudgetBypassTests(unittest.TestCase):
    """P1-01：expected 来自 IHDR，是不可信输入，不能覆盖全局限额。

    复现基线（实测）：全局限额压到 1MB 时，一个 26KB 的 PNG
    （IHDR 声称 3000×3000，要解 25.8MB）照样被放行。
    """

    def setUp(self):
        self._saved = limits.MAX_INFLATE_BYTES

    def tearDown(self):
        limits.MAX_INFLATE_BYTES = self._saved

    def test_lying_ihdr_rejected_before_inflate(self):
        """声称大图 + 高压缩 IDAT → 解压前就要拒。"""
        limits.MAX_INFLATE_BYTES = 1 * MB
        w = h = 3000                       # expected ≈ 25.8MB
        data = build_png(w, h, zlib.compress(b"\x00" * (h * (1 + w * 3)), 9))
        self.assertLess(len(data), 1024 * 1024, "测试文件本身应该很小")

        with self.assertRaises(limits.LimitError) as ctx:
            png.decode(data)
        msg = str(ctx.exception)
        self.assertIn("PNG 声称", msg)
        self.assertIn("超过上限", msg)

    def test_limit_check_happens_before_allocation(self):
        """要在解压之前拒 —— 不是解完再判断。"""
        limits.MAX_INFLATE_BYTES = 1 * MB
        w = h = 3000
        raw = b"\x00" * (h * (1 + w * 3))
        data = build_png(w, h, zlib.compress(raw, 9))
        calls = {"n": 0}
        real = limits.inflate

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        with mock.patch.object(limits, "inflate", counting):
            with self.assertRaises(limits.LimitError):
                png.decode(data)
        self.assertEqual(calls["n"], 0, "超限时根本不该调 inflate")

    def test_zero_or_negative_dimensions_rejected(self):
        bad = b"\x89PNG\r\n\x1a\n" + _chunk(
            b"IHDR", struct.pack(">IIBBBBB", 0, 0, 8, 2, 0, 0, 0)) + _chunk(b"IEND", b"")
        with self.assertRaises(Exception):
            png.decode(bad)

    def test_normal_pngs_still_work(self):
        """修复不能误杀 —— RGB / RGBA / 灰度 / 大图都要正常。"""
        for w, h, ct in ((4, 4, 2), (16, 16, 6), (32, 32, 0), (500, 500, 2)):
            data, _ = make_png(w, h, ct)
            px, info, ch = png.decode(data)
            self.assertEqual(len(px), w * h * ch, f"{w}×{h} ct={ct} 解出来的大小不对")

    def test_large_real_image_still_decodes(self):
        """2000×2000 RGB（11.4MB 原始）在默认 256MB 预算下必须正常。"""
        data, _ = make_png(2000, 2000, 2)
        px, _, ch = png.decode(data)
        self.assertEqual(len(px), 2000 * 2000 * ch)


class TruncatedStreamTests(unittest.TestCase):
    """P2-01：decompressobj 不保证输入完整，d.eof 为假要报错。

    复现基线：裁掉末尾 1/2/6 字节，旧实现返回正常 bytes；
    而标准库 zlib.decompress() 抛 Error -5 incomplete。
    """

    def test_truncated_streams_rejected(self):
        full = zlib.compress(b"hello world " * 500, 9)
        self.assertEqual(len(limits.inflate(full)), 6000)   # 完整流正常
        for cut in (1, 2, 6, 10):
            with self.assertRaises(zlib.error, msg=f"截掉 {cut} 字节应该报错"):
                limits.inflate(full[:-cut], what="截断测试 ")

    def test_behaviour_matches_stdlib(self):
        """和标准库 zlib.decompress 对齐 —— 调用方的损坏分支靠这个。"""
        full = zlib.compress(b"abc" * 1000, 9)
        for cut in (1, 5):
            stdlib_raised = False
            try:
                zlib.decompress(full[:-cut])
            except zlib.error:
                stdlib_raised = True
            ours_raised = False
            try:
                limits.inflate(full[:-cut])
            except zlib.error:
                ours_raised = True
            self.assertEqual(stdlib_raised, ours_raised,
                             f"截 {cut} 字节时和标准库行为不一致")

    def test_complete_stream_ok(self):
        for n in (0, 1, 100, 10000):
            data = b"x" * n
            self.assertEqual(limits.inflate(zlib.compress(data)), data)

    def test_garbage_still_raises_zlib_error(self):
        with self.assertRaises(zlib.error):
            limits.inflate(b"definitely not zlib")


class RenderSwallowTests(unittest.TestCase):
    """P2-02：渲染层不能把 LimitError 当「内容流损坏」吞掉。

    复现基线：`except Exception: return b""` 会连 LimitError 一起吞 ——
    32MB 内容流超限被跳过，渲染出一页空白还报成功。
    """

    @staticmethod
    def _renderer(stream_bytes, head=b"<< /Filter /FlateDecode >>"):
        from bookforge.pdf import render
        doc = mock.Mock()
        doc.streams = {7: stream_bytes}
        doc.objs = {7: head}
        return render.PageRenderer(doc=doc, scale=1.0)

    def test_over_limit_propagates(self):
        bomb = zlib.compress(b"\x00" * (limits.MAX_PDF_STREAM_BYTES + 8 * MB), 9)
        r = self._renderer(bomb)
        with self.assertRaises(limits.LimitError):
            r._decode_stream(7)

    def test_corrupt_stream_still_skipped(self):
        """真的损坏还是要跳过 —— 不能因为修这个把所有坏流都变成失败。"""
        r = self._renderer(b"this is not zlib at all")
        self.assertEqual(r._decode_stream(7), b"")

    def test_normal_stream_decodes(self):
        payload = b"q 1 0 0 1 0 0 cm Q"
        r = self._renderer(zlib.compress(payload, 9))
        self.assertEqual(r._decode_stream(7), payload)

    def test_truncated_stream_skipped_as_corrupt(self):
        """截断流抛 zlib.error → 走「损坏跳过」分支（P2-01 的连锁效果）。"""
        good = zlib.compress(b"q Q", 9)
        r = self._renderer(good[:-2])
        self.assertEqual(r._decode_stream(7), b"")

    def test_limit_is_stricter_than_image_streams(self):
        """PDF 内部流预算必须比通用图片流紧 —— 对象流正常只有几十 KB。"""
        self.assertLess(limits.MAX_PDF_STREAM_BYTES, limits.MAX_INFLATE_BYTES)


class NoUnboundedDecompressTests(unittest.TestCase):
    """守住「不许再有裸 zlib.decompress」这条线。"""

    def test_no_raw_decompress_in_source(self):
        """用 AST 找真的 Call 节点，别被注释/docstring 里的字面量骗了。"""
        import ast

        offenders = []
        for f in (ROOT / "bookforge").rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # 匹配 zlib.decompress(...)
                if (isinstance(fn, ast.Attribute) and fn.attr == "decompress"
                        and isinstance(fn.value, ast.Name) and fn.value.id == "zlib"):
                    offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         f"这些地方直接用了无界 zlib.decompress：{offenders}")


if __name__ == "__main__":
    unittest.main()
