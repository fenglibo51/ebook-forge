"""HTTP 接口。前端只认这些，引擎内部怎么改都不影响它。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import jobs, store

router = APIRouter(prefix="/api")

UPLOAD_DIR = store.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── 配额（都能用环境变量调） ──────────────────────────────────────────────
MB = 1024 * 1024
MAX_UPLOAD_MB = int(os.environ.get("EBOOKFORGE_MAX_UPLOAD_MB", "2048"))      # 单次上传总量
MAX_FILE_MB = int(os.environ.get("EBOOKFORGE_MAX_FILE_MB", "1024"))          # 单文件
MAX_FILES_UPLOAD = int(os.environ.get("EBOOKFORGE_MAX_FILES_UPLOAD", "50"))  # 单次文件数
MAX_FILES_TASK = int(os.environ.get("EBOOKFORGE_MAX_FILES_TASK", "50"))      # 单任务文件数
MAX_QUEUE = int(os.environ.get("EBOOKFORGE_MAX_QUEUE", "20"))                # 排队任务上限

# 目录筛选扫描结果缓存：{(base, fmt): (时间戳, 目录集合, 是否截断)}
_SCAN_CACHE: dict[tuple[str, str], tuple[float, set, bool, int]] = {}

# 上传的临时文件保留 24 小时，超时自动清理
UPLOAD_TTL = int(os.environ.get("EBOOKFORGE_UPLOAD_TTL", 86400))


def cleanup_uploads(max_age: float = UPLOAD_TTL) -> dict[str, Any]:
    """删掉超过 TTL 的上传副本。源文件在用户设备上，删了不心疼。"""
    import time
    now = time.time()
    removed = 0
    freed = 0
    for p in UPLOAD_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime > max_age:
            try:
                p.unlink()
                removed += 1
                freed += st.st_size
            except OSError:
                pass
    return {"removed": removed, "freed": freed}


def cache_info() -> dict[str, Any]:
    import time
    now = time.time()
    files = 0
    size = 0
    oldest_age = 0.0
    for p in UPLOAD_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files += 1
        size += st.st_size
        oldest_age = max(oldest_age, now - st.st_mtime)
    return {
        "files": files,
        "size": size,
        "oldest_age": int(oldest_age),
        "ttl": UPLOAD_TTL,
        "dir": str(UPLOAD_DIR),
    }

# 允许浏览器下载的目录（转换输出只可能落在这些地方）
def _download_roots() -> list[Path]:
    s = store.load_settings()
    roots = [UPLOAD_DIR]
    for key in ("library_root",):
        p = Path(str(s.get(key, ""))).expanduser()
        if p.exists():
            roots.append(p)
    return roots


# ── 引擎能力 ──────────────────────────────────────────────────────────────

@router.get("/health")
def health() -> dict[str, Any]:
    from bookforge import __version__
    from bookforge.pdf.textpdf import find_font, LayoutError
    from bookforge.pdf.to_comic import RENDER_FALLBACK, RENDER_DPI

    try:
        font = str(find_font(store.load_settings().get("font_path", "")))
        font_ok = True
    except LayoutError as e:
        font, font_ok = str(e), False

    return {
        "ok": True,
        "version": __version__,
        "font": font,
        "font_ok": font_ok,
        "render_dpi": RENDER_DPI,
        "render_fallback": RENDER_FALLBACK,
    }


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """格式矩阵。前端据此决定哪些目标按钮该置灰。"""
    from bookforge.convert import P0_INPUTS, TEXT_INPUTS
    comic_inputs = sorted(x.lstrip(".") for x in P0_INPUTS | {".cbr", ".cb7", ".cbc", ".mobi"})
    text_inputs = sorted(x.lstrip(".") for x in TEXT_INPUTS)

    return {
        "comic": {
            "inputs": comic_inputs,
            "targets": ["cbz", "zip", "epub", "pdf"],
        },
        "text": {
            "inputs": text_inputs,
            "targets": ["txt", "fb2", "docx", "epub", "mobi", "pdf"],
        },
        "unsupported": [
            {"fmt": "azw3", "why": "KF8 的碎片化 HTML + FDST/INDX 表未实现"},
            {"fmt": "cbr", "why": "RAR5 算法闭源，纯 Python 无实现"},
            {"fmt": "huff-mobi", "why": "只支持 PalmDOC 压缩的 mobi"},
        ],
        "lossy": [
            {"fmt": "mask-pdf", "why": "蒙版型 PDF 只能逐页渲染，文字不渲染"},
        ],
    }


# ── 上传 ──────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload(files: list[UploadFile]) -> dict[str, Any]:
    if len(files) > MAX_FILES_UPLOAD:
        raise HTTPException(413, f"一次最多传 {MAX_FILES_UPLOAD} 个文件，这次给了 {len(files)} 个")

    saved = []
    total = 0
    for f in files:
        name = Path(f.filename or "unnamed").name.replace("\x00", "")
        if not name:
            continue
        dest = UPLOAD_DIR / name
        n = 1
        while dest.exists():
            dest = UPLOAD_DIR / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        written = 0
        try:
            with dest.open("wb") as out:
                # 边写边累计检查 —— 不只看 Content-Length，客户端可以撒谎
                while chunk := await f.read(MB):
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_FILE_MB * MB:
                        raise HTTPException(
                            413, f"{name} 超过单文件上限 {MAX_FILE_MB} MB")
                    if total > MAX_UPLOAD_MB * MB:
                        raise HTTPException(
                            413, f"本次上传总量超过上限 {MAX_UPLOAD_MB} MB")
                    out.write(chunk)
        except HTTPException:
            # 超限就把写了一半的文件删掉，别留垃圾
            try:
                dest.unlink()
            except OSError:
                pass
            raise
        saved.append({"path": str(dest), "name": dest.name, "size": dest.stat().st_size})

    if not saved:
        raise HTTPException(400, "没有收到文件")
    return {"files": saved, "total_size": total}


# ── 书库 ──────────────────────────────────────────────────────────────────

@router.get("/library")
def library(path: str = "") -> dict[str, Any]:
    s = store.load_settings()
    root = Path(str(s.get("library_root", "/Book"))).expanduser()
    if not root.exists():
        raise HTTPException(404, f"书库根目录不存在：{root}（在设置里改）")

    target = (root / path.lstrip("/")).resolve() if path else root.resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(403, "路径越界")      # 防 ../ 穿越

    if not target.is_dir():
        raise HTTPException(404, "目录不存在")

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": 0 if entry.is_dir() else st.st_size,
                "mtime": int(st.st_mtime),
                "fmt": "" if entry.is_dir() else entry.suffix.lstrip(".").lower(),
            })
    except PermissionError:
        raise HTTPException(403, "没有读取权限")

    rel = target.relative_to(root.resolve())
    crumbs = [{"name": root.name or "书库", "path": ""}]
    acc = ""
    for part in rel.parts:
        acc = f"{acc}/{part}".strip("/")
        crumbs.append({"name": part, "path": acc})

    return {
        "root": str(root),
        "path": "" if str(rel) == "." else str(rel),
        "crumbs": crumbs,
        "items": items,
    }


@router.get("/scan")
def scan_format(fmt: str = Query(..., description="要检查的格式，如 mobi"),
                path: str = "") -> dict[str, Any]:
    """递归看当前目录下哪些子目录「内部含有该格式的文件」。

    筛选 mobi 时，只有内部真有 mobi 的文件夹才该留下 —— 文件夹本身没有格式，
    不判断就会出现「选了 mobi，所有文件夹全消失」的情况。

    性能：递归 scandir，限制扫描条目数防 NAS 上卡死；结果缓存 30 秒。
    """
    s = store.load_settings()
    root = Path(str(s.get("library_root", "/Book"))).expanduser()
    if not root.exists():
        raise HTTPException(404, f"书库根目录不存在：{root}")

    base = (root / path.lstrip("/")).resolve() if path else root.resolve()
    try:
        base.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(403, "路径越界")
    if not base.is_dir():
        raise HTTPException(404, "目录不存在")

    want = fmt.lower().lstrip(".")
    cache_key = (str(base), want)
    now = time.time()
    hit = _SCAN_CACHE.get(cache_key)
    if hit and now - hit[0] < 30:
        return {"dirs": sorted(hit[1]), "cached": True,
                "truncated": hit[2], "scanned": hit[3]}

    found: set[str] = set()
    truncated = False
    scanned = 0
    budget = 20000                     # 最多看 20000 个目录条目（文件和子目录都算），超了截断
    stack: list[tuple[Path, str]] = [(base, "")]
    while stack:
        cur, rel = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError):
            continue
        for e in entries:
            budget -= 1
            scanned += 1
            if budget <= 0:
                truncated = True
                break
            if e.name.startswith("."):
                continue
            child_rel = f"{rel}/{e.name}".lstrip("/")
            try:
                if e.is_dir():
                    stack.append((Path(e.path), child_rel))
                elif e.name.lower().endswith("." + want):
                    # 这个文件所在的目录（及它的所有上级）都算「含该格式」
                    parts = child_rel.split("/")[:-1]
                    for i in range(1, len(parts) + 1):
                        found.add("/".join(parts[:i]))
            except OSError:
                continue
        if truncated:
            break

    _SCAN_CACHE[cache_key] = (now, found, truncated, scanned)
    return {"dirs": sorted(found), "cached": False,
            "truncated": truncated, "scanned": scanned}


# ── 转换任务 ──────────────────────────────────────────────────────────────

class JobFile(BaseModel):
    path: str
    name: str
    size: int = 0


class JobIn(BaseModel):
    files: list[JobFile]
    target: str
    options: dict[str, Any] = {}


@router.post("/jobs")
def create_job(body: JobIn) -> dict[str, Any]:
    if not body.files:
        raise HTTPException(400, "没有文件")
    if len(body.files) > MAX_FILES_TASK:
        raise HTTPException(413, f"单个任务最多 {MAX_FILES_TASK} 个文件")
    pending = jobs.pending_count()
    if pending >= MAX_QUEUE:
        raise HTTPException(429, f"排队任务已满（{pending}/{MAX_QUEUE}），等前面的跑完再提交")

    cleanup_uploads()                      # 建任务时顺手清一次过期副本
    jobs.gc_old()                          # 顺手清内存里过期完成的任务
    for f in body.files:
        p = Path(f.path).expanduser()
        if not p.exists():
            raise HTTPException(400, f"文件不存在：{f.name}")
        if not p.is_file():
            raise HTTPException(400, f"不是文件：{f.name}")
    job = jobs.submit([f.model_dump() for f in body.files],
                      body.target.lower().lstrip("."), body.options)
    return {"job_id": job.id, "total": job.total}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在（或已过期清理）")
    return job.to_dict()


# ── 历史 ──────────────────────────────────────────────────────────────────

@router.get("/history")
def history(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    return {"stats": store.stats(), "items": store.load_history(limit)}


@router.delete("/history")
def clear_history() -> dict[str, Any]:
    return {"deleted": store.clear_history()}


# ── 设置 ──────────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return store.load_settings()


@router.put("/settings")
def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
    return store.save_settings(patch)


# ── 缓存（上传的临时副本） ────────────────────────────────────────────────

@router.get("/cache")
def get_cache() -> dict[str, Any]:
    return cache_info()


@router.delete("/cache")
def clear_cache() -> dict[str, Any]:
    """手动清空。只删上传副本，不碰用户设备上的原文件、也不碰书库。"""
    info = cache_info()
    for p in UPLOAD_DIR.iterdir():
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    return {"removed": info["files"], "freed": info["size"]}


# ── 下载 ──────────────────────────────────────────────────────────────────

@router.get("/download")
def download(path: str) -> FileResponse:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    allowed = False
    for root in _download_roots():
        try:
            p.relative_to(root.resolve())
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise HTTPException(403, "不在允许下载的目录内")
    return FileResponse(p, filename=p.name)
