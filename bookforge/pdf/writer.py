"""漫画 → PDF（手写 PDF，纯标准库）。

核心原则：**图片原字节直嵌，不解码、不重采样**。

    JPEG   → /Filter /DCTDecode，body 就是原始 jpg 字节
    JPEG2000 → /Filter /JPXDecode，原字节
    PNG    → /Filter /FlateDecode + /Predictor 15，body 就是 PNG 的 IDAT
             （PNG 行滤波和 PDF 预测器是同一套规范，连解压都不用）
    PNG(带透明/palette) → 解码成 RGB(A)，alpha 拆成 SMask

缩放一律走 PDF 的 cm 变换矩阵 —— 只改显示尺寸，不动像素。

不支持：TIFF / WEBP / GIF（明确报错，不静默转码）。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from ..formats import png
from ..model import ComicBook, PageSpec


class PDFError(ValueError):
    pass


A4 = (595.28, 841.89)
A5 = (419.53, 595.28)


@dataclass
class _PagePlan:
    page_id: int
    contents_id: int
    image_id: int
    smask_id: int = 0


def _jpeg_components(data: bytes) -> int:
    """从 SOF 段读通道数：1=灰度 3=YCbCr/RGB 4=CMYK"""
    pos = 2
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
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return data[pos + 9]
        pos += 2 + seglen
    return 3


def _pdf_text(s: str) -> str:
    """PDF 字符串：非 ASCII 用 UTF-16BE + BOM 的 hex 形式。"""
    if all(32 <= ord(c) < 127 for c in s):
        return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


class PDFWriter:
    """把一组图片页写成 PDF。"""

    def __init__(self, dpi: float = 300.0):
        if dpi <= 0:
            raise PDFError("dpi 必须大于 0")
        self.dpi = dpi
        self._objs: dict[int, bytes] = {}
        self._next = 1

    # ── 低层 ────────────────────────────────────────────────────────────
    def _alloc(self) -> int:
        i = self._next
        self._next += 1
        return i

    def _put(self, num: int, body: bytes) -> None:
        self._objs[num] = body

    def _stream(self, num: int, dict_body: str, data: bytes) -> None:
        head = f"{num} 0 obj\n<< {dict_body} /Length {len(data)} >>\nstream\n".encode()
        self._objs[num] = head + data + b"\nendstream\nendobj\n"

    # ── 单张图片 → XObject ───────────────────────────────────────────────
    def _image_obj(self, num: int, page, *, page_w: float, page_h: float,
                   fit: str) -> tuple[int, tuple[float, float, float, float]]:
        """写入图片 XObject，返回 (smask_obj_id, (sw, sh, tx, ty))"""
        data = page.data
        fmt = page.fmt
        smask_id = 0

        # 尺寸：优先用读取层探测到的，没有再现场探测，最后兜底。
        # 真实漫画包里存在「脏头 JPEG」（FFD8 不在偏移 0），探测可能失败；
        # 写进 PDF 的 /Width 绝对不能是 0，否则严格阅读器直接报错。
        w, h = page.width, page.height
        if not w or not h:
            w, h, _ = png.image_size(data)
        if not w or not h:
            w, h = 1000, 1414

        if fmt == "jpeg":
            comps = _jpeg_components(data)
            if comps == 1:
                cs = "/DeviceGray"
                extra = ""
            elif comps == 4:
                cs = "/DeviceCMYK"
                # Adobe CMYK JPEG 是反相的
                extra = " /Decode [1 0 1 0 1 0 1 0]"
            else:
                cs = "/DeviceRGB"
                extra = ""
            d = (f"/Type /XObject /Subtype /Image /Width {w} "
                 f"/Height {h} /ColorSpace {cs} "
                 f"/BitsPerComponent 8 /Filter /DCTDecode{extra}")
            self._stream(num, d, data)

        elif fmt == "jp2":
            d = (f"/Type /XObject /Subtype /Image /Width {w} "
                 f"/Height {h} /Filter /JPXDecode")
            self._stream(num, d, data)

        elif fmt == "png":
            info = png.probe(data)
            w, h = info.width, info.height
            if info.passthrough_ok:
                # IDAT 原样直通：PNG 行滤波 == PDF Predictor 15
                body = png.idat_bytes(data)
                d = (f"/Type /XObject /Subtype /Image /Width {info.width} "
                     f"/Height {info.height} /ColorSpace /{info.colorspace} "
                     f"/BitsPerComponent 8 /Filter /FlateDecode "
                     f"/DecodeParms << /Predictor 15 /Colors {info.channels} "
                     f"/BitsPerComponent 8 /Columns {info.width} >>")
                self._stream(num, d, body)
            else:
                pixels, info, ch = png.decode(data)
                if ch in (1, 2):
                    cs = "/DeviceGray"
                else:
                    cs = "/DeviceRGB"
                if ch == 2:                       # gray + alpha
                    gray = bytes(pixels[i] for i in range(0, len(pixels), 2))
                    alpha = bytes(pixels[i] for i in range(1, len(pixels), 2))
                elif ch == 4:                     # rgba
                    rgb = bytearray()
                    alpha = bytearray()
                    for i in range(0, len(pixels), 4):
                        rgb += pixels[i:i + 3]
                        alpha.append(pixels[i + 3])
                    gray, alpha = bytes(rgb), bytes(alpha)
                else:
                    gray, alpha = pixels, b""
                d = (f"/Type /XObject /Subtype /Image /Width {info.width} "
                     f"/Height {info.height} /ColorSpace {cs} "
                     f"/BitsPerComponent 8 /Filter /FlateDecode")
                if alpha:
                    smask_id = self._alloc()
                    self._stream(smask_id,
                                 f"/Type /XObject /Subtype /Image /Width {info.width} "
                                 f"/Height {info.height} /ColorSpace /DeviceGray "
                                 f"/BitsPerComponent 8 /Filter /FlateDecode",
                                 zlib.compress(alpha, 6))
                    d += f" /SMask {smask_id} 0 R"
                self._stream(num, d, zlib.compress(gray, 6))
        else:
            raise PDFError(f"暂不支持把 {fmt} 图片嵌进 PDF（支持 jpeg / jp2 / png）")

        if fit == "native":
            sw = w * 72.0 / self.dpi
            sh = h * 72.0 / self.dpi
            tx = ty = 0.0
        else:
            avail_w, avail_h = page_w, page_h
            scale = min(avail_w / w, avail_h / h)
            sw, sh = w * scale, h * scale
            tx, ty = (avail_w - sw) / 2, (avail_h - sh) / 2
        return smask_id, (sw, sh, tx, ty)

    # ── 主入口 ──────────────────────────────────────────────────────────
    def build(self, book: ComicBook, *, fit: str = "native",
              page_size: tuple[float, float] = A5,
              title: str = "", author: str = "") -> bytes:
        if not book.pages:
            raise PDFError("没有页面可写入")

        catalog_id, pages_id = 1, 2
        self._next = 3
        plans: list[_PagePlan] = []
        for _ in book.pages:
            plan = _PagePlan(self._alloc(), self._alloc(), self._alloc())
            plans.append(plan)

        if fit == "native":
            page_w, page_h = page_size
        else:
            page_w, page_h = page_size

        kids, prepared = [], []
        for plan, page in zip(plans, book.pages):
            smask_id, (sw, sh, tx, ty) = self._image_obj(
                plan.image_id, page, page_w=page_w, page_h=page_h, fit=fit)
            if fit == "native":
                # 页面尺寸跟着图片走
                pw = sw or page_w
                ph = sh or page_h
                tx = ty = 0.0
            else:
                pw, ph = page_w, page_h
            plan.smask_id = smask_id
            prepared.append((pw, ph, sw, sh, tx, ty))

        for plan, (pw, ph, sw, sh, tx, ty) in zip(plans, prepared):
            content = (f"q\n{sw:.4f} 0 0 {sh:.4f} {tx:.4f} {ty:.4f} cm\n/Im0 Do\nQ\n"
                       ).encode()
            self._stream(plan.contents_id, "", content)
            self._put(plan.page_id, (
                f"{plan.page_id} 0 obj\n<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {pw:.4f} {ph:.4f}] "
                f"/Resources << /XObject << /Im0 {plan.image_id} 0 R >> >> "
                f"/Contents {plan.contents_id} 0 R >>\nendobj\n").encode())
            kids.append(f"{plan.page_id} 0 R")

        self._put(catalog_id, (
            f"{catalog_id} 0 obj\n<< /Type /Catalog /Pages {pages_id} 0 R >>\nendobj\n"
        ).encode())
        self._put(pages_id, (
            f"{pages_id} 0 obj\n<< /Type /Pages /Count {len(plans)} "
            f"/Kids [{' '.join(kids)}] >>\nendobj\n").encode())

        info_id = 0
        if title or author:
            info_id = self._alloc()
            parts = []
            if title:
                parts.append(f"/Title {_pdf_text(title)}")
            if author:
                parts.append(f"/Author {_pdf_text(author)}")
            parts.append("/Producer (BookForge)")
            self._put(info_id, f"{info_id} 0 obj\n<< {' '.join(parts)} >>\nendobj\n".encode())

        return self._serialize(catalog_id, info_id)

    def _serialize(self, root_id: int, info_id: int) -> bytes:
        head = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
        out = bytearray(head)
        offsets: dict[int, int] = {}
        for num in sorted(self._objs):
            offsets[num] = len(out)
            out += self._objs[num]

        size = self._next
        xref_at = len(out)
        out += f"xref\n0 {size}\n".encode()
        out += b"0000000000 65535 f \n"
        for num in range(1, size):
            off = offsets.get(num, 0)
            out += f"{off:010d} 00000 n \n".encode()

        trailer = f"<< /Size {size} /Root {root_id} 0 R"
        if info_id:
            trailer += f" /Info {info_id} 0 R"
        trailer += " >>"
        out += f"trailer\n{trailer}\nstartxref\n{xref_at}\n%%EOF\n".encode()
        return bytes(out)
