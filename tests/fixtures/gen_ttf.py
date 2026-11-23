"""手搓一个带复合字形的最小 TrueType 字体（纯 struct，不依赖任何字体库）。

为什么要自造：容器里唯一的中文字体 NotoSansSC-Regular.ttf 有 30796 个字形，
但**一个复合字形都没有**，所以「子集化时 component GID 要跟着重编号」这条
测试永远 skip —— 等于没测。复合字形是子集化最容易出错的地方（编号忘改，
字就画到别的地方去了），不能靠「环境里碰巧有就测」蒙混过去。

字形布局（4 个字）：
    gid 0  .notdef    空字形
    gid 1  'A'        简单字形，一个三角形（3 个点）
    gid 2  'B'        简单字形，三角形平移
    gid 3  'À'        复合字形，**引用 gid 2（'B'）**

为什么复合字形要引用「排在后面的那个」而不是 gid 1：
子集化按旧 gid 顺序保留字形，`subset({'À'})` 之后 'B' 会被重新编成 1 号。
如果代码忘了重写 component，就会指回旧号 2 —— 而 2 在新字体里是 'À' 自己，
画出来是一坨错字。引用 gid 1 的话新编号恰好还是 1，这个 bug 就测不出来
（一开始就是这么写的，变异测试直接漏过）。

cmap 用 format 4：0x41→1、0x42→2、0xC0→3。
"""

from __future__ import annotations

import struct
from pathlib import Path

# ── glyf 里的字形 ──────────────────────────────────────────────────────


def _simple_glyph(dx: int = 100) -> bytes:
    """一个三角形的简单字形：3 个点，1 条轮廓。"""
    pts = [(dx, 0), (dx + 200, 0), (dx + 100, 300)]
    xmin = min(p[0] for p in pts)
    ymin = min(p[1] for p in pts)
    xmax = max(p[0] for p in pts)
    ymax = max(p[1] for p in pts)
    out = struct.pack(">h", 1)                       # numberOfContours
    out += struct.pack(">hhhh", xmin, ymin, xmax, ymax)
    out += struct.pack(">H", len(pts) - 1)           # endPtsOfContours
    out += struct.pack(">H", 0)                      # instructionLength
    out += bytes([0x01] * len(pts))                  # 全部 ON_CURVE，坐标用 int16
    prev = 0
    for x, _y in pts:                                # 相对增量
        out += struct.pack(">h", x - prev)
        prev = x
    prev = 0
    for _x, y in pts:
        out += struct.pack(">h", y - prev)
        prev = y
    return out


def _composite_glyph(base_gid: int) -> bytes:
    """复合字形：只有一个 component，引用 base_gid，偏移 (0,0)。"""
    out = struct.pack(">h", -1)                      # numberOfContours = -1 → 复合
    out += struct.pack(">hhhh", 100, 0, 300, 300)    # bbox
    # ARG_1_AND_2_ARE_WORDS | ARGS_ARE_XY_VALUES，没有 MORE_COMPONENTS
    out += struct.pack(">HHhh", 0x0003, base_gid, 0, 0)
    return out


# ── 各张表 ─────────────────────────────────────────────────────────────


def _head(index_to_loc: int = 0) -> bytes:
    return b"".join([
        struct.pack(">i", 0x00010000),               # version 1.0
        struct.pack(">i", 0x00010000),               # fontRevision
        struct.pack(">I", 0),                        # checkSumAdjustment
        struct.pack(">I", 0x5F0F3CF5),               # magicNumber
        struct.pack(">H", 0),                        # flags
        struct.pack(">H", 1000),                     # unitsPerEm
        struct.pack(">q", 0),                        # created
        struct.pack(">q", 0),                        # modified
        struct.pack(">hhhh", 100, 0, 300, 300),      # bbox
        struct.pack(">H", 0),                        # macStyle
        struct.pack(">H", 8),                        # lowestRecPPEM
        struct.pack(">h", 2),                        # fontDirectionHint
        struct.pack(">h", index_to_loc),             # indexToLocFormat
        struct.pack(">h", 0),                        # glyphDataFormat
    ])


