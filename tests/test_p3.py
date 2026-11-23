"""P3 回归测试：MOBI 读写。

覆盖：
    · PalmDOC 压缩/解压往返（含距离/长度对、字面量块、空格+大写特例）
    · MOBI 写 → 读往返：章节/段落/图片字节一致
    · PalmDB 容器结构（78 字节头、记录表、记录数）
    · AZW3（KF8）/ DRM / HUFF-CDC 明确报错，不静默处理
    · 漫画 mobi → cbz（图片即页）
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

from bookforge import ConvertError, convert                    # noqa: E402
from bookforge.formats import mobi                             # noqa: E402
from bookforge.model import Chapter, ImageBlock, Paragraph, TextBook  # noqa: E402


def make_png(w: int, h: int, seed: int = 0) -> bytes:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw += bytes(((x * 7 + seed) & 0xFF, (y * 5 + seed) & 0xFF,
                          ((x + y) * 3 + seed) & 0xFF))

    def ck(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr)
            + ck(b"IDAT", zlib.compress(bytes(raw), 6)) + ck(b"IEND", b""))


def sample_book(images: int = 0) -> TextBook:
    blocks: list = [Paragraph(text="第一段。"), Paragraph(text="第二段。")]
    for i in range(images):
        blocks.append(ImageBlock(data=make_png(40, 30, i), fmt="png", width=40, height=30))
    return TextBook(chapters=[
        Chapter(title="第一章", blocks=blocks[:2]),
        Chapter(title="第二章", blocks=blocks[2:] or [Paragraph(text="结尾。")]),
    ], metadata={"title": "测试书", "author": "某人"})


class TestPalmDOC(unittest.TestCase):
    def test_roundtrip_various(self):
        samples = [
            b"",
            b"a",
            b"hello world",
            b" " * 200,
            bytes(range(256)) * 3,
            "中文测试，压缩往返要正确。".encode("utf-8") * 20,
            b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            b"ab" * 500,
        ]
        for s in samples:
            comp = mobi.compress_palmdoc(s)
            self.assertEqual(mobi.decompress_palmdoc(comp), s,
                             f"PalmDOC 往返失败 len={len(s)}")

    def test_backref_distance(self):
        """回溯距离超过输出长度时必须报错，不能静默产垃圾"""
        bad = bytes([0x80 | 0x01, 0xFF])       # 距离 2047，但没有历史
        with self.assertRaises(mobi.MOBIError):
            mobi.decompress_palmdoc(bad)


class TestMOBIRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_text_roundtrip(self):
        book = sample_book()
        f = self.d / "a.mobi"
        f.write_bytes(mobi.to_bytes(book))
        back = mobi.read(f)
        self.assertEqual([c.title for c in back.chapters], ["第一章", "第二章"])
        self.assertEqual(
            [b.text for c in back.chapters for b in c.blocks if hasattr(b, "text")],
            ["第一段。", "第二段。", "结尾。"])
        self.assertEqual(back.metadata.get("title"), "测试书")

    def test_image_roundtrip_bytes(self):
        book = sample_book(images=3)
        f = self.d / "b.mobi"
        f.write_bytes(mobi.to_bytes(book))
        back = mobi.read(f)
        src = [b.data for c in book.chapters for b in c.blocks if isinstance(b, ImageBlock)]
        got = [b.data for c in back.chapters for b in c.blocks if isinstance(b, ImageBlock)]
        self.assertEqual(len(got), len(src))
        self.assertEqual([hashlib.md5(x).hexdigest() for x in got],
                         [hashlib.md5(x).hexdigest() for x in src])

    def test_palmdb_structure(self):
        f = self.d / "c.mobi"
        f.write_bytes(mobi.to_bytes(sample_book()))
        data = f.read_bytes()
        self.assertEqual(data[60:64], b"BOOK")
        self.assertEqual(data[64:68], b"MOBI")
        n = struct.unpack(">H", data[76:78])[0]
        self.assertGreaterEqual(n, 2)
        db = mobi.read_palmdb(f)
        self.assertEqual(len(db.records), n)
        self.assertEqual(db.records[0][16:20], b"MOBI")

    def test_html_is_complete_document(self):
        """正文必须是完整 HTML 文档，否则严格阅读器只解析出第一段"""
        f = self.d / "d.mobi"
        f.write_bytes(mobi.to_bytes(sample_book()))
        db = mobi.read_palmdb(f)
        hdr = mobi.parse_header(db)
        text = b"".join(mobi.decompress_palmdoc(db.records[i])
                        if hdr.compression == 2 else db.records[i]
                        for i in range(1, hdr.text_records + 1))
        self.assertTrue(text.startswith(b"<html>"))
        self.assertIn(b"</body></html>", text)


class TestMOBIReject(unittest.TestCase):
    def _write_header(self, version: int, encryption: int = 0,
                      compression: int = 1) -> Path:
        d = Path(tempfile.mkdtemp())
        f = d / "x.mobi"
        data = bytearray(mobi.to_bytes(sample_book()))
        # 改记录 0 里的 version / encryption / compression
        # PalmDOC 头：0-1 compression / 4-7 text_length / 8-9 text_records
        #              10-11 record_size / 12-13 encryption
        r0_off = struct.unpack(">I", data[78:82])[0]
        struct.pack_into(">H", data, r0_off, compression)
        struct.pack_into(">H", data, r0_off + 12, encryption)
        struct.pack_into(">I", data, r0_off + 36, version)
        f.write_bytes(bytes(data))
        return f

    def test_azw3_rejected(self):
        f = self._write_header(version=8)
        with self.assertRaises(mobi.MOBIError) as ctx:
            mobi.read(f)
        self.assertIn("AZW3", str(ctx.exception))

    def test_drm_rejected(self):
        f = self._write_header(version=6, encryption=2)
        with self.assertRaises(mobi.MOBIError) as ctx:
            mobi.read(f)
        self.assertIn("DRM", str(ctx.exception))

    def test_huff_rejected(self):
        f = self._write_header(version=6, compression=17480)
        with self.assertRaises(mobi.MOBIError) as ctx:
            mobi.read(f)
        self.assertIn("HUFF", str(ctx.exception))


class TestComicMOBI(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.f = self.d / "comic.mobi"
        self.f.write_bytes(mobi.to_bytes(sample_book(images=5)))

    def test_to_comic(self):
        book = mobi.to_comic(self.f)
        self.assertEqual(book.page_count, 5)

    def test_comic_mobi_to_cbz_lossless(self):
        out = self.d / "a.cbz"
        r = convert(self.f, "cbz", out=out)
        self.assertEqual(r["pages"], 5)
        src = [b.data for c in mobi.read(self.f).chapters
               for b in c.blocks if isinstance(b, ImageBlock)]
        with zipfile.ZipFile(out) as zf:
            got = [zf.read(n) for n in sorted(zf.namelist())
                   if n.lower().endswith(".png")]
        self.assertEqual([hashlib.md5(x).hexdigest() for x in got],
                         [hashlib.md5(x).hexdigest() for x in src])

    def test_comic_mobi_to_pdf_and_epub(self):
        for tgt in ("pdf", "epub"):
            out = self.d / f"x.{tgt}"
            r = convert(self.f, tgt, out=out)
            self.assertEqual(r["pages"], 5)

    def test_novel_mobi_not_comic(self):
        """只有封面图的 mobi 不该当漫画"""
        f = self.d / "novel.mobi"
        f.write_bytes(mobi.to_bytes(sample_book(images=1)))
        with self.assertRaises(mobi.MOBIError):
            mobi.to_comic(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
