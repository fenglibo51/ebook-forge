"""CCITT G3 / G4 传真编码解码（纯标准库）。

PDF 里的扫描件大量用 CCITTFaxDecode，最常见是 K=-1（G4 / T.6，二维编码）。
不实现它，P1「PDF → CBZ 不依赖 poppler」就是空话。

输出：1bit 位图，每行按字节对齐、MSB 在左（和 PNG 位深 1 的存储一致）。
      0 = 白，1 = 黑。

参考规范：ITU-T T.4（一维 run 编码）+ T.6（二维编码）。
码表是标准表，不是猜的；解码结果用真实扫描 PDF 对渲染器逐像素验证过。
"""

from __future__ import annotations

import bisect

WHITE = 0
BLACK = 1


class CCITTError(ValueError):
    pass


# ── 码表：码字 → run 长度 ─────────────────────────────────────────────────
# 键是 (位数, 码值)，值是 run 长度

_WHITE_TERM = {
    (8, 0b00110101): 0, (6, 0b000111): 1, (4, 0b0111): 2, (4, 0b1000): 3,
    (4, 0b1011): 4, (4, 0b1100): 5, (4, 0b1110): 6, (4, 0b1111): 7,
    (5, 0b10011): 8, (5, 0b10100): 9, (5, 0b00111): 10, (5, 0b01000): 11,
    (6, 0b001000): 12, (6, 0b000011): 13, (6, 0b110100): 14, (6, 0b110101): 15,
    (6, 0b101010): 16, (6, 0b101011): 17, (7, 0b0100111): 18, (7, 0b0001100): 19,
    (7, 0b0001000): 20, (7, 0b0010111): 21, (7, 0b0000011): 22, (7, 0b0000100): 23,
    (7, 0b0101000): 24, (7, 0b0101011): 25, (7, 0b0010011): 26, (7, 0b0100100): 27,
    (7, 0b0011000): 28, (8, 0b00000010): 29, (8, 0b00000011): 30, (8, 0b00011010): 31,
    (8, 0b00011011): 32, (8, 0b00010010): 33, (8, 0b00010011): 34, (8, 0b00010100): 35,
    (8, 0b00010101): 36, (8, 0b00010110): 37, (8, 0b00010111): 38, (8, 0b00101000): 39,
    (8, 0b00101001): 40, (8, 0b00101010): 41, (8, 0b00101011): 42, (8, 0b00101100): 43,
    (8, 0b00101101): 44, (8, 0b00000100): 45, (8, 0b00000101): 46, (8, 0b00001010): 47,
    (8, 0b00001011): 48, (8, 0b01010010): 49, (8, 0b01010011): 50, (8, 0b01010100): 51,
    (8, 0b01010101): 52, (8, 0b00100100): 53, (8, 0b00100101): 54, (8, 0b01011000): 55,
    (8, 0b01011001): 56, (8, 0b01011010): 57, (8, 0b01011011): 58, (8, 0b01001010): 59,
    (8, 0b01001011): 60, (8, 0b00110010): 61, (8, 0b00110011): 62, (8, 0b00110100): 63,
}

_WHITE_MAKEUP = {
    (5, 0b11011): 64, (5, 0b10010): 128, (6, 0b010111): 192,
    (7, 0b0110111): 256, (8, 0b00110110): 320, (8, 0b00110111): 384,
    (8, 0b01100100): 448, (8, 0b01100101): 512, (8, 0b01101000): 576,
    (8, 0b01100111): 640, (9, 0b011001100): 704, (9, 0b011001101): 768,
    (9, 0b011010010): 832, (9, 0b011010011): 896, (9, 0b011010100): 960,
    (9, 0b011010101): 1024, (9, 0b011010110): 1088, (9, 0b011010111): 1152,
    (9, 0b011011000): 1216, (9, 0b011011001): 1280, (9, 0b011011010): 1344,
    (9, 0b011011011): 1408, (9, 0b010011000): 1472, (9, 0b010011001): 1536,
    (9, 0b010011010): 1600, (6, 0b011000): 1664, (9, 0b010011011): 1728,
}

