FROM python:3.12-slim

# 安装 Calibre（锁定版本 8.5.0，转换结果稳定可复现）
RUN apt-get update && apt-get install -y --no-install-recommends \
    calibre=8.5.0* \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

VOLUME /data

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
