"""多文件上传的「整批原子」语义回归测试（清单 P2-02）。

早先的行为：传 3 个文件，第 2 个超限 → 接口返回 413，但**第 1 个已经登记落盘、
占着配额**。用户看到的是一次失败，服务器上却留了半批文件；他重试一次就再占
一份额度，几次之后配额莫名其妙满了，而他从没成功过。

现在语义明确：**要么整批成功，要么一个都不留**。

⚠️ HTTP 状态的语义（清单 P2-02 允许两种选择，这里选的是第一种）：
   整批原子成功 —— 响应里的 files 要么是全部，要么请求失败、目录里什么都不多。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from fastapi.testclient import TestClient                 # noqa: E402

from backend import api, main, uploads                    # noqa: E402

KB = 1024


class BatchRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf_batch_"))
        self.upload_dir = self.tmp / "uploads"
        self.upload_dir.mkdir()
        self.patches = [
            mock.patch.object(uploads, "INDEX_FILE", self.tmp / "uploads.json"),
            mock.patch.object(uploads, "QUOTA_TOTAL_BYTES", 64 << 20),
            mock.patch.object(uploads, "QUOTA_TOKEN_BYTES", 64 << 20),
            mock.patch.object(uploads, "QUOTA_SID_BYTES", 64 << 20),
            mock.patch.object(api, "UPLOAD_DIR", self.upload_dir),
            mock.patch.dict(uploads._inflight, {"total": 0}, clear=True),
            mock.patch.dict(uploads._inflight_token, {}, clear=True),
            mock.patch.dict(uploads._inflight_sid, {}, clear=True),
            mock.patch.dict(uploads._live,
                            {"total": 0, "tok": {}, "sid": {}, "count": 0,
                             "loaded": False}, clear=True),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _files(self):
        return sorted(p.name for p in self.upload_dir.iterdir())

    def _post(self, specs: list[tuple[str, bytes]]):
        return self.client.post(
            "/api/upload",
            files=[("files", (n, c, "application/octet-stream")) for n, c in specs])

    # ① 全部合规 → 全部保留
    def test_all_good_keeps_everything(self):
        r = self._post([("a.txt", b"aaa"), ("b.txt", b"bbb"), ("c.txt", b"ccc")])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["files"]), 3)
        self.assertEqual(len(self._files()), 3)
        self.assertEqual(uploads.usage()["by_token"].get("", 0) >= 0, True)

    # ② 中间一个超限 → 前面已成功的也得回滚
    def test_middle_failure_rolls_back_earlier_files(self):
        # 单文件上限压到 10 字节：a 合规，b 超限，c 根本没机会
        with mock.patch.object(api, "MAX_FILE_MB", 10 / (1 << 20)):
            r = self._post([("a.txt", b"ok"), ("b.txt", b"x" * 100), ("c.txt", b"ok")])
        self.assertEqual(r.status_code, 413, r.text)
        self.assertEqual(self._files(), [], f"失败后还留着文件：{self._files()}")
        self.assertEqual(uploads.usage()["files"], 0, "索引里还留着记录")
        self.assertEqual(uploads._live["total"], 0, "实时账本没回滚，额度被吃掉")
        self.assertEqual(uploads._inflight["total"], 0, "inflight 泄漏")

    # ③ 整批总量超限同样回滚
    def test_total_size_limit_rolls_back(self):
        with mock.patch.object(api, "MAX_UPLOAD_MB", 1 / (1 << 20)):
            r = self._post([("a.txt", b"x" * 100), ("b.txt", b"y" * 100)])
        self.assertEqual(r.status_code, 413, r.text)
        self.assertEqual(self._files(), [])
        self.assertEqual(uploads.usage()["files"], 0)

    # ④ 一个文件都没有 → 400，不建空任务
    def test_empty_upload_rejected(self):
        r = self.client.post("/api/upload", files=[])
        self.assertIn(r.status_code, (400, 422))

    # ⑤ 回滚之后额度要能正常再用（不能因为一次失败就永久少一块）
    def test_quota_usable_after_rollback(self):
        with mock.patch.object(api, "MAX_FILE_MB", 10 / (1 << 20)):
            self._post([("a.txt", b"ok"), ("b.txt", b"x" * 100)])
        r = self._post([("good.txt", b"hello")])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["files"]), 1)


if __name__ == "__main__":
    unittest.main()