_BLACK_TERM = {
    (10, 0b0000110111): 0, (3, 0b010): 1, (2, 0b11): 2, (2, 0b10): 3,
    (3, 0b011): 4, (4, 0b0011): 5, (4, 0b0010): 6, (5, 0b00011): 7,
    (6, 0b000101): 8, (6, 0b000100): 9, (7, 0b0000100): 10, (7, 0b0000101): 11,
    (7, 0b0000111): 12, (8, 0b00000100): 13, (8, 0b00000111): 14, (9, 0b000011000): 15,
    (10, 0b0000010111): 16, (10, 0b0000011000): 17, (10, 0b0000001000): 18,
    (11, 0b00001100111): 19, (11, 0b00001101000): 20, (11, 0b00001101100): 21,
    (11, 0b00000110111): 22, (11, 0b00000101000): 23, (11, 0b00000010111): 24,
    (11, 0b00000011000): 25, (12, 0b000011001010): 26, (12, 0b000011001011): 27,
    (12, 0b000011001100): 28, (12, 0b000011001101): 29, (12, 0b000001101000): 30,
    (12, 0b000001101001): 31, (12, 0b000001101010): 32, (12, 0b000001101011): 33,
    (12, 0b000011010010): 34, (12, 0b000011010011): 35, (12, 0b000011010100): 36,
    (12, 0b000011010101): 37, (12, 0b000011010110): 38, (12, 0b000011010111): 39,
    (12, 0b000001101100): 40, (12, 0b000001101101): 41, (12, 0b000011011010): 42,
    (12, 0b000011011011): 43, (12, 0b000001010100): 44, (12, 0b000001010101): 45,
    (12, 0b000001010110): 46, (12, 0b000001010111): 47, (12, 0b000001100100): 48,
    (12, 0b000001100101): 49, (12, 0b000001010010): 50, (12, 0b000001010011): 51,
    (12, 0b000000100100): 52, (12, 0b000000110111): 53, (12, 0b000000111000): 54,
    (12, 0b000000100111): 55, (12, 0b000000101000): 56, (12, 0b000001011000): 57,
    (12, 0b000001011001): 58, (12, 0b000000101011): 59, (12, 0b000000101100): 60,
    (12, 0b000001011010): 61, (12, 0b000001100110): 62, (12, 0b000001100111): 63,
}

_BLACK_MAKEUP = {
    (10, 0b0000001111): 64, (12, 0b000011001000): 128, (12, 0b000011001001): 192,
    (12, 0b000001011011): 256, (12, 0b000000110011): 320, (12, 0b000000110100): 384,
    (12, 0b000000110101): 448, (13, 0b0000001101100): 512, (13, 0b0000001101101): 576,
    (13, 0b0000001001010): 640, (13, 0b0000001001011): 704, (13, 0b0000001001100): 768,
    (13, 0b0000001001101): 832, (13, 0b0000001110010): 896, (13, 0b0000001110011): 960,
    (13, 0b0000001110100): 1024, (13, 0b0000001110101): 1088, (13, 0b0000001110110): 1152,
    (13, 0b0000001110111): 1216, (13, 0b0000001010010): 1280, (13, 0b0000001010011): 1344,
    (13, 0b0000001010100): 1408, (13, 0b0000001010101): 1472, (13, 0b0000001011010): 1536,
    (13, 0b0000001011011): 1600, (13, 0b0000001100100): 1664, (13, 0b0000001100101): 1728,
}

# 扩展 makeup（黑白通用）：1792 ~ 2560
_EXT_MAKEUP = {
    (11, 0b00000001000): 1792, (11, 0b00000001100): 1856, (11, 0b00000001101): 1920,
    (12, 0b000000010010): 1984, (12, 0b000000010011): 2048, (12, 0b000000010100): 2112,
    (12, 0b000000010101): 2176, (12, 0b000000010110): 2240, (12, 0b000000010111): 2304,
    (12, 0b000000011100): 2368, (12, 0b000000011101): 2432, (12, 0b000000011110): 2496,
    (12, 0b000000011111): 2560,
}

_TERM = {WHITE: _WHITE_TERM, BLACK: _BLACK_TERM}
_MAKEUP = {WHITE: _WHITE_MAKEUP, BLACK: _BLACK_MAKEUP}

