"""EbookForge · 后端入口

    python3 -m backend.main            # 开发
    uvicorn backend.main:app --host 0.0.0.0 --port 8506

前端是纯静态文件，直接由这个进程挂在根路径，不需要 nginx。
"""

from __future__ import annotations

import asyncio
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

    # 之后按小时自己清 —— NAS 上的实例可能几周不转换一次，
    # 光靠「启动时清 + 建任务时清」会让上传副本一直占着盘。
    # 间隔用 EBOOKFORGE_CACHE_INTERVAL 调（秒），设 0 关掉。
    interval = int(os.environ.get("EBOOKFORGE_CACHE_INTERVAL", "3600"))

    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(interval)
            try:
                # 扫目录是阻塞操作，丢线程里跑，别卡住正在处理的请求
                r = await asyncio.to_thread(cleanup_uploads)
                if r["removed"]:
                    print(f"[cache] 定时清理：{r['removed']} 个过期上传副本，"
                          f"释放 {r['freed'] / 1048576:.1f} MB")
            except Exception as e:
                print(f"[cache] 定时清理失败：{e}")

    task = asyncio.create_task(_periodic_cleanup()) if interval > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="EbookForge", version="1.0.1", docs_url="/api/docs",
              lifespan=lifespan)


# ── 管理令牌 ──────────────────────────────────────────────────────────────
# 设了 EBOOKFORGE_ADMIN_TOKEN 之后，/api/* 都要令牌，只有这两类豁免：
#   /api/s/*        分享链接（访客用）
#   /api/health, /api/capabilities  不敏感的只读信息
# 不设就完全不拦（内网自用）。
#
# 令牌只认两处：X-Admin-Token 头、ebf_admin cookie。
# 曾经支持 ?k=<token>，已移除 —— URL 会进浏览器历史、反代 access log、
# 分享出去的截图，等于把管理权限写在明面上。
# 为什么要 cookie：下载是 window.location 跳转，带不了自定义头。

_EXEMPT_PREFIX = ("/api/s/",)
_EXEMPT_PATH = ("/api/health", "/api/capabilities")

# 令牌来源，优先级：
#   ① 环境变量 EBOOKFORGE_ADMIN_TOKEN（部署时固定，最高优先）
#   ② 设置页里打开「分享模式」后自动生成的那个
# 两个都没有 = 不拦（开箱即用、纯内网自用）
# 哨兵：配置读不出来时用它，守卫会返回 503 而不是放行
_FAIL_CLOSED = "\x00fail-closed\x00"


def _active_admin_token() -> str:
    """当前生效的管理令牌。空字符串 = 不拦；_FAIL_CLOSED = 拒绝一切。

    不缓存：settings.json 只有两百多字节，读一次不到 1 毫秒；
    而加了 1 秒缓存之后，在设置页刚打开分享模式会有一秒钟「没生效」的窗口
    （实测踩过：toggle 完立刻请求还是 200）。

    ⚠️ fail closed：早先「读设置出错就返回空字符串」等于把认证关掉 ——
    settings.json 损坏时，本来开着分享模式（有令牌保护）的服务会退化成免认证。
    实测确认过。现在区分：
      文件不存在 → 首次安装，默认无令牌（正常）
      文件损坏/IO 失败 → 返回哨兵，守卫拒绝所有受保护请求
    """
    env_tok = os.environ.get("EBOOKFORGE_ADMIN_TOKEN", "").strip()
    if env_tok:
        return env_tok                   # 环境变量优先，不受设置文件影响
    try:
        from . import store
        s = store.load_settings(strict=True)
        return str(s.get("admin_token", "")).strip() if s.get("share_enabled") else ""
    except Exception as e:
        print(f"[auth] 读取 settings.json 失败，按 fail-closed 处理：{e}", file=sys.stderr)
        return _FAIL_CLOSED


@app.middleware("http")
async def admin_guard(request, call_next):
    path = request.url.path

    # 豁免判断必须排在 fail-closed 前面：
    # 否则 settings.json 一坏，/api/health 也 503，Docker 会判定容器不健康反复重启，
    # 连「去看看出了什么事」的入口都没了。下面这些本来就不返回敏感数据。
    if not path.startswith("/api/"):
        return await call_next(request)
    if path.startswith(_EXEMPT_PREFIX) or path in _EXEMPT_PATH:
        return await call_next(request)

    token = _active_admin_token()
    if token == _FAIL_CLOSED:
        return JSONResponse(
            status_code=503,
            content={"detail": "服务配置读取失败，为安全起见暂时拒绝所有请求。"
                               "请检查 data/settings.json 是否损坏或不可读。",
                     "config_error": True})
    if not token:
        return await call_next(request)

    tok = (request.headers.get("X-Admin-Token")
           or request.cookies.get("ebf_admin")
           or ""
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
