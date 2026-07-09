import os
import json
import time
import shutil
from pathlib import Path
from typing import Optional

from ..config import BOOK_PATH, TEMP_DIR


# 读写文件列表时统一转成相对路径，避免暴露容器内部路径
_BOOK = Path(BOOK_PATH)


def _abs_to_rel(abs_path: str) -> str:
    """将绝对路径转为相对 Book 的路径"""
    try:
        return str(Path(abs_path).relative_to(_BOOK))
    except ValueError:
        return abs_path


def _rel_to_abs(rel_path: str) -> Path:
    """将相对 Book 的路径转为绝对路径"""
    return _BOOK / rel_path


def list_books(subdir: str = "") -> list[dict]:
    """列出书库目录内容"""
    target = _BOOK
    if subdir:
        target = _BOOK / subdir.lstrip("/")
    
    if not target.exists() or not target.is_dir():
        return []
    
    results = []
    for entry in sorted(target.iterdir()):
        info = {
            "name": entry.name,
            "path": _abs_to_rel(str(entry)),
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if not entry.is_dir() else 0,
            "modified": entry.stat().st_mtime,
        }
        if not entry.is_dir():
            info["ext"] = entry.suffix.lower().lstrip(".")
        else:
            info["ext"] = ""
        results.append(info)
    
    return results


def get_breadcrumbs(subdir: str = "") -> list[dict]:
    """获取面包屑导航"""
    parts = [p for p in subdir.split("/") if p]
    crumbs = []
    path = ""
    for part in parts:
        path = f"{path}/{part}" if path else part
        crumbs.append({"name": part, "path": path})
    return crumbs


def scan_file(file_rel_path: str) -> Optional[dict]:
    """获取单个文件信息"""
    fpath = _rel_to_abs(file_rel_path)
    if not fpath.exists() or fpath.is_dir():
        return None
    return {
        "name": fpath.name,
        "path": file_rel_path,
        "ext": fpath.suffix.lower().lstrip("."),
        "size": fpath.stat().st_size,
        "modified": fpath.stat().st_mtime,
        "is_dir": False,
    }


def rename_file(file_rel_path: str, new_name: str) -> dict:
    """重命名文件"""
    fpath = _rel_to_abs(file_rel_path)
    if not fpath.exists():
        return {"success": False, "error": "文件不存在"}
    
    new_path = fpath.parent / new_name
    if new_path.exists():
        return {"success": False, "error": "目标文件已存在"}
    
    try:
        fpath.rename(new_path)
        return {"success": True, "new_path": _abs_to_rel(str(new_path))}
    except Exception as e:
        return {"success": False, "error": str(e)}


def batch_rename(file_pairs: list[tuple[str, str]]) -> list[dict]:
    """批量重命名，每对 (old_rel_path, new_name)"""
    results = []
    for old_path, new_name in file_pairs:
        results.append(rename_file(old_path, new_name))
    return results
