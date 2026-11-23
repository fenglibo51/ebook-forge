"""文件名 → 用户可见的显示名。

背景：Web 上传的文件在磁盘上用随机 file_id 命名（防覆盖、防路径穿越），
但用户看到的书名/下载名必须是原始文件名。这两者要在这里对齐，
不能靠「磁盘上叫什么就叫什么」——否则导出变成 x7Ab91Qm2.pdf。

name 来自客户端，属于不可信输入，所以 safe_stem() 会剥掉路径段、
结尾点号和超长部分，返回空串表示不可用，由调用方自己回退。
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["safe_stem", "display_stem"]

# 常见文件系统的单段名上限是 255 **字节**，不是字符。
# 一个汉字 UTF-8 占 3 字节，按字符截断 100 字会算出 300 字节 → ENAMETOOLONG。
# 这里留出扩展名（.epub 5 字节）与重名后缀（_9999 5 字节）的余量。
MAX_STEM_BYTES = 180
MAX_STEM_CHARS = 100        # 两条同时生效，谁先到算谁


def _truncate_bytes(s: str, limit: int) -> str:
    """按 UTF-8 字节截断，不会切出半个汉字。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore").rstrip()


def safe_stem(name: str) -> str:
    """把原始文件名变成安全的「书名主干」，失败返回空串。"""
    if not name:
        return ""
    # 统一分隔符后只取最后一段，挡掉 ../ 和绝对路径
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = base.replace("\x00", "").strip()
    if not base:
        return ""
    stem = Path(base).stem
    # Windows 不允许结尾的点或空格；"." ".." 这种没有可用信息
    stem = stem.strip().strip(".").strip()
    if not stem:
        return ""
    return _truncate_bytes(stem[:MAX_STEM_CHARS].strip(), MAX_STEM_BYTES)


def display_stem(path: str | Path, name: str = "") -> str:
    """优先用调用方给的原始名，取不到才用路径本身的 stem。

    传空 name 时行为与直接用 Path(path).stem 一致 —— CLI 场景无需改动。
    """
    return safe_stem(name) or Path(path).stem
