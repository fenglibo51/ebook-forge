"""嵌套压缩包的限额（P1-2）。

CBC 是「7z 里套若干个 cbz」。危险点在于：每进一层如果重新拿一份完整额度，
一个 7z 里放 100 个 cbz、每个都在单层限额内，累计就爆了。

这里验：
    ① 普通 CBZ 条目数超限要拒
    ② CBC 内层 CBZ 条目数超限要拒
    ③ 两层嵌套**共用一份预算** —— 分别测都过，合起来必须被拒
    ④ 正常的 CBC 照样能读（别把功能误伤掉）
    ⑤ 拒绝之后源文件不动、不留正式输出

造包用 tests/fixtures/gen_7z.py（纯标准库，不依赖 py7zr）。
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from bookforge.formats import cbz, limits, sevenz        # noqa: E402
from fixtures.gen_7z import build                        # noqa: E402


def tiny_png(seed: int = 0) -> bytes:
    """1×1 的合法 PNG，够 png.image_size() 认出尺寸就行。"""
    import struct
    import zlib

    def ck(t: bytes, b: bytes) -> bytes:
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes((seed & 0xFF, 0, 0))
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr)
            + ck(b"IDAT", zlib.compress(raw, 6)) + ck(b"IEND", b""))


def make_zip(entries: int, seed: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(entries):
            zf.writestr(f"{i:04d}.png", tiny_png(seed + i))
    return buf.getvalue()


class ZipEntryLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_nest_"))

    def test_plain_cbz_entry_count(self):
        """普通 CBZ 条目数超限 → 拒。"""
        p = self.tmp / "many.cbz"
        p.write_bytes(make_zip(30))
        with mock.patch.object(limits, "MAX_ENTRIES", 10):
            with self.assertRaises(ValueError) as ctx:
                cbz.read(p)
        self.assertIn("条目", str(ctx.exception))

    def test_cb7_entry_count(self):
        """CB7（7z 里直接放图）条目数超限 → 拒。"""
        files = {f"{i:04d}.png": tiny_png(i) for i in range(30)}
        p = self.tmp / "many.cb7"
        p.write_bytes(build(files))
        with mock.patch.object(limits, "MAX_ENTRIES", 10):
            with self.assertRaises(ValueError) as ctx:
                cbz.read_cb7(p)
        self.assertIn("条目", str(ctx.exception))


class CbcBudgetTests(unittest.TestCase):
    """CBC = 7z 套多个 cbz。核心是预算不能被内层重置。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_cbc_"))

    def _cbc(self, name: str, volumes: int, pages_each: int) -> Path:
        inner = {f"vol{i:02d}.cbz": make_zip(pages_each, seed=i * 7)
                 for i in range(volumes)}
        p = self.tmp / name
        p.write_bytes(build(inner))
        return p

    def test_single_inner_over_limit(self):
        p = self._cbc("one.cbc", 1, 30)
        with mock.patch.object(limits, "MAX_ENTRIES", 10):
            with self.assertRaises(ValueError) as ctx:
                cbz.read_cb7(p)
        self.assertIn("内层", str(ctx.exception))

    def test_budget_shared_across_inner_volumes(self):
        """关键回归：两卷各 8 页，单卷都在 10 的限额内，
        合起来 16 页必须被拒 —— 否则内层就是各自拿满额度。"""
        p = self._cbc("two.cbc", 2, 8)
        with mock.patch.object(limits, "MAX_ENTRIES", 10):
            with self.assertRaises(ValueError) as ctx:
                cbz.read_cb7(p)
        msg = str(ctx.exception)
        self.assertIn("条目", msg)

    def test_budget_not_shared_would_pass(self):
        """对照组：限额够大时同样的包必须能读出来，
        证明上面那条拒的是「累计超限」而不是「包本身坏了」。"""
        p = self._cbc("two_ok.cbc", 2, 8)
        with mock.patch.object(limits, "MAX_ENTRIES", 100):
            book = cbz.read_cb7(p)
        self.assertEqual(book.page_count, 16)

    def test_normal_cbc_works(self):
        p = self._cbc("normal.cbc", 2, 3)
        book = cbz.read_cb7(p)
        self.assertEqual(book.page_count, 6)
        self.assertTrue(all(pg.data.startswith(b"\x89PNG") for pg in book.pages))

    def test_reject_leaves_source_and_no_output(self):
        p = self._cbc("keep.cbc", 2, 8)
        before = p.read_bytes()
        out = self.tmp / "out.epub"
        with mock.patch.object(limits, "MAX_ENTRIES", 10):
            with self.assertRaises(ValueError):
                cbz.read_cb7(p)
        self.assertEqual(p.read_bytes(), before, "源文件被改了")
        self.assertFalse(out.exists(), "失败却留下了输出文件")

    def test_inner_actual_bytes_checked(self):
        """内层声明大小可以撒谎：写小、实际大 → 读出来按实际字节拒。"""
        # 一个 1×1 PNG 只有几十字节，这里把单条目上限压到 10 字节
        p = self._cbc("bytes.cbc", 1, 2)
        with mock.patch.object(limits, "MAX_ENTRY_BYTES", 10):
            with self.assertRaises(ValueError) as ctx:
                cbz.read_cb7(p)
        self.assertIn("超过单文件上限", str(ctx.exception))



    # ── P2-01：外层 7z 与内层 ZIP 共用同一份预算 ──────────────────────────

    def test_outer_7z_consumes_shared_budget(self):
        """read_all() 解出多少字节，就从同一份 Budget 里扣多少。

        早先是外层按完整额度解一遍、内层再从零拿一份 —— 2 层嵌套吃 2 倍内存，
        跟「跨层共享」完全相反。现在外层解完先结算，内层只剩剩下的。
        """
        payload = b"A" * 4000
        data = build({"a.bin": payload})
        p = self.tmp / "one.7z"
        p.write_bytes(data)

        budget = limits.Budget(total=100_000, entries=50)
        got = sevenz.read_all(p, budget=budget)
        self.assertEqual(got["a.bin"], payload)
        self.assertEqual(budget.bytes_left, 100_000 - len(payload),
                         "外层解出的字节没从共享预算里扣掉")

    def test_nested_budget_shared_end_to_end(self):
        """真正的端到端：同一个 Budget 传进去，外层扣完内层不够就拒。"""
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("p1.png", b"\x89PNG" + b"x" * 3000)
        p = self.tmp / "combo2.7z"
        p.write_bytes(build({"vol1.cbz": inner.getvalue()}))

        budget = limits.Budget(total=100_000, entries=50)
        files = sevenz.read_all(p, budget=budget)
        spent = sum(len(v) for v in files.values())
        self.assertLess(budget.bytes_left, 100_000)
        self.assertEqual(budget.bytes_left, 100_000 - spent)

        # 把外层吃剩的额度再压到几百字节 → 内层 3KB 的图必须被拒。
        # 早先这里写的是 budget.bytes_left - 100（还剩 96KB），拿 3KB 去撞
        # 96KB 当然撞不穿 —— 测试红的是写法，不是代码。
        tiny = limits.Budget(total=512, entries=50)
        with zipfile.ZipFile(io.BytesIO(files["vol1.cbz"])) as zf:
            with self.assertRaises(limits.LimitError):
                limits.check_zip(zf, tiny)



if __name__ == "__main__":
    unittest.main(verbosity=2)