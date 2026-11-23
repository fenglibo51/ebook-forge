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
    """P2-03 / P1-01：请求体上限按路径生效，且在路由前拦。

    直接调 ASGI 中间件，不走网络 —— 这样能精确控制 Content-Length 和分块。
    """

    def _call(self, headers, chunks, rules=None, default=1000, path="/api/upload"):
        sent = []

        async def app(scope, receive, send):
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

        mw = BodyLimitMiddleware(app, rules=rules or [], default=default)
        scope = {"type": "http", "path": path, "headers": headers}
        asyncio.run(mw(scope, receive, send))
        return sent

    @staticmethod
    def _body(sent):
        return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    def test_content_length_over_limit_rejected(self):
        sent = self._call([(b"content-length", b"999999")], [], default=1000)
        self.assertEqual(sent[0]["status"], 413)
        self.assertIn("请求体太大".encode(), self._body(sent))

    def test_chunked_over_limit_rejected(self):
        chunks = [{"type": "http.request", "body": b"x" * 600, "more_body": True}] * 5
        sent = self._call([], chunks, default=1000)
        self.assertEqual(sent[0]["status"], 413)

    def test_exactly_at_limit_passes(self):
        """刚好等于上限应该放行 —— 边界不能差一个字节就拒。"""
        chunks = [{"type": "http.request", "body": b"x" * 1000, "more_body": False}]
        sent = self._call([(b"content-length", b"1000")], chunks, default=1000)
        self.assertEqual(sent[0]["status"], 200)

    def test_normal_body_passes_through(self):
        chunks = [{"type": "http.request", "body": b"x" * 400, "more_body": True},
                  {"type": "http.request", "body": b"y" * 400, "more_body": False}]
        sent = self._call([(b"content-length", b"800")], chunks, default=1000)
        self.assertEqual(sent[0]["status"], 200)

    def test_path_specific_limits(self):
        """分享上传、管理端上传、JSON 接口各有各的上限。

        基线问题：全局只有一个上限（管理端那个 8GB），分享访客也能灌 8GB
        才被业务层拒 —— 而他的累计配额只有几百 MB。
        """
        rules = [("/api/s/upload", 5000), ("/api/upload", 20000), ("/api/s/", 100)]
        mw = BodyLimitMiddleware(lambda *a: None, rules=rules, default=50)
        self.assertEqual(mw.limit_for("/api/s/upload"), 5000)
        self.assertEqual(mw.limit_for("/api/upload"), 20000)
        self.assertEqual(mw.limit_for("/api/s/jobs"), 100)
        self.assertEqual(mw.limit_for("/api/jobs"), 50)      # 兜底
        self.assertEqual(mw.limit_for("/"), 50)

    def test_share_upload_rejected_earlier_than_admin(self):
        """/api/s/upload 的上限必须比 /api/upload 小，否则修了等于没修。"""
        from backend.main import _BODY_RULES, _BODY_DEFAULT
        mw = BodyLimitMiddleware(lambda *a: None, rules=_BODY_RULES, default=_BODY_DEFAULT)
        share = mw.limit_for("/api/s/upload")
        admin = mw.limit_for("/api/upload")
        js = mw.limit_for("/api/jobs")
        self.assertLess(share, admin, "分享入口的上限不该跟管理端一样大")
        self.assertLess(js, share, "JSON 接口不该按上传上限放行")
        self.assertGreaterEqual(share, 100 * 1024 * 1024, "分享上限别小到正常漫画都传不上")

    def test_non_http_scope_untouched(self):
        """非 HTTP 的 scope（比如 lifespan）原样放行，不加任何限制。"""
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})

        sent = []

        async def send(msg):
            sent.append(msg)

        asyncio.run(BodyLimitMiddleware(app, rules=[], default=10)(
            {"type": "lifespan"}, None, send))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], 204)


