"""v1.0.12 复审（基准 09e31ce）的 1 个 P1 + 1 个 P2 回归测试。

  P1  workflow_dispatch + 旧 tag ref + 空输入 → ver 从 ref 派生 → 覆盖历史版本镜像
  P2  docker pull :latest 失败一律当「首次发布」，网络故障时版本倒退保护失效

这一轮的教训特别值得记：P1 是上一轮修复的**残留**。
上轮只挡住了「推 latest」，晋升步骤里那句无条件 `docker push "$IMG:$VER"`
没动 —— 于是 latest 挡住了，正式版本号照推，历史镜像照样被覆盖。
所以本文件的断言重点从「latest 是否为空」改成「这个 ver 会不会推正式标签」。
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
DEV_PREFIXES = ("test-", "sha-", "pr-")


def is_dev_tag(v: str) -> bool:
    """这个版本号是可以安全推的开发标签吗？"""
    return v == "dev" or v.startswith(DEV_PREFIXES)


def is_formal_tag(v: str) -> bool:
    """这个版本号会覆盖正式镜像吗？（SemVer 或保留标签）"""
    if re.fullmatch(r"\d+\.\d+\.\d+", v):
        return True
    return v in ("latest", "candidate") or v.startswith("candidate-")



def _cur_version() -> str:
    """从 pyproject 读当前版本 —— 别在测试里写死，发版会过期。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)

