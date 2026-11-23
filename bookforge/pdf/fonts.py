"""TrueType 字体解析与子集化（纯标准库）。

只为了一件事：把「这本书用到的字」从 10MB 的 Noto Sans SC 里抠出来，
做成一个几十 KB 的子集字体嵌进 PDF。不子集化的话每本书都要背 10MB。

只支持 glyf 轮廓的 TrueType（.ttf）。CFF/OpenType（.otf）明确报错 ——
CFF 子集化是另一套（Type2 charstring），不在 P4 范围。

子集化的几个要点：

    · 复合字形（composite）会引用别的字形，被引用的也得留下
    · 子集内部重新编号：0 = .notdef，1..N = 保留字形
    · 复合字形里的 component GID 必须跟着重写，否则字会画错
    · loca 的 indexToLocFormat 有短（u16/2）长（u32）两种，要原样保持
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# 必需的表：glyf 轮廓和 CFF 轮廓两套
_REQUIRED_TTF = ("head", "hhea", "maxp", "loca", "glyf", "hmtx", "cmap")
_REQUIRED_CFF = ("head", "hhea", "maxp", "CFF ", "hmtx", "cmap")


class FontError(ValueError):
    pass


@dataclass
class TrueTypeFont:
    path: Path
    data: bytes = b""
    tables: dict[str, tuple[int, int]] = field(default_factory=dict)
    units_per_em: int = 1000
    num_glyphs: int = 0
    index_to_loc: int = 0
    ascender: int = 0
    descender: int = 0
    line_gap: int = 0
    num_hmetrics: int = 0
    cmap: dict[int, int] = field(default_factory=dict)     # codepoint → gid
    advances: list[int] = field(default_factory=list)      # gid → advance (font units)
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    loca: list[int] = field(default_factory=list)          # 字形偏移，load 时算一次
    glyf_table: bytes = b""                                # glyf 表字节，避免每次切片复制 10MB
    is_cff: bool = False                                   # True = OTTO/CFF 轮廓，不支持子集化

    # ── 载入 ────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path) -> "TrueTypeFont":
        f = cls(path=Path(path))
        f.data = f.path.read_bytes()
        d = f.data
        if len(d) < 12:
            raise FontError("字体文件太小")
        ver, n = struct.unpack(">IH", d[:6])
        if ver == 0x4F54544F:      # 'OTTO' = CFF/OpenType
            f.is_cff = True
        for i in range(n):
            off = 12 + i * 16
            tag, _csum, toff, tlen = struct.unpack(">4sIII", d[off:off + 16])
            f.tables[tag.decode("latin-1")] = (toff, tlen)
        required = _REQUIRED_CFF if f.is_cff else _REQUIRED_TTF
        missing = [t for t in required if t not in f.tables]
        if missing:
            kind = "CFF/OpenType" if f.is_cff else "TrueType"
            raise FontError(f"{kind} 字体缺表：{missing}")

        head = f._table("head")
        f.units_per_em = struct.unpack(">H", head[18:20])[0]
        f.index_to_loc = struct.unpack(">h", head[50:52])[0]
        f.bbox = struct.unpack(">hhhh", head[36:44])

        hhea = f._table("hhea")
        f.ascender = struct.unpack(">h", hhea[4:6])[0]
        f.descender = struct.unpack(">h", hhea[6:8])[0]
        f.line_gap = struct.unpack(">h", hhea[8:10])[0]
        f.num_hmetrics = struct.unpack(">H", hhea[34:36])[0]

        maxp = f._table("maxp")
        f.num_glyphs = struct.unpack(">H", maxp[4:6])[0]

        if f.units_per_em <= 0 or f.num_glyphs <= 0:
            raise FontError("字体头信息异常")

        f._parse_hmtx()
        f._parse_cmap()
        if not f.is_cff:
            f.loca = f._parse_loca()  # 只算一次：每次 glyph_data 都重算会让扫全字体变成 O(n²)
            f.glyf_table = f._table("glyf")
        return f

    def _table(self, tag: str) -> bytes:
        off, ln = self.tables[tag]
        return self.data[off:off + ln]

    def _parse_hmtx(self) -> None:
        hmtx = self._table("hmtx")
        adv = []
        for i in range(self.num_hmetrics):
            if 4 * i + 2 > len(hmtx):
                break
            adv.append(struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0])
        if not adv:
            raise FontError("hmtx 表为空")
        # 后面那些 glyph 共用最后一个 advance
        adv += [adv[-1]] * (self.num_glyphs - len(adv))
        self.advances = adv

    def _parse_cmap(self) -> None:
        cmap = self._table("cmap")
        (n,) = struct.unpack(">H", cmap[2:4])
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", cmap[4 + i * 8:12 + i * 8])
            score = -1
            if pid == 3 and eid == 10:
                score = 3          # UCS-4
            elif pid == 3 and eid == 1:
                score = 2          # BMP
            elif pid == 0:
                score = 1          # Unicode
            if score > 0 and (best is None or score > best[0]):
                best = (score, off)
        if best is None:
            raise FontError("cmap 里没有可用的 Unicode 子表")
        self._parse_cmap_subtable(cmap[best[1]:])

    def _parse_cmap_subtable(self, sub: bytes) -> None:
        fmt = struct.unpack(">H", sub[:2])[0]
        table = self.cmap
        if fmt == 4:
            seg_x2 = struct.unpack(">H", sub[6:8])[0]
            seg = seg_x2 // 2
            ends = struct.unpack(f">{seg}H", sub[14:14 + seg_x2])
            starts_off = 16 + seg_x2
            starts = struct.unpack(f">{seg}H", sub[starts_off:starts_off + seg_x2])
            deltas_off = starts_off + seg_x2
            deltas = struct.unpack(f">{seg}h", sub[deltas_off:deltas_off + seg_x2])
            ranges_off = deltas_off + seg_x2
            ranges = struct.unpack(f">{seg}H", sub[ranges_off:ranges_off + seg_x2])
            for i in range(seg):
                s, e = starts[i], ends[i]
                if s == 0xFFFF:
                    break
                for cp in range(s, min(e, 0xFFFE) + 1):
                    if ranges[i] == 0:
                        gid = (cp + deltas[i]) & 0xFFFF
                    else:
                        gi = ranges_off + i * 2 + ranges[i] + (cp - s) * 2
                        if gi + 1 >= len(sub):
                            continue
                        gid = struct.unpack(">H", sub[gi:gi + 2])[0]
                        if gid:
                            gid = (gid + deltas[i]) & 0xFFFF
                    if gid:
                        table[cp] = gid
        elif fmt == 12:
            (n,) = struct.unpack(">I", sub[12:16])
            for i in range(n):
                p = 16 + i * 12
                s, e, g0 = struct.unpack(">III", sub[p:p + 12])
                for k in range(min(e - s + 1, 0x10FFFF)):
                    table[s + k] = g0 + k
        elif fmt == 6:
            first, count = struct.unpack(">HH", sub[6:10])
            for k in range(count):
                gid = struct.unpack(">H", sub[10 + k * 2:12 + k * 2])[0]
                if gid:
                    table[first + k] = gid
        else:
            raise FontError(f"暂不支持的 cmap 子表格式 {fmt}")

    # ── 度量 ────────────────────────────────────────────────────────────
    def gid(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def advance(self, gid: int) -> int:
        if 0 <= gid < len(self.advances):
            return self.advances[gid]
        return self.advances[0] if self.advances else 0

    def text_width(self, text: str) -> int:
        """返回字体单位下的总宽度"""
        return sum(self.advance(self.gid(c)) for c in text)

    # ── 字形数据 ────────────────────────────────────────────────────────
    def _parse_loca(self) -> list[int]:
        loca = self._table("loca")
        n = self.num_glyphs + 1
        if self.index_to_loc == 0:
            if len(loca) < 2 * n:
                raise FontError("loca 表长度不足")
            return [v * 2 for v in struct.unpack(f">{n}H", loca[:2 * n])]
        if len(loca) < 4 * n:
            raise FontError("loca 表长度不足")
        return list(struct.unpack(f">{n}I", loca[:4 * n]))

    def glyph_data(self, gid: int) -> bytes:
        loca = self.loca
        if gid < 0 or gid + 1 >= len(loca):
            return b""
        s, e = loca[gid], loca[gid + 1]
        if e <= s:
            return b""          # 空字形（空格之类）
        return self.glyf_table[s:e]

    def composite_parts(self, gid: int) -> list[int]:
        """复合字形引用的字形号列表（普通字形返回空）"""
        g = self.glyph_data(gid)
        if len(g) < 10:
            return []
        ncont = struct.unpack(">h", g[:2])[0]
        if ncont >= 0:
            return []           # 简单字形
        out = []
        p = 10
        while p + 4 <= len(g):
            flags, comp_gid = struct.unpack(">HH", g[p:p + 4])
            out.append(comp_gid)
            p += 4
            if flags & 0x0001:          # ARG_1_AND_2_ARE_WORDS
                p += 4
            else:
                p += 2
            if flags & 0x0008:          # WE_HAVE_A_SCALE
                p += 2
            elif flags & 0x0040:        # WE_HAVE_AN_X_AND_Y_SCALE
                p += 4
            elif flags & 0x0080:        # WE_HAVE_A_TWO_BY_TWO
                p += 8
            if not (flags & 0x0020):    # MORE_COMPONENTS
                break
        return out

    # ── 子集化 ──────────────────────────────────────────────────────────
    def subset(self, chars: set[str]) -> tuple[bytes, dict[str, int]]:
        """按字符集做子集字体。

        返回 (子集 TTF 字节, {字符: 子集内 GID})。
        子集内 GID 从 1 开始（0 是 .notdef），并保持 loca 的原始格式。

        CFF/OTF 字体不做子集化：那要重写 Type2 charstring 和 subr 索引，
        工作量大、收益低（省体积而已）。整份字体嵌入，mapping 直接用原 GID ——
        PDF 侧 CIDFontType0 的 CID 就是 CharStrings 的索引，天然对齐。
        """
        if self.is_cff:
            mapping = {}
            for c in chars:
                g = self.cmap.get(ord(c))
                if g:
                    mapping[c] = g
            return self.data, mapping

        keep = {0}
        for c in chars:
            g = self.cmap.get(ord(c))
            if g:
                keep.add(g)
        # 复合字形会引用别的字形，被引用的也得留下（否则字画不出来）
        stack = list(keep)
        guard = 0
        while stack:
            g = stack.pop()
            guard += 1
            if guard > 200000:
                break
            for comp in self.composite_parts(g):
                if comp not in keep:
                    keep.add(comp)
                    stack.append(comp)

        order = sorted(keep)
        new_id = {old: i for i, old in enumerate(order)}

        # 重建 glyf：复合字形的 component GID 必须改成新编号
        glyf = bytearray()
        offsets = [0]
        for old in order:
            g = self.glyph_data(old)
            if g:
                g = self._rewrite_composite(g, new_id)
                glyf += g
                if len(glyf) % 2:
                    glyf += b"\x00"          # 4 字节对齐（loca 短格式要求偶数）
            offsets.append(len(glyf))

        if self.index_to_loc == 0:
            loca = struct.pack(f">{len(offsets)}H", *[o // 2 for o in offsets])
        else:
            loca = struct.pack(f">{len(offsets)}I", *offsets)

        hmtx = bytearray()
        for old in order:
            adv = self.advance(old)
            hmtx += struct.pack(">Hh", min(adv, 0xFFFF), 0)

        head = bytearray(self._table("head"))
        struct.pack_into(">I", head, 8, 0)          # checksumAdjustment 置 0
        struct.pack_into(">h", head, 50, self.index_to_loc)

        hhea = bytearray(self._table("hhea"))
        struct.pack_into(">H", hhea, 34, len(order))  # numberOfHMetrics

        maxp = bytearray(self._table("maxp"))
        struct.pack_into(">H", maxp, 4, len(order))

        out_tables = {
            "head": bytes(head),
            "hhea": bytes(hhea),
            "maxp": bytes(maxp),
            "hmtx": bytes(hmtx),
            "loca": loca,
            "glyf": bytes(glyf),
            "cmap": _build_cmap({cp: new_id[g]
                                 for cp, g in ((ord(c), self.cmap.get(ord(c)))
                                               for c in chars)
                                 if g in new_id}),
        }
        data = _build_sfnt(out_tables)
        mapping = {c: new_id[g] for c, g in
                   ((c, self.cmap.get(ord(c))) for c in chars) if g in new_id}
        return data, mapping

    def _rewrite_composite(self, g: bytes, new_id: dict[int, int]) -> bytes:
        if len(g) < 10:
            return g
        ncont = struct.unpack(">h", g[:2])[0]
        if ncont >= 0:
            return g                    # 简单字形原样
        out = bytearray(g[:10])
        p = 10
        while p + 4 <= len(g):
            flags, comp = struct.unpack(">HH", g[p:p + 4])
            out += struct.pack(">HH", flags, new_id.get(comp, 0))
            p += 4
            n = 4 if flags & 0x0001 else 2
            if flags & 0x0008:
                n += 2
            elif flags & 0x0040:
                n += 4
            elif flags & 0x0080:
                n += 8
            out += g[p:p + n]
            p += n
            if not (flags & 0x0020):
                break
        if p < len(g):
            out += g[p:]                # 尾部指令
        return bytes(out)


def _build_cmap(mapping: dict[int, int]) -> bytes:
    """生成只含 (3,1) 子表的 cmap，格式 4（BMP）。

    PDF 嵌入本身用 CIDToGIDMap，不依赖 cmap；但写上它，子集字体就是
    一个自洽的 TTF —— 能被别的工具读、能被我们自己的解析器验证结构。
    超出 BMP 的字符（emoji 等）不写进 cmap（format 4 表示不了），
    不影响 PDF 渲染。
    """
    items = sorted((cp, gid) for cp, gid in mapping.items() if cp <= 0xFFFE)
    seg_count = len(items) + 1                     # +1 是结尾的 0xFFFF 段
    ends, starts, deltas = [], [], []
    for cp, gid in items:
        starts.append(cp)
        ends.append(cp)
        deltas.append((gid - cp) & 0xFFFF)
    starts.append(0xFFFF)
    ends.append(0xFFFF)
    deltas.append(1)                               # 结尾段映射到 gid 0

    entry_selector = 0
    while (1 << (entry_selector + 1)) <= seg_count:
        entry_selector += 1
    search_range = (1 << entry_selector) * 2
    range_shift = seg_count * 2 - search_range

    body = bytearray()
    body += struct.pack(">HHHH", seg_count * 2, search_range,
                        entry_selector, range_shift)
    body += struct.pack(f">{seg_count}H", *ends)
    body += struct.pack(">H", 0)                   # reservedPad
    body += struct.pack(f">{seg_count}H", *starts)
    body += struct.pack(f">{seg_count}H", *[(d if d < 0x8000 else d - 0x10000) & 0xFFFF
                                            for d in deltas])
    body += struct.pack(f">{seg_count}H", *([0] * seg_count))

    sub = struct.pack(">HHH", 4, 6 + len(body), 0) + bytes(body)
    header = struct.pack(">HH", 0, 1)              # version, numTables
    record = struct.pack(">HHI", 3, 1, 12)         # (3,1) → offset 12
    return header + record + sub


def _checksum(data: bytes) -> int:
    if len(data) % 4:
        data = data + b"\x00" * (4 - len(data) % 4)
    total = 0
    for i in range(0, len(data), 4):
        total = (total + struct.unpack(">I", data[i:i + 4])[0]) & 0xFFFFFFFF
    return total


def _build_sfnt(tables: dict[str, bytes]) -> bytes:
    """把若干表拼成合法的 sfnt（TrueType）文件。"""
    tags = sorted(tables)
    n = len(tags)
    # searchRange / entrySelector / rangeShift
    entry_selector = 0
    while (1 << (entry_selector + 1)) <= n:
        entry_selector += 1
    search_range = (1 << entry_selector) * 16
    range_shift = n * 16 - search_range

    header = struct.pack(">IHHHH", 0x00010000, n, search_range, entry_selector, range_shift)
    offset = 12 + 16 * n
    dir_entries = bytearray()
    body = bytearray()

    for tag in tags:
        data = tables[tag]
        dir_entries += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(data),
                                   offset, len(data))
        padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
        body += padded
        offset += len(padded)

    font = bytearray(header + bytes(dir_entries) + bytes(body))
    # head.checksumAdjustment = 0xB1B0AFBA - 整体 checksum
    if "head" in tables:
        head_off = 12 + 16 * n
        for i, tag in enumerate(tags):
            if tag == "head":
                head_off += sum((len(tables[t]) + 3) // 4 * 4 for t in tags[:i])
                break
        adjust = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
        struct.pack_into(">I", font, head_off + 8, adjust)
    return bytes(font)
