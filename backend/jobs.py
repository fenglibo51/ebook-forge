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
    # 分享链接进来的任务记归属，防止拿到链接的人查/下别人的产物
    share_token: str = ""
    session_id: str = ""
    # 取消标记：置位后「当前文件跑完就不再启动后面的」
    cancel_requested: bool = False
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
            "canceled": self.cancel_requested,
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
           options: dict[str, Any], *,
           share_token: str = "", session_id: str = "") -> Job:
    """files: [{path, name, size}] —— path 是服务器上的绝对路径

    share_token / session_id 非空 = 走分享链接进来的，任务归属记下来，
    之后查进度和下载都要核对，拿到链接的人看不到别人的东西。
    """
    job = Job(
        id=uuid.uuid4().hex[:12],
        target=target,
        options=dict(options),
        items=[JobItem(src=f["path"], name=f["name"], size=f.get("size", 0))
               for f in files],
        share_token=share_token,
        session_id=session_id,
    )
    with _lock:
        _jobs[job.id] = job
    _executor.submit(_run, job)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def cancel(job_id: str) -> dict[str, Any]:
    """请求取消。

    排队中的任务立刻结束（一个字节都不会写盘）；
    正在跑的等当前文件转完就停。
    """
    job = _jobs.get(job_id)
    if not job:
        return {"ok": False, "reason": "任务不存在"}
    if job.status == "done":
        return {"ok": False, "reason": "任务已经结束了"}

    with _lock:
        job.cancel_requested = True
        if job.status == "pending":
            # 还没轮到它，直接标记完成，跳过所有文件
            for it in job.items:
                it.status = "canceled"
                it.error = "任务已取消"
            job.done = job.total
            job.status = "done"
            job.finished = time.time()
    return {"ok": True, "status": job.status, "done": job.done, "total": job.total}


def pending_count() -> int:
    """排队 + 正在跑的任务数，用来限制队列长度。"""
    with _lock:
        return sum(1 for j in _jobs.values() if j.status in ("pending", "running"))


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
        # 取消只跳过「还没开始」的文件。
        # 不打断正在跑的那个 —— convert() 是同步一次性调用，强杀会留下半截文件，
        # 比不取消更糟。所以当前文件跑完，后面的不再启动。
        if job.cancel_requested:
            item.status = "canceled"
            item.error = "任务已取消"
            job.done += 1
            continue
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
                # ① 先确认输出真的存在且有内容 —— 空文件/没写出来时删源等于丢数据
                out_p = Path(item.out) if item.out else None
                out_ok = False
                if out_p is not None:
                    try:
                        out_ok = out_p.exists() and out_p.stat().st_size > 0
                    except OSError:
                        out_ok = False

                # ② 上传的副本不删 —— 那是给用户下载用的临时文件，
                #    删了源目录里的原件也还在设备上，纯属自找麻烦
                from .api import UPLOAD_DIR
                is_upload = False
                try:
                    src_p.resolve().relative_to(UPLOAD_DIR.resolve())
                    is_upload = True
                except (ValueError, OSError):
                    is_upload = False

                # ③ 输出和源是同一个文件时绝不删（防手滑把自己删了）
                same = False
                try:
                    same = out_p is not None and out_p.resolve() == src_p.resolve()
                except OSError:
                    same = False

                if not out_ok:
                    item.note = _join_note(item.note, "输出文件不存在或为空，未删除源文件")
                elif is_upload:
                    pass                      # 副本，不碰
                elif same:
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
    """算输出路径。**保证结果永远不会是源文件本身。**

    output_dir 为空 → 和源文件放一起
    output_dir 有值 → 统一收进去，目录不存在就建
    任何情况下撞上源文件 → 加 _1、_2 换名

    ⚠️ 早先的写法是 `while out.exists() and out.resolve() != src.resolve()`：
    当目标路径和源路径**完全相同时**，循环条件为假直接返回源路径，
    于是同格式转换（txt→txt、pdf→pdf）就把源文件覆盖了。
    实测 txt→txt 不但覆盖，内容还被改写（全角缩进被吃掉，28 字节变 26 字节）。
    现在改成：只要候选路径等于源文件，就算"已占用"，照样换名。
    """
    folder: Path | None = None
    if output_dir.strip():
        folder = Path(output_dir).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"输出目录不可用：{folder}（{e.strerror or e}）") from e

    stem = src.stem
    try:
        src_resolved = src.resolve()
    except OSError:
        src_resolved = src

    def candidate(n: int) -> Path:
        nm = f"{stem}.{target}" if n == 0 else f"{stem}_{n}.{target}"
        return (folder / nm) if folder else src.with_name(nm)

    def occupied(p: Path) -> bool:
        if not p.exists():
            return False
        try:
            if p.resolve() == src_resolved:
                return True            # 就是源文件本身 → 绝对不能让出去
        except OSError:
            return True
        return True

    n = 0
    while occupied(candidate(n)):
        n += 1
        if n > 9999:
            raise RuntimeError(f"输出名重名过多：{stem}.{target}")
    return candidate(n)


def gc_old(max_age: float = 3600) -> None:
    """清掉一小时前完成任务的内存记录（历史已经落盘了）。"""
    now = time.time()
    with _lock:
        for jid in [k for k, v in _jobs.items()
                    if v.status == "done" and now - v.finished > max_age]:
            _jobs.pop(jid, None)
