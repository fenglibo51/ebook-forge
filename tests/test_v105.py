"""版本号注入 + 前端展示的回归测试。

背景：版本号原来是手写的，`bookforge/__init__.py` 和 `backend/main.py` 各一份，
CI 只把它写进 OCI 标签、不注入代码。后果很具体：

    打 tag v1.0.6 → 镜像 :1.0.6 → 拉下来 /api/health 还是报 1.0.5

而且两处硬编码会各自漂移，改一处忘一处。
现在改成 Dockerfile ARG VERSION → ENV → __version__ 读环境变量，
镜像标签和界面显示永远同一个值。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401


class VersionInjectionTests(unittest.TestCase):
    def test_dockerfile_declares_build_arg(self):
        df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIsNotNone(re.search(r"^ARG VERSION=", df, re.M),
                             "Dockerfile 没有 ARG VERSION")
        self.assertIn("EBOOKFORGE_VERSION=${VERSION}", df,
                      "ARG 没转成 ENV，运行时的 Python 读不到")

    def test_dockerfile_default_is_dev(self):
        """本地 docker build 不传 build-arg 时必须是 dev，不能冒充正式版。"""
        df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        m = re.search(r"^ARG VERSION=(\S+)", df, re.M)
        self.assertEqual(m.group(1), "dev")

    def test_ci_passes_version_build_arg(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                            .read_text(encoding="utf-8"))
        steps = wf["jobs"]["build-and-push"]["steps"]
        build = next(s for s in steps if "build-push-action" in str(s.get("uses", "")))
        args = build["with"].get("build-args", "")
        self.assertIn("VERSION=", args, "CI 构建没传 VERSION，镜像里还是源码兜底值")
        self.assertIn("steps.meta.outputs.ver", args, "应该传 tag 推导出的版本号")

    def test_env_var_overrides_version(self):
        """真跑一个子进程，确认 EBOOKFORGE_VERSION 会覆盖源码兜底值。"""
        env = dict(os.environ)
        env["EBOOKFORGE_VERSION"] = "9.9.9-test"
        r = subprocess.run(
            [sys.executable, "-c", "import bookforge; print(bookforge.__version__)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(r.stdout.strip(), "9.9.9-test")

    def test_no_env_falls_back_to_pyproject(self):
        env = dict(os.environ)
        env.pop("EBOOKFORGE_VERSION", None)
        r = subprocess.run(
            [sys.executable, "-c", "import bookforge; print(bookforge.__version__)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        pv = re.search(r'^version\s*=\s*"([^"]+)"',
                       (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(r.stdout.strip(), pv,
                         "源码兜底值应该跟 pyproject 一致，否则本地跑测试会莫名对不上")

    def test_cli_version_respects_env(self):
        env = dict(os.environ)
        env["EBOOKFORGE_VERSION"] = "2.3.4-cli"
        r = subprocess.run([sys.executable, "-m", "bookforge", "--version"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertIn("2.3.4-cli", r.stdout)

    def test_api_health_reports_injected_version(self):
        """/api/health 报的版本必须跟注入值一致 —— 这是界面上显示的那个。"""
        env = dict(os.environ)
        env["EBOOKFORGE_VERSION"] = "5.6.7-api"
        env["EBOOKFORGE_DATA"] = "/tmp/ebf-ver-test"
        code = (
            "import os\n"
            "from fastapi.testclient import TestClient\n"
            "from backend.main import app\n"
            "r = TestClient(app).get('/api/health')\n"
            "print(r.json()['version'])\n"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(r.stdout.strip().splitlines()[-1], "5.6.7-api")

    def test_main_does_not_hardcode_version(self):
        """main.py 里不能再出现手写的版本字符串。"""
        text = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r'version="\d+\.\d+\.\d+"',
                            "FastAPI 版本又被写死了，应该用 __version__")


class FrontendVersionBadgeTests(unittest.TestCase):
    """侧边栏底部要显示版本号，数据来自 /api/health。"""

    def setUp(self):
        self.html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "frontend" / "css" / "layout.css").read_text(encoding="utf-8")

    def test_badge_element_exists_inside_footer(self):
        foot = re.search(r'<div class="sb-foot">(.*?)</div>\s*</aside>', self.html, re.S)
        self.assertIsNotNone(foot, "找不到侧边栏底部")
        self.assertIn('id="appVersion"', foot.group(1),
                      "版本号不在侧边栏底部 —— 那是用户最容易看到的位置")

    def test_js_fills_badge_from_health(self):
        self.assertIn("#appVersion", self.js, "JS 没有把版本写进去")
        # 必须来自 health 而不是写死
        self.assertRegex(self.js, r"\$\('#appVersion'\)\.textContent\s*=\s*'v'\s*\+\s*h\.version")

    def test_badge_has_style(self):
        self.assertIn(".sb-ver", self.css, "版本号没有样式，会跟引擎状态挤在一起")
        self.assertIn("#sidebar.collapsed .sb-ver", self.css,
                      "侧边栏收起时（72px）也要能显示，得单独处理")

    def test_badge_survives_backend_down(self):
        """后端连不上时不能留个 "vundefined"。"""
        seg = self.js[self.js.index("#appVersion"):]
        self.assertIn("v—", seg[:2000], "后端挂了应该显示占位符，不是 vundefined")


if __name__ == "__main__":
    unittest.main()
