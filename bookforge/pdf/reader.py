"""PDF 对象解析（纯标准库）。

目标不是完整实现 PDF，只做**取出页面里的图片**这一件事需要的部分：

    trailer → /Root → /Pages → /Kids 递归       拿到每页
    每页 /Resources → /XObject → /Subtype /Image  拿到图片对象
    ObjStm（PDF 1.5+ 把对象压缩进流里）          必须支持，否则现代 PDF 一张图都找不到

不支持：加密、线性化的特殊寻址（照常扫对象，能读）、跨引用流异常。
遇到加密直接报错，不猜。
"""

from __future__ import annotations

from ..formats import limits
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

_OBJ_HEAD = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class PDFReadError(ValueError):
    pass


# ── 极简 PDF 字典解析 ─────────────────────────────────────────────────────

def dict_get(raw: bytes, key: str) -> bytes | None:
    """从 dict / stream 头部取 `/Key value` 的原始值片段。

    三种形态都要认，截断规则各不相同：
        /Key << ... >>       平衡括号
        /Key [ ... ]         平衡方括号
        /Key /Name           到空白为止
        /Key 12 或 12 0 R    到下一个 /Key、>>、stream、endobj 为止
    """
    m = re.search(rb"/" + key.encode() + rb"(?![A-Za-z0-9])", raw)
    if not m:
        return None
    rest = raw[m.end():].lstrip()
    if not rest:
        return b""

    if rest.startswith(b"<<"):
        depth, j = 0, 0
        while j < len(rest) - 1:
            if rest[j:j + 2] == b"<<":
                depth += 1
                j += 2
            elif rest[j:j + 2] == b">>":
                depth -= 1
                j += 2
                if depth == 0:
                    return rest[:j]
            else:
                j += 1
        return rest

    if rest.startswith(b"["):
        depth, j = 0, 0
        while j < len(rest):
            c = rest[j:j + 1]
            if c == b"[":
                depth += 1
            elif c == b"]":
                depth -= 1
                if depth == 0:
                    return rest[:j + 1]
            j += 1
        return rest

    if rest.startswith(b"/"):
        m2 = re.match(rb"/[A-Za-z0-9_.+-]*", rest)
        return m2.group(0) if m2 else rest[:64]

    end = len(rest)
    # ⚠️ PDF 里 /Key 之间**不要求空白**：/Height 6917/ImageMask true 是合法的。
    # 早先用 rb"\s/[A-Za-z]" 当终止符，遇到这种紧凑写法会把后面的键一起吞掉，
    # 结果 /Height 解析成 b"6917/ImageMask true/Intent/Perceptual/..."。
    for pat in (rb"/[A-Za-z]", rb">>", rb"\bstream\b", rb"\bendobj\b"):
        mm = re.search(pat, rest)
        if mm and mm.start() < end:
            end = mm.start()
    return rest[:end].strip()


def dict_int(raw: bytes, key: str, default: int = 0) -> int:
    v = dict_get(raw, key)
    if not v:
        return default
    m = re.search(rb"-?\d+", v)
    return int(m.group()) if m else default


def dict_name(raw: bytes, key: str) -> str:
    v = dict_get(raw, key)
    if not v:
        return ""
    m = re.match(rb"/([A-Za-z0-9_.+-]+)", v.strip())
    return m.group(1).decode() if m else ""


def dict_ref(raw: bytes, key: str) -> int | None:
    """取 `N 0 R` 里的对象号"""
    v = dict_get(raw, key)
    if not v:
        return None
    m = re.match(rb"\s*(\d+)\s+\d+\s+R", v)
    return int(m.group(1)) if m else None


def dict_array_refs(raw: bytes, key: str) -> list[int]:
    v = dict_get(raw, key)
    if not v:
        return []
    return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", v)]


def dict_names(raw: bytes, key: str) -> list[str]:
    v = dict_get(raw, key)
    if not v:
        return []
    return [x.decode() for x in re.findall(rb"/([A-Za-z0-9_.+-]+)", v)]


# ── 文档 ──────────────────────────────────────────────────────────────────

@dataclass
class ImageObj:
    obj_num: int
    width: int
    height: int
    bpc: int
    colorspace: str
    filters: list[str]
    stream: bytes                      # 原始流字节（未解码）
    page_index: int = 0
    page_width: float = 0.0
    page_height: float = 0.0
    is_mask: bool = False              # /ImageMask true 或被他处 /Mask 引用
    is_stencil: bool = False           # 蒙版语义：抽出来不能直接当页面
    decode_parms: dict = field(default_factory=dict)
    name: str = ""                     # XObject 名，如 Im0

    @property
    def min_side(self) -> int:
        return min(self.width, self.height)


