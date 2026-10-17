from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from typing import Optional

from ..services.file_manager import (
    list_books, get_breadcrumbs, scan_file, rename_file,
    batch_rename as batch_rename_files, dir_has_ext,
)
from ..config import BOOK_PATH, SUPPORTED_FORMATS

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/browse")
def browse(subdir: str = "", filter_ext: Optional[str] = None):
    """浏览书库目录

    filter_ext 同时作用于目录：只有内部（递归）真的含有该格式的目录才会显示，
    否则筛选后还会剩一堆无关目录，等于没筛。
    """
    items = list_books(subdir)

    if filter_ext:
        e = filter_ext.lower()
        filtered = []
        for it in items:
            if it.get("is_dir"):
                # 目录：递归检查内部是否有该格式的文件
                if dir_has_ext(Path(BOOK_PATH) / it["path"], e):
                    filtered.append(it)
            elif it.get("ext") == e:
                filtered.append(it)
        items = filtered

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


@router.get("/files")
def list_files_recursive(subdir: str = "", filter_ext: Optional[str] = None, only_supported: bool = False):
    """递归列出目录下所有文件（书库「全选」跨子目录批量转换用）

    漫画书库通常是 /book/comics/<系列>/<卷>.pdf 这种结构，
    在 comics 这一层点「全选」看到的全是目录，必须递归到文件才有意义。

    only_supported=True：只返回支持转换的格式（跳过封面图 jpg、音频 mp3 等无关文件）
    """
    base = Path(BOOK_PATH) / subdir.strip("/") if subdir.strip("/") else Path(BOOK_PATH)
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, "目录不存在")

    files = []

    def walk(d: Path, rel: str):
        try:
            for entry in sorted(d.iterdir()):
                if entry.name.startswith("."):
                    continue
                rel_path = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    walk(entry, rel_path)
                else:
                    ext = entry.suffix.lower().lstrip(".")
                    if filter_ext and ext != filter_ext.lower():
                        continue
                    if only_supported and ext not in SUPPORTED_FORMATS:
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    files.append({
                        "name": entry.name,
                        "path": rel_path,
                        "ext": ext,
                        "size": size,
                        "is_dir": False,
                    })
        except PermissionError:
            pass

    walk(base, subdir.strip("/"))
    return {"files": files, "count": len(files)}


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
