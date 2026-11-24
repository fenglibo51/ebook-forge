"""v1.1.3 复审（基准 4101068）的 1 个 P1 + 1 个 P2 回归测试。

  P1  同一本 PDF 的多个 /ObjStm 没有累计预算 —— 单个都合规，加起来爆内存
  P2  批量上传在 open('xb') 阶段失败时，不在整批回滚的 try 里

P2 是上一轮「整批原子」的补丁留了个口子：回滚逻辑写好了，
但文件创建那一段在 try 外面，根本走不到回滚。
"""

from __future__ import annotations

import builtins
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _testenv                                            # noqa: E402,F401

from fastapi.testclient import TestClient                  # noqa: E402

from backend import store, uploads                         # noqa: E402
from backend.main import app                               # noqa: E402
from bookforge.formats import limits                       # noqa: E402
from bookforge.pdf.reader import PDFDocument               # noqa: E402

MB = 1024 * 1024


def _obj(n, body, stream=None):
    if stream is None:
        return f"{n} 0 obj\n{body}\nendobj\n".encode()
    return (f"{n} 0 obj\n{body}\nstream\n".encode() + stream
            + b"\nendstream\nendobj\n")


def build_pdf_with_objstms(n_streams: int, mb_each: int) -> bytes:
    """造一本带 n 个 /ObjStm 的 PDF，每个解压后约 mb_each MB。"""
    parts = [b"%PDF-1.5\n",
             _obj(1, "<< /Type /Catalog /Pages 2 0 R >>"),
             _obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
             _obj(3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>")]
    for i in range(n_streams):
        onum = 100 + i
        header = f"{onum} 0\n".encode()
        content = b"<</X" + b"y" * (mb_each * MB - len(header) - 10) + b">>"
        data = header + content
        packed = zlib.compress(data, 9)
        parts.append(_obj(
            10 + i,
            f"<< /Type /ObjStm /N 1 /First {len(header)} "
            f"/Filter /FlateDecode /Length {len(packed)} >>", packed))
    return b"".join(parts) + b"\ntrailer\n<< /Root 1 0 R /Size 200 >>\n%%EOF\n"


class ObjStmTotalBudgetTests(unittest.TestCase):
    """P1：单流限额挡不住「很多个都合规」。

    复现基线（实测）：单流预算 2MB、累计预算 4MB 时，
    6 个各 1MB 的 ObjStm 在修复前照常打开。
    """

    def setUp(self):
        self._saved = (limits.MAX_PDF_STREAM_BYTES, limits.MAX_PDF_OBJSTM_TOTAL_BYTES)
        limits.MAX_PDF_STREAM_BYTES = 2 * MB
        limits.MAX_PDF_OBJSTM_TOTAL_BYTES = 4 * MB
        self.tmp = Path(tempfile.mkdtemp(prefix="objstm-"))

    def tearDown(self):
        limits.MAX_PDF_STREAM_BYTES, limits.MAX_PDF_OBJSTM_TOTAL_BYTES = self._saved

    def test_cumulative_over_budget_rejected(self):
        """6 个各 1MB：每个都在单流 2MB 预算内，但累计 6MB 超过 4MB。"""
        p = self.tmp / "six.pdf"
        p.write_bytes(build_pdf_with_objstms(6, 1))
        self.assertLess(p.stat().st_size, 200 * 1024, "测试文件本身应该很小")
        with self.assertRaises(limits.LimitError) as ctx:
            PDFDocument(p)
        self.assertIn("累计", str(ctx.exception))

    def test_under_budget_still_opens(self):
        """3 个各 1MB（累计 3MB）应该正常打开 —— 别误伤复杂但合法的 PDF。"""
        p = self.tmp / "three.pdf"
        p.write_bytes(build_pdf_with_objstms(3, 1))
        doc = PDFDocument(p)
        self.assertEqual(len(doc.page_objects()), 1)

    def test_single_stream_over_budget_still_rejected(self):
        """单个流超单流预算 —— 这条上一轮就有了，别退化。"""
        p = self.tmp / "one_big.pdf"
        p.write_bytes(build_pdf_with_objstms(1, 3))
        with self.assertRaises(limits.LimitError):
            PDFDocument(p)

    def test_default_limits_sane(self):
        self.assertGreater(limits.MAX_PDF_OBJSTM_TOTAL_BYTES, 0)
        self.assertGreater(limits.MAX_PDF_OBJSTM_TOTAL_BYTES,
                           limits.MAX_PDF_STREAM_BYTES,
                           "累计预算应该大于单流预算，否则单流那条就够用了")


class BatchCreateFailureTests(unittest.TestCase):
    """P2：open('xb') 失败也要走整批回滚。

    复现基线（实测）：两个文件，第二个 open 抛 OSError(ENOSPC) →
    HTTP 500，但第一个文件留在磁盘和 uploads.json 里，还占着额度。
    """

    def setUp(self):
        self.c = TestClient(app, raise_server_exceptions=False)
        self._clean()
        self._saved = (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
                       uploads.QUOTA_SID_BYTES)
        uploads.QUOTA_TOTAL_BYTES = uploads.QUOTA_TOKEN_BYTES = \
            uploads.QUOTA_SID_BYTES = 64 * MB
        uploads._live.update({"total": 0, "count": 0, "tok": {}, "sid": {}, "loaded": True})
        uploads._inflight["total"] = 0
        uploads._inflight_token.clear()
        uploads._inflight_sid.clear()

    def tearDown(self):
        (uploads.QUOTA_TOTAL_BYTES, uploads.QUOTA_TOKEN_BYTES,
         uploads.QUOTA_SID_BYTES) = self._saved
        self._clean()

    def _clean(self):
        up = store.DATA_DIR / "uploads"
        if up.exists():
            for f in up.iterdir():
                if f.is_file():
                    f.unlink()
        idx = store.DATA_DIR / "uploads.json"
        if idx.exists():
            idx.unlink()

    def _names(self):
        up = store.DATA_DIR / "uploads"
        return sorted(p.name for p in up.iterdir() if p.is_file()) if up.exists() else []

    def _idx(self) -> int:
        f = store.DATA_DIR / "uploads.json"
        if not f.exists():
            return 0
        try:
            return len(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return -1

    def _batch(self, names):
        import uuid
        b = "----b" + uuid.uuid4().hex
        body = b""
        for n in names:
            body += (f'--{b}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{n}"\r\nContent-Type: application/octet-stream'
                     f'\r\n\r\n').encode() + b"x" * 200 + b"\r\n"
        body += f"--{b}--\r\n".encode()
        hdr = {"Content-Type": f"multipart/form-data; boundary={b}"}
        tok = str(store.load_settings().get("admin_token", "") or "")
        if tok:
            hdr["X-Admin-Token"] = tok
        return self.c.post("/api/upload", content=body, headers=hdr)

    def test_open_failure_rolls_back_whole_batch(self):
        """第 2 个文件 open 失败 → 整批回滚，磁盘和索引都不能留东西。"""
        real_open = builtins.open
        calls = {"n": 0}

        def flaky(file, mode="r", *a, **kw):
            if "xb" in str(mode):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError(28, "No space left on device")
            return real_open(file, mode, *a, **kw)

        with mock.patch("builtins.open", flaky):
            r = self._batch(["a.txt", "b.txt"])

        self.assertGreaterEqual(r.status_code, 500)
        self.assertEqual(self._names(), [], "第一个文件没回滚掉")
        self.assertEqual(self._idx(), 0, "索引里还有本批次的记录")
        self.assertEqual(uploads._inflight["total"], 0, "inflight 泄漏了")

    def test_name_exhaustion_rolls_back(self):
        """连续 5 次重名 → dest is None → 也要整批回滚。"""
        (store.DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(uploads, "new_file_id", return_value="clash"):
            (store.DATA_DIR / "uploads" / "clash.txt").write_bytes(b"x")
            r = self._batch(["c.txt"])
        self.assertGreaterEqual(r.status_code, 500)
        # 只留那个占位文件（不是本批次创建的），不能有本批次的东西
        self.assertEqual(self._names(), ["clash.txt"])

    def test_normal_batch_still_works(self):
        r = self._batch(["d.txt", "e.txt"])
        self.assertEqual(r.status_code, 200, r.text[:120])
        self.assertEqual(len(self._names()), 2)
        self.assertEqual(self._idx(), 2)

    def test_quota_reusable_after_failure(self):
        """失败之后额度和目录都要能正常复用。"""
        real_open = builtins.open
        calls = {"n": 0}

        def flaky(file, mode="r", *a, **kw):
            if "xb" in str(mode):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError(28, "No space left on device")
            return real_open(file, mode, *a, **kw)

        with mock.patch("builtins.open", flaky):
            self._batch(["f.txt", "g.txt"])
        self.assertEqual(uploads._inflight["total"], 0)

        r = self._batch(["h.txt", "i.txt"])
        self.assertEqual(r.status_code, 200, "失败一次之后正常批次应该还能跑")
        self.assertEqual(len(self._names()), 2)


if __name__ == "__main__":
    unittest.main()
