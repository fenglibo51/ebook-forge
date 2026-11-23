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
        data[fid] = {
            "path": str(path),
            "name": display_name,
            "size": size,
            # 管理端为 ""，分享访客有值
            "token": token,
            "sid": sid,
            "created": int(time.time()),
        }
        _save(data)
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
        if data.pop(file_id, None) is not None:
            _save(data)


def purge_missing() -> int:
    """清掉记录里指向已不存在文件的条目（缓存清理后调用）。"""
    with _lock:
        data = _load()
        dead = [k for k, v in data.items() if not Path(v.get("path", "")).is_file()]
        for k in dead:
            data.pop(k, None)
        if dead:
            _save(data)
    return len(dead)


def count() -> int:
    return len(_load())
