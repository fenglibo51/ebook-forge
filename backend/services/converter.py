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


def _ensure_local(path: Path, min_size: int = 1 << 20) -> tuple:
    """把网络挂载盘上的文件先复制到本地 temp，返回 (可读路径, 临时目录或 None)

    背景：CD2 / 115 / WebDAV 这类 FUSE 挂载，随机读极慢 ——
    zip/rar/pdf 解析会做几百次 seek，每次都可能触发网络往返 + 网盘 API 调用，
    比本地磁盘慢几十倍。整体顺序复制一次（O(1) 次网络传输）再本地解析，
    能把「请求次数 × 延迟」的损耗干掉。

    本地磁盘场景：多一次 1MB+ 的文件复制（约 0.1 秒），可忽略。
    """
    try:
        if path.stat().st_size < min_size:
            return path, None
        base = TEMP_DIR if Path(TEMP_DIR).exists() else None
        tmpdir = Path(tempfile.mkdtemp(prefix="src_", dir=base))
        local = tmpdir / path.name
        shutil.copy2(str(path), str(local))
        return local, tmpdir
    except Exception:
        # 复制失败（权限/空间/网络抖动）就退回原路径，不让优化影响主流程
        return path, None


def _with_local_source(fn, source: Path, *args):
    """先把网盘源本地化，再调用提取器，用完清理临时副本

    用法：ok, err, pages = _with_local_source(_extract_pdf_to_cbz, source, output_path)
    """
    src, tmpdir = _ensure_local(source)
    try:
        return fn(src, *args)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


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

            total_size = sum(i.file_size for i in z.infolist())
            img_size = sum(z.getinfo(n).file_size for n in ordered)
            ratio = (img_size / total_size) if total_size else 0

            # 防呆：整卷漫画图片肯定多；但「番外 / 短篇」只有 4 页也还是漫画。
            # 判据 = (页数够多 且 图片占比不算低)  → 整卷漫画
            #      或 (图片占比极高 且 不止一张图) → 4 页番外：图片 4 张占 99%
            # 后一条仍拦得住小说 epub —— 小说通常只有 1 张封面图，图片数不到 2。
            looks_comic = (len(ordered) >= 5 and ratio >= 0.3) or \
                          (len(ordered) >= 2 and ratio >= 0.85)
            if not looks_comic:
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


