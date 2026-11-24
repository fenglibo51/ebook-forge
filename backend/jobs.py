"""转换任务队列。

引擎是同步 CPU 密集的，所以：
    · 单线程执行（多线程只会互相抢 GIL，反而更慢）
    · 任务放内存字典，前端轮询进度
    · 每转完一个文件就写历史记录（中途崩了也不丢已完成的）

进度粒度是「文件级」，不是「页级」—— 引擎内部没有进度回调，
按页报进度需要改引擎接口，暂时不做假进度条。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store, uploads
from bookforge.naming import safe_stem

# ── 产物单文件上限 ────────────────────────────────────────────────────────
# 和缓存配额是两件事：配额管「缓存总共占多少」，这个管「一次转换别写出个
# 巨无霸」。分享访客给得更严 —— 他们的累计配额本来就小。
#
# ⚠️ 这是写完之后才检查的。引擎没有统一的写入出口（每种目标格式各自写盘），
# 要做到「边写边按字节掐断」得给每个 writer 加计数器，改动面太大、风险更高。
# 所以峰值磁盘占用仍可能短暂超过这个值。真要防住，靠容器磁盘配额兜底。
MAX_OUTPUT_MB = int(os.environ.get("EBOOKFORGE_MAX_OUTPUT_MB", "2048"))
SHARE_OUTPUT_MB = int(os.environ.get("EBOOKFORGE_SHARE_OUTPUT_MB", "256"))
_MB = 1024 * 1024


def output_limit_mb(share_token: str = "") -> int:
    return SHARE_OUTPUT_MB if share_token else MAX_OUTPUT_MB


# 渲染 DPI 白名单。API 层已经校验并返回 422，这里是兜底 ——
# jobs 也可能被 CLI / 测试直接调用，不经过 API 那道闸。
# to_comic 模块初始化时的白名单只在 import 时跑一次，挡不住运行时赋值。
RENDER_DPI_CHOICES = (150, 200, 300)


def _safe_dpi(raw: Any) -> int:
    """把任意输入收敛到 150/200/300，非法值一律回落到 200。

    OverflowError 也要接：`int(float('inf'))` 抛的是它，不是 ValueError。
    JSON 里的 Infinity / 1e400 都会变 float('inf')，漏了就冒成 500。
    """
    try:
        v = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 200
    return v if v in RENDER_DPI_CHOICES else 200


# 渲染整本预算：按任务归属给不同值。
#
# ⚠️ 管理端的默认值必须从**环境变量**直接算，不能从 to_comic.TOTAL_RENDER_LIMIT 读 ——
# 那个模块级变量会被 _run() 按任务覆写（跑分享任务时改成 512MB）。
# 早先就是在 `_render_limit_for` 里第一次调用时缓存 it，结果：
#   跑过一次分享任务 → TOTAL_RENDER_LIMIT 变 512MB → 被缓存成「管理端默认值」
#   → 之后所有管理端任务都只有 512MB 预算，而且再也不会恢复。
# CI 抓到的就是这条（536870912 not less than 536870912）。
_RENDER_LIMIT_ADMIN_DEFAULT = int(
    os.environ.get("BOOKFORGE_MAX_TOTAL_RENDER_MB", "2048")) << 20


def _render_limit_for(share_token: str) -> int:
    """按任务归属返回整本渲染的累计上限（字节）。"""
    from bookforge.pdf import to_comic
    return to_comic.SHARE_TOTAL_RENDER_LIMIT if share_token else _RENDER_LIMIT_ADMIN_DEFAULT


class OutputTooLarge(RuntimeError):
    """产物超过单文件上限 —— 预期内的失败，不是 bug。"""


class QueueFull(RuntimeError):
    """排队超额。scope 说明是哪一维度满了，api 层据此给不同提示。"""

    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(detail)
        self.scope = scope        # global / sid / token


def _inflight(jobs: dict[str, "Job"]) -> list["Job"]:
    """在途任务 = 排队中 + 正在跑。done/canceled 不算，否则取消过的任务会永久占额度。"""
    return [j for j in jobs.values() if j.status in ("pending", "running")]


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
           share_token: str = "", session_id: str = "",
           max_total: int = 0, max_sid: int = 0,
           max_token: int = 0) -> Job:
    """files: [{path, name, size}] —— path 是服务器上的绝对路径

    share_token / session_id 非空 = 走分享链接进来的，任务归属记下来，
    之后查进度和下载都要核对，拿到链接的人看不到别人的东西。

    max_total / max_sid / max_token：在途任务上限，0 = 不限。
    **判断和登记在同一把锁里** —— 早先是 api 层先 pending_count() 再 submit()，
    两个并发请求能同时看到「还有空位」，双双挤进来。现在挪到锁内做，
    超了直接抛 QueueFull，由 api 层翻成 429。
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
        running = _inflight(_jobs)
        if max_total and len(running) >= max_total:
            raise QueueFull("global",
                            f"排队任务已满（{len(running)}/{max_total}），等前面的跑完再提交")
        if max_sid and session_id:
            n = sum(1 for j in running if j.session_id == session_id)
            if n >= max_sid:
                raise QueueFull("sid",
                                f"这个会话同时最多 {max_sid} 个任务，等前面的跑完再提交")
        if max_token and share_token:
            n = sum(1 for j in running if j.share_token == share_token)
            if n >= max_token:
                raise QueueFull("token",
                                f"这条链接同时最多 {max_token} 个任务，稍后再试")
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
    # 排队期间可能已经被 cancel() 收尾了 —— 它会把所有 item 标成 canceled、
    # done 直接设成 total、status 设成 done。但线程池里那个 future 撤不回来，
    # 轮到它时照样会执行到这里。早先没有这道检查，结果是：
    #   status 从 done 倒退成 running，然后每个 item 再 done += 1
    #   → done > total，前端进度条超过 100%。
    # 用同一把 _lock 做判断和状态切换，避免和 cancel() 撞在一起。
    with _lock:
        if job.status == "done":
            return
        job.status = "running"
        job.started = time.time()

    from bookforge import convert
    from bookforge.formats import mobi as mobi_mod
    from bookforge.pdf import to_comic

    # 渲染兜底是模块级开关，每次任务开始时同步一次
    to_comic.RENDER_FALLBACK = bool(job.options.get("render_fallback", True))
    to_comic.RENDER_DPI = _safe_dpi(job.options.get("render_dpi", 200))
    # 整本渲染预算：分享访客给更严的限制（公网来的请求不该占掉半台机器）
    to_comic.TOTAL_RENDER_LIMIT = _render_limit_for(job.share_token)

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
            out_path = _reserve_output(Path(item.src), job.target, item.name,
                                       str(job.options.get("output_dir", "")))
            try:
                r = convert(
                    item.src, job.target, out=out_path,
                    dpi=float(_safe_dpi(job.options.get("render_dpi", 200))),
                    page_size=str(job.options.get("page_size", "a5")),
                    font=str(job.options.get("font_path", "")),
                    src_name=item.name,     # 原始文件名，别让随机 file_id 变成书名
                )
            except BaseException:
                # 转换中途失败时引擎可能已经写了一半（见 _remove_stray 的说明）。
                # 那一刻 item.out 还是空字符串，下面的 _drop_output 拿不到路径，
                # 所以必须在这里按 out_path 清一次。
                _remove_stray(out_path, Path(item.src))
                raise
            item.mode = r.get("mode", "")
            item.note = r.get("note", "")
            item.pages = r.get("pages", 0)
            item.out = str(r.get("out", ""))
            item.status = "lossy" if item.mode == "render" else "ok"
            try:
                item.out_size = Path(item.out).stat().st_size if item.out else 0
            except OSError:
                item.out_size = 0
            # 单文件上限：配额之外单独一道，别让一次转换写出个几 GB 的产物
            # 把磁盘和后续下载都拖死。分享访客的额度更小，直接拒。
            limit_mb = output_limit_mb(job.share_token)
            if item.out_size > limit_mb * _MB:
                size_mb = item.out_size / _MB
                _drop_output(item)
                raise OutputTooLarge(
                    f"转换结果 {size_mb:.0f} MB 超过单文件上限 {limit_mb} MB")
            # 结果落在上传缓存目录时，登记进索引：
            # 否则 purge_orphans 会把它当孤儿删掉（不到 24 小时保留期就没了），
            # 而且这部分磁盘占用也算不进配额。
            #
            # 登记失败（含超出缓存配额）必须连带删掉产物并改判失败 ——
            # 之前只 log 一条 warning 就继续报成功，结果是「页面显示成功、
            # 一小时后文件被当孤儿清理、配额里也没这笔账」三头不一致。
            # item.ok 故意放在登记之后设：登记失败就走下面的 except 分支。
            if item.out:
                try:
                    _track_output(Path(item.out), item.out_size, job)
                except Exception as e:
                    _drop_output(item)
                    raise RuntimeError(str(e) or e.__class__.__name__) from e
            item.ok = True

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
            # 配额满、索引写不进去这类是「预期内的失败」，日志一行就够；
            # 甩整段 traceback 只会把真正的 bug 淹掉。
            cause = e.__cause__ if isinstance(e.__cause__, Exception) else None
            expected = isinstance(e, OutputTooLarge) or isinstance(
                cause, (uploads.QuotaError, OSError))
            if expected:
                logging.getLogger(__name__).warning(
                    "转换失败（%s）：%s", item.name, item.error)
            else:
                traceback.print_exc()
        item.elapsed = time.time() - t0
        job.done += 1

        # 历史只是一份存档，写不进去不能把整个任务搞成永远 running。
        # 实测：数据目录只读时 append_history 抛异常，job.status 再没被设成 done，
        # 前端就一直转圈。这里兜住，最坏情况只是这条记录没进历史。
        try:
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
        except Exception as e:
            logging.getLogger(__name__).warning("写历史记录失败：%s", e)

    job.finished = time.time()
    job.status = "done"


