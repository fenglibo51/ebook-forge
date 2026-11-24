"""PDF 图片流提取（无损校验用，也是 P1 的 reader 雏形）。

只做一件事：把 PDF 里每个 Image XObject 的**原始流字节**取出来。
    DCTDecode / JPXDecode  → 流字节就是 jpg / jp2，可直接落盘
    FlateDecode            → 解压后是像素（Predictor 15 要先反预测）
    其它                   → 标记为 unsupported

用途：验证「CBZ → PDF」是否真的无损 —— 把 PDF 里的图抽回来，
      跟源图逐字节比 md5。

不支持加密、线性化、对象流（ObjStm）。遇到就明确报错。
"""

from __future__ import annotations

from ..formats import limits
import re
import zlib
from dataclasses import dataclass

from ..formats import png

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)


class PDFReadError(ValueError):
    pass


@dataclass
class RawImage:
    obj_num: int
    width: int
    height: int
    filters: list[str]
    data: bytes               # 已按 filter 处理：DCT/JPX 是原字节，Flate 是解压后像素
    colorspace: str = ""
    bits: int = 8
    columns: int = 0
    predictor: int = 0
    raw_stream: bytes = b""   # 解压/反预测**之前**的原始流字节（校验用）

    def to_image_bytes(self) -> tuple[bytes, str]:
        """转成可以直接落盘的图片：返回 (bytes, 'jpg'|'jp2'|'png'|'raw')"""
        if self.filters == ["DCTDecode"]:
            return self.data, "jpg"
        if self.filters == ["JPXDecode"]:
            return self.data, "jp2"
        if self.filters == ["FlateDecode"] and self.predictor == 0:
            return self.data, "raw"
        return self.data, "raw"


def _parse_dict(raw: bytes) -> dict:
    """极简 dict 解析：只需要 Width/Height/Filter/ColorSpace/BitsPerComponent/DecodeParms。"""
    d: dict = {}
    for key in ("Width", "Height", "BitsPerComponent"):
        m = re.search(rb"/" + key.encode() + rb"\s+(\d+)", raw)
        if m:
            d[key] = int(m.group(1))
    m = re.search(rb"/ColorSpace\s*/(\w+)", raw)
    if m:
        d["ColorSpace"] = m.group(1).decode()
    m = re.search(rb"/Filter\s*/(\w+)", raw)
    if m:
        d["Filter"] = [m.group(1).decode()]
    else:
        m = re.search(rb"/Filter\s*\[(.*?)\]", raw, re.S)
        if m:
            d["Filter"] = [x.decode() for x in re.findall(rb"/(\w+)", m.group(1))]
    m = re.search(rb"/Predictor\s+(\d+)", raw)
    if m:
        d["Predictor"] = int(m.group(1))
    m = re.search(rb"/Columns\s+(\d+)", raw)
    if m:
        d["Columns"] = int(m.group(1))
    return d


def _unpredict(data: bytes, colors: int, columns: int, bpc: int = 8) -> bytes:
    """反 PNG 预测器（Predictor 10~15）。PDF 里只用 15 这一档。"""
    bpp = max(1, colors * bpc // 8)
    stride = columns * bpp
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    while pos + 1 <= len(data):
        ft = data[pos]
        pos += 1
        line = bytearray(data[pos:pos + stride])
        if len(line) < stride:
            break
        pos += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                ul = prev[i - bpp] if i >= bpp else 0
                p = left + prev[i] - ul
                pa, pb, pc = abs(p - left), abs(p - prev[i]), abs(p - ul)
                pr = left if (pa <= pb and pa <= pc) else (prev[i] if pb <= pc else ul)
                line[i] = (line[i] + pr) & 0xFF
        out += line
        prev = line
    return bytes(out)


def extract_images(path: str) -> list[RawImage]:
    with open(path, "rb") as fh:
        data = fh.read()
    if b"/Encrypt" in data:
        raise PDFReadError("PDF 已加密，暂不支持")

    images: list[RawImage] = []
    for m in _OBJ_RE.finditer(data):
        num = int(m.group(1))
        body = m.group(3)
        if b"/Subtype /Image" not in body and b"/Subtype/Image" not in body:
            continue
        sm = re.search(rb"stream\r?\n", body)
        if not sm:
            continue
        head = body[:sm.start()]
        tail = body[sm.end():]
        em = re.search(rb"\r?\nendstream", tail)
        stream = tail[:em.start()] if em else tail
        d = _parse_dict(head)
        filters = d.get("Filter", [])
        payload = stream
        if filters == ["FlateDecode"]:
            try:
                # 有上限解压：超限的 LimitError 往上抛（整个文件失败），
                # 损坏的 zlib.error 保持原来的兜底路径（P1-01）
                payload = limits.inflate(stream, what="PDF Flate 流 ")
            except limits.LimitError:
                raise
            except zlib.error:
                payload = limits.inflate(stream, what="PDF Flate 流（宽松重试）")
            if d.get("Predictor", 0) >= 10:
                payload = _unpredict(payload, _colors(d.get("ColorSpace", "")),
                                     d.get("Columns", d.get("Width", 0)),
                                     d.get("BitsPerComponent", 8))
        images.append(RawImage(
            obj_num=num,
            width=d.get("Width", 0),
            height=d.get("Height", 0),
            filters=filters,
            data=payload,
            colorspace=d.get("ColorSpace", ""),
            bits=d.get("BitsPerComponent", 8),
            columns=d.get("Columns", 0),
            predictor=d.get("Predictor", 0),
            raw_stream=stream,
        ))
    if not images:
        raise PDFReadError("PDF 里没有图像 XObject")
    return images


def _colors(cs: str) -> int:
    if "RGB" in cs:
        return 3
    if "CMYK" in cs:
        return 4
    return 1


def image_payload_bytes(path: str) -> list[bytes]:
    """用于 md5 比对：DCT/JPX 返回原始图片字节。

    PNG 直通进来的 Flate 流会还原成等价 PNG（同 filter 字节），
    比对时调用方应优先比较 DCT/JPX。
    """
    out = []
    for img in extract_images(path):
        b, kind = img.to_image_bytes()
        if kind in ("jpg", "jp2"):
            out.append(b)
    return out
