<div align="center">
  <img src="docs/images/logo.png" alt="EbookForge" width="380">

  <br>

  **电子书 / 漫画格式转换，引擎是自己写的**

  不需要 Calibre · 不需要 poppler · 不需要 unar
  镜像 172 MB（Calibre 方案要 1.5 GB）

  [![Python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
  [![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
  ![依赖](https://img.shields.io/badge/外部依赖-0-brightgreen)
</div>

![格式转换](docs/images/convert.png)

---

## 📖 能转什么

| 源格式 | CBZ | ZIP | EPUB | PDF | TXT | FB2 | DOCX | MOBI |
|:---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **CBZ / ZIP** | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **CB7 / CBC**（7z） | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **PDF**（图片型） | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **EPUB**（漫画） | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **MOBI**（漫画） | ✅ | ✅ | ✅ | ✅ | ➖ | ➖ | ➖ | ➖ |
| **TXT** | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **FB2** | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **DOCX** | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **EPUB**（小说） | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **MOBI**（小说） | ➖ | ➖ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

💡 **EPUB / PDF / MOBI 是两栖的** —— 里面装的是漫画图片就走漫画那条路，装的是文字就走文字那条路，
工具会自己看。所以同一本 EPUB 既能转 CBZ，也能转 TXT。

界面上会按你选的源文件自动置灰不能用的目标格式，鼠标放上去能看到原因。

### 🚫 不支持的

| 格式 | 原因 |
|:---|:---|
| **AZW3 / KF8** | Kindle 的碎片化格式，工作量最大、收益最低 |
| **CBR**（RAR） | RAR5 算法闭源，没有纯 Python 实现 |
| **DRM 加密书 / 加密 PDF** | 打不开 |
| **脚注 / 多栏 / 图文环绕 / 竖排** | 排版只做到单栏 |

这些会直接报错告诉你原因，**不会悄悄降级，也不会偷偷转成有损**。

---

## ✨ 转换质量

### 🖼️ 图片：能无损就绝不重压

图片原样搬进新容器，不解码、不重采样、不重新压缩。

所以 `CBZ → PDF → CBZ` 这样来回转，图片逐字节一致 —— 有校验命令可以自己验：

```bash
python3 -m bookforge verify 原文件.cbz 转换后.pdf
```

### 📝 文字：真排版，不是截图

文字书转 PDF 用的是自己写的排版引擎：

- A5 / A4，边距 2cm，单栏
- 章节另起页、页码、首行缩进 2 字符、1.5 倍行距
- 中文按字断行、英文按词断行
- 句号逗号不落行首、开括号不挂行尾
- 行末标点自动压窄，中英文之间留半个空格
- 字体只嵌书里用到的那些字（31 万字的小说，字体只占几十 KB）

转出来的 PDF 中文可选中、可复制、可搜索。

### ⚠️ 有损的情况会明说

有些 PDF 的页面是「图片蒙版」结构（脚本生成的漫画常见），抽出来是反相的黑白图。
这种只能逐页拍成图片，**里面的文字会丢**。

工具会转，但结果一定标成琥珀色的「有损」并说明原因，不会混在成功里蒙过去。
也可以在设置里关掉这个兜底 —— 关掉后这类文件直接报错。

---

## 🚀 快速开始

### Docker（推荐）

```bash
git clone https://github.com/fenglibo51/ebook-forge.git
cd ebook-forge
docker compose up -d --build
```

打开 `http://<你的地址>:8505` 就能用。**开箱即用，不需要密码。**

**唯一要改的是书库路径。** 仓库里的 `docker-compose.yml` 就是下面这份：

```yaml
services:
  ebook-forge:
    build: .
    image: ebook-forge:latest
    container_name: ebook-forge
    restart: unless-stopped

    ports:
      - "8505:8506"          # 左=NAS 端口（反代指它）右=容器内，别动右边

    volumes:
      - /volume1/Book:/Book:rw    # ← 改成你的书库路径，必须 rw
      - ./data:/data              # 记录/设置/上传副本

    environment:
      EBOOKFORGE_DATA: /data
      TZ: Asia/Shanghai
      BOOKFORGE_RENDER_DPI: "200"          # 150 / 200 / 300，界面里也能改
      BOOKFORGE_RENDER_FALLBACK: "true"    # false = 关掉渲染兜底，这类 PDF 直接报错
      EBOOKFORGE_UPLOAD_TTL: "86400"       # 上传副本保留 24 小时

    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8506/api/health', timeout=4)"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
```

> ⚠️ `:/Book` 后面必须是 `rw`（可写）—— 转换结果默认写回源文件所在的目录，只读会失败。

不想用 git clone 也行，把上面这份存成 `docker-compose.yml`，
再把仓库里的 `Dockerfile`、`bookforge/`、`backend/`、`frontend/`、`assets/`、`requirements.txt` 和它放同一层，
`docker compose up -d --build` 一样能起来。

### 不用 Docker

```bash
pip install -r requirements.txt
python3 -m backend.main          # → http://localhost:8506
```

### 命令行

```bash
python3 -m bookforge convert 漫画.cbz pdf            # 输出在同目录
python3 -m bookforge convert 小说.epub pdf -o 输出.pdf
python3 -m bookforge convert 漫画.cbz pdf --fit page --page-size a5
python3 -m bookforge verify 原文件.cbz 转换后.pdf      # 逐张校验无损
```

当成库用：

```python
from bookforge import convert
r = convert("漫画.cbz", "pdf", dpi=300)
print(r["pages"], r["out"], r["mode"])   # mode: "" 无损 / "render" 有损
```

---

## 🖥️ 界面

四个页面：**格式转换 · 书库浏览 · 转换记录 · 设置**。侧边栏可收起，深色模式一键切换。

### 📚 书库浏览

直接在服务器上勾文件批量转，不用先把上百 MB 的文件传一遍再下回来。

格式筛选会看**文件夹内部有没有这个格式** —— 选 MOBI 之后留下的都是真有 MOBI 的目录。

![书库](docs/images/library.png)

### 📋 转换记录

每次转换都留底，有损的用 `!` 标出来，可以直接下载或重试。

![记录](docs/images/history.png)

### ⚙️ 设置

排版默认值、渲染精度、输出目录、分享模式、引擎能力一览。

![设置](docs/images/settings.png)

### 🌙 深色模式

侧边栏底部一键切换，没手动选过就跟随系统主题。

![深色](docs/images/dark.png)

---

## 🔐 访问控制

**开箱即用不需要密码。** 装好直接访问，四个页面都能用 —— 适合纯内网自用。

要给别人临时转格式时，去 **设置 → 分享模式** 打开开关。点一下就行：

- 自动生成一个随机密钥并弹窗显示（**记不记都行**，设置页随时能查回来）
- 后台接口（书库 / 记录 / 设置）开始要求密钥，浏览器输一次记住
- 转换页右上角出现「🔗 生成分享链接」

关掉开关会立刻恢复免密，**并且让所有已发出的分享链接失效**。

### 分享链接长什么样

一条 `你的域名/s/xxxxx` 的地址，24 小时自动失效。

```
拿到链接的人能看到          碰不到
──────────────────────────────────────────────────────────
一个干净的转换页（无侧边栏）   书库、转换记录、设置
上传自己的文件               服务器上的已有文件
选目标格式                   别人的任务和产物
进度和下载                   删任何文件
```

一条链接可以同时给几个人用，按浏览器会话互相隔离。对方上传的文件同样 24 小时后清掉。

![分享页](docs/images/share-page.png)

### 忘记密钥怎么办

进容器跑一条命令，直接关掉分享模式：

```bash
docker exec ebook-forge python3 -c "
from backend import store
s = store.load_settings(); s['share_enabled'] = False; store.save_settings(s)
print('已关闭分享模式')
"
```

### 资源配额

```
单文件        1024 MB     EBOOKFORGE_MAX_FILE_MB
单次上传总量   8192 MB     EBOOKFORGE_MAX_UPLOAD_MB
单次文件数      10         EBOOKFORGE_MAX_FILES_UPLOAD
单任务文件数    10         EBOOKFORGE_MAX_FILES_TASK
排队任务上限    10         EBOOKFORGE_MAX_QUEUE
```

压缩包在解压前会按头部声明的大小检查（总解压量 / 单条目 / 条目数），炸弹包直接拒绝。

---

## 📁 转换完的文件去哪了

| 情况 | 结果 |
|:---|:---|
| **默认** | 和源文件放同一个目录。`神经榜.mobi` → `神经榜.cbz` |
| **重名** | 自动加 `_1`、`_2`，**绝不覆盖原件**（同格式转换也是，不会写回自己） |
| **自定义目录** | 设置里可以指定统一输出位置，目录不存在会自动创建 |
| **设备上传的文件** | 先存到服务器临时目录，转完给「下载」按钮 |
| **删除源文件** | 只在书库页勾选文件时可选，默认不删，勾了会二次确认；**转换失败时永远不删** |
| **上传副本** | 保留 24 小时后自动清理，设置页可以看占用、可以手动清 |

---

## ⚠️ 已知限制

| 项目 | 说明 |
|:---|:---|
| **内存** | 整本读进内存处理。单本几个 GB 的合集会吃很多内存 |
| **渲染速度** | 蒙版 PDF 走渲染兜底时约 5 秒/页，162 页大约 15 分钟 |
| **进度粒度** | 进度按文件算，不是按页 |
| **字体** | 只支持 TrueType（`.ttf`），CFF / OTF 会报错 |
| **DOCX 输出** | 暂不支持嵌图，图片会跳过并在结果里告诉你数量 |

---

## 🛠️ 技术说明

引擎用「一个中间格式」的思路：所有源格式先读成统一的书模型，再从书模型写出目标格式。
所以增加一种新格式，只需要写一个读或写，不影响其他任何转换。

```text
bookforge/          转换引擎（零依赖，可以单独 import 当库用）
  formats/          CBZ / EPUB / 7z / MOBI / TXT / FB2 / DOCX / PNG
  pdf/              PDF 读写、字体嵌入、排版、渲染兜底
backend/            FastAPI 服务
frontend/           原生 HTML / CSS / JS，无框架无构建
tests/              100 项单元测试
```

```bash
python3 -m unittest discover -s tests
```

各格式的内部实现笔记和取舍记录在 [docs/architecture.md](docs/architecture.md)。

---

## 🙏 致谢

格式规范来自公开文档：PDF 32000-1、ECMA-376、EPUB 3、MOBI/KF8 reference。

字体使用 [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC)，SIL Open Font License。