# 每种颜色一张合并表：{(位数, 码值): ("term"|"makeup", run)}
# 分开查两遍的写法有个致命 bug —— 查终止码失败时比特已经被消耗，后面全错位。
_RUN_TABLE: dict[int, dict] = {}
for _color in (WHITE, BLACK):
    _d: dict = {}
    for _k, _v in _TERM[_color].items():
        _d[_k] = ("term", _v)
    for _k, _v in _MAKEUP[_color].items():
        if _k in _d:
            raise AssertionError(f"CCITT 码表冲突 {_k}")
        _d[_k] = ("makeup", _v)
    for _k, _v in _EXT_MAKEUP.items():
        if _k in _d:
            raise AssertionError(f"CCITT 扩展码冲突 {_k}")
        _d[_k] = ("makeup", _v)
    _RUN_TABLE[_color] = _d

# 二维模式码（T.6）
# (位数, 码值) → (类型, 垂直偏移)
_MODES = {
    (1, 0b1): ("V", 0),
    (3, 0b011): ("V", 1), (3, 0b010): ("V", -1),
    (3, 0b001): ("H", 0),
    (4, 0b0001): ("P", 0),
    (6, 0b000011): ("V", 2), (6, 0b000010): ("V", -2),
    (7, 0b0000011): ("V", 3), (7, 0b0000010): ("V", -3),
    (7, 0b0000001): ("EXT", 0),
}


class _BitReader:
    """带位缓冲的读取器。

    G4 解码是逐码字查表的，如果每次查表都逐位试（1..13 位），
    32MP 一页要 7 秒。改成「预读 13 位 + 一次查表」之后快 3~4 倍。
    """
    __slots__ = ("data", "pos", "nbits", "cache", "have")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.nbits = len(data) * 8
        self.cache = 0
        self.have = 0

    def _fill(self) -> None:
        while self.have < 24 and self.pos < len(self.data):
            self.cache = ((self.cache << 8) | self.data[self.pos]) & 0xFFFFFFFF
            self.pos += 1
            self.have += 8

    def peek13(self) -> int:
        """看接下来 13 位（不足时低位补 0）"""
        if self.have < 13:
            self._fill()
        if self.have >= 13:
            return (self.cache >> (self.have - 13)) & 0x1FFF
        v = self.cache & ((1 << self.have) - 1) if self.have else 0
        return (v << (13 - self.have)) & 0x1FFF

    def skip(self, n: int) -> None:
        self.have -= n
        if self.have < 0:
            self.have = 0

    def bit(self) -> int:
        if self.have < 1:
            self._fill()
        if self.have < 1:
            raise CCITTError("数据提前结束")
        b = (self.cache >> (self.have - 1)) & 1
        self.have -= 1
        return b

    def align(self) -> None:
        used = (self.pos * 8) - self.have
        target = (used + 7) & ~7
        drop = target - used
        if drop > 0:
            self.skip(drop)

    @property
    def eof(self) -> bool:
        return self.pos * 8 - self.have >= self.nbits


# 13 位预查表：把「短码 + 长码」摊平成一次查表
def _build_lookup(table: dict) -> list:
    """13 位前缀 → (码长, 键)。

    先填长码再填短码，保证短码不覆盖长码已经占用的前缀（最长匹配）。
    """
    lut: list = [None] * 8192
    for key in sorted(table, key=lambda k: -k[0]):
        n, code = key
        if n > 13:
            continue
        shift = 13 - n
        start = code << shift
        for i in range(start, start + (1 << shift)):
            if lut[i] is None:
                lut[i] = (n, key)
    return lut


_RUN_LUT = {WHITE: _build_lookup(_RUN_TABLE[WHITE]),
            BLACK: _build_lookup(_RUN_TABLE[BLACK])}
_MODE_LUT = _build_lookup(_MODES)


def _read_code_tail(br: _BitReader, table: dict):
    """末尾兜底：逐位试，成功才推进（失败回滚到原状态）。

    查表路径要求手里有 13 位，数据尾巴常常不够 —— 直接放弃会丢掉最后一两个码，
    解码出来少一条 run（实测「竖条纹」这种变化点密集的图会整行错）。
    """
    saved = (br.cache, br.have, br.pos)
    acc = 0
    for n in range(1, 14):
        if br.have < 1:
            br._fill()
        if br.have < 1:
            break
        acc = (acc << 1) | br.bit()
        if (n, acc) in table:
            return (n, acc)
    br.cache, br.have, br.pos = saved
    return None


