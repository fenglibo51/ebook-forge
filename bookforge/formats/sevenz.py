"""7z 容器解析（纯标准库 lzma）。

只为了 CB7 / CBC 漫画包能读进来，不做压缩写入。

7z 的结构是「头在末尾」：

    [签名头 32 字节]  →  指向 NextHeader 的偏移/大小/CRC
    [压缩数据流]
    [Next Header]     ← 文件名、大小、编码链都在这里

Next Header 自己也可能被压缩（kEncodedHeader），要先解一层才看得到内容。

支持：Copy / LZMA / LZMA2 / BCJ(ARM/PPC/SPARC/IA64) / Delta 单链与多链。
不支持：BCJ2（四流交叉）、AES 加密 —— 明确报错。
"""

from __future__ import annotations

import lzma
from dataclasses import dataclass, field
from pathlib import Path

from . import limits

SIG = b"7z\xbc\xaf\x27\x1c"

# kHeader 系列
kEnd = 0x00
kHeader = 0x01
kArchiveProperties = 0x02
kAdditionalStreamsInfo = 0x03
kMainStreamsInfo = 0x04
kFilesInfo = 0x05
kPackInfo = 0x06
kUnpackInfo = 0x07
kSubStreamsInfo = 0x08
kSize = 0x09
kCRC = 0x0A
kFolder = 0x0B
kCodersUnpackSize = 0x0C
kNumUnpackStream = 0x0D
kEmptyStream = 0x0E
kEmptyFile = 0x0F
kName = 0x11
kEncodedHeader = 0x17

# codec id → lzma 过滤器
_CODECS = {
    0x00: None,                       # Copy
    0x21: lzma.FILTER_LZMA2,
    0x030101: lzma.FILTER_LZMA1,
    0x03030103: lzma.FILTER_X86,
    0x03030205: lzma.FILTER_POWERPC,
    0x03030401: lzma.FILTER_IA64,
    0x03030501: lzma.FILTER_ARM,
    0x03030701: lzma.FILTER_ARMTHUMB,
    0x03030805: lzma.FILTER_SPARC,
    0x03: lzma.FILTER_DELTA,
}
_UNSUPPORTED = {0x0303011B: "BCJ2"}


class SevenZError(ValueError):
    pass


class _Reader:
    __slots__ = ("d", "p")

    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise SevenZError("7z 头意外结束")
        v = self.d[self.p]
        self.p += 1
        return v

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise SevenZError("7z 数据被截断")
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def number(self) -> int:
        """7z 变长整数：首字节高位标记后续字节数"""
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if not (first & mask):
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def bits(self, count: int) -> list[bool]:
        out = []
        cur = 0
        mask = 0
        for _ in range(count):
            if mask == 0:
                cur = self.byte()
                mask = 0x80
            out.append(bool(cur & mask))
            mask >>= 1
        return out


@dataclass
class _Folder:
    coders: list[tuple[int, bytes, int, int]] = field(default_factory=list)  # (id, props, in, out)
    pack_indices: list[int] = field(default_factory=list)
    unpack_sizes: list[int] = field(default_factory=list)


@dataclass
class _StreamsInfo:
    pack_pos: int = 0
    pack_sizes: list[int] = field(default_factory=list)
    folders: list[_Folder] = field(default_factory=list)
    sub_counts: list[int] = field(default_factory=list)
    sub_sizes: list[int] = field(default_factory=list)
    # 哪些 folder 的 CRC 已经在 UnpackInfo 里给过（单 substream 的不再重复列）
    folder_crc: set[int] = field(default_factory=set)


def _read_digests(r: _Reader, count: int) -> list[bool]:
    """读一段 7z CRC 列表，返回哪些位有值（值本身丢掉，我们用不上）。

    ⚠️ 那个 AllAreDefined 字节必须读 —— 漏了它后面全错位
    （症状：解析主 header 时报「未知类型 112」这种鬼数字）。
    """
    if count <= 0:
        return []
    all_defined = r.byte()
    if all_defined:
        r.take(4 * count)
        return [True] * count
    flags = r.bits(count)
    for defined in flags:
        if defined:
            r.u32()

    return flags

def _read_pack_info(r: _Reader, si: _StreamsInfo) -> None:
    si.pack_pos = r.number()
    n = r.number()
    while True:
        t = r.number()
        if t == kEnd:
            break
        # ⚠️ 不能在这里先读一个 size：kSize/kCRC 后面跟的是列表，不是长度前缀。
        # 早先统一 `size = r.number()` 再分支，kSize 分支又读 n 个数字，
        # 等于多吃一个字节 → 后面整个头错位。
        if t == kSize:
            si.pack_sizes = [r.number() for _ in range(n)]
        elif t == kCRC:
            _read_digests(r, n)
        else:
            r.take(r.number())


