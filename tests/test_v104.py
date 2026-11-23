"""v1.0.4 复审（基准 e8e5ae2）的 2 项 P1 + 2 项 P2 回归测试。

这一轮的两个 P1 都是「发布流程本身有洞」：
  P1-01  测试依赖没进 requirements-dev.txt，CI 在缺 yaml 时直接红
  P1-02  发布工作流没有 test 门禁 —— CI 红了照样打 tag 发镜像（v1.0.4 就是这么出去的）

两个 P2 是任务状态和输出文件的竞争：
  P2-01  排队中的任务被取消后，线程池轮到时还会跑一遍 _run
  P2-02  输出名靠 exists() 判断，选名到写盘之间有 TOCTOU 窗口
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from backend import jobs                                    # noqa: E402
from bookforge.convert import convert                       # noqa: E402


class DevRequirementsTests(unittest.TestCase):
    """P1-01：测试用到的依赖必须都在 requirements-dev.txt 里。

    真实事故：tests/test_v103.py 里 import yaml，本地 venv 装了 PyYAML
    所以一直是绿的，CI 装的是 requirements*.txt 里那几行，直接
    ModuleNotFoundError，222 个测试里 1 个 error。
    """

    def test_dev_requirements_cover_test_imports(self):
        req = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        self.assertIn("pyyaml", req, "测试要 import yaml，但 requirements-dev.txt 没有 PyYAML")
        self.assertIn("httpx2", req)

    def test_yaml_used_by_tests_is_declared(self):
        """不是硬编码包名 —— 真的去扫 tests/ 里 import 了哪些第三方包。"""
        req = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        third_party = set()
        for py in (ROOT / "tests").glob("*.py"):
            for line in py.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(?:import|from)\s+([a-zA-Z_][\w]*)", line)
                if not m:
                    continue
                mod = m.group(1)
                if mod in ("backend", "bookforge", "tests", "_testenv", "__future__"):
                    continue
                # 仓库里的本地包（tests/fixtures 之类）不需要安装
                if (ROOT / mod).is_dir() or (ROOT / "tests" / mod).is_dir():
                    continue
                third_party.add(mod)
        # 只检查确实要外部安装的那几个，标准库不算
        import sys as _sys
        stdlib = set(_sys.stdlib_module_names)
        external = third_party - stdlib
        for mod in external:
            pkg = {"yaml": "pyyaml", "fastapi": "fastapi", "starlette": "starlette",
                   "httpx": "httpx2", "httpx2": "httpx2", "multipart": "python-multipart"}.get(mod, mod)
            self.assertIn(pkg.lower(), req,
                          f"tests/ 里 import 了 {mod}，但 requirements-dev.txt 没写 {pkg}")

    def test_production_requirements_stay_minimal(self):
        """PyYAML 绝不能混进生产依赖 —— 镜像里没人 import 它。"""
        prod = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("pyyaml", prod)


class PublishGateTests(unittest.TestCase):
    """P1-02：正式发布必须先过单元测试。

    真实事故：e8e5ae2 的 CI 是 failure（缺 yaml），但打 v1.0.4 tag 之后
    Publish image 照样把镜像推上去了。发布脚本自己的冒烟只证明「能启动、
    能转一个文件」，替代不了全量测试。
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml")
                                .read_text(encoding="utf-8"))

    def test_has_test_job(self):
        self.assertIn("test", self.wf["jobs"], "发布工作流里没有测试 job")

    def test_build_depends_on_test(self):
        needs = self.wf["jobs"]["build-and-push"].get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        self.assertIn("test", needs, "build-and-push 没有 needs: test，测试失败也会推镜像")

    def test_test_job_runs_full_suite(self):
        steps = self.wf["jobs"]["test"]["steps"]
        runs = "\n".join(str(s.get("run", "")) for s in steps)
        self.assertIn("unittest discover -s tests", runs,
                      "发布前的 test job 必须跑完整测试，不能只跑某个文件")
        self.assertIn("requirements-dev.txt", runs, "test job 没装测试依赖")

    def test_test_job_mirrors_ci(self):
        """发布门禁和 CI 用同一套依赖和命令，避免两边标准不一致。"""
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        docker_steps = "\n".join(str(s.get("run", ""))
                                 for s in self.wf["jobs"]["test"]["steps"])
        for frag in ("pip install -r requirements.txt -r requirements-dev.txt",
                     "python -m unittest discover -s tests"):
            self.assertIn(frag, ci, f"CI 里没有 {frag}")
            self.assertIn(frag, docker_steps, f"发布门禁里没有 {frag}")


