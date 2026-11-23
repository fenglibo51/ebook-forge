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
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
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
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def clear_history() -> int:
    with _lock:
        n = 0
        if HISTORY_FILE.exists():
            try:
                n = len(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                n = 0
            HISTORY_FILE.write_text("[]", encoding="utf-8")
        return n


def stats() -> dict[str, int]:
    data = load_history(limit=5000)
    return {
        "total": len(data),
        "ok": sum(1 for x in data if x.get("status") == "ok"),
        "lossy": sum(1 for x in data if x.get("status") == "lossy"),
        "fail": sum(1 for x in data if x.get("status") == "fail"),
    }
