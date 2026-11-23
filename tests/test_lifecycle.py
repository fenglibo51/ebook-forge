"""转换结果的缓存生命周期回归测试（清单 P1-03）。

问题根因：转换产物就写在 uploads/ 缓存目录里，但只有**上传的源文件**登记进
uploads.json。purge_orphans() 只认索引里的文件 —— 没登记的、超过最小年龄
（默认 1 小时）的一律当垃圾删掉。实测复现：一个 2 小时前生成的转换结果，
下次建任务触发清理时就被扫走了，用户还在 24 小时保留期内却下载不到，
而且这部分磁盘占用也没算进配额。

修法：转换结果落在缓存目录时登记进索引（kind="output"），和源文件一起
纳入 TTL / 配额 / 撤销链路的统一管理。

这里验的：
    ① 已登记的转换结果不被孤儿清理删掉
    ② 转换结果计入磁盘占用统计（含 token 维度）
    ③ 真正的孤儿（没登记）照样被清 —— 闸门不能因为修 bug 而形同虚设
    ④ 刚生成的文件（年龄不到阈值）一律不动，哪怕没登记
    ⑤ 撤销分享链接时，该链接的结果跟着清理，不留无法管理的文件
    ⑥ 落在书库里的结果不进缓存索引（那是用户自己的正式产物）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from backend import store, uploads                       # noqa: E402

KB = 1024


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_life_"))
        self.cache = self.tmp / "uploads"
        self.cache.mkdir()
        self.patches = [
            mock.patch.object(uploads, "INDEX_FILE", self.tmp / "uploads.json"),
            mock.patch.object(uploads, "QUOTA_TOTAL_BYTES", 64 << 20),
            mock.patch.object(uploads, "QUOTA_TOKEN_BYTES", 32 << 20),
            mock.patch.object(uploads, "QUOTA_SID_BYTES", 32 << 20),
            mock.patch.dict(uploads._inflight, {"total": 0}, clear=True),
            mock.patch.dict(uploads._inflight_token, {}, clear=True),
            mock.patch.dict(uploads._inflight_sid, {}, clear=True),
            mock.patch.dict(uploads._live,
                            {"total": 0, "tok": {}, "sid": {}, "count": 0,
                             "loaded": False}, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _age(self, path: Path, seconds: int) -> None:
        """把文件 mtime 往前拨，模拟「已经放了 N 秒」。"""
        t = time.time() - seconds
        os.utime(path, (t, t))

    def _make_output(self, name: str = "book.pdf", size: int = 2 * KB,
                     token: str = "", sid: str = "", job: str = "j1") -> Path:
        out = self.cache / name
        out.write_bytes(b"%PDF-1.4" + b"x" * size)
        uploads.register_output(out, size, token=token, sid=sid, job_id=job)
        return out

    # ① 已登记的结果不能被当孤儿删掉
    def test_registered_output_survives_orphan_purge(self):
        out = self._make_output()
        self._age(out, 7200)                  # 2 小时前生成（超过 1 小时阈值）
        removed = uploads.purge_orphans(self.cache)
        self.assertEqual(removed, [], f"转换结果被误删了：{removed}")
        self.assertTrue(out.is_file(), "转换结果文件不见了")

    # ② 转换结果要计入磁盘占用（总账 + 链接维度）
    def test_output_counted_in_usage(self):
        self._make_output(name="a.pdf", size=3 * KB, token="tok-X", sid="s1")
        self._make_output(name="b.pdf", size=5 * KB, token="tok-X", sid="s1")
        u = uploads.usage()
        self.assertEqual(u["total"], 8 * KB)
        self.assertEqual(u["by_token"]["tok-X"], 8 * KB)
        self.assertEqual(u["files"], 2)

    # ③ 真孤儿照样被清 —— 不能因为修这个 bug 就把清理闸门废了
    def test_real_orphan_still_removed(self):
        junk = self.cache / "leftover.bin"
        junk.write_bytes(b"half written")
        self._age(junk, 7200)
        removed = uploads.purge_orphans(self.cache)
        self.assertIn("leftover.bin", removed)
        self.assertFalse(junk.exists())

    # ④ 刚生成的文件不动（不管登没登记）
    def test_recent_file_is_never_touched(self):
        fresh = self.cache / "in-progress.pdf"
        fresh.write_bytes(b"%PDF partial")
        # mtime 就是现在，年龄 0 秒
        removed = uploads.purge_orphans(self.cache)
        self.assertEqual(removed, [])
        self.assertTrue(fresh.is_file())

    # ⑤ 撤销分享链接 → 该链接的结果跟源文件一起清
    def test_drop_token_removes_outputs_too(self):
        src = self.cache / "src.txt"
        src.write_bytes(b"source")
        uploads.register(src, "src.txt", 6, token="tok-Y", sid="s1")
        out = self._make_output(name="src.pdf", size=2 * KB, token="tok-Y", sid="s1")

        n = uploads.drop_token("tok-Y")
        self.assertGreaterEqual(n, 2, f"只清了 {n} 个，源文件和结果都该清")
        self.assertFalse(out.exists(), "撤销链接后结果文件还留着")
        self.assertEqual(uploads.usage()["total"], 0, "撤销后额度没归还")
        self.assertEqual(uploads._live["total"], 0, "实时账本没跟着减")

    # ⑥ 书库里的结果不归缓存管
    def test_library_output_not_tracked(self):
        """_track_output 只认缓存目录 —— 书库里的转换结果是用户的正式产物，
        不该进缓存索引，否则 24 小时后会被当成临时文件清掉。"""
        from backend import jobs
        lib = self.tmp / "library"
        lib.mkdir()
        out = lib / "novel.pdf"
        out.write_bytes(b"%PDF lib")

        job = mock.Mock()
        job.share_token = ""
        job.session_id = ""
        job.id = "j-lib"
        with mock.patch.object(store, "DATA_DIR", self.tmp / "data"):
            (self.tmp / "data" / "uploads").mkdir(parents=True, exist_ok=True)
            jobs._track_output(out, out.stat().st_size, job)

        self.assertEqual(uploads.usage()["files"], 0, "书库产物被登记进缓存索引了")

    # ⑦ 缓存目录里的结果必须登记
    def test_cache_output_is_tracked(self):
        from backend import jobs
        data = self.tmp / "data2"
        cache_dir = data / "uploads"
        cache_dir.mkdir(parents=True)
        out = cache_dir / "novel.pdf"
        out.write_bytes(b"%PDF cache")

        job = mock.Mock()
        job.share_token = "tok-Z"
        job.session_id = "sess-Z"
        job.id = "j-cache"
        with mock.patch.object(store, "DATA_DIR", data):
            jobs._track_output(out, out.stat().st_size, job)

        u = uploads.usage()
        self.assertEqual(u["files"], 1, "缓存里的转换结果没登记")
        self.assertEqual(u["by_token"].get("tok-Z"), out.stat().st_size)

    # ⑧ 登记失败必须抛出去 —— 语义在 94de76f 复审后反过来了。
    # 早先是「吞掉，转换照样算成功」，后果是页面显示成功、一小时后产物
    # 被 purge_orphans 当孤儿删、配额里也没这笔账。
    def test_track_output_failure_propagates(self):
        from backend import jobs
        job = mock.Mock()
        job.share_token = ""
        job.session_id = ""
        job.id = "j-x"
        data = self.tmp / "data3"
        (data / "uploads").mkdir(parents=True)
        out = data / "uploads" / "x.pdf"
        out.write_bytes(b"%PDF")
        with mock.patch.object(store, "DATA_DIR", data), \
             mock.patch.object(uploads, "register_output",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                jobs._track_output(out, 4, job)

    # ⑨ 但书库里的产物（不在缓存目录）本来就不用登记，不该抛
    def test_track_output_skips_library_files(self):
        from backend import jobs
        job = mock.Mock()
        job.share_token = ""
        job.session_id = ""
        job.id = "j-y"
        data = self.tmp / "data4"
        (data / "uploads").mkdir(parents=True)
        lib = self.tmp / "library.pdf"
        lib.write_bytes(b"%PDF")
        with mock.patch.object(store, "DATA_DIR", data), \
             mock.patch.object(uploads, "register_output",
                               side_effect=OSError("不该被调用")):
            jobs._track_output(lib, 4, job)      # 不抛，因为压根不走登记

    # ⑩ 丢弃产物：文件删掉、任务项清干净，源文件不碰
    def test_drop_output_removes_only_the_product(self):
        from backend import jobs
        job_dir = self.tmp / "uploads2"
        job_dir.mkdir()
        src = job_dir / "src.txt"
        src.write_bytes(b"source")
        out = job_dir / "out.pdf"
        out.write_bytes(b"%PDF-1.4")

        item = jobs.JobItem(src=str(src), name="x.txt", size=6)
        item.out = str(out)
        item.out_size = 8
        item.pages = 1
        jobs._drop_output(item)

        self.assertFalse(out.exists(), "产物应该被删掉")
        self.assertTrue(src.exists(), "源文件不能被牵连")
        self.assertEqual(item.out, "")
        self.assertEqual(item.out_size, 0)
        self.assertEqual(item.pages, 0)


if __name__ == "__main__":
    unittest.main()
