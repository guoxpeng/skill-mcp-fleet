#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleet-directory.py v1.1 加固补丁自测。

覆盖：
  1. 无 token + 监听非本机 → 拒绝启动(exit 2)
  2. 未认证 GET / 不 dump 节点清单与落盘路径
  3. 认证后 GET / 正常返回节点
  4. 未认证 POST /register → 401
  5. name 非法 → 400；合法注册 → 200 且 changed 正确
  6. 非 ASCII token 不再抛 TypeError
  7. 注册限速生效（超阈值 → 429）
  8. 响应头不再有 ACAO 通配
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"<home>\.workbuddy\binaries\python\versions\3.13.12\python.exe"
SRC = os.path.join(HERE, "fleet-directory.py")
BASE = os.path.join(HERE, ".selftest_dir_tmp")
TOK = "DIRTOKEN_XYZ789"
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


def start(port, extra=(), home=None):
    env = dict(os.environ)
    if home:
        env["HOME"] = home
        env["USERPROFILE"] = home
    # 注意必须写文件而不是 PIPE：服务端日志量大，PIPE 没人读会填满缓冲区
    # 把服务端阻塞在 print 上（表现为客户端莫名超时）。
    log = open(os.path.join(BASE, "dir_%d.log" % port), "w", encoding="utf-8")
    p = subprocess.Popen([PY, SRC, "--host", "127.0.0.1", "--port", str(port)] + list(extra),
                         stdout=log, stderr=subprocess.STDOUT, env=env)
    for _ in range(60):
        try:
            c = socket.create_connection(("127.0.0.1", port), 0.3)
            c.close()
            return p
        except OSError:
            time.sleep(0.15)
    p.kill()
    raise RuntimeError("目录服务未在 9s 内监听")


def req(port, method, path, body=None, headers=None, timeout=6):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = dict(headers or {})
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=raw, headers=h)
    r = c.getresponse()
    data = r.read().decode("utf-8", "replace")
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, data, hdrs


def main():
    if os.path.isdir(BASE):
        shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)
    home = os.path.join(BASE, "home")
    os.makedirs(home)

    # ---- 1. 无 token + 非本机监听 → 拒绝启动 ----
    r = subprocess.run([PY, SRC, "--host", "0.0.0.0", "--port", str(free_port())],
                       capture_output=True, timeout=20, env=dict(os.environ, HOME=home))
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    rec("1 无 token+非本机 → 拒绝启动(exit 2)", r.returncode == 2, r.returncode)
    rec("1b 提示含投毒风险说明", "FATAL" in out and "token" in out)

    # ---- 2~8. 带 token 启动 ----
    port = free_port()
    proc = start(port, ["--token", TOK], home=home)
    try:
        st, body, hdrs = req(port, "GET", "/")
        j = json.loads(body)
        rec("2 未认证 GET / 200", st == 200, st)
        rec("2b 未认证不 dump 节点与存储路径",
            "nodes" not in j and "store" not in j, j)
        rec("2c 响应头无 ACAO 通配", "Access-Control-Allow-Origin" not in hdrs,
            hdrs.get("Access-Control-Allow-Origin"))

        st, body, _ = req(port, "GET", "/", headers={"X-Fleet-Token": TOK})
        j = json.loads(body)
        rec("3 认证后 GET / 可见节点字段", st == 200 and "nodes" in j and "store" in j, st)

        st, body, _ = req(port, "POST", "/register", body={"name": "nas", "url": "https://a.example.com"})
        rec("4 未认证注册 → 401", st == 401, st)

        st, body, _ = req(port, "POST", "/register",
                          body={"name": "bad name!!", "url": "https://x.example.com"},
                          headers={"X-Fleet-Token": TOK})
        rec("5 name 非法 → 400", st == 400, "%s %s" % (st, body[:70]))

        st, body, _ = req(port, "POST", "/register",
                          body={"name": "nas", "url": "https://a.example.com"},
                          headers={"X-Fleet-Token": TOK})
        j = json.loads(body)
        rec("5b 合法注册 → 200 且 changed=True", st == 200 and j.get("changed") is True, j)

        st, body, _ = req(port, "POST", "/register",
                          body={"name": "nas", "url": "https://a.example.com"},
                          headers={"X-Fleet-Token": TOK})
        rec("5c 同地址重复注册 changed=False", json.loads(body).get("changed") is False)

        st, body, _ = req(port, "POST", "/register",
                          body={"name": "nas", "url": "https://evil.example.net"},
                          headers={"X-Fleet-Token": TOK})
        j = json.loads(body)
        rec("5d 换域名被标记 domain_changed", j.get("domain_changed") is True, j)

        st, body, _ = req(port, "GET", "/nodes", headers={"X-Fleet-Token": TOK})
        rec("5e GET /nodes 可见新地址",
            st == 200 and "evil.example.net" in body, st)

        # 非 ASCII token
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
        c.putrequest("GET", "/nodes")
        c.putheader("X-Fleet-Token", b"\xff\xfe\x80")
        c.endheaders()
        rr = c.getresponse()
        rr.read()
        c.close()
        rec("6 非 ASCII token → 401（不再抛异常）", rr.status == 401, rr.status)
        rec("6b 进程存活", proc.poll() is None, proc.poll())

        # 限速
        codes = []
        for i in range(40):
            st, _, _ = req(port, "POST", "/register",
                           body={"name": "n%d" % i, "url": "https://n%d.example.com" % i},
                           headers={"X-Fleet-Token": TOK})
            codes.append(st)
        rec("7 注册限速触发 429", 429 in codes, "最后5个=%s" % codes[-5:])
    finally:
        proc.kill()

    ok = sum(1 for _, c in RESULTS if c)
    print("\n==== %d/%d 通过 ====" % (ok, len(RESULTS)))
    for n, c in RESULTS:
        if not c:
            print("  FAILED: " + n)
    shutil.rmtree(BASE, ignore_errors=True)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