class MetaDerivationTests(unittest.TestCase):
    """P1：版本号只能由「push + 严格 SemVer tag」产生。

    复现基线（实测 2026-11-24，跑真的「准备 tag」脚本）：
        workflow_dispatch + refs/tags/v1.0.11 + 空输入 → ver=1.0.11
        latest 被挡住（is_release=0），但晋升步骤 docker push "$IMG:$VER"
        照样把 :1.0.11 覆盖掉。
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

    # ── 清单给的 4 个验收用例，逐一断言 ver ──

    def test_case1_dispatch_old_tag_empty_input(self):
        """手动跑 + ref=旧 tag + 空输入 → ver 不能是那个 tag。"""
        r, ver, latest, rel = self._run("workflow_dispatch", f"refs/tags/v{_cur_version()}")
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertFalse(is_formal_tag(ver),
                         f"ver={ver!r} 会覆盖正式版本标签 —— 这就是本轮 P1")
        self.assertEqual(ver, "dev", "空输入的手动触发应该当开发构建")
        self.assertEqual(latest, "")
        self.assertEqual(rel, "0")

    def test_case2_dispatch_old_tag_with_dev_tag(self):
        r, ver, latest, rel = self._run("workflow_dispatch", f"refs/tags/v{_cur_version()}", "test-abc")
        self.assertEqual(ver, "test-abc")
        self.assertFalse(is_formal_tag(ver))
        self.assertEqual(latest, "")

    def test_case3_dispatch_with_formal_input_rejected(self):
        r, ver, latest, rel = self._run("workflow_dispatch", "refs/heads/master", "1.0.11")
        self.assertNotEqual(r.returncode, 0, "手动填正式版本号应该被拒")
        self.assertIn("dev", r.stdout + r.stderr, "拒绝信息要告诉用户能用什么")

    def test_case4_push_semver_tag_is_release(self):
        r, ver, latest, rel = self._run("push", f"refs/tags/v{_cur_version()}")
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertEqual(ver, _cur_version())
        self.assertEqual(rel, "1")
        self.assertEqual(latest, "ghcr.io/fenglibo51/ebook-forge:latest")

    # ── 补充：所有手动场景都不该产出正式标签 ──

    def test_no_dispatch_combination_yields_formal_tag(self):
        """把所有「手动触发 × ref」组合扫一遍，ver 一律不能是正式标签。"""
        combos = [
            ("refs/heads/master", ""),
            ("refs/heads/master", "dev"),
            ("refs/heads/master", "test-abc"),
            ("refs/heads/master", "sha-123456"),
            ("refs/heads/master", "pr-42"),
            (f"refs/tags/v{_cur_version()}", ""),          # ← 这是 P1 那条路径
            (f"refs/tags/v{_cur_version()}", ""),
            (f"refs/tags/v{_cur_version()}", ""),
            ("refs/tags/vfoo", ""),
        ]
        for ref, inp in combos:
            r, ver, latest, rel = self._run("workflow_dispatch", ref, inp)
            if r.returncode != 0:
                continue                     # 拒绝也是可接受的
            self.assertFalse(
                is_formal_tag(ver),
                f"workflow_dispatch + ref={ref} + input={inp!r} → ver={ver!r} 会覆盖正式标签")
            self.assertEqual(rel, "0")
            self.assertEqual(latest, "")

    def test_non_semver_tag_push_rejected(self):
        """push 一个非 vX.Y.Z 的 tag（vfoo / vlatest / v1.2）应该直接拒绝。"""
        for ref in ("refs/tags/vfoo", "refs/tags/vlatest", "refs/tags/v1.2", "refs/tags/v1.0"):
            r, ver, latest, rel = self._run("push", ref)
            self.assertNotEqual(r.returncode, 0, f"{ref} 应该被拒绝而不是当开发构建")
            self.assertIn("不是 vX.Y.Z", r.stdout + r.stderr)

    def test_branch_push_is_dev(self):
        r, ver, latest, rel = self._run("push", "refs/heads/master")
        self.assertEqual(ver, "dev")
        self.assertEqual(rel, "0")
        self.assertEqual(latest, "")


class PromoteGuardTests(unittest.TestCase):
    """P1 的第二道闸：晋升步骤不能无条件推 $IMG:$VER。"""

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
        cls.wf = wf
        cls.promote = next(s for s in wf["jobs"]["build-and-push"]["steps"]
                           if "晋升" in s.get("name", ""))
        cls.run_script = cls.promote["run"]

    def test_promote_whitelists_dev_tags_when_not_release(self):
        """非正式发布时，晋升步骤要自己再查一遍 VER 是不是开发标签。"""
        self.assertIn('if [ "$IS_RELEASE" != "1" ]', self.run_script,
                      "晋升步骤没有对非正式发布做判断")
        self.assertRegex(self.run_script, r"dev\|test-\*\|sha-\*\|pr-\*",
                         "晋升步骤没有开发标签白名单")
        self.assertIn("非正式发布却要推", self.run_script,
                      "拒绝时要给出明确原因")

    def test_promote_still_pushes_version_tag(self):
        """正式发布时还是要推版本号标签 —— 别把正常路径一起挡了。"""
        self.assertIn('docker push "$IMG:$VER"', self.run_script)


class PullFailClosedTests(unittest.TestCase):
    """P2：docker pull :latest 失败时不能一律当首次发布。"""

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
        cls.run_script = next(s for s in wf["jobs"]["build-and-push"]["steps"]
                              if "晋升" in s.get("name", ""))["run"]

    def test_uses_manifest_inspect_to_distinguish(self):
        self.assertIn("docker manifest inspect", self.run_script,
                      "没有用 manifest inspect 区分「真的没有」和「拉不下来」")

    def test_fails_closed_when_cannot_confirm(self):
        """无法确认 latest 存不存在时（网络/权限故障）必须拒绝。

        v1.1.0 又收了一轮：不能靠「两个命令都失败」推断「不存在」，
        必须拿到 registry 明确的 manifest unknown 才认作首次发布。
        """
        self.assertIn("无法确认", self.run_script)
        self.assertIn("manifest unknown", self.run_script,
                      "要按 registry 的明确回答判断，不是按退出码")

    def test_first_publish_still_allowed(self):
        self.assertIn("明确回答", self.run_script,
                      "真的首次发布要放行，不能一律拒绝")

    def test_non_semver_current_version_rejected(self):
        """现有 latest 的 version label 不是 SemVer 时，不能瞎比大小。"""
        self.assertIn("不是 SemVer", self.run_script)
        self.assertIn("人工检查", self.run_script)

    def test_compare_logic_semver_aware(self):
        """sort -V 判先后：1.0.10 > 1.0.9，1.1.0 > 1.0.12。"""
        def higher(a, b):
            r = subprocess.run(["bash", "-c",
                                f'printf "%s\\n%s\\n" "{a}" "{b}" | sort -V | tail -1'],
                               capture_output=True, text=True)
            return r.stdout.strip()

        self.assertEqual(higher("1.0.9", "1.0.10"), "1.0.10")
        self.assertEqual(higher("1.0.12", "1.1.0"), "1.1.0")
        self.assertEqual(higher("1.1.12", "1.2.0"), "1.2.0")


class VersionRuleStillHoldsTests(unittest.TestCase):
    """这一轮要发 1.1.0 —— 顺便确认版本号规则没被破坏。"""

    def test_patch_not_over_twelve(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        v = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
        parts = v.split(".")
        self.assertEqual(len(parts), 3)
        self.assertLessEqual(int(parts[2]), 12,
                             f"{v} 的末位超过 12，该进位了")


if __name__ == "__main__":
    unittest.main()