def _read_mode(br: _BitReader):
    """读一个二维模式码（查表 + 末尾兜底）。"""
    if br.have < 13:
        br._fill()
    if br.have >= 13:
        e = _MODE_LUT[br.peek13()]
        if e is not None:
            n, key = e
            br.skip(n)
            return key
    return _read_code_tail(br, _MODES)


def _read_code(br: _BitReader, lut: list) -> int | None:
    """查表读一个码字（不破坏性：匹配不到不推进）。"""
    if br.have < 13:
        br._fill()
    if br.have < 13:
        return None
    e = lut[br.peek13()]
    if e is None:
        return None
    n, key = e
    br.skip(n)
    return key


def _read_run(br: _BitReader, color: int) -> int:
    """读一个 run 长度：makeup（可多个）+ 一个 terminating"""
    total = 0
    table = _RUN_TABLE[color]
    lut = _RUN_LUT[color]
    for _ in range(64):
        key = _read_code(br, lut)
        if key is None:
            key = _read_code_tail(br, table)     # 数据尾巴
        if key is None:
            raise CCITTError("run 码无法解析")
        kind, value = table[key]
        total += value
        if kind == "term":
            return total
    raise CCITTError("makeup 码过多")


def _find_b1_parts(even: list, odd: list, a0: int, color: int, columns: int) -> int:
    """参考行里第一个 > a0、且奇偶性与 color 一致的变化点。

    ⚠️ 早先是「从列表头线性扫」，实测一页 32MP 的蒙版要调 121 万次、
    吃掉 52% 的总耗时（13 秒）。变化点列表本身是递增的，改成二分即可。
    """
    lst = even if color == 0 else odd
    i = bisect.bisect_right(lst, a0)
    return lst[i] if i < len(lst) else columns


def _next_change_parts(ref: list, pos: int, columns: int) -> int:
    i = bisect.bisect_right(ref, pos)
    return ref[i] if i < len(ref) else columns


def _find_b1(ref: list[int], a0: int, color: int, columns: int) -> int:
    """兼容旧签名（内部走二分）。"""
    return _find_b1_parts(ref[0::2], ref[1::2], a0, color, columns)


def _next_change(ref: list[int], pos: int, columns: int) -> int:
    for v in ref:
        if v > pos:
            return v
    return columns


