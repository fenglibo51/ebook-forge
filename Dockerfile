# EbookForge —— 纯自研电子书/漫画格式转换引擎 + Web 界面
#
# 引擎零外部依赖（不需要 Calibre / poppler / unar / p7zip），
# 只有 Web 层需要 fastapi / uvicorn / python-multipart。
#
#   docker build -t ebook-forge .
#   docker run -d -p 8506:8506 -v /volume1/Book:/Book -v ./data:/data ebook-forge

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EBOOKFORGE_DATA=/data

WORKDIR /app

# Web 层依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 引擎 + 后端 + 前端
COPY bookforge/ ./bookforge/
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 中文字体：引擎只认 glyf 轮廓的 TrueType，所以不能装 fonts-noto-cjk（那是 CFF）
# 字体随镜像走，体积 +10MB，换来「开箱就能转中文 PDF」
COPY assets/fonts/NotoSansSC-Regular.ttf /usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf

RUN mkdir -p /data/uploads

VOLUME ["/data"]
EXPOSE 8506

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8506/api/health',timeout=4)"

CMD ["python3", "-m", "backend.main"]
