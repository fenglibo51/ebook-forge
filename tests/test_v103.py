"""v1.0.3 复审（基准 b6a6443）的 1 项 P1 + 3 项 P2 回归测试。

每一条都是从真实复现出发的：
  P1    分享上传上限按单链接额度算（520MB），但会话额度只有 128MB
  P2-1  转换中途写盘失败会留下半成品，而 item.out 此刻是空的，清不掉
  P2-2  手动触发填个正式版本号就能覆盖已发布的镜像标签
  P2-3  convert.py 里有个没人用、也没跟着发版的 __version__ = "0.1.0"
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge.convert import convert                       # noqa: E402
from backend import jobs                                    # noqa: E402
from backend.main import BodyLimitMiddleware, _BODY_RULES, _BODY_DEFAULT  # noqa: E402


class ShareUploadLimitTests(unittest.TestCase):
    """P1：分享上传的 body 上限要跟「实际受约束的最小额度」对齐。

    复现基线：_SHARE_UPLOAD_MB = SHARE_QUOTA(512) + 8 = 520，
    而会话额度只有 128 —— 访客传 300MB，body 层放行，解析完才在业务层被拒。
    """

    def test_default_is_min_of_the_three_quotas(self):
        from backend.main import _SHARE_UPLOAD_MB
        # 默认：SHARE 512 / SID 128 / MAX_UPLOAD 8192 → 取 128，再加 8 的开销
        self.assertEqual(_SHARE_UPLOAD_MB, 128 + 8)

    def test_limit_smaller_than_share_quota(self):
        """分享上传上限不能等于单链接额度 —— 会话额度更小才是真正的瓶颈。"""
        mw = BodyLimitMiddleware(lambda *a: None, rules=_BODY_RULES, default=_BODY_DEFAULT)
        share = mw.limit_for("/api/s/upload") / 1048576
        self.assertLess(share, 512, "又按单链接额度算了")
        self.assertGreaterEqual(share, 64, "太小了，正常漫画传不上")

    def test_admin_upload_unaffected(self):
        mw = BodyLimitMiddleware(lambda *a: None, rules=_BODY_RULES, default=_BODY_DEFAULT)
        self.assertGreaterEqual(mw.limit_for("/api/upload") / 1048576, 1024,
                                "管理端的大文件能力不能被分享限制牵连")

    def test_env_override_follows_the_min(self):
        """改小 SID 额度，HTTP 上限要跟着收紧，不能还按 512 算。"""
        env = dict(os.environ)
        env.update({
            "EBOOKFORGE_DATA": tempfile.mkdtemp(prefix="ebf-env-"),
            "EBOOKFORGE_SID_QUOTA_MB": "16",
            "EBOOKFORGE_SHARE_QUOTA_MB": "512",
        })
        r = subprocess.run(
            [sys.executable, "-c", "from backend.main import _SHARE_UPLOAD_MB as x; print(x)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertEqual(int(r.stdout.strip()), 16 + 8)
        shutil.rmtree(env["EBOOKFORGE_DATA"], ignore_errors=True)


class StrayOutputTests(unittest.TestCase):
    """P2-1：转换中途失败不能留下半成品，也不能碰源文件和已有文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf-stray-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cbz(self, path: Path, pages: int = 3) -> Path:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            for i in range(pages):
                zf.writestr(f"{i+1:03d}.png", b"\x89PNG\r\n\x1a\n" + b"x" * 400)
        return path

    @staticmethod
    def _die_on_writestr(n: int):
        orig = zipfile.ZipFile.writestr
        seen = {"k": 0}

        def f(self, name, data, *a, **kw):
            seen["k"] += 1
            if seen["k"] >= n:
                raise OSError(28, "No space left on device")
            return orig(self, name, data, *a, **kw)

        return f

    def test_engine_really_leaves_half_written_file(self):
        """先确认这个风险是真的 —— 不是纸面推断。"""
        src = self._cbz(self.tmp / "a.cbz")
        out = self.tmp / "a.epub"
        with mock.patch.object(zipfile.ZipFile, "writestr", self._die_on_writestr(2)):
            with self.assertRaises(OSError):
                convert(src, "epub", out=out)
        self.assertTrue(out.exists(), "引擎应该留下了半成品（这是我们要清理的东西）")
        self.assertLess(out.stat().st_size, src.stat().st_size * 2)

    def test_remove_stray_deletes_half_written(self):
        src = self._cbz(self.tmp / "b.cbz")
        half = self.tmp / "b.epub"
        half.write_bytes(b"PK\x03\x04 half")
        jobs._remove_stray(half, src)
        self.assertFalse(half.exists())
        self.assertTrue(src.exists(), "源文件不能被牵连")

    def test_remove_stray_never_touches_source(self):
        """同格式转换时目标和源可能指同一个地方 —— 绝不能被删。"""
        same = self._cbz(self.tmp / "same.cbz")
        before = same.read_bytes()
        jobs._remove_stray(same, same)
        self.assertTrue(same.exists())
        self.assertEqual(same.read_bytes(), before)

    def test_out_path_never_picks_an_existing_file(self):
        """调度层靠这个前提才敢删：选出来的路径调用前一定不存在。"""
        src = self._cbz(self.tmp / "c.cbz")
        existing = self.tmp / "c.epub"
        existing.write_bytes(b"already here")
        before = existing.read_bytes()
        picked = jobs._out_path(src, "epub", "c.cbz")
        self.assertNotEqual(picked, existing, "撞上已有文件应该换名避让")
        self.assertEqual(existing.read_bytes(), before, "已有文件内容不能被改")

    def test_three_writer_paths_cleanup(self):
        """TXT→PDF、CBZ→EPUB、CBZ→CBZ 三条代表性写盘路径。"""
        txt = self.tmp / "t.txt"
        txt.write_text("第一章\n\n正文。\n" * 50, encoding="utf-8")
        cbz = self._cbz(self.tmp / "d.cbz", 4)

        cases = [(txt, "pdf", 1), (cbz, "epub", 2), (cbz, "cbz", 2)]
        for src, target, die_at in cases:
            out = jobs._out_path(src, target, src.name)
            self.assertFalse(out.exists(), f"{target}: 调用前不该存在")
            with mock.patch.object(zipfile.ZipFile, "writestr",
                                   self._die_on_writestr(die_at)):
                try:
                    convert(src, target, out=out)
                except Exception:
                    pass
            # 不管引擎留没留，调度层的清理都要让目录回到干净状态
            jobs._remove_stray(out, src)
            self.assertFalse(out.exists(), f"{src.name} → {target}: 还有半成品")
            self.assertTrue(src.exists(), f"{src.name} → {target}: 源文件被牵连")


