import subprocess
import os
import re
import json
import tempfile
import shutil
from pathlib import Path
from typing import Optional

from ..config import BOOK_PATH, SUPPORTED_FORMATS, TEMP_DIR


def _parse_metadata(file_path: str) -> dict:
    """从电子书提取元数据"""
    try:
        result = subprocess.run(
            ["ebook-meta", file_path],
            capture_output=True, text=True, timeout=30
        )
        meta = {"title": None, "author": None, "year": None}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Title"):
                meta["title"] = line.split(":", 1)[1].strip()
            elif line.startswith("Authors"):
                meta["author"] = line.split(":", 1)[1].strip()
        return meta
    except Exception:
        return {"title": None, "author": None, "year": None}


def _guess_title_from_filename(filename: str) -> str:
    """从文件名猜测书名，去掉扩展名和常见后缀"""
    name = Path(filename).stem
    # 去掉时间戳后缀 _20250709143022
    name = re.sub(r'_\d{14}$', '', name)
    name = re.sub(r'_\d{8}$', '', name)
    # 去掉常见后缀
    for suffix in ["(Z-Library)", "(z-lib.org)", "[精校版]", "[校对版]", "（校对版全本）"]:
        name = name.replace(suffix, "")
    # 去掉括号内容中的作者信息
    name = re.sub(r'[\(（][^)）]*[\)）]', '', name).strip()
    name = re.sub(r'\s+', ' ', name).strip()
    return name if name else Path(filename).stem


def _apply_naming_rule(rule: str, title: str, author: str, ext: str, year: Optional[str] = None) -> str:
    """应用命名规则"""
    if not author:
        author = "未知"
    if not title:
        title = "未知"
    # 清理非法文件名字符
    title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
    author = re.sub(r'[\\/:*?"<>|]', '', author).strip()
    
    result = rule
    result = result.replace("{title}", title)
    result = result.replace("{author}", author)
    result = result.replace("{year}", year or "")
    # {ext} 在外面统一拼，这里替换为空防双重后缀
    result = result.replace("{ext}", "")
    # 清理多余空格和点
    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r'\.{2,}', '.', result)
    result = result.strip('. ')
    return result


def convert_file(source_path: str, target_format: str, naming_rule: str = "{title}.{author}.{ext}", keep_source: bool = False, use_original_name: bool = False, target_dir: str = "temp") -> dict:
    """
    转换单个电子书文件
    返回: {"success": bool, "output_path": str, "error": str, "metadata": dict}
    """
    source = Path(source_path)
    if not source.exists():
        return {"success": False, "error": f"文件不存在: {source_path}"}
    
    ext = source.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_FORMATS:
        return {"success": False, "error": f"不支持源格式: {ext}"}
    if target_format not in SUPPORTED_FORMATS:
        return {"success": False, "error": f"不支持目标格式: {target_format}"}
    
    # 提取元数据
    meta = _parse_metadata(str(source))
    if not meta.get("title"):
        meta["title"] = _guess_title_from_filename(source.name)

    # 生成新文件名
    if use_original_name:
        new_name = source.stem
    else:
        new_name = _apply_naming_rule(naming_rule, meta["title"], meta.get("author", ""), "", meta.get("year"))
    output_path = source.parent / f"{new_name}.{target_format}"
    
    # 如果 keep_source=False 且目标格式已存在，先删除
    if output_path.exists() and not keep_source:
        output_path.unlink()
    
    try:
        result = subprocess.run(
            ["ebook-convert", str(source), str(output_path)],
            capture_output=True, text=True, timeout=300
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "转换失败，未知错误"
            return {"success": False, "error": error_msg}
        
        final_path = output_path
        
        # 如果目标不是 temp，复制到目标目录并清理 temp
        if target_dir != "temp" and output_path.exists():
            book_dest = Path(BOOK_PATH) / target_dir.lstrip("/") / output_path.name
            book_dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil as sh
            sh.copy2(str(output_path), str(book_dest))
            final_path = book_dest
            # 清理 temp 中的源文件和输出文件
            if str(source).startswith(TEMP_DIR):
                try: source.unlink()
                except: pass
            try: output_path.unlink()
            except: pass
        
        # 上传的文件在 temp 里，永远不删原始书库文件
        if not keep_source and output_path.exists() and str(source).startswith(TEMP_DIR):
            source.unlink()
        
        return {
            "success": True,
            "output_path": str(final_path),
            "metadata": meta
        }
        
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "转换超时（5分钟）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_supported_targets(source_ext: str) -> list:
    """获取某格式支持转换的目标格式列表"""
    from ..config import CONVERT_MATRIX
    ext = source_ext.lower().lstrip(".")
    return CONVERT_MATRIX.get(ext, [])


def check_calibre_available() -> bool:
    """检查 Calibre ebook-convert 是否可用"""
    try:
        result = subprocess.run(["ebook-convert", "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False
