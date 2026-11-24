"""解压/读取的共享限额。

所有能「由用户提供压缩包」的入口都走这里，避免只保护 CBZ 而漏掉 7z / EPUB / DOCX。
数值都能用环境变量调，默认值按家用 NAS（约 20GB 内存）估的。
"""

from __future__ import annotations

import os
import zlib


def _mb(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default))) << 20


def _fmt_bytes(n: int) -> str:
    """人话大小。64KB 显示成「0 MB」会让人以为限额设错了。"""
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    if n >= 1 << 10:
        return f"{n / 1024:.0f} KB"
    return f"{n} 字节"


# 单个条目的解压上限（一张图 / 一个 xml）
MAX_ENTRY_BYTES = _mb("BOOKFORGE_MAX_ENTRY_MB", 256)

# 整个压缩包解压后的总字节上限
MAX_TOTAL_BYTES = _mb("BOOKFORGE_MAX_TOTAL_MB", 4096)

# 条目数量上限（防「几百万个 1 字节文件」这种）
MAX_ENTRIES = int(os.environ.get("BOOKFORGE_MAX_ENTRIES", "5000"))

# 7z 的嵌套层级（CB7 里套 CBZ 这种）
MAX_NEST_DEPTH = int(os.environ.get("BOOKFORGE_MAX_NEST_DEPTH", "2"))


class LimitError(Exception):
    """超限。调用方转成自己的错误类型。"""


# 单个 zlib 流解压后的通用上限（图片流这类，按单条目 256MB 算）
MAX_INFLATE_BYTES = _mb("BOOKFORGE_MAX_INFLATE_MB", 256)

# PDF 内部流（对象流 / 内容流 / 蒙版流）单独一个更紧的预算。
# 这些东西正常只有几十 KB —— 用 256MB 那个通用值等于没防：
# 实测一个 8MB 的 ObjStm 炸弹在 256MB 限额下照常放行。
MAX_PDF_STREAM_BYTES = _mb("BOOKFORGE_MAX_PDF_STREAM_MB", 32)


def inflate(data: bytes, max_bytes: int = 0, *, what: str = "压缩流") -> bytes:
    """有上限的 zlib 解压 —— 边解边数，超了立刻停。

    ⚠️ 为什么不能用 `zlib.decompress()`：它是「解完再返回」。
    压缩炸弹会先把内存吃光，你才拿到结果去 len() 判断 —— 那时候进程可能已经
    OOM 了。实测 2 MB 全零数据压成 2056 字节（1020 倍），一个 2 MB 的流能解出 2 GB。

    `decompressobj().decompress(data, max_length)` 只产出最多 max_length 字节，
    剩下的留在 `unconsumed_tail` —— 所以能在真正分配大块内存之前就拦住。

    2026-11-24 深度审查 P1-01：这条路径原来散在 pdf/reader.py（Object Stream）、
    pdf/images.py（Flate 图片）、pdf/render.py（内容流/蒙版流）、
    pdf/extract.py、formats/png.py（IDAT）五处，全是无界 zlib.decompress。
    """
    if max_bytes <= 0:
        max_bytes = MAX_INFLATE_BYTES
    d = zlib.decompressobj()
    # ⚠️ zlib.error 不在这里包 —— 「数据损坏」和「超限」是两回事：
    # 损坏的流原来的调用方是「跳过继续」（比如对象流解析），超限的必须
    # 让整个文件失败。包成一个异常就分不开了，所以让 zlib.error 原样冒出去。
    out = d.decompress(data, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise LimitError(
            f"{what}解压后超过 {_fmt_bytes(max_bytes)} 上限"
            "（可能是压缩炸弹；对应限额可用环境变量调整）")
    out += d.flush()
    if len(out) > max_bytes:
        raise LimitError(
            f"{what}解压后超过 {_fmt_bytes(max_bytes)} 上限"
            "（可能是压缩炸弹；对应限额可用环境变量调整）")
    return out


def check_entry(name: str, size: int) -> None:
    if size > MAX_ENTRY_BYTES:
        raise LimitError(
            f"{name} 有 {size / 2**20:.0f} MB，超过单文件上限 "
            f"{MAX_ENTRY_BYTES / 2**20:.0f} MB（BOOKFORGE_MAX_ENTRY_MB 可调）")


def check_total(total: int) -> None:
    if total > MAX_TOTAL_BYTES:
        raise LimitError(
            f"压缩包解压后共 {total / 2**30:.1f} GB，超过上限 "
            f"{MAX_TOTAL_BYTES / 2**30:.0f} GB（BOOKFORGE_MAX_TOTAL_MB 可调）")


def check_count(n: int) -> None:
    if n > MAX_ENTRIES:
        raise LimitError(f"压缩包里有 {n} 个条目，超过上限 {MAX_ENTRIES}")


def describe() -> str:
    return (f"单条目 {MAX_ENTRY_BYTES / 2**20:.0f} MB / "
            f"总量 {MAX_TOTAL_BYTES / 2**30:.0f} GB / "
            f"条目数 {MAX_ENTRIES}")

class Budget:
    """跨层共享的解压预算。

    为什么需要「跨层」：CBC 是 7z 里套 CBZ，若每层都用完整额度，
    一个 3 层嵌套的包就能吃掉 3 倍内存。这里一个包一次转换只建一个
    Budget，外层花掉的额度内层不能再用。

    并发安全不需要 —— Budget 只在单次转换的线程里用，不跨线程共享。
    """

    __slots__ = ("bytes_left", "entries_left")

    def __init__(self, total: int | None = None, entries: int | None = None):
        self.bytes_left = MAX_TOTAL_BYTES if total is None else total
        self.entries_left = MAX_ENTRIES if entries is None else entries

    def take_entry(self, name: str, size: int) -> None:
        """占一个条目的额度：先查单项上限，再扣总预算。"""
        check_entry(name, size)
        if size > self.bytes_left:
            raise LimitError(
                f"解压预算已用完：{name} 还要 {size / 2**20:.0f} MB，"
                f"只剩 {self.bytes_left / 2**20:.0f} MB"
                f"（总上限 {MAX_TOTAL_BYTES / 2**30:.0f} GB，嵌套也共用这一份）")
        self.bytes_left -= size

    def take_count(self, n: int) -> None:
        if n > self.entries_left:
            raise LimitError(
                f"条目数超限：这一层有 {n} 个，累计上限 {MAX_ENTRIES}")
        self.entries_left -= n

    def child(self) -> "Budget":
        """内层嵌套用：共享同一份额度（返回自身即可）。"""
        return self


def check_zip(zf, budget: Budget | None = None) -> Budget:
    """ZIP 入口的统一检查：条目数 + 单项大小 + 累计字节。

    只信 declared 值（ZipInfo.file_size）；高风险路径调用方读出来后再按
    实际字节数复核。返回本次使用的预算对象，供内层复用。
    """
    b = budget if budget is not None else Budget()
    infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    b.take_count(len(infos))
    for i in infos:
        b.take_entry(i.filename, i.file_size)
    return b