def _hhea(num_hmetrics: int) -> bytes:
    return b"".join([
        struct.pack(">i", 0x00010000),
        struct.pack(">h", 800),                      # ascender
        struct.pack(">h", -200),                     # descender
        struct.pack(">h", 0),                        # lineGap
        struct.pack(">H", 600),                      # advanceWidthMax
        struct.pack(">h", 0), struct.pack(">h", 0), struct.pack(">h", 300),
        struct.pack(">h", 1), struct.pack(">h", 0), struct.pack(">h", 0),
        struct.pack(">hhhh", 0, 0, 0, 0),            # 4 个 reserved
        struct.pack(">h", 0),                        # metricDataFormat
        struct.pack(">H", num_hmetrics),
    ])


def _maxp(num_glyphs: int) -> bytes:
    return struct.pack(">I", 0x00010000) + struct.pack(">H", num_glyphs) + b"\x00" * 26


def _hmtx(n: int, adv: int = 600) -> bytes:
    return b"".join(struct.pack(">Hh", adv, 0) for _ in range(n))


def _cmap() -> bytes:
    """format 4，两个码位各占一段 + 结尾 0xFFFF 段。"""
    segs = [(0x41, 0x41, 1 - 0x41), (0x42, 0x42, 2 - 0x42),
            (0xC0, 0xC0, 3 - 0xC0), (0xFFFF, 0xFFFF, 1)]
    n = len(segs)
    body = b"".join(struct.pack(">H", e) for _s, e, _d in segs)
    body += struct.pack(">H", 0)                     # reservedPad
    body += b"".join(struct.pack(">H", s) for s, _e, _d in segs)
    body += b"".join(struct.pack(">h", d) for _s, _e, d in segs)
    body += b"".join(struct.pack(">H", 0) for _ in segs)   # idRangeOffset 全 0
    # ⚠️ format 4 的头是 14 字节（7 个 u16），不是 16。
    # 长度字段写大了 fontTools 会报 "cmap subtable is truncated"。
    length = 14 + len(body)
    header = b"".join([
        struct.pack(">HHH", 4, length, 0),           # format / length / language
        struct.pack(">HHH", n * 2, 2 * (1 << (n.bit_length() - 1)),
                    1 << (n.bit_length() - 1)),
        struct.pack(">H", n * 2 - 2 * (1 << (n.bit_length() - 1))),
    ])
    sub = header + body
    # cmap 头 + 一条 (3, 1) 记录
    return struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + sub


def build() -> bytes:
    g1 = _simple_glyph(100)
    if len(g1) % 2:
        g1 += b"\x00"                                # 短 loca 要求偏移是偶数
    g2 = _simple_glyph(400)
    if len(g2) % 2:
        g2 += b"\x00"
    g3 = _composite_glyph(2)                         # 'À' 引用 'B'（gid 2）
    if len(g3) % 2:
        g3 += b"\x00"
    glyf = g1 + g2 + g3
    o1 = len(g1)
    o2 = o1 + len(g2)
    o3 = o2 + len(g3)
    loca = struct.pack(">HHHHH", 0, 0, o1 // 2, o2 // 2, o3 // 2)

    tables = {
        "cmap": _cmap(),
        "glyf": glyf,
        "head": _head(0),
        "hhea": _hhea(4),
        "hmtx": _hmtx(4),
        "loca": loca,
        "maxp": _maxp(4),
    }

    tags = sorted(tables)
    n = len(tags)
    entry_selector = n.bit_length() - 1
    search_range = (1 << entry_selector) * 16
    out = struct.pack(">IHHHH", 0x00010000, n, search_range,
                      entry_selector, n * 16 - search_range)
    offset = 12 + n * 16
    blobs = {}
    for tag in tags:
        blob = tables[tag]
        blobs[tag] = (offset, blob)
        out += struct.pack(">4sIII", tag.encode("latin-1"), 0, offset, len(blob))
        offset += len(blob)
        pad = (-len(blob)) % 4
        offset += pad
    for tag in tags:
        off, blob = blobs[tag]
        out += blob + b"\x00" * ((-len(blob)) % 4)
    return out


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "composite.ttf"
    target.write_bytes(build())
    print(f"写出 {target} ({target.stat().st_size} 字节)")
