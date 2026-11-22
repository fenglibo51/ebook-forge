# 架构与取舍

## 一句话

N 个源格式 × M 个目标格式，不写 N×M 条路径，写成 **N 个解析器 + M 个写入器**，
中间过一层 IR。

```
源容器 ──[read]──> IR ──[write]──> 目标容器
```

IR 只有两种：

```
ComicBook   pages: [Page{data, fmt, w, h}]   图片字节原样，不解码
TextBook    chapters: [Chapter{title, blocks:[Paragraph|Image]}]
            page_spec / metadata
```

P0 只实现了 ComicBook 的使用路径；TextBook 的骨架已放在 `bookforge/model.py`，
P4 做文字书排版时往上长。

## 为什么不用 Calibre

```
Calibre + Qt6 依赖                       约 410 MB
其中 QtWebEngine                         240 MB（只为「生成 PDF」而存在）
漫画包 → EPUB/PDF 的画质                  实测 600×5301 被压成 584×754，8 张图 0 张字节一致
```

不是「重」的问题，是**它会把漫画图片缩放重编码**。我们要的无损，它给不了。

## 无损是怎么做到的

```
JPEG      → /Filter /DCTDecode   body 就是原始 jpg 字节
JPEG2000  → /Filter /JPXDecode   原字节
PNG       → /Filter /FlateDecode + /DecodeParms <</Predictor 15>>
            body 就是 PNG 的 IDAT —— PNG 行滤波和 PDF 预测器是同一套规范
```

缩放一律走 PDF 的 `cm` 变换矩阵：只改显示尺寸，不动像素。所以「页面大小」和
「图片质量」是两件独立的事。

验证方式不是「看着没问题」，是逐张 md5：

```
源 5 张 / 目标 5 张 / 字节一致 5 张 → 无损
```

## 三个真实遇到的坑

### 1. PNG 的 ColorSpace 不能直接拼

`PNGInfo.colorspace` 返回 `DeviceRGB`（不带斜杠），拼进 PDF 要写成 `/ColorSpace /DeviceRGB`。
漏一个斜杠 → 提取器按 1 通道解析 → 数据长度对不上。测试里 37×23 的图给出 2479 字节
（期望 2553），根因就是这个。

### 2. 脏头 JPEG

真实漫画包存在 `FFD8` 不在偏移 0 的文件（第 1 页前面有 27 字节垃圾）。尺寸探测失败 →
PDF 里 `/Width` 写成 0 → MuPDF 宽容能渲染，但 `extract_image` 直接报
`image width is zero`。处置：探测时容忍前 1KB 内的前导垃圾，写 PDF 时尺寸兜底绝不为 0。

### 3. GBK 文件名

zip 条目名没有 UTF-8 flag 时，Python 按 cp437 解码，中文变成
`╟¼╝½╖¿╒≤╡┌╢■▓π`。不影响转换（写入时一律重编号成 `00001.jpg`），
但不能拿原文件名当标题，P2 再处理编码探测。

## 为什么 CBZ 永远不作为 Calibre 的输出（历史结论，仍然成立）

Calibre 有 `comic_input`（认 cbz/cbr/cb7/cbc）但**没有 comic_output**，
`ebook-convert x.cbz y.cbz` 会报 `No plugin to handle output format: cbz`。
所以 CBZ 只能自己拼 zip —— 这条经验直接推动了「自己写容器」的路线。

## 排版相关的设计（P4 前不实现，先把约束记下）

页面尺寸只在目标有固定版式时存在：

```
PDF / DOCX     有，必须给定
EPUB/MOBI/AZW3 无（reflowable），读者自己调
CBZ            有，= 图片本身尺寸
```

小说 → PDF 的档位：

```
L1  单栏分页 + 章节另起 + 页码 + 图按宽缩放      交付底线
L2  L1 + 中文禁则 / 标点挤压 / 首行缩进 / 脚注    能看的水平
```

默认版式（常规）：A5 148×210、小四 12pt、1.5 倍行距、首行缩进 2 字符。
EPUB / MOBI 是 reflowable，这组值只在转 PDF / DOCX 时生效。

## 没有做的事（明确写下，避免以后被当成 bug）

- 不给墨水屏做灰度转换 —— 有损且不可逆，阅读器自己会做。
- 不把纯文字 PDF 送进渲染器 —— 那是把小说拍成图片，不是用户要的。
- 不静默降级 —— 提取不到图就报错，绝不偷偷转成有损。
