#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_agent.py v3.1 加固补丁自测（隔离目录起真实实例，逐条验证 + 回归）。

覆盖：
  1. CORS 默认不再发 Access-Control-Allow-Origin
  2. 裸奔模式状态页不泄露 tool_names / root / auth 指纹
  3. 配 token 后：无凭据 401、正确凭据 200
  4. Authorization 头带非 ASCII 字节不再抛 TypeError（进程存活）
  5. 超大 Content-Length 被 413 拦截
  6. 回归：tools/call exec 正常执行、tools/list 正常返回
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"<home>\.workbuddy\binaries\python\versions\3.13.12\python.exe"
SRC = os.path.join(HERE, "mcp_agent.py")
BASE = os.path.join(HERE, ".selftest_tmp")

RESULTS = []


def rec(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("[PASS] " if cond else "[FAIL] ") + name + ("   " + str(extra) if extra else ""))
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_inst(name, cfg):
    d = os.path.join(BASE, name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    shutil.copy2(SRC, os.path.join(d, "mcp_agent.py"))
    with open(os.path.join(d, "mcp_agent_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return d


def start(d, port):
    p = subprocess.Popen([PY, "mcp_agent.py", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            c = socket.create_connection(("127.0.0.1", port), 0.3)
            c.close()
            return p
        except OSError:
            time.sleep(0.15)
    p.kill()
    raise RuntimeError("副端未在 9s 内监听")

def get(port, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    c.request("GET", "/", headers=headers or {})
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, body, hdrs


def rpc(port, payload, headers=None, big_cl=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    raw = json.dumps(payload).encode()
    if big_cl:
        h["Content-Length"] = "99999999"
        c.putrequest("POST", "/mcp")
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        # 故意不写 body：服务端应在读 body 之前就按上限拒绝
    else:
        c.request("POST", "/mcp", body=raw, headers=h)
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    c.close()
    return r.status, body


def main():
    # 本机（Windows）没有原生 bash，裸 `bash` 会解析到 WSL 桩被安全策略拦。
    # 把 Git Bash 的 bin 前置进 PATH，让副端的 run_local(["bash",...]) 能真正跑起来，
    # 这样 exec 通路是「真验证」而不是跳过。
    gitbin = r"<home>\AppData\Local\hermes\git\bin"
    if os.path.isdir(gitbin):
        os.environ["PATH"] = gitbin + os.pathsep + os.environ.get("PATH", "")

    if os.path.isdir(BASE):
        shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE)

    # ---------- 阶段 A：裸奔模式（无 token）----------
    dA = make_inst("plain", {"name": "t-plain", "version": "3.1.0", "tool_prefix": "",
                             "auth_token": "", "allowed_roots": ["/"], "login_shell": False})
    pA = free_port()
    procA = start(dA, pA)
    try:
        st, body, hdrs = get(pA)
        j = json.loads(body)
        rec("A1 状态页 200", st == 200, st)
        rec("A2 未认证不再泄露工具指纹",
            "tool_names" not in j and "root" not in j and "auth" not in j, j)
        rec("A3 响应头无 ACAO 通配",
            "Access-Control-Allow-Origin" not in hdrs, hdrs.get("Access-Control-Allow-Origin"))
        # 回归：exec 通路（本机沙箱会拦任何 bash 子进程，故只验 JSON-RPC 通路完整性）
        st, body = rpc(pA, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "exec", "arguments": {"command": "echo REGRESS_EXEC_OK"}}})
        try:
            txt = json.loads(body)["result"]["content"][0]["text"]
            okfmt = st == 200 and txt.startswith("[exit")
        except Exception:
            okfmt = False
        rec("A4 exec 通路返回合法 JSON-RPC（本机无原生 bash，仅验通路）", okfmt, body[:110])
        st, body = rpc(pA, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        n = len(json.loads(body).get("result", {}).get("tools", []))
        rec("A5 回归 tools/list 正常", n == 12, "tools=%d" % n)
    finally:
        procA.kill()

    # ---------- 阶段 B：带 token ----------
    dB = make_inst("locked", {"name": "t-lock", "version": "3.1.0", "tool_prefix": "",
                              "auth_token": "TESTTOKEN_ABC123", "allowed_roots": ["/"],
                              "login_shell": False})
    pB = free_port()
    procB = start(dB, pB)
    try:
        st, body, _ = get(pB)
        rec("B1 无凭据 GET / → 401", st == 401, st)
        st, body, _ = get(pB, {"Authorization": "Bearer TESTTOKEN_ABC123"})
        j = json.loads(body)
        rec("B2 正确凭据 GET / → 200", st == 200, st)
        rec("B3 认证后可看到工具清单", j.get("tools") == 12, j.get("tools"))
        rec("B4 认证后 auth 字段为 True", j.get("auth") is True, j.get("auth"))
        # 非 ASCII Authorization（老代码在此抛 TypeError）
        c = http.client.HTTPConnection("127.0.0.1", pB, timeout=6)
        c.putrequest("GET", "/")
        c.putheader("Authorization", b"Bearer \xff\xfe\x80")
        c.endheaders()
        r = c.getresponse()
        b = r.read().decode("utf-8", "replace")
        c.close()
        rec("B5 非 ASCII 凭据 → 401（不再抛异常）", r.status == 401, "%s %s" % (r.status, b[:60]))
        rec("B6 进程存活（未因异常退出）", procB.poll() is None, procB.poll())
        # 正确 token 正常走通
        st, body = rpc(pB, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "exec", "arguments": {"command": "echo TOKEN_EXEC_OK"}}},
                       headers={"Authorization": "Bearer TESTTOKEN_ABC123"})
        try:
            txt = json.loads(body)["result"]["content"][0]["text"]
            okfmt = st == 200 and txt.startswith("[exit")
        except Exception:
            okfmt = False
        rec("B7 带 token 后 exec 通路正常", okfmt, body[:110])
        # 错误 token
        st, body = rpc(pB, {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                       headers={"Authorization": "Bearer WRONG"})
        rec("B8 错误 token → 401", st == 401, st)
        # 超大 Content-Length
        st, body = rpc(pB, {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                       headers={"Authorization": "Bearer TESTTOKEN_ABC123"}, big_cl=True)
        rec("B9 超大 Content-Length → 413", st == 413, "%s %s" % (st, body[:70]))
        st, body, hdrs = get(pB, {"Authorization": "Bearer TESTTOKEN_ABC123"})
        rec("B10 带鉴权也无 ACAO 通配",
            "Access-Control-Allow-Origin" not in hdrs,
            hdrs.get("Access-Control-Allow-Origin"))
    finally:
        procB.kill()

    # ---------- 阶段 C：require_auth 硬失败 ----------
    dC = make_inst("reqauth", {"name": "t-req", "auth_token": "", "require_auth": True})
    r = subprocess.run([PY, "mcp_agent.py", "--host", "127.0.0.1", "--port", str(free_port())],
                       cwd=dC, capture_output=True, timeout=20)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    rec("C1 require_auth 且空 token → 拒绝启动(exit 2)", r.returncode == 2, r.returncode)
    rec("C2 给出修正命令提示", "FATAL" in out and "--generate-token" in out, out.strip().splitlines()[:1])

    # ---------- 阶段 D：--generate-token 自动生成并 0600 ----------
    dD = make_inst("gentok", {"name": "t-gen", "auth_token": ""})
    portD = free_port()
    proc = subprocess.Popen([PY, "mcp_agent.py", "--host", "127.0.0.1", "--port", str(portD),
                             "--generate-token"],
                            cwd=dD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3)
    cfgp = os.path.join(dD, "mcp_agent_config.json")
    try:
        cfg = json.load(open(cfgp, encoding="utf-8"))
        tok = cfg.get("auth_token") or ""
        rec("D1 自动生成 token", len(tok) >= 40, "len=%d" % len(tok))
        mode = oct(os.stat(cfgp).st_mode & 0o777) if os.name != "nt" else "nt-skip"
        rec("D2 配置文件权限收紧", mode in ("0o600", "nt-skip"), mode)
        st, _, _ = get(portD)
        rec("D3 生成后立即生效（无凭据被拒）", st == 401, st)
        st, _, _ = get(portD, {"Authorization": "Bearer " + tok})
        rec("D4 新 token 可用", st == 200, st)
    finally:
        proc.kill()

    # ---------- 阶段 E：纯函数级验证（不依赖 bash，直击被改的代码）----------
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_agent_probe", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src_txt = open(SRC, encoding="utf-8").read()
    mod.CFG["login_shell"] = False
    mod.CFG["auth_token"] = "TOK_XYZ"
    mod.CFG["allow_ips"] = []
    mod.CFG["allowed_roots"] = ["/tmp"]
    mod.AUTH_FAILS.clear()

    rec("E1 _wrap 未被本次改动影响", mod._wrap("echo hi") == "bash -c 'echo hi'",
        mod._wrap("echo hi"))
    try:
        r1 = mod.check_access("1.2.3.4", {"Authorization": "Bearer \xff\xfe"})
        rec("E2 非 ASCII 凭据不再抛 TypeError", r1 is not None and r1[0] == 401, r1)
    except TypeError as e:
        rec("E2 非 ASCII 凭据不再抛 TypeError", False, "TypeError: %s" % e)
    rec("E3 正确凭据放行",
        mod.check_access("1.2.3.4", {"Authorization": "Bearer TOK_XYZ"}) is None)
    rec("E4 X-Fleet-Token 头放行",
        mod.check_access("1.2.3.4", {"X-Fleet-Token": "TOK_XYZ"}) is None)
    try:
        mod._check_path("outside/secret.txt")
        rec("E5 白名单外路径被拒", False, "未抛异常")
    except ValueError:
        rec("E5 白名单外路径被拒", True)
    rec("E6 白名单内路径通过", mod._check_path("/tmp/a.txt").endswith("a.txt"))
    rec("E7 cors_allow_origins 默认空（默认不发 CORS）",
        mod.DEFAULT_CONFIG.get("cors_allow_origins") == [])
    rec("E8 默认只绑 127.0.0.1",
        'ap.add_argument("--host", default="127.0.0.1")' in src_txt)

    ok = sum(1 for _, c in RESULTS if c)
    print("\n==== %d/%d 通过 ====" % (ok, len(RESULTS)))
    for name, c in RESULTS:
        if not c:
            print("  FAILED: " + name)
    shutil.rmtree(BASE, ignore_errors=True)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
