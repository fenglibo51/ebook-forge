"""持久化：历史记录 + 设置。都用 JSON 文件，不引数据库。

两个文件放在 data/ 下（Docker 里挂卷），重启不丢。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("EBOOKFORGE_DATA", Path(__file__).parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

_lock = threading.Lock()


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再原子替换。

    直接 write_text 的话，写到一半断电/进程被杀就留下一个半截 JSON ——
    下次读取解析失败，设置和历史全没了（代码里虽然会退回默认值，但数据确实丢了）。
    Path.replace 在同一文件系统内是原子的。
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())        # 确保数据真的落盘再替换
    tmp.replace(path)

DEFAULT_SETTINGS: dict[str, Any] = {
    # 文字排版
    "page_size": "a5",          # a5 / a4
    "font_size": 12.0,
    "line_height": 1.5,
    "para_indent": 24.0,
    "font_path": "",            # 空 = 自动找 NotoSansSC
    # 漫画渲染
    "render_dpi": 200,          # 150 / 200 / 300
    "render_fallback": True,
    # 书库
    "library_root": "/Book",
    "upload_dir": "temp",
    # 输出位置：留空 = 和源文件放一起；填绝对路径则统一收进去
    "output_dir": "",
    # 分享模式：关闭时后台不需要密钥（纯内网自用）；开启后后台要密钥，
    # 转换页的「生成分享链接」按钮才可用。默认关闭。
    "share_enabled": False,
    "admin_token": "",
}


# ── 设置 ──────────────────────────────────────────────────────────────────

def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    return out


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """只接受已知字段，避免前端塞奇怪的东西进来。"""
    with _lock:
        cur = load_settings()
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                cur[k] = v
        # 类型兜底
        cur["render_dpi"] = int(cur["render_dpi"]) if str(cur["render_dpi"]).isdigit() else 200
        if cur["render_dpi"] not in (150, 200, 300):
            cur["render_dpi"] = 200
        if cur["page_size"] not in ("a5", "a4"):
            cur["page_size"] = "a5"
        cur["render_fallback"] = bool(cur["render_fallback"])
        _atomic_write(SETTINGS_FILE, json.dumps(cur, ensure_ascii=False, indent=2))
        return cur


# ── 历史 ──────────────────────────────────────────────────────────────────

def load_history(limit: int = 500) -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data[-limit:][::-1]          # 新的在前


def append_history(entry: dict[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("id", f"{int(time.time() * 1000)}-{os.urandom(3).hex()}")
    entry.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    with _lock:
        data = []
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except (json.JSONDecodeError, OSError):
                data = []
        data.append(entry)
        # 防无限增长：只留最近 5000 条
        if len(data) > 5000:
            data = data[-5000:]
        _atomic_write(HISTORY_FILE, json.dumps(data, ensure_ascii=False, indent=1))


def clear_history() -> int:
    with _lock:
        n = 0
        if HISTORY_FILE.exists():
            try:
                n = len(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                n = 0
            _atomic_write(HISTORY_FILE, "[]")
        return n


def stats() -> dict[str, int]:
    data = load_history(limit=5000)
    return {
        "total": len(data),
        "ok": sum(1 for x in data if x.get("status") == "ok"),
        "lossy": sum(1 for x in data if x.get("status") == "lossy"),
        "fail": sum(1 for x in data if x.get("status") == "fail"),
    }
