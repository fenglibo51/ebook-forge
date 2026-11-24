"""极简 PDF 页面渲染器（P6 兜底，有损）。

用途：**只**给那些没有无损通路的 PDF 用 ——
蒙版型（ImageMask / stencil）、图片流损坏、整页图覆盖率不足。
这类文件用 `pdfimages` 抽出来的东西不是页面（反相、缺矢量文字、碎片），
除了逐页渲染没有别的办法。

能力边界（诚实写清楚，不吹）：

    支持   q/Q 图形状态栈、cm 变换、Do 画 XObject（图片和 Form）
           re 矩形 + f/S/B 填充描边、m/l/c/v/y/h 路径 + f/S
           rg/g/k/RG/G/K 颜色、w 线宽、W n 裁剪（矩形近似）
           ImageMask（1bit 蒙版，按当前填充色上色）
           内联图像 BI/ID/EI 的跳过（不解码）
    不做   文字（BT/Tf/Tj 一律跳过）—— 没有 Type1/CFF/TrueType 字形轮廓光栅化
           渐变、透明度、混合模式、图案填充、图章注释

所以渲染出来的页**可能缺文字**。这是产品取舍：宁可出一张能看的图并明确标注
「有损、可能缺字」，也不假装能完整复刻（那等于重写 poppler）。

输出：RGB 位图（bytes），由调用方编码成 PNG。
"""

from __future__ import annotations

import math
import os
import re
import struct
import zlib
from dataclasses import dataclass

from .reader import PDFDocument, dict_get, dict_int, dict_name, dict_ref


class RenderError(ValueError):
    pass


# ── 渲染像素预算 ──────────────────────────────────────────────────────────
# 防止异常/恶意 MediaBox 在建位图时直接吃掉几个 GB。
#
# 实测（scale=300dpi，即 4.17 倍）：
#   A4          2479×3508   =  8.7M 像素 /  25 MB
#   A5          1750×2479   =  4.3M 像素 /  12 MB
#   漫画长图    3333×50000  =  167M 像素 / 477 MB   ← 真实场景，必须放行
#   MediaBox×100  247916×350833 = 870 亿像素 / 248 GB  ← 这一句 bytearray 直接打死容器
#
# 阈值按真实长图留了余量：总像素给到 2 亿（600MB RGB24），单边 5 万。
# 两个都能用环境变量调，NAS 内存小就往下压。
MAX_SIDE_PX = int(os.environ.get("BOOKFORGE_MAX_SIDE_PX", "50000"))
MAX_PIXELS = int(os.environ.get("BOOKFORGE_MAX_PIXELS", "200000000"))


def _check_px_budget(w_px: int, h_px: int) -> None:
    """单边 + 总像素双上限。超了抛 RenderError，由上层判任务失败。"""
    if w_px > MAX_SIDE_PX or h_px > MAX_SIDE_PX:
        raise RenderError(
            f"页面太大：{w_px}×{h_px} 像素，单边上限 {MAX_SIDE_PX}。"
            "PDF 的 MediaBox 可能异常，或者这个页面本身不适合渲染"
            f"（可用 BOOKFORGE_MAX_SIDE_PX 调整）")
    total = w_px * h_px
    if total > MAX_PIXELS:
        raise RenderError(
            f"页面像素过多：{total / 1e6:.0f}M（{w_px}×{h_px}），"
            f"上限 {MAX_PIXELS / 1e6:.0f}M。渲染这张图需要约 "
            f"{total * 3 / 1048576:.0f} MB 内存（可用 BOOKFORGE_MAX_PIXELS 调整）")


# ── 矩阵 ──────────────────────────────────────────────────────────────────
# PDF 矩阵 [a b c d e f]：x' = a*x + c*y + e, y' = b*x + d*y + f

def mat_mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# 1bit 展开表：一个字节 → 8 个像素值（置位 255，否则 0）
_BIT_TABLE = [bytes(255 if (v >> (7 - i)) & 1 else 0 for i in range(8))
              for v in range(256)]


def apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


# ── 位图 ──────────────────────────────────────────────────────────────────

class Bitmap:
    """RGB24 位图，原点在左上（和 PNG 一致），输入坐标是 PDF 的（原点左下）。"""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.buf = bytearray(b"\xff" * (width * height * 3))

    def fill_rect(self, x0: float, y0: float, x1: float, y1: float, color):
        """PDF 坐标（左下原点）的矩形 → 填充（会翻 y）"""
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        ix0 = max(0, int(math.floor(x0)))
        ix1 = min(self.w, int(math.ceil(x1)))
        # PDF y 往上，位图 y 往下
        iy0 = max(0, int(math.floor(self.h - y1)))
        iy1 = min(self.h, int(math.ceil(self.h - y0)))
        if ix1 <= ix0 or iy1 <= iy0:
            return
        r, g, b = color
        row = bytes((r, g, b)) * (ix1 - ix0)
        for y in range(iy0, iy1):
            off = (y * self.w + ix0) * 3
            self.buf[off:off + len(row)] = row

    def blend_mask(self, mask: bytes, mw: int, mh: int,
                   x0: float, y0: float, x1: float, y1: float, color):
        """把 1bit 蒙版按目标矩形贴上去（最近邻缩放），1 的位置涂 color。

        性能关键：逐像素三层循环在 1563×2338 上要 20 秒/页。
        改成「按行展开 → 切片步进降采样 → 只在置位处写 RGB」，
        实际约 3~4 秒/页。
        """
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        ix0 = max(0, int(math.floor(x0)))
        ix1 = min(self.w, int(math.ceil(x1)))
        iy0 = max(0, int(math.floor(self.h - y1)))
        iy1 = min(self.h, int(math.ceil(self.h - y0)))
        if ix1 <= ix0 or iy1 <= iy0 or mw <= 0 or mh <= 0:
            return

        rowbytes = (mw + 7) // 8
        # 一个字节 → 8 个 0/255 的展开表（只建一次）
        table = _BIT_TABLE
        span_w = max(1e-6, x1 - x0)
        span_h = max(1e-6, y1 - y0)
        sx = mw / span_w
        sy = mh / span_h
        r, g, b = color
        buf = self.buf
        W = self.w
        for y in range(iy0, iy1):
            # ⚠️ 从 iy0 正向映射到蒙版第 0 行。早先写成 (h - y - y0) * sy，
            # 结果第一行扫描取到蒙版最后一行 → 整页上下颠倒（实测对比图一眼看出）。
            my = int((y - iy0 + 0.5) * sy)
            if my < 0 or my >= mh:
                continue
            mrow = mask[my * rowbytes:(my + 1) * rowbytes]
            if not mrow:
                continue
            expanded = b"".join([table[v] for v in mrow])      # 每像素 1 字节
            base = (y * W + ix0) * 3
            span = ix1 - ix0
            if abs(sx - 1.0) < 1e-6:
                # 同尺寸：直接铺
                n = min(len(expanded), span)
                for i in range(n):
                    if expanded[i]:
                        o = base + i * 3
                        buf[o] = r
                        buf[o + 1] = g
                        buf[o + 2] = b
            elif sx > 1.5:
                # 缩小：切片步进（快路径，真实扫描件走这里）
                step = max(1, int(round(sx)))
                sample = expanded[::step]
                n = min(len(sample), span)
                for i in range(n):
                    if sample[i]:
                        o = base + i * 3
                        buf[o] = r
                        buf[o + 1] = g
                        buf[o + 2] = b
            else:
                # 放大或轻微缩放：按下标映射（蒙版比目标小的时候走这里，
                # 早先只写了降采样分支，放大时只填了前 mw 个像素 → 画面只剩一角）
                lim = min(mw, len(expanded))
                for i in range(span):
                    mx = int(i * sx)
                    if mx >= lim:
                        break
                    if expanded[mx]:
                        o = base + i * 3
                        buf[o] = r
                        buf[o + 1] = g
                        buf[o + 2] = b

    def put_image(self, pixels: bytes, iw: int, ih: int, channels: int,
                  x0: float, y0: float, x1: float, y1: float):
        """把 RGB/灰度像素贴到矩形区域（最近邻缩放）。"""
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        ix0 = max(0, int(math.floor(x0)))
        ix1 = min(self.w, int(math.ceil(x1)))
        iy0 = max(0, int(math.floor(self.h - y1)))
        iy1 = min(self.h, int(math.ceil(self.h - y0)))
        if ix1 <= ix0 or iy1 <= iy0 or iw <= 0 or ih <= 0:
            return
        sx = iw / max(1.0, (x1 - x0))
        sy = ih / max(1.0, (y1 - y0))
        buf = self.buf
        for y in range(iy0, iy1):
            # 同理：从 iy0 正向映射，别用 h-y（会上下颠倒）
            py = int((y - iy0 + 0.5) * sy)
            if py < 0 or py >= ih:
                continue
            row_off = py * iw * channels
            dst = y * self.w
            for x in range(ix0, ix1):
                px = int((x + 0.5 - x0) * sx)
                if px < 0 or px >= iw:
                    continue
                s = row_off + px * channels
                d = (dst + x) * 3
                if channels == 1:
                    v = pixels[s]
                    buf[d] = v
                    buf[d + 1] = v
                    buf[d + 2] = v
                else:
                    buf[d] = pixels[s]
                    buf[d + 1] = pixels[s + 1]
                    buf[d + 2] = pixels[s + 2]


