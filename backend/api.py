"""HTTP 接口。前端只认这些，引擎内部怎么改都不影响它。"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import jobs, share, store, uploads

router = APIRouter(prefix="/api")

UPLOAD_DIR = store.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── 配额（都能用环境变量调） ──────────────────────────────────────────────
MB = 1024 * 1024
# 单次上传总量：批量场景的约束（磁盘），默认 8 GB
MAX_UPLOAD_MB = int(os.environ.get("EBOOKFORGE_MAX_UPLOAD_MB", "8192"))
# 单文件：电子书几十上百 MB 常见，1 GB 足够；再大引擎整本读内存也扛不住
MAX_FILE_MB = int(os.environ.get("EBOOKFORGE_MAX_FILE_MB", "1024"))
MAX_FILES_UPLOAD = int(os.environ.get("EBOOKFORGE_MAX_FILES_UPLOAD", "10"))
MAX_FILES_TASK = int(os.environ.get("EBOOKFORGE_MAX_FILES_TASK", "10"))
MAX_QUEUE = int(os.environ.get("EBOOKFORGE_MAX_QUEUE", "10"))
# 单会话 / 单链接的在途任务上限。只防「一个人占满全局队列」，
# 不影响正常用户 —— 一个人同时排两个转换任务已经很少见了。
MAX_PENDING_PER_SID = int(os.environ.get("EBOOKFORGE_MAX_PENDING_SID", "2"))
MAX_PENDING_PER_TOKEN = int(os.environ.get("EBOOKFORGE_MAX_PENDING_TOKEN", "6"))

# 渲染 DPI 只允许这三个值。前端下拉框是这三个，但 API 是公开的 ——
# 实测传 100000 / -5 / 0 都被原样接受，任务里存的也是这些值。
# 为什么必须在服务端卡：to_comic 模块初始化时那个白名单只在 import 时跑一次，
# _run() 里 `to_comic.RENDER_DPI = int(...)` 会把它覆盖掉。
# 渲染时 scale = dpi/72，100000dpi 就是 1389 倍放大，几亿像素的 bytearray 能把容器打到 OOM。
RENDER_DPI_CHOICES = (150, 200, 300)


def _clean_render_dpi(raw: Any) -> int:
    """校验并返回 render_dpi。不合法直接 422，别让它进任务。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            422, f"render_dpi 只能是 {'/'.join(map(str, RENDER_DPI_CHOICES))}，"
                 f"收到的是 {raw!r}")
    if v not in RENDER_DPI_CHOICES:
        raise HTTPException(
            422, f"render_dpi 只能是 {'/'.join(map(str, RENDER_DPI_CHOICES))}，收到 {v}")
    return v

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
    try:
        from . import uploads as _u
        _u.purge_missing()
        # 索引里没有的文件（半截上传、登记失败）也一并扫掉，
        # 否则它们永远不会到期，一直占着磁盘
        orphans = _u.purge_orphans(UPLOAD_DIR)
        removed += len(orphans)
    except Exception:
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

async def _do_upload(files: list[UploadFile], token: str = "", sid: str = "") -> dict[str, Any]:
    """真正干活的上传实现，管理端和分享访客共用。

    ⚠️ 存储名用随机 file_id，不用原始文件名 —— 两个原因：
      ① 归属：只有服务端知道 file_id 对应谁上传的，猜不到别人的
      ② 并发：`exists()` + `open("wb")` 之间有竞态，同名并发会互相覆盖。
         改成 `open("xb")`（O_EXCL）一旦撞名就抛错重来，天然排他。
    原始文件名只作为展示用，存在 uploads.json 里。
    """
    if len(files) > MAX_FILES_UPLOAD:
        raise HTTPException(413, f"一次最多传 {MAX_FILES_UPLOAD} 个文件，这次给了 {len(files)} 个")

    # 配额凭据：边写边准入，并发上传不可能一起越过上限
    ticket = uploads.begin(token, sid)
    try:
        total, saved = await _save_loop(files, ticket, token, sid)
    finally:
        ticket.finish()
    return {"files": saved, "total_size": total}


