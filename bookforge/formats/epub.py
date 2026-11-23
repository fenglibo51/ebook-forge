"""EPUB 读写（纯标准库）。

读：EPUB 就是「带目录结构的 zip」。
    META-INF/container.xml → OPF → manifest(id→href) + spine(阅读顺序)
    然后按 spine 顺序逐个取图。**不能按文件名排序**：漫画 epub 的封面
    往往是序号最大的那个文件（00193.jpeg），但它是第一页。

写：产出 EPUB 3（同时带 EPUB 2 的 toc.ncx，老设备/墨水屏也能开）。
    图片原字节直嵌，一个像素都不动。
"""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..model import IMAGE_EXTS, ComicBook, Page, normalize_fmt
from . import limits
from . import png


def _check_zip_limits(zf: zipfile.ZipFile) -> None:
    """打开 zip 后先按头部声明的大小过一遍限额，再决定要不要真解压。

    用 zipinfo.file_size（声明的解压后大小），不用真解压就能拦住
    「几百 KB 解出几十 GB」的包。CBZ / EPUB / DOCX 共用同一套限额。
    """
    infos = zf.infolist()
    limits.check_count(len(infos))
    total = 0
    for info in infos:
        limits.check_entry(info.filename, info.file_size)
        total += info.file_size
        limits.check_total(total)


class EPUBError(ValueError):
    pass


# ── 读 ────────────────────────────────────────────────────────────────────

_CONTAINER = "META-INF/container.xml"


def _read_opf_path(zf: zipfile.ZipFile) -> str:
    try:
        xml = zf.read(_CONTAINER).decode("utf-8", "ignore")
    except KeyError:
        raise EPUBError("epub 缺少 META-INF/container.xml")
    m = re.search(r'full-path\s*=\s*"([^"]+)"', xml)
    if not m:
        raise EPUBError("container.xml 里找不到 rootfile")
    return m.group(1)


