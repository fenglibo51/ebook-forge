"""EbookForge · 后端入口

    python3 -m backend.main            # 开发
    uvicorn backend.main:app --host 0.0.0.0 --port 8506

前端是纯静态文件，直接由这个进程挂在根路径，不需要 nginx。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

# 让 `python3 -m backend.main` 在项目根目录下能找到 bookforge
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI                                    # noqa: E402
from fastapi.responses import JSONResponse                     # noqa: E402
from fastapi.staticfiles import StaticFiles                    # noqa: E402
from contextlib import asynccontextmanager                     # noqa: E402

from .api import cleanup_uploads, router                       # noqa: E402

FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时清一次过期的上传副本（24h TTL）
    try:
        r = cleanup_uploads()
        if r["removed"]:
            print(f"[cache] 启动清理：{r['removed']} 个过期上传副本，释放 {r['freed'] / 1048576:.1f} MB")
    except Exception as e:
        print(f"[cache] 启动清理失败：{e}")
    yield


app = FastAPI(title="EbookForge", version="0.1.0", docs_url="/api/docs",
              lifespan=lifespan)


# ── 管理令牌 ──────────────────────────────────────────────────────────────
# 设了 EBOOKFORGE_ADMIN_TOKEN 之后，/api/* 都要令牌，只有这两类豁免：
#   /api/s/*        分享链接（访客用）
#   /api/health, /api/capabilities  不敏感的只读信息
# 不设就完全不拦（内网自用）。
#
# 令牌可以放在三处任意一个：X-Admin-Token 头、ebf_admin cookie、?k= 参数。
# 为什么要 cookie：下载是 window.location 跳转，带不了自定义头。

_ADMIN_TOKEN = os.environ.get("EBOOKFORGE_ADMIN_TOKEN", "").strip()
_EXEMPT_PREFIX = ("/api/s/",)
_EXEMPT_PATH = ("/api/health", "/api/capabilities")


@app.middleware("http")
async def admin_guard(request, call_next):
    if not _ADMIN_TOKEN:
        return await call_next(request)

    path = request.url.path
    # 静态资源（前端页面、图片）不拦，否则连登录框都打不开
    if not path.startswith("/api/"):
        return await call_next(request)
    if path.startswith(_EXEMPT_PREFIX) or path in _EXEMPT_PATH:
        return await call_next(request)

    tok = (request.headers.get("X-Admin-Token")
           or request.cookies.get("ebf_admin")
           or request.query_params.get("k")
           or "")
    if tok and secrets.compare_digest(tok, _ADMIN_TOKEN):
        return await call_next(request)
    return JSONResponse(status_code=401,
                        content={"detail": "需要管理令牌", "need_token": True})


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    """别把 traceback 甩给前端，但日志里要有。"""
    import traceback
    traceback.print_exc()
    return JSONResponse(status_code=500,
                        content={"detail": f"{exc.__class__.__name__}: {exc}"})


app.include_router(router)


@app.get("/s/{token}")
def share_page(token: str):
    """分享链接也渲染同一份前端，由前端自己切到受限模式。"""
    from fastapi.responses import FileResponse
    return FileResponse(FRONTEND / "index.html")

# 静态前端挂根路径（必须在 API 之后注册，否则 /api 会被吃掉）
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")


def main() -> None:
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8506, reload=False)


if __name__ == "__main__":
    main()
