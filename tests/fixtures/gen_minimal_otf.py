"""生成一个最小的合法 OTTO/CFF 字体，供 CI 覆盖 CFF 路径。

为什么要自己造：真实的 Source Han Sans 有 16MB，进 git 太重；而 CFF 路径
（/FontFile3 + /CIDFontType0）如果 CI 里永远 skip，就等于没测过。

这个字体只有 2 个字形（.notdef 和 A），几百字节，结构上完全合法：
    sfnt(OTTO) + head + hhea + maxp + hmtx + cmap + CFF

用法：
    python3 tests/fixtures/gen_minimal_otf.py [输出路径]
默认写 tests/fixtures/minimal-cff.otf。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * ((4 - len(b) % 4) % 4)


def _index(items: list[bytes]) -> bytes:
    """CFF INDEX：count + offSize + offsets + data。"""
    if not items:
        return b"\0\0"
    data = b"".join(items)
    off_size = 1 if len(data) + 1 < 0x100 else (2 if len(data) + 1 < 0x10000 else 4)
    offs = [1]
    for it in items:
        offs.append(offs[-1] + len(it))
    out = struct.pack(">H", len(items)) + bytes([off_size])
    for o in offs:
        out += o.to_bytes(off_size, "big")
    return out + data


def build_cff(charstrings_offset: int) -> bytes:
    """CFF 表。CharStrings 的偏移要由调用方算好传进来。"""
    header = bytes([1, 0, 4, 1])                       # major minor hdrSize offSize

    name_index = _index([b"MinCFF"])

    # Top DICT：CharStrings(17) + Private(18)[size, offset]
    # 数字用 5 字节形式（29 = 32-bit 整数），省得算 1/2 字节编码
    def i32(v: int) -> bytes:
        return b"\x1d" + v.to_bytes(4, "big", signed=True)

    top_dict = (i32(charstrings_offset) + b"\x11"        # CharStrings
                + i32(0) + i32(0) + b"\x12")            # Private size=0 offset=0
    top_dict_index = _index([top_dict])

    string_index = b"\0\0"                              # 无自定义字符串
    gsubr_index = b"\0\0"                               # 无全局子程序

    # 2 个字形：.notdef 和 A，都只有 endchar(14)
    charstrings = _index([b"\x0e", b"\x0e"])

    return header + name_index + top_dict_index + string_index + gsubr_index + charstrings


def build_cmap() -> bytes:
    """format 4，映射 'A'(0x41) → gid 1。"""
    seg_count = 2
    end_codes = [0x41, 0xFFFF]
    start_codes = [0x41, 0xFFFF]
    id_deltas = [(1 - 0x41) & 0xFFFF, 1]
    id_range_offsets = [0, 0]

    length = 14 + seg_count * 8
    sub = struct.pack(">HHHHHHH", 4, length, 0, seg_count * 2, 4, 1, 0)
    sub += struct.pack(">" + "H" * seg_count, *end_codes)
    sub += struct.pack(">H", 0)                          # reservedPad
    sub += struct.pack(">" + "H" * seg_count, *start_codes)
    sub += struct.pack(">" + "H" * seg_count, *id_deltas)
    sub += struct.pack(">" + "H" * seg_count, *id_range_offsets)

    return struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + sub


def build_head() -> bytes:
    return struct.pack(
        ">IIIIHHqqhhhhHHhhh",
        0x00010000,          # version
        0x00010000,          # fontRevision
        0,                   # checkSumAdjustment
        0x5F0F3CF5,          # magicNumber
        3,                   # flags
        1000,                # unitsPerEm
        0, 0,                # created / modified
        0, -200, 1000, 800,  # xMin yMin xMax yMax
        0,                   # macStyle
        8,                   # lowestRecPPEM
        2,                   # fontDirectionHint
        0,                   # indexToLocFormat（CFF 无 loca，设 0）
        0,                   # glyphDataFormat
    )


def build_hhea() -> bytes:
    # 16 个 2 字节字段 + version；顺序严格按 OpenType hhea 规范
    fields = [
        800, -200, 0,        # ascender / descender / lineGap
        1000,                # advanceWidthMax
        0, 0, 0,             # minLeftSideBearing / minRightSideBearing / xMaxExtent
        1, 0, 0,             # caretSlopeRise / caretSlopeRun / caretOffset
        0, 0, 0, 0,          # reserved ×4
        0,                   # metricDataFormat
        2,                   # numberOfHMetrics
    ]
    assert len(fields) == 16, len(fields)
    return struct.pack(">I", 0x00010000) + struct.pack(">" + "h" * 3 + "H" + "h" * 11 + "H", *fields)


def build_maxp() -> bytes:
    return struct.pack(">IH", 0x00005000, 2)              # v0.5 + numGlyphs=2（CFF 用 0.5）


def build_hmtx() -> bytes:
    return struct.pack(">Hh", 1000, 0) + struct.pack(">Hh", 500, 0)


def build_sfnt(tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    header = struct.pack(">4sHHHH", b"OTTO", n, 16 * n, 0, 0)

    offset = 12 + 16 * n
    records = b""
    body = b""
    for tag in tags:
        data = _pad4(tables[tag])
        records += struct.pack(">4sIII", tag.encode("latin-1"), 0, offset + len(body), len(tables[tag]))
        body += data
    return header + records + body


def build() -> bytes:
    # 先算 CFF 里 CharStrings 的偏移：header(4) + name(9) + topdict(22) + string(2) + gsubr(2)
    charstrings_offset = 4 + 9 + 22 + 2 + 2
    cff = build_cff(charstrings_offset)
    tables = {
        "head": build_head(),
        "hhea": build_hhea(),
        "maxp": build_maxp(),
        "hmtx": build_hmtx(),
        "cmap": build_cmap(),
        "CFF ": cff,
    }
    return build_sfnt(tables)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("minimal-cff.otf")
    data = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{out}  {len(data)} 字节")
