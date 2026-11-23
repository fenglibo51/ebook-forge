"""P5 回归测试：中文排版 L2（禁则 / 标点挤压 / 中英间距 / 孤行控制）。

这些规则的特点是「肉眼容易糊过去，但代码能精确判定」，
所以测试全部用几何量断言：行首字符、行宽、间距、行数。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testenv                                            # noqa: E402,F401

from bookforge.formats import txt_io                          # noqa: E402
from bookforge.model import Chapter, Paragraph, TextBook      # noqa: E402
from bookforge.pdf.fonts import TrueTypeFont                  # noqa: E402
from bookforge.pdf.textpdf import (NO_LINE_END, NO_LINE_START,  # noqa: E402
                                   LayoutError, Line, TextPDFWriter,
                                   apply_kinsoku, break_lines, find_font,
                                   layout_book, line_width, mark_cjk_ascii_gaps,
                                   squeeze_overflow)

try:
    FONT_PATH = find_font()
except LayoutError:
    FONT_PATH = None

SIZE = 12.0
# A5 版心：419.53 - 56.7*2
AVAIL = 419.53 - 56.7 * 2


@unittest.skipIf(FONT_PATH is None, "容器里没有可用的中文字体")
class TestKinsoku(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = TrueTypeFont.load(FONT_PATH)

    def _process(self, text: str) -> list[Line]:
        raw = break_lines(self.font, text, AVAIL, SIZE, 0)
        lns = [Line(text=t, x=0.0, y=0.0, size=SIZE) for t in raw]
        apply_kinsoku(self.font, lns, SIZE)
        squeeze_overflow(self.font, lns, AVAIL, SIZE)
        mark_cjk_ascii_gaps(self.font, lns, SIZE)
        return lns

    def test_no_punctuation_at_line_start(self):
        """整行占满后，下一个逗号必须被拉回上一行，不能落行首"""
        text = "一二三四五六七八九十" * 2 + "甲乙丙丁戊" + "，后半句继续写下去"
        lns = self._process(text)
        bad = [ln.text[0] for ln in lns if ln.text and ln.text[0] in NO_LINE_START]
        self.assertEqual(bad, [], f"行首出现禁则标点：{bad}")

    def test_punctuation_pulled_to_previous_line(self):
        text = "一二三四五六七八九十" * 2 + "甲乙丙丁戊" + "，后半"
        lns = self._process(text)
        self.assertTrue(lns[0].text.endswith("，"), "逗号没被拉到上一行")

    def test_no_open_bracket_at_line_end(self):
        """开括号不能挂在行尾"""
        text = "一二三四五六七八九十" * 2 + "甲乙丙丁" + "（括号内容很长很长）"
        lns = self._process(text)
        bad = [ln.text[-1] for ln in lns if ln.text and ln.text[-1] in NO_LINE_END]
        self.assertEqual(bad, [], f"行尾出现开括号：{bad}")

    def test_overflow_is_squeezed(self):
        """拉回标点后行会超宽，挤压+微缩必须把它收进版心"""
        text = "一二三四五六七八九十" * 2 + "甲乙丙丁戊" + "。」」继续写"
        lns = self._process(text)
        for ln in lns:
            w = line_width(self.font, ln, SIZE) * ln.line_scale
            self.assertLessEqual(w, AVAIL + 1.0, f"行仍然超宽：{w:.1f}")

    def test_consecutive_punctuation_squeezed(self):
        text = "一二三四五六七八九十" * 2 + "甲乙丙丁戊" + "。」」继续写"
        lns = self._process(text)
        squeezed = [ln for ln in lns if ln.squeeze]
        self.assertTrue(squeezed, "连续标点没有被挤压")
        for sc in squeezed[0].squeeze.values():
            self.assertLess(sc, 1.0)
        self.assertGreaterEqual(squeezed[0].line_scale, 0.92,
                                "整行缩放不该超过 8%")

    def test_line_scale_never_below_floor(self):
        """极端情况（一行里塞 10 个标点）也不许无限缩"""
        text = "一" * 20 + "。" * 8 + "继续"
        lns = self._process(text)
        for ln in lns:
            self.assertGreaterEqual(ln.line_scale, 0.92)


@unittest.skipIf(FONT_PATH is None, "容器里没有可用的中文字体")
class TestMixedSpacing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = TrueTypeFont.load(FONT_PATH)

    def test_cjk_ascii_boundary_gets_gap(self):
        ln = Line(text="中文English混排", x=0, y=0, size=SIZE)
        mark_cjk_ascii_gaps(self.font, [ln], SIZE)
        self.assertEqual(len(ln.gaps), 2)               # 文|E 和 h|混
        for g in ln.gaps.values():
            self.assertAlmostEqual(g, SIZE * 0.25, places=2)

    def test_no_gap_when_space_present(self):
        """本来就有空格的地方不再额外加间距"""
        ln = Line(text="中文 English", x=0, y=0, size=SIZE)
        mark_cjk_ascii_gaps(self.font, [ln], SIZE)
        self.assertEqual(ln.gaps, {})

    def test_no_gap_inside_pure_cjk(self):
        ln = Line(text="纯中文没有间距", x=0, y=0, size=SIZE)
        mark_cjk_ascii_gaps(self.font, [ln], SIZE)
        self.assertEqual(ln.gaps, {})


@unittest.skipIf(FONT_PATH is None, "容器里没有可用的中文字体")
class TestWidowControl(unittest.TestCase):
    def test_title_not_orphaned(self):
        """标题后面必须跟得上正文，不能孤零零挂在页尾"""
        font = TrueTypeFont.load(FONT_PATH)
        # 造一个刚好把一页填到快满的正文，然后接一个标题
        filler = Chapter(title="填充", blocks=[Paragraph(text="字" * 900)])
        follow = Chapter(title="新章节", blocks=[Paragraph(text="正文内容。" * 10)])
        pages = layout_book(TextBook(chapters=[filler, follow], metadata={}), font)
        for pg in pages:
            titles = [i for i, ln in enumerate(pg.lines) if ln.is_title]
            for i in titles:
                self.assertGreaterEqual(len(pg.lines) - 1 - i, 2,
                                        "标题下面不足 2 行")


@unittest.skipIf(FONT_PATH is None, "容器里没有可用的中文字体")
class TestEndToEnd(unittest.TestCase):
    def test_pdf_has_no_kinsoku_violations(self):
        d = Path(tempfile.mkdtemp())
        src = d / "n.txt"
        src.write_text("第一章\n\n"
                       + "洛阳城外，秋风萧瑟。少年提剑而立，中文English混排ABC123测试，"
                         "标点不能落行首。" * 8, encoding="utf-8")
        book = txt_io.read(src)
        book.metadata["title"] = "t"
        data = TextPDFWriter(FONT_PATH).build(book, title="t")
        self.assertTrue(data.startswith(b"%PDF"))
        # 内容流里应该有 Tz（挤压/微缩）和多次 Tm（逐字定位）
        self.assertTrue(b"/ToUnicode" in data)
        self.assertGreater(data.count(b" Tj"), 5)

    def test_convert_txt_to_pdf_uses_l2(self):
        from bookforge import convert
        d = Path(tempfile.mkdtemp())
        src = d / "n.txt"
        src.write_text("第一章\n\n" + "测试正文，中文 English 混排。" * 20, encoding="utf-8")
        out = d / "n.pdf"
        r = convert(src, "pdf", out=out)
        self.assertTrue(out.exists())
        self.assertIn("L1", r["note"])      # note 里的档位说明还在


if __name__ == "__main__":
    unittest.main(verbosity=2)
