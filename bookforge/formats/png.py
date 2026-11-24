"""PNG 编解码（纯标准库 zlib）。

只做两件事，够用了：

1. probe()  读 IHDR，拿宽高/色彩类型，不解压
2. decode() 解成原始像素（8bit，支持 gray / rgb / gray+alpha / rgba / palette）
3. encode() 原始像素 → PNG

为什么需要解码：PDF 里嵌 PNG 最省事的做法是把 IDAT 原样搬过去，
配 /Predictor 15（PNG 行滤波和 PDF 预测器完全兼容），零重编码。
但带 tRNS 透明、交错、位深非 8 的 PNG 直接搬会坏，这时必须先解码再写。

不做：16bit、隔行、APNG、调色板 + tRNS。遇到明确报错，不静默降级。
"""

from __future__ import annotations

import struct
import zlib

from . import limits
from dataclasses import dataclass

PNG_SIG = b"\x89PNG\r\n\x1a\n"

# color type → 每像素通道数
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

# color type → PDF /ColorSpace，用于直通判断
COLORSPACE = {0: "DeviceGray", 2: "DeviceRGB", 4: "DeviceGray", 6: "DeviceRGB"}


class PNGError(ValueError):
    pass


@dataclass
class PNGInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    palette: bytes | None = None
    trns: bytes | None = None

    @property
    def channels(self) -> int:
        return _CHANNELS.get(self.color_type, 0)

    @property
    def passthrough_ok(self) -> bool:
        """能否把 IDAT 原样塞进 PDF（Predictor 15 直通）。

        条件：非调色板、非交错、8bit、无 tRNS（透明要 SMask，P0 不做）。
        """
        return (self.interlace == 0 and self.bit_depth == 8
                and self.color_type in (0, 2) and self.trns is None)

    @property
    def colorspace(self) -> str:
        return COLORSPACE.get(self.color_type, "")


def _chunks(data: bytes):
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PNGError("PNG 数据被截断")
        yield ctype, body
        pos += 12 + length


def probe(data: bytes) -> PNGInfo:
    """只读文件头，不解压。非 PNG 直接报错。"""
    if not data.startswith(PNG_SIG):
        raise PNGError("不是 PNG（签名不对）")
    ihdr = None
    palette = None
    trns = None
    for ctype, body in _chunks(data):
        if ctype == b"IHDR":
            if len(body) != 13:
                raise PNGError("IHDR 长度异常")
            w, h, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", body)
            if comp != 0 or filt != 0:
                raise PNGError("PNG 压缩/滤波方法异常")
            ihdr = (w, h, depth, color, inter)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            break
    if ihdr is None:
        raise PNGError("PNG 缺少 IHDR")
    w, h, depth, color, inter = ihdr
    return PNGInfo(w, h, depth, color, inter, palette, trns)


