# BookForge

电子书 / 漫画格式转换引擎。**纯 Python 标准库自研**，不含 Calibre、不依赖 poppler / unar，
不调用任何外部二进制。

> 当前进度：P0（漫画链）已完成，文字书链路在 P1~P6。

## 现在能做什么

```
漫画链
  CBZ / ZIP              →  CBZ / ZIP / EPUB / PDF
  CB7 / CBC（7z）        →  CBZ / EPUB / PDF
  EPUB(漫画)             →  CBZ / EPUB / PDF
  PDF(图片型漫画)        →  CBZ / EPUB / PDF
  MOBI(漫画)             →  CBZ / EPUB / PDF

文字链
  TXT / FB2 / DOCX / EPUB(小说) / MOBI(小说)
      →  TXT / FB2 / DOCX / EPUB / MOBI / PDF
```

PDF 输出分两条路，互不干扰：

```
漫画 → PDF    图片原字节直嵌（DCTDecode / JPXDecode / FlateDecode + Predictor 15）
文字 → PDF    书排版引擎（L1：单栏分页 / 章节另起页 / 页码 / 首行缩进 2 字符）
              + TrueType 子集化嵌入（10MB 字体按书用到的字裁成几十 KB）
```

图片一律**原字节直嵌**：JPEG 走 DCTDecode、JPEG2000 走 JPXDecode、PNG 走 FlateDecode
+ Predictor 15，不解码、不重采样、不二次压缩。

PDF 输出也一样：PDF 里的 Flate 流可以直接当 PNG 的 IDAT（两边行滤波规范相同），
所以 PDF → CBZ 也是零解码零重压。

**不依赖任何外部二进制** —— 没有 poppler、没有 Calibre、没有 unar、没有 p7zip、没有 kindlegen。

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

## 已知限制

```
CBR（RAR）            永久不支持：RAR5 算法闭源，纯 Python 无实现
AZW3（KF8）           不支持：碎片化 HTML + FDST/INDX 表还没做，明确报错
MOBI + DRM            明确报错（有 DRM 的 mobi 无法解）
MOBI + HUFF/CDIC      明确报错（只做 PalmDOC 压缩；实测库里 42 个 mobi 全是 PalmDOC）
MOBI 输出             不压缩（compression=1），体积比原版大，但结构最稳
带透明通道的 PNG      能进 PDF，但会拆成 SMask，字节不再一致（像素无损）
TIFF / WEBP / GIF     明确报错，不静默转码
GBK 文件名的 zip      条目名会显示成乱码（不影响转换，因为写入时重编号）
DOCX 输出             暂不支持嵌图（会跳过并提示数量）
```

文字 → PDF 的排版是 L2：

```
有     单栏分页 / 章节另起页 / 页码 / 首行缩进 2 字符 / 图按可用宽度缩放
      行首禁则（。，、）」等不落行首，拉回上一行）
      行尾禁则（（「《 不挂行尾，推到下一行）
      标点挤压（行末/连续标点压到半角）
      中英混排间距（1/4 em）
      孤行控制（标题后至少跟 2 行）
      中文按字符断行、英文按词断行
没有   脚注、多栏、图文环绕
字体   只用 TrueType(glyf)；CFF/OTF 明确报错。默认找 ~/.fonts/NotoSansSC-Regular.ttf
```

PDF 作为源时：

```
蒙版型 PDF            页面是 /ImageMask 结构（典型：iTextSharp 脚本生成）
                      抽出来是反相的黑白图，不是页面
                      → 走渲染兜底（有损），mode=render，note 写明原因
覆盖率不足            有页提不出整页图 → 同样走渲染兜底
Indexed 调色板 PDF    暂不支持（P2 补）
JBIG2 编码的 PDF      暂不支持（P2 补）
加密 PDF              明确报错
```

渲染兜底（P6）的边界：

```
支持   q/Q、cm、Do（图片和 Form）、re/m/l/c/h + f/S、rg/g/k、w、ImageMask
不做   文字（BT/Tf/Tj 全部跳过）—— 没有字形轮廓光栅化
      渐变、透明度、混合模式、图案、内联图像、JPEG/JP2 图片
代价   约 5 秒/页（纯 Python）。162 页的书约 15 分钟，poppler 是 8 分钟
开关   BOOKFORGE_RENDER_DPI（默认 200，可选 150/300）
      BOOKFORGE_RENDER_FALLBACK=false 时彻底禁用 → 这类 PDF 直接报错
```

防炸：单页解压超 256MB、整包解压超 4GB 直接拒（`BOOKFORGE_MAX_PAGE_MB` /
`BOOKFORGE_MAX_TOTAL_MB` 可调）。用压缩包里声明的大小预判，不解压就能拦住。
`zip → cbz` 是容器等价复制，不解压，百 GB 的包也照过。

⚠️ CBZ / EPUB 作为源时所有页一次性读进内存（25 页 / 29MB 实测峰值 152MB）。
500 页以上的大包会撞上限，流式处理排在 P2。
PDF 作为源不受此限 —— 解析是流式的，203 页 / 76MB 实测 0.8 秒、内存 80MB。

## 路线图

```
P0  漫画链无损        IR + cbz/epub + png + pdf/writer         已完成
P1  PDF → CBZ         pdf/reader + images + ccitt              已完成
P2  容器扩展          sevenz + fb2 + txt + docx
P3  Kindle            mobi 读 + 写
P4  文字书 → PDF      book/model + css + layout(L1) + fonts
P5  中文排版          layout 升 L2（禁则、标点挤压、脚注）
P6  渲染兜底          极简渲染器（蒙版/矢量 PDF，有损）
```

细节与取舍见 `docs/architecture.md`。
