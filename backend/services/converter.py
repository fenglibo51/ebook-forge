import subprocess
import os
import re
import json
import glob
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from ..config import BOOK_PATH, SUPPORTED_FORMATS, TEMP_DIR, DEFAULT_CBZ_DPI


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


def _rasterize_to_cbz(pdf_path: Path, out_cbz: Path, dpi: int = DEFAULT_CBZ_DPI,
                      img_format: str = "jpeg", quality: int = 85) -> tuple:
    """把 PDF 每页光栅化成图片并打包为 CBZ。

    Calibre 没有 CBZ 输出插件（No plugin to handle output format: cbz），
    所以电子书转 CBZ 走这条路径：电子书 → PDF → 图片(每页) → zip 打包。

    img_format: "jpeg"（默认，照片/漫画页体积小）或 "png"（无损，文字/线条图体积小）
    返回 (成功: bool, 错误信息: str)
    """
    base = TEMP_DIR if Path(TEMP_DIR).exists() else None
    workdir = Path(tempfile.mkdtemp(prefix="cbz_", dir=base))
    try:
        ext = "jpg" if img_format == "jpeg" else "png"
        cmd = ["pdftoppm", "-r", str(dpi)]
        if img_format == "jpeg":
            cmd += ["-jpeg", "-jpegopt", f"quality={quality}"]
        else:
            cmd += ["-png"]
        cmd += [str(pdf_path), str(workdir / "page")]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return False, (r.stderr.strip() or r.stdout.strip() or "PDF 光栅化失败")
        pages = sorted(workdir.glob(f"page*.{ext}"))
        if not pages:
            return False, "光栅化后没有生成任何页面"
        with zipfile.ZipFile(str(out_cbz), "w", zipfile.ZIP_DEFLATED) as z:
            for p in pages:
                z.write(str(p), p.name)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "光栅化超时（15 分钟）"
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def convert_file(source_path: str, target_format: str, naming_rule: str = "{title}.{author}.{ext}", keep_source: bool = False, use_original_name: bool = False, target_dir: str = "temp", dpi: int = DEFAULT_CBZ_DPI, img_format: str = "jpeg") -> dict:
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
    
    is_comic = ext in ("zip", "cbz")
    
    # → CBZ
    if target_format == "cbz":
        if is_comic:
            # 漫画源：Calibre 没有 CBZ 输出插件，直接复制改名（zip 与 cbz 内容等价）
            meta = {"title": _guess_title_from_filename(source.name), "author": None, "year": None}
        else:
            # 电子书源：Calibre 不能输出 CBZ → 走 电子书→PDF→PNG→CBZ 光栅化
            meta = _parse_metadata(str(source))
            if not meta.get("title"):
                meta["title"] = _guess_title_from_filename(source.name)

        if use_original_name:
            new_name = source.stem
            new_name = re.sub(r'_\d{14}$', '', new_name)
            new_name = re.sub(r'_\d{8}$', '', new_name)
        else:
            new_name = _apply_naming_rule(naming_rule, meta["title"], meta.get("author", ""), "", meta.get("year"))
        output_path = source.parent / f"{new_name}.cbz"
        if output_path.exists() and not keep_source:
            output_path.unlink()

        if is_comic:
            shutil.copy2(str(source), str(output_path))
        else:
            # 临时 PDF 放 TEMP_DIR：源文件可能在只读书库目录，且避免并发同名冲突
            tmp_pdf = Path(TEMP_DIR) / f".{new_name}.{os.getpid()}.__tmp__.pdf"
            try:
                env = {**os.environ, "QTWEBENGINE_DISABLE_SANDBOX": "1"}
                r = subprocess.run(
                    ["ebook-convert", str(source), str(tmp_pdf)],
                    capture_output=True, text=True, timeout=900, env=env,
                )
                if r.returncode != 0 or not tmp_pdf.exists():
                    err = (r.stderr.strip() or r.stdout.strip() or "转 PDF 失败")
                    return {"success": False, "error": err[-800:]}
                ok, err = _rasterize_to_cbz(tmp_pdf, output_path, dpi, img_format)
                if not ok:
                    return {"success": False, "error": err}
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "转 PDF 超时（15 分钟）"}
            finally:
                try: tmp_pdf.unlink()
                except: pass

        final_path = output_path
        if target_dir != "temp" and output_path.exists():
            book_dest = Path(BOOK_PATH) / target_dir.lstrip("/") / output_path.name
            book_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(output_path), str(book_dest))
            final_path = book_dest
            if str(source).startswith(TEMP_DIR):
                try: source.unlink()
                except: pass
            try: output_path.unlink()
            except: pass
        elif not keep_source and str(source).startswith(TEMP_DIR):
            try: source.unlink()
            except: pass
        return {"success": True, "output_path": str(final_path), "metadata": meta}
    
    # 漫画 ZIP → EPUB/PDF 等：Calibre 按后缀识别格式，zip 必须先复制成 .cbz 才会走漫画输入路径
    real_input = source
    cbz_tmp = None
    if ext == "zip":
        cbz_tmp = source.with_suffix(".cbz")
        if not cbz_tmp.exists():
            shutil.copy2(str(source), str(cbz_tmp))
        real_input = cbz_tmp
    
    # 提取元数据
    meta = _parse_metadata(str(real_input))
    if not meta.get("title"):
        meta["title"] = _guess_title_from_filename(source.name)
    
    # 生成新文件名
    if use_original_name:
        new_name = source.stem
        new_name = re.sub(r'_\d{14}$', '', new_name)
        new_name = re.sub(r'_\d{8}$', '', new_name)
    else:
        new_name = _apply_naming_rule(naming_rule, meta["title"], meta.get("author", ""), "", meta.get("year"))
    output_path = source.parent / f"{new_name}.{target_format}"
    
    # 如果 keep_source=False 且目标格式已存在，先删除
    if output_path.exists() and not keep_source:
        output_path.unlink()
    
    try:
        # PDF 输出依赖 QtWebEngine，root 容器必须禁用沙箱
        env = {**os.environ, "QTWEBENGINE_DISABLE_SANDBOX": "1"}
        result = subprocess.run(
            ["ebook-convert", str(real_input), str(output_path)],
            capture_output=True, text=True, timeout=300, env=env
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "转换失败，未知错误"
            return {"success": False, "error": error_msg}
        
        final_path = output_path
        
        # 如果目标不是 temp，复制到目标目录并清理 temp
        if target_dir != "temp" and output_path.exists():
            book_dest = Path(BOOK_PATH) / target_dir.lstrip("/") / output_path.name
            book_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(output_path), str(book_dest))
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
        
        # 清理 zip 转换产生的临时 cbz 副本
        if cbz_tmp and cbz_tmp.exists() and str(cbz_tmp).startswith(TEMP_DIR):
            try: cbz_tmp.unlink()
            except: pass
        
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
