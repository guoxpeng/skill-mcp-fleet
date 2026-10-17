# -*- coding: utf-8 -*-
"""本地主端 -> 远程副端 MCP 协议连通性实测（Streamable HTTP）。"""
import json
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.10:3199/mcp"
HDR = {"Content-Type": "application/json",
       "Accept": "application/json, text/event-stream"}
SID = {"v": None}


def post(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers=HDR, method="POST")
    if SID["v"]:
        req.add_header("Mcp-Session-Id", SID["v"])
    with urllib.request.urlopen(req, timeout=25) as r:
        if r.headers.get("Mcp-Session-Id"):
            SID["v"] = r.headers["Mcp-Session-Id"]
        body = r.read().decode("utf-8", "ignore")
    txt = body
    for line in body.splitlines():
        if line.startswith("data:"):
            txt = line[5:].strip()
    if not txt.strip():
        return {}          # 通知类请求正常返回 202 + 空 body
    return json.loads(txt)


init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "workbuddy-probe", "version": "1.0"}}})
print("initialize ->", init.get("result", {}).get("serverInfo"), "sid=", SID["v"])

post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
print("tools/list ->", len(names), names)

call = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
    "name": "exec", "arguments": {"command": "uname -a && echo MCP_E2E_OK"}}})
print("tools/call exec ->", json.dumps(call.get("result", {}), ensure_ascii=False)[:400])

call2 = post({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
    "name": "sysinfo", "arguments": {}}})
print("tools/call sysinfo ->", json.dumps(call2.get("result", {}), ensure_ascii=False)[:300])
