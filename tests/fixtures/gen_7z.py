"""造最小的合法 7z 文件，供测试走真实的 sevenz.read_all()。

为什么需要它：早先的 7z 限额测试是在测试函数里把生产检查逻辑抄一遍再主动抛异常，
生产代码被删掉测试照样绿。要真验证，就必须有一个七个字节都合法的 7z 让读取器真跑。

只支持一种形态：每个文件一个 folder、无 SubStreamsInfo（读取器对单 substream
folder 有默认推导，省略这段是合法的）。编码器可选 Copy 或 LZMA1。
"""
from __future__ import annotations

import lzma
import struct
import zlib

SIG = b"7z\xbc\xaf\x27\x1c"

kEnd, kHeader, kMainStreamsInfo, kFilesInfo = 0x00, 0x01, 0x04, 0x05
kPackInfo, kUnpackInfo, kSubStreamsInfo = 0x06, 0x07, 0x08
kSize, kFolder, kCodersUnpackSize = 0x09, 0x0B, 0x0C
kNumUnPackStream = 0x0D
kName = 0x11


def num(v: int) -> bytes:
    """7z 变长整数编码：首字节用前导 1 的个数表示后续跟随几个字节。"""
    for i in range(9):
        if i == 8:
            return b"\xff" + v.to_bytes(8, "little")
        if v < (1 << (8 * i + (7 - i))):
            prefix = (0xFF << (8 - i)) & 0xFF
            first = prefix | (v >> (8 * i))
            return bytes([first & 0xFF]) + (v & ((1 << (8 * i)) - 1)).to_bytes(i, "little")
    raise AssertionError


def _lzma_props(dict_size: int = 1 << 16) -> bytes:
    # lc=3 lp=0 pb=2 → 3 + 0*9 + 2*45 = 93；后 4 字节是字典大小
    return bytes([93]) + dict_size.to_bytes(4, "little")


def build(files: dict[str, bytes], *, use_lzma: bool = False,
          declared_unpack: dict[str, int] | None = None) -> bytes:
    """返回一个 7z 文件字节串。

    use_lzma=False → Copy 编码（内容原样）
    declared_unpack  → 在头里谎报解压后大小（测「声明不可信」路径）
    """
    packs: list[bytes] = []
    unpack_sizes: list[int] = []
    folder_bytes: list[bytes] = []

    for name in files:
        raw = files[name]
        if use_lzma:
            dict_size = 1 << 16
            comp = lzma.compress(raw, format=lzma.FORMAT_RAW,
                                 filters=[{"id": lzma.FILTER_LZMA1,
                                           "dict_size": dict_size,
                                           "lc": 3, "lp": 0, "pb": 2}])
            props = _lzma_props(dict_size)
            # flags: 低 4 位 = id 字节数(3)，0x20 = 有属性
            coder = bytes([0x23]) + b"\x03\x01\x01" + num(len(props)) + props
        else:
            comp = raw
            coder = bytes([0x01]) + b"\x00"        # id 0x00 = Copy，1 字节 id
        packs.append(comp)
        real = len(raw) if declared_unpack is None else declared_unpack[name]
        unpack_sizes.append(real)
        folder_bytes.append(num(1) + coder)        # ncoders=1 + coder

    pack_data = b"".join(packs)

    h = bytearray()
    h += num(kHeader)
    h += num(kMainStreamsInfo)

    # ── PackInfo ──
    h += num(kPackInfo)
    h += num(0)                                    # pack_pos
    h += num(len(packs))
    h += num(kSize)
    for p in packs:
        h += num(len(p))
    h += num(kEnd)

    # ── UnpackInfo ──
    h += num(kUnpackInfo)
    h += num(kFolder)
    h += num(len(folder_bytes))
    h += b"\x00"                                   # external = 0
    for fb in folder_bytes:
        h += fb
    h += num(kCodersUnpackSize)
    for u in unpack_sizes:
        h += num(u)
    h += num(kEnd)                                 # 结束 UnpackInfo
    # ── SubStreamsInfo ──（必填：py7zr 会校，缺了直接读不了）
    h += num(kSubStreamsInfo)
    h += num(kNumUnPackStream)
    for _ in files:                                # 每个 folder 一个 substream
        h += num(1)
    h += num(kEnd)
    h += num(kEnd)                                 # 结束 StreamsInfo

    # ── FilesInfo ──
    h += num(kFilesInfo)
    h += num(len(files))
    raw_names = b"".join(n.encode("utf-16-le") + b"\x00\x00" for n in files)
    h += num(kName)
    h += num(len(raw_names) + 1)
    h += b"\x00"                                   # external = 0
    h += raw_names
    h += num(kEnd)
    h += num(kEnd)                                 # 结束 Header

    header = bytes(h)
    start_header = struct.pack("<QQI", len(pack_data), len(header),
                               zlib.crc32(header) & 0xFFFFFFFF)
    return (SIG + b"\x00\x04" + struct.pack("<I", zlib.crc32(start_header) & 0xFFFFFFFF)
            + start_header + pack_data + header)
