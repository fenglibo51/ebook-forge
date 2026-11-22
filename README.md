# BookForge

电子书 / 漫画格式转换引擎。**纯 Python 标准库自研**，不含 Calibre、不依赖 poppler / unar，
不调用任何外部二进制。

> 当前进度：P0（漫画链）已完成，文字书链路在 P1~P6。

## 现在能做什么

```
CBZ / ZIP  →  CBZ / ZIP / EPUB / PDF
EPUB(漫画) →  CBZ / EPUB / PDF
```

图片一律**原字节直嵌**：JPEG 走 DCTDecode、JPEG2000 走 JPXDecode、PNG 走 FlateDecode
+ Predictor 15，不解码、不重采样、不二次压缩。

## 怎么用

```bash
# 漫画包 → PDF（默认 300dpi 换算页面，图片不缩放）
python3 -m bookforge convert 漫画.cbz pdf

# 漫画包 → EPUB
python3 -m bookforge convert 漫画.cbz epub -o 输出.epub

# epub 漫画 → cbz
python3 -m bookforge convert 漫画.epub cbz

# 图片等比缩进 A5 页面（不裁剪、不改像素）
python3 -m bookforge convert 漫画.cbz pdf --fit page --page-size a5
```

作为库用：

```python
from bookforge import convert
r = convert("漫画.cbz", "pdf", dpi=300)
print(r["pages"], r["out"])
```

## 验证是不是真无损

```bash
python3 scripts/verify_lossless.py 源.cbz 目标.pdf
```

输出逐张 md5 比对结果。**只有「字节一致 N/N」才算无损**，页数对不上直接判失败。

PDF 只比对 DCTDecode / JPXDecode（原样嵌入的 JPEG / JPEG2000）；
PNG 直通走像素级比对。

## 依赖

无。Python ≥ 3.11，不用 pip install 任何东西。

跑测试：

```bash
python3 -m unittest discover -s tests
```

## 已知限制（P0）

```
CBR / CB7 / CBC      还没接（P2 做 7z 解析；RAR 闭源，永久不支持）
MOBI / AZW3 / TXT    还没做（P1~P3）
带透明通道的 PNG      能进 PDF，但会拆成 SMask，字节不再一致（像素无损）
                      解码走纯 Python，约 1.5 秒/页（JPEG 走直通，0.01 秒/页）
TIFF / WEBP / GIF    明确报错，不静默转码
GBK 文件名的 zip      条目名会显示成乱码（不影响转换，因为写入时重编号）
```

防炸：单页解压超 256MB、整包解压超 4GB 直接拒（`BOOKFORGE_MAX_PAGE_MB` /
`BOOKFORGE_MAX_TOTAL_MB` 可调）。用压缩包里声明的大小预判，不解压就能拦住。
`zip → cbz` 是容器等价复制，不解压，百 GB 的包也照过。

⚠️ 所有页一次性读进内存（25 页 / 29MB 实测峰值 152MB）。500 页以上的大包会撞上限，
流式处理排在 P1。

## 路线图

```
P0  漫画链无损        IR + cbz/epub + png + pdf/writer         已完成
P1  PDF → CBZ         pdf/reader + images + ccitt，摆脱 poppler
P2  容器扩展          sevenz + fb2 + txt + docx
P3  Kindle            mobi 读 + 写
P4  文字书 → PDF      book/model + css + layout(L1) + fonts
P5  中文排版          layout 升 L2（禁则、标点挤压、脚注）
P6  渲染兜底          极简渲染器（蒙版/矢量 PDF，有损）
```

细节与取舍见 `docs/architecture.md`。
