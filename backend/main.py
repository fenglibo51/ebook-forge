import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# 将 backend 加入路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.routers import convert, library, history, settings
from backend.services.converter import check_calibre_available
from backend.config import BOOK_PATH, SUPPORTED_FORMATS, TEMP_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时检查
    calibre_ok = check_calibre_available()
    book_ok = Path(BOOK_PATH).exists()
    
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs("/data", exist_ok=True)
    
    print(f"[EbookForge] Calibre: {'✅' if calibre_ok else '❌'}")
    print(f"[EbookForge] Book path: {BOOK_PATH} ({'✅' if book_ok else '❌'})")
    print(f"[EbookForge] Formats: {', '.join(SUPPORTED_FORMATS.keys())}")
    
    yield


app = FastAPI(
    title="EbookForge",
    description="电子书格式转换工具",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(convert.router)
app.include_router(library.router)
app.include_router(history.router)
app.include_router(settings.router)


# 挂载前端静态文件
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return {"message": "EbookForge API is running", "docs": "/docs"}