def _join_note(a: str, b: str) -> str:
    return (a + " · " + b) if a else b



def _track_output(path: Path, size: int, job: "Job") -> None:
    """转换结果落在上传缓存目录时，把它登记进索引。

    为什么只登记「缓存目录里的」：书库里的转换结果是用户自己的正式产物，
    放在他自己的目录里，不该纳入缓存 TTL 清理，也不该算进缓存配额。
    只有上传缓存目录里的东西是我们代为保管的临时结果。
    """
    try:
        cache_dir = (store.DATA_DIR / "uploads").resolve()
        if path.resolve().parent != cache_dir:
            return
    except OSError:
        return
    # 这里不再吞异常：登记不上（配额满、uploads.json 写不进去）就把异常抛回去，
    # 由调用方丢掉产物并把这个文件判失败。吞掉的后果是「报成功但文件没有归属」。
    uploads.register_output(path, size, token=job.share_token,
                            sid=job.session_id, job_id=job.id)


def _drop_output(item: "JobItem") -> None:
    """登记失败时丢弃本次产物，并把任务项清成「没有产物」。

    为什么必须删：文件留在 uploads/ 里但索引里没有，一小时后
    purge_orphans 会把它当孤儿清掉。与其给用户一个明天就消失的下载链接，
    不如当场说清楚 —— 也免得配额里漏掉这笔占用。
    只动本次产物，源文件和其他任务的结果一律不碰。
    """
    raw = item.out
    item.out = ""
    item.out_size = 0
    item.pages = 0
    if not raw:
        return
    try:
        Path(raw).unlink()
    except OSError:
        pass


