#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleet.py v1.1 防投毒补丁自测。

核心要证明的一件事：
  地址目录被投毒后，主端**不会**再把副端的 Bearer 令牌发给攻击者指定的地址。
  —— 原实现只要对方回 {"status":"ok"} 就算探测通过，然后 token 就发出去了。

另外覆盖域名同级校验、登记表权限、shlex 拼接与 SSH 主机密钥选项。
"""
import importlib.util
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "fleet.py")
RESULTS = []


def rec(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("[PASS] " if cond else "[FAIL] ") + name + ("   " + str(extra) if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(cls):
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/mcp" % port


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self._reply(*self.ANSWER)


class FakePoison(_Base):
    """投毒者的典型回包：只要能让探测过，就回一句 ok。"""
    ANSWER = (200, {"status": "ok"})


class FakeAuth(_Base):
    """开了鉴权的真副端：无凭据给 401 + 高辨识度提示语。"""
    ANSWER = (401, {"error": "缺少凭据。请在请求头带 Authorization: Bearer <token>",
                    "hint": "auth_token 配置见 mcp_agent_config.json"})


class FakeReal(_Base):
    ANSWER = (200, {"jsonrpc": "2.0", "id": 1,
                    "result": {"serverInfo": {"name": "fake-node", "version": "3.1.0"}}})


def main():
    spec = importlib.util.spec_from_file_location("fleet_probe", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src_txt = open(SRC, encoding="utf-8").read()

    # ---- 域名工具 ----
    rec("1 reg_suffix 取末两段",
        mod.reg_suffix("abc.def.trycloudflare.com") == "trycloudflare.com",
        mod.reg_suffix("abc.def.trycloudflare.com"))
    rec("2 reg_suffix 对 IP 原样返回",
        mod.reg_suffix("192.168.1.10") == "192.168.1.10")
    rec("3 host_of 提取主机名",
        mod.host_of("https://x.trycloudflare.com/mcp") == "x.trycloudflare.com")

    # ---- 域名采信校验 ----
    rec("4 同 host → 通过",
        mod.domain_ok("https://a.example.com/mcp", "https://a.example.com/mcp") is True)
    rec("5 quick tunnel 换域名（同注册域）→ 通过",
        mod.domain_ok("https://old-x.trycloudflare.com/mcp",
                      "https://new-y.trycloudflare.com/mcp") is True)
    rec("6 跳到 evil.example.com → 拒绝",
        mod.domain_ok("https://old.trycloudflare.com/mcp",
                      "https://evil.example.com/mcp") is False)
    os.environ["FLEET_ALLOW_DOMAIN_CHANGE"] = "1"
    rec("7 显式放行开关生效",
        mod.domain_ok("https://old.trycloudflare.com/mcp",
                      "https://evil.example.com/mcp") is True)
    os.environ.pop("FLEET_ALLOW_DOMAIN_CHANGE", None)

    # ---- 无凭据探活（防投毒的核心）----
    s1, u1 = serve(FakePoison)
    s2, u2 = serve(FakeAuth)
    s3, u3 = serve(FakeReal)
    try:
        rec("8 假节点回 {\"status\":\"ok\"} → 识破（不再误判通过）",
            mod.probe_anonymous(u1, timeout=4) is False)
        rec("9 401+副端提示语 → 认作副端",
            mod.probe_anonymous(u2, timeout=4) is True)
        rec("10 真 initialize 响应 → 认作副端",
            mod.probe_anonymous(u3, timeout=4) is True)
        rec("11 连不通的地址 → False",
            mod.probe_anonymous("http://127.0.0.1:%d/mcp" % free_port(), timeout=2) is False)
    finally:
        for s in (s1, s2, s3):
            s.shutdown()

    # ---- 源码层确认 ----
    rec("12 save_registry 收紧 0600", "os.chmod(REGISTRY, 0o600)" in src_txt)
    rec("13 bootstrap 不再用 StrictHostKeyChecking=no",
        '"StrictHostKeyChecking=no"' not in src_txt
        and '"StrictHostKeyChecking=accept-new"' in src_txt)
    rec("14 bootstrap 用 shlex.quote 拼参数",
        "q = shlex.quote" in src_txt and "--auth-token '%s'" not in src_txt)
    rec("15 sync_from_directory 采信前过域名校验",
        "if not domain_ok(cur_url, new_mcp)" in src_txt)
    rec("16 cmd_sync 采信前也做校验（原来完全不探测）",
        "if not domain_ok(cur, new)" in src_txt)
    rec("17 sync_from_directory 先匿名探活再发令牌",
        src_txt.index("probe_anonymous(new_mcp") < src_txt.index("http_probe(new_mcp, timeout=min(timeout, 6), token=token)"))

    ok = sum(1 for _, c in RESULTS if c)
    print("\n==== %d/%d 通过 ====" % (ok, len(RESULTS)))
    for n, c in RESULTS:
        if not c:
            print("  FAILED: " + n)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
