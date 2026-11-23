"""EbookForge · 后端入口

    python3 -m backend.main            # 开发
    uvicorn backend.main:app --host 0.0.0.0 --port 8506

前端是纯静态文件，直接由这个进程挂在根路径，不需要 nginx。
"""

from __future__ import annotations

import os
import secrets
import time
import sys
from typing import Any
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


app = FastAPI(title="EbookForge", version="1.0.0", docs_url="/api/docs",
              lifespan=lifespan)


# ── 管理令牌 ──────────────────────────────────────────────────────────────
# 设了 EBOOKFORGE_ADMIN_TOKEN 之后，/api/* 都要令牌，只有这两类豁免：
#   /api/s/*        分享链接（访客用）
#   /api/health, /api/capabilities  不敏感的只读信息
# 不设就完全不拦（内网自用）。
#
# 令牌可以放在三处任意一个：X-Admin-Token 头、ebf_admin cookie、?k= 参数。
# 为什么要 cookie：下载是 window.location 跳转，带不了自定义头。

_EXEMPT_PREFIX = ("/api/s/",)
_EXEMPT_PATH = ("/api/health", "/api/capabilities")

# 令牌来源，优先级：
#   ① 环境变量 EBOOKFORGE_ADMIN_TOKEN（部署时固定，最高优先）
#   ② 设置页里打开「分享模式」后自动生成的那个
# 两个都没有 = 不拦（开箱即用、纯内网自用）
def _active_admin_token() -> str:
    """当前生效的管理令牌，空字符串 = 不拦。

    不缓存：settings.json 只有两百多字节，读一次不到 1 毫秒；
    而加了 1 秒缓存之后，在设置页刚打开分享模式会有一秒钟「没生效」的窗口
    （实测踩过：toggle 完立刻请求还是 200）。
    """
    env_tok = os.environ.get("EBOOKFORGE_ADMIN_TOKEN", "").strip()
    if env_tok:
        return env_tok
    try:
        from . import store
        s = store.load_settings()
        return str(s.get("admin_token", "")).strip() if s.get("share_enabled") else ""
    except Exception:
        return ""                      # 设置读不出来就别拦，别把人锁在外面


@app.middleware("http")
async def admin_guard(request, call_next):
    token = _active_admin_token()
    if not token:
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
    if tok and secrets.compare_digest(tok, token):
        return await call_next(request)
    return JSONResponse(status_code=401,
                        content={"detail": "需要管理令牌", "need_token": True})


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception) -> JSONResponse:
    """异常详情只写日志，回给浏览器一句通用话。

    原来把 `异常类名: 消息` 直接回前端 —— 文件路径、内部实现细节都可能漏出去，
    对拿到分享链接的人来说等于免费侦察。
    """
    import traceback
    print(f"[500] {request.method} {request.url.path}", file=sys.stderr)
    traceback.print_exc()
    return JSONResponse(status_code=500,
                        content={"detail": "服务内部错误，详情见服务日志"})


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