class PDFDocument:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        # /Encrypt 可能在 trailer（文件末尾），只查开头会漏
        if b"/Encrypt" in self.data[:200000] or b"/Encrypt" in self.data[-200000:]:
            raise PDFReadError("PDF 已加密，暂不支持")
        self.objs: dict[int, bytes] = {}        # 对象号 → 头部字节（含 stream 前的 dict）
        self.streams: dict[int, bytes] = {}     # 对象号 → 原始 stream 字节
        self._scan_objects()
        self._expand_objstm()
        self.mask_objs: set[int] = set()
        self._collect_masks()

    # ── 对象扫描 ────────────────────────────────────────────────────────
    def _scan_objects(self) -> None:
        data = self.data
        pos = 0
        n = len(data)
        while True:
            m = _OBJ_HEAD.search(data, pos)
            if not m:
                break
            num = int(m.group(1))
            body_start = m.end()
            eo = data.find(b"endobj", body_start)
            if eo < 0:
                eo = n

            sp = data.find(b"stream", body_start, eo)
            if sp < 0:
                self.objs[num] = data[body_start:eo]
                pos = eo + 6
                continue

            head = data[body_start:sp]
            p = sp + 6
            if data[p:p + 2] == b"\r\n":
                p += 2
            elif data[p:p + 1] in (b"\n", b"\r"):
                p += 1
            stream_start = p
            length = dict_int(head, "Length", -1)
            if 0 <= length and stream_start + length <= n:
                stream_end = stream_start + length
            else:
                es = data.find(b"endstream", stream_start)
                stream_end = es if es > 0 else eo

            self.objs[num] = head
            self.streams[num] = data[stream_start:stream_end]
            nxt = data.find(b"endobj", stream_end)
            pos = (nxt + 6) if nxt > 0 else stream_end

    def _expand_objstm(self) -> None:
        """PDF 1.5+ 的对象流：/Type /ObjStm，内部对象不在文件里直接出现"""
        objstm_total = 0
        for num in list(self.objs):
            head = self.objs[num]
            if dict_name(head, "Type") != "ObjStm":
                continue
            raw = self.streams.get(num, b"")
            if not raw:
                continue
            try:
                # 有上限解压：Object Stream 是 PDF 初始化的必经路径，
                # 这里的压缩炸弹能在打开文件那一刻就打满内存（P1-01）。
                # 超限的 LimitError 往上抛让整个 PDF 失败；
                # 损坏的流还是照原样跳过。
                data = limits.inflate(raw, limits.MAX_PDF_STREAM_BYTES,
                                      what="PDF 对象流 ")
            except limits.LimitError:
                raise
            except zlib.error:
                continue
            # 累计预算：单流都合规，加起来也可能爆。
            # 解析出的对象字节会一直留在 self.objs 里，不是用完就释放的临时量。
            objstm_total += len(data)
            if objstm_total > limits.MAX_PDF_OBJSTM_TOTAL_BYTES:
                raise limits.LimitError(
                    f"这本 PDF 的对象流累计解出 "
                    f"{objstm_total / (1 << 20):.0f} MB，超过上限 "
                    f"{limits.MAX_PDF_OBJSTM_TOTAL_BYTES / (1 << 20):.0f} MB"
                    "（BOOKFORGE_MAX_PDF_OBJSTM_TOTAL_MB 可调）")
            count = dict_int(head, "N", 0)
            first = dict_int(head, "First", 0)
            header = data[:first].split()
            pairs = []
            for i in range(0, min(len(header), count * 2) - 1, 2):
                try:
                    pairs.append((int(header[i]), int(header[i + 1])))
                except ValueError:
                    break
            for idx, (onum, off) in enumerate(pairs):
                end = pairs[idx + 1][1] if idx + 1 < len(pairs) else len(data) - first
                if onum not in self.objs:      # 文件里直接出现的优先
                    self.objs[onum] = data[first + off:first + end]

    def _collect_masks(self) -> None:
        for num, head in self.objs.items():
            for key in ("Mask", "SMask"):
                ref = dict_ref(head, key)
                if ref:
                    self.mask_objs.add(ref)
            # /Mask [ref ref ...]（颜色键蒙版不允许被当页图）
            v = dict_get(head, "Mask")
            if v and v.startswith(b"["):
                self.mask_objs.update(int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", v))

    # ── 页面树 ──────────────────────────────────────────────────────────
    def _trailer_root(self) -> int | None:
        for m in re.finditer(rb"trailer\s*<<", self.data):
            seg = self.data[m.start():m.start() + 2000]
            ref = dict_ref(seg, "Root")
            if ref and ref in self.objs:
                return ref
        # 交叉引用流 PDF 没有 trailer：找 /Type /Catalog
        for num, head in self.objs.items():
            if dict_name(head, "Type") == "Catalog":
                return num
        return None

    def page_objects(self) -> list[tuple[int, bytes]]:
        """按阅读顺序返回 [(对象号, 页面 dict)]"""
        root = self._trailer_root()
        pages: list[tuple[int, bytes]] = []

        def walk(num: int, depth: int = 0) -> None:
            if depth > 32 or num not in self.objs:
                return
            head = self.objs[num]
            typ = dict_name(head, "Type")
            if typ == "Page":
                pages.append((num, head))
                return
            for kid in dict_array_refs(head, "Kids"):
                walk(kid, depth + 1)

        if root is not None:
            pages_ref = dict_ref(self.objs[root], "Pages")
            if pages_ref:
                walk(pages_ref)

        if not pages:
            # 兜底：按对象号顺序收集所有 /Type /Page
            for num in sorted(self.objs):
                if dict_name(self.objs[num], "Type") == "Page":
                    pages.append((num, self.objs[num]))
        return pages

    # ── 图片 ────────────────────────────────────────────────────────────
    def page_images(self, page_index: int, page_head: bytes) -> list[ImageObj]:
        res_num = dict_ref(page_head, "Resources")
        res = self.objs.get(res_num, b"") if res_num else (dict_get(page_head, "Resources") or b"")
        return self._images_in_resources(res, page_index, depth=0, parent_res=b"")

    def _images_in_resources(self, res: bytes, page_index: int,
                             depth: int, parent_res: bytes = b"") -> list[ImageObj]:
        """Resources → XObject。

        图可能藏在 Form XObject 里（InDesign / 脚本生成的 PDF 常见），
        所以要递归进去 —— 只看页面自己的 XObject 会漏掉整页图。

        Form 可能**没有自己的 /Resources**，这时按规范继承调用者的资源字典。
        漏了这条回退，整本书的图就一张都找不到（实测踩到）。
        """
        xo_num = dict_ref(res, "XObject")
        if xo_num:
            xo = self.objs.get(xo_num, b"")
        else:
            xo = dict_get(res, "XObject") or b""
        if not xo:
            return []

        out: list[ImageObj] = []
        for m in re.finditer(rb"/([A-Za-z0-9_]+)\s+(\d+)\s+\d+\s+R", xo):
            name, onum = m.group(1).decode(), int(m.group(2))
            head = self.objs.get(onum)
            if head is None:
                continue
            subtype = dict_name(head, "Subtype")

            if subtype == "Form" and depth < 8:
                sub_num = dict_ref(head, "Resources")
                sub = (self.objs.get(sub_num, b"") if sub_num
                       else (dict_get(head, "Resources") or b""))
                if not sub:
                    sub = parent_res          # 没有就继承调用者的资源
                if sub:
                    out.extend(self._images_in_resources(sub, page_index,
                                                         depth + 1, res))
                continue

            if subtype != "Image":
                continue

            img = ImageObj(
                obj_num=onum,
                width=dict_int(head, "Width", 0),
                height=dict_int(head, "Height", 0),
                bpc=dict_int(head, "BitsPerComponent", 8),
                colorspace=dict_name(head, "ColorSpace"),
                filters=dict_names(head, "Filter"),
                stream=self.streams.get(onum, b""),
                page_index=page_index,
                name=name,
                decode_parms=self._decode_parms(head),
            )
            v = dict_get(head, "ImageMask")
            img.is_mask = bool(v and v.strip() == b"true") or onum in self.mask_objs
            img.is_stencil = img.is_mask
            out.append(img)
        return out

    def _decode_parms(self, head: bytes) -> dict:
        v = dict_get(head, "DecodeParms")
        if not v:
            return {}
        parms: dict = {}
        for key in ("K", "Columns", "Rows", "BlackIs1", "Predictor", "Colors",
                    "BitsPerComponent", "EndOfLine", "EndOfBlock", "EncodedByteAlign"):
            sub = dict_get(v, key)
            if sub is None:
                continue
            sub = sub.strip()
            if sub in (b"true", b"false"):
                parms[key] = sub == b"true"
            else:
                m = re.match(rb"-?\d+", sub)
                if m:
                    parms[key] = int(m.group())
        return parms

    def all_images(self) -> list[ImageObj]:
        out: list[ImageObj] = []
        for idx, (_num, head) in enumerate(self.page_objects()):
            pb = dict_get(head, "MediaBox")
            pw = ph = 0.0
            if pb:
                nums = [float(x) for x in re.findall(rb"-?\d+\.?\d*", pb)]
                if len(nums) >= 4:
                    pw, ph = nums[2] - nums[0], nums[3] - nums[1]
            for img in self.page_images(idx, head):
                img.page_width, img.page_height = pw, ph
                out.append(img)
        return out

    @property
    def page_count(self) -> int:
        return len(self.page_objects())
