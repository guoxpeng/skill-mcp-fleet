#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fleet 节点健康看门：probe 所有已注册节点，只在状态变化时输出。
供 cron 定时调用，无变化则静默。

用法：python3 fleet_watch.py [--agent workbuddy]
状态文件：~/.mcp-fleet/watch_state.json
"""
import json
import os
import subprocess
import sys

STATE_FILE = os.path.expanduser("~/.mcp-fleet/watch_state.json")
FLEET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.py")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def list_nodes(agent):
    r = subprocess.run([sys.executable, FLEET, "list", "--agent", agent, "--json"],
                       capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def _is_tailnet_url(url):
    """Tailscale 内网地址（100.64.0.0/10 / fd7a:115c:a1e0::/48）？"""
    import ipaddress
    import urllib.parse
    try:
        host = urllib.parse.urlparse(url or "").hostname or ""
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return (ip in ipaddress.ip_network("100.64.0.0/10")
            or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def probe_node(name, url, agent):
    """返回 (ok: bool, detail: str)"""
    r = subprocess.run([sys.executable, FLEET, "probe", name,
                        "--agent", agent, "--no-sync"],
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    # Tailscale 内网节点：后台任务的 POST 会被运行时网络策略拦截，
    # 只能做 GET 探活——"[+] HTTP 可达"（副端返回了 server/version 信息）即视为在线。
    ok = ("[+] HTTP 可达" in out) if _is_tailnet_url(url) else ("[+] MCP 握手成功" in out)
    # 提炼一行摘要
    detail = ""
    for line in out.splitlines():
        if "HTTP 可达" in line or "MCP 握手成功" in line or "失败" in line:
            detail = line.strip()[:100]
            break
    return ok, detail


def main():
    agent = "workbuddy"
    if "--agent" in sys.argv:
        agent = sys.argv[sys.argv.index("--agent") + 1]
    nodes = list_nodes(agent)
    if not nodes:
        print("WATCH: 没有已注册节点")
        return 0
    prev = load_state()
    cur, changes = {}, []
    for name in sorted(nodes):
        info = nodes[name] or {}
        url = info.get("url") if isinstance(info, dict) else info
        ok, detail = probe_node(name, url, agent)
        cur[name] = {"ok": ok, "detail": detail}
        was = prev.get(name, {}).get("ok")
        if was is None:
            changes.append("新节点 %s：%s" % (name, "在线" if ok else "不在线"))
        elif was and not ok:
            changes.append("掉线 %s：%s" % (name, detail or "probe 失败"))
        elif not was and ok:
            changes.append("恢复 %s：在线" % name)
    # 注册表中消失的节点
    for name in sorted(set(prev) - set(cur)):
        changes.append("注销 %s：已从注册表移除" % name)
    save_state(cur)
    if changes:
        print("WATCH 变化：")
        for c in changes:
            print("  - " + c)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
