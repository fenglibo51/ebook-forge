"""v1.0.11 复审（基准 cab89ae）的 2 个 P2 回归测试。

  P2-01  workflow_dispatch 指定 tag 作为 ref 时，仍会推 latest
  P2-02  多个正式发布并发/重跑时，后完成的旧版本会把 latest 压回去

PDF 内存这轮清单已列为「已知接受边界」，按波哥指示不再动。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

WF = ROOT / ".github" / "workflows" / "docker.yml"


class ReleaseTriggerTests(unittest.TestCase):
    """P2-01：latest 只认「push 事件 + 严格 SemVer tag」。

    复现基线（实测 2026-11-24，跑真的「准备 tag」脚本）：
        workflow_dispatch + refs/tags/v1.0.10 → is_release 旧版会输出 latest
        根因：只看 GITHUB_REF 匹配 refs/tags/v*，不看事件类型。
        GitHub 允许 workflow_dispatch 指定 tag 作为 ref，所以这条是真实路径。
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
        cls.wf = wf
        step = next(s for s in wf["jobs"]["build-and-push"]["steps"]
                    if s.get("id") == "meta")
        cls.script = (step["run"]
                      .replace("${{ github.run_id }}", "1234567")
                      .replace("${{ github.run_attempt }}", "1"))

    def _run(self, event: str, ref: str, input_tag: str = ""):
        f = tempfile.NamedTemporaryFile("w", suffix=".out", delete=False)
        f.close()
        env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF=ref,
                   INPUT_TAG=input_tag, GITHUB_OUTPUT=f.name,
                   IMAGE="ghcr.io/fenglibo51/ebook-forge")
        r = subprocess.run(["bash", "-e", "-c", self.script], env=env,
                           capture_output=True, text=True)
        out = Path(f.name).read_text(encoding="utf-8")
        os.unlink(f.name)
        get = lambda k: next((l.split("=", 1)[1] for l in out.splitlines()
                              if l.startswith(k + "=")), "")
        return r, get("ver"), get("latest"), get("is_release")

    def test_dispatch_with_tag_ref_does_not_publish_latest(self):
        """手动跑 + ref 指向 tag（包括旧版 tag）都不能动 latest。"""
        for tag in ("v1.0.10", "v1.0.11", "v0.9.0"):
            r, ver, latest, rel = self._run("workflow_dispatch", f"refs/tags/{tag}")
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertEqual(latest, "",
                             f"workflow_dispatch + {tag} 居然要推 latest")
            self.assertEqual(rel, "0", "is_release 应该是 0")

    def test_push_semver_tag_publishes_latest(self):
        r, ver, latest, rel = self._run("push", "refs/tags/v1.0.12")
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertEqual(ver, "1.0.12")
        self.assertEqual(latest, "ghcr.io/fenglibo51/ebook-forge:latest")
        self.assertEqual(rel, "1")

    def test_non_semver_tags_never_publish(self):
        """vfoo / vlatest / v1.2 这些也以 refs/tags/v 开头，但不能算正式版。"""
        for ref in ("refs/tags/vfoo", "refs/tags/vlatest", "refs/tags/v1.2",
                    "refs/tags/v1.0", "refs/tags/v1.0.12-rc1"):
            r, ver, latest, rel = self._run("push", ref)
            self.assertEqual(latest, "", f"{ref} 不该推 latest")
            self.assertEqual(rel, "0", f"{ref} 的 is_release 应该是 0")

    def test_branch_push_does_not_publish(self):
        r, ver, latest, rel = self._run("push", "refs/heads/master")
        self.assertEqual(ver, "dev")
        self.assertEqual(latest, "")
        self.assertEqual(rel, "0")

    def test_manual_dev_tags_still_allowed(self):
        for tag in ("dev", "test-abc", "sha-123456", "pr-42"):
            r, ver, latest, rel = self._run("workflow_dispatch", "refs/heads/master", tag)
            self.assertEqual(r.returncode, 0, f"{tag} 应该放行：{r.stderr[-200:]}")
            self.assertEqual(ver, tag)
            self.assertEqual(latest, "")

    def test_promote_step_uses_is_release_not_ref_alone(self):
        """晋升步骤必须用 is_release 判断，不能只看 GITHUB_REF。"""
        promote = next(s for s in self.wf["jobs"]["build-and-push"]["steps"]
                       if "晋升" in s.get("name", ""))
        run = promote["run"]
        self.assertIn("is_release", run, "晋升步骤没用 is_release")
        # 反例：不能出现「只判断 GITHUB_REF 是不是 refs/tags/v*」的老写法
        self.assertNotIn('[[ "${GITHUB_REF}" != refs/tags/v* ]]', run,
                         "又回到只看 GITHUB_REF 了 —— workflow_dispatch 能绕过")


