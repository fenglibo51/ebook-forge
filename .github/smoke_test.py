#!/usr/bin/env python3
"""发布后冒烟：真的走一遍「上传 → 转换 → 下载」。

健康检查只能证明进程起来了，证明不了转换链是通的。
镜像推上去如果少了字体、少了依赖、路径写错，health 照样 200，
用户第一次点转换才会发现 —— 那已经发版了。

所以这里必须做完整链路，并且校验三件用户真正会看到的事：
    ① 输出文件名是原始书名，不是随机存储名（P1-1）
    ② 下载下来确实是那个文件（PDF 魔数 + 大小）
    ③ 中文正文真的进了产物 —— 走一趟 txt → epub → txt，看关键词还在不在

第 ③ 条为什么绕这一圈：PDF 只做字节校验的话，字体缺失导致中文全渲染成
方块也一样能过。epub 是可逆的文字容器，把结果再转回 txt 一比，正文有没有
丢、编码有没有错就藏不住了。

只用标准库，本地和生产同一个脚本：
    python3 .github/smoke_test.py --base http://127.0.0.1:8600
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TIMEOUT = 30


def _req(url: str, data: bytes | None = None, headers: dict | None = None,
         method: str | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read(), dict(r.headers)


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----smoke" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; '
        f'filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8506")
    ap.add_argument("--keep", action="store_true", help="失败时保留上传文件方便排查")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # ① 健康
    for _ in range(30):
        try:
            st, body, _ = _req(base + "/api/health")
            if st == 200:
                ok(f"健康检查 {body.decode()[:80]}")
                break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    else:
        fail("健康检查一直没过")

    # ② 上传（用中文 + 空格 + 多点，覆盖 P1-1 的坑）
    # 名字带随机后缀：同一个数据目录连跑两次时，输出的 .pdf 已存在会走
    # 「重名避让」变成 xxx_1.pdf —— 那是对的行为，但硬断言文件名会把它判失败。
    # CI 每次是新容器看不出来，在生产上一跑就露。
    uniq = uuid.uuid4().hex[:6]
    display = f"冒烟 测试 {uniq}.txt"
    payload = "冒烟测试内容 SMOKE-MARKER-42\n".encode("utf-8")
    body, ctype = _multipart("files", display, payload)
    st, raw, _ = _req(base + "/api/upload", body, {"Content-Type": ctype}, "POST")
    if st != 200:
        fail(f"上传失败 {st} {raw[:300]}")
    up = json.loads(raw)
    fid = up["files"][0]["file_id"]
    if up["files"][0]["name"] != display:
        fail(f"上传回显名不对：{up['files'][0]['name']!r} != {display!r}")
    ok(f"上传成功 file_id={fid}")

    # ③ 建任务
    job_in = json.dumps({
        "files": [{"file_id": fid, "name": display, "size": len(payload)}],
        "target": "pdf",
    }).encode()
    st, raw, _ = _req(base + "/api/jobs", job_in,
                      {"Content-Type": "application/json"}, "POST")
    if st != 200:
        fail(f"建任务失败 {st} {raw[:300]}")
    jid = json.loads(raw)["job_id"]
    ok(f"任务 {jid} 已提交")

    # ④ 等完成
    state = {}
    for _ in range(120):
        st, raw, _ = _req(f"{base}/api/jobs/{jid}")
        state = json.loads(raw)
        if state.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(1)
    if state.get("status") != "done":
        fail(f"转换没成功：{json.dumps(state, ensure_ascii=False)[:500]}")
    item = state["items"][0]
    out_path = item["out"]
    ok(f"转换完成 {item['out_size']} 字节 {item['elapsed']}s")

    # ⑤ 输出名必须是原始书名，不能是随机 file_id
    import os
    out_name = os.path.basename(out_path)
    expect = f"冒烟 测试 {uniq}.pdf"
    if out_name != expect:
        fail(f"输出文件名不对：{out_name!r}，应为 {expect!r}"
             "（如果只是多了 _1 后缀，说明是重名避让而不是命名 bug）")
    ok(f"输出名正确：{out_name}")

    # ⑥ 下载并校验内容
    st, data, headers = _req(base + "/api/download?path=" + urllib.parse.quote(out_path))
    if st != 200:
        fail(f"下载失败 {st}")
    if not data.startswith(b"%PDF"):
        fail(f"下载到的不是 PDF，前 16 字节：{data[:16]!r}")
    if len(data) != item["out_size"]:
        fail(f"下载大小 {len(data)} 与任务报告 {item['out_size']} 不一致")
    ok(f"下载校验通过 {len(data)} 字节，PDF 魔数正确")

    disp = headers.get("Content-Disposition", "")
    if "filename" in disp and "%" not in disp and "冒烟" not in disp:
        print(f"  ⚠️ Content-Disposition: {disp}")

    # ⑦ 中文正文往返：txt → epub → txt，关键词必须还在
    #    （PDF 那条只能证明「文件是 PDF」，证明不了字真写进去了）
    marker = "SMOKE-MARKER-42"
    body, ctype = _multipart("files", display, payload)
    st, raw, _ = _req(base + "/api/upload", body, {"Content-Type": ctype}, "POST")
    if st != 200:
        fail(f"往返测试上传失败 {st}")
    fid2 = json.loads(raw)["files"][0]["file_id"]

    st, raw, _ = _req(base + "/api/jobs",
                      json.dumps({"files": [{"file_id": fid2, "name": display,
                                             "size": len(payload)}],
                                  "target": "epub"}).encode(),
                      {"Content-Type": "application/json"}, "POST")
    if st != 200:
        fail(f"转 epub 失败 {st} {raw[:200]}")
    jid2 = json.loads(raw)["job_id"]
    st2 = {}
    for _ in range(120):
        st, raw, _ = _req(f"{base}/api/jobs/{jid2}")
        st2 = json.loads(raw)
        if st2.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(1)
    if st2.get("status") != "done":
        fail(f"转 epub 没成功：{json.dumps(st2, ensure_ascii=False)[:400]}")
    epub_path = st2["items"][0]["out"]
    st, epub_bytes, _ = _req(base + "/api/download?path=" + urllib.parse.quote(epub_path))
    if st != 200 or not epub_bytes.startswith(b"PK"):
        fail(f"下载 epub 异常 {st}，前 4 字节 {epub_bytes[:4]!r}")
    ok(f"txt → epub 成功（{len(epub_bytes)} 字节）")

    # epub 再传回去转 txt，看正文是否原样还在
    body, ctype = _multipart("files", "往返.epub", epub_bytes)
    st, raw, _ = _req(base + "/api/upload", body, {"Content-Type": ctype}, "POST")
    if st != 200:
        fail(f"epub 回传失败 {st}")
    fid3 = json.loads(raw)["files"][0]["file_id"]
    st, raw, _ = _req(base + "/api/jobs",
                      json.dumps({"files": [{"file_id": fid3, "name": "往返.epub",
                                             "size": len(epub_bytes)}],
                                  "target": "txt"}).encode(),
                      {"Content-Type": "application/json"}, "POST")
    if st != 200:
        fail(f"epub → txt 失败 {st} {raw[:200]}")
    jid3 = json.loads(raw)["job_id"]
    st3 = {}
    for _ in range(120):
        st, raw, _ = _req(f"{base}/api/jobs/{jid3}")
        st3 = json.loads(raw)
        if st3.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(1)
    if st3.get("status") != "done":
        fail(f"epub → txt 没成功：{json.dumps(st3, ensure_ascii=False)[:400]}")
    txt_path = st3["items"][0]["out"]
    st, txt_bytes, _ = _req(base + "/api/download?path=" + urllib.parse.quote(txt_path))
    if st != 200:
        fail(f"下载往返 txt 失败 {st}")
    text = txt_bytes.decode("utf-8", "replace")
    if marker not in text:
        fail(f"往返后正文丢了标记词 {marker}，实际拿到：{text[:200]!r}")
    if "冒烟测试内容" not in text:
        fail(f"往返后中文正文损坏：{text[:200]!r}")
    ok(f"中文正文往返完好（关键词 {marker} 命中）")

    if not args.keep:
        pass  # 服务端自己有 TTL 清理，脚本不额外删
    print("\n冒烟全部通过 ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}: {e.read()[:500]}")
        sys.exit(1)
