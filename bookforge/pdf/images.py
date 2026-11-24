"""PDF 图像流 → 可落盘的图片字节。

四种编码，各自的路子：

    DCTDecode      原始流就是 JPEG        → 原字节，不重压
    JPXDecode      原始流就是 JPEG2000    → 原字节
    FlateDecode    解压 + 反预测          → 像素 → 我们自己的 PNG 编码器
    CCITTFaxDecode G4/G3 传真编码         → 1bit 位图 → PNG（位深 1）

不做的：LZW / RunLength / JBIG2 —— 明确报错，不静默转码。
"""

from __future__ import annotations

from ..formats import limits
import zlib

from ..formats import png
from . import ccitt
from .reader import ImageObj, PDFReadError

# /ColorSpace 名 → 组件数
_COMPONENTS = {
    "DeviceGray": 1, "CalGray": 1,
    "DeviceRGB": 3, "CalRGB": 3,
    "DeviceCMYK": 4,
}


def _components(img: ImageObj, doc=None) -> int:
    cs = img.colorspace
    if cs in _COMPONENTS:
        return _COMPONENTS[cs]
    if cs in ("ICCBased",):
        n = img.decode_parms.get("_icc_n")
        return int(n) if n else 3
    if cs == "Indexed":
        return 1
    if cs == "" and img.is_mask:
        return 1
    return 3


def _unpredict(data: bytes, colors: int, columns: int, bpc: int) -> bytes:
    """PNG 预测器（Predictor 10~15）反解 → 直接复用 png 模块里那份优化过的实现。

    PDF 的预测器和 PNG 行滤波是同一套规范（每行前面一个 filter byte），
    所以不需要第二份实现 —— 早期这里各写一遍，结果这份慢 4 倍。
    """
    stride_bpp = max(1, colors * bpc // 8)
    rows = (len(data) // ((columns * colors * bpc + 7) // 8 + 1)) if columns else 0
    if not rows:
        return data
    return png.unfilter(data, columns, rows, stride_bpp)


def to_image_bytes(img: ImageObj, icc_components: int = 3) -> tuple[bytes, str]:
    """→ (图片字节, 扩展名)。扩展名只会是 jpeg / jp2 / png。

    Filter 可能是**链式**的，真实漫画 PDF 里极常见：
        [/FlateDecode /DCTDecode]  → zlib 里包着 JPEG，剥掉 Flate 才是原图
        [/FlateDecode /JPXDecode]  → 同上，JPEG2000
    按 PDF 规范，解码时从数组第一个开始依次应用，所以先剥掉传输层编码
    （Flate/LZW/ASCII），剩下的才是终端图像编码。
    """
    f = [x for x in img.filters]
    stream = img.stream
    raw_stream = img.stream          # 未解压的原始 zlib 流，零拷贝路径要用它

    while f and f[0] in ("FlateDecode", "LZWDecode", "ASCIIHexDecode", "ASCII85Decode"):
        enc = f.pop(0)
        if enc == "FlateDecode":
            # 有上限解压：一张图的 Flate 流超过 256MB 就不正常（P1-01）
            stream = limits.inflate(stream, what="PDF 图片流 ")
        else:
            raise PDFReadError(f"暂不支持的传输层编码 {enc}")

    if f == ["DCTDecode"]:
        return stream, "jpeg"
    if f == ["JPXDecode"]:
        return stream, "jp2"

    if not f:
        raw = stream
        colors = icc_components if img.colorspace == "ICCBased" else _components(img)
        bpc = img.bpc or 8
        pred = img.decode_parms.get("Predictor", 1)
        cols = img.decode_parms.get("Columns") or img.width
        rows = img.decode_parms.get("Rows") or img.height

        # ── 零拷贝路径 ────────────────────────────────────────────────
        # PDF 的 Predictor 15 和 PNG 行滤波是同一套规范，所以 Flate 流可以
        # 原样当 PNG 的 IDAT：不解压、不反滤波、不重压。实测 9.5MB 一页
        # 从 4.1 秒降到 0.06 秒，而且是字节等价（不是「像素等价」）。
        if (pred and pred >= 10 and colors in (1, 3)
                and bpc in (1, 2, 4, 8) and cols and rows
                and img.filters[:1] == ["FlateDecode"]):
            probe = stream                # while 循环已解压，正好用来校验 filter byte
            rowbytes = (cols * colors * bpc + 7) // 8
            if png.idat_filters_ok(probe, rowbytes, rows):
                ct = {1: 0, 3: 2}[colors]
                return png.wrap_idat(raw_stream, cols, rows, ct, bpc), "png"

        if img.colorspace == "Indexed":
            raise PDFReadError("Indexed 调色板 PDF 图片暂不支持")
        if pred and pred >= 10:
            raw = _unpredict(raw, colors, cols, bpc)
        if bpc == 1:
            rowbytes = (img.width + 7) // 8
            bits = raw[:rowbytes * img.height]
            if len(bits) < rowbytes * img.height:
                bits = bits + bytes(rowbytes * img.height - len(bits))
            return png.encode(bits, img.width, img.height, 1, bit_depth=1), "png"
        if bpc != 8:
            raise PDFReadError(f"暂不支持 {bpc}bit 的裸位图")
        if colors == 4:
            # CMYK → RGB（PDF 的 CMYK 是减色，需要反相）
            rgb = bytearray()
            for i in range(0, len(raw) - 3, 4):
                c, m, y, k = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
                rgb += bytes((((255 - c) * (255 - k)) // 255,
                              ((255 - m) * (255 - k)) // 255,
                              ((255 - y) * (255 - k)) // 255))
            raw = bytes(rgb)
            colors = 3
        return png.encode(raw, img.width, img.height, colors), "png"

    if f == ["CCITTFaxDecode"]:
        k = img.decode_parms.get("K", -1)
        columns = img.decode_parms.get("Columns") or img.width
        rows = img.decode_parms.get("Rows") or img.height
        bits = (ccitt.decode_g4(stream, columns, rows) if k < 0
                else ccitt.decode_g3(stream, columns, rows, k == 0))
        # ⚠️ CCITT 解码出来 1 = 黑像素（T.4/T.6 的语义：白 run 先编码，黑 bit 置 1）。
        # PNG 的位深 1 灰度恰好相反：0 = 黑、1 = 白。不逐字节取反就是整页黑白颠倒 ——
        # 这个坑和 ebook-forge 里「stencil 蒙版抽出来是反相」是同一类。
        bits = bytes(b ^ 0xFF for b in bits)
        return png.encode(bits, columns, rows, 1, bit_depth=1), "png"

    raise PDFReadError(f"暂不支持的图像编码 {f}")
