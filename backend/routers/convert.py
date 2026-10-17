import json
import os
import uuid
import threading
from datetime import datetime
from pathlib import Path
import shutil
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..models.schemas import ConvertRequest, BatchConvertRequest, ConvertTask
from ..services.converter import convert_file, get_supported_targets, check_calibre_available
from ..services.file_manager import scan_file
from ..config import SUPPORTED_FORMATS, BOOK_PATH, HISTORY_DB, TEMP_DIR

router = APIRouter(prefix="/api/convert", tags=["convert"])

# 内存中的任务状态（持久化到 HISTORY_DB）
_tasks: dict[str, ConvertTask] = {}
_lock = threading.Lock()


def _load_history():
    """加载历史记录"""
    db = Path(HISTORY_DB)
    if db.exists():
        try:
            with open(db) as f:
                data = json.load(f)
                with _lock:
                    for k, v in data.items():
                        if isinstance(v, dict):
                            _tasks[k] = ConvertTask(**v)
                        else:
                            _tasks[k] = v
        except Exception:
            pass


def _save_history():
    """保存历史记录到磁盘（最多保留 500 条）"""
    db = Path(HISTORY_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _lock:
            data = {k: v.model_dump() if hasattr(v, 'model_dump') else v for k, v in _tasks.items()}
        # 按时间排序，只保留最近 500 条
        sorted_items = sorted(data.items(), key=lambda x: x[1].get("created_at", "") if isinstance(x[1], dict) else "", reverse=True)
        pruned = dict(sorted_items[:500])
        with open(db, "w") as f:
            json.dump(pruned, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


_load_history()


@router.get("/status")
def get_status():
    """检查服务状态"""
    calibre_ok = check_calibre_available()
    return {
        "calibre_available": calibre_ok,
        "formats": SUPPORTED_FORMATS,
        "book_path": BOOK_PATH,
    }


@router.post("/start")
def start_convert(req: ConvertRequest):
    """开始转换单个文件"""
    # 验证文件存在
    finfo = scan_file(req.source_path)
    if not finfo:
        raise HTTPException(400, f"文件不存在: {req.source_path}")
    
    task_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    
    task = ConvertTask(
        task_id=task_id,
        source_path=req.source_path,
        target_format=req.target_format,
        status="running",
        progress=0,
        created_at=now,
    )
    
    with _lock:
        _tasks[task_id] = task
    _save_history()  # 立即持久化，防止刷新丢失

    # 后台线程执行转换
    def _run():
        src = req.source_path
        if not src.startswith(TEMP_DIR):
            src = str(Path(BOOK_PATH) / src)
        result = convert_file(
            source_path=src,
            target_format=req.target_format,
            naming_rule=req.naming_rule or "{title}.{author}.{ext}",
            keep_source=req.keep_source,
            use_original_name=req.use_original_name,
            target_dir=req.target_dir,
        )
        with _lock:
            if task_id in _tasks:
                if result["success"]:
                    _tasks[task_id].status = "success"
                    _tasks[task_id].progress = 100
                    _tasks[task_id].output_path = result.get("output_path", "")
                else:
                    _tasks[task_id].status = "failed"
                    _tasks[task_id].error = result.get("error", "未知错误")
                _tasks[task_id].completed_at = datetime.now().isoformat()
        _save_history()
    
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    
    return {"task_id": task_id}


@router.post("/batch")
def batch_convert(req: BatchConvertRequest):
    """批量转换"""
    task_ids = []
    for item in req.files:
        finfo = scan_file(item.source_path)
        if not finfo:
            continue
        
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        task = ConvertTask(
            task_id=task_id,
            source_path=item.source_path,
            target_format=item.target_format,
            status="pending",
            progress=0,
            created_at=now,
        )
        
        with _lock:
            _tasks[task_id] = task
        _save_history()

        task_ids.append(task_id)
    
    # 后台批量执行
    def _run_batch():
        for tid in task_ids:
            with _lock:
                if tid in _tasks:
                    _tasks[tid].status = "running"
            item = next((f for f in req.files if True), None)
            matching = [f for f in req.files if True]
            actual_req = None
            for i, tid2 in enumerate(task_ids):
                if tid2 == tid and i < len(req.files):
                    actual_req = req.files[i]
                    break
            
            if actual_req:
                batch_src = actual_req.source_path
                if not batch_src.startswith(TEMP_DIR):
                    batch_src = str(Path(BOOK_PATH) / batch_src)
                result = convert_file(
                    source_path=batch_src,
                    target_format=actual_req.target_format,
                    naming_rule=actual_req.naming_rule or "{title}.{author}.{ext}",
                    keep_source=actual_req.keep_source,
                    use_original_name=actual_req.use_original_name,
                    target_dir=actual_req.target_dir,
                )
                with _lock:
                    if tid in _tasks:
                        if result["success"]:
                            _tasks[tid].status = "success"
                            _tasks[tid].progress = 100
                            _tasks[tid].output_path = result.get("output_path", "")
                        else:
                            _tasks[tid].status = "failed"
                            _tasks[tid].error = result.get("error", "未知错误")
                        _tasks[tid].completed_at = datetime.now().isoformat()
            _save_history()
    
    thread = threading.Thread(target=_run_batch, daemon=True)
    thread.start()
    
    return {"task_ids": task_ids}


@router.get("/task/{task_id}")
def get_task(task_id: str):
    """查询单个任务状态"""
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/tasks")
def list_tasks(limit: int = 50):
    """列出最近的任务"""
    with _lock:
        sorted_tasks = sorted(
            _tasks.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )[:limit]
    return sorted_tasks


@router.post("/retry/{task_id}")
def retry_task(task_id: str):
    """重试失败的任务"""
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "failed":
        raise HTTPException(400, "只能重试失败的任务")
    
    # 新建重试任务
    new_req = ConvertRequest(
        source_path=task.source_path,
        target_format=task.target_format,
        keep_source=True,
    )
    return start_convert(new_req)


@router.get("/retry-failed")
def retry_all_failed():
    """重试所有失败任务"""
    with _lock:
        failed = [t for t in _tasks.values() if t.status == "failed"]
    
    task_ids = []
    for task in failed:
        new_req = ConvertRequest(
            source_path=task.source_path,
            target_format=task.target_format,
            keep_source=True,
        )
        result = start_convert(new_req)
        task_ids.append(result["task_id"])
    
    return {"task_ids": task_ids}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件，统一存到临时目录"""
    dest_dir = Path(TEMP_DIR)
    os.makedirs(dest_dir, exist_ok=True)
    stem = Path(file.filename).stem if file.filename else "file"
    ext = Path(file.filename).suffix if file.filename else ".tmp"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = dest_dir / f"{stem}_{ts}{ext}"
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return {"path": str(dest), "name": file.filename, "size": dest.stat().st_size}
    except Exception as e:
        raise HTTPException(500, f"上传失败: {e}")


@router.get("/download/{file_path:path}")
def download_file(file_path: str):
    """下载临时目录中的文件（防路径遍历）"""
    # 防止路径遍历攻击：只取文件名，忽略目录
    clean = Path(file_path).name
    fpath = Path(TEMP_DIR) / clean
    try:
        fpath.resolve().relative_to(Path(TEMP_DIR).resolve())
    except ValueError:
        raise HTTPException(400, "非法路径")
    if not fpath.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>文件已过期</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0f1117;color:#e8eaed;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#1a1b23;border:1px solid #2d2e3a;border-radius:12px;padding:40px;text-align:center;max-width:400px}
.icon{font-size:48px;margin-bottom:16px}
h2{margin:0 0 8px;font-size:20px}
p{color:#9aa0a6;font-size:14px;line-height:1.6}
.btn{display:inline-block;margin-top:16px;padding:8px 20px;background:#6366f1;color:white;border-radius:8px;text-decoration:none;font-size:13px}
</style></head><body>
<div class="card"><div class="icon">⏰</div>
<h2>文件已过期</h2>
<p>该临时文件已被清理或已超过保留时间。<br>如需重新转换，请回到 EbookForge 重新操作。</p>
<a class="btn" href="/">返回 EbookForge</a>
</div></body></html>""", status_code=404)
    return FileResponse(str(fpath), filename=fpath.name)


@router.get("/temp-stats")
def temp_stats():
    """临时目录统计"""
    d = Path(TEMP_DIR)
    if not d.exists():
        return {"file_count": 0, "total_size": 0, "files": []}
    total = 0
    files = []
    for f in d.iterdir():
        if f.is_file():
            total += f.stat().st_size
            files.append({"name": f.name, "size": f.stat().st_size})
    return {"file_count": len(files), "total_size": total, "files": sorted(files, key=lambda x: x["name"])}


@router.delete("/temp-clear")
def temp_clear():
    """清空临时目录"""
    d = Path(TEMP_DIR)
    if d.exists():
        count = 0
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
        return {"deleted": count}
    return {"deleted": 0}


@router.get("/supported-targets/{ext}")
def get_targets(ext: str):
    """查询某格式支持转换的目标格式"""
    return {"targets": get_supported_targets(ext)}


@router.get("/matrix")
def get_matrix():
    """返回完整的「源格式 → 可用目标格式」矩阵

    前端批量转换前用它做兼容性校验：提前告诉用户哪些选中文件转不了目标格式，
    避免转完才发现一堆失败。
    """
    from ..config import CONVERT_MATRIX
    return {"matrix": CONVERT_MATRIX}
