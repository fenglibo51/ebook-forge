"""分享模式的权限边界回归测试（P1-01 ~ P1-03）。

这些用例全部来自实测复现过的漏洞，不是"看起来可能有问题"。
改动这几块之前先跑一遍，别把已经堵上的口子重新打开。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 本文件要跑在真实的 backend 模块上，用临时数据目录隔离
_TMP = Path(tempfile.mkdtemp(prefix="ebf_audit_"))
import os                                                        # noqa: E402

os.environ["EBOOKFORGE_DATA"] = str(_TMP)

from fastapi.testclient import TestClient                        # noqa: E402

from backend.main import app                                     # noqa: E402

TOKEN = "audit-token-8chars"


def _mp(name: str, data: bytes) -> dict:
    return {"files": (name, data, "application/octet-stream")}


class ShareBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)
        # 开分享模式
        cls.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})
        cls.h = {"X-Admin-Token": TOKEN}

    @classmethod
    def tearDownClass(cls):
        cls.c.post("/api/share/toggle", json={"enabled": False}, headers=cls.h)
        shutil.rmtree(_TMP, ignore_errors=True)

    def _link(self) -> str:
        r = self.c.post("/api/shares", json={"ttl_hours": 1}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _upload(self, token: str, sid: str, name="a.txt", data=b"hello") -> dict:
        r = self.c.post(f"/api/s/upload?t={token}&sid={sid}", files=_mp(name, data))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["files"][0]

    # ── P1-01：上传文件必须绑定到上传者 ────────────────────────────────

    def test_upload_does_not_leak_server_path(self):
        """上传响应里不能出现服务器绝对路径 —— 拿到就能拼别人文件的路径。"""
        tok = self._link()
        f = self._upload(tok, "sid-1")
        self.assertNotIn("path", f)
        self.assertIn("file_id", f)

    def test_other_session_cannot_reuse_file(self):
        """A 上传的文件，B 拿同一个 file_id 不能建任务。"""
        tok = self._link()
        f = self._upload(tok, "sess-A")
        r = self.c.post(f"/api/s/jobs?t={tok}&sid=sess-B",
                        json={"files": [f], "target": "pdf", "options": {}})
        self.assertEqual(r.status_code, 403)
        self.assertIn("不是你上传", r.json()["detail"])

    def test_owner_can_use_own_file(self):
        """A 自己用必须正常。"""
        tok = self._link()
        f = self._upload(tok, "sess-A2")
        r = self.c.post(f"/api/s/jobs?t={tok}&sid=sess-A2",
                        json={"files": [f], "target": "pdf", "options": {}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_share_api_rejects_raw_path(self):
        """分享接口不接受服务器路径，只认 file_id。"""
        tok = self._link()
        r = self.c.post(f"/api/s/jobs?t={tok}&sid=sess-A",
                        json={"files": [{"path": "/data/uploads/x.txt", "name": "x.txt", "size": 1}],
                              "target": "pdf", "options": {}})
        self.assertEqual(r.status_code, 403)
        self.assertIn("不接受服务器路径", r.json()["detail"])

    # ── P1-04：并发上传不能互相覆盖 ───────────────────────────────────

    def test_concurrent_same_name_uploads(self):
        """10 个同名文件并发上传，每个都要独立存在。"""
        import concurrent.futures as cf
        tok = self._link()

        def one(i):
            return self._upload(tok, "sess-C", name="same.txt", data=f"内容{i}".encode())

        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(one, range(10)))
        ids = {r["file_id"] for r in results}
        self.assertEqual(len(ids), 10, "file_id 必须互不相同")
        for r in results:
            self.assertEqual(r["name"], "same.txt", "展示名保留原名")

    # ── P1-02：任何一个关闭入口都要让旧链接失效 ────────────────────────

    def test_disable_via_settings_revokes_links(self):
        tok = self._link()
        self._upload(tok, "sess-D")

        # 走通用设置接口关闭（不是 /api/share/toggle）
        r = self.c.put("/api/settings", json={"share_enabled": False}, headers=self.h)
        self.assertEqual(r.status_code, 200)

        r = self.c.post(f"/api/s/upload?t={tok}&sid=sess-D", files=_mp("b.txt", b"x"))
        self.assertEqual(r.status_code, 403, "关闭后旧 token 必须失效")

        # 恢复
        self.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})

    def test_cannot_create_link_when_disabled(self):
        self.c.post("/api/share/toggle", json={"enabled": False}, headers=self.h)
        r = self.c.post("/api/shares", json={"ttl_hours": 1}, headers=self.h)
        self.assertEqual(r.status_code, 403)
        self.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})

    def test_share_ctx_checks_global_switch(self):
        """就算 token 还在，分享模式关了也不许用。"""
        tok = self._link()
        # 直接把 settings 改成关闭，但不清 shares.json（模拟异常路径）
        settings = _TMP / "settings.json"
        data = json.loads(settings.read_text())
        data["share_enabled"] = False
        settings.write_text(json.dumps(data), encoding="utf-8")

        r = self.c.post(f"/api/s/upload?t={tok}&sid=s", files=_mp("c.txt", b"y"))
        self.assertEqual(r.status_code, 403)
        self.assertIn("分享模式已关闭", r.json()["detail"])

        self.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})


class FailClosedTests(unittest.TestCase):
    """P1-03：配置损坏时不能退化成免认证。"""

    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)
        cls.h = {"X-Admin-Token": TOKEN}

    def setUp(self):
        self.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})
        self.settings = _TMP / "settings.json"
        self.backup = self.settings.read_text()

    def tearDown(self):
        self.settings.write_text(self.backup, encoding="utf-8")

    def test_corrupt_settings_fails_closed(self):
        self.settings.write_text("{ 这不是合法 JSON", encoding="utf-8")
        r = self.c.get("/api/settings")
        self.assertEqual(r.status_code, 503, "敏感接口必须 503，不能放行")

    def test_health_still_works_when_corrupt(self):
        """健康检查必须豁免 —— 否则容器被判不健康会反复重启，连排查入口都没了。"""
        self.settings.write_text("{ 坏", encoding="utf-8")
        self.assertEqual(self.c.get("/api/health").status_code, 200)
        self.assertEqual(self.c.get("/api/capabilities").status_code, 200)

    def test_frontend_still_loads_when_corrupt(self):
        """前端页面要能打开，不然用户看不到任何错误提示。"""
        self.settings.write_text("{ 坏", encoding="utf-8")
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_recovers_after_fix(self):
        self.settings.write_text("{ 坏", encoding="utf-8")
        self.assertEqual(self.c.get("/api/settings").status_code, 503)
        self.settings.write_text(json.dumps({"share_enabled": True, "admin_token": TOKEN}),
                                 encoding="utf-8")
        self.assertEqual(self.c.get("/api/settings", headers=self.h).status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
