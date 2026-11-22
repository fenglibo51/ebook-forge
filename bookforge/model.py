"""中间表示（IR）。

整个引擎只认两种书：

    ComicBook   一堆图片页，按顺序排好
    TextBook    章节 + 段落/插图，可重排

源格式读进来先变成这两种之一，目标格式再从它们写出去。
N 个源 × M 个目标 → N 个解析器 + M 个写入器，不再各写一遍打包逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

# 阅读器普遍认识的图片后缀。写入端只会产出这几种，不做转码。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".tif", ".tiff", ".webp", ".gif")

# 规范化的格式名，用于 metadata / 页内 fmt 字段
FMT_ALIASES = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".jp2": "jp2",
    ".jpx": "jp2",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".webp": "webp",
    ".gif": "gif",
}


def normalize_fmt(suffix: str) -> str:
    """'.JPG' / 'jpg' → 'jpeg'"""
    s = suffix.lower()
    if not s.startswith("."):
        s = "." + s
    return FMT_ALIASES.get(s, s.lstrip("."))


@dataclass
class Page:
    """一张图片页。data 永远是**原始字节**，进出一律不解码、不重压。"""

    data: bytes
    fmt: str                       # jpeg / png / jp2 / tiff ...
    width: int = 0                 # 像素，未知时 0（不阻塞转换）
    height: int = 0
    name: str = ""                 # 源容器里的原始条目名，仅用于排错

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class ComicBook:
    pages: list[Page] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    source_format: str = ""
    # 读取阶段可能改变处理方式（如 PDF 走渲染兜底 = 有损），要把结论带出去
    mode: str = ""          # "" / "extract"（无损）/ "render"（有损）
    note: str = ""          # 给人看的说明

    @property
    def page_count(self) -> int:
        return len(self.pages)


# ── 文字书 ────────────────────────────────────────────────────────────────

@dataclass
class PageSpec:
    """页面几何，单位 pt（1pt = 1/72 inch）。只在目标有固定版式时才有意义。"""

    width: float = 419.53       # A5 宽 148mm
    height: float = 595.28      # A5 高 210mm
    margin_top: float = 56.7    # 20mm
    margin_bottom: float = 56.7
    margin_left: float = 56.7
    margin_right: float = 56.7

    @classmethod
    def a5(cls) -> "PageSpec":
        return cls()

    @classmethod
    def from_mm(cls, w: float, h: float, margin: float = 20.0) -> "PageSpec":
        k = 72.0 / 25.4
        m = margin * k
        return cls(w * k, h * k, m, m, m, m)

    @classmethod
    def from_px(cls, w: int, h: int, dpi: float = 300.0) -> "PageSpec":
        """图片像素 → 页面尺寸（漫画 / 扫描件用）"""
        k = 72.0 / dpi
        return cls(w * k, h * k, 0, 0, 0, 0)


@dataclass
class Paragraph:
    text: str
    bold: bool = False
    italic: bool = False
    align: str = "left"          # left / center / right / justify
    indent: float = 0.0          # 首行缩进，单位 pt


@dataclass
class ImageBlock:
    data: bytes
    fmt: str
    width: int = 0
    height: int = 0
    display_ratio: float = 1.0   # 相对可用宽度（1.0 = 满宽），不重采样


Block = Union[Paragraph, ImageBlock]


@dataclass
class Chapter:
    title: str = ""
    blocks: list[Block] = field(default_factory=list)


@dataclass
class TextBook:
    chapters: list[Chapter] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    page_spec: PageSpec = field(default_factory=PageSpec.a5)
    source_format: str = ""

    @property
    def word_count(self) -> int:
        return sum(len(p.text) for c in self.chapters for p in c.blocks
                   if isinstance(p, Paragraph))


# ── 默认版式（中文小说常规档，只在 →PDF / DOCX 时生效）────────────────────
# EPUB / MOBI 是 reflowable，读者自己在阅读器里调字号行距，这里不参与。
DEFAULT_TEXT_STYLE = {
    "font_size": 12.0,      # 小四 12pt
    "line_height": 1.5,     # 1.5 倍行距
    "para_indent": 24.0,    # 首行缩进 2 字符（2 × 12pt）
    "para_spacing": 0.0,    # 中文用缩进，不加段间空行
    "align": "justify",
}
