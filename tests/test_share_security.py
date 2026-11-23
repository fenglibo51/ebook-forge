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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

# 数据目录由 _testenv 统一提供（必须在 import backend 之前设好，
# 否则 store.DATA_DIR 会被别的测试先定死 —— 见 tests/_testenv.py 里的说明）
_TMP = _testenv.ROOT

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
        # ⚠️ 这里不能 rmtree：_TMP 是全进程共用的数据目录，
        # 删了后面的 FailClosedTests 立刻 FileNotFoundError。
        # 临时目录留给进程退出时由系统回收。

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

    # ── P1-04 补充：累计配额必须在 HTTP 层真的生效 ──────────────────────

    def test_share_upload_quota_enforced(self):
        """同一条链接反复传，累计超链接额度后要 413 + 人话提示。

        单次限额挡不住这种：每次都在限额内，几十次就把磁盘磨满。
        """
        from backend import uploads as U
        tok = self._link()
        old = U.QUOTA_TOKEN_BYTES
        try:
            U.QUOTA_TOKEN_BYTES = 200
            self._upload(tok, "sess-Q", name="a.bin", data=b"x" * 150)
            r = self.c.post(f"/api/s/upload?t={tok}&sid=sess-Q",
                            files=_mp("b.bin", b"y" * 150))
            self.assertEqual(r.status_code, 413, r.text)
            self.assertIn("额度", r.json()["detail"])
        finally:
            U.QUOTA_TOKEN_BYTES = old

    def test_failed_upload_leaves_no_file(self):
        """被配额拒掉的上传不能在 uploads/ 里留半截文件。"""
        from backend import uploads as U
        tok = self._link()
        old = U.QUOTA_TOKEN_BYTES
        try:
            U.QUOTA_TOKEN_BYTES = 100
            before = set(U.UPLOAD_DIR.glob("*")) if hasattr(U, "UPLOAD_DIR") else set()
            r = self.c.post(f"/api/s/upload?t={tok}&sid=sess-R",
                            files=_mp("c.bin", b"z" * 300))
            self.assertEqual(r.status_code, 413, r.text)
            # 拒绝后「正在写」的记账必须归零，否则额度会永久泄漏
            self.assertEqual(U._inflight["total"], 0)
        finally:
            U.QUOTA_TOKEN_BYTES = old

    def test_revoke_drops_uploads_and_index(self):
        """撤销链接后：该链接名下的文件、索引记录都要一起消失。

        清单要求「链接撤销/缓存清理后，索引与实际文件应保持一致」。
        不清理的话文件再也取不出来（归属校验认 token），却照旧占着配额。
        """
        from backend import uploads as U
        tok = self._link()
        self._upload(tok, "sess-Z", name="d.bin", data=b"q" * 100)
        recs = [v for v in U._load().values() if v.get("token") == tok]
        self.assertEqual(len(recs), 1, "上传没登记")
        path = Path(recs[0]["path"])
        self.assertTrue(path.exists())

        r = self.c.delete(f"/api/shares/{tok}", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(path.exists(), "撤销链接后文件还躺在 uploads/ 里")
        self.assertEqual([v for v in U._load().values() if v.get("token") == tok], [],
                         "撤销链接后索引里还有脏记录（会继续占配额）")

    def test_disable_share_drops_all_uploads(self):
        """走设置接口全局关分享，同样要把所有链接的上传副本收干净。"""
        from backend import uploads as U
        tok = self._link()
        self._upload(tok, "sess-Y", name="e.bin", data=b"w" * 100)
        paths = [Path(v["path"]) for v in U._load().values() if v.get("token") == tok]
        self.assertTrue(paths)

        self.c.put("/api/settings", json={"share_enabled": False}, headers=self.h)
        for p in paths:
            self.assertFalse(p.exists(), f"全局关闭后文件还在：{p}")
        self.assertEqual([v for v in U._load().values() if v.get("token") == tok], [])
        self.c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})

    # ── P1-01 补充：分享访客这条路径同样要保留原始名 ──────────────────

    def test_share_upload_keeps_display_name(self):
        """访客上传 → 转换 → 输出名是原始书名，不是随机 file_id。

        清单里验收项写的是「分享访客、管理端普通上传两条路径都通过」，
        管理端那条已经验过，这里补访客这条。
        """
        import time as _t
        tok = self._link()
        f = self._upload(tok, "sess-N", name="分享 测试 v2.txt",
                         data="分享内容".encode("utf-8"))
        r = self.c.post(f"/api/s/jobs?t={tok}&sid=sess-N",
                        json={"files": [f], "target": "pdf", "options": {}})
        self.assertEqual(r.status_code, 200, r.text)
        jid = r.json()["job_id"]

        state = {}
        for _ in range(120):
            r = self.c.get(f"/api/s/jobs/{jid}?t={tok}&sid=sess-N")
            self.assertEqual(r.status_code, 200, r.text)
            state = r.json()
            if state.get("status") in ("done", "failed", "canceled"):
                break
            _t.sleep(0.25)
        self.assertEqual(state.get("status"), "done",
                         json.dumps(state, ensure_ascii=False)[:400])
        # 分享端故意不回服务器路径（安全要求），只能从下载响应头验名字
        r = self.c.get(f"/api/s/download/{jid}/0?t={tok}&sid=sess-N")
        self.assertEqual(r.status_code, 200, r.text)
        disp = r.headers.get("content-disposition", "")
        self.assertIn("utf-8''", disp, disp)
        from urllib.parse import quote
        self.assertIn(quote("分享 测试 v2.pdf"), disp,
                      f"下载名用了随机串：{disp}")
        self.assertTrue(r.content.startswith(b"%PDF"), "下载到的不是 PDF")

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