class CancelRaceTests(unittest.TestCase):
    """P2-01：排队中被取消的任务，线程池轮到时不能再跑一遍。

    复现：cancel() 对 pending 任务直接 status=done、done=total。
    但那个 future 撤不回来，_run() 一到就把 status 改回 running，
    然后每个 item 再 done += 1 → done > total，前端进度条超过 100%。
    """

    def _job(self, n: int = 2) -> "jobs.Job":
        items = [jobs.JobItem(src=f"/tmp/nope-{i}.txt", name=f"f{i}.txt", size=1)
                 for i in range(n)]
        return jobs.Job(id="t-cancel", target="pdf", options={}, items=items)

    def test_run_returns_immediately_if_already_done(self):
        job = self._job(2)
        job.status = "done"
        job.done = job.total
        for it in job.items:
            it.status = "canceled"

        jobs._run(job)          # 应该直接 return

        self.assertEqual(job.status, "done", "状态被倒退成 running 了")
        self.assertEqual(job.done, job.total, f"done 被多加了一遍：{job.done}/{job.total}")
        self.assertLessEqual(job.done, job.total)
        for it in job.items:
            self.assertEqual(it.status, "canceled", "已取消的 item 又被改动了")

    def test_cancelled_pending_job_never_converts(self):
        """真并发：占住 worker，取消排队的第二个任务，它一次 convert 都不该跑。"""
        calls = []

        def fake_convert(src, target, **kw):
            calls.append(Path(src).name)
            import time as _t
            _t.sleep(0.35)
            return {"ok": True, "out": str(kw.get("out", "")), "pages": 1,
                    "mode": "text", "note": ""}

        tmp = Path(tempfile.mkdtemp(prefix="ebf-cancel-"))
        a = tmp / "slow.txt"
        a.write_text("第一章\n\n正文。\n" * 200, encoding="utf-8")
        b = tmp / "queued.txt"
        b.write_text("第二章\n\n正文。\n", encoding="utf-8")

        with mock.patch("bookforge.convert", fake_convert), \
             mock.patch.object(jobs, "_executor") as ex:
            # 用一个真的单线程池，保证第二个任务一定排在后面
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=1)
            ex.submit.side_effect = pool.submit

            first = jobs.submit([{"path": str(a), "name": "slow.txt", "size": a.stat().st_size}],
                                "txt", {})
            second = jobs.submit([{"path": str(b), "name": "queued.txt", "size": b.stat().st_size}],
                                 "txt", {})
            jobs.cancel(second.id)

            # 等第一个跑完
            import time as _t
            deadline = _t.time() + 10
            while _t.time() < deadline:
                j1 = jobs.get(first.id)
                if j1 and j1.status == "done":
                    break
                _t.sleep(0.05)
            pool.shutdown(wait=True)
            _t.sleep(0.2)

        j2 = jobs.get(second.id)
        self.assertIsNotNone(j2)
        self.assertEqual(j2.status, "done", "取消后的任务不该再变回 running")
        self.assertEqual(j2.done, j2.total)
        self.assertLessEqual(j2.done, j2.total, f"done 超了：{j2.done}/{j2.total}")
        self.assertNotIn("queued.txt", calls, "被取消的任务居然还调用了 convert")
        self.assertTrue(b.exists(), "取消不该动源文件")

        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_running_job_finishes_current_then_stops(self):
        """运行中的任务取消：当前文件允许跑完，后面的不再启动。"""
        calls = []

        def fake_convert(src, target, **kw):
            calls.append(Path(src).name)
            return {"ok": True, "out": str(kw.get("out", "")), "pages": 1,
                    "mode": "text", "note": ""}

        tmp = Path(tempfile.mkdtemp(prefix="ebf-run-cancel-"))
        files = []
        for i in range(3):
            p = tmp / f"f{i}.txt"
            p.write_text(f"第{i}章\n\n正文。\n", encoding="utf-8")
            files.append({"path": str(p), "name": p.name, "size": p.stat().st_size})

        job = jobs.Job(id="t-run", target="txt", options={},
                       items=[jobs.JobItem(src=f["path"], name=f["name"], size=f["size"])
                              for f in files])
        # 模拟「第一个文件跑完时用户点了取消」
        real_convert = fake_convert
        state = {"n": 0}

        def convert_and_cancel(src, target, **kw):
            state["n"] += 1
            if state["n"] == 1:
                job.cancel_requested = True
            return real_convert(src, target, **kw)

        with mock.patch("bookforge.convert", convert_and_cancel):
            jobs._run(job)

        self.assertEqual(len(calls), 1, f"取消后不该继续启动后面的文件，实际跑了 {calls}")
        self.assertEqual(job.status, "done")
        self.assertEqual(job.done, job.total)
        self.assertEqual(job.items[1].status, "canceled")
        self.assertEqual(job.items[2].status, "canceled")
        for it in job.items:
            self.assertTrue(Path(it.src).exists(), "取消不能删源文件")

        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


