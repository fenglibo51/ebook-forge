"""分享模式隔离实测：拿真实 HTTP 接口验「访客只能碰自己的东西」。

不 mock、不走 TestClient，直接打站点 —— 生产上跑一次才算数。
用法：python3 tools/share_isolation_check.py --base http://127.0.0.1:8505

检查项（全部来自清单 §8 第 6 条）：
  ① 访客能上传、能转换、能下载自己的结果
  ② 访客 A 拿不到访客 B 的 file_id
  ③ 访客拿不到他人任务
  ④ 访客碰不到 NAS 书库（/api/library、/api/scan）
  ⑤ 访客碰不到后台设置（/api/settings、/api/history）
  ⑥ 拿不到 /data/uploads 里的服务器路径
  ⑦ 分享关闭后所有入口立刻失效
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


def _req(base: str, path: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict[str, str] | None = None, timeout: float = 15.0):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except urllib.error.URLError as e:
        return 0, {}, str(e).encode()


def upload(base: str, token: str, sid: str, name: str, content: bytes):
    boundary = "----b" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body += (f'Content-Disposition: form-data; name="files"; '
             f'filename="{name}"\r\n').encode("utf-8")
    body += f"Content-Type: {ctype}\r\n\r\n".encode()
    body += content
    body += f"\r\n--{boundary}--\r\n".encode()
    q = urllib.parse.urlencode({"t": token, "sid": sid})
    return _req(base, f"/api/s/upload?{q}", method="POST", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8505")
    ap.add_argument("--admin-token", default="")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    fails: list[str] = []
    oks: list[str] = []

    def check(ok: bool, msg: str) -> None:
        (oks if ok else fails).append(msg)
        print(("  ✓ " if ok else "  ✗ ") + msg)

    # 0) 健康
    st, _h, body = _req(base, "/api/health")
    check(st == 200, f"服务活着（{st}）")

    # 管理端：确认分享开关状态
    admin = args.admin_token
    hdr = {"X-Admin-Token": admin} if admin else {}
    st, _h, body = _req(base, "/api/share/status", headers=hdr)
    if st != 200:
        print(f"  ! /api/share/status 返回 {st}，需要 --admin-token（{body[:80]}）")
        return 2
    enabled = json.loads(body).get("enabled")
    print(f"  当前分享开关：{enabled}")

    if not enabled:
        st, _h, body = _req(base, "/api/share/toggle", method="POST",
                            data=json.dumps({"enabled": True,
                                             "admin_token": admin}).encode(),
                            headers={**hdr, "Content-Type": "application/json"})
        if st != 200:
            print(f"  ! 打不开分享模式：{st} {body[:120]}")
            return 2
        # 打开分享的同时服务端会生成/沿用管理令牌 —— 后续管理接口都要带它
        admin = json.loads(body).get("admin_token", admin)
        hdr = {"X-Admin-Token": admin}
        print(f"  已临时打开分享模式（管理令牌 {len(admin)} 字符）")

    # 建一条链接
    st, _h, body = _req(base, "/api/shares", method="POST",
                        data=json.dumps({"ttl_hours": 1, "note": "隔离实测"}).encode(),
                        headers={**hdr, "Content-Type": "application/json"})
    check(st == 200, f"建分享链接（{st}）")
    if st != 200:
        print(body[:200])
        return 2
    token = json.loads(body)["token"]
    print(f"  token = {token}")

    sid_a, sid_b = "ck-a-" + uuid.uuid4().hex[:6], "ck-b-" + uuid.uuid4().hex[:6]

    # ① 访客 A 上传 + 转换 + 下载
    st, _h, body = upload(base, token, sid_a, "隔离 A.txt",
                          "第一章\n\n访客 A 的正文。\n".encode("utf-8"))
    check(st == 200, f"访客 A 能上传（{st}）")
    fa = json.loads(body)["files"][0]

    st, _h, body = _req(base, f"/api/s/jobs?{urllib.parse.urlencode({'t': token, 'sid': sid_a})}",
                        method="POST",
                        data=json.dumps({"files": [fa], "target": "pdf",
                                         "options": {}}).encode(),
                        headers={"Content-Type": "application/json"})
    check(st == 200, f"访客 A 能提交转换（{st}）")
    jid_a = json.loads(body)["job_id"]

    state = {}
    for _ in range(160):
        st, _h, body = _req(base, f"/api/s/jobs/{jid_a}?" +
                            urllib.parse.urlencode({"t": token, "sid": sid_a}))
        state = json.loads(body)
        if state.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(0.25)
    check(state.get("status") == "done", f"访客 A 转换完成（{state.get('status')}）")
    check("out" not in (state.get("items") or [{}])[0],
          "任务响应里没有服务器路径（out 字段被剥离）")

    st, hdrs, content = _req(base, f"/api/s/download/{jid_a}/0?" +
                             urllib.parse.urlencode({"t": token, "sid": sid_a}))
    check(st == 200 and content.startswith(b"%PDF"), f"访客 A 能下载自己的结果（{st}）")
    disp = hdrs.get("Content-Disposition", "") or hdrs.get("content-disposition", "")
    check("隔离 A.pdf" in urllib.parse.unquote(disp),
          f"下载名是原始书名（{urllib.parse.unquote(disp)[:60]}）")

    # ② 访客 B 用 A 的 file_id
    st, _h, body = _req(base, f"/api/s/jobs?{urllib.parse.urlencode({'t': token, 'sid': sid_b})}",
                        method="POST",
                        data=json.dumps({"files": [fa], "target": "pdf",
                                         "options": {}}).encode(),
                        headers={"Content-Type": "application/json"})
    check(st in (403, 404, 422), f"访客 B 不能用 A 的 file_id（{st}）")

    # ③ 访客 B 看 A 的任务
    st, _h, _b = _req(base, f"/api/s/jobs/{jid_a}?" +
                      urllib.parse.urlencode({"t": token, "sid": sid_b}))
    check(st == 404, f"访客 B 看不到 A 的任务（{st}）")

    st, _h, _b = _req(base, f"/api/s/download/{jid_a}/0?" +
                      urllib.parse.urlencode({"t": token, "sid": sid_b}))
    check(st == 404, f"访客 B 下不了 A 的文件（{st}）")

    # ④ 书库
    q = urllib.parse.urlencode({"t": token, "sid": sid_a})
    for path in ("/api/library", "/api/scan", "/api/history", "/api/settings", "/api/cache"):
        st, _h, _b = _req(base, f"{path}?{q}")
        check(st in (401, 403, 404, 405), f"访客碰不到 {path}（{st}）")

    # ⑤ 拿服务器路径当 file_id
    st, _h, _b = _req(base, f"/api/s/jobs?{urllib.parse.urlencode({'t': token, 'sid': sid_a})}",
                      method="POST",
                      data=json.dumps({"files": ["/data/uploads/whatever.txt"],
                                       "target": "pdf", "options": {}}).encode(),
                      headers={"Content-Type": "application/json"})
    check(st in (403, 404, 422), f"不接受原始服务器路径（{st}）")

    # ⑥ 关掉分享，一切失效
    st, _h, _b = _req(base, "/api/share/toggle", method="POST",
                      data=json.dumps({"enabled": False, "admin_token": admin}).encode(),
                      headers={**hdr, "Content-Type": "application/json"})
    check(st == 200, f"关闭分享模式（{st}）")
    st, _h, _b = upload(base, token, sid_a, "关后.txt", b"x")
    check(st == 403, f"关闭后上传被拒（{st}）")
    st, _h, _b = _req(base, f"/api/s/jobs/{jid_a}?" +
                      urllib.parse.urlencode({"t": token, "sid": sid_a}))
    check(st == 403, f"关闭后任务查询被拒（{st}）")

    print(f"\n通过 {len(oks)} 项，失败 {len(fails)} 项")
    if fails:
        print("\n失败项：")
        for f in fails:
            print("  · " + f)
        return 1
    print("分享隔离实测全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
