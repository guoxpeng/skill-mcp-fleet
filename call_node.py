# -*- coding: utf-8 -*-
"""直接走 MCP 协议调用副端工具（不必重启 WorkBuddy）。

用法：
  python call_node.py <mcp-url> "<命令1>" ["<命令2>" ...]
  python call_node.py <mcp-url> --tool sysinfo
  python call_node.py <mcp-url> --list
"""
import json
import sys
import urllib.request


def _open(req, timeout):
    """内网/本机/tailnet(100.64.0.0/10、*.ts.net) 地址绕过 http_proxy 直连。"""
    import ipaddress
    import urllib.parse
    host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower().rstrip(".")
    direct = host == "localhost" or host.endswith((".local", ".ts.net"))
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip in ipaddress.ip_network("100.64.0.0/10") or \
            ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    handlers = [urllib.request.ProxyHandler({})] if direct else []
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def make_client(url):
    sid = {"v": None}

    def post(payload, timeout=90):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            method="POST")
        if sid["v"]:
            req.add_header("Mcp-Session-Id", sid["v"])
        with _open(req, timeout=timeout) as r:
            if r.headers.get("Mcp-Session-Id"):
                sid["v"] = r.headers["Mcp-Session-Id"]
            body = r.read().decode("utf-8", "ignore")
        txt = body
        for line in body.splitlines():
            if line.startswith("data:"):
                txt = line[5:].strip()
        return json.loads(txt)

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "workbuddy-call", "version": "1.0"}}})
    return post


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    url = sys.argv[1]
    post = make_client(url)
    args = sys.argv[2:]

    if args[0] == "--list":
        d = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        for t in d.get("result", {}).get("tools", []):
            print("-", t["name"], "|", (t.get("description") or "").split("\n")[0][:70])
        return 0

    if args[0] == "--tool":
        name = args[1]
        arguments = json.loads(args[2]) if len(args) > 2 else {}
    else:
        name, arguments = "exec", {"command": args[0]}

    d = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})
    res = d.get("result", d.get("error", {}))
    content = res.get("content")
    if content:
        for c in content:
            print(c.get("text", ""))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