async def _save_loop(files: list[UploadFile], ticket: "uploads.Ticket",
                     token: str, sid: str) -> tuple[int, list[dict[str, Any]]]:
    """逐个落盘。任何异常都要把半截文件删掉、索引不能留脏记录。"""
    saved: list[dict[str, Any]] = []
    total = 0
    for f in files:
        name = Path(f.filename or "unnamed").name.replace("\x00", "")
        if not name:
            continue
        suffix = Path(name).suffix.lower()
        if len(suffix) > 16:
            suffix = ""
        # 排他创建：拿不到就换一个 id 重试（实际几乎不会撞）
        dest = None
        fid = ""
        for _ in range(5):
            fid = uploads.new_file_id()
            candidate = UPLOAD_DIR / f"{fid}{suffix}"
            try:
                fh = open(candidate, "xb")
                dest = candidate
                break
            except FileExistsError:
                continue
        if dest is None:
            raise HTTPException(500, "分配存储名失败，重试一次")

        written = 0
        registered = False
        try:
            with fh as out:
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
                    # 累计配额（整个目录 / 这条链接 / 这个会话）
                    try:
                        ticket.admit(len(chunk))
                    except uploads.QuotaError as e:
                        raise HTTPException(413, str(e)) from e
                    out.write(chunk)
            size = dest.stat().st_size
            uploads.register(dest, name, size, token=token, sid=sid, file_id=fid)
            registered = True
        except BaseException:
            # ⚠️ 不能只 catch HTTPException：
            # 磁盘满、权限、客户端断流抛的都是 OSError/ConnectionError，
            # 那些同样会留下写了一半的文件。任何异常都必须清干净。
            if not registered:
                try:
                    dest.unlink()
                except OSError:
                    pass
            # 整批要么全成，要么一个都不留。
            # 早先只清当前这个文件：传 3 个、第 2 个超限时，第 1 个已经登记落盘
            # 并占着额度，接口却返回 413 —— 用户重试一次就重复占一份配额，
            # 而且他根本不知道有个半截批次留在服务器上。
            for rec in saved:
                info = uploads.get(rec["file_id"]) or {}
                try:
                    Path(str(info.get("path", ""))).unlink()
                except OSError:
                    pass
                uploads.forget(rec["file_id"])
            saved.clear()
            raise
        # 只回 file_id，不回服务器路径 —— 访客拿不到别人的路径就没法冒用
        saved.append({"file_id": fid, "name": name, "size": size})

    if not saved:
        raise HTTPException(400, "没有收到文件")
    return total, saved


