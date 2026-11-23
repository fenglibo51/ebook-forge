"""分享模式隔离实测：拿真实 HTTP 接口验「访客只能碰自己的东西」。

不 mock、不走 TestClient，直接打站点 —— 生产上跑一次才算数。
用法：python3 tools/share_isolation_check.py --base http://127.0.0.1:8505

⚠️ 这个脚本会**临时打开分享模式**（如果本来是关的），跑完自己关回去。
   如果进来时分享本来就是开着的，默认**不会再关掉它** —— 那会撤销别人
   正在用的链接、连带清掉上传副本。要验证「关掉后全部失效」得显式加
   --allow-toggle-off，并且只在测试实例上用。

检查项：
  ① 访客能上传、能转换、能下载自己的结果（且下载名是原始书名）
  ② 访客 B 拿不到访客 A 的 file_id
  ③ 访客拿不到他人任务 / 他人结果
  ④ 访客碰不到 NAS 书库（/api/library、/api/scan）
  ⑤ 访客碰不到后台设置（/api/settings、/api/history、/api/cache）
  ⑥ 拿不到 /data/uploads 里的服务器路径
  ⑦ 分享关闭后所有入口立刻失效（需 --allow-toggle-off）
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
    ap.add_argument("--allow-toggle-off", action="store_true",
                    help="允许验证「关闭分享后入口失效」。"
                         "会把本来开着的分享关掉再恢复；只在测试实例上用")
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

    # ── 现场记录：跑完要恢复成什么样 ──────────────────────────────────
    # was_enabled=True  → 进来时分享是开着的，可能有人正在用
    # opened_by_me=True → 这个开关是脚本自己打开的，跑完必须关回去
    was_enabled = bool(json.loads(body).get("enabled"))
    opened_by_me = False
    created_token = ""
    closed_in_test = False
    print(f"  当前分享开关：{was_enabled}")

    try:
        if not was_enabled:
            st, _h, body = _req(base, "/api/share/toggle", method="POST",
                                data=json.dumps({"enabled": True,
                                                 "admin_token": admin}).encode(),
                                headers={**hdr, "Content-Type": "application/json"})
            if st != 200:
                print(f"  ! 打不开分享模式：{st} {body[:120]}")
                return 2
            opened_by_me = True
            # 打开分享的同时服务端会生成/沿用管理令牌 —— 后续管理接口都要带它
            admin = json.loads(body).get("admin_token", admin)
            hdr = {"X-Admin-Token": admin}
            print(f"  已临时打开分享模式（管理令牌 {len(admin)} 字符），跑完会关回去")

        # 建一条链接（记下来，跑完删掉）
        st, _h, body = _req(base, "/api/shares", method="POST",
                            data=json.dumps({"ttl_hours": 1, "note": "隔离实测"}).encode(),
                            headers={**hdr, "Content-Type": "application/json"})
        check(st == 200, f"建分享链接（{st}）")
        if st != 200:
            print(body[:200])
            return 2
        token = json.loads(body)["token"]
        created_token = token
        print(f"  token = {token}")

        sid_a, sid_b = "ck-a-" + uuid.uuid4().hex[:6], "ck-b-" + uuid.uuid4().hex[:6]

        # ① 访客 A 上传 + 转换 + 下载
        st, _h, body = upload(base, token, sid_a, "隔离 A.txt",
                              "第一章\n\n访客 A 的正文。\n".encode("utf-8"))
        check(st == 200, f"访客 A 能上传（{st}）")
        fa = json.loads(body)["files"][0]

        # files 必须是对象数组（JobFile），每项要 name —— 早先这里传的是
        # 纯 file_id 字符串数组，接口 422 直接打回来，脚本却在下一行 KeyError
        st, _h, body = _req(base, f"/api/s/jobs?{urllib.parse.urlencode({'t': token, 'sid': sid_a})}",
                            method="POST",
                            data=json.dumps({"files": [{"file_id": fa["file_id"],
                                                        "name": fa["name"],
                                                        "size": fa["size"]}],
                                             "target": "pdf", "options": {}}).encode(),
                            headers={"Content-Type": "application/json"})
        if st != 200:
            check(False, f"访客 A 能建任务（{st} {body[:160]!r}）")
            return 1
        check(True, f"访客 A 能建任务（{st}）")
        jid_a = json.loads(body)["job_id"]

        for _ in range(60):
            st, _h, body = _req(base, f"/api/s/jobs/{jid_a}?" +
                                urllib.parse.urlencode({"t": token, "sid": sid_a}))
            if st == 200 and json.loads(body).get("status") in ("done", "error"):
                break
            time.sleep(0.5)
        job = json.loads(body)
        check(job.get("status") == "done", f"访客 A 的任务完成（{job.get('status')}）")

        st, h, body = _req(base, f"/api/s/download/{jid_a}/0?" +
                           urllib.parse.urlencode({"t": token, "sid": sid_a}))
        check(st == 200 and body[:4] == b"%PDF", f"访客 A 能下载 PDF（{st}，{len(body)} 字节）")
        # uvicorn/h11 会把响应头名规范成小写，所以不能按 "Content-Disposition" 直取
        cd = next((v for k, v in h.items() if k.lower() == "content-disposition"), "")
        check("隔离 A.pdf" in urllib.parse.unquote(cd), f"下载名是原始书名（{cd[:60]}）")

        # ② 访客 B 拿 A 的 file_id
        st, _h, body = _req(base, f"/api/s/jobs?{urllib.parse.urlencode({'t': token, 'sid': sid_b})}",
                            method="POST",
                            data=json.dumps({"files": [{"file_id": fa["file_id"],
                                                        "name": fa["name"],
                                                        "size": fa["size"]}],
                                             "target": "pdf", "options": {}}).encode(),
                            headers={"Content-Type": "application/json"})
        check(st in (403, 404, 422), f"访客 B 不能用 A 的 file_id（{st}）")

        # ③ 访客 B 看 A 的任务
        st, _h, _b = _req(base, f"/api/s/jobs/{jid_a}?" +
                          urllib.parse.urlencode({"t": token, "sid": sid_b}))
        check(st == 404, f"访客 B 看不到 A 的任务（{st}）")

        st, _h, _b = _req(base, f"/api/s/download/{jid_a}/0?" +
                          urllib.parse.urlencode({"t": token, "sid": sid_b}))
        check(st == 404, f"访客 B 下不了 A 的文件（{st}）")

        # ④ 书库 / 后台
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
        #    这一条动全局开关：本来开着的时候关它 = 撤销别人正在用的链接。
        #    所以默认只在「这个开关是脚本自己打开的」时才跑。
        if was_enabled and not args.allow_toggle_off:
            print("  ⏭ 跳过「关闭分享后失效」验证：进来时分享本来是开着的，"
                  "关掉会撤销正在使用的链接（真要测请加 --allow-toggle-off，"
                  "并且只在测试实例上用）")
        else:
            st, _h, _b = _req(base, "/api/share/toggle", method="POST",
                              data=json.dumps({"enabled": False, "admin_token": admin}).encode(),
                              headers={**hdr, "Content-Type": "application/json"})
            check(st == 200, f"关闭分享模式（{st}）")
            closed_in_test = True
            st, _h, _b = upload(base, token, sid_a, "关后.txt", b"x")
            check(st == 403, f"关闭后上传被拒（{st}）")
            st, _h, _b = _req(base, f"/api/s/jobs/{jid_a}?" +
                              urllib.parse.urlencode({"t": token, "sid": sid_a}))
            check(st == 403, f"关闭后任务查询被拒（{st}）")
    finally:
        # ── 恢复现场 ─────────────────────────────────────────────────
        # 不管中途正常结束还是抛异常，这两件事都要做：
        #   1. 删掉脚本自己建的那条链接（否则留一条 1 小时的垃圾链接和它的上传副本）
        #   2. 分享开关回到进来时的样子
        if created_token:
            st, _h, _b = _req(base, f"/api/shares/{created_token}", method="DELETE",
                              headers=hdr)
            print(f"  ↩ 撤销测试链接（{st}）")
        if closed_in_test and was_enabled:
            # 测试把别人开着的分享关了 → 重新打开
            st, _h, _b = _req(base, "/api/share/toggle", method="POST",
                              data=json.dumps({"enabled": True, "admin_token": admin}).encode(),
                              headers={**hdr, "Content-Type": "application/json"})
            print(f"  ↩ 恢复分享模式（{st}）")
        elif opened_by_me:
            st, _h, _b = _req(base, "/api/share/toggle", method="POST",
                              data=json.dumps({"enabled": False, "admin_token": admin}).encode(),
                              headers={**hdr, "Content-Type": "application/json"})
            print(f"  ↩ 关回分享模式（{st}）")

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
