from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from typing import Optional

from ..services.file_manager import list_books, get_breadcrumbs, scan_file, rename_file, batch_rename as batch_rename_files
from ..config import BOOK_PATH, SUPPORTED_FORMATS

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/browse")
def browse(subdir: str = "", filter_ext: Optional[str] = None):
    """浏览书库目录"""
    items = list_books(subdir)
    
    if filter_ext:
        items = [it for it in items if it.get("ext") == filter_ext.lower() or it.get("is_dir")]
    
    breadcrumbs = get_breadcrumbs(subdir)
    
    # 统计
    file_count = sum(1 for it in items if not it["is_dir"])
    dir_count = sum(1 for it in items if it["is_dir"])
    
    return {
        "path": subdir,
        "breadcrumbs": breadcrumbs,
        "items": items,
        "stats": {"files": file_count, "dirs": dir_count},
    }


@router.get("/info")
def file_info(path: str = Query(..., description="文件相对路径")):
    """获取单个文件信息"""
    finfo = scan_file(path)
    if not finfo:
        raise HTTPException(404, "文件不存在")
    
    # 查询支持的目标格式
    from ..services.converter import get_supported_targets
    finfo["supported_targets"] = get_supported_targets(finfo.get("ext", ""))
    return finfo


@router.get("/filter")
def filter_by_format(ext: str = Query(..., description="格式过滤"), subdir: str = ""):
    """按格式筛选文件"""
    items = list_books(subdir)
    return [it for it in items if it.get("ext") == ext.lower()]


@router.post("/rename")
def rename(item_path: str = Query(...), new_name: str = Query(...)):
    """重命名文件"""
    result = rename_file(item_path, new_name)
    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result


@router.post("/batch-rename")
def batch_rename_endpoint(items: list[dict]):
    """批量重命名，items: [{path, new_name}]"""
    pairs = [(it["path"], it["new_name"]) for it in items]
    results = batch_rename_files(pairs)
    return {"results": results}


@router.get("/stats")
def library_stats():
    """书库统计"""
    all_items = []
    def scan(dir_path, rel=""):
        try:
            for entry in sorted(Path(dir_path).iterdir()):
                if entry.name.startswith("."):
                    continue
                rel_path = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    all_items.append({"name": entry.name, "path": rel_path, "type": "dir"})
                    scan(entry, rel_path)
                else:
                    ext = entry.suffix.lower().lstrip(".")
                    all_items.append({"name": entry.name, "path": rel_path, "type": "file", "ext": ext})
        except PermissionError:
            pass
    
    scan(BOOK_PATH)
    
    # 按格式统计
    format_counts = {}
    for it in all_items:
        if it.get("type") == "file":
            ext = it.get("ext", "?")
            format_counts[ext] = format_counts.get(ext, 0) + 1
    
    return {
        "total_files": sum(1 for it in all_items if it.get("type") == "file"),
        "total_dirs": sum(1 for it in all_items if it.get("type") == "dir"),
        "by_format": format_counts,
    }