def _extract_archive_to_cbz(archive: Path, out_cbz: Path) -> tuple:
    """RAR / 7z 漫画包（cbr / cb7 / cbc）→ CBZ。

    Calibre 只能「读」这些格式（comic_input 的 file_types 含 cbr/cb7/cbc），
    但没有任何 CBZ 输出插件，所以自己解压取图再原样打包成 zip。
    用 unar 解压（一个工具同时覆盖 rar / 7z / zip，节省依赖）。

    返回 (成功: bool, 错误信息: str, 页数: int)
    """
    base = TEMP_DIR if Path(TEMP_DIR).exists() else None
    workdir = Path(tempfile.mkdtemp(prefix="arc_", dir=base))
    try:
        r = subprocess.run(
            ["unar", "-quiet", "-force-overwrite", "-output-directory", str(workdir), str(archive)],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or "解压失败")
            return False, f"解压失败：{err[:300]}", 0

        # 解压后可能有多层目录，递归收集图片并按路径排序（漫画包通常按序号命名）
        pages = sorted(p for p in workdir.rglob("*")
                       if p.is_file() and p.suffix.lower() in IMG_EXTS)
        if not pages:
            return False, "压缩包里没有图片，无法转换为 CBZ", 0
        if len(pages) < 5:
            return False, f"压缩包里只有 {len(pages)} 张图片，不像是漫画", 0

        with zipfile.ZipFile(str(out_cbz), "w") as zout:
            for i, p in enumerate(pages, 1):
                e = p.suffix.lower()
                comp = zipfile.ZIP_DEFLATED if e == ".png" else zipfile.ZIP_STORED
                zout.writestr(f"{i:05d}{e}", p.read_bytes(), compress_type=comp)
        return True, "", len(pages)
    except subprocess.TimeoutExpired:
        return False, "解压超时（15 分钟）", 0
    except FileNotFoundError:
        return False, "系统缺少 unar 解压工具", 0
    except Exception as e:
        return False, f"解压打包失败: {e}", 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _extract_pdf_to_cbz(pdf_path: Path, out_cbz: Path) -> tuple:
    """扫描版 / 图片型漫画 PDF → CBZ：pdfimages -all 无损提取内嵌图片流，原样打包。

    只对「每页是图」的 PDF 有效（扫描或导出的漫画 PDF 都是这种）。
    矢量 / 文字版 PDF 提取不到图片 —— 明确报错，不做有损渲染（用户定下的原则）。

    返回 (成功: bool, 错误信息: str, 页数: int)
    """
    base = TEMP_DIR if Path(TEMP_DIR).exists() else None
    workdir = Path(tempfile.mkdtemp(prefix="pdf_", dir=base))
    # pdfimages -all 保持原始编码：JPEG→.jpg、JPEG2000→.jp2、CCITT→.tif、raw→.ppm
    RASTER = (".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".tif", ".tiff", ".jbig2", ".jb2")
    RAW = (".ppm", ".pgm")
    try:
        r = subprocess.run(
            ["pdfimages", "-all", str(pdf_path), str(workdir / "pg")],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or "PDF 解析失败")
            return False, f"PDF 解析失败：{err[:300]}", 0

        files = sorted(workdir.glob("pg*"))
        images = [f for f in files if f.suffix.lower() in RASTER]
        if not images:
            # 没有常见图片格式时，退一步看 raw 光栅图（FlateDecode 的页面图就是 .ppm）
            images = [f for f in files if f.suffix.lower() in RAW]

        if not images:
            return False, ("这个 PDF 里提取不到图片，可能是矢量/文字版 PDF。"
                           "本功能只支持扫描版漫画 PDF，不做有损渲染"), 0
        if len(images) < 5:
            return False, f"PDF 里只提取到 {len(images)} 张图片，不像是漫画", 0

        with zipfile.ZipFile(str(out_cbz), "w") as zout:
            for i, p in enumerate(images, 1):
                e = p.suffix.lower()
                comp = zipfile.ZIP_DEFLATED if e in (".png", ".ppm", ".pgm") else zipfile.ZIP_STORED
                zout.writestr(f"{i:05d}{e}", p.read_bytes(), compress_type=comp)
        return True, "", len(images)
    except subprocess.TimeoutExpired:
        return False, "PDF 提取超时（15 分钟）", 0
    except Exception as e:
        return False, f"PDF 转 CBZ 失败: {e}", 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kindle_to_cbz(source: Path, out_cbz: Path) -> tuple:
    """Kindle 漫画（mobi / azw3）→ CBZ：先转成临时 epub，再按 epub 提取原始图片。

    mobi/azw3 是二进制容器，没有现成的图片提取路径；Calibre 转 epub 时会把内嵌
    图片原样导出，所以走「mobi → epub → 提取图片」这条链，最终仍然无损
    （ChainLP 之类工具做的漫画 mobi 里就是逐页 JPEG）。

    返回 (成功: bool, 错误信息: str, 页数: int)
    """
    tmp_epub = Path(TEMP_DIR) / f".{source.stem}.{os.getpid()}.__tmp__.epub"
    try:
        env = {**os.environ, "QTWEBENGINE_DISABLE_SANDBOX": "1"}
        r = subprocess.run(
            ["ebook-convert", str(source), str(tmp_epub)],
            capture_output=True, text=True, timeout=900, env=env,
        )
        if r.returncode != 0 or not tmp_epub.exists():
            err = (r.stderr.strip() or r.stdout.strip() or "转 epub 失败")
            return False, f"Kindle 格式转 epub 失败：{err[-500:]}", 0
        return _extract_images_to_cbz(tmp_epub, out_cbz)
    except subprocess.TimeoutExpired:
        return False, "Kindle 格式转换超时（15 分钟）", 0
    except Exception as e:
        return False, f"Kindle 转 CBZ 失败: {e}", 0
    finally:
        try:
            tmp_epub.unlink()
        except Exception:
            pass


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
    
    ZIP_LIKE = ("zip", "cbz")               # 内容本身就是 zip，可直接复制改名
    ARCHIVE_LIKE = ("cbr", "cb7", "cbc")    # RAR / 7z 等，需解压后重新打包
    is_comic = ext in ZIP_LIKE or ext in ARCHIVE_LIKE

    # → CBZ
    if target_format == "cbz":
        if is_comic:
            meta = {"title": _guess_title_from_filename(source.name), "author": None, "year": None}
        elif ext in ("epub", "pdf", "mobi", "azw3"):
            # epub/pdf：直接无损提取内嵌图；mobi/azw3：先转 epub 再提取
            meta = _parse_metadata(str(source))
            if not meta.get("title"):
                meta["title"] = _guess_title_from_filename(source.name)
        else:
            return {"success": False, "error": "只有 EPUB、PDF、MOBI、AZW3 和漫画包（ZIP/CBZ/CBR/CB7/CBC）能转换为 CBZ"}

        if use_original_name:
            new_name = source.stem
            new_name = re.sub(r'_\d{14}$', '', new_name)
            new_name = re.sub(r'_\d{8}$', '', new_name)
        else:
            new_name = _apply_naming_rule(naming_rule, meta["title"], meta.get("author", ""), "", meta.get("year"))
        output_path = source.parent / f"{new_name}.cbz"
        if output_path.exists() and not keep_source:
            output_path.unlink()

        if ext in ZIP_LIKE:
            # zip / cbz：内容等价，直接复制改名（顺序读，无需本地化）
            shutil.copy2(str(source), str(output_path))
        elif ext in ARCHIVE_LIKE:
            # cbr / cb7 / cbc：先把网盘源拉到本地再解压（rar/7z 解析是随机读）
            ok, err, pages = _with_local_source(_extract_archive_to_cbz, source, output_path)
            if not ok:
                return {"success": False, "error": err}
        elif ext == "pdf":
            # 扫描版漫画 PDF：pdfimages 无损提取（同样先本地化）
            ok, err, pages = _with_local_source(_extract_pdf_to_cbz, source, output_path)
            if not ok:
                return {"success": False, "error": err}
        elif ext in ("mobi", "azw3"):
            # Kindle 漫画：先转 epub，再按 epub 无损提取图片
            ok, err, pages = _with_local_source(_kindle_to_cbz, source, output_path)
            if not ok:
                return {"success": False, "error": err}
        else:
            # epub：按 OPF spine 阅读顺序提取内嵌图片
            ok, err, pages = _with_local_source(_extract_images_to_cbz, source, output_path)
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
        # 源文件清理：keep_source=False 且输出确实生成成功 → 删源
        # （书库源同样适用；输出不存在/为空时绝不删，避免转换失败还丢原文件）
        if not keep_source:
            try:
                if (source.exists() and Path(final_path).exists()
                        and Path(final_path).stat().st_size > 0
                        and str(source) != str(final_path)):
                    source.unlink()
            except Exception:
                pass
        return {"success": True, "output_path": str(final_path), "metadata": meta}
    
    # 漫画包 → EPUB/PDF 等：Calibre 按后缀认格式
    # - zip：内容就是 cbz，复制成 .cbz 后缀即可（放 temp，源可能在只读目录）
    # - cbr/cb7/cbc：Debian 版 Calibre 缺 unrardll 模块、读不了 RAR/7z，
    #   所以先自己解压打包成 cbz，再交给它转换
    real_input = source
    cbz_tmp = None
    if ext in ("zip", "cbr", "cb7", "cbc"):
        cbz_tmp = Path(TEMP_DIR) / f".{source.stem}.{os.getpid()}.__tmp__.cbz"
        if ext == "zip":
            shutil.copy2(str(source), str(cbz_tmp))
        else:
            ok, err, pages = _with_local_source(_extract_archive_to_cbz, source, cbz_tmp)
            if not ok:
                return {"success": False, "error": err}
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
        
        # 源文件清理：
        # - 源在 temp（上传中转）：上面的分支已处理
        # - 源在书库：仅当用户明确勾选「不保留原文件」(keep_source=False)
        #   且输出确实生成成功（存在且非空）时才删除，避免转换失败还丢原文件
        if not keep_source:
            try:
                if (source.exists() and Path(final_path).exists()
                        and Path(final_path).stat().st_size > 0
                        and str(source) != str(final_path)):
                    source.unlink()
            except Exception:
                pass
        
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