class ReserveOutputTests(unittest.TestCase):
    """P2-02：输出名必须原子占位，不能靠 exists() 判断。

    复现思路：exists() 判断完到真正开始写之间有窗口，另一个进程在这段时间
    创建同名文件，之后我们失败清理按 exists() 一删，删掉的就是别人的。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ebf-reserve-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, name: str = "a.cbz") -> Path:
        p = self.tmp / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        return p

    def test_concurrent_reservations_get_distinct_names(self):
        src = self._src("b.cbz")
        got: list[Path] = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            got.append(jobs._reserve_output(src, "epub", "b.cbz"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(got), 10)
        self.assertEqual(len(set(got)), 10, f"抢到重复名字：{sorted(p.name for p in got)}")
        for p in got:
            self.assertTrue(p.exists())
            p.unlink(missing_ok=True)

    def test_placeholder_created_atomically(self):
        src = self._src("c.cbz")
        p = jobs._reserve_output(src, "epub", "c.cbz")
        try:
            self.assertTrue(p.exists())
            self.assertEqual(p.stat().st_size, 0, "占位文件应该是 0 字节")
        finally:
            p.unlink(missing_ok=True)

    def test_custom_output_dir_respected(self):
        src = self._src("d.cbz")
        outdir = self.tmp / "export"
        p = jobs._reserve_output(src, "epub", "d.cbz", str(outdir))
        try:
            self.assertEqual(p.parent, outdir)
            self.assertTrue(outdir.is_dir())
        finally:
            p.unlink(missing_ok=True)

    def test_source_never_reserved_even_same_format(self):
        same = self._src("e.cbz")
        before = same.read_bytes()
        p = jobs._reserve_output(same, "cbz", "e.cbz")
        try:
            self.assertNotEqual(p, same)
            self.assertEqual(same.read_bytes(), before)
        finally:
            p.unlink(missing_ok=True)

    def test_convert_overwrites_placeholder_cleanly(self):
        """占位文件是 0 字节，真实转换要能正常覆盖它。"""
        txt = self.tmp / "t.txt"
        txt.write_text("第一章\n\n正文内容。\n" * 20, encoding="utf-8")
        out = jobs._reserve_output(txt, "pdf", "t.txt")
        self.assertEqual(out.stat().st_size, 0)
        r = convert(txt, "pdf", out=out)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500, "转换没把内容写进去")
        self.assertEqual(out.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
