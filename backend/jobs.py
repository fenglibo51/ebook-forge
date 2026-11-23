"""转换任务队列。

引擎是同步 CPU 密集的，所以：
    · 单线程执行（多线程只会互相抢 GIL，反而更慢）
    · 任务放内存字典，前端轮询进度
    · 每转完一个文件就写历史记录（中途崩了也不丢已完成的）

进度粒度是「文件级」，不是「页级」—— 引擎内部没有进度回调，
按页报进度需要改引擎接口，暂时不做假进度条。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store


@dataclass
class JobItem:
    src: str
    name: str
    size: int = 0
    ok: bool = False
    status: str = "pending"        # pending / running / ok / lossy / fail
    mode: str = ""
    note: str = ""
    error: str = ""
    out: str = ""
    pages: int = 0
    out_size: int = 0
    elapsed: float = 0.0


@dataclass
class Job:
    id: str
    target: str
    options: dict[str, Any]
    items: list[JobItem]
    created: float = field(default_factory=time.time)
    status: str = "pending"        # pending / running / done
    done: int = 0
    started: float = 0.0
    finished: float = 0.0

    @property
    def total(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        elapsed = 0.0
        if self.started:
            elapsed = (self.finished or time.time()) - self.started
        return {
            "id": self.id,
            "status": self.status,
            "target": self.target,
            "total": self.total,
            "done": self.done,
            "elapsed": round(elapsed, 1),
            "items": [
                {
                    "name": it.name, "status": it.status, "mode": it.mode,
                    "note": it.note, "error": it.error, "pages": it.pages,
                    "out": it.out, "out_size": it.out_size,
                    "elapsed": round(it.elapsed, 2),
                }
                for it in self.items
            ],
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convert")


def submit(files: list[dict[str, Any]], target: str,
           options: dict[str, Any]) -> Job:
    """files: [{path, name, size}] —— path 是服务器上的绝对路径"""
    job = Job(
        id=uuid.uuid4().hex[:12],
        target=target,
        options=dict(options),
        items=[JobItem(src=f["path"], name=f["name"], size=f.get("size", 0))
               for f in files],
    )
    with _lock:
        _jobs[job.id] = job
    _executor.submit(_run, job)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _run(job: Job) -> None:
    from bookforge import convert
    from bookforge.formats import mobi as mobi_mod
    from bookforge.pdf import to_comic

    job.status = "running"
    job.started = time.time()

    # 渲染兜底是模块级开关，每次任务开始时同步一次
    to_comic.RENDER_FALLBACK = bool(job.options.get("render_fallback", True))
    to_comic.RENDER_DPI = int(job.options.get("render_dpi", 200))

    for item in job.items:
        item.status = "running"
        t0 = time.time()
        try:
            out_path = _out_path(Path(item.src), job.target, item.name,
                                 str(job.options.get("output_dir", "")))
            r = convert(
                item.src, job.target, out=out_path,
                dpi=float(job.options.get("render_dpi", 200)),
                page_size=str(job.options.get("page_size", "a5")),
                font=str(job.options.get("font_path", "")),
            )
            item.ok = True
            item.mode = r.get("mode", "")
            item.note = r.get("note", "")
            item.pages = r.get("pages", 0)
            item.out = str(r.get("out", ""))
            item.status = "lossy" if item.mode == "render" else "ok"
            try:
                item.out_size = Path(item.out).stat().st_size if item.out else 0
            except OSError:
                item.out_size = 0

            # 删源文件：只在转换成功后做，失败绝不动源文件
            if job.options.get("delete_source"):
                src_p = Path(item.src)
                # 输出和源是同一个文件时绝不删（防手滑把自己删了）
                same = False
                try:
                    same = Path(item.out).resolve() == src_p.resolve()
                except OSError:
                    same = False
                if same:
                    item.note = _join_note(item.note, "输出与源同路径，未删除源文件")
                else:
                    try:
                        src_p.unlink()
                        item.note = _join_note(item.note, "源文件已删除")
                    except OSError as e:
                        item.note = _join_note(item.note, f"删除源文件失败：{e.strerror or e}")
        except Exception as e:                       # 单个文件失败不影响整批
            item.ok = False
            item.status = "fail"
            item.error = str(e) or e.__class__.__name__
            traceback.print_exc()
        item.elapsed = time.time() - t0
        job.done += 1

        store.append_history({
            "status": item.status,
            "src_name": item.name,
            "src_path": item.src,
            "src_size": item.size,
            "target": job.target,
            "out": item.out,
            "out_size": item.out_size,
            "pages": item.pages,
            "mode": item.mode,
            "note": item.note,
            "error": item.error,
            "elapsed": round(item.elapsed, 2),
        })

    job.finished = time.time()
    job.status = "done"


def _join_note(a: str, b: str) -> str:
    return (a + " · " + b) if a else b


def _out_path(src: Path, target: str, name: str, output_dir: str = "") -> Path:
    """算输出路径。

    output_dir 为空 → 和源文件放一起（覆盖前会加序号，绝不覆盖原件）
    output_dir 有值 → 统一收进去，目录不存在就建
    """
    stem = src.stem
    if output_dir.strip():
        folder = Path(output_dir).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"输出目录不可用：{folder}（{e.strerror or e}）") from e
        out = folder / f"{stem}.{target}"
    else:
        out = src.with_name(f"{stem}.{target}")

    n = 1
    while out.exists() and out.resolve() != src.resolve():
        if output_dir.strip():
            out = Path(output_dir).expanduser() / f"{stem}_{n}.{target}"
        else:
            out = src.with_name(f"{stem}_{n}.{target}")
        n += 1
    return out


def gc_old(max_age: float = 3600) -> None:
    """清掉一小时前完成任务的内存记录（历史已经落盘了）。"""
    now = time.time()
    with _lock:
        for jid in [k for k, v in _jobs.items()
                    if v.status == "done" and now - v.finished > max_age]:
            _jobs.pop(jid, None)
