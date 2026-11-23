"""上传文件的归属记录。

为什么要这个：分享链接的路径检查只保证「文件在公共上传目录里」，
**不保证「是你上传的」**。A、B 用同一条链接时，B 只要知道或猜到 A 的文件名，
就能把它提交成自己的转换任务，然后下载结果。

所以上传时生成一个不可猜的 file_id，把归属记下来：

    file_id → {path, name, size, token, sid, created}

分享任务只认 file_id，服务端核对 token + sid 一致才放行。
存储名也改成随机名，原始文件名只当展示用 —— 顺带解决同名并发覆盖（P1-04）。

记录存 data/uploads.json，和上传目录一起清理。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import store

INDEX_FILE = store.DATA_DIR / "uploads.json"
_lock = threading.Lock()


def _mb_env(name: str, default: int) -> int:
    """读「多少 MB」形式的环境变量，转成字节。"""
    try:
        return int(os.environ.get(name, str(default))) << 20
    except ValueError:
        return default << 20


# ── 配额 ────────────────────────────────────────────────────────────────
# 单次请求的大小限制挡不住「同一条分享链接反复传」：每次都在限额内，
# 几十次之后就把 /data/uploads 塞满了。所以还要有「累计」这一层。
#
# 三级额度，默认值按家用 NAS 的小磁盘估的，都能用环境变量调：
#   全目录总占用    EBOOKFORGE_UPLOAD_QUOTA_MB   默认 2048
#   单条分享链接    EBOOKFORGE_SHARE_QUOTA_MB    默认 512
#   单会话（sid）   EBOOKFORGE_SID_QUOTA_MB      默认 128
QUOTA_TOTAL_BYTES = _mb_env("EBOOKFORGE_UPLOAD_QUOTA_MB", 2048)
QUOTA_TOKEN_BYTES = _mb_env("EBOOKFORGE_SHARE_QUOTA_MB", 512)
QUOTA_SID_BYTES = _mb_env("EBOOKFORGE_SID_QUOTA_MB", 128)

# 正在写、还没登记的字节。不落盘 —— 它只用于并发准入判断，
# 进程重启时这些半截文件会被孤儿清理扫掉。
_inflight = {"total": 0}
_inflight_token: dict[str, int] = {}
_inflight_sid: dict[str, int] = {}

# ── 实时账本 ────────────────────────────────────────────────────────────
# 为什么需要它：admit() 每块数据都要判一次额度。如果每次都去读
# uploads.json（2000 条记录 ≈ 8ms），一个 1GB 文件光判额度就要 8 秒。
#
# 早先的写法更糟 —— Ticket 在 begin() 时把占用拍了张快照，之后一直用旧值：
#   A、B 同时 begin()，都看到 base=0；
#   A 传完 3MB 登记落盘、finish；B 仍拿旧快照，再申请 3MB 照样放行。
#   结果总量 6MB 撞穿 4MB 上限。实测复现过。
#
# 现在：内存里维护一份实时合计，register/forget/drop/purge 时增量增减，
# admit 只读它。begin() 时从 uploads.json 全量校准一次（一次上传 8ms，可接受）。
#
# ⚠️ 前提是单进程（uvicorn 单 worker，当前部署就是这样）。
# 多 worker 时各进程账本会各算各的 —— 那时候得改成共享存储或在 SQLite 里做。
_live: dict[str, Any] = {"total": 0, "tok": {}, "sid": {}, "count": 0, "loaded": False}


def _live_sync() -> None:
    """把实时账本和 uploads.json 对齐。调用方必须已持有 _lock。"""
    u = usage()
    _live["total"] = u["total"]
    _live["tok"] = dict(u["by_token"])
    _live["sid"] = dict(u["by_sid"])
    _live["count"] = u["files"]
    _live["loaded"] = True


def _live_bump(rec: dict[str, Any], sign: int) -> None:
    """加/减一条记录的占用。调用方必须已持有 _lock。"""
    if not _live["loaded"]:
        return                     # 没校准过就别瞎改，下次 sync 会算对
    n = int(rec.get("size", 0) or 0) * sign
    _live["total"] = max(0, _live["total"] + n)
    _live["count"] += sign
    for key, bucket in (("token", "tok"), ("sid", "sid")):
        k = str(rec.get(key, "") or "")
        if not k:
            continue
        v = _live[bucket].get(k, 0) + n
        if v > 0:
            _live[bucket][k] = v
        else:
            _live[bucket].pop(k, None)


class QuotaError(Exception):
    """累计配额已满。调用方转成 413 + 人话提示。"""


def _load() -> dict[str, dict[str, Any]]:
    if not INDEX_FILE.exists():
        return {}
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    tmp = INDEX_FILE.with_name(INDEX_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(INDEX_FILE)


def new_file_id() -> str:
    """不可猜的存储主键。"""
    return secrets.token_urlsafe(16)


def register(path: Path, display_name: str, size: int, *,
             token: str = "", sid: str = "", file_id: str = "") -> str:
    """登记一次上传，返回 file_id。

    管理端上传 token/sid 为空；分享访客上传会记下它属于哪条链接、哪个会话。

    ⚠️ file_id 可以由调用方传入 —— 上传时文件名已经用了它，
    这里再生成一个新的会出现「记录 key ≠ 文件名」，查归属永远查不到。
    """
    fid = file_id or new_file_id()
    with _lock:
        data = _load()
        rec = {
            "path": str(path),
            "name": display_name,
            "size": size,
            # 管理端为 ""，分享访客有值
            "token": token,
            "sid": sid,
            "created": int(time.time()),
            "kind": "upload",
        }
        data[fid] = rec
        _save(data)
        _live_bump(rec, +1)
    return fid


def register_output(path: Path, size: int, *, token: str = "", sid: str = "",
                    job_id: str = "") -> str:
    """把转换结果登记进索引。

    为什么不登记会出事：结果 PDF 就放在 uploads/ 里，而 purge_orphans()
    只认 uploads.json 里有的文件 —— 没登记的超过 1 小时就被当垃圾删掉，
    用户明明还在 24 小时保留期内却下载不到，而且这部分磁盘占用也不计入配额。
    （实测复现过：转换产物 2 小时后再清理就被扫掉了。）

    kind="output" 用来和上传源文件区分：清理规则一样（都按 TTL），
    但排查时能一眼看出这个文件是哪儿来的。
    """
    fid = "out_" + new_file_id()
    with _lock:
        data = _load()
        rec = {
            "path": str(path),
            "name": Path(path).name,
            "size": size,
            "token": token,
            "sid": sid,
            "job": job_id,
            "created": int(time.time()),
            "kind": "output",
        }
        data[fid] = rec
        _save(data)
        _live_bump(rec, +1)
    return fid


def get(file_id: str) -> dict[str, Any] | None:
    return _load().get(file_id or "")


def owns(file_id: str, token: str, sid: str) -> dict[str, Any] | None:
    """核对归属：这个 file_id 是不是这条链接、这个会话上传的。

    管理端调用时传 token=""/sid=""，只匹配管理端自己的上传。
    """
    rec = _load().get(file_id or "")
    if not rec:
        return None
    if rec.get("token", "") != token or rec.get("sid", "") != sid:
        return None
    return rec


def forget(file_id: str) -> None:
    with _lock:
        data = _load()
        rec = data.pop(file_id, None)
        if rec is not None:
            _save(data)
            _live_bump(rec, -1)


def drop_token(token: str) -> int:
    """删掉某条分享链接名下的所有上传副本 + 索引记录，返回清理条数。

    为什么必须做：链接被撤销/关闭分享以后，那些文件再也取不出来了
    （归属校验认的是 token），可索引还留着、配额还照算 ——
    用户明明「什么都没上传」，额度却显示快满。撤销这一刻就该收干净。
    """
    if not token:
        return 0
    with _lock:
        data = _load()
        doomed = [k for k, v in data.items() if v.get("token") == token]
        for k in doomed:
            try:
                Path(data[k].get("path", "")).unlink()
            except OSError:
                pass
            _live_bump(data[k], -1)
            data.pop(k, None)
        if doomed:
            _save(data)
    return len(doomed)


def drop_sid(token: str, sid: str) -> int:
    """删掉某条链接下某个会话的上传副本（会话结束后可选调用）。"""
    if not token or not sid:
        return 0
    with _lock:
        data = _load()
        doomed = [k for k, v in data.items()
                  if v.get("token") == token and v.get("sid") == sid]
        for k in doomed:
            try:
                Path(data[k].get("path", "")).unlink()
            except OSError:
                pass
            _live_bump(data[k], -1)
            data.pop(k, None)
        if doomed:
            _save(data)
    return len(doomed)


def purge_missing() -> int:
    """清掉记录里指向已不存在文件的条目（缓存清理后调用）。"""
    with _lock:
        data = _load()
        dead = [k for k, v in data.items() if not Path(v.get("path", "")).is_file()]
        for k in dead:
            _live_bump(data[k], -1)
            data.pop(k, None)
        if dead:
            _save(data)
    return len(dead)


def count() -> int:
    return len(_load())

def usage() -> dict[str, Any]:
    """按 uploads.json 里的登记记录算当前占用。

    不用扫目录 —— 目录扫描在文件多时很慢，而且磁盘上的半截文件不该计入配额。
    孤儿文件由 reconcile() 定期清掉，保证索引和磁盘不会越走越偏。
    """
    data = _load()
    by_token: dict[str, int] = {}
    by_sid: dict[str, int] = {}
    total = 0
    for rec in data.values():
        n = int(rec.get("size", 0) or 0)
        total += n
        t = str(rec.get("token", "") or "")
        if t:
            by_token[t] = by_token.get(t, 0) + n
        sd = str(rec.get("sid", "") or "")
        if sd:
            by_sid[sd] = by_sid.get(sd, 0) + n
    return {"total": total, "by_token": by_token, "by_sid": by_sid, "files": len(data)}


def _fmt(n: int) -> str:
    return f"{n / 2**20:.0f} MB" if n < 2**30 else f"{n / 2**30:.1f} GB"


class Ticket:
    """一次上传的配额凭据。

    用法：begin() 拿票 → 每写一块 admit(n) → 结束 finish()。
    admit 在锁里比较「已登记 + 别处正在写 + 本次已写 + 新增」，
    所以两个并发上传不可能一起越过上限（不需要事后补偿）。
    """

    __slots__ = ("token", "sid", "written", "closed")

    def __init__(self, token: str, sid: str):
        self.token = token
        self.sid = sid
        self.written = 0
        self.closed = False
        with _lock:
            # 每次开始上传都从磁盘校准一次账本 —— 万一索引被外部改过
            # （手动编辑、别的进程），这里能纠回来。一次上传只花一次。
            _live_sync()

    def admit(self, nbytes: int) -> None:
        """再写 nbytes 允许吗？不允许抛 QuotaError。"""
        if self.closed or nbytes <= 0:
            return
        with _lock:
            # 全局「已登记 + 别处正在写 + 本次已写」—— 每次都用最新值，
            # 不依赖 Ticket 创建时的快照（快照会过期，实测能撞穿上限）
            others = _inflight["total"] - self.written
            used_total = _live["total"] + others + self.written
            if used_total + nbytes > QUOTA_TOTAL_BYTES:
                raise QuotaError(
                    f"上传目录已满：当前用 {_fmt(used_total)}，"
                    f"上限 {_fmt(QUOTA_TOTAL_BYTES)}。"
                    "旧文件会在 24 小时后自动清理，也可以在设置页手动清")
            if self.token:
                other_t = _inflight_token.get(self.token, 0) - self.written
                used = _live["tok"].get(self.token, 0) + other_t + self.written
                if used + nbytes > QUOTA_TOKEN_BYTES:
                    raise QuotaError(
                        f"这条分享链接的额度用完了：已用 {_fmt(used)}，"
                        f"上限 {_fmt(QUOTA_TOKEN_BYTES)}")
            if self.sid:
                other_s = _inflight_sid.get(self.sid, 0) - self.written
                used = _live["sid"].get(self.sid, 0) + other_s + self.written
                if used + nbytes > QUOTA_SID_BYTES:
                    raise QuotaError(
                        f"当前会话的上传额度用完了：已用 {_fmt(used)}，"
                        f"上限 {_fmt(QUOTA_SID_BYTES)}")
            # 记账：本次已写 + 全局正在写
            self.written += nbytes
            _inflight["total"] += nbytes
            if self.token:
                _inflight_token[self.token] = _inflight_token.get(self.token, 0) + nbytes
            if self.sid:
                _inflight_sid[self.sid] = _inflight_sid.get(self.sid, 0) + nbytes

    def finish(self) -> None:
        """成功或失败都要调 —— 把「正在写」的记账还回去。

        成功时文件已进 uploads.json（size 计入正式占用），
        失败时文件被删掉，两种情况下 inflight 都必须归零，否则配额会永久泄漏。
        """
        if self.closed:
            return
        with _lock:
            _inflight["total"] = max(0, _inflight["total"] - self.written)
            if self.token:
                v = _inflight_token.get(self.token, 0) - self.written
                if v > 0:
                    _inflight_token[self.token] = v
                else:
                    _inflight_token.pop(self.token, None)
            if self.sid:
                v = _inflight_sid.get(self.sid, 0) - self.written
                if v > 0:
                    _inflight_sid[self.sid] = v
                else:
                    _inflight_sid.pop(self.sid, None)
            self.closed = True


def begin(token: str = "", sid: str = "") -> Ticket:
    return Ticket(token, sid)


def purge_orphans(upload_dir: Path, *, min_age: float = 3600.0) -> list[str]:
    """删掉目录里「索引查不到」的文件，返回删掉的名单。

    为什么会有孤儿：上传写到一半进程被杀、写完但 register 抛异常、
    索引文件被外部编辑过。这些文件永远不会到期清理（清理按索引走），
    会一直占着磁盘。

    ⚠️ min_age：刚创建的文件先别动 —— 上传时是「先 open("xb") 建文件、
    写完之后才 register」，中间那段时间它在索引里查不到，删了就是把
    正在传的文件干掉。默认只清 1 小时以上的。
    """
    import time
    now = time.time()
    known = {Path(v.get("path", "")).name for v in _load().values() if v.get("path")}
    removed: list[str] = []
    try:
        entries = list(upload_dir.iterdir())
    except OSError:
        return removed
    for f in entries:
        if not f.is_file() or f.name in known or f.name.endswith(".tmp"):
            continue
        try:
            if now - f.stat().st_mtime < min_age:
                continue
            f.unlink()
            removed.append(f.name)
        except OSError:
            pass
    return removed


def stats() -> dict[str, Any]:
    u = usage()
    return {
        "used_bytes": u["total"],
        "used_human": _fmt(u["total"]),
        "limit_bytes": QUOTA_TOTAL_BYTES,
        "limit_human": _fmt(QUOTA_TOTAL_BYTES),
        "files": u["files"],
        "percent": round(u["total"] * 100 / QUOTA_TOTAL_BYTES, 1) if QUOTA_TOTAL_BYTES else 0.0,
        "token_quota_human": _fmt(QUOTA_TOKEN_BYTES),
        "sid_quota_human": _fmt(QUOTA_SID_BYTES),
    }
