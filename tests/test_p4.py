"""P4 回归测试：TrueType 解析 / 子集化 / 排版 / 文字版 PDF 输出。

字体只测结构正确性（cmap 回读、宽度一致、复合字形重编号），
PDF 只测结构（页面数、字体嵌入、ToUnicode、内容流），
真正的「中文渲染对不对」靠 MuPDF 人工核验（容器里没有把 pymupdf 当项目依赖）。
"""

from __future__ import annotations

import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge import convert                                  # noqa: E402
from bookforge.formats import txt_io                           # noqa: E402
from bookforge.model import (Chapter, ImageBlock, PageSpec,    # noqa: E402
                             Paragraph, TextBook)
from bookforge.pdf import extract                              # noqa: E402
from bookforge.pdf.fonts import FontError, TrueTypeFont        # noqa: E402
from bookforge.pdf.textpdf import (LayoutError, TextPDFWriter,  # noqa: E402
                                   break_lines, find_font, layout_book)


def _font_path() -> Path | None:
    try:
        return find_font()
    except LayoutError:
        return None


FONT = _font_path()
FIXTURE_TTF = Path(__file__).resolve().parent / "fixtures" / "composite.ttf"


def sample_book(n_chapters: int = 3) -> TextBook:
    chapters = []
    for i in range(1, n_chapters + 1):
        text = (f"这是第{i}章的第一段。中文断行应该按字符切，"
                f"英文按单词切，像 ABC def 这样的混排也要正确断行。") * 3
        chapters.append(Chapter(title=f"第 {i} 章", blocks=[
            Paragraph(text=text),
            Paragraph(text="第二段，用来测试首行缩进和段落分隔。"),
        ]))
    return TextBook(chapters=chapters, metadata={"title": "排版测试"})


