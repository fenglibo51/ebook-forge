"""EbookForge · 后端入口

    python3 -m backend.main            # 开发
    uvicorn backend.main:app --host 0.0.0.0 --port 8506

前端是纯静态文件，直接由这个进程挂在根路径，不需要 nginx。
"""

from __future__ import annotations

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


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    """别把 traceback 甩给前端，但日志里要有。"""
    import traceback
    traceback.print_exc()
    return JSONResponse(status_code=500,
                        content={"detail": f"{exc.__class__.__name__}: {exc}"})


app.include_router(router)

# 静态前端挂根路径（必须在 API 之后注册，否则 /api 会被吃掉）
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")


def main() -> None:
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8506, reload=False)


if __name__ == "__main__":
    main()
