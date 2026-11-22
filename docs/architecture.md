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

---

# P1：PDF 读取（不依赖 poppler）

## 为什么不走 xref

真实漫画 PDF 的 xref 经常是坏的（增量更新、被工具改过、对象流混用）。
所以读者是**直接线性扫**文件里的 `N 0 obj`，遇到 stream 就按 /Length 跳过
（/Length 是间接引用或缺失时退回找 `endstream`）。

PDF 1.5+ 的对象塞在 /ObjStm 流里，文件里根本没有 `N 0 obj` 字样 ——
必须解压 ObjStm、读 /N /First、按「对象号 + 偏移」表切分，否则现代 PDF 一张图都找不到。

## 链式 Filter（真实数据的第一道坎）

```
/Filter [/FlateDecode /DCTDecode]
```

不是「两种编码冲突」，而是**zlib 里包着 JPEG**。按规范从数组第一个依次解码，
所以先剥掉传输层（Flate/LZW/ASCII），剩下的才是终端图像编码。

实测：`reg_bleach54.pdf` 203 页里 140 页是 `[/Flate /DCT]`、63 页是 `[/Flate /JPX]`，
不做链式剥皮就一张也提不出来。

## 零拷贝 PNG（P1 最大的性能赢面）

PDF 的 `Predictor 15` 和 PNG 的行滤波是**同一套规范**：每行前面一个 0~4 的 filter byte，
后面是像素差分。所以 PDF 的 Flate 流可以直接当成 PNG 的 IDAT 用：

```
PDF FlateDecode 流  ==  zlib(每行 filter byte + 差分像素)
PNG IDAT            ==  zlib(每行 filter byte + 差分像素)
                    ↑ 完全一样
```

前提：Predictor ≥ 10、Colors ∈ {1,3}、BitsPerComponent ∈ {1,2,4,8}，
且解压后每行 filter byte 都在 0~4（校验一次就行）。

实测（600×5301 长条图，9.5MB 像素）：

```
逐字节反滤波 + 重新压缩     4.08 秒/页
直接复用原始流当 IDAT       0.062 秒/页      快 66 倍
```

而且**输出体积反而更小**（18.2MB vs MuPDF 重压的 22.7MB）—— 因为 PDF 里
用的是 Paeth/Up 滤波，压缩率比「无滤波重压」高。

## CCITT G4：一个悄无声息的 bug

G4 解码用两张码表（白/黑 run 码）+ 一张模式码表。最初的写法是「先查终止码表，
失败再查 makeup 表」，看起来没问题，但 `_read_code` 是**破坏性读取** ——
查终止码失败那一次已经把比特吃掉了，后面全部错位。

症状：全白行能解（1 个 bit 就够），稍微复杂一点就 `run 码无法解析`。

修法：`_read_code` 改成 peek 式（匹配成功才推进读指针），并把终止码和 makeup 码
合并成一张表（顺带加了冲突断言）。

验证方式：自己写一个最小 G4 编码器（只用 H 模式和 V0）做往返测试，
11 组图案全过 —— 全白 / 全黑 / 竖条纹 / 棋盘 / 1px 细线 / 宽 37 非字节对齐 /
宽 2000 触发 makeup 码。

另一个坑：**一个 H 模式只携带两个 run（白 + 黑）**，一行要发多次 H。
把这个写成「一个 H 后面跟整行 run」时，解码器只读两个就去找下一个模式码，
后面全错位 —— 这个错误是往返测试抓出来的，光看代码看不出来。

## 判据三件套（照抄 ebook-forge 的实测结论）

```
碎片过滤    min(宽,高) < 200 的是扫描切边/空白条
覆盖率      按「有整页图的页数 / 总页数」，不是按图片张数（碎片会灌水）
蒙版占比    /ImageMask 超过半数 = 蒙版型 PDF，只能渲染
```

处置：覆盖率 100% 输出；90%~100% 和 <90% 都报错（缺页不静默少页）；
完全无图 = 文字版，直接拒绝；蒙版型报「需要渲染（P6）」。

## P1 实测数据（真实文件）

```
文件                        页数   耗时     输出      与独立渲染器比对
──────────────────────────────────────────────────────────────────────────
reg_bleach54.pdf (76MB)     203   0.78s   80.7MB   MuPDF 203/203 字节一致
reg_cangfeng.pdf (18MB)       7   0.42s   18.2MB   像素 7/7 一致（体积更小）
自造 PDF 回读                25   0.14s    —      25/25 字节一致
文字版 PDF                    1   0.01s     —      正确拒绝
蒙版型 PDF (104MB)          178   —         —      345/347 蒙版 → 正确拒绝
```

