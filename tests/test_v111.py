"""v1.0.10 复审（基准 40366d2）的 1 个 P1 + 2 个 P2 回归测试。

  P1     手动 tag=latest 能绕过「只有 git tag 才更新 latest」
  P2-01  固定 :candidate 标签，并发发布会互相覆盖
  P2-02  PNG 累计字节预算 ≠ 真实内存预算（按用户要求收口，只做准确描述 + 分层默认值）

PDF 内存这块的历史：前后被审了三轮（单页像素 → 整本累计 → 真实 RSS）。
用户明确「搞不定就算了，优化到最佳就行」，所以本文件只断言
「描述准确 + 默认值分层 + 不回归」，不再尝试把实现改造成流式。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from backend.jobs import _page_px_for, _render_limit_for    # noqa: E402
from bookforge.pdf import render as pdf_render             # noqa: E402
from bookforge.pdf import to_comic                         # noqa: E402


class MetaTagWhitelistTests(unittest.TestCase):
    """P1：手动触发必须走白名单。

    复现基线（实测 2026-11-24，跑真的「准备 tag」脚本）：
        tag=latest    → 放行，ver=latest → 晋升步骤 docker tag ... :latest → 覆盖正式 latest
        tag=candidate → 放行，覆盖候选标签
        tag=prod/LATEST → 同样放行（黑名单只拦 SemVer，拦不住这些）
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                            .read_text(encoding="utf-8"))
        step = next(s for s in wf["jobs"]["build-and-push"]["steps"]
                    if s.get("id") == "meta")
        cls.script = (step["run"]
                      .replace("${{ github.run_id }}", "1234567")
                      .replace("${{ github.run_attempt }}", "1"))

    def _run(self, ref: str, input_tag: str):
        f = tempfile.NamedTemporaryFile("w", suffix=".out", delete=False)
        f.close()
        env = dict(os.environ, GITHUB_REF=ref, INPUT_TAG=input_tag,
                   GITHUB_OUTPUT=f.name, IMAGE="ghcr.io/fenglibo51/ebook-forge")
        r = subprocess.run(["bash", "-e", "-c", self.script], env=env,
                           capture_output=True, text=True)
        out = Path(f.name).read_text(encoding="utf-8")
        os.unlink(f.name)
        get = lambda k: next((l.split("=", 1)[1] for l in out.splitlines()
                              if l.startswith(k + "=")), "")
        return r, get("ver"), get("latest"), get("candidate")

    def test_latest_and_candidate_rejected(self):
        for tag in ("latest", "candidate", "LATEST", "Candidate"):
            r, _, _, _ = self._run("refs/heads/master", tag)
            self.assertNotEqual(r.returncode, 0,
                                f"tag={tag} 居然放行了 —— 会覆盖正式/候选标签")

    def test_official_versions_rejected(self):
        for tag in ("1.0.10", "v1.0.10", "V1.2.3", "0.0.1"):
            r, _, _, _ = self._run("refs/heads/master", tag)
            self.assertNotEqual(r.returncode, 0, f"tag={tag} 应该被拒")

    def test_arbitrary_names_rejected(self):
        """白名单之外的一律拒 —— prod、release、随便什么都不能过。"""
        for tag in ("prod", "release", "main", "master", "xxx"):
            r, _, _, _ = self._run("refs/heads/master", tag)
            self.assertNotEqual(r.returncode, 0, f"tag={tag} 在白名单外，应该被拒")

    def test_dev_tags_allowed(self):
        for tag in ("dev", "test-abc", "sha-123456", "pr-42"):
            r, ver, latest, _ = self._run("refs/heads/master", tag)
            self.assertEqual(r.returncode, 0, f"tag={tag} 应该放行：{r.stderr[-200:]}")
            self.assertEqual(ver, tag)
            self.assertEqual(latest, "", f"tag={tag} 不该推 latest")

    def test_git_tag_still_publishes(self):
        r, ver, latest, cand = self._run("refs/tags/v1.0.11", "")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(ver, "1.0.11")
        self.assertEqual(latest, "ghcr.io/fenglibo51/ebook-forge:latest")
        self.assertIn("candidate-", cand)


