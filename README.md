# EbookForge

电子书 / 漫画格式转换工具。**转换引擎完全自研，纯 Python 标准库实现** ——
不需要 Calibre、不需要 poppler、不需要 unar、不调用任何外部二进制。

装好就能跑，镜像 172 MB（对比 Calibre 方案的 1.5 GB）。

![格式转换](docs/images/convert.png)

---

## 能转什么

行是源格式，列是目标格式。`●` 支持　`—` 不适用

```
源                 CBZ   ZIP   EPUB   PDF   TXT   FB2   DOCX  MOBI
─────────────────────────────────────────────────────────────────────
CBZ / ZIP           ●     ●     ●      ●     —     —     —     —
CB7 / CBC（7z）     ●     ●     ●      ●     —     —     —     —
PDF（图片型）        ●     ●     ●      ●     —     —     —     —
EPUB（漫画）         ●     ●     ●      ●     —     —     —     —
MOBI（漫画）         ●     ●     ●      ●     —     —     —     —
TXT                 —     —     ●      ●     ●     ●     ●     ●
FB2                 —     —     ●      ●     ●     ●     ●     ●
DOCX                —     —     ●      ●     ●     ●     ●     ●
EPUB（小说）         —     —     ●      ●     ●     ●     ●     ●
MOBI（小说）         —     —     ●      ●     ●     ●     ●     ●
```

**EPUB / PDF / MOBI 是两栖的**：里面装的是漫画图片就走漫画那条路，装的是文字就走文字那条路，
工具会自己判断。所以同一本 EPUB 既能转成 CBZ，也能转成 TXT。

界面上会按你选的源文件类型自动置灰不可用的目标格式，鼠标悬停给出原因。

### 明确不支持

```
AZW3 / KF8     Kindle 的碎片化 HTML + FDST/INDX 索引表，成本最高收益最低
CBR（RAR）     RAR5 算法闭源，没有纯 Python 实现
DRM 加密的电子书、加密 PDF
HUFF/CDIC 压缩的 MOBI
脚注、多栏、图文环绕、竖排排版
```

遇到这些会直接报错说明原因，不会悄悄降级、也不会偷偷转成有损。

---

## 转换质量

### 图片：能无损就绝不重压

图片一律**原字节直嵌**，不解码、不重采样、不二次压缩：

```
JPEG        →  PDF 的 DCTDecode 流（原样搬运）
JPEG2000    →  PDF 的 JPXDecode 流
PNG         →  PDF 的 FlateDecode + Predictor 15
PDF 里的流  →  PNG 的 IDAT（两边行滤波规范相同，直接复用）
```

所以 `CBZ → PDF → CBZ` 这样的往返，图片逐字节一致（有校验脚本，见下）。

### 文字：有排版引擎

文字书转 PDF 不是把字拍成图片，是真的排版：

```
页面      A5 / A4，边距 2cm
版式      单栏、章节另起页、页码、首行缩进 2 字符、1.5 倍行距
断行      中文按字符断、英文按词断
禁则      句号逗号不落行首、开括号不挂行尾
标点挤压  行末标点压到半角，还超就整行微缩（≤8%）
中英间距  汉字和英文之间留 1/4 个字宽
字体      TrueType 子集化嵌入 —— 10 MB 字体按书里用到的字裁成几十 KB
```

一本 31 万字的小说转出来约 700 页、3 MB，中文可选中可复制可搜索。

### 有损的情况会明确告诉你

有些 PDF 的页面是「图像蒙版」结构（常见于脚本生成的漫画），抽出来是反相的黑白图，不是页面。
这种只能逐页渲染成图片，**文字部分会丢**。

工具会走渲染兜底，但结果里一定标记为「有损」并说明原因，醒目的琥珀色警告，不会混在成功里。
也可以在设置里关掉兜底 —— 关掉后这类文件直接报错。

---

## 快速开始

### Docker（推荐）

```bash
git clone https://github.com/fenglibo51/ebook-forge.git
cd ebook-forge
docker compose up -d --build
```

打开 `http://<你的地址>:8505`。

compose 里要改的只有书库路径：

```yaml
volumes:
  - /你的/书库路径:/Book:rw     # 要 rw：转换结果默认写回源文件同目录
  - ./data:/data
```

### 源码运行

```bash
pip install -r requirements.txt
python3 -m backend.main          # → http://localhost:8506
```

### 命令行

```bash
python3 -m bookforge convert 漫画.cbz pdf              # 自动放在同目录
python3 -m bookforge convert 小说.epub pdf -o 输出.pdf
python3 -m bookforge convert 漫画.cbz pdf --fit page --page-size a5
python3 -m bookforge verify 原文件.cbz 转换后.pdf        # 逐张校验无损
```

当库用：

```python
from bookforge import convert
r = convert("漫画.cbz", "pdf", dpi=300)
print(r["pages"], r["out"], r["mode"])     # mode: "" 无损 / "render" 有损
```

---

## 界面

四个页面：格式转换、书库浏览、转换记录、设置。左侧栏可收起（收起后只剩图标）。

**书库浏览** —— 直接在服务器上勾文件批量转，不用先把上百 MB 的文件传一遍。
格式筛选能识别「文件夹内部有没有该格式」，选 MOBI 之后留下的都是真有 MOBI 的目录。

![书库](docs/images/library.png)

**转换记录** —— 每次转换都留底，有损的用 `!` 标出来。

![记录](docs/images/history.png)

**深色模式** —— 侧边栏底部一键切换，跟随系统主题。

![深色](docs/images/dark.png)

---

## 输出与文件

```
默认      转换结果和源文件放在同一个目录
          例：神经榜.mobi → 神经榜.cbz（同目录）
          重名自动加 _1、_2，绝不覆盖原件

自定义    设置里可以指定统一输出目录，不存在会自动创建

删除源文件  只在书库页勾选文件时可选，默认不删，勾了会二次确认
            转换失败时永远不删源文件
            从设备上传的文件不会删（服务器上只是副本）

上传副本   保留 24 小时后自动清理，设置页可看占用、可手动清
```

---

## 已知限制

```
内存        整本读进内存处理。单本几个 GB 的合集会很吃内存
渲染速度    蒙版 PDF 走渲染兜底时约 5 秒/页（纯 Python），162 页约 15 分钟
进度粒度    转换进度按文件算，不是按页 —— 引擎内部没有页级回调
字体        只支持 TrueType（.ttf）。CFF/OTF 字体会报错
DOCX 输出   暂不支持嵌图，图片会跳过并在结果里报数量
```

---

## 技术说明

引擎用「中间表示」架构：N 个源格式解析成统一的书模型，再从书模型写 M 个目标格式。
所以 N×M 条转换路径只需要写 N+M 个模块，新增一个格式不用改其他任何地方。

各格式的内部实现笔记、遇到的具体问题和取舍，在 [docs/architecture.md](docs/architecture.md)。

```
bookforge/          转换引擎（零外部依赖，可独立当库用）
  formats/          CBZ / EPUB / 7z / MOBI / TXT / FB2 / DOCX / PNG
  pdf/              PDF 读写、CCITT 解码、字体子集化、排版、渲染兜底
backend/            FastAPI 服务
frontend/           原生 HTML / CSS / JS（无框架、无构建步骤）
tests/              100 项单元测试
```

跑测试：

```bash
python3 -m unittest discover -s tests
```

---

## 致谢

这些格式规范全部来自公开文档：PDF 32000-1、ECMA-376、EPUB 3、KF8 reference。
字体使用 [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC)（SIL Open Font License）。
