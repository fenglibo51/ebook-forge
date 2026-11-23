"""PWA 与静态资源契约测试（P2-06）。

不跑真浏览器（CI 里没有），改成对 sw.js / manifest.json / index.html 做
**契约检查** —— 这几条一旦破了，症状全是「用户那边还是旧版本」「装到桌面
一打开白屏」，而且很难靠人眼看出来：

    ① 缓存名必须带版本，且 activate 里要删旧缓存（否则旧壳永远堆着）
    ② /api/* 绝不能进缓存（缓存了任务进度和文件列表就永远显示旧的）
    ③ js/css 必须网络优先 —— 缓存优先会让用户升级后还跑旧代码
    ④ manifest 里引用的图标必须真实存在
    ⑤ 分享页在 /s/<token> 下打开，所有静态资源必须是绝对路径
       （相对路径会被解析成 /s/css/... 然后 404，这个坑踩过一次）
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"
SW = FRONT / "sw.js"
MANIFEST = FRONT / "manifest.json"
INDEX = FRONT / "index.html"


class ServiceWorkerContractTests(unittest.TestCase):
    def setUp(self):
        self.src = SW.read_text(encoding="utf-8")

    def test_cache_name_is_versioned(self):
        """缓存名必须能区分版本，否则改了策略用户也拿不到。"""
        self.assertRegex(self.src, r"const\s+VERSION\s*=\s*\d+")
        self.assertRegex(self.src, r"const\s+CACHE\s*=\s*`ebf-v\$\{VERSION\}`")

    def test_old_caches_deleted_on_activate(self):
        """activate 里必须删掉不等于当前版本的缓存。"""
        self.assertIn("caches.keys()", self.src)
        self.assertIn("k !== CACHE", self.src)
        self.assertIn("caches.delete(k)", self.src)

    def test_api_never_cached(self):
        """接口一律直连 —— 缓存任务进度会显示过期数据。"""
        self.assertIn("startsWith('/api/')", self.src)

    def test_code_is_network_first(self):
        """js/css 必须网络优先。

        这条最容易写反：一旦写成缓存优先，用户点刷新还是旧 app.js，
        表现出来就是「你说修好了但我这还坏着」。
        """
        m = re.search(r"const isCode\s*=.*?\n(.*?)return;", self.src, re.S)
        self.assertIsNotNone(m, "找不到 isCode 分支")
        body = m.group(1)
        fetch_pos = body.find("fetch(req)")
        cache_pos = body.find("caches.match(req)")
        self.assertGreater(fetch_pos, -1, "isCode 分支里没有网络请求")
        self.assertGreater(cache_pos, -1, "isCode 分支没有离线回退")
        self.assertLess(fetch_pos, cache_pos, "js/css 写成了缓存优先，升级后用户会跑到旧代码")

    def test_shell_listed_files_exist(self):
        """预缓存清单里的每个路径都必须有对应文件，否则安装时报 404。"""
        block = re.search(r"const SHELL\s*=\s*\[(.*?)\];", self.src, re.S)
        self.assertIsNotNone(block, "找不到 SHELL 清单")
        paths = re.findall(r"'([^']+)'", block.group(1))
        self.assertGreater(len(paths), 5)
        missing = [p for p in paths if not (FRONT / p.lstrip("/")).exists()]
        self.assertEqual(missing, [], f"预缓存清单里有不存在的文件：{missing}")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.m = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_required_fields(self):
        for key in ("name", "short_name", "start_url", "display", "icons", "theme_color", "background_color"):
            self.assertIn(key, self.m, f"manifest 缺 {key}")
        self.assertEqual(self.m["display"], "standalone")

    def test_icons_exist_and_cover_sizes(self):
        sizes = set()
        for icon in self.m["icons"]:
            src = icon["src"].lstrip("/")
            self.assertTrue((FRONT / src).exists(), f"图标不存在：{icon['src']}")
            sizes.add(icon["sizes"])
        # 192 + 512 是 Chrome 装到桌面的最低要求
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)


class SharePagePathTests(unittest.TestCase):
    """分享页在 /s/<token> 下打开，相对路径会被解析错。"""

    def test_all_static_refs_are_absolute(self):
        html = INDEX.read_text(encoding="utf-8")
        # /s/<token> 里 page 的 href/src 必须以 / 开头
        bad = []
        for m in re.finditer(r'(?:src|href)="([^"]+)"', html):
            val = m.group(1)
            if val.startswith(("http", "//", "#", "mailto:", "data:", "/")):
                continue
            bad.append(val)
        self.assertEqual(bad, [], f"这些静态资源是相对路径，在 /s/<token> 下会 404：{bad}")

    def test_no_leftover_token_in_query(self):
        """管理令牌不能走 ?k= —— 会留在浏览器历史和反代日志里。"""
        html = INDEX.read_text(encoding="utf-8")
        js = (FRONT / "js" / "api.js").read_text(encoding="utf-8")
        for name, src in (("index.html", html), ("api.js", js)):
            self.assertNotIn("?k=", src, f"{name} 里还有 ?k= 传令牌的写法")


if __name__ == "__main__":
    unittest.main()
