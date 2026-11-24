"""v1.0.9 复审（基准 d64dcba）的 1 个 P1 + 1 个 P2 + 1 个 P3 回归测试。

  P1  发布流程先 push 正式标签再冒烟 —— 冒烟失败时 GHCR 的 latest 已经是坏镜像
  P2  单页像素预算挡不住「几百页加起来」的内存
  P3  render_dpi 传 Infinity 抛 OverflowError，两个入口都变成 500
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from fastapi import HTTPException                          # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

from backend.api import _clean_render_dpi                  # noqa: E402
from backend.jobs import _render_limit_for, _safe_dpi      # noqa: E402
from backend.main import app                               # noqa: E402
from bookforge.pdf import to_comic                         # noqa: E402

TOKEN = "v110-token"
HDR = {"X-Admin-Token": TOKEN}


def _admin_headers() -> dict:
    """取当前生效的管理令牌。

    不能写死：全量跑测试时前面的用例会把 settings.json 里的 token 换成别的值，
    固定值会 401。分享没开、也没有环境变量令牌时是免密的，返回空 dict 即可。
    """
    from backend import store
    tok = str(store.load_settings().get("admin_token", "") or "")
    return {"X-Admin-Token": tok} if tok else {}


class OverflowDpiTests(unittest.TestCase):
    """P3：int() 对 inf 抛的是 OverflowError，不是 ValueError。

    复现基线（实测）：
        管理端 + 分享端传 Infinity / -Infinity / 1e400 → HTTP 500
        传 NaN → 422（走 ValueError 分支，正常）
    """

    BAD_VALUES = (float("inf"), float("-inf"), float("nan"), 1e400)
    RAW_JSON = ("Infinity", "-Infinity", "NaN", "1e400")

    def test_helper_handles_overflow(self):
        for v in self.BAD_VALUES:
            with self.assertRaises(HTTPException) as ctx:
                _clean_render_dpi(v)
            self.assertEqual(ctx.exception.status_code, 422, f"{v!r} 应该是 422")

    def test_safe_dpi_falls_back(self):
        for v in self.BAD_VALUES:
            self.assertEqual(_safe_dpi(v), 200, f"{v!r} 应该回落 200")

    def test_int_overflow_really_happens(self):
        """先确认这风险是真的 —— int(inf) 抛 OverflowError 而不是 ValueError。"""
        for v in (float("inf"), float("-inf")):
            with self.assertRaises(OverflowError):
                int(v)
        with self.assertRaises(ValueError):
            int(float("nan"))

    def _post_jobs(self, client, body_files, raw_dpi):
        payload = ('{"files": %s, "target": "pdf", "options": {"render_dpi": %s}}'
                   % (json.dumps(body_files), raw_dpi))
        hdr = {"Content-Type": "application/json"}
        hdr.update(_admin_headers())
        return client.post("/api/jobs", content=payload, headers=hdr)

    def test_admin_api_returns_422_not_500(self):
        c = TestClient(app, raise_server_exceptions=False)
        up = c.post("/api/upload", files={"files": ("a.txt", "正文\n".encode(), "text/plain")},
                    headers=_admin_headers())
        fa = up.json()["files"][0]
        files = [{"file_id": fa["file_id"], "name": fa["name"], "size": fa["size"]}]

        for raw in self.RAW_JSON:
            r = self._post_jobs(c, files, raw)
            self.assertEqual(r.status_code, 422,
                             f"render_dpi={raw} 应该 422，实际 {r.status_code}：{r.text[:80]}")
            self.assertIn("render_dpi", r.text)

    def test_share_api_returns_422_not_500(self):
        c = TestClient(app, raise_server_exceptions=False)
        hdr = _admin_headers()
        c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN}, headers=hdr)
        tok = c.post("/api/shares", json={"ttl_hours": 1},
                     headers=_admin_headers()).json()["token"]
        up = c.post(f"/api/s/upload?t={tok}&sid=ck-ovf",
                    files={"files": ("b.txt", "正文\n".encode(), "text/plain")})
        fa = up.json()["files"][0]
        files = [{"file_id": fa["file_id"], "name": fa["name"], "size": fa["size"]}]

        for raw in self.RAW_JSON:
            payload = ('{"files": %s, "target": "pdf", "options": {"render_dpi": %s}}'
                       % (json.dumps(files), raw))
            r = c.post(f"/api/s/jobs?t={tok}&sid=ck-ovf", content=payload,
                       headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 422,
                             f"分享端 render_dpi={raw} 应该 422，实际 {r.status_code}")


class TotalRenderBudgetTests(unittest.TestCase):
    """P2：单页预算管不住「几百页加起来」。

    _render_pages() 把所有页的 PNG 都塞进 list，直到整本转完才释放。
    单页每张都合规，加起来照样能把 2GB 的机器打死。
    """

    def setUp(self):
        self._saved = to_comic.TOTAL_RENDER_LIMIT

    def tearDown(self):
        to_comic.TOTAL_RENDER_LIMIT = self._saved

    def test_constants_exist_and_sane(self):
        self.assertGreater(to_comic.TOTAL_RENDER_LIMIT, 0)
        self.assertGreater(to_comic.SHARE_TOTAL_RENDER_LIMIT, 0)
        self.assertLess(to_comic.SHARE_TOTAL_RENDER_LIMIT,
                        to_comic.TOTAL_RENDER_LIMIT,
                        "分享访客的整本预算应该比管理端严")

    def test_render_stops_when_total_exceeds(self):
        """让每页返回 1MB，预算设成 3MB —— 第 4 页就该被拦下。"""
        to_comic.TOTAL_RENDER_LIMIT = 3 << 20
        fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024 - 8)
        calls = {"n": 0}

        def fake_render(doc, idx, scale):
            calls["n"] += 1
            return fake_png

        doc = mock.Mock()
        with mock.patch("bookforge.pdf.render.render_page_to_png", fake_render), \
             mock.patch("bookforge.formats.png.image_size", lambda d: (100, 100, 3)):
            with self.assertRaises(to_comic.PDFComicError) as ctx:
                to_comic._render_pages(doc, 10, 200)

        self.assertIn("累计图量", str(ctx.exception))
        self.assertEqual(calls["n"], 4, "应该在第 4 页超限时就停手，不是跑完 10 页")

    def test_fits_under_budget(self):
        to_comic.TOTAL_RENDER_LIMIT = 10 << 20
        fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024 - 8)
        doc = mock.Mock()
        with mock.patch("bookforge.pdf.render.render_page_to_png",
                        lambda d, i, scale: fake_png), \
             mock.patch("bookforge.formats.png.image_size", lambda d: (100, 100, 3)):
            pages = to_comic._render_pages(doc, 5, 200)
        self.assertEqual(len(pages), 5)

    def test_limit_picked_by_task_origin(self):
        """分享任务拿严的，管理端拿松的。"""
        from bookforge.pdf import to_comic as tc
        admin = _render_limit_for("")
        share = _render_limit_for("tok-abc")
        self.assertEqual(share, tc.SHARE_TOTAL_RENDER_LIMIT)
        self.assertGreater(admin, share, "管理端预算不该被分享任务改小")

    def test_share_task_does_not_poison_admin_budget(self):
        """先跑一次分享任务，再取管理端预算 —— 不能拿到被改小的值。"""
        from backend import jobs
        jobs._RENDER_LIMIT_ADMIN = None          # 重置缓存
        admin_before = _render_limit_for("")
        _render_limit_for("some-share-token")
        admin_after = _render_limit_for("")
        self.assertEqual(admin_before, admin_after)


class PublishOrderTests(unittest.TestCase):
    """P1：必须先验证候选镜像，全部通过后才晋升正式标签。

    复现基线：build-push-action 带 push: true 直接推 `${{ver}}` + `latest`，
    然后才拉回来起容器冒烟。冒烟失败时 latest 已经是坏镜像了。
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                                .read_text(encoding="utf-8"))
        cls.steps = cls.wf["jobs"]["build-and-push"]["steps"]
        cls.names = [s.get("name", "") for s in cls.steps]

    def _idx(self, keyword: str) -> int:
        for i, n in enumerate(self.names):
            if keyword in n:
                return i
        self.fail(f"找不到含「{keyword}」的步骤，现有：{self.names}")

    def test_no_immediate_push_to_release_tags(self):
        """build-push-action 只能推 candidate，不能推版本号或 latest。"""
        build = next(s for s in self.steps if "build-push-action" in str(s.get("uses", "")))
        tags = build["with"]["tags"]
        self.assertIn(":candidate", tags, "构建步骤应该只推 candidate")
        self.assertNotIn("steps.meta.outputs.latest", tags,
                         "构建步骤不能直接推 latest —— 那是验证前")
        self.assertNotIn("steps.meta.outputs.ver }", tags.replace("VERSION", ""),
                         "构建步骤不能直接推正式版本号")

    def test_verify_before_promote(self):
        build_i = next(i for i, s in enumerate(self.steps)
                       if "build-push-action" in str(s.get("uses", "")))
        smoke_i = self._idx("端到端冒烟")
        promote_i = self._idx("晋升")
        self.assertLess(build_i, smoke_i, "构建要在冒烟之前")
        self.assertLess(smoke_i, promote_i, "晋升正式标签必须在冒烟通过之后")

    def test_promote_step_pushes_version_and_latest(self):
        promote = self.steps[self._idx("晋升")]
        run = promote["run"]
        self.assertIn('docker push "$IMG:$VER"', run)
        self.assertIn("latest", run)
        self.assertIn("org.opencontainers.image.revision", run,
                      "晋升后要复核 revision，确认推的就是验证过的那个")

    def test_smoke_runs_against_candidate(self):
        """冒烟跑在 candidate 上，不是某个还没验证的正式标签。"""
        container = self.steps[self._idx("起容器")]
        self.assertIn(":candidate", container["run"])

    def test_digest_consistency_note_present(self):
        """晋升用的是 docker tag（本地改指针），不是重新 build。"""
        promote = self.steps[self._idx("晋升")]
        self.assertIn('docker tag "$SRC"', promote["run"])
        self.assertNotIn("build-push-action", json.dumps(promote),
                         "晋升步骤不能重新构建 —— 那就变成验 A 发 B 了")


if __name__ == "__main__":
    unittest.main()