def _parse_opf(opf: str) -> tuple[dict[str, str], list[str], dict]:
    """返回 (manifest: id→href, spine: [id...], metadata)"""
    manifest: dict[str, str] = {}
    for m in re.finditer(r"<item\b[^>]*>", opf):
        tag = m.group(0)
        idm = re.search(r'\bid\s*=\s*"([^"]*)"', tag)
        href = re.search(r'\bhref\s*=\s*"([^"]*)"', tag)
        if idm and href:
            manifest[idm.group(1)] = html.unescape(href.group(1))

    spine: list[str] = []
    for m in re.finditer(r"<itemref\b[^>]*>", opf):
        tag = m.group(0)
        idref = re.search(r'\bidref\s*=\s*"([^"]*)"', tag)
        if idref:
            spine.append(idref.group(1))

    meta = {}
    for tag, key in (("title", "title"), ("creator", "author"),
                     ("language", "language"), ("publisher", "publisher")):
        m = re.search(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", opf, re.S)
        if m:
            meta[key] = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
    return manifest, spine, meta


_IMG_SRC_RE = (
    re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I),
    # Calibre 生成的封面用 SVG 包装，只匹配 <img> 会漏掉封面
    re.compile(r"""<image\b[^>]*\bxlink:href\s*=\s*["']([^"']+)["']""", re.I),
    re.compile(r"""<image\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.I),
)


def read(path: str | Path) -> ComicBook:
    """EPUB → ComicBook。只保留图片，文字/样式丢掉（这是漫画路径）。"""
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise EPUBError("epub 文件损坏（不是有效的 zip 结构）")

    _check_zip_limits(zf)

    with zf:
        names = set(zf.namelist())
        opf_path = _read_opf_path(zf)
        opf = zf.read(opf_path).decode("utf-8", "ignore")
        manifest, spine, meta = _parse_opf(opf)
        base = posixpath.dirname(opf_path)

        ordered: list[str] = []

        def resolve(ref: str, cur_dir: str) -> str:
            ref = html.unescape(ref.split("#")[0])
            p = posixpath.normpath(posixpath.join(cur_dir, ref) if cur_dir else ref)
            return p

        for sid in spine:
            href = manifest.get(sid)
            if not href:
                continue
            full = resolve(href, base)
            if full not in names:
                continue
            ext = posixpath.splitext(full)[1].lower()
            if ext in IMAGE_EXTS:
                if full not in ordered:
                    ordered.append(full)
            elif ext in (".html", ".xhtml", ".htm", ".svg"):
                body = zf.read(full).decode("utf-8", "ignore")
                sub = posixpath.dirname(full)
                for rx in _IMG_SRC_RE:
                    for src in rx.findall(body):
                        p = resolve(src, sub)
                        if p in names and p not in ordered:
                            ordered.append(p)

        # OPF 解析不出顺序时退回文件名排序（会有封面错位风险，只作兜底）
        if not ordered:
            ordered = sorted(n for n in names
                             if posixpath.splitext(n)[1].lower() in IMAGE_EXTS)

        if not ordered:
            raise EPUBError("这本 epub 里没有图片，无法按漫画处理")

        pages: list[Page] = []
        for n in ordered:
            data = zf.read(n)
            w, h, fmt = png.image_size(data)
            pages.append(Page(data=data, fmt=fmt or normalize_fmt(
                posixpath.splitext(n)[1]), width=w, height=h, name=n))

    return ComicBook(pages=pages, metadata=meta, source_format="epub")


def looks_like_comic(book: ComicBook, total_size: int = 0) -> bool:
    """两条判据任一成立即为漫画。

    ① 页数够多 且 图片占比不算低   → 整卷漫画
    ② 图片占比极高 且 不止一张图   → 番外 / 短篇（4 页占 99%）

    小说 epub 通常只有 1 张封面图，两条都拦得住。
    """
    n = book.page_count
    if not n:
        return False
    img_size = sum(p.size for p in book.pages)
    ratio = (img_size / total_size) if total_size else 1.0
    return (n >= 5 and ratio >= 0.30) or (n >= 2 and ratio >= 0.85)


# ── 写 ────────────────────────────────────────────────────────────────────

_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_MEDIA = {"jpeg": "image/jpeg", "png": "image/png", "jp2": "image/jp2",
          "tiff": "image/tiff", "gif": "image/gif", "webp": "image/webp"}


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def write(book: ComicBook, path: str | Path, *, title: str = "",
          author: str = "", language: str = "zh") -> Path:
    """ComicBook → EPUB3（带 ncx）。图片原字节直嵌。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    title = title or (book.metadata or {}).get("title") or path.stem
    author = author or (book.metadata or {}).get("author") or ""
    lang = (book.metadata or {}).get("language") or language
    n = len(book.pages)
    width = max(5, len(str(n)))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # mimetype 必须是第一个条目且不压缩，否则不合规
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(_CONTAINER, _CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)

        items, refs, nav_points = [], [], []
        for i, page in enumerate(book.pages, 1):
            ext = "." + page.fmt.replace("jpeg", "jpg")
            media = _MEDIA.get(page.fmt, "application/octet-stream")
            img_name = f"images/{i:0{width}d}{ext}"
            zf.writestr(f"OEBPS/{img_name}", page.data, compress_type=zipfile.ZIP_STORED)

            xhtml = (f'<?xml version="1.0" encoding="utf-8"?>\n'
                     '<!DOCTYPE html>\n'
                     '<html xmlns="http://www.w3.org/1999/xhtml" '
                     'xmlns:epub="http://www.idpf.org/2007/ops" '
                     f'lang="{_esc(lang)}" xml:lang="{_esc(lang)}">\n'
                     f'<head><meta charset="utf-8"/><title>{i}</title>'
                     '<style>html,body{margin:0;padding:0;height:100%}'
                     'body{text-align:center}'
                     'img{max-width:100%;max-height:100%;object-fit:contain}'
                     '</style></head>\n'
                     f'<body><div><img src="../{img_name}" alt="{i}"/></div></body>\n'
                     '</html>')
            page_name = f"pages/p{i:0{width}d}.xhtml"
            zf.writestr(f"OEBPS/{page_name}", xhtml, compress_type=zipfile.ZIP_DEFLATED)

            items.append(f'    <item id="img{i}" href="{img_name}" media-type="{media}"/>')
            items.append(f'    <item id="page{i}" href="{page_name}" '
                         'media-type="application/xhtml+xml"/>')
            refs.append(f'    <itemref idref="page{i}"/>')
            nav_points.append(
                f'  <navPoint id="np{i}" playOrder="{i}"><navLabel><text>{i}</text>'
                f'</navLabel><content src="{page_name}"/></navPoint>')

        nav_xhtml = (f'<?xml version="1.0" encoding="utf-8"?>\n'
                     '<!DOCTYPE html>\n'
                     '<html xmlns="http://www.w3.org/1999/xhtml" '
                     'xmlns:epub="http://www.idpf.org/2007/ops" '
                     f'lang="{_esc(lang)}" xml:lang="{_esc(lang)}">\n'
                     f'<head><meta charset="utf-8"/><title>{_esc(title)}</title></head>\n'
                     '<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>\n'
                     + "".join(f'<li><a href="pages/p{i:0{width}d}.xhtml">{i}</a></li>'
                               for i in range(1, n + 1))
                     + '\n</ol></nav></body></html>')
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)

        ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
               f'<head><meta name="dtb:uid" content="{_esc(path.stem)}"/>'
               f'<meta name="dtb:depth" content="1"/>'
               f'<meta name="dtb:totalPageCount" content="0"/>'
               f'<meta name="dtb:maxPageNumber" content="0"/></head>\n'
               f'<docTitle><text>{_esc(title)}</text></docTitle>\n'
               '<navMap>\n' + "\n".join(nav_points) + '\n</navMap>\n</ncx>')
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)

        opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
               'unique-identifier="bookid">\n'
               '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
               f'    <dc:identifier id="bookid">{_esc(path.stem)}</dc:identifier>\n'
               f'    <dc:title>{_esc(title)}</dc:title>\n'
               f'    <dc:language>{_esc(lang)}</dc:language>\n'
               + (f'    <dc:creator>{_esc(author)}</dc:creator>\n' if author else "")
               + f'    <meta property="dcterms:modified">{now}</meta>\n'
               '  </metadata>\n'
               '  <manifest>\n'
               + "\n".join(items)
               + '\n    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
               '\n    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
               '  </manifest>\n'
               '  <spine toc="ncx">\n'
               + "\n".join(refs)
               + '\n  </spine>\n'
               '</package>')
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)

    return path


