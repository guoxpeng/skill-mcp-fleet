#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 主端 · 副设备注册工具
=========================
装好副端之后，在【主端电脑（WorkBuddy 所在机器）】运行本脚本，
它会：1) 探测副端是否可达  2) 测试工具调用  3) 自动把该副端写入 ~/.workbuddy/mcp.json

用法：
  python add_fleet_node.py --name fnos --ip 192.168.5.4 --port 3100
  python add_fleet_node.py --name nas  --ip 192.168.1.10 --port 3100 --remove
  python add_fleet_node.py --list

改完记得重启 WorkBuddy 或重载 MCP 配置才会生效。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MCP = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")


def http_json(url, payload=None, timeout=10):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def load_mcp(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg.setdefault("mcpServers", {})
    return cfg


def save_mcp(path, cfg):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def probe(ip, port):
    """返回 (ok, 描述, 工具数量)。"""
    base = "http://%s:%d" % (ip, port)
    try:
        info = http_json(base + "/")
    except urllib.error.URLError as e:
        return False, "连不上 %s （%s）" % (base, e.reason), 0
    except Exception as e:
        return False, "连不上 %s （%r）" % (base, e), 0
    n = info.get("tools", 0)
    return True, "副端在线：%s v%s，%d 个工具" % (info.get("server"), info.get("version"), n), n


def test_call(ip, port):
    """真实调用一次 exec，验证工具可用。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "exec", "arguments": {"command": "hostname; uname -srm"}}}
    try:
        d = http_json("http://%s:%d/mcp" % (ip, port), body, timeout=25)
        c = d.get("result", {}).get("content", [{}])[0].get("text", "")
        return True, c.strip().replace("\n", " / ")[:120]
    except Exception as e:
        return False, "工具调用失败：%r" % (e,)


def main():
    ap = argparse.ArgumentParser(description="注册 / 移除 MCP 副端")
    ap.add_argument("--name", required=False, help="副端标识（mcp.json 里的 key）")
    ap.add_argument("--ip", required=False, help="副端 IP")
    ap.add_argument("--port", type=int, default=3100, help="副端端口（默认 3100）")
    ap.add_argument("--mcp", default=DEFAULT_MCP, help="mcp.json 路径")
    ap.add_argument("--remove", action="store_true", help="从 mcp.json 移除该副端")
    ap.add_argument("--list", action="store_true", help="列出已注册的副端")
    ap.add_argument("--no-test", action="store_true", help="跳过连通性测试")
    args = ap.parse_args()

    cfg = load_mcp(args.mcp)

    if args.list:
        print("mcp.json: %s" % args.mcp)
        if not cfg["mcpServers"]:
            print("  (空)")
        for k, v in cfg["mcpServers"].items():
            loc = v.get("url") or (v.get("command", "") + " " + " ".join(v.get("args", [])))
            print("  - %-12s [%s] %s" % (k, v.get("type", "?"), loc.strip()))
        return 0

    if not args.name:
        ap.error("需要 --name（或用 --list）")

    if args.remove:
        if args.name in cfg["mcpServers"]:
            del cfg["mcpServers"][args.name]
            save_mcp(args.mcp, cfg)
            print("[+] 已从 mcp.json 移除副端: %s" % args.name)
        else:
            print("[i] mcp.json 里没有 %s，无需移除" % args.name)
        return 0

    if not args.ip:
        ap.error("需要 --ip")

    url = "http://%s:%d/mcp" % (args.ip, args.port)

    if not args.no_test:
        ok, msg, n = probe(args.ip, args.port)
        print(("[+] " if ok else "[x] ") + msg)
        if not ok:
            print("\n请先在副端执行安装脚本：")
            print("  sudo bash install.sh --name %s --port %d" % (args.name, args.port))
            return 2
        ok2, out = test_call(args.ip, args.port)
        print(("[+] " if ok2 else "[!] ") + "工具测试: " + out)

    cfg["mcpServers"][args.name] = {
        "type": "http",
        "url": url,
        "description": "%s 副端 MCP（远程操控该设备：exec/文件/Docker/systemd/系统信息）" % args.name,
    }
    save_mcp(args.mcp, cfg)

    print("\n[+] 已写入 %s" % args.mcp)
    print('    "%s": { "type": "http", "url": "%s" }' % (args.name, url))
    print("\n下一步：重启 WorkBuddy（或重载 MCP 配置），即可调用 %s 的工具。" % args.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