class LatestTagTests(unittest.TestCase):
    """P2-01：只有正式 git tag 才能更新 latest。

    两轮修下来才到位：
      第一版   tags 里写死 :latest          → 手动 dev 也覆盖
      第二版   判断 TAGGED != "dev"          → 手动输入 test123 还是覆盖
      现在     判断触发来源是不是 refs/tags/v*
    """

    def setUp(self):
        self.yml = (Path(__file__).resolve().parent.parent
                    / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")

    def test_latest_comes_from_condition_ver(self):
        self.assertIn("steps.meta.outputs.latest", self.yml,
                      "tags 应该引用条件生成的 latest")

    def test_no_hardcoded_latest_in_tags(self):
        m = re.search(r"tags:\s*\|\n((?:\s+\$\{\{.*\n)+)", self.yml)
        self.assertIsNotNone(m, "找不到 tags 块")
        self.assertNotIn("env.IMAGE }}:latest", m.group(1),
                         "tags 里又写死了 :latest，手动构建会覆盖正式镜像")

    def test_latest_gated_on_git_tag_not_on_name(self):
        """判断依据必须是 GITHUB_REF，不是标签名等于什么。"""
        self.assertIn('[[ "${GITHUB_REF}" == refs/tags/v* ]]', self.yml,
                      "latest 应该由「是不是正式 tag 触发」决定")
        # 反例：按名字判断的写法不能再回来
        self.assertNotIn('if [ "$TAGGED" = "dev" ]', self.yml,
                         "又回到按标签名判断了 —— 手动输入 test123 会漏过去")
        self.assertIn('echo "latest=" >> "$GITHUB_OUTPUT"', self.yml)


class OutputLimitTests(unittest.TestCase):
    """P1-02：产物单文件上限。

    补充说明：缓存配额管的是「总共占多少」，这个是「一次别写出个巨无霸」。
    两者独立 —— 配额够也可能被单文件上限拦下。
    """

    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_share_limit_stricter_than_admin(self):
        self.assertLess(jobs.SHARE_OUTPUT_MB, jobs.MAX_OUTPUT_MB,
                        "分享访客的产物上限应该比管理端小")

    def test_limit_picked_by_token(self):
        self.assertEqual(jobs.output_limit_mb("tok-1"), jobs.SHARE_OUTPUT_MB)
        self.assertEqual(jobs.output_limit_mb(""), jobs.MAX_OUTPUT_MB)

    def test_oversized_output_dropped_and_failed(self):
        """真跑一次转换：小文本转出超过上限的 PDF，必须删产物并判失败。"""
        import time
        para = "把产物撑大的正文内容，重复很多遍。\n"
        payload = (para * (700_000 // len(para.encode()))).encode()
        up = self.c.post("/api/upload", files=_mp("超大 产物.txt", payload), headers=HDR)
        self.assertEqual(up.status_code, 200, up.text)
        fa = up.json()["files"][0]

        old = jobs.MAX_OUTPUT_MB
        jobs.MAX_OUTPUT_MB = 1          # 产物会到 ~2MB，必超
        try:
            r = self.c.post("/api/jobs", headers=HDR, json={
                "files": [{"file_id": fa["file_id"], "name": fa["name"], "size": fa["size"]}],
                "target": "pdf", "options": {}})
            self.assertEqual(r.status_code, 200, r.text)
            jid = r.json()["job_id"]
            st = None
            for _ in range(300):
                st = self.c.get(f"/api/jobs/{jid}", headers=HDR).json()
                if st.get("status") == "done":
                    break
                time.sleep(0.1)
        finally:
            jobs.MAX_OUTPUT_MB = old

        it = st["items"][0]
        self.assertEqual(it["status"], "fail", "产物超限应该判失败")
        self.assertIn("超过单文件上限", it["error"])
        self.assertEqual(it["out"], "", "超限产物不能留在任务记录里")
        self.assertEqual(it["out_size"], 0)

        # 源文件必须还在 —— 失败不能牵连上传的源文件
        srcs = list((_TMP / "uploads").glob("*"))
        self.assertTrue(any(p.suffix == ".txt" for p in srcs),
                        "源文件被牵连删掉了")


if __name__ == "__main__":
    unittest.main()
