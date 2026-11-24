"""v1.1.1 深度审查（基准 74a04d8）的 2 个 P1 回归测试。

  P1-01  PDF/PNG 里的 zlib 流是无界解压 —— 压缩炸弹能直接把进程打死
  P1-02  同一次 multipart 请求的多个文件，配额被重复计算

P2/P3（正式 tag 可重建覆盖、撤销 token 不取消在途任务、目录扫描跟随 symlink、
目录名首尾空格）本轮不改，风险写进 README 的「已知限制」，不在本文件断言。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _testenv                                            # noqa: E402,F401

from fastapi.testclient import TestClient                  # noqa: E402

from backend import store, uploads                         # noqa: E402
from backend.main import app                               # noqa: E402
from bookforge.formats import limits, png                  # noqa: E402

MB = 1024 * 1024


def _admin_headers() -> dict:
    """取当前生效的管理令牌。

    全量跑测试时前面的用例会把 settings.json 里的 token 换成别的值，
    写死会 401。分享没开、也没环境变量令牌时是免密的，返回空 dict。
    """
    tok = str(store.load_settings().get("admin_token", "") or "")
    return {"X-Admin-Token": tok} if tok else {}


class InflateLimitTests(unittest.TestCase):
    """P1-01：zlib 解压必须有上限，而且要在分配大块内存之前就停。

    复现基线：2MB 全零数据压成 2055 字节（1021 倍），
    `zlib.decompress` 会先解出 2MB 再返回 —— 换成 2GB 的炸弹就是一次 OOM kill。
    """

    def test_bomb_rejected(self):
        packed = zlib.compress(b"\x00" * (2 * MB), 9)
        self.assertLess(len(packed), 4096, "测试载荷应该是高压缩比")
        with self.assertRaises(limits.LimitError) as ctx:
            limits.inflate(packed, 64 * 1024, what="测试炸弹 ")
        self.assertIn("超过", str(ctx.exception))

    def test_normal_data_untouched(self):
        data = b"hello world " * 1000
        self.assertEqual(limits.inflate(zlib.compress(data)), data)

    def test_just_under_limit_ok(self):
        data = b"x" * (64 * 1024)
        self.assertEqual(limits.inflate(zlib.compress(data, 9), 64 * 1024), data)

    def test_corrupt_data_raises_zlib_error_not_limit(self):
        """损坏和超限要能区分 —— 前者调用方是「跳过」，后者要「整个文件失败」。"""
        with self.assertRaises(zlib.error):
            limits.inflate(b"not zlib data at all", 1000)

    def test_default_limits_exist(self):
        self.assertGreater(limits.MAX_INFLATE_BYTES, 0)
        self.assertGreater(limits.MAX_PDF_STREAM_BYTES, 0)
        self.assertLess(limits.MAX_PDF_STREAM_BYTES, limits.MAX_INFLATE_BYTES,
                        "PDF 内部流的预算应该比通用图片流更紧")


class PngBombTests(unittest.TestCase):
    """PNG 的 IDAT 要用 IHDR 算出的预期大小当上限，不是套用通用值。"""

    @staticmethod
    def _chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    def _png(self, w, h, idat_payload, color_type=2):
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
        return sig + self._chunk(b"IHDR", ihdr) + self._chunk(b"IDAT", idat_payload) \
            + self._chunk(b"IEND", b"")

    def test_bomb_rejected_before_allocation(self):
        """IHDR 说 10×10（应解 310 字节），IDAT 想解出 10MB。"""
        bomb = self._png(10, 10, zlib.compress(b"\x00" * (10 * MB), 9))
        self.assertLess(len(bomb), 64 * 1024, "炸弹文件本身应该很小")
        with self.assertRaises(Exception) as ctx:
            png.decode(bomb)
        self.assertIsInstance(ctx.exception, limits.LimitError)

    def test_normal_png_still_decodes(self):
        w = h = 4
        raw = b"".join(b"\x00" + bytes([255, 0, 0]) * w for _ in range(h))
        px, info, ch = png.decode(self._png(w, h, zlib.compress(raw, 9)))
        self.assertEqual(len(px), w * h * ch)
        self.assertEqual(px[:3], bytes([255, 0, 0]))

    def test_large_real_image_still_decodes(self):
        """2000×2000 RGB（原始 11.4MB）不能被误杀。"""
        w = h = 2000
        raw = b"".join(b"\x00" + bytes([10, 20, 30]) * w for _ in range(h))
        px, _, ch = png.decode(self._png(w, h, zlib.compress(raw, 1)))
        self.assertEqual(len(px), w * h * ch)


class PdfStreamBombTests(unittest.TestCase):
    """PDF Object Stream 是打开文件的必经路径，这里的炸弹最危险。"""

    @staticmethod
    def _obj(n, body, stream=None):
        if stream is None:
            return f"{n} 0 obj\n{body}\nendobj\n".encode()
        return (f"{n} 0 obj\n{body}\nstream\n".encode() + stream
                + b"\nendstream\nendobj\n")

    def _pdf_with_objstm(self, bomb_mb):
        bomb = zlib.compress(b"\x00" * (bomb_mb * MB), 9)
        return b"".join([
            b"%PDF-1.4\n",
            self._obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
            self._obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            self._obj(3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"),
            self._obj(4, f"<< /Type /ObjStm /N 0 /First 0 /Filter /FlateDecode "
                         f"/Length {len(bomb)} >>", bomb),
        ]) + b"\ntrailer\n<< /Root 1 0 R /Size 5 >>\n%%EOF\n"

    def test_giant_objstm_rejected(self):
        from bookforge.pdf.reader import PDFDocument
        bomb_mb = limits.MAX_PDF_STREAM_BYTES // MB + 20
        tmp = Path(tempfile.mkdtemp(prefix="pdfbomb-"))
        p = tmp / "bomb.pdf"
        p.write_bytes(self._pdf_with_objstm(bomb_mb))
        self.assertLess(p.stat().st_size, 1024 * 1024, "炸弹文件本身应该很小")
        with self.assertRaises(Exception) as ctx:
            PDFDocument(p)
        self.assertIsInstance(ctx.exception, limits.LimitError)

    def test_small_objstm_passes(self):
        from bookforge.pdf.reader import PDFDocument
        tmp = Path(tempfile.mkdtemp(prefix="pdfok-"))
        p = tmp / "ok.pdf"
        p.write_bytes(self._pdf_with_objstm(1))
        doc = PDFDocument(p)
        self.assertEqual(len(doc.page_objects()), 1)


class BatchQuotaTests(unittest.TestCase):
    """P1-02：一次 multipart 传多个文件时，配额不能被算两次。

    复现基线（实测，404 之前）：4MB 额度下传 2MB + 1MB（真实 3MB）→ 413。
    根因：第一个文件 register() 进了 _live，但 Ticket 仍把它算在 inflight 里，
    第二次 admit() 看到 _live(2MB) + inflight(2MB) + 新文件(1MB) = 5MB。
    """

    def setUp(self):
        self.c = TestClient(app, raise_server_exceptions=False)
        self._saved = (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
                       uploads.QUOTA_SID_BYTES)
        self._hard_reset(4)

    def tearDown(self):
        (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
         uploads.QUOTA_SID_BYTES) = self._saved
        uploads._live.update({"total": 0, "count": 0, "tok": {}, "sid": {}, "loaded": False})
        uploads._inflight["total"] = 0
        uploads._inflight_token.clear()
        uploads._inflight_sid.clear()

    def _hard_reset(self, total_mb: int):
        up = store.DATA_DIR / "uploads"
        if up.exists():
            for f in up.iterdir():
                if f.is_file():
                    f.unlink()
        idx = store.DATA_DIR / "uploads.json"
        if idx.exists():
            idx.unlink()
        uploads.QUOTA_TOTAL_BYTES = total_mb * MB
        uploads.QUOTA_TOKEN_BYTES = 64 * MB
        uploads.QUOTA_SID_BYTES = 64 * MB
        uploads._live.update({"total": 0, "count": 0, "tok": {}, "sid": {}, "loaded": False})
        uploads._inflight["total"] = 0
        uploads._inflight_token.clear()
        uploads._inflight_sid.clear()

    def _batch(self, sizes):
        import uuid
        b = "----b" + uuid.uuid4().hex
        body = b""
        for i, n in enumerate(sizes):
            body += (f'--{b}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="f{i}.bin"\r\nContent-Type: application/octet-stream'
                     f'\r\n\r\n').encode() + b"x" * n + b"\r\n"
        body += f"--{b}--\r\n".encode()
        hdr = {"Content-Type": f"multipart/form-data; boundary={b}"}
        hdr.update(_admin_headers())
        return self.c.post("/api/upload", content=body, headers=hdr)

    def _ondisk(self) -> int:
        up = store.DATA_DIR / "uploads"
        return sum(f.stat().st_size for f in up.iterdir() if f.is_file()) if up.exists() else 0

    def test_two_files_under_quota_succeeds(self):
        """4MB 额度传 2MB + 1MB（真实 3MB）必须成功 —— 这就是本轮修的那个。"""
        r = self._batch([2 * MB, 1 * MB])
        self.assertEqual(r.status_code, 200, f"应该成功，实际 {r.status_code}: {r.text[:120]}")
        self.assertEqual(self._ondisk(), 3 * MB)
        self.assertEqual(uploads._inflight["total"], 0, "finish 之后 inflight 要归零")

    def test_exact_boundary_succeeds(self):
        for sizes in ([1 * MB] * 4, [3 * MB, 1 * MB]):
            self._hard_reset(4)
            r = self._batch(sizes)
            self.assertEqual(r.status_code, 200,
                             f"{[s//MB for s in sizes]} 正好 4MB 应该通过，实际 {r.status_code}")

    def test_over_quota_rejected_and_rolled_back(self):
        """4MB 额度传 2MB + 3MB（真实 5MB）→ 413 且整批清空。"""
        r = self._batch([2 * MB, 3 * MB])
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self._ondisk(), 0, "失败要整批回滚，磁盘不能留文件")
        self.assertEqual(uploads._live["total"], 0)
        self.assertEqual(uploads._inflight["total"], 0, "inflight 不能泄漏")

    def test_five_files_over_quota(self):
        r = self._batch([1 * MB] * 5)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self._ondisk(), 0)

    def test_sequential_requests_accumulate_correctly(self):
        """两个 2MB 请求，总 4MB —— 都该成功，且总量正好 4MB。"""
        self.assertEqual(self._batch([2 * MB]).status_code, 200)
        self.assertEqual(self._batch([2 * MB]).status_code, 200)
        self.assertEqual(uploads._live["total"], 4 * MB)

    def test_commit_never_goes_negative(self):
        """commit 传超过 written 的值不能把账本搞成负数。"""
        t = uploads.begin("", "")
        t.admit(1000)
        t.commit(99999)          # 故意超
        self.assertGreaterEqual(t.written, 0)
        self.assertGreaterEqual(uploads._inflight["total"], 0)
        t.finish()


if __name__ == "__main__":
    unittest.main()