class ManualTagGuardTests(unittest.TestCase):
    """P2-2：手动触发不许占用正式版本号。

    复现基线：workflow_dispatch 填 1.0.3 会直接推到 ghcr:1.0.3，
    把已经发布的那版镜像盖掉。
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                            .read_text(encoding="utf-8"))
        step = next(s for s in wf["jobs"]["build-and-push"]["steps"]
                    if s.get("id") == "meta")
        cls.script = step["run"]

    def _run(self, ref: str, input_tag: str):
        out_file = tempfile.NamedTemporaryFile("w", suffix=".out", delete=False)
        out_file.close()
        env = dict(os.environ)
        env.update({
            "GITHUB_REF": ref,
            "INPUT_TAG": input_tag,
            "GITHUB_OUTPUT": out_file.name,
            "IMAGE": "ghcr.io/fenglibo51/ebook-forge",
        })
        r = subprocess.run(["bash", "-e", "-c", self.script], env=env,
                           capture_output=True, text=True)
        produced = Path(out_file.name).read_text(encoding="utf-8")
        os.unlink(out_file.name)
        return r, produced

    def _latest(self, produced: str) -> str:
        for line in produced.splitlines():
            if line.startswith("latest="):
                return line.split("=", 1)[1]
        return "<没有 latest 这一行>"

    def test_manual_official_version_rejected(self):
        for tag in ("1.0.3", "v1.0.3", "2.0.0", "V1.2.3"):
            r, _ = self._run("refs/heads/master", tag)
            self.assertNotEqual(r.returncode, 0,
                                f"手动填 {tag} 居然通过了，会覆盖正式镜像标签")
            self.assertIn("正式版本号", r.stdout + r.stderr)

    def test_manual_dev_like_tags_allowed(self):
        for tag in ("dev", "test-abc", "sha-1234567", "pr-42"):
            r, produced = self._run("refs/heads/master", tag)
            self.assertEqual(r.returncode, 0, f"{tag} 应该允许：{r.stderr[-200:]}")
            self.assertEqual(self._latest(produced), "", f"{tag} 不该推 latest")

    def test_git_tag_updates_latest(self):
        r, produced = self._run("refs/tags/v1.0.4", "")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(self._latest(produced), "ghcr.io/fenglibo51/ebook-forge:latest")
        self.assertIn("ver=1.0.4", produced)

    def test_no_input_and_no_tag_is_dev(self):
        r, produced = self._run("refs/heads/master", "")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ver=dev", produced)
        self.assertEqual(self._latest(produced), "")


class VersionConsistencyTests(unittest.TestCase):
    """P2-3：版本号只有一处来源，README 跟得上。"""

    def test_no_stray_version_constant(self):
        text = (ROOT / "bookforge" / "convert.py").read_text(encoding="utf-8")
        # 只允许注释里提到，不允许真的定义
        defs = re.findall(r'^__version__\s*=', text, re.M)
        self.assertEqual(defs, [], "convert.py 里不该再有自己的 __version__")

    def test_package_and_api_agree(self):
        import bookforge
        from backend.main import app
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        pv = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
        self.assertEqual(bookforge.__version__, pv, "包版本和 pyproject 不一致")
        self.assertEqual(app.version, pv, "FastAPI 版本和 pyproject 不一致")

    def test_cli_reports_same_version(self):
        r = subprocess.run([sys.executable, "-m", "bookforge", "--version"],
                           cwd=str(ROOT), capture_output=True, text=True)
        import bookforge
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertIn(bookforge.__version__, r.stdout)

    def test_readme_latest_points_at_current(self):
        import bookforge
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r":latest\s+最新正式版（当前 ([\d.]+)）", readme)
        self.assertIsNotNone(m, "README 里找不到「最新正式版」那一行")
        self.assertEqual(m.group(1), bookforge.__version__)


if __name__ == "__main__":
    unittest.main()
