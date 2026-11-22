#!/usr/bin/env python3
"""无损校验：把两个容器里的图片逐张比对 md5。

用法：
    python3 scripts/verify_lossless.py 源.cbz 目标.pdf
    python3 scripts/verify_lossless.py 源.epub 目标.cbz

判据（三条都要过）：
    ① 页数对得上
    ② 逐张字节一致（md5）
    ③ 首图顺序没被打乱（同一批字节，顺序一致）

PDF 只比对 DCTDecode / JPXDecode 这类原样嵌入的图（JPEG / JPEG2000），
Flate 直通的 PNG 走像素级比对，见 --allow-flate。
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookforge.formats import cbz, epub, png           # noqa: E402
from bookforge.pdf import extract                      # noqa: E402

IMG_EXTS = (".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".tif", ".tiff", ".webp", ".gif")


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def images_from_zip(path: Path) -> list[bytes]:
    with zipfile.ZipFile(path) as zf:
        names = sorted(
            (n for n in zf.namelist()
             if Path(n).suffix.lower() in IMG_EXTS and not n.startswith("__MACOSX")),
            key=cbz.natural_key)
        return [zf.read(n) for n in names]


def images_from_epub(path: Path) -> list[bytes]:
    return [p.data for p in epub.read(path).pages]


def images_from_pdf(path: Path) -> tuple[list[bytes], list[str]]:
    raw = extract.extract_images(str(path))
    out, kinds = [], []
    for img in raw:
        data, kind = img.to_image_bytes()
        if kind == "raw" and img.predictor == 15 and img.columns:
            # PNG 直通：把反预测后的像素重新拼成 PNG 再比（像素级比对）
            ch = 3 if "RGB" in img.colorspace else 1
            data = png.encode(data, img.columns, len(data) // (img.columns * ch), ch)
            kind = "png-rebuilt"
        out.append(data)
        kinds.append(kind)
    return out, kinds


def load_any(path: Path) -> list[bytes]:
    ext = path.suffix.lower()
    if ext in (".cbz", ".zip"):
        return images_from_zip(path)
    if ext == ".epub":
        return images_from_epub(path)
    if ext == ".pdf":
        return images_from_pdf(path)[0]
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

    same = sum(1 for x, y in zip(ia, ib) if md5(x) == md5(y))
    print(f"字节一致 {same}/{len(ia)}  ({same / len(ia):.1%})")
    if same != len(ia):
        for i, (x, y) in enumerate(zip(ia, ib), 1):
            if md5(x) != md5(y):
                print(f"  第 {i} 张不一致：源 {len(x)}B / 目标 {len(y)}B")
                print(f"    源   {md5(x)}")
                print(f"    目标 {md5(y)}")
                break
        print("⚠️ 不是字节级无损（可能走了像素级直通或重新编码）")
        return 1
    print("✅ 逐张字节一致，无损")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
