# 📚 EbookForge

> 电子书格式互转 Web 工具 — 基于 Calibre `ebook-convert` 引擎，常见电子书格式互转，漫画压缩包也能处理。

## ✨ 功能

- 📖 **格式转换** — EPUB、MOBI、AZW3、PDF、TXT、DOCX、FB2 互转
- 🗜️ **漫画压缩包** — ZIP / CBZ 作为输入，可转 EPUB、PDF；也能把 ZIP 规范化为 CBZ
- ⬆️ **上传转换** — 上传文件 → 选格式 → 转换 → 下载
- 🗂️ **书库管理** — 浏览服务器上的书库目录，勾选批量转换

![格式转换页](assets/screenshots/convert-page.png)

![书库浏览页](assets/screenshots/library-page.png)

- 📊 **实时进度** — 进度条 + 日志，不刷屏
- 🕘 **历史记录** — 自动持久化，刷新不丢失（最多保留 500 条）
- 🧹 **临时文件管理** — 设置页查看占用大小，一键清理

![转换记录页](assets/screenshots/history-page.png)

![设置页](assets/screenshots/settings-page.png)

## 🛠️ 技术栈

- 🐍 **后端** — Python + FastAPI
- 📖 **转换引擎** — Calibre `ebook-convert`
- 🎨 **前端** — 纯 HTML / CSS / JS 单页应用（零构建）
- 🐳 **部署** — Docker + Docker Compose
- ⚖️ **协议** — GPLv3（因使用 Calibre 引擎）

## 🚀 快速开始

### 方式一：使用现成镜像（推荐）

在你希望存放数据的目录下建好三个子目录，然后启动：

```bash
mkdir -p books temp data
```

```yaml
# docker-compose.yaml
services:
  ebook-forge:
    image: fenglibo51/ebook-forge:latest
    container_name: ebook-forge
    ports:
      # 左边 8505 是对外访问端口，若被占用改成别的即可（如 "8600:8000"）
      - "8505:8000"
    volumes:
      # 电子书目录：要转换的书放这里（对应界面里的"书库"）
      - ./books:/Book:rw
      # 临时文件目录：上传中转 + 转换结果
      # 清空后，历史记录中的对应文件将无法重新下载
      - ./temp:/temp:rw
      # 持久化目录：转换历史 + 设置，删除会丢历史记录
      - ./data:/data
    environment:
      - BOOK_PATH=/Book
      - TEMP_DIR=/temp
      - CLEANUP_HOURS=24          # 临时文件保留小时数
    restart: unless-stopped
```

```bash
docker compose up -d
```

打开 `http://<你的机器IP>:8505` 即可使用 🎉

> ⚠️ 镜像目前只提供 `amd64`（x86_64）架构，ARM 设备（树莓派、Apple 芯片 Mac 等）需要自行构建。

### 方式二：从源码构建

```bash
git clone https://github.com/fenglibo51/ebook-forge.git
cd ebook-forge
mkdir -p books temp
docker compose up -d --build
```

源码版 `docker-compose.yaml` 用 `build: .`，把书库挂载路径按需改成你自己的目录即可。

## 💾 数据持久化

- 📚 `./books` — 电子书目录，映射到容器 `/Book`（界面里的「书库」）
- 🔄 `./temp` — 临时目录，映射到容器 `/temp`（上传中转、转换结果、下载）
- 💾 `./data` — 持久化目录，映射到容器 `/data`（转换历史 `convert_history.json`、设置 `settings.json`）

转换历史最多保留 500 条，超出后自动裁掉最旧的；临时文件默认保留 24 小时，可在设置页或通过 `CLEANUP_HOURS` 调整。

## 💻 本地开发

```bash
# 宿主机直接运行（需要本地已安装 Calibre）
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --port 8000
```

前端是零构建的纯静态页面，改完直接刷新即可；后端改动需要重启服务。

## 📄 License

GPLv3 — 因为使用了 Calibre 引擎。
