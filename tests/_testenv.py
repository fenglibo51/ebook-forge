"""所有测试共用的临时数据目录（在任何人 import backend 之前设好）。

⚠️ 为什么需要这个文件：
`backend.store` 在 import 的瞬间就把 DATA_DIR 定死了（`DATA_DIR = Path(os.environ[...])`）。
于是「哪个测试文件先 import backend，整个进程的数据目录就由它决定」。

以前只有 test_share_security 设了自己的临时目录：
  - 单跑 `python -m unittest tests.test_share_security` —— 它是第一个 import 的，没事；
  - 一起跑 `discover -s tests` —— 别的文件先 import 了 backend，DATA_DIR 成了项目根的 data/，
    而 test_share_security 里硬编码的 `_TMP / "settings.json"` 根本不存在
    → `FileNotFoundError: /tmp/ebf_audit_xxx/settings.json`。

现在每个测试文件第一件事就 import 本模块，环境变量保证先设好，全进程共用一份目录。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="ebf_tests_"))
os.environ["EBOOKFORGE_DATA"] = str(ROOT)
os.environ["EBOOKFORGE_BOOK_DIR"] = str(ROOT / "Book")

(ROOT / "uploads").mkdir(parents=True, exist_ok=True)
(ROOT / "Book").mkdir(parents=True, exist_ok=True)


def reset() -> None:
    """把数据目录清成干净状态，但保留目录本身（store/backend 的模块常量指着它）。"""
    import shutil

    for child in ROOT.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    (ROOT / "uploads").mkdir(parents=True, exist_ok=True)
    (ROOT / "Book").mkdir(parents=True, exist_ok=True)
