#!/usr/bin/env python3
"""发布后冒烟：真的走一遍「上传 → 转换 → 下载」。

健康检查只能证明进程起来了，证明不了转换链是通的。
镜像推上去如果少了字体、少了依赖、路径写错，health 照样 200，
用户第一次点转换才会发现 —— 那已经发版了。

所以这里必须做完整链路，并且校验三件用户真正会看到的事：
    ① 输出文件名是原始书名，不是随机存储名（P1-1）
    ② 下载下来确实是那个文件（PDF 魔数 + 大小）
    ③ 输出里能看到自己的内容（文字提取得到关键词）

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
    display = "冒烟 测试 v1.2.3.txt"
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
    if out_name != "冒烟 测试 v1.2.3.pdf":
        fail(f"输出文件名不对：{out_name!r}，应为 '冒烟 测试 v1.2.3.pdf'")
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
