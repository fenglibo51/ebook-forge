"""分享链接：开一个受限入口，让别人能转换但碰不到你的东西。

不是全站加密码，而是生成一条带 token 的链接。持有链接的人只能：

    上传自己的文件 → 选目标格式 → 看进度 → 下载自己的转换结果

碰不到的：

    书库浏览 / 转换记录 / 设置 / 缓存管理
    服务器上的任何已有文件
    别人的任务和产物（多会话按浏览器会话隔离）
    删除源文件（受限模式下强制关闭）

token 存 data/shares.json，24 小时自动过期，随时可手动撤销。
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

SHARES_FILE = store.DATA_DIR / "shares.json"
DEFAULT_TTL = int(os.environ.get("EBOOKFORGE_SHARE_TTL", str(24 * 3600)))   # 24 小时

_lock = threading.Lock()


def _load() -> dict[str, dict[str, Any]]:
    if not SHARES_FILE.exists():
        return {}
    try:
        data = json.loads(SHARES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    tmp = SHARES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SHARES_FILE)          # 原子替换，别写一半断电留下破文件


def create(ttl: int = DEFAULT_TTL, note: str = "") -> dict[str, Any]:
    """生成一条分享链接。token 是 32 字节随机，猜不出来。"""
    token = secrets.token_urlsafe(24)
    now = time.time()
    entry = {
        "created": int(now),
        "expires": int(now + ttl) if ttl > 0 else 0,   # 0 = 不设期限
        "note": note[:80],
    }
    with _lock:
        data = _load()
        # 顺手清掉过期的，别越攒越多
        data = {k: v for k, v in data.items() if _alive(v)}
        data[token] = entry
        _save(data)
    return {"token": token, "expires": entry["expires"], "note": entry["note"]}


def _alive(entry: dict[str, Any]) -> bool:
    exp = entry.get("expires", 0)
    return exp == 0 or time.time() < exp


def check(token: str) -> dict[str, Any] | None:
    """校验 token。有效返回记录，无效/过期返回 None。"""
    if not token:
        return None
    data = _load()
    entry = data.get(token)
    if not entry or not _alive(entry):
        return None
    return entry


def revoke(token: str) -> bool:
    with _lock:
        data = _load()
        if token in data:
            del data[token]
            _save(data)
            return True
        return False


def list_all() -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for token, e in _load().items():
        if not _alive(e):
            continue
        remaining = (e.get("expires", 0) - now) if e.get("expires") else None
        out.append({
            "token": token,
            "created": e.get("created", 0),
            "expires": e.get("expires", 0),
            "remaining": int(remaining) if remaining is not None else None,
            "note": e.get("note", ""),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)