# ── 文字版 EPUB（小说，reflowable）────────────────────────────────────────

from html.parser import HTMLParser      # noqa: E402
from ..model import Chapter as _Chapter, ImageBlock, Paragraph, TextBook  # noqa: E402

_MEDIA_ALIAS = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "jp2": "jp2", "gif": "gif", "webp": "webp"}


class _BlockParser(HTMLParser):
    """把一章 xhtml 拆成 标题 / 段落 / 图片 的序列。

    不追求完整 HTML 语义，只要能还原「读起来是什么样」：
    块级标签断段，<br> 当空格，img 记下 src 由调用方去 zip 里取。
    """

    _BLOCK = {"p", "div", "li", "blockquote", "td", "section", "article"}
    _HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._buf: list[str] = []
        self._in_head = ""
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        # head 里的一切都不是正文：<title>书名</title> 会被当成段落读进来，
        # 实测导致每章多出一段标题文字（字数差 0.24%）。
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._HEAD:
            self._flush()
            self._in_head = tag
        elif tag in self._BLOCK:
            self._flush()
        elif tag == "br":
            self._buf.append(" ")
        elif tag == "img":
            src = dict(attrs).get("src") or ""
            if src:
                self._flush()
                self.items.append(("img", src))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._HEAD:
            self._flush()
            self._in_head = ""
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        self._buf.clear()
        if text:
            self.items.append(("h" if self._in_head else "p", text))


def read_text(path: str | Path) -> TextBook:
    """文字版 EPUB → TextBook。按 spine 顺序，一章一段。"""
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise EPUBError("epub 文件损坏（不是有效的 zip 结构）")

    _check_zip_limits(zf)

    with zf:
        names = set(zf.namelist())
        opf_path = _read_opf_path(zf)
        opf = zf.read(opf_path).decode("utf-8", "ignore")
        manifest, spine, meta = _parse_opf(opf)
        base = posixpath.dirname(opf_path)

        chapters: list[_Chapter] = []

        for sid in spine:
            href = manifest.get(sid)
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(base, href) if base else href)
            if full not in names:
                continue
            ext = posixpath.splitext(full)[1].lower()
            if ext not in (".html", ".xhtml", ".htm", ".svg"):
                continue

            parser = _BlockParser()
            try:
                parser.feed(zf.read(full).decode("utf-8", "ignore"))
            except Exception:
                continue
            if not parser.items:
                continue

            ch = _Chapter(title="")
            sub = posixpath.dirname(full)
            for kind, value in parser.items:
                if kind == "h" and not ch.title:
                    ch.title = value
                elif kind == "img":
                    target = posixpath.normpath(
                        posixpath.join(sub, value.split("#")[0]) if sub else value)
                    if target in names:
                        data = zf.read(target)
                        w, h, fmt = png.image_size(data)
                        ch.blocks.append(ImageBlock(
                            data=data, fmt=fmt or "jpeg", width=w, height=h))
                else:
                    ch.blocks.append(Paragraph(text=value))

            if ch.title or ch.blocks:
                if not ch.title:
                    ch.title = f"第 {len(chapters) + 1} 章"
                chapters.append(ch)

    if not chapters:
        raise EPUBError("这本 epub 里没有可提取的正文（可能是纯图片漫画）")
    return TextBook(chapters=chapters, metadata=meta, source_format="epub")