class CandidatePerRunTests(unittest.TestCase):
    """P2-01：candidate 标签必须跟运行绑定，并发发布不互相覆盖。"""

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                            .read_text(encoding="utf-8"))
        cls.wf = wf
        cls.steps = wf["jobs"]["build-and-push"]["steps"]
        step = next(s for s in cls.steps if s.get("id") == "meta")
        cls.script = step["run"]

    def _candidate_for(self, run_id: str) -> str:
        script = (self.script
                  .replace("${{ github.run_id }}", run_id)
                  .replace("${{ github.run_attempt }}", "1"))
        f = tempfile.NamedTemporaryFile("w", suffix=".out", delete=False)
        f.close()
        env = dict(os.environ, GITHUB_REF="refs/tags/v1.0.11", INPUT_TAG="",
                   GITHUB_OUTPUT=f.name, IMAGE="ghcr.io/fenglibo51/ebook-forge")
        subprocess.run(["bash", "-e", "-c", script], env=env,
                       capture_output=True, text=True)
        out = Path(f.name).read_text(encoding="utf-8")
        os.unlink(f.name)
        return next((l.split("=", 1)[1] for l in out.splitlines()
                     if l.startswith("candidate=")), "")

    def test_different_runs_get_different_candidates(self):
        c1 = self._candidate_for("1111111")
        c2 = self._candidate_for("2222222")
        self.assertNotEqual(c1, c2, "两个 run 拿到同一个候选标签 —— 并发会互相覆盖")
        self.assertIn("candidate-1111111", c1)
        self.assertIn("candidate-2222222", c2)

    def test_no_hardcoded_candidate_tag_in_steps(self):
        """除注释外，步骤里不能再出现写死的 :candidate。"""
        for s in self.steps:
            run = str(s.get("run", "")) + str(s.get("with", {}).get("tags", ""))
            for line in run.splitlines():
                bare = line.split("#")[0]
                self.assertNotIn("IMAGE }}:candidate", bare,
                                 f"步骤「{s.get('name')}」里还有写死的 :candidate")
                self.assertNotIn('IMG="${{ env.IMAGE }}:candidate"', bare)

    def test_promote_step_rechecks_trigger_source(self):
        """晋升那步要再确认一次 GITHUB_REF，不能只靠 tag 名字。"""
        promote = next(s for s in self.steps if "晋升" in s.get("name", ""))
        self.assertIn("refs/tags/v*", promote["run"],
                      "晋升步骤没有二次确认触发来源")


class RenderMemoryHonestyTests(unittest.TestCase):
    """P2-02：预算口径必须诚实，默认值按机器分层。

    用户明确说这块「来来去去好几次」，所以这里只锁三件事：
      ① 注释/README 不能说成「内存上限」
      ② 分享访客的单页和整本预算都要比管理端严
      ③ 实测数据留在代码里，别再凭感觉调
    """

    def test_render_module_documents_real_peak(self):
        src = (ROOT / "bookforge" / "pdf" / "render.py").read_text(encoding="utf-8")
        self.assertIn("2347", src, "实测 200M 像素 ≈ 2347MB 峰值的数据没写进去")
        self.assertIn("不是内存上限", src,
                      "必须写清「像素预算 ≠ 内存上限」，否则下一轮又会被当成硬保证")
        self.assertNotIn("600MB RGB24）", src,
                         "旧注释把 200M 像素说成 600MB，实际峰值是 2.3GB")

    def test_share_limits_stricter_than_admin(self):
        self.assertLess(pdf_render.SHARE_MAX_PIXELS, pdf_render.MAX_PIXELS,
                        "分享访客的单页像素预算应该更严")
        self.assertLess(to_comic.SHARE_TOTAL_RENDER_LIMIT, to_comic.TOTAL_RENDER_LIMIT,
                        "分享访客的整本预算应该更严")

    def test_page_px_picked_by_origin(self):
        admin = _page_px_for("")
        share = _page_px_for("tok-abc")
        self.assertEqual(share, pdf_render.SHARE_MAX_PIXELS)
        self.assertGreater(admin, share)

    def test_share_task_does_not_poison_admin_px_budget(self):
        """跟整本预算一样的坑：不能缓存会被覆写的模块变量。"""
        admin_before = _page_px_for("")
        saved = pdf_render.MAX_PIXELS
        try:
            pdf_render.MAX_PIXELS = _page_px_for("tok-x")   # 模拟 _run 覆写
            admin_after = _page_px_for("")
        finally:
            pdf_render.MAX_PIXELS = saved
        self.assertEqual(admin_before, admin_after, "管理端单页预算被分享任务带歪了")

    def test_long_image_still_supported(self):
        """真实漫画长图（3333×50000 = 166.7M 像素）不能被拒。"""
        self.assertGreaterEqual(pdf_render.MAX_PIXELS, 166_700_000,
                                "默认单页预算低于真实长图，会把正常用例拒掉")
        self.assertGreaterEqual(pdf_render.MAX_SIDE_PX, 50000)

    def test_a4_at_300dpi_well_under(self):
        a4_px = 2479 * 3508          # 实测 A4 @300dpi
        self.assertLess(a4_px, pdf_render.SHARE_MAX_PIXELS / 5,
                        "分享预算连 A4 都容不下就太小了")


if __name__ == "__main__":
    unittest.main()