蒙版型那本顺带验证了 G4 解码器的正确性：取第 176 页的 stencil 对象
（4634×6918，32MP）解码，1 的占比 **58.3%**；MuPDF 渲染同一页的暗像素
**57.8%** —— 差 0.5 个百分点。这同时证明了三件事：

```
码表正确      否则解出来是乱码/死循环
P/V 模式正确   32MP 无报错，且像素分布对得上
反相处理正确   T.6 语义 1=黑、PNG 位深 1 是 0=黑，不取反就是整页颠倒
```

⚠️ 反面教训：`/ImageMask true` 的对象抽出来**不是页面**（蒙版语义 + 缺矢量文字），
所以判据里必须把它们挡掉并报「需要渲染」，不能硬塞进 CBZ。

## CCITT 验证的边界（诚实记录）

书库 1610 本 PDF 里只有 2 本用 CCITT，**两本都是蒙版型**（iTextSharp 脚本生成）：

```
file1  178 页  345/347 蒙版   非蒙版页仅 2
file2  186 页  359/362 蒙版   非蒙版页仅 3
```

所以真实数据验证覆盖到的是「蒙版页里的 G4 流解码正确」（32MP，黑占比 58.3% vs 渲染 57.8%），
**没有真实样本能验证「非蒙版 CCITT → 直接输出 CBZ」这条主路径** —— 那条只有
自造往返测试（H 模式 + V0，11 组图案）。

这不是隐瞒，是样本限制。等以后遇到非蒙版 CCITT 的 PDF（普通扫描件）再补测。
真要现在就验，只能自己造一个 CCITT 编码的 PDF，但我们的编码器只支持 H/V0，
造出来的样本覆盖不到真实扫描件的 P/VR/VL 组合 —— 用假样本「验证通过」反而更危险。

---

# P2：文字书四格式 + 7z 容器

## 文字书这条线在做什么

```
txt ⇄ fb2 ⇄ docx ⇄ epub        容器互转，正文/段落/插图都要留住
```

IR 里对应 `TextBook`（chapters → blocks[Paragraph|ImageBlock]），P0 就定义好了，
P2 只是把四个 Reader / Writer 接上去。

## 中文 txt 的两个真实坑

**① 编码**：国标 txt 是 GBK/GB18030，直接按 utf-8 读会整个乱码。
判定顺序：BOM → utf-8 → gb18030 → big5 → shift_jis → latin-1（兜底不丢字节）。

**② 分段方向**：中文小说**每段首行缩进 2 字符**，所以「缩进开头 = 新段落」，
「不缩进的续行 = 同一段被硬换行拆开」。我第一版写反了，两段被粘成一段。
英文 txt 恰好相反（无缩进、空行分段），所以这条必须按中文习惯来。

## epub 文字读写的两个坑

**① `<head><title>书名</title>` 不是正文**。HTMLParser 默认会把 title 里的文字
当数据吐出来，结果每章多出一段标题文字（实测 125 章差 774 字 = 0.24%）。
修法是整个 `<head>` 期间 skip。

**② epub → epub 必须先判断是漫画还是小说**。
早先没判断，走进漫画路径（`epub.read()` 只取图片），真实小说 126 章 31.7 万字
转出来只剩 6 章（6 张插图），**正文全丢**。现在的判据是 P0 那套
（页数 + 图片占体积比），小说走 `read_text`，漫画走 `read`。

## 7z 解析：五个坑，全是「多读一个字节」

7z 的头是位级紧凑编码，`number()` 是变长整数，错一个字节后面全乱。
实测调试顺序：

```
症状                          真因
──────────────────────────────────────────────────────────────────
7z 头意外结束                  _read_pack_info 里预先读了一个 size，
                              但 kSize/kCRC 后面跟的是列表不是长度前缀
7z UnPackInfo 结构异常         UnpackInfo 后面直接跟 kFolder，没有 count 字段
7z 数据被截断 （第 3 次）      kCRC 前面还有个 AllAreDefined 字节
未知类型 112 （鬼数字）        同上，AllAreDefined 没读导致错位
Corrupt input data             PackPos 是相对「签名头之后」（偏移 32），
                              不是相对文件开头
名字串行                       不能 raw.split(b"\x00\x00")：UTF-16LE 里
                              ASCII 字符是「字符+00」，两个字符之间就能凑出
                              00 00，必须按 2 字节步进
```