def idat_bytes(data: bytes) -> bytes:
    """把全部 IDAT 拼成一个连续的 zlib 流（PNG 允许分多个 IDAT）。"""
    out = bytearray()
    for ctype, body in _chunks(data):
        if ctype == b"IDAT":
            out += body
    if not out:
        raise PNGError("PNG 里没有 IDAT")
    return bytes(out)


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    """逐行反滤波（filter 0~4）。bpp = 每像素字节数。

    纯 Python 逐字节循环，所以这里做了内联优化：Paeth 不调函数、
    Up 用 zip 推导。实测 1920×1080 全 Paeth 从 4.4s 降到 1.5s 左右。
    调用方应优先走 IDAT 直通（不需要解码），这条路径只服务
    palette / 透明 / 非 8bit 的少数文件。
    """
    stride = width * bpp
    out = bytearray(stride * height)
    prev = bytes(stride)
    pos = 0
    for y in range(height):
        if pos >= len(raw):
            raise PNGError("PNG 像素数据不足（行数对不上）")
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) != stride:
            raise PNGError("PNG 像素数据不足（行长度对不上）")
        pos += stride
        if ft == 0:
            pass
        elif ft == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:      # Up
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ft == 3:      # Average
            for i in range(bpp):
                line[i] = (line[i] + (prev[i] >> 1)) & 0xFF
            for i in range(bpp, stride):
                line[i] = (line[i] + ((line[i - bpp] + prev[i]) >> 1)) & 0xFF
        elif ft == 4:      # Paeth
            for i in range(bpp):
                line[i] = (line[i] + prev[i]) & 0xFF
            for i in range(bpp, stride):
                a = line[i - bpp]
                b = prev[i]
                c = prev[i - bpp]
                p = a + b - c
                pa = p - a
                if pa < 0:
                    pa = -pa
                pb = p - b
                if pb < 0:
                    pb = -pb
                pc = p - c
                if pc < 0:
                    pc = -pc
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise PNGError(f"不认识的 PNG 行滤波类型 {ft}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def decode(data: bytes) -> tuple[bytes, PNGInfo, int]:
    """解成原始像素，返回 (pixels, info, channels)。

    只支持 8bit、非隔行；调色板会展开成 RGB(A)。
    """
    info = probe(data)
    if info.bit_depth != 8:
        raise PNGError(f"暂不支持 {info.bit_depth}bit PNG（只支持 8bit）")
    if info.interlace != 0:
        raise PNGError("暂不支持隔行扫描（Adam7）PNG")
    if info.color_type not in _CHANNELS:
        raise PNGError(f"不认识的 PNG 色彩类型 {info.color_type}")

    ch = _CHANNELS[info.color_type]
    # 按 IHDR 算这张图「应该」解出多少字节：每行 1 字节滤波标记 + width×ch。
    #
    # ⚠️ 但这个 expected 来自**上传文件自己声明的 IHDR**，完全不可信。
    # 拿它直接当 inflate 的上限，等于让文件自己给自己定预算 ——
    # 实测（2026-11-24 复审 P1-01）：全局限额压到 1MB 时，
    # 一个 26KB 的 PNG（声称 3000×3000）照样解出 25.7MB。
    # 所以先在 expected 上再卡一道全局预算，然后才解压。
    if info.width <= 0 or info.height <= 0:
        raise PNGError(f"PNG 尺寸非法：{info.width}×{info.height}")
    expected = info.height * (1 + info.width * ch)
    if expected > limits.MAX_INFLATE_BYTES:
        raise limits.LimitError(
            f"PNG 声称 {info.width}×{info.height}，要解出 "
            f"{expected / (1 << 20):.0f} MB，超过上限 "
            f"{limits.MAX_INFLATE_BYTES / (1 << 20):.0f} MB"
            "（BOOKFORGE_MAX_INFLATE_MB 可调）")
    raw = limits.inflate(idat_bytes(data), expected,
                         what=f"PNG 图像 {info.width}×{info.height} ")
    px = _unfilter(raw, info.width, info.height, ch)

    if info.color_type == 3:
        pal = info.palette
        if not pal or len(pal) % 3:
            raise PNGError("调色板 PNG 缺少有效 PLTE")
        n = len(pal) // 3
        triples = [pal[i * 3:i * 3 + 3] for i in range(n)]
        has_alpha = info.trns is not None
        out = bytearray()
        for idx in px:
            if idx >= n:
                raise PNGError("调色板索引越界")
            out += triples[idx]
            if has_alpha:
                a = info.trns[idx] if idx < len(info.trns) else 255
                out.append(a)
        return bytes(out), info, (4 if has_alpha else 3)

    return px, info, ch


def wrap_idat(idat: bytes, width: int, height: int,
              color_type: int, bit_depth: int) -> bytes:
    """直接用一段现成的 zlib 流当 PNG 的 IDAT 拼出完整文件。

    这是「PDF FlateDecode + Predictor 15 → PNG」的零拷贝路径：
    两边的行滤波规范完全一样（每行前面一个 0~4 的 filter byte），
    所以不需要解压、不需要反滤波、不需要重新压缩。

    前提（调用方必须先校验）：idat 是 zlib 流，且每行 filter byte 都在 0~4。
    """

    def chunk(t: bytes, b: bytes) -> bytes:
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


def idat_filters_ok(raw: bytes, rowbytes: int, height: int) -> bool:
    """校验解压后的数据每行开头都是合法 filter（0~4）—— 决定能不能直接当 IDAT 用。"""
    step = rowbytes + 1
    if len(raw) < step * height:
        return False
    for y in range(height):
        if raw[y * step] > 4:
            return False
    return True


def unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    """公开入口：PDF 预测器和 PNG 行滤波共用的反滤波实现。"""
    return _unfilter(raw, width, height, bpp)


def encode(pixels: bytes, width: int, height: int, channels: int = 3,
           bit_depth: int = 8) -> bytes:
    """原始像素 → PNG（filter 0，zlib 默认压缩）。

    bit_depth=8：pixels 是 8bit 像素（channels 1/2/3/4）
    bit_depth=1：pixels 是每行字节对齐的 1bit 位图（channels 只能是 1），
                 调用方负责给出 ((width+7)//8)*height 长度
    """
    if bit_depth == 1:
        if channels != 1:
            raise PNGError("1bit PNG 只支持单通道")
        rowbytes = (width + 7) // 8
        if len(pixels) < rowbytes * height:
            raise PNGError("1bit 像素数据长度不足")
        raw = bytearray()
        for y in range(height):
            raw.append(0)
            raw += pixels[y * rowbytes:(y + 1) * rowbytes]

        def ck1(t: bytes, b: bytes) -> bytes:
            return (struct.pack(">I", len(b)) + t + b
                    + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
        return (PNG_SIG + ck1(b"IHDR", ihdr)
                + ck1(b"IDAT", zlib.compress(bytes(raw), 9))
                + ck1(b"IEND", b""))

    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    stride = width * channels
    if len(pixels) != stride * height:
        raise PNGError("像素数据长度与宽高不匹配")

    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # filter: None
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (PNG_SIG + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def image_size(data: bytes) -> tuple[int, int, str]:
    """通用尺寸探测：返回 (宽, 高, 格式)。认 PNG / JPEG / JP2 / TIFF / GIF / WEBP。

    读不出来返回 (0, 0, '')，不抛异常 —— 尺寸只用于排版，不该阻塞转换。
    """
    try:
        if data.startswith(PNG_SIG):
            i = probe(data)
            return i.width, i.height, "png"
        if data[:2] == b"\xff\xd8":
            return (*_jpeg_size(data), "jpeg")
        # 脏头 JPEG：真实漫画包里常见（下载工具/打包器留下几十字节前缀），
        # SOI 不在偏移 0。往前 1KB 内找得到且后面有合法 SOF，就当 JPEG 处理。
        soi = data.find(b"\xff\xd8", 0, 1024)
        if soi > 0:
            w, h = _jpeg_size(data, soi)
            if w and h:
                return w, h, "jpeg"
        if data[:4] == b"\x00\x00\x00\x0cjP  " or data[4:8] == b"jP  ":
            return (*_jp2_size(data), "jp2")
        if data[:6] in (b"II*\x00", b"MM\x00*"):
            return (*_tiff_size(data), "tiff")
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return w, h, "gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return (*_webp_size(data), "webp")
    except Exception:
        pass
    return 0, 0, ""


def _jpeg_size(data: bytes, start: int = 0) -> tuple[int, int]:
    pos = start + 2
    n = len(data)
    while pos + 9 < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (seglen,) = struct.unpack(">H", data[pos + 2:pos + 4])
        # SOF0..SOF15（排除 DHT/JPG/DAC）
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
            return w, h
        pos += 2 + seglen
    return 0, 0


def _jp2_size(data: bytes) -> tuple[int, int]:
    pos = data.find(b"ihdr")
    if pos < 0 or pos + 12 > len(data):
        return 0, 0
    h, w = struct.unpack(">II", data[pos + 4:pos + 12])
    return w, h


def _tiff_size(data: bytes) -> tuple[int, int]:
    le = data[:2] == b"II"
    e = "<" if le else ">"
    (offset,) = struct.unpack(e + "I", data[4:8])
    (count,) = struct.unpack(e + "H", data[offset:offset + 2])
    w = h = 0
    for i in range(count):
        p = offset + 2 + i * 12
        tag, typ = struct.unpack(e + "HH", data[p:p + 4])
        if typ == 3:
            (val,) = struct.unpack(e + "H", data[p + 8:p + 10])
        elif typ == 4:
            (val,) = struct.unpack(e + "I", data[p + 8:p + 12])
        else:
            continue
        if tag == 256:
            w = val
        elif tag == 257:
            h = val
    return w, h


def _webp_size(data: bytes) -> tuple[int, int]:
    fmt = data[12:16]
    if fmt == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if fmt == b"VP8L":
        b = data[21:25]
        bits = int.from_bytes(b, "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if fmt == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return 0, 0
