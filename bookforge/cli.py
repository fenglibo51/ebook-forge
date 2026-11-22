"""命令行入口。

    python3 -m bookforge convert 漫画.cbz pdf --dpi 300
    python3 -m bookforge convert 漫画.epub cbz -o 输出.cbz
    python3 -m bookforge verify 源.cbz 目标.pdf
"""

from __future__ import annotations

import argparse
import json
import sys

from . import ConvertError, __version__, convert


def cmd_convert(args) -> int:
    try:
        r = convert(args.src, args.target, out=args.out, dpi=args.dpi,
                    fit=args.fit, page_size=args.page_size)
    except (ConvertError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print(f"✅ {r['out']}  {r['pages']} 页  mode={r['mode']}")
        if r.get("note"):
            print(f"   {r['note']}")
    return 0


def cmd_verify(args) -> int:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                         .parent.parent / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "verify_lossless",
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "scripts" / "verify_lossless.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.argv = ["verify_lossless", args.src, args.out]
    return mod.main()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bookforge",
                                description="电子书/漫画格式转换（纯自研引擎）")
    p.add_argument("-V", "--version", action="version", version=f"bookforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="转换单个文件")
    c.add_argument("src")
    c.add_argument("target", help="cbz / zip / epub / pdf")
    c.add_argument("-o", "--out")
    c.add_argument("--dpi", type=float, default=300.0,
                   help="转 PDF 时的页面换算密度，默认 300（漫画扫描件主流）")
    c.add_argument("--fit", choices=["native", "page"], default="native",
                   help="native=页面跟图片走；page=等比缩进 page-size")
    c.add_argument("--page-size", choices=["a5", "a4"], default="a5")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_convert)

    v = sub.add_parser("verify", help="逐张 md5 校验是否无损")
    v.add_argument("src")
    v.add_argument("out")
    v.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
