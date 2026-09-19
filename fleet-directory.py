#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fleet-directory —— MCP-Fleet 副端「地址目录」服务（零依赖）

解决的问题
----------
副端用 cloudflared 快速隧道时，地址是临时的：副端一重启/一重建隧道，
trycloudflare 就给一个新域名。主端 mcp.json 里写死的地址立刻失效，
表现就是「昨天还好好的，今天连不上了」。

地址目录让副端自己把「我现在在哪」上报上来，主端按名字查最新地址：

    副端 mcp-watchdog.sh  --POST /register-->  fleet-directory  <--GET /resolve--
                                                                      主端 fleet.py

用法
----
    # 主端（或任意一台 7x24 的机器，比如 NAS / 云主机）跑起来：
    python3 fleet-directory.py --port 8790

    # 想带鉴权（推荐，因为注册接口是公开的）：
    python3 fleet-directory.py --port 8790 --token <一串随机>
    # 副端侧再配 --directory-token <同一串>，安装时用：
    #   install.sh --directory http://<主端>:8790 --directory-token <同一串>

    # 主端查地址：
    curl http://<主端>:8790/resolve?name=nas
    curl http://<主端>:8790/nodes

    # 也可以后台常驻：
    setsid nohup python3 fleet-directory.py --port 8790 >> ~/.mcp-fleet/directory.log 2>&1 &

接口
----
    POST /register         {"name":"nas","url":"https://xxx.trycloudflare.com","port":3100,"ts":1710000000}
                           -> {"ok":true,"name":"nas","url":"...","changed":true}
    GET  /nodes            -> {"nodes":{...},"ttl":600}
    GET  /resolve?name=nas -> 纯文本，一行 URL（查不到返回 404）
    GET  /                 -> 状态页（JSON）
    DELETE /nodes?name=nas -> 删除一条（需 token）

设计取舍
--------
* 只用标准库（http.server + json + threading），和副端 mcp_agent.py 一个路子，
  任何有 python3 的机器都能跑，不需要 pip install。
* 落盘到 ~/.mcp-fleet/directory.json，重启不丢；原子写（先写 .tmp 再 replace）。
* 不做过期删除：地址旧了也留着，主端自己看 ts 判断新鲜度；
  隧道重建后副端会立刻再上报，所以目录里永远是「最近一次上报」。
* 注册接口默认允许任何来源（因为副端可能在任意 NAT 后面），
  但只要设了 --token，就必须带 X-Fleet-Token，否则 401。
