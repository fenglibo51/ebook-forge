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


app = FastAPI(title="EbookForge", version="1.0.3", docs_url="/api/docs",
              lifespan=lifespan)


# ── 请求体硬上限 ──────────────────────────────────────────────────────────
# 为什么要有这一层：FastAPI 的 UploadFile 是 multipart 解析完才拿到的对象，
# Starlette 会把超过 1MB 的 part 落到 SpooledTemporaryFile。也就是说
# 「逐块读、逐块判额度」全都发生在磁盘已经被占之后 —— 反代没配 body 上限时，
# 一个几十 GB 的 POST 能把临时盘写满，应用层的配额检查根本来不及出场。
#
# 上限取「单次上传总量」再多留 64MB，给 multipart 边界和协议开销。

class BodyLimitMiddleware:
    """按路径设不同上限，在路由和落盘之前就挡。

    ⚠️ 超限时**不能从 receive() 抛异常**。实测过：FastAPI 的 routing.py
    把 body 解析阶段的任何非 HTTPException 一律转成
    `400 There was an error parsing the body`，状态码就不对了；而抛
    HTTPException 也没用 —— 这个中间件挂在 ExceptionMiddleware 外层，
    抛出去会变成 500。所以在 send 层把响应改写成 413。

    为什么不能用一个全局上限：管理端上传确实需要几个 GB，但分享访客的
    累计配额只有几百 MB —— 给访客开 8 GB 的 body 上限，等于让公网随便
    往这里灌 8 GB 才被业务层拒绝。所以按路径分开配。
    """

    def __init__(self, app, rules, default: int) -> None:
        self.app = app
        self.rules = list(rules)        # [(路径前缀, 字节上限)]，按顺序匹配，先中先得
        self.default = default

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.rules:
            if path.startswith(prefix):
                return limit
        return self.default

    def _payload(self, limit: int) -> bytes:
        return ('{"detail":"请求体太大，上限 %d MB"}' % (limit >> 20)).encode()

    async def _reject(self, send, limit: int) -> None:
        body = self._payload(limit)
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        limit = self.limit_for(scope.get("path", ""))
        declared: int | None = None
        for k, v in scope.get("headers") or ():
            if k.lower() == b"content-length":
                try:
                    declared = int(v)
                except (TypeError, ValueError):
                    declared = None
        # ① 声明就超标：一个字节的 body 都不收，直接回 413
        if declared is not None and declared > limit:
            return await self._reject(send, limit)

        received = 0
        too_large = False
        replied = False

        async def _receive():
            nonlocal received, too_large, replied
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body") or b"")
                if received > limit:
                    too_large = True
                    if not replied:
                        # 直接用原始 send 把 413 发出去，不等框架 ——
                        # 框架收到 disconnect 会自己抛 400，那就抢不到状态码了。
                        replied = True
                        await self._reject(send, limit)
                    # 告诉框架「对端没了」，它就会停手不再读 body
                    return {"type": "http.disconnect"}
            return msg

        async def _send(msg):
            nonlocal replied
            if not too_large:
                return await send(msg)
            # 超限：把框架原本要发的响应整个换掉，只发一次 413
            if replied:
                return
            replied = True
            if msg["type"] == "http.response.start":
                await self._reject(send, limit)
            elif msg["type"] == "http.response.body":
                await self._reject(send, limit)

        try:
            await self.app(scope, _receive, _send)
        except Exception:
            if not too_large:
                raise
            # 超限后下游多半因为 disconnect 抛异常（Starlette 抛
            # ClientDisconnect，FastAPI 转 400）。413 已经发出去了，
            # 再让它变成 500 没意义 —— 这里吞掉。
        # ② 框架因为 disconnect 什么都没发，我们补上 413
        if too_large and not replied:
            await self._reject(send, limit)

# ── 请求体上限：按路径分开配 ───────────────────────────────────────────────
# 管理端上传确实需要几个 GB；但分享访客的累计配额只有几百 MB，给他开 8 GB
# 的 body 上限等于让公网随便灌 8 GB 才被业务层拒。JSON 接口更不需要大 body。
# 注册放在文件末尾（admin_guard 之后）—— Starlette 后加的中间件在最外层，
# 需要它比 admin_guard 更外，否则框架自己抛的 400 会先占住响应。
_MB = 1024 * 1024

# 管理端上传：单次上传总量 + 64MB 的 multipart 开销
_ADMIN_UPLOAD_MB = int(os.environ.get("EBOOKFORGE_MAX_UPLOAD_MB", "8192")) + 64
# 分享上传：超过这条链接的累计配额就必然被拒，body 再大也没意义，多留 8MB 开销
_SHARE_UPLOAD_MB = int(os.environ.get("EBOOKFORGE_SHARE_QUOTA_MB", "512")) + 8
# JSON 接口（建任务、改设置这些）：4MB 绰绰有余
_JSON_MB = int(os.environ.get("EBOOKFORGE_MAX_JSON_MB", "4"))

_BODY_RULES = [
    ("/api/s/upload", _SHARE_UPLOAD_MB * _MB),     # 分享访客上传
    ("/api/upload", _ADMIN_UPLOAD_MB * _MB),       # 管理端上传
    ("/api/s/", _JSON_MB * _MB),                   # 分享端其余接口
    ("/api/", _JSON_MB * _MB),                     # 管理端其余接口
]
_BODY_DEFAULT = _JSON_MB * _MB                     # 静态资源只 GET，留个小的兜底


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


# 最后注册 → 最外层。见上面 BodyLimitMiddleware 的说明。
app.add_middleware(BodyLimitMiddleware, rules=_BODY_RULES, default=_BODY_DEFAULT)


if __name__ == "__main__":
    main()
