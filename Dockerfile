# EbookForge —— 纯自研电子书/漫画格式转换引擎 + Web 界面
#
# 引擎零外部依赖（不需要 Calibre / poppler / unar / p7zip），
# 只有 Web 层需要 fastapi / uvicorn / python-multipart。
#
#   docker build -t ebook-forge .
#   docker run -d -p 8506:8506 -v /volume1/Book:/Book -v ./data:/data ebook-forge

FROM python:3.12-slim

# 版本号：构建时从 git tag 注入，不在代码里写死。
# CI 打 tag 时传 build-args: VERSION=<tag 去掉 v>；
# 本地 docker build 不传就是 dev —— 本地构建永远不会冒充正式版。
ARG VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EBOOKFORGE_DATA=/data \
    EBOOKFORGE_VERSION=${VERSION}

WORKDIR /app

# Web 层依赖（国内 NAS 直连 pypi 常常超时，默认走清华源；要换就 --build-arg）
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

# 引擎 + 后端 + 前端
COPY bookforge/ ./bookforge/
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 中文字体：引擎只认 glyf 轮廓的 TrueType，所以不能装 fonts-noto-cjk（那是 CFF）
# 字体随镜像走，体积 +10MB，换来「开箱就能转中文 PDF」
COPY assets/fonts/NotoSansSC-Regular.ttf /usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf

# ⚠️ COPY 会保留源文件权限，而这些文件在宿主机上是 600/700 ——
# 不放开的话，切成非 root 用户后 /app 里的代码一律 Permission denied（实测踩过）。
RUN chmod -R a+rX /app
# 字体同样要放开：它是 COPY 进来的，默认 600 root，
# 非 root 读不到 → 健康检查能过，一转文字书就 Permission denied（实测踩过）
RUN chmod a+r /usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf

RUN mkdir -p /data/uploads && chown -R 10000:10000 /data

# 非 root 运行：与 NAS 上 data 目录的属主保持一致（uid 10000）。
# /Book 是 777，所以写转换结果不受影响；data 是 700 属主 10000，正好能写。
USER 10000:10000

VOLUME ["/data"]
EXPOSE 8506

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8506/api/health',timeout=4)"

CMD ["python3", "-m", "backend.main"]