def _read_folder(r: _Reader) -> _Folder:
    folder = _Folder()
    ncoders = r.number()
    total_in = total_out = 0
    for _ in range(ncoders):
        flags = r.byte()
        id_size = flags & 0x0F
        is_complex = bool(flags & 0x10)
        has_attrs = bool(flags & 0x20)
        cid = 0
        for b in r.take(id_size):
            cid = (cid << 8) | b
        nin = nout = 1
        if is_complex:
            nin = r.number()
            nout = r.number()
        props = b""
        if has_attrs:
            props = r.take(r.number())
        total_in += nin
        total_out += nout
        folder.coders.append((cid, props, nin, nout))

    bind_pairs = total_out - 1
    bound_ins: set[int] = set()
    for _ in range(bind_pairs):
        in_i = r.number()
        r.number()                      # out index
        bound_ins.add(in_i)

    npacked = total_in - bind_pairs
    if npacked == 1:
        # 只有一个包流时它不显式存储：取第一个没被 bind 的输入
        free = [i for i in range(total_in) if i not in bound_ins]
        folder.pack_indices = free[:1] or [0]
    else:
        folder.pack_indices = [r.number() for _ in range(npacked)]
    return folder


def _read_unpack_info(r: _Reader, si: _StreamsInfo) -> None:
    # ⚠️ UnpackInfo 后面**直接**跟 kFolder，没有 count 字段。
    # 照着 PackInfo 的样子在这里读一个 number() 会把 kFolder 标记吃掉，
    # 之后全错位（症状：7z 数据被截断）。
    while True:
        t = r.number()
        if t == kEnd:
            break
        if t == kFolder:
            nfolders = r.number()
            external = r.byte()
            if external:
                raise SevenZError("7z 外部文件夹定义不支持")
            si.folders = [_read_folder(r) for _ in range(nfolders)]
            t3 = r.number()
            if t3 != kCodersUnpackSize:
                raise SevenZError("7z 缺少 CodersUnpackSize")
            for f in si.folders:
                f.unpack_sizes = [r.number() for _ in f.coders]
        elif t == kCRC:
            defined = _read_digests(r, len(si.folders))
            si.folder_crc = {i for i, d in enumerate(defined) if d}
        else:
            size = r.number()
            r.take(size)


def _read_substreams(r: _Reader, si: _StreamsInfo) -> None:
    nfolders = len(si.folders)
    si.sub_counts = [1] * nfolders
    while True:
        t = r.number()
        if t == kEnd:
            break
        if t == kNumUnpackStream:
            si.sub_counts = [r.number() for _ in range(nfolders)]
        elif t == kSize:
            total = sum(si.sub_counts)
            sizes = []
            for i, cnt in enumerate(si.sub_counts):
                for _ in range(cnt - 1):
                    sizes.append(r.number())
                # 每个 folder 最后一个 substream 的大小由 unpack size 推出
                last = si.folders[i].unpack_sizes[-1] - sum(sizes[-cnt + 1:] if cnt > 1 else [])
                sizes.append(last)
            si.sub_sizes = sizes[:total]
        elif t == kCRC:
            # 每个 substream 一个 CRC；但「只有一个 stream 的 folder」用它自己的
            # folder CRC，不再单独列 → 数量要减掉这些 folder
            total = sum(si.sub_counts)
            # 单 substream 的 folder，若 CRC 已在 UnpackInfo 给过，这里就不重复；
            # 否则每个 substream 都要一份。早先无条件减去单流 folder 数，
            # 碰上「folder 自己没 CRC」的真实文件就会少读 4*N 字节 → 后面全错位。
            covered = sum(1 for i, c in enumerate(si.sub_counts)
                          if c == 1 and i in si.folder_crc)
            _read_digests(r, total - covered)
        else:
            r.take(r.number())


def _read_streams_info(r: _Reader) -> _StreamsInfo:
    si = _StreamsInfo()
    while True:
        t = r.number()
        if t == kEnd:
            break
        if t == kPackInfo:
            _read_pack_info(r, si)
        elif t == kUnpackInfo:
            _read_unpack_info(r, si)
        elif t == kSubStreamsInfo:
            _read_substreams(r, si)
        else:
            raise SevenZError(f"7z StreamsInfo 里未知的类型 {t}")
    if not si.sub_counts or len(si.sub_counts) != len(si.folders):
        si.sub_counts = [1] * len(si.folders)
    if not si.sub_sizes:
        si.sub_sizes = [f.unpack_sizes[-1] for f in si.folders]
    return si


