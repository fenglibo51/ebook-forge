"""最小 G4 编码器 —— 只用于测试 ccitt.decode_g4。

真实扫描件几乎不用它（我们不会重新编码扫描件），但需要一个「已知答案」
的生成器来做往返验证：编码 → 解码 → 比像素。

支持：H（水平）模式 + V0。够覆盖 run 码表、行缓冲、变化点语义。
不支持的（P / VR1-3 / VL1-3）在真实数据里才会出现，靠真实样本验证。
"""

from __future__ import annotations

from .ccitt import _BLACK_MAKEUP, _BLACK_TERM, _EXT_MAKEUP, _WHITE_MAKEUP, _WHITE_TERM


class _BitWriter:
    def __init__(self):
        self.bits: list[str] = []

    def write(self, code: str) -> None:
        self.bits.append(code)

    def bytes(self) -> bytes:
        s = "".join(self.bits)
        s += "0" * ((8 - len(s) % 8) % 8)
        return bytes(int(s[i:i + 8], 2) for i in range(0, len(s), 8))


_TERM_INV = {}
for color, table in ((0, _WHITE_TERM), (1, _BLACK_TERM)):
    d = {}
    for (n, code), run in table.items():
        d[(color, run)] = format(code, f"0{n}b")
    _TERM_INV.update(d)

_MAKEUP_INV = {}
for color, table in ((0, _WHITE_MAKEUP), (1, _BLACK_MAKEUP)):
    for (n, code), run in table.items():
        _MAKEUP_INV[(color, run)] = format(code, f"0{n}b")
for (n, code), run in _EXT_MAKEUP.items():
    _MAKEUP_INV[(0, run)] = format(code, f"0{n}b")
    _MAKEUP_INV[(1, run)] = format(code, f"0{n}b")


def _run_code(color: int, length: int) -> str:
    out = []
    while length >= 2624:
        out.append(_MAKEUP_INV[(color, 2560)])
        length -= 2560
    makeup = 0
    if length >= 64:
        makeup = (length // 64) * 64
        if makeup > 2560:
            makeup = 2560
        out.append(_MAKEUP_INV[(color, makeup)])
        length -= makeup
    out.append(_TERM_INV[(color, length)])
    return "".join(out)


def encode_g4_rows(rows: list[list[int]], columns: int) -> bytes:
    """rows: 每行是变化点列表（行首白，changes[0] 处转黑），按 T.6 语义编码。

    全白行用 V0（1 个 bit）；其它行全部用 H 模式。
    **注意：一个 H 模式只携带两个 run（白 + 黑）**，所以一行要发多次 H。
    这一点写错过 —— 把整行的 run 全塞在一个 H 后面，解码器只读两个就去找
    下一个模式码，后面全错位。
    """
    bw = _BitWriter()
    for changes in rows:
        if not changes:
            bw.write("1")                    # V0：与参考行一致（全白）
            continue
        # 拆成 (颜色, 长度) 序列
        runs: list[tuple[int, int]] = []
        pos = 0
        color = 0
        for c in changes + [columns]:
            runs.append((color, max(0, c - pos)))
            pos = c
            color ^= 1
        if len(runs) % 2:
            runs.append((color, 0))          # 补齐成对，行尾补 0 长度黑 run

        for i in range(0, len(runs), 2):
            bw.write("001")                  # H
            bw.write(_run_code(*runs[i]))
            bw.write(_run_code(*runs[i + 1]))
    return bw.bytes()


def encode_bitmap(bitmap: list[list[int]], columns: int) -> bytes:
    """bitmap[y][x] = 0 白 / 1 黑 → G4 字节流"""
    rows = []
    for line in bitmap:
        changes = []
        prev = 0
        for x, v in enumerate(line):
            if v != prev:
                changes.append(x)
                prev = v
        rows.append(changes)
    return encode_g4_rows(rows, columns)
