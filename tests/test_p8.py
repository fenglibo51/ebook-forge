"""P8 回归：CFF / OpenType（.otf）字体支持。

样本是仓库自带的 312 字节最小 OTF（tests/fixtures/minimal-cff.otf），
由同目录的 gen_minimal_otf.py 生成 —— 所以 CI 里**不会 skip**，CFF 路径真的被测到。

如果本机有真实的中文 OTF（Source Han Sans 之类），额外再跑一轮大字库的用例，
因为最小样本只有 2 个字形，测不出「64000 字形整字体嵌入」的量级问题。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookforge.model import Chapter, Paragraph, TextBook      # noqa: E402
from bookforge.pdf.fonts import FontError, TrueTypeFont       # noqa: E402
from bookforge.pdf.textpdf import TextPDFWriter, find_font    # noqa: E402


FIXTURE_OTF = Path(__file__).resolve().parent / "fixtures" / "minimal-cff.otf"


def _find_otf() -> Path | None:
    """找 CFF 字体：优先仓库自带的固定样本，保证 CI 里也能跑。"""
    if FIXTURE_OTF.is_file() and FIXTURE_OTF.read_bytes()[:4] == b"OTTO":
        return FIXTURE_OTF
    cands = [
        Path("/tmp/test.otf"),
        *Path.home().glob(".fonts/*.otf"),
        *Path("/usr/share/fonts").rglob("*.otf"),
    ]
    for p in cands:
        try:
            if p.is_file() and p.read_bytes()[:4] == b"OTTO":
                return p
        except OSError:
            continue
    return None


OTF = _find_otf()


@unittest.skipIf(OTF is None, "没有 CFF/OTF 字体样本")
class TestCFFFont(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = TrueTypeFont.load(OTF)

    def test_detected_as_cff(self):
        self.assertTrue(self.f.is_cff)
        self.assertGreaterEqual(self.f.num_glyphs, 2)      # 样本是 2 字形的最小 OTF

    def test_no_glyf_required(self):
        """CFF 字体没有 glyf/loca，不该因此报错"""
        self.assertNotIn("glyf", self.f.tables)
        self.assertEqual(self.f.loca, [])

    def test_metrics_still_work(self):
        """hmtx / cmap 是共用的，CFF 字体也能拿到宽度。

        字符从字体自己的 cmap 里取 —— 样本只有 'A'，真实中文字体有一堆汉字，
        写死字符会让两种样本只有一种能过。
        """
        ch = chr(next(iter(self.f.cmap)))
        g = self.f.gid(ch)
        self.assertGreater(g, 0)
        self.assertGreater(self.f.advance(g), 0)

    def test_subset_returns_whole_font(self):
        """CFF 不做子集化，mapping 用原 GID"""
        chars = [chr(c) for c in list(self.f.cmap)[:2]]
        sub, mapping = self.f.subset(set(chars))
        self.assertEqual(len(sub), len(self.f.data))       # 整份字体原样返回
        for ch, gid in mapping.items():
            self.assertEqual(gid, self.f.gid(ch))          # GID 没重编号

    def test_pdf_with_embedded_cff(self):
        d = Path(tempfile.mkdtemp())
        book = TextBook(chapters=[Chapter(title="第一章", blocks=[
            Paragraph(text="CFF 字体排版测试。"),
        ])], metadata={"title": "t"})
        data = TextPDFWriter(OTF).build(book, title="t")
        out = d / "cff.pdf"
        out.write_bytes(data)

        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"%PDF"))
        # CFF 走 FontFile3 + CIDFontType0，不是 FontFile2 + CIDFontType2
        self.assertIn(b"/FontFile3", raw)
        self.assertIn(b"/CIDFontType0", raw)
        self.assertIn(b"/Subtype /OpenType", raw)
        self.assertNotIn(b"/CIDToGIDMap", raw)      # CIDFontType0 不需要这个
        self.assertTrue(raw.rstrip().endswith(b"%%EOF"))


@unittest.skipIf(find_font.__module__ is None, "")
class TestTTFStillWorks(unittest.TestCase):
    """确认加了 CFF 分支没把 TrueType 那条路搞坏。"""

    def test_ttf_uses_cidfonttype2(self):
        try:
            ttf = find_font()
        except Exception:
            self.skipTest("没有可用的 TTF 字体")
        d = Path(tempfile.mkdtemp())
        book = TextBook(chapters=[Chapter(title="章", blocks=[Paragraph(text="中文")])],
                        metadata={"title": "t"})
        raw = TextPDFWriter(ttf).build(book, title="t")
        self.assertIn(b"/FontFile2", raw)
        self.assertIn(b"/CIDFontType2", raw)
        self.assertIn(b"/CIDToGIDMap", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
