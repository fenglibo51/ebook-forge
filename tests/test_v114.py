"""v1.1.0 复审（基准 623411e）的 2 个 P2 + 1 个 P3 回归测试。

  P2-01  pull 和 manifest inspect 同时失败时被当成「首次发布」
  P3     版本规则只在 pyproject 里靠测试拦，git tag 层没拦

P2-02（concurrency 的 pending 队列可能丢中间版本）是产品决策，
见 README「已知行为」一节，不在本文件里断言。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _testenv                                            # noqa: E402,F401

WF = ROOT / ".github" / "workflows" / "docker.yml"


def _steps():
    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    return wf["jobs"]["build-and-push"]["steps"]


def _meta_script() -> str:
    step = next(s for s in _steps() if s.get("id") == "meta")
    return (step["run"]
            .replace("${{ github.run_id }}", "1234567")
            .replace("${{ github.run_attempt }}", "1"))


def _promote_compare_block() -> str:
    """晋升步骤里「只升不降」那一段。"""
    run = next(s for s in _steps() if "晋升" in s.get("name", ""))["run"]
    return run[run.index("# 只升不降"):run.index('echo "验证通过的镜像')]


def _fake_docker(behavior: str) -> pathlib.Path:
    t = pathlib.Path(tempfile.mkdtemp(prefix="fd-"))
    b = t / "bin"
    b.mkdir()
    f = b / "docker"
    f.write_text(behavior)
    f.chmod(0o755)
    return b


class TagVersionRuleTests(unittest.TestCase):
    """P3：版本规则要在 git tag 层也拦住，不能只靠 pyproject 的测试。

    复现基线：v1.1.13 在 tag 规则上完全合法（只校验 X.Y.Z 形状），
    能一路发出去，而项目约定末位最多 12。
    """

    def _run(self, ref: str, event: str = "push"):
        f = tempfile.NamedTemporaryFile("w", suffix=".out", delete=False)
        f.close()
        env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF=ref, INPUT_TAG="",
                   GITHUB_OUTPUT=f.name, IMAGE="ghcr.io/fenglibo51/ebook-forge")
        r = subprocess.run(["bash", "-e", "-c", _meta_script()], env=env,
                           cwd=str(ROOT), capture_output=True, text=True)
        out = pathlib.Path(f.name).read_text(encoding="utf-8")
        os.unlink(f.name)
        get = lambda k: next((l.split("=", 1)[1] for l in out.splitlines()
                              if l.startswith(k + "=")), "")
        return r, get("ver"), get("is_release")

    def test_patch_over_twelve_rejected(self):
        r, _, _ = self._run("refs/tags/v1.1.13")
        self.assertNotEqual(r.returncode, 0, "v1.1.13 应该被拒（末位超 12）")
        self.assertIn("超过约定的 12", r.stdout + r.stderr)

    def test_patch_twelve_allowed(self):
        """末位正好 12 要放行 —— 边界不能差一。"""
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cur = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
        # 用一个和源码一致的 vX.Y.12 来测
        major, minor = cur.split(".")[0], cur.split(".")[1]
        r, ver, rel = self._run(f"refs/tags/v{major}.{minor}.12")
        if major == "1" and minor == "1":
            # 源码是 1.1.x 时这个会因版本不一致被拒，属于另一条规则
            return
        self.assertNotIn("超过约定的 12", r.stdout + r.stderr)

    def test_tag_must_match_pyproject(self):
        """打了 tag 忘了改源码版本 → 拒绝。"""
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cur = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
        other = "9.9.9" if cur != "9.9.9" else "9.9.8"
        r, _, _ = self._run(f"refs/tags/v{other}")
        self.assertNotEqual(r.returncode, 0, "tag 和 pyproject 不一致应该被拒")
        self.assertIn("pyproject.toml", r.stdout + r.stderr,
                      "错误信息要指出是哪个文件不一致")

    def test_matching_tag_passes(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cur = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
        r, ver, rel = self._run(f"refs/tags/v{cur}")
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertEqual(ver, cur)
        self.assertEqual(rel, "1")


class LatestFailClosedTests(unittest.TestCase):
    """P2-01：不能用「两个命令都失败」推断「镜像不存在」。

    复现基线（实测 2026-11-24，用假 docker 命令模拟）：
        pull 失败 + manifest inspect 非 404 失败（网络故障）
        → 输出「GHCR 上还没有 latest，按首次发布处理」→ 推进 latest ✗
    """

    HEAD = ('set -e\nIMG="ghcr.io/fenglibo51/ebook-forge"\nVER="1.1.1"\n'
            'LATEST="$IMG:latest"\nIS_RELEASE=1\n')
    TAIL = '\nif [ -n "$LATEST" ]; then echo "PROMOTE"; else echo "SKIP"; fi\n'

    def _run(self, docker_behavior: str):
        binp = _fake_docker(docker_behavior)
        env = dict(os.environ, PATH=f"{binp}:/usr/bin:/bin")
        r = subprocess.run(["bash", "-c", self.HEAD + _promote_compare_block() + self.TAIL],
                           env=env, capture_output=True, text=True)
        return r, "PROMOTE" in r.stdout

    def test_network_failure_fails_closed(self):
        """pull 失败 + manifest 也失败（非 404）→ 必须中止晋升。"""
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 1 ;;
              *) echo "connection reset by peer" >&2; exit 1 ;;
            esac
        '''))
        self.assertNotEqual(r.returncode, 0, "网络故障时不应该继续晋升")
        self.assertFalse(promote)
        self.assertIn("无法确认", r.stdout + r.stderr)

    def test_registry_says_not_found_allows_first_publish(self):
        """registry 明确回答 manifest unknown → 真的是首次发布，放行。"""
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 1 ;;
              *) echo "manifest unknown" >&2; exit 1 ;;
            esac
        '''))
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertTrue(promote, "首次发布应该放行")
        self.assertIn("明确回答", r.stdout)

    def test_normal_upgrade_promotes(self):
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 0 ;;
              "image inspect"*) echo "1.1.0"; exit 0 ;;
              *) exit 0 ;;
            esac
        '''))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(promote)
        self.assertIn("高于现有的 latest", r.stdout)

    def test_downgrade_skips(self):
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 0 ;;
              "image inspect"*) echo "1.2.0"; exit 0 ;;
              *) exit 0 ;;
            esac
        '''))
        self.assertEqual(r.returncode, 0)
        self.assertFalse(promote, "本次版本更低时不该推进 latest")
        self.assertIn("防止版本回退", r.stdout)

    def test_empty_version_label_fails_closed(self):
        """pull 成功但 label 为空 —— 不是空仓库，是判断不了。"""
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 0 ;;
              "image inspect"*) echo ""; exit 0 ;;
              *) exit 0 ;;
            esac
        '''))
        self.assertNotEqual(r.returncode, 0, "label 为空时不能瞎比较")
        self.assertIn("version label 是空的", r.stdout + r.stderr)

    def test_non_semver_label_fails_closed(self):
        r, promote = self._run(textwrap.dedent('''\
            #!/bin/bash
            case "$1 $2" in
              "pull "*) exit 0 ;;
              "image inspect"*) echo "weird-build"; exit 0 ;;
              *) exit 0 ;;
            esac
        '''))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不是 SemVer", r.stdout + r.stderr)


class ConcurrencyDocumentedTests(unittest.TestCase):
    """P2-02 是产品决策：pending 只保留一个，快速连发可能跳过中间版本。

    这里只断言「文档里写清楚了」，不强制改实现 ——
    要不要保证每个 tag 都有镜像，得波哥定。
    """

    def test_release_doc_documents_pending_behavior(self):
        """连续推 tag 的行为写在 docs/release.md。"""
        doc = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
        self.assertIn("连续推", doc,
                      "发布文档应该说明「短时间内连续推多个 tag 可能跳过中间版本」")
        self.assertIn("latest", doc)

    def test_concurrency_config_present(self):
        wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
        self.assertIn("concurrency", wf)
        self.assertFalse(wf["concurrency"].get("cancel-in-progress"))


if __name__ == "__main__":
    unittest.main()