@unittest.skipIf(FONT is None, "容器里没有可用的中文字体")
class TestFont(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = TrueTypeFont.load(FONT)

    def test_basic_metrics(self):
        self.assertGreater(self.f.units_per_em, 0)
        self.assertGreater(self.f.num_glyphs, 100)
        self.assertEqual(len(self.f.advances), self.f.num_glyphs)

    def test_cmap(self):
        for ch in "中文测试。，ABC0":
            self.assertGreater(self.f.gid(ch), 0, f"{ch!r} 查不到字形")

    def test_cjk_is_fullwidth(self):
        """中文字宽应等于 unitsPerEm（全角）"""
        adv = self.f.advance(self.f.gid("中"))
        self.assertEqual(adv, self.f.units_per_em)

    def test_subset_size_shrinks(self):
        chars = set("第一章排版测试这是文本") | set("abcdefghij")
        sub, mapping = self.f.subset(chars)
        self.assertLess(len(sub), len(self.f.data) / 5,
                        f"子集没缩小：{len(sub)} vs 原 {len(self.f.data)}")
        self.assertGreater(len(mapping), 0)

    def test_subset_reparsable_and_correct(self):
        chars = set("中文测试ABC")
        sub, mapping = self.f.subset(chars)
        p = Path(tempfile.mkdtemp()) / "s.ttf"
        p.write_bytes(sub)
        f2 = TrueTypeFont.load(p)
        self.assertEqual(f2.units_per_em, self.f.units_per_em)
        # cmap 回读要指回正确的子集 GID
        for ch, cid in mapping.items():
            self.assertEqual(f2.cmap.get(ord(ch)), cid)
            # 宽度必须与原字体一致
            self.assertEqual(f2.advance(cid), self.f.advance(self.f.gid(ch)))

    def test_composite_references_rewritten(self):
        """复合字形的 component 必须跟着重新编号，且指向新范围里的那个字形。

        以前这条会 skip：容器里唯一的中文字体 NotoSansSC 有 30796 个字形，
        **一个复合字形都没有** —— 等于「子集化最容易出错的路径」从来没被测过。
        现在改用 tests/fixtures/composite.ttf（手搓的 368 字节字体，
        gid2 'À' 引用 gid1 'A'），跑的是同一条生产代码，但一定有效。
        """
        fx = TrueTypeFont.load(FIXTURE_TTF)
        comps = [g for g in range(1, fx.num_glyphs) if fx.composite_parts(g)]
        self.assertTrue(comps, "fixture 里应该有复合字形")
        g = comps[0]
        orig_parts = fx.composite_parts(g)
        chs = [c for c, gg in fx.cmap.items() if gg == g]
        self.assertTrue(chs, "fixture 的复合字形没挂到任何字符上")
        ch = chr(chs[0])

        sub, mapping = fx.subset({ch})
        p = Path(tempfile.mkdtemp()) / "c.ttf"
        p.write_bytes(sub)
        f2 = TrueTypeFont.load(p)

        parts = f2.composite_parts(mapping[ch])
        self.assertEqual(len(parts), len(orig_parts))
        for x in parts:
            self.assertLess(x, f2.num_glyphs, "component 指到了新范围外")
            self.assertTrue(f2.glyph_data(x), "被引用的字形丢了数据")
        # 关键断言：被引用的 'B' 在旧字体里是 gid 2，子集里只剩 B 和 À 两个字，
        # 按旧顺序重排后 B 变 1、À 变 2。忘了重写 component 就会指回旧号 2 ——
        # 而 2 在新字体里正好是 'À' 自己，等于自引用，画出来是一坨错字。
        self.assertNotIn(mapping[ch], parts, "component 指回了自己（没重编号）")
        self.assertEqual(parts[0], 1,
                         f"component 指向 {parts[0]}，应该指向 B 的新编号 1")


@unittest.skipIf(FONT is None, "容器里没有可用的中文字体")
class TestLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = TrueTypeFont.load(FONT)

    def test_break_lines_cjk(self):
        text = "洛阳城外，秋风萧瑟。" * 5
        lines = break_lines(self.f, text, 200.0, 12.0)
        self.assertGreater(len(lines), 1)
        for ln in lines:
            self.assertLessEqual(self.f.text_width(ln) * 12.0 / self.f.units_per_em, 200.1)

    def test_break_lines_english_words(self):
        text = "hello world " * 10
        lines = break_lines(self.f, text, 100.0, 12.0)
        for ln in lines:
            self.assertFalse(ln.endswith(" "), "行尾不该留空格")

    def test_chapter_starts_new_page(self):
        pages = layout_book(sample_book(3), self.f)
        self.assertGreaterEqual(len(pages), 3)

    def test_first_line_indent(self):
        book = TextBook(chapters=[Chapter(title="章", blocks=[Paragraph(text="正文" * 50)])],
                        metadata={})
        pages = layout_book(book, self.f, spec=PageSpec.a5())
        text_lines = [ln for ln in pages[0].lines if not ln.is_title]
        self.assertGreater(len(text_lines), 1)
        # 首行有缩进，后续行没有
        self.assertAlmostEqual(text_lines[0].x - text_lines[1].x, 24.0, places=1)

    def test_no_blank_pages(self):
        pages = layout_book(sample_book(4), self.f)
        blank = [i for i, pg in enumerate(pages) if not pg.lines and not pg.images]
        self.assertEqual(blank, [], f"出现空白页：{blank}")


@unittest.skipIf(FONT is None, "容器里没有可用的中文字体")
class TestTextPDF(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _pdf(self) -> Path:
        out = self.d / "a.pdf"
        convert_book = sample_book(2)
        writer = TextPDFWriter(FONT)
        out.write_bytes(writer.build(convert_book, title="排版测试"))
        return out

    def test_structure(self):
        data = self._pdf().read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.7"))
        self.assertIn(b"/Type /Catalog", data)
        self.assertIn(b"/Type /Font", data)
        self.assertIn(b"/Subtype /Type0", data)
        self.assertIn(b"/CIDFontType2", data)
        self.assertIn(b"/FontFile2", data)        # 字体确实嵌进去了
        self.assertIn(b"/ToUnicode", data)
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        m = re.search(rb"startxref\s+(\d+)", data)
        self.assertEqual(data[int(m.group(1)):int(m.group(1)) + 4], b"xref")

    def test_page_count_matches(self):
        f = self._pdf()
        data = f.read_bytes()
        m = re.search(rb"/Type /Pages /Count (\d+)", data)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 2)

    def test_font_is_subset_small(self):
        """PDF 里嵌的字体必须是子集，不能是 10MB 原字体"""
        data = self._pdf().read_bytes()
        sizes = [len(x) for x in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S)]
        biggest = max(sizes) if sizes else 0
        self.assertLess(biggest, 2_000_000,
                        f"嵌进去的东西太大（{biggest} 字节），不像子集字体")

    def test_txt_to_pdf_via_convert(self):
        txt = self.d / "n.txt"
        txt.write_text("第一章 甲\n\n　　正文一。\n\n第二章 乙\n\n　　正文二。\n", encoding="utf-8")
        out = self.d / "n.pdf"
        r = convert(txt, "pdf", out=out)
        self.assertTrue(out.exists())
        self.assertEqual(r["mode"], "text")
        self.assertGreater(out.stat().st_size, 1000)

    def test_novel_epub_to_pdf(self):
        """小说 epub → PDF 不能被当漫画（只出几页图片）"""
        src = self.d / "n.txt"
        src.write_text("".join(
            f"第{i}章\n\n　　这是第{i}章的正文，用来撑出足够的页数。\n\n" for i in range(1, 6)),
            encoding="utf-8")
        epub = self.d / "n.epub"
        convert(src, "epub", out=epub)
        pdf = self.d / "out.pdf"
        r = convert(epub, "pdf", out=pdf)
        self.assertEqual(r["mode"], "text")
        self.assertGreaterEqual(r["pages"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
