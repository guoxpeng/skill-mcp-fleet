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
import re
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

VERSION = "1.1.0"
HOME_DIR = os.path.join(os.path.expanduser("~"), ".mcp-fleet")
STORE = os.path.join(HOME_DIR, "directory.json")

# 节点名白名单：避免超长名/奇怪字符把目录撑坏（目录是按 name 覆盖写的）
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_NODES = 200

# 注册限速：默认无 token 时注册接口对任何来源开放，不加限速可被刷爆内存/磁盘
RATE = {}            # {ip: [次数, 窗口起点]}
RATE_LIMIT = 30      # 每窗口最多注册次数
RATE_WINDOW = 60     # 窗口秒数

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
        try:
            os.chmod(STORE, 0o600)      # 节点清单属于内部拓扑，不收权限等于对外公开
        except Exception:
            pass
    except Exception as e:
        print("[!] 写 %s 失败：%r" % (STORE, e))


def rate_ok(ip):
    """按来源 IP 限速，防止注册接口被刷（默认无 token 时它是对任何来源开放的）。"""
    now = time.time()
    with _lock:
        cnt, start = RATE.get(ip, [0, now])
        if now - start > RATE_WINDOW:
            cnt, start = 0, now
        cnt += 1
        RATE[ip] = [cnt, start]
        # 字典只增不减会成为内存慢泄漏，顺手清理过期条目
        if len(RATE) > 512:
            for k in [k for k, v in list(RATE.items()) if now - v[1] > RATE_WINDOW * 5]:
                RATE.pop(k, None)
    return cnt <= RATE_LIMIT


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def _host_of(u):
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""


def register(payload):
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip().rstrip("/")
    if not name:
        return 400, {"ok": False, "error": "缺少 name"}
    # name 是覆盖写的唯一键，必须限字符与长度，否则可被超长/畸形名撑坏
    if not NAME_RE.match(name):
        return 400, {"ok": False, "error": "name 仅允许字母数字._- 且不超过 64 字符"}
    if not url.startswith(("http://", "https://")):
        return 400, {"ok": False, "error": "url 必须是 http(s):// 开头"}
    if len(url) > 512:
        return 400, {"ok": False, "error": "url 过长"}

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
        if old is None and len(_state["nodes"]) >= MAX_NODES:
            return 429, {"ok": False, "error": "节点数已达上限 %d" % MAX_NODES}
        changed = (not old) or old.get("url") != url
        old_host = _host_of(old.get("url") or "") if old else ""
        new_host = _host_of(url)
        # 域名整体换掉（不只是路径/端口）是个强信号：quick tunnel 重建属正常，
        # 但也正是「投毒」的形态。这里只做标记，是否采信由主端决定。
        domain_changed = bool(old_host) and old_host != new_host
        hits = (old or {}).get("hits", 0) + 1
        _state["nodes"][name] = {
            "url": url,
            "port": port,
            "ts": ts,
            "seen": int(time.time()),
            "hits": hits,
            "remote": payload.get("_remote", ""),
            "domain_changed": domain_changed,
        }
        # 只在地址变化或每 10 次上报时落盘：原实现每次注册都写盘，
        # 无 token 时可被刷爆磁盘 IO。
        if changed or hits % 10 == 1:
            save_store()
    print("[+] %s -> %s%s%s" % (name, url, "  (地址变更)" if changed else "",
                                "  [域名变更]" if domain_changed else ""))
    return 200, {"ok": True, "name": name, "url": url, "changed": changed,
                 "domain_changed": domain_changed}


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
        # 默认不发任何 CORS 头。原实现无条件回 Access-Control-Allow-Origin: *，
        # 等于允许任意网页跨域读取整个集群的节点清单与入口地址。
        # 目录服务是给主端/副端程序调的，浏览器本就不该直连。
        try:
            allow = list(ARGS.cors_origins or []) if ARGS is not None else []
        except Exception:
            allow = []
        origin = (self.headers.get("Origin") or "").strip()
        if allow and origin and origin in allow:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-Fleet-Token, Authorization")
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
        if not got:
            return False
        # 必须比 bytes：HTTP 头按 latin-1 解码，请求头里塞非 ASCII 字节会让
        # compare_digest(str, str) 抛 TypeError —— 一条可被外部触发的异常路径。
        return hmac.compare_digest(got.encode("utf-8", "surrogateescape"),
                                   ARGS.token.encode("utf-8", "surrogateescape"))

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
        ip = self.client_address[0] if self.client_address else "?"
        if not rate_ok(ip):
            return self._send(429, {"ok": False, "error": "注册过于频繁，请稍后重试"})
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
            # 探活留着（监控/负载均衡要用），但**不向未认证者** dump 全量
            # 节点清单与落盘路径 —— 原实现任何人 GET / 就能拿到整个集群的
            # 公网入口列表 + 服务端存储路径。
            if not self._authed():
                return self._send(200, {
                    "ok": True,
                    "service": "fleet-directory",
                    "version": VERSION,
                    "auth_required": True,
                })
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
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1；要给别的机器用请显式写 --host 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8790, help="监听端口（默认 8790）")
    ap.add_argument("--token", default=os.environ.get("FLEET_DIRECTORY_TOKEN", ""),
                    help="注册/查询都要带的 X-Fleet-Token（对非本机监听时为必需）")
    ap.add_argument("--cors-origins", default="",
                    help="可选：允许跨域的来源，逗号分隔。留空 = 完全不发 CORS 头（推荐）")
    ap.add_argument("--i-know-its-insecure", action="store_true",
                    help="明知风险：在无 token 且监听非本机的情况下强行启动")
    ap.add_argument("--ttl", type=int, default=600,
                    help="多少秒没上报就算「陈旧」（默认 600，仅用于标记，不删数据）")
    ARGS = ap.parse_args()
    ARGS.cors_origins = [s.strip() for s in (ARGS.cors_origins or "").split(",") if s.strip()]

    # ---- 启动前安全校验（v1.1）-------------------------------------------
    if not ARGS.token and ARGS.host not in ("127.0.0.1", "localhost", "::1"):
        if not ARGS.i_know_its_insecure:
            print("[FATAL] 监听 %s 但未设置 --token。" % ARGS.host, file=sys.stderr)
            print("        无 token 的注册接口对任何来源开放 = 任何人都能改写节点地址；",
                  file=sys.stderr)
            print("        主端随后会把副端令牌发向他们指定的地址（令牌泄漏 + 节点劫持）。",
                  file=sys.stderr)
            print("        请加 --token <强随机串>；确实要裸跑请再加 --i-know-its-insecure。",
                  file=sys.stderr)
            sys.exit(2)
        print("[warn] 无 token 且监听 %s —— 任何能访问该端口的人都能注册/改写节点地址。"
              % ARGS.host, file=sys.stderr)

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