最后一个坑还有个变体：SubStreamsInfo 的 CRC 数量不是 `sum(count-1)`，
而是「总 substream 数，减去只有一个 stream 的 folder 数」（那种 folder 用
自己的 CRC，不单列）。

验证方式：用 py7zr 生成样本（LZMA2 / LZMA / Copy / BCJ+LZMA2 四种编码），
我们的解析器逐个解出来比对字节 —— 四种全过，中文文件名也对。
`tests/fixtures/tiny.cb7`（2.4KB）是固化的样本，测试不依赖 py7zr。

## P2 实测数据

```
项目                          结果
──────────────────────────────────────────────────────────
txt→fb2→txt                   段落文本 100% 一致
txt→docx→txt                  同上
txt→epub→txt                  同上
真实小说 epub（765KB）        125 章 / 316,155 字，0.26 秒
  → txt                       962KB  0.27 秒
  → fb2                      1222KB  0.29 秒
  → docx                      411KB  0.35 秒
  → epub                      719KB  0.34 秒
epub → epub 往返              125 章 / 316,155 字，逐章比对 0 处差异
生成的 epub 用 MuPDF 打开      页数/目录/中文断行都正常
CB7 → CBZ                    图片逐张字节一致
```

---

# P3：MOBI 读写

## MOBI 是什么

Palm 数据库：78 字节库头 + 记录头表 + 一堆记录。

```
记录 0    PalmDOC 头(16) + MOBI 头(232/264) + EXTH 元数据 + 全名
记录 1~N  正文，每条 ≤4096 字节，PalmDOC 算法压缩
记录 N+1~ 图片，用 HTML 里的 <img recindex="00001"> 引用
```

## 三个踩到的坑

**① PalmDOC 距离对的首字节范围**。0x80~0xBF 是「距离+长度」对，
0xC0~0xFF 是「空格 + 字符」编码。写压缩时把首字节写成 `0xC0 | ...`，
解压端按空格语义解 → 200 个空格压出来变成 `'@\x0f'` 这种垃圾。
正确是 `0x80 | ((pair >> 8) & 0x3F)`。

**② 闭包里的增强赋值**。`out += data[...]` 在嵌套函数里会被当成局部变量赋值，
直接 `UnboundLocalError: cannot access local variable 'out'`。用 `out.extend(...)`。

**③ 正文必须是完整 HTML 文档**。只写 `<h1>/<p>` 片段时，MuPDF 只解析出第一段
（实测「转出来只有标题」）。真机 mobi 的正文开头是
`<html><head><guide></guide></head><body>`，照抄它。

**④ PalmDB 库名塞不下中文**。那 32 字节是单字节编码，中文写 UTF-8 进去读出来是
`æªå½å`。真正的标题走 EXTH(503) + full name(UTF-8)，库名用 ASCII 兜底。

**⑤ header 字段偏移要减 16**。这些字段的偏移都相对「记录 0」，而 MOBI 头从记录 0
的 16 开始。写进 mobi 数组时不减，元数据全错。对照真实文件实测的值：

```
记录0 偏移  含义              实测值
──────────────────────────────────────
84          full name offset   732
88          full name length   23
92          locale             0x11
104         last content idx   5
108         first image idx    5
128         EXTH flags         0x850
```

## P3 实测数据

```
项目                              结果
──────────────────────────────────────────────────────────────
真实漫画 mobi（10.8MB / 64 图）    解析 0.03s，读出 64 张图 10.6MB
  → cbz                          64 页 / 10.6MB / 0.04s，图片 64/64 字节一致
  → pdf                          64 页 / 10.7MB / 0.07s
  → epub                         64 页 / 10.7MB / 0.06s
自造 mobi 写→读                   文字/章节/图片全对
生成的 mobi 用 MuPDF 打开          文字内容完整（图片 MuPDF 不渲染 mobi 图片，它自己读
                                 真实漫画 mobi 也是 0 张，所以这条不能当判据）
```

## P3 的边界

```
AZW3（KF8）      明确报错。碎片化 HTML + FDST/INDX 表，8 天工作量，
                 你库里 0 个 —— 按你之前的决定不做
DRM mobi         明确报错
HUFF/CDIC mobi   明确报错。库里 42 个 mobi 实测 compression=2（PalmDOC），
                 没有 HUFF 样本，实现它等于写 200 行没人验证的代码
MOBI 输出        用 compression=1（不压缩）。PalmDOC 压缩器已实现且往返测试通过，
                 但生成端不压缩更稳 —— 体积大一点，结构不会错
```
