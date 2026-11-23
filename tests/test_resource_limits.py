"""资源耗尽防护的回归测试（P1-05）。

核心是「**用户给的字节不能决定我们吃多少内存**」：
    ① 压缩包头部声明的大小会撒谎 —— 声明检查之后还必须按实际解压量再卡一次
    ② CBZ / EPUB / DOCX 三个 zip 入口现在共用一套限额，别只保一个漏两个
    ③ 条目数量也得卡，否则「几百万个 1 字节文件」能把 inode 和循环时间吃光

这些用例全部是构造出来的恶意/异常包，不需要真实大文件。
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from bookforge.formats import limits                      # noqa: E402
from bookforge.formats import sevenz                      # noqa: E402
from fixtures.gen_7z import build                          # noqa: E402
from bookforge.formats.docx import read as docx_read      # noqa: E402
from bookforge.formats.epub import read_text              # noqa: E402


def _zip_with_big_entry(path: Path, name: str, declared: int) -> None:
    """造一个 zip，里面有个条目声明解压后 declared 字节。

    注意：不能只改 local header —— zipfile 读的是 central directory，
    两边必须一致才能让 ZipInfo.file_size 变成我们想要的值。
    所以这里真实写入 declared 字节（ZIP_STORED，不压缩，几 MB 很快），
    这恰好也模拟了「真炸开就是这么占内存」的形态。
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(name, b"\0" * declared)


