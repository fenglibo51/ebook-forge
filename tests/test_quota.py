"""上传累计配额（P1-4）的回归测试。

单次请求限额挡不住「同一条分享链接传几十次」——每次都在限额内，
几十次之后 /data/uploads 就满了。这里验的是累计那一层：

    ① 全目录总额度
    ② 单条分享链接额度
    ③ 单会话（sid）额度
    ④ 并发上传不会一起越过上限
    ⑤ 失败/中止后「正在写」的记账必须归零，否则额度会永久泄漏
    ⑥ 孤儿清理不能误删正在上传的新文件

全部用缩小后的额度，不造大文件。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from backend import uploads                              # noqa: E402

KB = 1024


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_quota_"))
        self.upload_dir = self.tmp / "uploads"
        self.upload_dir.mkdir()
        self.patches = [
            mock.patch.object(uploads, "INDEX_FILE", self.tmp / "uploads.json"),
            mock.patch.object(uploads, "QUOTA_TOTAL_BYTES", 4 << 20),
            mock.patch.object(uploads, "QUOTA_TOKEN_BYTES", 2 << 20),
            mock.patch.object(uploads, "QUOTA_SID_BYTES", 8 << 20),   # 默认放开，专测 sid 的用例单独压紧
            mock.patch.dict(uploads._inflight, {"total": 0}, clear=True),
            mock.patch.dict(uploads._inflight_token, {}, clear=True),
            mock.patch.dict(uploads._inflight_sid, {}, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    # ── 辅助：模拟一次完整的「占额度 + 登记」 ──
    def _upload(self, nbytes: int, token: str = "", sid: str = "") -> None:
        t = uploads.begin(token, sid)
        try:
            t.admit(nbytes)
        finally:
            t.finish()
        f = self.upload_dir / f"{uploads.new_file_id()}.bin"
        f.write_bytes(b"x" * nbytes)
        uploads.register(f, "x.bin", nbytes, token=token, sid=sid)

    # ① 单条链接的累计额度
    def test_token_quota_blocks_repeat_uploads(self):
        """同一条链接连传 3 次，第三次撞上 2MB 链接额度。"""
        for _ in range(2):
            self._upload(800 * KB, token="tok-A", sid="s1")
        with self.assertRaises(uploads.QuotaError) as ctx:
            self._upload(800 * KB, token="tok-A", sid="s2")
        self.assertIn("分享链接", str(ctx.exception))

    # ② 单会话额度比链接额度更紧
    def test_sid_quota_is_tighter(self):
        with mock.patch.object(uploads, "QUOTA_SID_BYTES", 1 << 20):
            self._upload(600 * KB, token="tok-A", sid="s1")
            with self.assertRaises(uploads.QuotaError) as ctx:
                self._upload(600 * KB, token="tok-A", sid="s1")
            self.assertIn("会话", str(ctx.exception))

    # ③ 别的链接不共用额度
    def test_other_token_has_own_quota(self):
        self._upload(1_500 * KB, token="tok-A", sid="s1")
        self._upload(1_500 * KB, token="tok-B", sid="s2")   # 不该被 A 拖累
        u = uploads.usage()
        self.assertEqual(u["by_token"]["tok-A"], 1_500 * KB)
        self.assertEqual(u["by_token"]["tok-B"], 1_500 * KB)

    # ④ 全目录总额度
    def test_total_quota(self):
        for i in range(3):
            self._upload(1_200 * KB, token=f"tok-{i}", sid=f"s{i}")
        with self.assertRaises(uploads.QuotaError) as ctx:
            self._upload(1_200 * KB, token="tok-x", sid="sx")
        self.assertIn("上传目录已满", str(ctx.exception))

    # ⑤ 并发：同时申请，合计不能越过上限
    def test_concurrent_admit_never_exceeds(self):
        """20 个线程同时各要 200KB，总额度只有 4MB → 最多放行 20 个中的一部分，
        但**放行的总量绝不超过 4MB**。"""
        allowed = []
        lock = threading.Lock()
        gate = threading.Barrier(20)

        def worker():
            t = uploads.begin("tok", "sid")
            try:
                gate.wait(timeout=10)
                try:
                    t.admit(200 * KB)
                    with lock:
                        allowed.append(200 * KB)
                except uploads.QuotaError:
                    pass
            finally:
                t.finish()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

        self.assertLessEqual(sum(allowed), uploads.QUOTA_TOTAL_BYTES,
                             "并发申请把总量写超了")
        self.assertGreater(len(allowed), 0, "一个都没放行，说明额度算错了")

    # ⑥ 失败后记账必须归零
    def test_finish_releases_inflight(self):
        t = uploads.begin("tok", "s1")
        t.admit(500 * KB)
        self.assertEqual(uploads._inflight["total"], 500 * KB)
        t.finish()
        self.assertEqual(uploads._inflight["total"], 0)
        self.assertEqual(uploads._inflight_token, {})
        self.assertEqual(uploads._inflight_sid, {})

    def test_finish_is_idempotent(self):
        """重复 finish 不能把别人的额度减掉。"""
        t = uploads.begin("tok", "s1")
        t.admit(500 * KB)
        t.finish()
        t.finish()
        self.assertEqual(uploads._inflight["total"], 0)

    # ⑦ 清理后额度恢复
    def test_quota_recovers_after_cleanup(self):
        self._upload(1_100 * KB, token="tok-A", sid="s1")
        with self.assertRaises(uploads.QuotaError):      # 2.2MB > 2MB 链接额度
            self._upload(1_100 * KB, token="tok-A", sid="s1")
        # 模拟 TTL 清理：文件删了、索引也清了
        for rec in list(uploads._load().values()):
            Path(rec["path"]).unlink()
            uploads.forget(Path(rec["path"]).stem)
        uploads.purge_missing()
        self._upload(1_100 * KB, token="tok-A", sid="s1")   # 又能传了

    # ⑧ 孤儿清理不能碰刚上传的文件
    def test_purge_orphans_spares_fresh_files(self):
        fresh = self.upload_dir / "just-created.bin"
        fresh.write_bytes(b"y" * 100)
        old = self.upload_dir / "old-orphan.bin"
        old.write_bytes(b"z" * 100)
        import os
        os.utime(old, (0, 0))                     # 装成很老的
        removed = uploads.purge_orphans(self.upload_dir)
        self.assertIn("old-orphan.bin", removed)
        self.assertTrue(fresh.exists(), "刚建的文件被误删了")
        self.assertFalse(old.exists())

    def test_purge_orphans_keeps_registered(self):
        self._upload(100, token="", sid="")
        rec = next(iter(uploads._load().values()))
        removed = uploads.purge_orphans(self.upload_dir, min_age=0)
        self.assertEqual(removed, [])
        self.assertTrue(Path(rec["path"]).exists())

    def test_stats_shape(self):
        self._upload(1 << 20, token="", sid="")
        st = uploads.stats()
        self.assertEqual(st["used_bytes"], 1 << 20)
        self.assertEqual(st["limit_bytes"], uploads.QUOTA_TOTAL_BYTES)
        self.assertEqual(st["files"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
