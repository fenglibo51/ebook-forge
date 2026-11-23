"""解压/读取的共享限额。

所有能「由用户提供压缩包」的入口都走这里，避免只保护 CBZ 而漏掉 7z / EPUB / DOCX。
数值都能用环境变量调，默认值按家用 NAS（约 20GB 内存）估的。
"""

from __future__ import annotations

import os


def _mb(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default))) << 20


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