@router.post("/upload")
async def upload(files: list[UploadFile]) -> dict[str, Any]:
    return await _do_upload(files)


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
    # 上传的文件用 file_id（服务端核对归属）；书库里的文件用 path（仅管理端）
    file_id: str = ""
    path: str = ""
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

    cleanup_uploads()                      # 建任务时顺手清一次过期副本
    jobs.gc_old()                          # 顺手清内存里过期完成的任务

    resolved: list[dict[str, Any]] = []
    for f in body.files:
        if f.file_id:
            rec = uploads.owns(f.file_id, "", "")
            if not rec:
                raise HTTPException(400, f"文件不存在或已过期：{f.name}")
            resolved.append({"path": rec["path"], "name": rec.get("name", f.name),
                             "size": rec.get("size", 0)})
            continue
        # 书库里的文件直接给路径（管理端才有这个能力）
        p = Path(f.path).expanduser()
        if not p.exists():
            raise HTTPException(400, f"文件不存在：{f.name}")
        if not p.is_file():
            raise HTTPException(400, f"不是文件：{f.name}")
        resolved.append({"path": str(p), "name": f.name, "size": f.size})
    # 管理端也是走 API 的，options 里的 render_dpi 一样要校验 ——
    # 「自己人不会乱传」不是理由，脚本/前端 bug 一样能把 dpi 传成 100000。
    opts = dict(body.options)
    opts["render_dpi"] = _clean_render_dpi(opts.get("render_dpi", 200))
    try:
        job = jobs.submit(resolved, body.target.lower().lstrip("."), opts,
                          max_total=MAX_QUEUE)
    except jobs.QueueFull as e:
        raise HTTPException(429, str(e))
    return {"job_id": job.id, "total": job.total}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在（或已过期清理）")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """取消任务。

    排队中的立刻结束（不写盘）；正在跑的等当前文件转完就停，
    不会打断正在写的那个文件（强杀会留半截文件）。
    已完成的不删、源文件不动。
    """
    return jobs.cancel(job_id)


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
    before = bool(store.load_settings().get("share_enabled"))
    cur = store.save_settings(patch)
    # 走设置接口关分享，和走 /api/share/toggle 关，效果必须一致：旧链接全部作废
    if before and not cur.get("share_enabled"):
        n = 0
        for it in share.list_all():
            if share.revoke(it["token"]):
                uploads.drop_token(it["token"])
                n += 1
        if n:
            print(f"[share] 设置里关闭分享模式，已撤销 {n} 条链接")
    return cur


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


# ── 分享模式开关 ──────────────────────────────────────────────────────────
# 默认关闭：后台不拦、分享按钮不可用，开箱即用不会莫名其妙要密码。
# 打开后：自动生成管理密钥，后台接口要它，分享链接功能同时可用。

class ShareToggleIn(BaseModel):
    enabled: bool
    admin_token: str = ""      # 用户自己设；留空 = 自动生成


@router.get("/share/status")
def share_status() -> dict[str, Any]:
    s = store.load_settings()
    enabled = bool(s.get("share_enabled"))
    env_locked = bool(os.environ.get("EBOOKFORGE_ADMIN_TOKEN", "").strip())
    return {
        "enabled": enabled,
        # 已经生成过就回显，方便用户找回
        "admin_token": str(s.get("admin_token", "")) if enabled else "",
        # 环境变量钉死的情况下，设置页的开关不起作用
        "env_locked": env_locked,
    }


@router.post("/share/toggle")
def share_toggle(body: ShareToggleIn) -> dict[str, Any]:
    if os.environ.get("EBOOKFORGE_ADMIN_TOKEN", "").strip():
        raise HTTPException(409, "令牌已由环境变量 EBOOKFORGE_ADMIN_TOKEN 固定，设置页改不了")

    s = store.load_settings()
    if body.enabled:
        want = body.admin_token.strip()
        if want and len(want) < 8:
            raise HTTPException(400, "密钥至少 8 位")
        # 用户输了自己的就用他的；没输就沿用之前的，再没有才随机生成
        tok = want or str(s.get("admin_token", "")).strip() or secrets.token_urlsafe(18)
        store.save_settings({"share_enabled": True, "admin_token": tok})
        return {"enabled": True, "admin_token": tok}

    store.save_settings({"share_enabled": False})
    # 关掉分享模式 = 之前发出去的链接全部作废
    revoked = 0
    for it in share.list_all():
        if share.revoke(it["token"]):
            uploads.drop_token(it["token"])
            revoked += 1
    return {"enabled": False, "revoked": revoked}


# ── 分享链接（管理端，只有主人用） ────────────────────────────────────────

class ShareIn(BaseModel):
    ttl_hours: int = 24          # 0 = 不设期限
    note: str = ""


@router.post("/shares")
def create_share(body: ShareIn) -> dict[str, Any]:
    if not store.load_settings().get("share_enabled"):
        raise HTTPException(403, "分享模式未开启，先去设置页打开")
    ttl = int(body.ttl_hours) * 3600 if body.ttl_hours else 0
    r = share.create(ttl=ttl, note=body.note)
    return {"token": r["token"], "path": f"/s/{r['token']}",
            "expires": r["expires"], "note": r["note"]}


@router.get("/shares")
def list_shares() -> dict[str, Any]:
    return {"items": share.list_all()}


@router.delete("/shares/{token}")
def revoke_share(token: str) -> dict[str, Any]:
    ok = share.revoke(token)
    if ok:
        # 链接没了，它名下的上传副本就再也取不出来 —— 当场收掉，别继续占配额
        uploads.drop_token(token)
    return {"revoked": ok}


# ── 分享链接（访客端，权限只到「转换」） ──────────────────────────────────

def _share_ctx(t: str) -> dict[str, Any]:
    """校验分享上下文。

    ⚠️ 必须**先看全局开关**再看 token —— 早先只看 token，
    于是通过 /api/settings（而不是 /api/share/toggle）关掉分享模式后，
    shares.json 里的旧 token 依然有效，链接还能继续用。实测确认过。
    """
    if not store.load_settings().get("share_enabled"):
        raise HTTPException(403, "分享模式已关闭")
    entry = share.check(t)
    if not entry:
        raise HTTPException(403, "链接无效或已过期，找发链接的人重新生成")
    return entry


@router.post("/s/upload")
async def share_upload(files: list[UploadFile], t: str = Query(...),
                       sid: str = Query(...)) -> dict[str, Any]:
    """分享访客上传：同样走 _do_upload，但把归属记到这条链接 + 这个会话上。"""
    _share_ctx(t)
    if not sid:
        raise HTTPException(400, "缺少会话标识")
    return await _do_upload(files, token=t, sid=sid)


@router.post("/s/jobs")
def share_create_job(body: JobIn, t: str = Query(...), sid: str = Query(...)) -> dict[str, Any]:
    entry = _share_ctx(t)
    if not sid:
        raise HTTPException(400, "缺少会话标识")
    if not body.files:
        raise HTTPException(400, "没有文件")
    if len(body.files) > MAX_FILES_TASK:
        raise HTTPException(413, f"单个任务最多 {MAX_FILES_TASK} 个文件")

    # ⚠️ 访客只能处理「自己上传的」文件。
    # 早先只检查路径在 UPLOAD_DIR 内 —— 那只能证明文件在公共上传目录里，
    # 不能证明是这个人传的：A、B 用同一条链接时，B 知道 A 的文件名就能抢着转。
    # 实测确认过这个漏洞（B 用 A 的路径建任务返回 200、能查进度、能下载）。
    # 现在只认 file_id，服务端核对 token + sid 必须都属于当前访客。
    resolved: list[dict[str, Any]] = []
    for f in body.files:
        if not f.file_id:
            raise HTTPException(403, f"缺少文件标识：{f.name}（分享接口不接受服务器路径）")
        rec = uploads.owns(f.file_id, t, sid)
        if not rec:
            raise HTTPException(403, f"这不是你上传的文件：{f.name}")
        if not Path(rec["path"]).is_file():
            raise HTTPException(400, f"文件已被清理：{f.name}")
        resolved.append({"path": rec["path"], "name": rec.get("name", f.name),
                         "size": rec.get("size", 0)})

    # 强制收紧：不许删源文件、不许指定输出目录
    opts = {
        "page_size": str(body.options.get("page_size", "a5")),
        "render_dpi": _clean_render_dpi(body.options.get("render_dpi", 200)),
        "render_fallback": bool(body.options.get("render_fallback", True)),
        "font_path": "",
        "output_dir": "",
        "delete_source": False,
    }
    cleanup_uploads()
    try:
        job = jobs.submit(resolved, body.target.lower().lstrip("."), opts,
                          share_token=t, session_id=sid,
                          max_total=MAX_QUEUE,
                          max_sid=MAX_PENDING_PER_SID,
                          max_token=MAX_PENDING_PER_TOKEN)
    except jobs.QueueFull as e:
        # 全局满了和「你自己排太多」要分开说，不然访客不知道是自己占的
        raise HTTPException(429, str(e))
    return {"job_id": job.id, "total": job.total, "expires": entry.get("expires", 0)}


@router.get("/s/jobs/{job_id}")
def share_get_job(job_id: str, t: str = Query(...), sid: str = Query(...)) -> dict[str, Any]:
    _share_ctx(t)
    job = jobs.get(job_id)
    # 只认自己这条链接、自己这个会话创建的任务
    if not job or job.share_token != t or job.session_id != sid:
        raise HTTPException(404, "任务不存在")
    data = job.to_dict()
    # 不给访客看服务器上的绝对路径
    for it in data["items"]:
        it.pop("out", None)
    return data


@router.post("/s/jobs/{job_id}/cancel")
def share_cancel(job_id: str, t: str = Query(...), sid: str = Query(...)) -> dict[str, Any]:
    """分享链接也要能取消自己那个任务。"""
    _share_ctx(t)
    job = jobs.get(job_id)
    if not job or job.share_token != t or job.session_id != sid:
        raise HTTPException(404, "任务不存在")
    return jobs.cancel(job_id)


@router.get("/s/download/{job_id}/{index}")
def share_download(job_id: str, index: int, t: str = Query(...),
                   sid: str = Query(...)) -> FileResponse:
    _share_ctx(t)
    job = jobs.get(job_id)
    if not job or job.share_token != t or job.session_id != sid:
        raise HTTPException(404, "任务不存在")
    if index < 0 or index >= len(job.items):
        raise HTTPException(404, "没有这一项")
    it = job.items[index]
    if not it.out or not Path(it.out).is_file():
        raise HTTPException(404, "文件还没生成好或已被清理")
    # 路径必须落在上传目录里 —— 双重保险，防止任务记录被篡改
    p = Path(it.out).resolve()
    try:
        p.relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "不允许下载这个文件")
    return FileResponse(p, filename=p.name)


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


def _serve_output(raw: str) -> FileResponse:
    """把「任务记录里登记过的产物」发出去。

    为什么不复用 /api/download?path=：那个接口只认上传目录和书库根目录，
    用户把输出目录设成 Compose 另外挂的 /Export 时，转换明明成功、
    产物就在那儿，下载却 403。

    把 /Export 整个加进允许根目录也能修，但那等于开个任意文件下载口。
    这里换了个思路：路径不接受用户输入，只能来自服务端自己的任务/历史记录，
    所以「能下载」== 「确实是自己转出来的东西」。
    """
    if not raw:
        raise HTTPException(404, "这一项没有产物")
    p = Path(raw)
    try:
        if not p.is_file():
            raise HTTPException(404, "文件还没生成好或已被清理")
    except OSError:
        raise HTTPException(404, "文件读不出来")
    return FileResponse(p, filename=p.name)


@router.get("/jobs/{job_id}/download/{index}")
def job_download(job_id: str, index: int) -> FileResponse:
    """管理端按任务取产物，路径由任务记录决定，自定义输出目录也能下。"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if index < 0 or index >= len(job.items):
        raise HTTPException(404, "没有这一项")
    it = job.items[index]
    if it.status not in ("ok", "lossy"):
        raise HTTPException(404, "这一项没有成功")
    return _serve_output(it.out)


@router.get("/history/{hid}/download")
def history_download(hid: str) -> FileResponse:
    """历史记录也要能下载 —— 任务对象重启就没了，只有历史还留着产物路径。"""
    for h in store.load_history(limit=5000):
        if h.get("id") != hid:
            continue
        if h.get("status") not in ("ok", "lossy"):
            raise HTTPException(404, "这条记录没有产物")
        return _serve_output(str(h.get("out", "")))
    raise HTTPException(404, "记录不存在")
