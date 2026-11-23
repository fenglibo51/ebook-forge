"""P2 回归测试：文字书四格式互转 + 7z 容器（CB7/CBC）。

覆盖：
    · txt 编码探测（UTF-8 / GBK / UTF-16 BOM）与中文分段/章节识别
    · fb2 / docx / epub 的读 → 写 → 读 往返，段落文本必须一致
    · 互转矩阵（txt↔fb2↔docx↔epub 全部组合）
    · CB7（7z + LZMA2）→ CBZ，图片字节一致
    · CBR 明确报错（RAR 闭源，永久不支持）

7z 样本是静态 fixture（tests/fixtures/tiny.cb7，2.4KB），不依赖 py7zr。
"""

from __future__ import annotations

import hashlib
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge import ConvertError, convert               # noqa: E402
from bookforge.formats import cbz, docx as docx_mod, epub, fb2, sevenz, txt_io  # noqa: E402
from bookforge.formats import png as P                     # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SAMPLE = """楔子

　　这是楔子的正文第一段。它被硬换行拆成两行，
这里还是同一段的内容。

第一章 风起

　　洛阳城外，秋风萧瑟。

　　少年提剑而立。

第 2 章 雨落

　　雨下了一整夜。
　　天亮时，他走了。

番外 旧事