def _read_files_info(r: _Reader) -> tuple[list[str], list[bool]]:
    n = r.number()
    names: list[str] = []
    empty: list[bool] = [False] * n
    while True:
        ptype = r.number()
        if ptype == kEnd:
            break
        size = r.number()
        start = r.p
        if ptype == kEmptyStream:
            empty = r.bits(n)
        elif ptype == kName:
            external = r.byte()
            if not external:
                raw = r.take(size - 1)
                # ⚠️ 不能 raw.split(b"\x00\x00")：UTF-16LE 里 ASCII 字符是
                # 「字符 + 00」，两个字符之间就可能凑出 00 00，切出来的名字会串行。
                # 必须按 2 字节步进，遇到 \x00\x00 才算结束符。
                names = []
                buf = bytearray()
                for i in range(0, len(raw) - 1, 2):
                    pair = raw[i:i + 2]
                    if pair == b"\x00\x00":
                        names.append(buf.decode("utf-16-le", "replace"))
                        buf = bytearray()
                    else:
                        buf += pair
                if buf:
                    names.append(buf.decode("utf-16-le", "replace"))
        r.p = start + size          # 跳过不关心的属性
    return names, empty


# 增量解压的每块大小。1MB 足够小，让峰值内存不随解压总量线性上涨
_CHUNK = 1 << 20