class ZipLimitTests(unittest.TestCase):
    """CBZ / EPUB / DOCX 三个入口共用同一套限额。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_limits_"))
        self._orig = limits.MAX_ENTRY_BYTES

    def tearDown(self):
        limits.MAX_ENTRY_BYTES = self._orig

    def test_epub_rejects_oversized_entry(self):
        """EPUB 里有个条目声明解压后 1GB，单条目上限 10MB → 必须拒绝。"""
        limits.MAX_ENTRY_BYTES = 10 << 20
        p = self.tmp / "bomb.epub"
        _zip_with_big_entry(p, "OEBPS/big.jpg", 1 << 30)
        with self.assertRaises(Exception) as ctx:
            read_text(p)
        self.assertIn("上限", str(ctx.exception))

    def test_docx_rejects_oversized_entry(self):
        limits.MAX_ENTRY_BYTES = 10 << 20
        p = self.tmp / "bomb.docx"
        _zip_with_big_entry(p, "word/media/image1.png", 1 << 30)
        with self.assertRaises(Exception) as ctx:
            docx_read(p)
        self.assertIn("上限", str(ctx.exception))

    def test_entry_count_limit(self):
        """条目数超限要拒绝 —— 不是只看字节数。"""
        limits.MAX_ENTRIES = 5
        try:
            p = self.tmp / "many.epub"
            with zipfile.ZipFile(p, "w") as zf:
                for i in range(20):
                    zf.writestr(f"f{i}.txt", b"x")
            with self.assertRaises(Exception) as ctx:
                read_text(p)
            self.assertIn("条目", str(ctx.exception))
        finally:
            limits.MAX_ENTRIES = int(limits.os.environ.get("BOOKFORGE_MAX_ENTRIES", "5000"))

    def test_limits_are_env_tunable(self):
        """限额必须能用环境变量调 —— 大书用户要能放开。"""
        import os
        os.environ["BOOKFORGE_MAX_ENTRY_MB"] = "64"
        try:
            import importlib
            importlib.reload(limits)
            self.assertEqual(limits.MAX_ENTRY_BYTES, 64 << 20)
        finally:
            del os.environ["BOOKFORGE_MAX_ENTRY_MB"]
            import importlib
            importlib.reload(limits)


class SevenZLimitTests(unittest.TestCase):
    """7z 的两道闸，**全部走真实的 sevenz.read_all()**。

    早先这里是「把生产检查逻辑在测试里抄一遍再主动抛异常」，
    生产代码删掉测试照样绿 —— 等于没测。现在用 gen_7z 造真文件喂给真读取器。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_7z_"))

    def _write(self, name: str, blob: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(blob)
        return p

    def test_declared_size_rejected(self):
        """头里声明解压后 1GB → 真 read_all 在解压前就拒。"""
        p = self._write("declared.7z",
                        build({"a.txt": b"x" * 100},
                              declared_unpack={"a.txt": 1 << 30}))
        with self.assertRaises(sevenz.SevenZError) as ctx:
            sevenz.read_all(p, max_total=1 << 20)
        self.assertIn("声明解压后", str(ctx.exception))

    def test_actual_bytes_rejected(self):
        """头部撒谎（声明 100 字节、实际 3MB）→ 解压后按实际字节再拒一次。"""
        payload = b"A" * (3 << 20)
        p = self._write("liar.7z",
                        build({"a.txt": payload}, declared_unpack={"a.txt": 100}))
        with self.assertRaises(sevenz.SevenZError) as ctx:
            sevenz.read_all(p, max_total=1 << 20)
        msg = str(ctx.exception)
        self.assertTrue("超" in msg or "上限" in msg, msg)

    def test_lzma_bomb_stops_midway(self):
        """LZMA 炸弹：压缩后几十 KB，解压 8MB，上限 1MB → 必须中止而不是解完再看。"""
        payload = b"\0" * (8 << 20)
        blob = build({"big.bin": payload}, use_lzma=True,
                     declared_unpack={"big.bin": 64})
        self.assertLess(len(blob), 1 << 20, "压缩后应该很小，否则不算炸弹")
        p = self._write("bomb.7z", blob)
        with self.assertRaises(sevenz.SevenZError):
            sevenz.read_all(p, max_total=1 << 20)

    def test_multi_folder_content_not_mixed(self):
        """多 folder 的 7z 每个文件必须是自己的内容。

        回归用：早先 pack_indices 被当全局下标，所有 folder 都解第一段数据，
        三个文件全变成同一个内容且**不报错**。
        """
        data = {"a.txt": b"AAA-first", "b.txt": b"BBB-second", "c.txt": b"CCC-third"}
        for use_lzma in (False, True):
            p = self._write(f"multi{int(use_lzma)}.7z",
                            build(data, use_lzma=use_lzma))
            got = sevenz.read_all(p)
            self.assertEqual(got, data, f"use_lzma={use_lzma} 时内容串了")

    def test_normal_7z_still_works(self):
        """正常包不能被限额误伤。"""
        data = {"x.txt": b"hello", "y.txt": b"world"}
        p = self._write("ok.7z", build(data, use_lzma=True))
        self.assertEqual(sevenz.read_all(p), data)

    def test_limits_come_from_production_code(self):
        """限额必须真的由生产代码执行 —— 放宽后才允许通过同一份文件。

        这条证明上面那些拒绝不是因为测试自己抛的异常：
        同一个文件，上限 1MB 拒、上限 100MB 过。
        """
        payload = b"B" * (2 << 20)
        p = self._write("two.7z", build({"a.txt": payload}, use_lzma=True))
        with self.assertRaises(sevenz.SevenZError):
            sevenz.read_all(p, max_total=1 << 20)
        self.assertEqual(sevenz.read_all(p, max_total=100 << 20), {"a.txt": payload})

    def test_failure_leaves_source_intact(self):
        """拒绝之后源文件还在，目录里没多出垃圾。"""
        p = self._write("keep.7z", build({"a.bin": b"C" * (2 << 20)},
                                        declared_unpack={"a.bin": 10}))
        before = p.stat().st_size
        with self.assertRaises(sevenz.SevenZError):
            sevenz.read_all(p, max_total=1 << 20)
        self.assertTrue(p.exists())
        self.assertEqual(p.stat().st_size, before)
        self.assertEqual([f.name for f in self.tmp.iterdir()], ["keep.7z"])


class CleanupTests(unittest.TestCase):
    """超限/失败后不能留垃圾文件。"""

    def test_no_temp_left_behind_on_reject(self):
        tmp = Path(tempfile.mkdtemp(prefix="ebf_clean_"))
        p = tmp / "bomb.epub"
        _zip_with_big_entry(p, "big.jpg", 1 << 30)
        orig = limits.MAX_ENTRY_BYTES
        limits.MAX_ENTRY_BYTES = 1 << 20
        try:
            try:
                read_text(p)
            except Exception:
                pass
            # 原始包本身要还在（我们只读不删用户文件）
            self.assertTrue(p.exists())
            # 目录里不该多出别的临时文件
            extra = [f.name for f in tmp.iterdir() if f.name != "bomb.epub"]
            self.assertEqual(extra, [], f"拒绝之后留下了临时文件：{extra}")
        finally:
            limits.MAX_ENTRY_BYTES = orig


if __name__ == "__main__":
    unittest.main()