def _remove_stray(out: Path, src: Path) -> None:
    """删掉本次转换占住/写了一半的那条路径。

    什么时候会有半成品：引擎是边写边落盘的（CBZ/EPUB 用 zipfile 直接打开
    最终路径），写到第 N 个条目时磁盘满或 I/O 出错，文件就留在那儿了 ——
    实测留下过一个 134 字节的坏 zip，能打开但只有 1 个条目。

    为什么这里敢删：out 是 `_reserve_output()` 用 `O_CREAT|O_EXCL` 原子抢到的
    名字，抢到那一刻它就是本任务的，别人不可能也持有同一个名字。所以失败后
    删它绝不会误伤其他进程写出来的同名文件（早先靠 exists() 判断就有这个风险）。

    绝不碰源文件 —— 同格式转换时两者可能指同一个地方，这里再挡一道。
    """
    try:
        if out.resolve() == src.resolve():
            return
    except OSError:
        pass
    try:
        if out.is_file():
            out.unlink()
    except OSError:
        pass


def _reserve_output(src: Path, target: str, name: str, output_dir: str = "") -> Path:
    """选出输出名并**原子占位**，返回占住的路径。

    output_dir 为空 → 和源文件放一起
    output_dir 有值 → 统一收进去，目录不存在就建
    撞名 → 加 _1、_2 换名
    **任何情况下都不会占源文件本身**

    为什么不能用 `exists()` 判重名：判断「这个名字没人用」和真正开始写之间
    有一段空窗。共享 NAS 目录上另一个进程（第二个容器、用户手动跑 CLI）
    完全可能在这段时间里创建同名文件；等我们失败清理时按 exists() 一删，
    删掉的就是别人的东西。
    `os.open(..., O_CREAT | O_EXCL)` 是原子操作：抢到了这个名字就是我的，
    抢不到抛 FileExistsError 换下一个。

    占位的是个 0 字节文件，转换直接覆盖写它；失败时由调用方 `_remove_stray()`
    删掉 —— 因为它是我们 O_EXCL 抢来的，删它绝不会误伤别人。

    ⚠️ 早先的写法是 `while out.exists() and out.resolve() != src.resolve()`：
    当目标路径和源路径**完全相同时**，循环条件为假直接返回源路径，
    于是同格式转换（txt→txt、pdf→pdf）就把源文件覆盖了。
    实测 txt→txt 不但覆盖，内容还被改写（全角缩进被吃掉，28 字节变 26 字节）。
    """
    folder: Path | None = None
    if output_dir.strip():
        folder = Path(output_dir).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"输出目录不可用：{folder}（{e.strerror or e}）") from e

    # 关键：用用户看到的原始文件名，不用磁盘上的随机存储名。
    # name 是上传时登记的展示名；取不到才回退到路径 stem（书库里的文件本来就是真名）。
    stem = safe_stem(name) or src.stem
    try:
        src_resolved = src.resolve()
    except OSError:
        src_resolved = src

    for n in range(0, 9999):
        nm = f"{stem}.{target}" if n == 0 else f"{stem}_{n}.{target}"
        cand = (folder / nm) if folder else src.with_name(nm)
        try:
            if cand.resolve() == src_resolved:
                continue              # 就是源文件本身 → 绝对不能让出去
        except OSError:
            continue
        try:
            fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue                  # 被别人占了，换下一个名字
        except OSError as e:
            raise RuntimeError(
                f"输出文件创建失败：{cand}（{e.strerror or e}）") from e
        os.close(fd)
        return cand
    raise RuntimeError(f"输出名重名过多：{stem}.{target}")


def gc_old(max_age: float = 3600) -> None:
    """清掉一小时前完成任务的内存记录（历史已经落盘了）。"""
    now = time.time()
    with _lock:
        for jid in [k for k, v in _jobs.items()
                    if v.status == "done" and now - v.finished > max_age]:
            _jobs.pop(jid, None)