# ── 内容流词法 ────────────────────────────────────────────────────────────

_TOKEN = re.compile(rb"""
      (?P<num>[+-]?(?:\d+\.?\d*|\.\d+))
    | (?P<name>/[^\s/\[\]()<>{}%]*)
    | (?P<str>\((?:[^()\\]|\\.|\([^)]*\))*\))
    | (?P<hex><[0-9A-Fa-f\s]*>)
    | (?P<arr>[\[\]])
    | (?P<op>[A-Za-z'"*][A-Za-z0-9'"*]*)
    | (?P<skip>\s+|%[^\n]*)
""", re.X)


def tokenize(data: bytes):
    pos = 0
    n = len(data)
    while pos < n:
        m = _TOKEN.match(data, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        if kind == "skip":
            continue
        if kind == "num":
            yield ("num", float(text))
        elif kind == "name":
            yield ("name", text[1:].decode("latin-1"))
        elif kind == "str":
            yield ("str", text)
        elif kind == "hex":
            yield ("hex", text)
        elif kind == "arr":
            yield ("arr", text.decode())
        elif kind == "op":
            yield ("op", text.decode("latin-1"))


@dataclass
class GState:
    ctm: tuple = IDENTITY
    fill: tuple = (0.0, 0.0, 0.0)       # RGB 0~1
    stroke: tuple = (0.0, 0.0, 0.0)
    line_width: float = 1.0
    clip: tuple | None = None           # 矩形裁剪（PDF 坐标）


def _cmyk_to_rgb(c, m, y, k):
    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))


