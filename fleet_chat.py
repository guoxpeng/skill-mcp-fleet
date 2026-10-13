# -*- coding: utf-8 -*-
"""fleet_chat —— AI 之间通过 fleet 节点邮箱对话（Python 3.6+，仅标准库）。

邮箱放在双方都能访问的节点（如十堰 http://100.91.169.103:3100/mcp）。

用法：
  python3 fleet_chat.py send  --me muse --to hark "消息内容"     # 也可用 - 从 stdin 读
  python3 fleet_chat.py recv  --me muse                          # 取新消息，立即返回
  python3 fleet_chat.py wait  --me muse [--timeout 1800]         # 阻塞直到有新消息
  python3 fleet_chat.py history [--lines 20]

连接参数（可放环境变量）：
  --url   / FLEET_CHAT_URL    默认 http://127.0.0.1:3100/mcp
  --token / FLEET_CHAT_TOKEN  默认读取同目录或 /root/fleet-agent、~/fleet-agent 下 mcp_agent_config.json 的 token
"""
import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PREFIX_TRY = ("", "fleet_", "nas_")


def _find_token():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, "/root/fleet-agent", os.path.expanduser("~/fleet-agent"), "/opt/fleet-agent"):
        p = os.path.join(d, "mcp_agent_config.json")
        if os.path.exists(p):
            try:
                with open(p) as f:
                    _c = json.load(f)
                t = _c.get("auth_token") or _c.get("token")
                if t:
                    return t
            except (OSError, ValueError):
                pass
    return ""


def _opener(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    direct = host == "localhost" or host.endswith(".ts.net")
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip in ipaddress.ip_network("100.64.0.0/10") or ip.is_private or ip.is_loopback
    except ValueError:
        pass
    return urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if direct else []))


class Client(object):
    def __init__(self, url, token):
        self.url, self.token, self.sid, self.n = url, token, None, 0
        self.op = _opener(url)

    def _post(self, payload, timeout):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        with self.op.open(req, timeout=timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            body = r.read().decode("utf-8", "replace")
        if not body.strip():
            return None
        if body.lstrip().startswith("{"):
            return json.loads(body)
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None

    def call(self, tool, args, timeout=60):
        if self.sid is None:
            self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "fleet_chat", "version": "1"}}}, 30)
        last = None
        for pre in PREFIX_TRY:
            self.n += 1
            r = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                            "params": {"name": pre + tool, "arguments": args}}, timeout)
            res = (r or {}).get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content", []))
            if text.startswith("未知工具"):
                last = text
                continue
            return text, bool(res.get("isError"))
        return (last or "调用失败") + "（目标节点 fleet-agent 版本太旧，未包含 msg_* 工具）", True


def main():
    ap = argparse.ArgumentParser(description="AI 之间通过 fleet 节点邮箱对话")
    ap.add_argument("cmd", choices=["send", "recv", "wait", "history"])
    ap.add_argument("text", nargs="*")
    ap.add_argument("--me", default=os.environ.get("FLEET_CHAT_ME", ""))
    ap.add_argument("--to", default="")
    ap.add_argument("--timeout", type=int, default=1800, help="wait 最长秒数，0=一直等")
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--url", default=os.environ.get("FLEET_CHAT_URL", "http://127.0.0.1:3100/mcp"))
    ap.add_argument("--token", default=os.environ.get("FLEET_CHAT_TOKEN") or _find_token())
    a, extra = ap.parse_known_args()
    bad = [x for x in extra if x.startswith("--")]
    if bad:
        ap.error("未知参数: %s" % " ".join(bad))
    a.text = list(a.text) + extra  # 兼容 Python 3.6：选项之后的正文
    c = Client(a.url, a.token)

    if a.cmd == "send":
        text = sys.stdin.read() if a.text in ([], ["-"]) else " ".join(a.text)
        out, err = c.call("msg_send", {"from": a.me, "to": a.to, "text": text})
    elif a.cmd == "history":
        out, err = c.call("msg_history", {"lines": a.lines})
    elif a.cmd == "recv":
        out, err = c.call("msg_recv", {"me": a.me})
    else:
        end = time.time() + a.timeout if a.timeout > 0 else None
        while True:
            left = 300 if end is None else int(min(300, end - time.time()))
            if left <= 0:
                out, err = "（等待超时，没有新消息）", False
                break
            try:
                out, err = c.call("msg_recv", {"me": a.me, "wait": left}, timeout=left + 30)
            except Exception as e:  # 网络抖动：稍后重试
                sys.stderr.write("连接出错，5 秒后重试: %r\n" % (e,))
                time.sleep(5)
                c.sid = None
                continue
            if err or not out.startswith("（没有新消息"):
                break
    print(out)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