"""

import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

VERSION = "1.0.0"
HOME_DIR = os.path.join(os.path.expanduser("~"), ".mcp-fleet")
STORE = os.path.join(HOME_DIR, "directory.json")

_lock = threading.Lock()
_state = {"nodes": {}}

ARGS = None


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def load_store():
    global _state
    if not os.path.isfile(STORE):
        return
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
            _state = data
    except Exception as e:
        print("[!] 读取 %s 失败（忽略，按空目录启动）：%r" % (STORE, e))


def save_store():
    """原子落盘：先写临时文件再 replace，避免半截 JSON。"""
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)
    except Exception as e:
        print("[!] 写 %s 失败：%r" % (STORE, e))


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def register(payload):
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip().rstrip("/")
    if not name:
        return 400, {"ok": False, "error": "缺少 name"}
    if not url.startswith(("http://", "https://")):
        return 400, {"ok": False, "error": "url 必须是 http(s):// 开头"}

    port = payload.get("port") or 0
    try:
        port = int(port)
    except Exception:
        port = 0

    ts = payload.get("ts")
    try:
        ts = int(ts)
    except Exception:
        ts = int(time.time())

    with _lock:
        old = _state["nodes"].get(name)
        changed = (not old) or old.get("url") != url
        _state["nodes"][name] = {
            "url": url,
            "port": port,
            "ts": ts,
            "seen": int(time.time()),
            "hits": (old or {}).get("hits", 0) + 1,
            "remote": payload.get("_remote", ""),
        }
        save_store()
    print("[+] %s -> %s%s" % (name, url, "  (地址变更)" if changed else ""))
    return 200, {"ok": True, "name": name, "url": url, "changed": changed}


def nodes_view():
    now = int(time.time())
    out = {}
    with _lock:
        items = list(_state["nodes"].items())
    for name, v in items:
        age = now - int(v.get("seen") or v.get("ts") or now)
        out[name] = dict(v, age=age, stale=age > ARGS.ttl)
    return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-directory/" + VERSION

    def log_message(self, fmt, *a):  # 默认日志太吵，收成一行
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % a))

    # ---- 工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", raw=False):
        if not raw:
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Fleet-Token, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _authed(self):
        """没设 token 就全放行；设了就必须带对。"""
        if not ARGS.token:
            return True
        got = self.headers.get("X-Fleet-Token", "")
        if not got:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
        return bool(got) and hmac.compare_digest(got, ARGS.token)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- 路由 ----
    def do_OPTIONS(self):
        self._send(204, b"", raw=True)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/register":
            return self._send(404, {"ok": False, "error": "未知路径，试试 POST /register"})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "鉴权失败：缺少或错误的 X-Fleet-Token"})
        payload = self._json_body()
        payload["_remote"] = self.client_address[0] if self.client_address else ""
        code, resp = register(payload)
        return self._send(code, resp)

    def do_DELETE(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path != "/nodes":
            return self._send(404, {"ok": False, "error": "未知路径"})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "鉴权失败"})
        name = (parse_qs(u.query).get("name") or [""])[0].strip()
        with _lock:
            existed = _state["nodes"].pop(name, None) is not None
            save_store()
        return self._send(200 if existed else 404, {"ok": existed, "name": name})

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)

        if path == "/resolve":
            if not self._authed():
                return self._send(401, {"ok": False, "error": "鉴权失败"})
            name = (q.get("name") or [""])[0].strip()
            with _lock:
                v = _state["nodes"].get(name)
            if not v:
                return self._send(404, {"ok": False, "error": "没有 %s 的记录" % name})
            return self._send(200, v["url"] + "\n", "text/plain; charset=utf-8", raw=True)

        if path == "/nodes":
            if not self._authed():
                return self._send(401, {"ok": False, "error": "鉴权失败"})
            return self._send(200, {"ok": True, "ttl": ARGS.ttl, "nodes": nodes_view()})

        if path == "/":
            return self._send(200, {
                "ok": True,
                "service": "fleet-directory",
                "version": VERSION,
                "auth": bool(ARGS.token),
                "ttl": ARGS.ttl,
                "store": STORE,
                "count": len(nodes_view()),
                "nodes": nodes_view(),
                "endpoints": ["POST /register", "GET /nodes", "GET /resolve?name=<n>",
                              "DELETE /nodes?name=<n>"],
            })

        return self._send(404, {"ok": False, "error": "未知路径"})


# --------------------------------------------------------------------------- #
def main():
    global ARGS
    ap = argparse.ArgumentParser(
        description="MCP-Fleet 地址目录：副端上报自己的公网地址，主端按名字查最新地址",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[-1] if "用法" in __doc__ else "")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8790, help="监听端口（默认 8790）")
    ap.add_argument("--token", default=os.environ.get("FLEET_DIRECTORY_TOKEN", ""),
                    help="可选：注册/查询都要带的 X-Fleet-Token")
    ap.add_argument("--ttl", type=int, default=600,
                    help="多少秒没上报就算「陈旧」（默认 600，仅用于标记，不删数据）")
    ARGS = ap.parse_args()

    load_store()

    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    srv.daemon_threads = True

    print("=" * 68)
    print(" MCP-Fleet 地址目录  v%s" % VERSION)
    print(" 监听      : http://%s:%d" % (ARGS.host, ARGS.port))
    print(" 鉴权      : %s" % ("已开启（X-Fleet-Token）" if ARGS.token else "未开启 —— 任何人可读写，公网请务必 --token"))
    print(" 存储      : %s" % STORE)
    print(" 已知副端  : %d 个" % len(nodes_view()))
    print("-" * 68)
    print(" 副端安装时加：--directory http://<本机可达IP>:%d%s"
          % (ARGS.port, " --directory-token <token>" if ARGS.token else ""))
    print(" 主端查地址  ：python3 fleet.py sync        （或 curl .../resolve?name=nas）")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 退出")
    finally:
        with _lock:
            save_store()


if __name__ == "__main__":
    main()
