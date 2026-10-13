import subprocess
import os
import re
import json
import tempfile
import shutil
import zipfile
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


IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")


def _extract_images_to_cbz(epub_path: Path, out_cbz: Path) -> tuple:
    """漫画 epub → CBZ：无损、秒级。

    epub 本质是带目录结构的 zip。漫画 epub 里几乎全是图片，
    按 OPF spine 的**阅读顺序**取出内嵌图片原样打包即可 ——
    不经 Calibre、不转 PDF、不重编码，画质 100% 保留。

    用 spine 而不是文件名排序的原因：漫画 epub 的封面文件名常常
    是序号最大的那个（如 00193.jpeg），但它是第 1 页；按文件名排会把封面丢到最后。

    返回 (成功: bool, 错误信息: str, 页数: int)
    """
    import posixpath
    try:
        with zipfile.ZipFile(str(epub_path)) as z:
            names = set(z.namelist())

            # 1) 从 META-INF/container.xml 定位 OPF
            opf_path = None
            if "META-INF/container.xml" in names:
                c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
                m = re.search(r'full-path="([^"]+)"', c)
                if m:
                    opf_path = m.group(1)

            ordered = []
            if opf_path and opf_path in names:
                base = posixpath.dirname(opf_path)
                opf = z.read(opf_path).decode("utf-8", "ignore")

                # manifest: id -> href
                manifest = {}
                for tag in re.findall(r"<item\b[^>]*/?>", opf):
                    idm = re.search(r'id="([^"]+)"', tag)
                    hm = re.search(r'href="([^"]+)"', tag)
                    if idm and hm:
                        manifest[idm.group(1)] = hm.group(1)

                # 按 spine 顺序遍历
                spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
                for sid in spine:
                    href = manifest.get(sid)
                    if not href:
                        continue
                    full = posixpath.normpath(posixpath.join(base, href) if base else href)
                    if full not in names:
                        continue
                    e = posixpath.splitext(full)[1].lower()
                    if e in IMG_EXTS:
                        if full not in ordered:
                            ordered.append(full)
                    elif e in (".html", ".xhtml", ".htm"):
                        html = z.read(full).decode("utf-8", "ignore")
                        srcs = (re.findall(r'<img[^>]*\bsrc="([^"]+)"', html)
                                + re.findall(r'<image[^>]*\bxlink:href="([^"]+)"', html)
                                + re.findall(r'<image[^>]*\bhref="([^"]+)"', html))
                        sub = posixpath.dirname(full)
                        for s in srcs:
                            p = posixpath.normpath(posixpath.join(sub, s) if sub else s)
                            if p in names and p not in ordered:
                                ordered.append(p)

            # 2) 兜底：OPF 解析不出来就按文件名排序
            if not ordered:
                ordered = sorted(n for n in names
                                 if posixpath.splitext(n)[1].lower() in IMG_EXTS)

            if not ordered:
                return False, "这本 epub 里没有图片，无法转换为 CBZ", 0

            # 防呆：图片太少或占体积比例过低 → 不是漫画
            # （小说类 epub 通常只有 1 张封面图，直接提取会产出只有一页的垃圾 cbz）
            total_size = sum(i.file_size for i in z.infolist())
            img_size = sum(z.getinfo(n).file_size for n in ordered)
            ratio = (img_size / total_size) if total_size else 0
            if len(ordered) < 5 or ratio < 0.3:
                return False, (f"这本 epub 不像漫画（图片 {len(ordered)} 张、约占体积 {ratio:.0%}），"
                               f"没有可提取的漫画内容。小说类书籍请转 EPUB / TXT / MOBI 等格式"), 0

            # 3) 原样打包：JPEG 本身已压缩，用 STORED 不再二次压缩（最快）；
            #    PNG 用 DEFLATED 收一下空间。重命名成 00001.jpg 保证阅读顺序。
            with zipfile.ZipFile(str(out_cbz), "w") as zout:
                for i, n in enumerate(ordered, 1):
                    e = posixpath.splitext(n)[1].lower()
                    comp = zipfile.ZIP_DEFLATED if e == ".png" else zipfile.ZIP_STORED
                    zout.writestr(f"{i:05d}{e}", z.read(n), compress_type=comp)

            return True, "", len(ordered)
    except zipfile.BadZipFile:
        return False, "epub 文件损坏（不是有效的 zip 结构）", 0
    except Exception as e:
        return False, f"提取图片失败: {e}", 0


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
    
    is_comic = ext in ("zip", "cbz")
    
    # → CBZ
    if target_format == "cbz":
        if is_comic:
            # 漫画压缩包（zip/cbz）：内容等价，直接复制改名
            meta = {"title": _guess_title_from_filename(source.name), "author": None, "year": None}
        elif ext == "epub":
            # 漫画 epub：本质是带目录结构的 zip，按阅读顺序提取内嵌图片，无损秒转
            meta = _parse_metadata(str(source))
            if not meta.get("title"):
                meta["title"] = _guess_title_from_filename(source.name)
        else:
            return {"success": False, "error": "只有 EPUB 和漫画压缩包（ZIP/CBZ）能转换为 CBZ"}

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
            ok, err, pages = _extract_images_to_cbz(source, output_path)
            if not ok:
                return {"success": False, "error": err}
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
