"""v1.0.7 复审（基准 53bd000）的 1 组 P1 + 1 个 P2 + 2 个 P3 回归测试。

  P1-01  render_dpi 服务端不校验 —— 传 100000 直接进任务，渲染时放大 1389 倍
  P1-02  MediaBox 没有像素预算 —— 实测 [0 0 59500 84200] @300dpi = 870 亿像素 / 248 GB
  P2-01  分享队列只有全局限制 —— 一个访客能占满所有人的队
  P3-01  版本号 CSS 跟 prefers-color-scheme 走，但主题是 data-theme
  P3-02  发布冒烟不比对版本号 —— 版本注入没生效也能发出去
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from fastapi.testclient import TestClient                  # noqa: E402

from backend import jobs                                   # noqa: E402
from backend.api import (MAX_PENDING_PER_SID, MAX_PENDING_PER_TOKEN,  # noqa: E402
                         MAX_QUEUE, RENDER_DPI_CHOICES, _clean_render_dpi)
from backend.main import app                               # noqa: E402
from bookforge.pdf import render                           # noqa: E402

TOKEN = "v108-admin-token"
HDR = {"X-Admin-Token": TOKEN}


def _mp(name: str, data: bytes) -> dict:
    return {"files": (name, data, "application/octet-stream")}


class RenderDpiTests(unittest.TestCase):
    """P1-01：DPI 必须在服务端卡死。

    复现基线：分享接口 `int(body.options.get("render_dpi", 200))` 原样接受，
    实测 100000 / -5 / 0 都进了任务，`abc` 抛 ValueError 变成 500。
    """

    def test_whitelist_helper(self):
        for ok in RENDER_DPI_CHOICES:
            self.assertEqual(_clean_render_dpi(ok), ok)
            self.assertEqual(_clean_render_dpi(str(ok)), ok)
        for bad in (0, -5, 100000, 72, 301, "abc", "3.7", None, [], {}):
            with self.assertRaises(Exception) as ctx:
                _clean_render_dpi(bad)
            self.assertIn("render_dpi", str(ctx.exception))

    def test_jobs_layer_clamps_anything(self):
        """jobs 兜底：不管传什么，落到的都是 150/200/300。"""
        for raw, want in ((100000, 200), (-5, 200), (0, 200), ("abc", 200),
                          (None, 200), (300, 300), (150, 150), ("200", 200)):
            self.assertEqual(jobs._safe_dpi(raw), want, f"输入 {raw!r}")

    def test_share_api_rejects_bad_dpi(self):
        c = TestClient(app)
        c.post("/api/share/toggle", json={"enabled": True, "admin_token": TOKEN})
        tok = c.post("/api/shares", json={"ttl_hours": 1}, headers=HDR).json()["token"]
        up = c.post(f"/api/s/upload?t={tok}&sid=ck-dpi",
                    files=_mp("d.txt", "正文\n".encode()))
        fa = up.json()["files"][0]

        for bad in (100000, -5, 0, "abc"):
            r = c.post(f"/api/s/jobs?t={tok}&sid=ck-dpi", json={
                "files": [{"file_id": fa["file_id"], "name": fa["name"],
                           "size": fa["size"]}],
                "target": "pdf", "options": {"render_dpi": bad}})
            self.assertEqual(r.status_code, 422,
                             f"render_dpi={bad!r} 应该被拒，实际 {r.status_code}")
            self.assertIn("render_dpi", r.text)

        # 合法的三个值要放行。每个 sid 各自上传一份 —— file_id 绑定了 sid，
        # 换了 sid 就取不到别人的文件（这本身是 P1-01 那条隔离在起作用）。
        for i, good in enumerate(RENDER_DPI_CHOICES):
            sid = f"ck-dpi-ok-{i}"
            up2 = c.post(f"/api/s/upload?t={tok}&sid={sid}",
                         files=_mp(f"d{i}.txt", "正文\n".encode()))
            f2 = up2.json()["files"][0]
            r = c.post(f"/api/s/jobs?t={tok}&sid={sid}", json={
                "files": [{"file_id": f2["file_id"], "name": f2["name"],
                           "size": f2["size"]}],
                "target": "pdf", "options": {"render_dpi": good}})
            self.assertEqual(r.status_code, 200, f"{good} 应该放行：{r.text[:120]}")

    def test_admin_api_rejects_bad_dpi(self):
        c = TestClient(app)
        auto = c.post("/api/upload", files=_mp("a.txt", "正文\n".encode()), headers=HDR)
        fa = auto.json()["files"][0]
        r = c.post("/api/jobs", headers=HDR, json={
            "files": [{"file_id": fa["file_id"], "name": fa["name"], "size": fa["size"]}],
            "target": "pdf", "options": {"render_dpi": 100000}})
        self.assertEqual(r.status_code, 422, "管理端也不能放行离谱 DPI")


class PixelBudgetTests(unittest.TestCase):
    """P1-02：MediaBox → Bitmap 之间必须有像素预算。

    复现基线（measure 过，不是猜的）：
        A4 @300dpi      2479×3508  =   8.7M px /  25 MB
        漫画长图 @300   3333×50000 = 166.7M px / 477 MB   ← 真实场景
        MediaBox ×100   247916×350833 = 870 亿 px / 248 GB  ← 直接 OOM
    """

    def _render(self, mediabox: bytes, scale: float):
        head = b"<< /Type /Page /MediaBox " + mediabox + b" >>"
        r = render.PageRenderer(doc=mock.Mock(), scale=scale)
        r.doc.page_objects.return_value = [(1, head)]
        r._contents = lambda h: []
        r._resources = lambda h: {}
        with mock.patch.object(render, "Bitmap") as B:
            B.return_value = mock.Mock()
            r.render(0)
            return B.call_args[0]

    def test_normal_pages_allowed(self):
        s = 300 / 72
        self.assertEqual(self._render(b"[0 0 595 842]", s), (2479, 3508))     # A4
        self.assertEqual(self._render(b"[0 0 420 595]", s), (1750, 2479))     # A5
        # 漫画长图必须放行 —— 不能为了安全把真实用例也拒了
        w, h = self._render(b"[0 0 800 12000]", s)
        self.assertEqual((w, h), (3333, 50000))

    def test_giant_mediabox_rejected(self):
        with self.assertRaises(render.RenderError) as ctx:
            self._render(b"[0 0 59500 84200]", 300 / 72)
        self.assertIn("页面太大", str(ctx.exception))

    def test_single_side_too_long_rejected(self):
        with self.assertRaises(render.RenderError):
            self._render(b"[0 0 3000 90000]", 300 / 72)

    def test_total_pixels_rejected(self):
        """单边没超但总面积超 —— 走的是第二条闸。"""
        old_side = render.MAX_SIDE_PX
        render.MAX_SIDE_PX = 100000          # 放宽单边，逼出总像素那条
        try:
            with self.assertRaises(render.RenderError) as ctx:
                self._render(b"[0 0 20000 20000]", 300 / 72)
            self.assertIn("像素过多", str(ctx.exception))
        finally:
            render.MAX_SIDE_PX = old_side

    def test_garbage_mediabox_falls_back_to_default(self):
        """MediaBox 里是非数字时，正则取不满 4 个数，回落到默认 A4 尺寸。

        实测：`[0 0 nan 842]` 的 findall 结果只有 3 个数字 → len < 4 → 走默认值，
        不会拿 NaN 去建位图。这是既有行为，写下来防止以后改成 float() 直解析时退化。
        """
        w, h = self._render(b"[0 0 nan 842]", 300 / 72)
        self.assertEqual((w, h), (2479, 3508), "应该回落成默认 A4，而不是拿 NaN 算")

    def test_scientific_notation_not_exploitable(self):
        """1e300 会被正则拆成 1 和 300，得到的尺寸很小 —— 不会爆内存。"""
        w, h = self._render(b"[0 0 1e300 1e300]", 300 / 72)
        self.assertLess(w * h, 1_000_000, f"科学计数法居然算出 {w}×{h}")

    def test_budget_env_tunable(self):
        self.assertIsInstance(render.MAX_SIDE_PX, int)
        self.assertIsInstance(render.MAX_PIXELS, int)
        self.assertGreater(render.MAX_PIXELS, 166_700_000,
                           "默认预算不该低于真实漫画长图的 167M 像素")


class QueueFairnessTests(unittest.TestCase):
    """P2-01：分享队列要有会话/链接维度的额度，且准入原子。

    复现基线：只有全局 `pending_count() >= MAX_QUEUE`，一个访客能占满所有人的队。
    """

    def setUp(self):
        # 每个测试用干净的 _jobs
        self._saved = dict(jobs._jobs)
        jobs._jobs.clear()

    def tearDown(self):
        jobs._jobs.clear()
        jobs._jobs.update(self._saved)

    def _submit(self, **kw):
        return jobs.submit([{"path": "/tmp/nope.txt", "name": "x.txt", "size": 1}],
                           "pdf", {}, **kw)

    def _block_executor(self):
        """别让 worker 真去跑 —— 任务停在 pending 就好。"""
        return mock.patch.object(jobs, "_executor")

    def test_sid_limit(self):
        with self._block_executor():
            self._submit(session_id="s-1", share_token="t-1", max_sid=2, max_token=99)
            self._submit(session_id="s-1", share_token="t-1", max_sid=2, max_token=99)
            with self.assertRaises(jobs.QueueFull) as ctx:
                self._submit(session_id="s-1", share_token="t-1", max_sid=2, max_token=99)
            self.assertEqual(ctx.exception.scope, "sid")

    def test_other_sessions_unaffected(self):
        with self._block_executor():
            for _ in range(2):
                self._submit(session_id="s-A", share_token="t-1", max_sid=2, max_token=99)
            # s-A 满了，但 s-B 还能用
            self._submit(session_id="s-B", share_token="t-1", max_sid=2, max_token=99)

    def test_token_limit(self):
        with self._block_executor():
            for i in range(3):
                self._submit(session_id=f"s-{i}", share_token="t-1",
                             max_sid=99, max_token=3)
            with self.assertRaises(jobs.QueueFull) as ctx:
                self._submit(session_id="s-x", share_token="t-1",
                             max_sid=99, max_token=3)
            self.assertEqual(ctx.exception.scope, "token")

    def test_global_limit(self):
        with self._block_executor():
            self._submit(session_id="s-1", share_token="t-1", max_total=2,
                         max_sid=99, max_token=99)
            self._submit(session_id="s-2", share_token="t-2", max_total=2,
                         max_sid=99, max_token=99)
            with self.assertRaises(jobs.QueueFull) as ctx:
                self._submit(session_id="s-3", share_token="t-3", max_total=2,
                             max_sid=99, max_token=99)
            self.assertEqual(ctx.exception.scope, "global")

    def test_finished_jobs_release_quota(self):
        with self._block_executor():
            a = self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)
            with self.assertRaises(jobs.QueueFull):
                self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)
            a.status = "done"          # 跑完了应该把额度还回来
            self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)

    def test_cancelled_jobs_release_quota(self):
        with self._block_executor():
            a = self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)
            with self.assertRaises(jobs.QueueFull):
                self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)
            a.status = "done"          # cancel() 对 pending 任务就是把 status 设成 done
            self._submit(session_id="s-1", share_token="t-1", max_sid=1, max_token=9)

    def test_concurrent_submits_cannot_exceed_sid_limit(self):
        """8 个线程同时抢同一个 sid 的唯一名额 —— 只能成功 1 个。

        这就是「先 pending_count 再 submit」那个窗口的回归：
        早先两个请求会同时看到空位，双双挤进来。
        """
        ok, rejected = [], []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                with self._block_executor():
                    ok.append(self._submit(session_id="s-race", share_token="t-race",
                                           max_sid=1, max_token=99))
            except jobs.QueueFull:
                rejected.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ok), 1, f"居然成功了 {len(ok)} 个，锁没生效")
        self.assertEqual(len(rejected), 7)

    def test_api_constants_are_sane(self):
        self.assertLess(MAX_PENDING_PER_SID, MAX_QUEUE,
                        "单会话额度不该等于全局队列")
        self.assertLessEqual(MAX_PENDING_PER_TOKEN, MAX_QUEUE)
        self.assertGreaterEqual(MAX_PENDING_PER_SID, 1, "0 会让分享链接彻底不能用")


class ThemeSelectorTests(unittest.TestCase):
    """P3-01：深色样式必须跟 data-theme，不能跟 prefers-color-scheme。"""

    def setUp(self):
        self.css = (ROOT / "frontend" / "css" / "layout.css").read_text(encoding="utf-8")
        self.js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")

    def test_theme_is_applied_via_data_theme(self):
        """先确认项目的主题机制确实是 data-theme，不是 media query。"""
        # applyTheme 写的是 document.documentElement.dataset.theme
        self.assertIn("dataset.theme", self.js)
        variables = (ROOT / "frontend" / "css" / "variables.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"]', variables,
                      "深色变量应该挂在 data-theme 上")

    def test_version_badge_uses_theme_aware_variable(self):
        """版本号颜色必须用随 data-theme 切换的变量，不能写死也不能用 media query。

        历史：先叠 opacity（深色下发灰），再改成 @media(prefers-color-scheme)
        （手动切主题不生效）。现在直接用 --ink-2 —— 这个变量本身就挂在
        `:root[data-theme="dark"]` 上，自动跟着主题走。
        """
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        m = re.search(r"\.sb-ver\s*\{([^}]*)\}", stripped)
        self.assertIsNotNone(m, "找不到 .sb-ver 的规则")
        body = m.group(1)
        self.assertIn("var(--ink-2)", body,
                      "版本号该用 --ink-2（浅色 5.36:1、深色 7.49:1，都过 AA）")
        self.assertNotIn("opacity", body, "又叠 opacity 了 —— 深色下会发灰")
        # 反例：不能用 media query 单独改这个元素
        bad = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{[^}]*\.sb-ver",
                        stripped)
        self.assertIsNone(bad, "再用 media query 改 .sb-ver 了")

    def test_ink2_variable_is_theme_aware(self):
        """--ink-2 必须在深浅两套里都定义，否则换主题时颜色不会变。"""
        variables = (ROOT / "frontend" / "css" / "variables.css").read_text(encoding="utf-8")
        self.assertRegex(variables, r"--ink-2:\s*#[0-9A-Fa-f]{6}",
                         "浅色主题里没有 --ink-2")
        dark_block = variables[variables.index(':root[data-theme="dark"]'):]
        self.assertRegex(dark_block, r"--ink-2:\s*#[0-9A-Fa-f]{6}",
                         "深色主题里没有 --ink-2，切换时颜色不会变")


class SmokeVersionAssertTests(unittest.TestCase):
    """P3-02：发布冒烟必须硬断言版本号。"""

    def setUp(self):
        self.smoke = (ROOT / ".github" / "smoke_test.py").read_text(encoding="utf-8")

    def test_smoke_accepts_expected_version(self):
        self.assertIn("--expected-version", self.smoke)

    def test_smoke_actually_compares(self):
        self.assertRegex(self.smoke, r'got\s*=\s*str\(health\.get\("version"')
        self.assertIn("镜像版本号不对", self.smoke,
                      "只是打印不算校验，必须有「对不上就 fail」的分支")

    def test_publish_workflow_passes_expected_version(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                            .read_text(encoding="utf-8"))
        runs = "\n".join(str(s.get("run", "")) for s in wf["jobs"]["build-and-push"]["steps"])
        self.assertIn("--expected-version", runs,
                      "发布流程没传期望版本，断言等于没写")
        self.assertIn("steps.meta.outputs.ver", runs,
                      "应该传 tag 推导出的版本号，不是写死的值")

    def test_smoke_version_check_is_testable_offline(self):
        """脚本没有额外依赖，--expected-version 缺省时不校验（CI 的 dev 构建也跑它）。"""
        self.assertIn('default=""', self.smoke)
        self.assertIn("if want_version:", self.smoke)


if __name__ == "__main__":
    unittest.main()