def _row_from_changes(changes: list[int], columns: int) -> bytearray:
    """变化点 → 1bit 位图行（行首是白，changes[0] 处转黑）。

    按字节填充而不是逐像素置位：6918 行 × 4634 列的点操作要 2.5 秒，
    改成整字节 0xFF + 两端掩码后几乎不耗时。
    """
    row = bytearray((columns + 7) // 8)
    for i in range(0, len(changes), 2):
        s = changes[i]
        e = changes[i + 1] if i + 1 < len(changes) else columns
        if s >= columns:
            break
        if e > columns:
            e = columns
        if e <= s:
            continue
        b0, b1 = s >> 3, (e - 1) >> 3
        if b0 == b1:
            row[b0] |= (0xFF >> (s & 7)) & (0xFF << (7 - ((e - 1) & 7)) & 0xFF)
        else:
            row[b0] |= 0xFF >> (s & 7)
            if b1 > b0 + 1:
                row[b0 + 1:b1] = b"\xff" * (b1 - b0 - 1)
            row[b1] |= (0xFF << (7 - ((e - 1) & 7))) & 0xFF
    return row


def decode_g4(data: bytes, columns: int, rows: int) -> bytes:
    """G4（T.6）二维编码 → 1bit 位图"""
    if columns <= 0 or rows <= 0:
        raise CCITTError("CCITT 缺少 Columns / Rows")
    br = _BitReader(data)
    rowbytes = (columns + 7) // 8
    out = bytearray(rowbytes * rows)
    ref = [columns, columns]     # 起始参考行：整行白
    ref_even = ref[0::2]
    ref_odd = ref[1::2]

    for y in range(rows):
        changes: list[int] = []
        a0 = -1
        color = WHITE
        guard = 0
        while a0 < columns:
            guard += 1
            if guard > columns * 4 + 64:
                raise CCITTError(f"第 {y} 行解码死循环")
            key = _read_mode(br)
            if key is None:
                # 数据结束：剩余行按空白填充
                return bytes(out)
            kind, delta = _MODES[key]

            if kind == "P":                     # pass：整段与参考行相同，跳过
                b1 = _find_b1_parts(ref_even, ref_odd, a0, color, columns)
                a0 = _next_change_parts(ref, b1, columns)
            elif kind == "H":                   # horizontal：两个 run
                if a0 < 0:
                    a0 = 0
                r1 = _read_run(br, color)
                r2 = _read_run(br, 1 - color)
                changes.append(min(a0 + r1, columns))
                changes.append(min(a0 + r1 + r2, columns))
                a0 = a0 + r1 + r2
            elif kind == "V":                   # vertical：相对参考行偏移
                b1 = _find_b1_parts(ref_even, ref_odd, a0, color, columns)
                a0 = b1 + delta
                changes.append(a0)
                color = 1 - color
            else:                               # EXT
                raise CCITTError("不支持 CCITT 2D 扩展码")

        row = _row_from_changes(changes, columns)
        out[y * rowbytes:(y + 1) * rowbytes] = row
        ref = changes if changes else [columns, columns]
        ref_even = ref[0::2]
        ref_odd = ref[1::2]

    return bytes(out)


def decode_g3(data: bytes, columns: int, rows: int, is_1d: bool = True) -> bytes:
    """G3（T.4）。1D 最常见；K>0 的混合模式只处理每行的 1D/2D 标志位。"""
    if columns <= 0 or rows <= 0:
        raise CCITTError("CCITT 缺少 Columns / Rows")
    br = _BitReader(data)
    rowbytes = (columns + 7) // 8
    out = bytearray(rowbytes * rows)
    ref = [columns, columns]
    ref_even = ref[0::2]
    ref_odd = ref[1::2]

    for y in range(rows):
        if not is_1d:
            try:
                flag = br.bit()             # 1 = 1D 行，0 = 2D 行
            except CCITTError:
                break
            if flag == 0:
                # 2D 行，复用 G4 的单行逻辑
                changes, a0, color = [], -1, WHITE
                guard = 0
                while a0 < columns:
                    guard += 1
                    if guard > columns * 4 + 64:
                        raise CCITTError(f"第 {y} 行解码死循环")
                    key = _read_mode(br)
                    if key is None:
                        return bytes(out)
                    kind, delta = _MODES[key]
                    if kind == "P":
                        b1 = _find_b1_parts(ref_even, ref_odd, a0, color, columns)
                        a0 = _next_change_parts(ref, b1, columns)
                    elif kind == "H":
                        if a0 < 0:
                            a0 = 0
                        r1 = _read_run(br, color)
                        r2 = _read_run(br, 1 - color)
                        changes.append(min(a0 + r1, columns))
                        changes.append(min(a0 + r1 + r2, columns))
                        a0 = a0 + r1 + r2
                    elif kind == "V":
                        b1 = _find_b1_parts(ref_even, ref_odd, a0, color, columns)
                        a0 = b1 + delta
                        changes.append(a0)
                        color = 1 - color
                    else:
                        raise CCITTError("不支持 CCITT 2D 扩展码")
                out[y * rowbytes:(y + 1) * rowbytes] = _row_from_changes(changes, columns)
                ref = changes if changes else [columns, columns]
                br.align()
                continue

        # 1D 行：交替读白 / 黑 run
        changes: list[int] = []
        pos = 0
        color = WHITE
        while pos < columns:
            r = _read_run(br, color)
            pos += r
            changes.append(min(pos, columns))
            color = 1 - color
            if r == 0 and len(changes) > columns * 2:
                break
        out[y * rowbytes:(y + 1) * rowbytes] = _row_from_changes(changes, columns)
        ref = changes if changes else [columns, columns]
        br.align()

    return bytes(out)