def write_text(book: TextBook, path: str | Path, *, title: str = "",
               author: str = "", language: str = "zh") -> Path:
    """TextBook → 文字版 EPUB3（带 ncx）。图片原字节内嵌。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = book.metadata or {}
    title = title or meta.get("title") or path.stem
    author = author or meta.get("author") or ""
    lang = meta.get("language") or language
    chapters = book.chapters or [_Chapter(title=title)]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[str] = []
    refs: list[str] = []
    nav: list[str] = []
    ncx_points: list[str] = []
    media: dict[str, bytes] = {}

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(_CONTAINER, _CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)

        for idx, ch in enumerate(chapters, 1):
            body: list[str] = []
            if ch.title:
                body.append(f"<h1>{_esc(ch.title)}</h1>")
            for block in ch.blocks:
                if isinstance(block, Paragraph):
                    if block.text.strip():
                        body.append(f"<p>{_esc(block.text)}</p>")
                elif isinstance(block, ImageBlock):
                    ext = "." + block.fmt.replace("jpeg", "jpg")
                    name = f"images/{idx:04d}-{len(media) + 1:02d}{ext}"
                    media[name] = block.data
                    body.append(f'<p class="img"><img src="../{name}" alt=""/></p>')

            xhtml = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                     f'<html xmlns="http://www.w3.org/1999/xhtml" '
                     f'xmlns:epub="http://www.idpf.org/2007/ops" '
                     f'lang="{_esc(lang)}" xml:lang="{_esc(lang)}">\n'
                     f'<head><meta charset="utf-8"/>'
                     f'<title>{_esc(ch.title or str(idx))}</title>'
                     '<style>body{margin:1em} p{margin:0;text-indent:2em;line-height:1.6}'
                     ' h1{font-size:1.4em;margin:1.5em 0 1em}'
                     ' p.img{text-indent:0;text-align:center}'
                     ' p.img img{max-width:100%}</style></head>\n'
                     '<body>\n' + "\n".join(body) + '\n</body>\n</html>')
            page = f"text/ch{idx:04d}.xhtml"
            zf.writestr("OEBPS/" + page, xhtml, compress_type=zipfile.ZIP_DEFLATED)
            items.append(f'    <item id="ch{idx}" href="{page}" '
                         'media-type="application/xhtml+xml"/>')
            refs.append(f'    <itemref idref="ch{idx}"/>')
            label = _esc(ch.title or f"第 {idx} 章")
            nav.append(f'<li><a href="{page}">{label}</a></li>')
            ncx_points.append(
                f'  <navPoint id="np{idx}" playOrder="{idx}">'
                f'<navLabel><text>{label}</text></navLabel>'
                f'<content src="{page}"/></navPoint>')

        for i, (name, data) in enumerate(media.items(), 1):
            zf.writestr("OEBPS/" + name, data, compress_type=zipfile.ZIP_STORED)
            ext = name.rsplit(".", 1)[-1].lower()
            mt = _MEDIA.get(_MEDIA_ALIAS.get(ext, "jpeg"), "image/jpeg")
            items.append(f'    <item id="img{i}" href="{name}" media-type="{mt}"/>')

        nav_xhtml = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                     f'<html xmlns="http://www.w3.org/1999/xhtml" '
                     f'xmlns:epub="http://www.idpf.org/2007/ops" '
                     f'lang="{_esc(lang)}" xml:lang="{_esc(lang)}">\n'
                     f'<head><meta charset="utf-8"/>'
                     f'<title>{_esc(title)}</title></head>\n'
                     '<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>\n'
                     + "".join(nav) + '\n</ol></nav></body></html>')
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)

        ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
               f'<head><meta name="dtb:uid" content="{_esc(path.stem)}"/>'
               '<meta name="dtb:depth" content="1"/></head>\n'
               f'<docTitle><text>{_esc(title)}</text></docTitle>\n'
               '<navMap>\n' + "\n".join(ncx_points) + '\n</navMap>\n</ncx>')
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)

        opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
               'unique-identifier="bookid">\n'
               '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
               f'    <dc:identifier id="bookid">{_esc(path.stem)}</dc:identifier>\n'
               f'    <dc:title>{_esc(title)}</dc:title>\n'
               f'    <dc:language>{_esc(lang)}</dc:language>\n'
               + (f'    <dc:creator>{_esc(author)}</dc:creator>\n' if author else "")
               + f'    <meta property="dcterms:modified">{now}</meta>\n'
               '  </metadata>\n  <manifest>\n'
               + "\n".join(items)
               + '\n    <item id="nav" href="nav.xhtml" '
                 'media-type="application/xhtml+xml" properties="nav"/>'
                 '\n    <item id="ncx" href="toc.ncx" '
                 'media-type="application/x-dtbncx+xml"/>\n'
               '  </manifest>\n  <spine toc="ncx">\n'
               + "\n".join(refs)
               + '\n  </spine>\n</package>')
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)

    return path
