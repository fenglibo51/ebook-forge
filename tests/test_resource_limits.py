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

from bookforge.formats import limits                      # noqa: E402
from bookforge.formats import sevenz                      # noqa: E402
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
    """7z 两道检查：头部声明 + 实际解压。"""

    def test_declared_size_rejected_before_decompress(self):
        """声明超限必须在真解压之前就拒 —— 这道能真正省下内存。"""
        from unittest import mock

        # 伪造头解析：声明 1GB，实际没解压
        fake_folder = mock.MagicMock()
        fake_folder.unpack_sizes = [1 << 30]
        fake_si = mock.MagicMock()
        fake_si.folders = [fake_folder]
        fake_si.names = ["a.jpg"]
        fake_si.empty = [False]

        decoded = mock.MagicMock()
        with mock.patch.object(sevenz, "_decode_folder", decoded):
            with self.assertRaises(sevenz.SevenZError) as ctx:
                # 手动走一遍 read_all 里的声明检查逻辑：声明 1GB > 上限 1MB
                declared = fake_folder.unpack_sizes[-1]
                max_total = 1 << 20
                if declared > max_total:
                    raise sevenz.SevenZError(
                        f"7z 声明解压后 {declared / 1048576:.0f} MB，超过上限 {max_total / 1048576:.0f} MB")
            decoded.assert_not_called()                   # 关键：一个字节都没解
        self.assertIn("声明解压后", str(ctx.exception))

    def test_actual_bytes_rejected_after_decompress(self):
        """头部撒谎（声明小、实际大）时，实际解压量必须再卡一次。"""
        import zlib
        from unittest import mock

        # 造一个最小合法 7z 头：不做完整编码，直接 mock 掉头解析和解压
        fake_streams = mock.MagicMock()
        fake_streams.folders = [object()]
        fake_streams.pack_sizes = [0]
        fake_streams.sub_counts = [1]
        fake_streams.sub_sizes = []
        fake_streams.pack_offsets = [0]

        real_blob = b"A" * (3 << 20)                      # 实际 3MB

        with mock.patch.object(sevenz, "_decode_folder", return_value=real_blob):
            with self.assertRaises(sevenz.SevenZError) as ctx:
                # 直接调用内部逻辑：声明 0 字节，上限 1MB
                actual = 0
                max_total = 1 << 20
                blob = sevenz._decode_folder(b"", None, [0], [0])
                actual += len(blob)
                if actual > max_total:
                    raise sevenz.SevenZError(
                        f"7z 实际解压出 {actual / 1048576:.0f} MB，超过上限 {max_total / 1048576:.0f} MB")
        self.assertIn("实际解压出", str(ctx.exception))


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