def _decode_folder(data: bytes, folder: _Folder, pack_sizes: list[int],
                   pack_offsets: list[int],
                   limit: int | None = None, pack_base: int = 0) -> bytes:
    if len(folder.pack_indices) != 1:
        raise SevenZError("7z 多包流（BCJ2 等）暂不支持")
    # pack_indices 是 folder **内部**的序号，不是全局包流下标。
    # 多 folder 的 7z 里每个 folder 都从 0 开始，直接用会把所有 folder
    # 都解成第一段数据（静默数据错误，不报错）。必须加外层累加的基准。
    idx = folder.pack_indices[0] + pack_base
    raw = data[pack_offsets[idx]:pack_offsets[idx] + pack_sizes[idx]]

    if len(folder.coders) == 1 and folder.coders[0][0] == 0x00:
        return raw                                  # Copy

    filters = []
    for cid, props, _nin, _nout in reversed(folder.coders):   # 编码顺序的反向是解码顺序
        if cid in _UNSUPPORTED:
            raise SevenZError(f"7z 编码 {_UNSUPPORTED[cid]} 暂不支持")
        filt = _CODECS.get(cid)
        if filt is None:
            raise SevenZError(f"7z 未知编码 id 0x{cid:x}")
        entry = {"id": filt}
        if cid in (0x21, 0x030101) and len(props) >= 5:
            # LZMA 属性：1 字节 lc/lp/pb + 4 字节 dict size
            lc_lp_pb = props[0]
            dic = int.from_bytes(props[1:5], "little")
            entry["lc"] = lc_lp_pb % 9
            entry["lp"] = (lc_lp_pb // 9) % 5
            entry["pb"] = lc_lp_pb // 45
            if dic:
                entry["dict_size"] = dic
        filters.append(entry)

    # ⚠️ 不能再用 lzma.decompress(...) 一次性解完：
    # 那样上限只能事后检查，一个炸弹 folder 在解压过程中就已经把内存吃满。
    # 改成增量解码，每块 1MB，累计一超限立刻停手。
    if limit is None:
        limit = limits.MAX_TOTAL_BYTES
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    out = bytearray()
    src = raw
    try:
        while True:
            chunk = dec.decompress(src, max_length=_CHUNK)
            src = b""                       # 一次喂完，之后靠 output 缓冲
            if chunk:
                out += chunk
                if len(out) > limit:
                    raise SevenZError(
                        f"7z 单个数据块解压超过 {limit / 2**30:.1f} GB 上限，"
                        "已中止（解压炸弹防护）")
            if dec.eof:
                break
            if not chunk and dec.needs_input:
                raise SevenZError("7z 数据流意外结束（输入已耗尽但没有解压完）")
    except lzma.LZMAError as e:
        raise SevenZError(f"7z 解压失败：{e}")
    return bytes(out)


def _pack_offsets(si: _StreamsInfo) -> list[int]:
    """打包流在文件里的绝对起点。

    ⚠️ PackPos 是**相对签名头之后**（偏移 32）的位置，不是相对文件开头。
    直接拿 32 当起点会读错几十字节，解压报 `Corrupt input data`。
    """
    start = 32 + si.pack_pos
    offsets = [start]
    for s in si.pack_sizes[:-1]:
        offsets.append(offsets[-1] + s)
    return offsets


def read_all(path: str | Path, *,
             max_total: int | None = None,
             max_entry: int | None = None,
             max_entries: int | None = None) -> dict[str, bytes]:
    """把 7z 里所有文件解出来 → {名字: 内容}。

    max_total / max_entry / max_entries：解压前按 7z 头里的声明大小拒绝炸弹包。

    ⚠️ 默认值取 limits 里的那一套，不要在这里另写一份常量。
    早先这里硬编码 4GB/256MB/5000，和 limits.py 各说各话：
    改环境变量只能改到一半，测 CB7 限额时改了 limits 也完全不起作用。
    """
    max_total = limits.MAX_TOTAL_BYTES if max_total is None else max_total
    max_entry = limits.MAX_ENTRY_BYTES if max_entry is None else max_entry
    max_entries = limits.MAX_ENTRIES if max_entries is None else max_entries
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(SIG):
        raise SevenZError("不是 7z 文件（签名不对）")

    r = _Reader(data, 6)
    r.take(2)                       # version
    r.u32()                         # start header crc
    next_off = r.u64()
    next_size = r.u64()
    r.u32()                         # next header crc

    pos = 32 + next_off
    header = data[pos:pos + next_size]
    if not header:
        raise SevenZError("7z 头为空")

    hr = _Reader(header)
    kind = hr.number()
    if kind == kEncodedHeader:
        # 头本身被压缩了一层
        enc_si = _read_streams_info(hr)
        if not enc_si.folders:
            raise SevenZError("7z 编码头里没有文件夹")
        # 编码头本身也可能是个炸弹（头声明「我要解出 4GB」）。
        # 但头正常就几 KB，用正文的额度去放它太宽松 —— 单独卡到单条目上限。
        header = _decode_folder(data, enc_si.folders[0], enc_si.pack_sizes,
                                _pack_offsets(enc_si), limit=max_entry)
        hr = _Reader(header)
        kind = hr.number()
    if kind != kHeader:
        raise SevenZError(f"7z 头类型异常：{kind}")

    streams: _StreamsInfo | None = None
    names: list[str] = []
    empty: list[bool] = []
    while True:
        t = hr.number()
        if t == kEnd:
            break
        if t == kMainStreamsInfo:
            streams = _read_streams_info(hr)
        elif t == kFilesInfo:
            names, empty = _read_files_info(hr)
        else:
            raise SevenZError(f"7z 头里未知的类型 {t}")

    if streams is None or not names:
        raise SevenZError("7z 里没有文件")

    # ── 解压限制 ──────────────────────────────────────────────────────
    # 7z 头里声明了每个 folder 的解压大小，解压前就能拒绝炸弹包。
    # 之前没有这道检查：一个 100KB 的 7z 能声明解压出几十 GB，直接把内存吃干。
    if len(names) > max_entries:
        raise SevenZError(f"7z 里条目太多（{len(names)} > {max_entries}）")
    declared = sum((f.unpack_sizes[-1] if f.unpack_sizes else 0)
                   for f in streams.folders)
    if declared > max_total:
        raise SevenZError(
            f"7z 声明解压后 {declared / 1048576:.0f} MB，超过上限 {max_total / 1048576:.0f} MB")
    for s in streams.sub_sizes or []:
        if s > max_entry:
            raise SevenZError(
                f"7z 里有单个条目声明 {s / 1048576:.0f} MB，超过单文件上限 {max_entry / 1048576:.0f} MB")

    pack_offsets = _pack_offsets(streams)

    blobs: list[bytes] = []
    actual = 0
    pack_base = 0
    for folder in streams.folders:
        blob = _decode_folder(data, folder, streams.pack_sizes, pack_offsets,
                              limit=max_total, pack_base=pack_base)
        pack_base += max(1, len(folder.pack_indices))
        actual += len(blob)
        # 头部声明的大小可以撒谎，这里按**实际解压出来的字节数**再卡一次。
        # 注意：lzma.decompress 是一次性调用，解压过程中拦不住 ——
        # 这段只能保证「解出来之后不再继续用它」，真正省内存还得靠头部声明那一道。
        if actual > max_total:
            raise SevenZError(
                f"7z 实际解压出 {actual / 1048576:.0f} MB，超过上限 "
                f"{max_total / 1048576:.0f} MB（头部声明的 {declared / 1048576:.0f} MB 不可信）")
        blobs.append(blob)

    # 按 substream 切分并分配给非空文件
    out: dict[str, bytes] = {}
    flat: list[bytes] = []
    for blob, cnt in zip(blobs, streams.sub_counts):
        if cnt <= 1:
            flat.append(blob)
        else:
            sizes = streams.sub_sizes[:cnt]
            streams.sub_sizes = streams.sub_sizes[cnt:]
            pos2 = 0
            for s in sizes:
                flat.append(blob[pos2:pos2 + s])
                pos2 += s

    it = iter(flat)
    for name, is_empty in zip(names, empty):
        if is_empty:
            continue
        try:
            out[name] = next(it)
        except StopIteration:
            break
    return out
