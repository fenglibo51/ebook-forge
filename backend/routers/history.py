import json
from pathlib import Path
from fastapi import APIRouter
from typing import Optional

from ..config import HISTORY_DB
from .convert import _tasks, _lock

router = APIRouter(prefix="/api/history", tags=["history"])


def _read_history():
    """从磁盘读取持久化的历史任务"""
    db = Path(HISTORY_DB)
    if db.exists():
        try:
            with open(db) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


@router.get("")
def get_history(status: Optional[str] = None, limit: int = 500):
    """获取转换历史（从磁盘读取，刷新不丢失）"""
    # 从磁盘读取持久化的历史
    disk_data = _read_history()
    
    # 合并内存中的最新状态（运行中的任务）
    with _lock:
        for tid, task in _tasks.items():
            disk_data[tid] = task.model_dump() if hasattr(task, 'model_dump') else task
    
    items = list(disk_data.values())
    
    if status:
        items = [t for t in items if isinstance(t, dict) and t.get("status") == status]
    
    items.sort(key=lambda t: t.get("created_at", "") if isinstance(t, dict) else "", reverse=True)
    
    total = len(items)
    success = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "success")
    failed = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "failed")
    running = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "running")
    
    return {
        "tasks": items[:limit],
        "stats": {"total": total, "success": success, "failed": failed, "running": running},
    }


@router.get("/recent")
def get_recent(limit: int = 10):
    """最近转换记录"""
    disk_data = _read_history()
    with _lock:
        for tid, task in _tasks.items():
            disk_data[tid] = task.model_dump() if hasattr(task, 'model_dump') else task
    items = sorted(disk_data.values(), key=lambda t: t.get("created_at", "") if isinstance(t, dict) else "", reverse=True)[:limit]
    return items


@router.delete("/clear")
def clear_history():
    """清空历史"""
    db = Path(HISTORY_DB)
    if db.exists():
        db.unlink()
    with _lock:
        _tasks.clear()
    return {"success": True}