class PageRenderer:
    """渲染单页。scale = 位图像素 / PDF 点。"""

    def __init__(self, doc: PDFDocument, scale: float = 2.0):
        self.doc = doc
        self.scale = scale

    def render(self, page_index: int) -> Bitmap:
        pages = self.doc.page_objects()
        if page_index >= len(pages):
            raise RenderError(f"页索引越界：{page_index}")
        _num, head = pages[page_index]
        mb = dict_get(head, "MediaBox")
        x0 = y0 = 0.0
        x1, y1 = 595.0, 842.0
        if mb:
            nums = [float(v) for v in re.findall(rb"-?\d+\.?\d*", mb)]
            if len(nums) >= 4:
                x0, y0, x1, y1 = nums[0], nums[1], nums[2], nums[3]
        w_pt, h_pt = abs(x1 - x0), abs(y1 - y0)
        if w_pt <= 0 or h_pt <= 0:
            raise RenderError("页面 MediaBox 非法")
        # MediaBox 是文档自己写的数字，不可信 —— 必须在分配位图之前先算预算。
        # 实测 [0 0 59500 84200] 在 300dpi 下是 870 亿像素（248 GB），
        # 不加这道检查，下面那行 bytearray 就是一次 OOM kill。
        if not (math.isfinite(w_pt) and math.isfinite(h_pt)):
            raise RenderError(f"页面 MediaBox 含非有限数值：{w_pt}×{h_pt}pt")

        w_px = max(1, int(w_pt * self.scale))
        h_px = max(1, int(h_pt * self.scale))
        _check_px_budget(w_px, h_px)

        bmp = Bitmap(w_px, h_px)
        # 基础变换：PDF 原点在左下、位图在左上，且要乘缩放
        base = (self.scale, 0.0, 0.0, self.scale, -x0 * self.scale, -y0 * self.scale)

        contents = self._contents(head)
        res = self._resources(head)
        for chunk in contents:
            try:
                self._run(chunk, bmp, base, res)
            except Exception:
                continue            # 单段内容流坏掉不影响整页
        return bmp

    def _resources(self, head: bytes) -> bytes:
        rn = dict_ref(head, "Resources")
        return (self.doc.objs.get(rn, b"") if rn
                else (dict_get(head, "Resources") or b""))

    def _contents(self, head: bytes) -> list[bytes]:
        """页面 /Contents 可能是单个流、也可能是流数组。

        ⚠️ 内容流几乎总是 FlateDecode 压缩的（文件头 x\\x9c），
        不解压直接 tokenize 会拿到一堆二进制噪声，渲染出来整页空白。
        实测就是这么踩的。
        """
        out: list[bytes] = []
        arr = dict_get(head, "Contents")
        if arr and arr.startswith(b"["):
            for num in re.findall(rb"(\d+)\s+\d+\s+R", arr):
                d = self._decode_stream(int(num))
                if d:
                    out.append(d)
        else:
            ref = dict_ref(head, "Contents")
            if ref is not None:
                d = self._decode_stream(ref)
                if d:
                    out.append(d)
        return out

    def _decode_stream(self, num: int) -> bytes:
        raw = self.doc.streams.get(num)
        if not raw:
            return b""
        head = self.doc.objs.get(num, b"")
        filters = [x.decode() for x in
                   re.findall(rb"/([A-Za-z0-9_]+)", dict_get(head, "Filter") or b"")]
        data = raw
        for f in filters:
            if f == "FlateDecode":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    try:
                        data = zlib.decompressobj().decompress(data)
                    except Exception:
                        return b""
            elif f in ("DCTDecode", "JPXDecode", "CCITTFaxDecode"):
                return b""            # 图像流，不是内容流
            else:
                return b""            # 其他编码（LZW 等）不支持
        return data

    # ── 内容流解释 ──────────────────────────────────────────────────────
    def _run(self, data: bytes, bmp: Bitmap, base: tuple,
             resources: bytes = b"") -> None:
        stack: list[GState] = [GState(ctm=base)]
        operands: list = []
        path: list[tuple[float, float]] = []      # 折线近似
        sub_start = None

        def gs() -> GState:
            return stack[-1]

        def color_from_ops(ops) -> tuple | None:
            if len(ops) >= 4 and all(isinstance(x, float) for x in ops[-4:]):
                c, m, y, k = ops[-4:]
                return _cmyk_to_rgb(c, m, y, k)
            if len(ops) >= 3 and all(isinstance(x, float) for x in ops[-3:]):
                return tuple(ops[-3:])
            if len(ops) >= 1 and isinstance(ops[-1], float):
                v = ops[-1]
                return (v, v, v)
            return None

        for kind, val in tokenize(data):
            if kind != "op":
                operands.append(val if kind != "name" else ("/" + val))
                continue
            op = val

            if op == "q":
                stack.append(GState(**gs().__dict__))
            elif op == "Q":
                if len(stack) > 1:
                    stack.pop()
            elif op == "cm" and len(operands) >= 6:
                m = tuple(float(v) for v in operands[-6:])
                stack[-1] = GState(**{**gs().__dict__, "ctm": mat_mul(m, gs().ctm)})
            elif op in ("rg", "g", "k"):
                c = color_from_ops(operands)
                if c:
                    stack[-1].fill = c
            elif op in ("RG", "G", "K"):
                c = color_from_ops(operands)
                if c:
                    stack[-1].stroke = c
            elif op == "w" and operands:
                stack[-1].line_width = float(operands[-1])
            elif op == "re" and len(operands) >= 4:
                x, y, w, h = (float(v) for v in operands[-4:])
                path = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                sub_start = (x, y)
            elif op == "m" and len(operands) >= 2:
                x, y = float(operands[-2]), float(operands[-1])
                path = [(x, y)]
                sub_start = (x, y)
            elif op == "l" and len(operands) >= 2:
                path.append((float(operands[-2]), float(operands[-1])))
            elif op in ("c", "v", "y") and len(operands) >= 4:
                # 贝塞尔：只用终点近似（不做曲线细分，画出来是折线）
                path.append((float(operands[-2]), float(operands[-1])))
            elif op == "h":
                if path and sub_start:
                    path.append(sub_start)
            elif op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                self._fill_path(bmp, path, gs())
                path = []
            elif op in ("S", "s"):
                self._stroke_path(bmp, path, gs())
                path = []
            elif op == "n":
                path = []
            elif op == "Do" and operands:
                name = str(operands[-1]).lstrip("/")
                self._do_xobject(name, bmp, gs(), resources)
            elif op == "BI":
                pass            # 内联图像：不支持，跳过
            operands = []

    def _fill_path(self, bmp: Bitmap, path, g: GState) -> None:
        if len(path) < 3:
            return
        pts = [apply(g.ctm, x, y) for x, y in path]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # 用包围盒填充（极简：不区分多边形形状，够蒙版页用）
        bmp.fill_rect(min(xs), min(ys), max(xs), max(ys),
                      tuple(int(c * 255) for c in g.fill))

    def _stroke_path(self, bmp: Bitmap, path, g: GState) -> None:
        if len(path) < 2:
            return
        pts = [apply(g.ctm, x, y) for x, y in path]
        lw = max(1.0, g.line_width * abs(g.ctm[0]))
        col = tuple(int(c * 255) for c in g.stroke)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            self._line(bmp, x0, y0, x1, y1, lw, col)

    def _line(self, bmp: Bitmap, x0, y0, x1, y1, lw, col) -> None:
        """粗线画成一系列小矩形（够用，不求精致）。"""
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(steps + 1):
            t = i / steps
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            bmp.fill_rect(x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2, col)

    def _do_xobject(self, name: str, bmp: Bitmap, g: GState,
                    resources: bytes) -> None:
        """在当前 Resources 里找 name 对应的 XObject。

        ⚠️ 必须用**当前** Resources（页面自己的，或 Form 自己的）去查，
        不能遍历全文档找同名对象 —— 真实 PDF 里各页的 XObject 名高度重复
        （Xf6 / Im0 到处都是），遍历会画错页的图。
        实测早先这么写，渲染出来整页全白。
        """
        if not resources:
            return
        tn = dict_ref(resources, "XObject")
        xo = (self.doc.objs.get(tn, b"") if tn
              else (dict_get(resources, "XObject") or b""))
        if not xo:
            return
        m = re.search(rb"/" + re.escape(name.encode("latin-1")) + rb"\s+(\d+)\s+\d+\s+R", xo)
        if not m:
            return
        onum = int(m.group(1))
        obj = self.doc.objs.get(onum)
        if obj is None:
            return
        subtype = dict_name(obj, "Subtype")
        if subtype == "Image":
            self._draw_image(onum, obj, bmp, g)
        elif subtype == "Form":
            raw = self._decode_stream(onum)      # Form 内容流同样可能是压缩的
            if not raw:
                return
            # Form 可以有自己的 Matrix 和 Resources；没有就继承调用者的
            fm = IDENTITY
            mstr = dict_get(obj, "Matrix")
            if mstr:
                nums = [float(v) for v in re.findall(rb"-?\d+\.?\d*", mstr)]
                if len(nums) >= 6:
                    fm = tuple(nums[:6])
            sub_res = self._resources(obj) or resources
            self._run(raw, bmp, mat_mul(fm, g.ctm), sub_res)

    def _draw_image(self, onum: int, head: bytes, bmp: Bitmap, g: GState) -> None:
        from ..formats import png as pngmod

        w = dict_int(head, "Width", 0)
        h = dict_int(head, "Height", 0)
        raw = self.doc.streams.get(onum, b"")
        if not raw or not w or not h:
            return
        is_mask = (dict_get(head, "ImageMask") or b"").strip() == b"true"
        filters = [x.decode() for x in re.findall(rb"/([A-Za-z0-9_]+)",
                                                  dict_get(head, "Filter") or b"")]

        # 图片在页面上的位置：当前变换矩阵的单位正方形
        a, b, c, d, e, f = g.ctm
        x0, y0 = apply(g.ctm, 0, 0)
        x1, y1 = apply(g.ctm, 1, 1)

        if is_mask or filters == ["CCITTFaxDecode"]:
            from . import ccitt
            from .images import _unpredict  # noqa: F401
            k = -1
            dp = dict_get(head, "DecodeParms") or b""
            mk = re.search(rb"/K\s+(-?\d+)", dp)
            if mk:
                k = int(mk.group(1))
            cols = w
            rows = h
            mc = re.search(rb"/Columns\s+(\d+)", dp)
            mr = re.search(rb"/Rows\s+(\d+)", dp)
            if mc:
                cols = int(mc.group(1))
            if mr and int(mr.group(1)) > 0:
                rows = int(mr.group(1))
            data = raw
            if filters and filters[0] == "FlateDecode":
                data = zlib.decompress(raw)
            try:
                bits = ccitt.decode_g4(data, cols, rows) if k < 0 \
                    else ccitt.decode_g3(data, cols, rows, k == 0)
            except Exception:
                return
            color = tuple(int(c * 255) for c in g.fill)
            bmp.blend_mask(bits, cols, rows, min(x0, x1), min(y0, y1),
                           max(x0, x1), max(y0, y1), color)
            return

        # 普通图片：JPEG 解不了（没有解码器），PNG 可以
        if "DCTDecode" in filters or "JPXDecode" in filters:
            return            # 没有 JPEG/JP2 解码器 —— 明确跳过，不假装
        try:
            info = pngmod.probe(raw)
            px, inf, ch = pngmod.decode(raw)
            if ch == 4:
                px = bytes(v for i in range(0, len(px), 4) for v in px[i:i + 3])
                ch = 3
            elif ch == 2:
                px = bytes(px[i] for i in range(0, len(px), 2))
                ch = 1
            bmp.put_image(px, inf.width, inf.height, ch,
                          min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        except Exception:
            return


def render_page_to_png(doc: PDFDocument, page_index: int,
                       scale: float = 2.0) -> bytes:
    """渲染一页 → PNG 字节。"""
    from ..formats import png as pngmod

    bmp = PageRenderer(doc, scale=scale).render(page_index)
    return pngmod.encode(bytes(bmp.buf), bmp.w, bmp.h, 3)