class LatestNeverRollsBackTests(unittest.TestCase):
    """P2-02：旧版本重跑/并发完成时，不能把 latest 压回旧版。"""

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
        cls.steps = cls.wf["jobs"]["build-and-push"]["steps"]

    def test_concurrency_configured(self):
        self.assertIn("concurrency", self.wf, "工作流没有 concurrency 控制")
        conc = self.wf["concurrency"]
        self.assertIn("group", conc)
        self.assertFalse(conc.get("cancel-in-progress"),
                         "发布不能被后来的取消，否则候选推了、验证一半被砍")

    def test_promote_compares_versions(self):
        promote = next(s for s in self.steps if "晋升" in s.get("name", ""))
        run = promote["run"]
        self.assertIn("sort -V", run,
                      "晋升步骤没有按版本序比较 —— 后完成的旧版本会把 latest 压回去")
        self.assertIn('LATEST=""', run,
                      "发现本次版本更低时要把 LATEST 清空，不能只打日志")

    def test_version_compare_logic(self):
        """把比较逻辑单独跑一遍：旧版本必须被判为「不该晋升」。"""
        def should_promote(current: str, new: str) -> bool:
            if not current or current == "dev" or current == new:
                return True
            r = subprocess.run(
                ["bash", "-c", f'printf "%s\\n%s\\n" "{current}" "{new}" | sort -V | tail -1'],
                capture_output=True, text=True)
            return r.stdout.strip() == new

        # 升：新版本更高 → 该晋升
        self.assertTrue(should_promote("1.0.11", "1.0.12"))
        self.assertTrue(should_promote("1.0.9", "1.0.10"), "1.0.10 > 1.0.9（别按字典序）")
        # 降：重跑旧版本 → 不该晋升
        self.assertFalse(should_promote("1.0.12", "1.0.11"))
        self.assertFalse(should_promote("1.0.10", "1.0.9"))
        # 相同 / 首次发布 → 该晋升
        self.assertTrue(should_promote("1.0.11", "1.0.11"))
        self.assertTrue(should_promote("", "1.0.12"))
        self.assertTrue(should_promote("dev", "1.0.12"))

    def test_semver_sort_is_not_lexicographic(self):
        """1.0.10 必须排在 1.0.9 之后 —— 用字典序就会判反。"""
        r = subprocess.run(
            ["bash", "-c", 'printf "%s\\n%s\\n" "1.0.9" "1.0.10" | sort -V | tail -1'],
            capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "1.0.10")


class VersionSchemeTests(unittest.TestCase):
    """版本号规则：末位最多到 12，到顶就进位。

    2026-11-24 定的：1.0.12 之后下一个必须是 1.1.0，不存在 1.0.13。
    不是技术限制，是免得版本号越滚越长（1.0.47 这种）。

    这条测试会拦住「顺手把 patch 递增成 13」——CI 直接红，逼着改 minor。
    """

    MAX_PATCH = 12

    def _version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)

    def test_patch_does_not_exceed_twelve(self):
        v = self._version()
        parts = v.split(".")
        self.assertEqual(len(parts), 3, f"版本号应该是 X.Y.Z，实际 {v}")
        patch = int(parts[2])
        self.assertLessEqual(
            patch, self.MAX_PATCH,
            f"版本号 {v} 的末位超过 {self.MAX_PATCH} 了 —— "
            f"该进位成 {parts[0]}.{int(parts[1]) + 1}.0")

    def test_readme_documents_the_rule(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("末位最多到", readme, "README 没写版本号规则")
        self.assertIn("1.1.0", readme, "README 没说明到顶后进位成什么")

    def test_next_version_after_1_0_12_is_1_1_0(self):
        """当前正好在 1.0.12 的话，下一个必须 1.1.0 而不是 1.0.13。"""
        v = self._version()
        if v != "1.0.12":
            self.skipTest(f"当前版本是 {v}，不在边界上")
        parts = [int(x) for x in v.split(".")]
        self.assertEqual(parts[2], self.MAX_PATCH)
        self.assertEqual(f"{parts[0]}.{parts[1] + 1}.0", "1.1.0",
                         "1.0.12 的下一个应该是 1.1.0")


class ReadmeConsistencyTests(unittest.TestCase):
    def test_readme_matches_trigger_rule(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("只能填开发标签", readme)
        # README 里描述 latest 的条件要跟代码一致
        self.assertRegex(readme, r"正式 git tag|正式 tag|v\*",
                         "README 没说明 latest 的触发条件")


if __name__ == "__main__":
    unittest.main()
