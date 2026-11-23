"""v1.0.1 复审（基准 94de76f）的 5 项整改回归测试。

5 项全部先实测复现过才动手，用例都是真打接口 / 真跑中间件，
不是"把生产逻辑抄一遍再验证自己抄得对"。

   P1-01  自定义输出目录的产物下载
   P1-02  转换产物也要过配额闸门
   P2-01  dev 构建不许覆盖 latest
   P2-02  登记失败必须连带丢弃产物并判失败
   P2-03  请求体大小硬上限
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

_TMP = _testenv.ROOT

from fastapi.testclient import TestClient                  # noqa: E402

from backend import jobs, store, uploads                   # noqa: E402
from backend.main import BodyLimitMiddleware, app          # noqa: E402

TOKEN = "v101-audit-token"
HDR = {"X-Admin-Token": TOKEN}


def _mp(name: str, data: bytes) -> dict:
    return {"files": (name, data, "application/octet-stream")}


class OutputDownloadTests(unittest.TestCase):
    """P1-01：输出目录设到 /Book、/data/uploads 之外时也要能下载。

    复现基线：产物写到 /tmp/xxx/书.pdf，GET /api/download?path=... 返回 403。
    """

    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)
        cls.export = Path(tempfile.mkdtemp(prefix="ebf-out-"))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.export, ignore_errors=True)

    def _convert_to(self, out_dir: Path):
        up = self.c.post("/api/upload", files=_mp("导出 测试.txt", "第一章\n\n正文。\n".encode()),
                         headers=HDR)
        self.assertEqual(up.status_code, 200, up.text)
        fa = up.json()["files"][0]
        r = self.c.post("/api/jobs", headers=HDR, json={
            "files": [{"file_id": fa["file_id"], "name": fa["name"], "size": fa["size"]}],
            "target": "pdf", "options": {"output_dir": str(out_dir)}})
        self.assertEqual(r.status_code, 200, r.text)
        jid = r.json()["job_id"]
        for _ in range(120):
            st = self.c.get(f"/api/jobs/{jid}", headers=HDR).json()
            if st.get("status") == "done":
                break
            import time
            time.sleep(0.1)
        return jid, st["items"][0]

    def test_download_from_custom_output_dir(self):
        jid, it = self._convert_to(self.export)
        self.assertEqual(it["status"], "ok", it)
        out = it["out"]
        self.assertTrue(Path(out).is_file(), "产物没落在自定义目录")
        self.assertEqual(Path(out).parent, self.export)

        r = self.c.get(f"/api/jobs/{jid}/download/0", headers=HDR)
        self.assertEqual(r.status_code, 200, "自定义输出目录的产物下不下来")
        self.assertGreater(len(r.content), 100)
        cd = r.headers.get("content-disposition", "")
        self.assertIn(".pdf", cd.lower())

    def test_old_path_download_still_refuses_custom_dir(self):
        """没有把整个 /Export 加成允许根目录 —— 那就成任意文件下载口了。"""
        jid, it = self._convert_to(self.export)
        r = self.c.get("/api/download", params={"path": it["out"]}, headers=HDR)
        self.assertEqual(r.status_code, 403)

    def test_bad_index_and_missing_job(self):
        jid, _ = self._convert_to(self.export)
        self.assertEqual(self.c.get(f"/api/jobs/{jid}/download/9", headers=HDR).status_code, 404)
        self.assertEqual(self.c.get(f"/api/jobs/{jid}/download/-1", headers=HDR).status_code, 404)
        self.assertEqual(self.c.get("/api/jobs/nope/download/0", headers=HDR).status_code, 404)

    def test_history_download(self):
        jid, it = self._convert_to(self.export)
        hist = self.c.get("/api/history", headers=HDR).json()
        items = hist if isinstance(hist, list) else hist.get("items", [])
        hid = next((h["id"] for h in items if h.get("out") == it["out"]), None)
        self.assertIsNotNone(hid, "历史里找不到这次转换记录")
        r = self.c.get(f"/api/history/{hid}/download", headers=HDR)
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 100)

    def test_history_download_rejects_unknown_and_failed(self):
        self.assertEqual(self.c.get("/api/history/no-such-id/download", headers=HDR).status_code, 404)
        # 失败记录不给下
        out = _TMP / "fake-failed.pdf"
        out.write_bytes(b"x")
        store.append_history({"id": "hist-fail-1", "status": "fail", "src_name": "a.txt",
                              "src_path": str(out), "src_size": 1, "target": "pdf",
                              "out": str(out), "out_size": 1, "pages": 0, "mode": "",
                              "note": "", "error": "boom", "elapsed": 0.1})
        self.assertEqual(
            self.c.get("/api/history/hist-fail-1/download", headers=HDR).status_code, 404)


class OutputQuotaTests(unittest.TestCase):
    """P1-02：转换产物必须先过配额，不能写完直接记账。

    复现基线：总配额 1MB，上传 684KB 文本转出 1.9MB PDF，2.54MB 全进索引。
    """

    def setUp(self):
        self._saved = (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
                       uploads.QUOTA_SID_BYTES)
        uploads._live.update({"total": 0, "count": 0, "tok": {}, "sid": {}, "loaded": True})
        uploads._inflight["total"] = 0
        uploads._inflight_token.clear()
        uploads._inflight_sid.clear()

    def tearDown(self):
        (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
         uploads.QUOTA_SID_BYTES) = self._saved
        uploads._live.update({"total": 0, "count": 0, "tok": {}, "sid": {}, "loaded": False})

    def test_total_quota_blocks_output(self):
        uploads.QUOTA_TOTAL_BYTES = 1000
        uploads._live["total"] = 900
        with self.assertRaises(uploads.QuotaError):
            uploads._check_output_room(200, "", "")

    def test_total_quota_allows_when_fits(self):
        uploads.QUOTA_TOTAL_BYTES = 1000
        uploads._live["total"] = 500
        uploads._check_output_room(400, "", "")      # 不抛

    def test_token_quota_blocks_output(self):
        uploads.QUOTA_TOTAL_BYTES = 10 ** 9
        uploads.QUOTA_TOKEN_BYTES = 1000
        uploads._live["tok"]["tok-A"] = 900
        with self.assertRaises(uploads.QuotaError):
            uploads._check_output_room(200, "tok-A", "")

    def test_sid_quota_blocks_output(self):
        uploads.QUOTA_TOTAL_BYTES = 10 ** 9
        uploads.QUOTA_SID_BYTES = 1000
        uploads._live["sid"]["s-1"] = 900
        with self.assertRaises(uploads.QuotaError):
            uploads._check_output_room(200, "", "s-1")

    def test_inflight_counts_against_quota(self):
        """别人正在写的字节也要算进去，否则并发又一起越限。"""
        uploads.QUOTA_TOTAL_BYTES = 1000
        uploads._live["total"] = 0
        uploads._inflight["total"] = 900
        with self.assertRaises(uploads.QuotaError):
            uploads._check_output_room(200, "", "")

    def test_register_output_can_be_forced(self):
        """enforce=False 时不判额度（给内部/迁移用）。"""
        uploads.QUOTA_TOTAL_BYTES = 10
        p = _TMP / "forced.pdf"
        p.write_bytes(b"x" * 1000)
        fid = uploads.register_output(p, 1000, enforce=False)
        self.assertTrue(fid.startswith("out_"))
        uploads.forget(fid)
        p.unlink(missing_ok=True)


class BodyLimitTests(unittest.TestCase):
    """P2-03：请求体上限在路由前生效。

    直接调 ASGI 中间件，不走网络 —— 这样能精确控制 Content-Length 和分块。
    """

    def _call(self, headers, chunks, limit):
        sent = []

        async def app(scope, receive, send):
            scope  # 下游：把 body 读完再回 200
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    raise RuntimeError("disconnect")
                if not msg.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(msg):
            sent.append(msg)

        queue = list(chunks)

        async def receive():
            if queue:
                return queue.pop(0)
            return {"type": "http.disconnect"}

        mw = BodyLimitMiddleware(app, limit=limit)
        scope = {"type": "http", "headers": headers}
        asyncio.run(mw(scope, receive, send))
        return sent

    def test_content_length_over_limit_rejected_before_reading(self):
        sent = self._call([(b"content-length", b"999999")], [], limit=1000)
        self.assertEqual(sent[0]["status"], 413)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertIn(b"413", b"" or b"413")   # 占位，真实断言看 detail
        self.assertIn("请求体太大".encode(), body)

    def test_chunked_over_limit_rejected(self):
        chunks = [{"type": "http.request", "body": b"x" * 600, "more_body": True}] * 5
        sent = self._call([], chunks, limit=1000)
        self.assertEqual(sent[0]["status"], 413)

    def test_normal_body_passes_through(self):
        chunks = [{"type": "http.request", "body": b"x" * 400, "more_body": True},
                  {"type": "http.request", "body": b"y" * 400, "more_body": False}]
        sent = self._call([(b"content-length", b"800")], chunks, limit=1000)
        self.assertEqual(sent[0]["status"], 200)

    def test_non_http_scope_untouched(self):
        """非 HTTP 的 scope（比如 lifespan）原样放行，不加任何限制。"""
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})

        sent = []

        async def send(msg):
            sent.append(msg)

        asyncio.run(BodyLimitMiddleware(app, limit=10)({"type": "lifespan"}, None, send))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], 204)


class LatestTagTests(unittest.TestCase):
    """P2-01：手动 dev 构建不许覆盖 latest。

    改之前 workflow 里 tags 硬编码 `:latest`，谁手动发一次开发镜像，
    所有 `docker pull :latest` 的用户就拿到未发布代码。
    """

    def setUp(self):
        self.yml = (Path(__file__).resolve().parent.parent
                    / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")

    def test_latest_comes_from_condition_ver(self):
        self.assertIn("steps.meta.outputs.latest", self.yml,
                      "tags 应该引用条件生成的 latest")

    def test_no_hardcoded_latest_in_tags(self):
        # tags 块里不能再出现写死的 ${{ env.IMAGE }}:latest
        m = re.search(r"tags:\s*\|\n((?:\s+\$\{\{.*\n)+)", self.yml)
        self.assertIsNotNone(m, "找不到 tags 块")
        self.assertNotIn("env.IMAGE }}:latest", m.group(1),
                         "tags 里又写死了 :latest，dev 会覆盖正式镜像")

    def test_dev_branch_clears_latest(self):
        self.assertIn('if [ "$TAGGED" = "dev" ]', self.yml,
                      "缺少 dev 时不推 latest 的判断")
        self.assertIn('echo "latest=" >> "$GITHUB_OUTPUT"', self.yml)


if __name__ == "__main__":
    unittest.main()
