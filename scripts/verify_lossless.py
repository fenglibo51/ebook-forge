#!/usr/bin/env python3
"""无损校验：把两个容器里的图片逐张比对，判据必须同层次。

用法：
    python3 scripts/verify_lossless.py 源.cbz 目标.pdf
    python3 scripts/verify_lossless.py 源.epub 目标.cbz

比对规则（关键，别搞混层次）：

    JPEG / JP2     直接比原始图片字节
    PNG（直通）    比 PNG 的 IDAT 字节 vs PDF 里 FlateDecode 流的原始字节
                   —— 我们就是把 IDAT 原样塞进 PDF，所以这两个必须完全相同
    PNG（拆 SMask）  比解压后的像素

判定：
    ① 页数一致
    ② 每页的「比对层次」一致（JPEG 对 JPEG、IDAT 对 IDAT）
    ③ 字节完全一致 → 无损
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookforge.formats import cbz, epub, png           # noqa: E402
from bookforge.pdf import extract                      # noqa: E402

IMG_EXTS = (".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".tif", ".tiff", ".webp", ".gif")


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _payload(data: bytes) -> tuple[bytes, str]:
    """容器里的图片 → (可比对字节, 层次标签)"""
    w, h, fmt = png.image_size(data)
    if fmt == "png":
        try:
            return png.idat_bytes(data), "png-idat"
        except Exception:
            return data, "png-bytes"
    if fmt in ("jpeg", "jp2"):
        return data, fmt
    return data, fmt or "raw"


def images_from_zip(path: Path) -> list[tuple[bytes, str]]:
    with zipfile.ZipFile(path) as zf:
        names = sorted(
            (n for n in zf.namelist()
             if Path(n).suffix.lower() in IMG_EXTS
             and not n.startswith("__MACOSX")
             and not Path(n).name.startswith("._")),
            key=cbz.natural_key)
        return [_payload(zf.read(n)) for n in names]


def images_from_epub(path: Path) -> list[tuple[bytes, str]]:
    return [_payload(p.data) for p in epub.read(path).pages]


def images_from_pdf(path: Path) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for img in extract.extract_images(str(path)):
        f = img.filters
        if f == ["DCTDecode"]:
            out.append((img.data, "jpeg"))
        elif f == ["JPXDecode"]:
            out.append((img.data, "jp2"))
        elif f == ["FlateDecode"] and img.predictor == 15:
            # PNG 直通：raw_stream 就是源 PNG 的 IDAT
            out.append((img.raw_stream, "png-idat"))
        elif f == ["FlateDecode"]:
            ch = 3 if "RGB" in img.colorspace else 1
            h = len(img.data) // (img.columns * ch) if img.columns else 0
            if h:
                out.append((png.encode(img.data, img.columns, h, ch), "png-pixels"))
            else:
                out.append((img.data, "raw"))
        else:
            out.append((img.data, ",".join(f) or "raw"))
    return out


def load_any(path: Path) -> list[tuple[bytes, str]]:
    ext = path.suffix.lower()
    if ext in (".cbz", ".zip"):
        return images_from_zip(path)
    if ext == ".epub":
        return images_from_epub(path)
    if ext == ".pdf":
        return images_from_pdf(path)
    raise SystemExit(f"不认识的容器：{ext}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ia, ib = load_any(a), load_any(b)

    print(f"源    {a.name}: {len(ia)} 张")
    print(f"目标  {b.name}: {len(ib)} 张")
    if len(ia) != len(ib):
        print(f"❌ 页数不一致（{len(ia)} vs {len(ib)}）")
        return 1

    bad_layer = [(i, x[1], y[1]) for i, (x, y) in enumerate(zip(ia, ib), 1)
                 if x[1] != y[1]]
    if bad_layer:
        i, la, lb = bad_layer[0]
        print(f"❌ 第 {i} 张比对层次不同（源 {la} / 目标 {lb}），无法判定无损")
        return 1

    same = sum(1 for (x, _), (y, _) in zip(ia, ib) if md5(x) == md5(y))
    layer = ia[0][1] if ia else "?"
    print(f"比对层次 {layer}   字节一致 {same}/{len(ia)}  ({same / len(ia):.1%})")
    if same != len(ia):
        for i, ((x, _), (y, _)) in enumerate(zip(ia, ib), 1):
            if md5(x) != md5(y):
                print(f"  第 {i} 张不一致：{len(x)}B vs {len(y)}B")
                print(f"    源   {md5(x)}")
                print(f"    目标 {md5(y)}")
                break
        print("⚠️ 不是字节级无损")
        return 1
    print("✅ 逐张字节一致，无损")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