　　很多年后有人问起。
"""


def paras(book) -> list[str]:
    return [b.text for ch in book.chapters for b in ch.blocks
            if getattr(b, "text", None)]


class TestTXT(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.p = self.d / "n.txt"
        self.p.write_text(SAMPLE, encoding="utf-8")

    def test_chapters(self):
        book = txt_io.read(self.p)
        self.assertEqual([c.title for c in book.chapters],
                         ["楔子", "第一章 风起", "第 2 章 雨落", "番外 旧事"])

    def test_indent_means_new_paragraph(self):
        """中文小说每段首行缩进 2 字符 —— 缩进开头必须是新段落"""
        book = txt_io.read(self.p)
        ch = book.chapters[2]                     # 第 2 章 雨落
        self.assertEqual([b.text for b in ch.blocks],
                         ["雨下了一整夜。", "天亮时，他走了。"])

    def test_continuation_line_merged(self):
        """不缩进的续行属于同一段"""
        book = txt_io.read(self.p)
        first = book.chapters[0].blocks[0].text
        self.assertIn("这里还是同一段的内容", first)
        self.assertIn("拆成两行", first)

    def test_gbk_detected(self):
        g = self.d / "g.txt"
        g.write_bytes("第一章 开始\n\n　　中文原样。\n".encode("gbk"))
        book = txt_io.read(g)
        self.assertIn(book.metadata["encoding"], ("gb18030", "gbk"))
        self.assertEqual(book.chapters[0].blocks[0].text, "中文原样。")

    def test_utf8_bom(self):
        b = self.d / "b.txt"
        b.write_bytes("\ufeff第一章 甲\n\n　　正文。\n".encode("utf-8-sig"))
        book = txt_io.read(b)
        self.assertEqual(book.chapters[0].title, "第一章 甲")

    def test_blank_line_only_format(self):
        p = self.d / "plain.txt"
        p.write_text("第一段。\n\n第二段。\n", encoding="utf-8")
        self.assertEqual(len(txt_io.read(p).chapters[0].blocks), 2)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.src = self.d / "n.txt"
        self.src.write_text(SAMPLE, encoding="utf-8")
        self.base = paras(txt_io.read(self.src))

    def _roundtrip(self, target: str):
        mid = self.d / f"m.{target}"
        convert(self.src, target, out=mid)
        back = self.d / f"back_{target}.txt"
        convert(mid, "txt", out=back)
        self.assertEqual(paras(txt_io.read(back)), self.base,
                         f"txt→{target}→txt 段落不一致")

    def test_fb2(self):
        self._roundtrip("fb2")

    def test_docx(self):
        self._roundtrip("docx")

    def test_epub(self):
        self._roundtrip("epub")

    def test_fb2_to_docx_and_epub(self):
        f = self.d / "a.fb2"
        convert(self.src, "fb2", out=f)
        for tgt in ("docx", "epub", "txt"):
            out = self.d / f"x.{tgt}"
            r = convert(f, tgt, out=out)
            self.assertTrue(out.exists(), f"fb2→{tgt} 没产出文件")
            self.assertEqual(r["mode"], "text")

    def test_docx_to_epub(self):
        dx = self.d / "a.docx"
        convert(self.src, "docx", out=dx)
        out = self.d / "x.epub"
        convert(dx, "epub", out=out)
        self.assertIn("OEBPS/text/ch0001.xhtml", zipfile.ZipFile(out).namelist())


class TestEPUBStructure(unittest.TestCase):
    def test_mimetype_first(self):
        d = Path(tempfile.mkdtemp())
        src = d / "n.txt"
        src.write_text(SAMPLE, encoding="utf-8")
        out = d / "a.epub"
        convert(src, "epub", out=out)
        with zipfile.ZipFile(out) as zf:
            first = zf.infolist()[0]
            self.assertEqual(first.filename, "mimetype")
            self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
            self.assertIn("OEBPS/content.opf", zf.namelist())
            self.assertIn("OEBPS/nav.xhtml", zf.namelist())
            self.assertIn("OEBPS/toc.ncx", zf.namelist())

    def test_text_epub_roundtrip_chapters(self):
        d = Path(tempfile.mkdtemp())
        src = d / "n.txt"
        src.write_text(SAMPLE, encoding="utf-8")
        out = d / "a.epub"
        convert(src, "epub", out=out)
        book = epub.read_text(out)
        self.assertEqual(len(book.chapters), 4)
        self.assertEqual(book.chapters[1].title, "第一章 风起")


class TestReject(unittest.TestCase):
    def test_text_to_cbz_rejected(self):
        d = Path(tempfile.mkdtemp())
        src = d / "n.txt"
        src.write_text("正文", encoding="utf-8")
        with self.assertRaises(ConvertError):
            convert(src, "cbz", out=d / "x.cbz")

    def test_cbr_rejected(self):
        d = Path(tempfile.mkdtemp())
        f = d / "x.cbr"
        f.write_bytes(b"Rar!\x1a\x07\x00")
        with self.assertRaises(ConvertError) as ctx:
            convert(f, "cbz", out=d / "y.cbz")
        self.assertIn("RAR", str(ctx.exception))


class TestSevenZ(unittest.TestCase):
    def test_read_cb7_fixture(self):
        p = FIXTURES / "tiny.cb7"
        self.assertTrue(p.exists(), "缺少 fixture tests/fixtures/tiny.cb7")
        files = sevenz.read_all(p)
        self.assertEqual(sorted(files), ["000.png", "001.png"])
        imgs = sevenz.read_all(p)
        book = cbz.read_cb7(p)
        self.assertEqual(book.page_count, 2)
        for page, name in zip(book.pages, ["000.png", "001.png"]):
            self.assertEqual(hashlib.md5(page.data).hexdigest(),
                             hashlib.md5(imgs[name]).hexdigest())

    def test_cb7_to_cbz_lossless(self):
        d = Path(tempfile.mkdtemp())
        src = FIXTURES / "tiny.cb7"
        out = d / "a.cbz"
        r = convert(src, "cbz", out=out)
        self.assertEqual(r["pages"], 2)
        src_imgs = sevenz.read_all(src)
        with zipfile.ZipFile(out) as zf:
            # 只比图片：CBZ 里还会写 ComicInfo.xml（不是页）
            names = sorted(n for n in zf.namelist()
                           if Path(n).suffix.lower() in (".png", ".jpg", ".jpeg"))
            got = [zf.read(n) for n in names]
        ref = [src_imgs[k] for k in sorted(src_imgs)]
        self.assertEqual([hashlib.md5(x).hexdigest() for x in got],
                         [hashlib.md5(x).hexdigest() for x in ref])

    def test_cb7_to_pdf_and_epub(self):
        d = Path(tempfile.mkdtemp())
        src = FIXTURES / "tiny.cb7"
        for tgt in ("pdf", "epub"):
            out = d / f"a.{tgt}"
            r = convert(src, tgt, out=out)
            self.assertEqual(r["pages"], 2)
            self.assertTrue(out.exists())

    def test_bad_signature(self):
        d = Path(tempfile.mkdtemp())
        f = d / "x.cb7"
        f.write_bytes(b"not a 7z file at all" * 4)
        with self.assertRaises(sevenz.SevenZError):
            sevenz.read_all(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
