#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-fleet 统一命令行 —— 节点之间互相操控（不分主副，谁发起谁就是控制方）
=========================================
零依赖（只用 Python 3 标准库），可在任何 agent / 任何操作系统上运行。

核心价值：不依赖某个特定 agent 的 MCP 支持。只要这台机器能访问副端端口，
本脚本就能直接按 MCP 协议（Streamable HTTP）操控副端，不必重启 agent、
不必改任何配置文件。

子命令
------
  list                          列出已注册的副端（扫所有 agent 配置 + 本地登记表）
  add                           注册副端（写 agent 的 mcp.json，同时登记到本地表）
  remove <name>                 注销副端
  tools <target>                列副端工具清单
  probe <target>                连通性 + 握手 + 列工具 + 试调 exec（只读诊断）
  doctor <target>               完整链路体检（逐项打分，失败给排查方向）
  exec <target> <命令...>       在副端执行 shell 命令
  shell <target>                交互式 shell（PTY，可跑 vim/top，替代 ssh）
  forward <target> -L 8080:h:80 端口转发（等价 ssh -L / -R）
  call <target> --tool T [--args JSON]   调任意工具
  health [--deep]               批量体检所有副端
  install-cmd                   打印「把一台 Linux 设备变成副端」的安装命令
  bootstrap                     通过 SSH 把副端装到远程设备（离线优先，不必外网）
  tunnel <target>               查看 / 启动 / 停止副端的 cloudflared 公网隧道
  sync [名字...]                按地址目录刷新副端地址（隧道换域名后自动跟随）
  directory                     查看/设置地址目录（副端上报地址、主端自动跟随）
  targets                       列出所有可作为 target 的名字与地址
  pair <对方地址|名字>          双向配对（v3.5）：互相登记，之后两边都能控制对方

target 写法
-----------
  名字   → 从配置/登记表里解析，如  nas
  完整地址 → http://192.168.1.10:3100/mcp  或 192.168.1.10:3100

地址自动跟随（隧道副端推荐）
---------------------------
副端用 cloudflared 快隧时地址是临时的，重启就换域名。副端保活守护会把当前地址
持续上报到「地址目录」，主端这边在地址不通时会自动问一次目录、换成新地址 ——
不用再手工改 mcp.json。关掉：--no-sync 或 FLEET_NO_SYNC=1。

示例
----
  python fleet.py list
  python fleet.py exec nas "docker ps --format '{{.Names}}'"
  python fleet.py call nas --tool sysinfo
  python fleet.py call cloud --tool read --args '{"filePath":"/etc/os-release"}'
  python fleet.py health --deep
  python fleet.py install-cmd --name pi --port 3100 --mirror ghfast
  python fleet.py bootstrap --host 192.168.1.9 --user root --name pi
  python fleet.py tunnel cloud --status
  python fleet.py directory --url http://192.168.1.10:8790 --token <token>
  python fleet.py sync cloud
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------- 基础

VERSION = "5.2.2"
PROTOCOL = "2024-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))

# v5.2: CLI 内置提示词。菜单/`fleet prompt` 直接输出，可原样粘给 AI 客户端。
PROMPT_PACK = {
    "self_repair": (
        "你是 MCP Fleet 节点上的运维 agent。先调用 fleet_selfcheck 获取结构化发现；"
        "如果存在可修复项，调用 fleet_repair_loop(confirm=<节点名>, max_attempts=2)，"
        "只允许它做重启/权限/pid 修复；失败立即停止，把 remaining findings 原样报告，"
        "禁止循环重试、禁止执行未列出的命令、禁止删除数据或轮换 token。"),
    "diagnose": (
        "先用只读工具（fleet_doctor、sysinfo、ps、docker_ps、net_check、df）收集证据，"
        "再给结论。任何删除/重启/改配置前先向我确认；最后按“结论/证据/建议”三段式回复，"
        "每条结论必须引用具体命令输出。"),
    "collaborate": (
        "用 fleet_plan 把目标拆成带验收标准的小步；按节点能力把步骤分给 self 或 peer，"
        "每步执行后检查验收条件；连续两次失败就停止并请求人工介入，不要无限重试。"),
    "pair": (
        "用 fleet_invite 生成邀请，让对方执行 fleet join；首次配对必须核对两端 6 位 SAS，"
        "不一致立即中止。配对后用 fleet nodes 确认在线与指纹。"),
    "upgrade": (
        "升级前先 fleet status 记录当前版本与指纹；执行 fleet update（会下载 Release 并校验 "
        "SHA256、停旧服务、重启）；健康检查失败用 fleet update --rollback，"
        "仍失败则保留 .failed-* 目录与日志并报告。"),
}


def prompt_text(topic=None):
    """返回提示词文本；topic 为空则返回全部（带标题）。"""
    if topic:
        return PROMPT_PACK.get(topic, "未知提示词：%s（可选 %s）"
                               % (topic, ", ".join(sorted(PROMPT_PACK))))
    out = []
    for key in ("self_repair", "diagnose", "collaborate", "pair", "upgrade"):
        out.append("### %s\n%s" % (key, PROMPT_PACK[key]))
    return "\n\n".join(out)
# 登记表位置：
#   * 本脚本装在节点目录里（同目录有 mcp_agent_config.json，v3.5 安装器会这么放）
#     → 用 <节点目录>/peers.json，与本节点 agent 的 peers / peer_call 工具共用一份对等节点表；
#   * 否则（如只当控制端的 Windows 电脑）→ ~/.mcp-fleet/nodes.json。
#   FLEET_REGISTRY 环境变量可强制指定。
if os.environ.get("FLEET_REGISTRY"):
    REGISTRY = os.environ["FLEET_REGISTRY"]
elif os.path.isfile(os.path.join(HERE, "mcp_agent_config.json")):
    REGISTRY = os.path.join(HERE, "peers.json")
else:
    REGISTRY = os.path.join(os.path.expanduser("~"), ".mcp-fleet", "nodes.json")

try:                                  # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

C_OK, C_ERR, C_WARN, C_DIM, C_OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    C_OK = C_ERR = C_WARN = C_DIM = C_OFF = ""


def ok(s):
    return C_OK + s + C_OFF


def err(s):
    return C_ERR + s + C_OFF


def warn(s):
    return C_WARN + s + C_OFF


def dim(s):
    return C_DIM + s + C_OFF


def die(msg, code=1):
    try:
        sys.stdout.flush()
    except Exception:
        pass
    print(err("[x] ") + msg, file=sys.stderr)
    return code


# ---------------------------------------------------------------- MCP 客户端


def _is_tailnet_host(host):
    """判断是否 tailnet 地址：100.64.0.0/10（CGNAT）、fd7a:115c:a1e0::/48、*.ts.net。

    Tailscale 的地址段正好落在「私网」范围里，但它的可达性语义和局域网
    完全不同——见 _opener_for 的说明。
    """
    h = (host or "").lower().strip("[]")
    if not h:
        return False
    if h.endswith(".ts.net"):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(h)
        if ip.version == 4:
            return ip in ipaddress.ip_network("100.64.0.0/10")
        return ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")
    except ValueError:
        return False


def _proxy_configured():
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
                or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))


def _opener_for(url, insecure=False):
    """内网/本机地址不走 http_proxy：机器上配了代理时，局域网节点会被代理成 404/超时。

    例外（tailnet）：100.64.0.0/10 是 Tailscale 的 CGNAT 段，虽然 ipaddress
    把它算作私网，但它的可达性来自 tailscaled 而不是本机路由。当机器配了
    代理、而这条代理正是通往 tailnet 的唯一通路（托管沙盒的受限隧道代理）
    时，绕过代理等于把 tailnet 节点全部变成「连不上」。所以：
    配了代理 + tailnet 地址 → 交给代理走。
    """
    import ipaddress
    import urllib.parse
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host == "localhost" or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    if _is_tailnet_host(host) and _proxy_configured():
        direct = False
    import ssl
    handlers = []
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    if urllib.parse.urlsplit(url).scheme == "https":
        if direct or insecure:
            # 本机/局域网节点常为自签证书：仅在本机命令或私网/回环时豁免校验；
            # 公网对端（insecure=False 且非私网）仍使用系统 CA。
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        # 公网 HTTPS（GitHub 等）保持系统默认证书校验，防中间人
    return urllib.request.build_opener(*handlers)


class MCPError(Exception):
    pass


def _pool_plan(url):
    """决定这个地址用哪种传输，返回 (mode, proxy) —— mode ∈ "direct"/"proxy"/None。

    * http + 内网/本机 → 直连（局域网节点必须绕开代理，否则会被代理成 404）
    * http + 其余（公网 / tailnet / 有代理）→ 走代理长连接
    * https → None（TLS 与自签证书逻辑留在 urllib，不复制第二份）
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return None, None
    if parts.scheme != "http":
        return None, None
    host = parts.hostname or ""
    if not host:
        return None, None
    local = host == "localhost" or host.endswith(".local")
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        local = local or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    if _is_tailnet_host(host):
        local = False            # tailnet 不是本机路由，必须走 tailnet 通路
    if local:
        return "direct", None
    if _proxy_configured():
        # 代理下的长连接默认关闭：实测各家代理对「同一连接上的第二条请求」
        # 处理不一致（有的只在首条请求上把 absolute-URI 改写成 origin-form，
        # 第二条原样转发，服务端看到 "http://host/mcp" 直接 404）。
        # 想试可显式开 FLEET_POOL_PROXY=1；失败也只是回退到每次新建连接。
        if os.environ.get("FLEET_POOL_PROXY") == "1":
            return "proxy", _proxy_addr(parts.scheme)
        return None, None
    return "direct", None


def _proxy_addr(scheme):
    """取 http 代理的 (host, port, auth_header)；没有则 None。"""
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        if "://" not in raw:
            raw = "http://" + raw
        try:
            p = urllib.parse.urlsplit(raw)
        except Exception:
            continue
        if not p.hostname:
            continue
        auth = ""
        if p.username:
            cred = "%s:%s" % (urllib.parse.unquote(p.username),
                              urllib.parse.unquote(p.password or ""))
            auth = "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")
        return (p.hostname, p.port or 80, auth)
    return None


class _PooledHTTP(object):
    """最小化的 HTTP/1.1 长连接（支持直连与 http 代理两种模式）。

    为什么需要它：MCP 是无状态 HTTP，但**无状态不等于要每次重建 TCP**。
    跨公网/跨 tailnet 时单次 RTT 常有 300–600ms，每个工具调用都重新
    三次握手 + 慢启动，等于把一半时间花在建连上。`fleet cp` 一个 1MB
    文件要 9 个来回，累积起来就是十几秒的体感差距。

    只处理 http；https 仍走 urllib，避免把证书与代理逻辑复制一份出来。
    """

    def __init__(self, url, timeout=30, proxy=None):
        parts = urllib.parse.urlsplit(url)
        self.url = url
        self.host = parts.hostname
        self.port = parts.port or 80
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query
        self.timeout = timeout
        self.proxy = proxy
        self._conn = None

    def _target_path(self):
        """走代理时请求行必须是绝对 URI，直连时是路径。"""
        if self.proxy:
            return "http://%s:%d%s" % (self.host, self.port, self.path)
        return self.path

    def _connect(self):
        import http.client
        self.close()
        if self.proxy:
            phost, pport, _auth = self.proxy
            self._conn = http.client.HTTPConnection(phost, pport, timeout=self.timeout)
        else:
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        # 立刻建连并关掉 Nagle：http.client 是「写头 → 写体 → 读响应」的
        # 小包序列，攒包会让每请求多等一拍延迟 ACK（实测 174KB 响应
        # 从 50ms 变 250ms）。延迟建连的话第一次 request 已经发出去了，
        # 来不及设，所以这里主动 connect()。
        try:
            self._conn.connect()
            self._conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def post(self, body, headers, timeout=None):
        """POST 一次。连接被对端关掉时重连并重试一次（幂等由调用方保证）。"""
        last = None
        for attempt in (1, 2):
            conn = self._conn or self._connect()
            hdrs = dict(headers)
            # 走代理时必须显式写 Host：http.client 默认把 Host 设成「连的那台」
            # （也就是代理），虚拟主机路由就会错，服务端回 404。
            hdrs.setdefault("Host", "%s:%d" % (self.host, self.port))
            if self.proxy and self.proxy[2]:
                hdrs["Proxy-Authorization"] = self.proxy[2]
            try:
                conn.timeout = timeout or self.timeout
                conn.request("POST", self._target_path(), body=body, headers=hdrs)
                resp = conn.getresponse()
                data = _read_capped(resp)
                return resp.status, resp.getheaders(), data
            except (ConnectionError, OSError) as e:
                # 空闲连接被对端/中间设备回收是最常见的情况：
                # 重连一次即可，不能让用户看到「连不上」。
                last = e
                self.close()
                if attempt == 2:
                    raise
            except Exception:
                self.close()
                raise
        raise last

    def __del__(self):
        self.close()


class MCPClient:
    """Streamable HTTP 客户端：initialize → notifications/initialized → tools/*"""

    def __init__(self, url, timeout=30, token="", insecure=False):
        self.url = url
        self.insecure = bool(insecure)
        self.timeout = timeout
        self.sid = None
        self.info = {}
        self.token = (token or "").strip()
        self._pool = None         # 直连 http 时的长连接（见 _PooledHTTP）
        self._ready = False       # 是否已 initialize（延迟握手，见 call()）
        # http.client 的连接**不是线程安全的**：两个线程同时发请求会把状态机
        # 打乱，抛 "CannotSendRequest: Request-sent"。交互式 shell 天然是两个
        # 线程（读/写），所以这里必须串行化；需要并发的场景请各建一个客户端。
        self._lock = threading.Lock()

    # -- 传输层 --------------------------------------------------
    def _post(self, payload, timeout=None, retry=False):
        """发一条 JSON-RPC。

        retry=True 只给**幂等**方法用（initialize / tools/list / 只读探测）：
        连不上或超时重试一次，覆盖 tailnet/隧道抖动的瞬时失败。
        tools/call 这类会真正执行命令的请求**永不重试**——重试等于把
        `rm -rf` 跑两遍。401/403 同样不重试（令牌错了重试没用）。
        """
        attempts = 2 if retry else 1
        last = None
        for i in range(attempts):
            try:
                return self._post_once(payload, timeout=timeout)
            except MCPError as e:
                msg = str(e)
                # 鉴权/协议类错误不重试，只重试「连不上 / 超时 / 5xx」
                if ("HTTP 401" in msg or "HTTP 403" in msg or "HTTP 4" in msg
                        or "不是合法 JSON" in msg or "超过上限" in msg):
                    raise
                last = e
                if i + 1 < attempts:
                    time.sleep(1.0)
        raise last

    def _auth_error(self, code, reason=""):
        """401/403 的可操作提示：区分「没带令牌」和「令牌不对」。"""
        if not self.token:
            hint = ("副端开了鉴权，但本地没有它的 token。\n"
                    "     补一个： python fleet.py add --name <名字> --url %s --token <令牌>\n"
                    "     （令牌在副端 /opt/<名字>_mcp/mcp_agent_config.json 的 auth_token，"
                    "或跑 `bash mcp-ctl.sh` 时打印过）" % self.url)
        else:
            hint = ("副端拒绝了令牌（HTTP %d）。确认 auth_token 与主端填的一致，"
                    "或本机 IP 不在 allow_ips 白名单里。" % code)
        return MCPError("HTTP %s %s\n     %s" % (code, reason, hint))

    def _post_once(self, payload, timeout=None):
        with self._lock:
            return self._post_locked(payload, timeout=timeout)

    def _post_locked(self, payload, timeout=None):
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Connection": "keep-alive"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid

        mode, proxy = _pool_plan(self.url)
        if mode:
            # 直连或经代理：都复用 TCP，省掉每次调用的三次握手
            if self._pool is None:
                self._pool = _PooledHTTP(self.url, timeout=self.timeout,
                                         proxy=proxy if mode == "proxy" else None)
            try:
                status, hdrs, raw = self._pool.post(data, headers, timeout=timeout)
            except (ConnectionError, OSError) as e:
                raise MCPError("连不上 %s（%s）" % (self.url, e))
            except socket.timeout:
                raise MCPError("超时（%ss）" % (timeout or self.timeout))
            hmap = dict((k.lower(), v) for k, v in hdrs)
            if hmap.get("mcp-session-id"):
                self.sid = hmap["mcp-session-id"]
            if status in (401, 403):
                raise self._auth_error(status)
            if status >= 400:
                raise MCPError("HTTP %s" % status)
            body = raw.decode("utf-8", "ignore")
        else:
            req = urllib.request.Request(self.url, data=data, method="POST",
                                         headers=headers)
            try:
                with _opener_for(self.url, insecure=self.insecure).open(
                        req, timeout=timeout or self.timeout) as r:
                    if r.headers.get("Mcp-Session-Id"):
                        self.sid = r.headers["Mcp-Session-Id"]
                    body = _read_capped(r).decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise self._auth_error(e.code, e.reason)
                raise MCPError("HTTP %s %s" % (e.code, e.reason))
            except urllib.error.URLError as e:
                raise MCPError("连不上 %s（%s）" % (self.url, e.reason))
            except socket.timeout:
                raise MCPError("超时（%ss）" % (timeout or self.timeout))
        # 兼容 SSE 帧
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except ValueError:
            raise MCPError("返回不是合法 JSON：%s" % body[:200])

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # -- 协议层 --------------------------------------------------
    def handshake(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp-fleet", "version": VERSION}}}, retry=True)
        self.info = r.get("result", {}).get("serverInfo", {}) or {}
        self._ready = True
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                        "params": {}}, timeout=15)
        except MCPError:
            pass                      # 通知失败不影响后续
        return self.info

    def list_tools(self):
        r = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       retry=True)
        if "error" in r:
            raise MCPError(str(r["error"]))
        return r.get("result", {}).get("tools", [])

    def call(self, tool, arguments=None, timeout=None):
        """调用工具。

        延迟握手（v5.2.1）：本节点是无状态 HTTP 服务，`tools/call` 并不依赖
        `initialize` 建立会话。跨公网/tailnet 时握手要 2 个往返，在 900ms
        的链路上就是 1.8 秒白等——而 CLI 的绝大多数动作只是「调一个工具」。
        所以先直接发；只有对端明确要求先 initialize 时才补握手重试，
        对严格的第三方 MCP 服务器依然兼容。
        """
        try:
            return self._call_once(tool, arguments, timeout)
        except MCPError as e:
            msg = str(e).lower()
            if not self._ready and ("initialize" in msg or "not initialized" in msg
                                    or "session" in msg):
                self.handshake()
                return self._call_once(tool, arguments, timeout)
            raise

    def _call_once(self, tool, arguments=None, timeout=None):
        r = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments or {}}},
                       timeout=timeout)
        if "error" in r:
            raise MCPError(str(r["error"]))
        res = r.get("result", {})
        content = res.get("content")
        if content:
            # 注意判的是 content 是否存在，**不是 text 是否为空**：
            # `download` 读空文件、读越界 offset 时合法返回空字符串，
            # 按 `if not text` 兜底会把 "" 变成一坨 JSON，调用方拿去
            # base64 解码就是垃圾数据——这类错误极难从现象反推。
            text = "\n".join(c.get("text", "") for c in content)
        else:
            # 没有 content 字段（resources / prompts 之类）：保留完整结构便于排查
            text = json.dumps(res, ensure_ascii=False, indent=2)
        return text, bool(res.get("isError"))


READ_CAP = 2 * 1024 * 1024      # 单次响应上限 2MB：防对端/中间人灌爆内存


def _read_capped(resp, limit=READ_CAP):
    """有界读取响应体。超过 limit 直接抛 MCPError，而不是把内存吃光。

    一个失控的节点（或隧道里被注入的响应）可以无上限地吐数据；
    主控侧只做诊断，没有理由接受 2MB 以上的 JSON。
    """
    cl = resp.headers.get("Content-Length")
    if cl:
        try:
            if int(cl) > limit:
                raise MCPError("响应体 %s 字节，超过上限 %d 字节（已拒绝读取）"
                               % (cl, limit))
        except ValueError:
            pass
    chunks, total = [], 0
    while True:
        buf = resp.read(65536)
        if not buf:
            break
        total += len(buf)
        if total > limit:
            raise MCPError("响应体超过上限 %d 字节（已中止读取）" % limit)
        chunks.append(buf)
    return b"".join(chunks)


def http_probe(base_url, timeout=8, token="", retries=3):
    """GET 副端根路径，拿 {status, server, version, tools, ...}。

    隧道刚起来 / 网络抖动时首次请求可能超时，默认重试 3 次（指数退避
    1s / 2s / 4s），都失败才抛异常。retries=0 可关闭重试。

    401/403 不重试：令牌错了重试一百次还是错的，只会让 `fleet doctor`
    白等 7 秒，还会把「鉴权问题」伪装成「网络问题」。
    """
    base = base_url[:-4] if base_url.endswith("/mcp") else base_url.rstrip("/")
    req = urllib.request.Request(base + "/", headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    last_err = None
    for attempt in range(max(1, retries + 1)):
        try:
            with _opener_for(base_url).open(req, timeout=timeout) as r:
                return json.loads(_read_capped(r).decode("utf-8", "ignore") or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise MCPError("HTTP %d %s（鉴权失败，不重试）" % (e.code, e.reason))
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s ...
        except MCPError:
            raise                     # 超限是确定性的，重试无意义
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s ...
    raise last_err


def normalize_url(host_or_url, port=3100):
    """把用户输入（裸 IP / 主机名 / 完整 URL）规整成 http(s)://host:port/mcp。

    S9：裸 IPv6 字面量必须加方括号，否则拼出的 URL 非法（冒号歧义）——
    `fd00::1` 会被解析成 host=fd00、port 解析失败。IPv6 在家庭局域网
    （ULA fd00::/8）和 tailnet（fd7a:115c:a1e0::/48）里都很常见。
    """
    s = (host_or_url or "").strip()
    if not s:
        raise ValueError("空的地址")
    if not s.startswith(("http://", "https://")):
        host = s
        # 已经带端口的 IPv6 写作 [addr]:port，不再补端口
        if host.startswith("[") and "]:" in host:
            s = "http://%s" % host
        elif host.startswith("["):
            s = "http://%s:%d" % (host, port)
        else:
            try:
                import ipaddress
                if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                    s = "http://[%s]:%d" % (host, port)
                else:
                    s = "http://%s:%d" % (host, port)
            except ValueError:
                s = "http://%s:%d" % (host, port)
    s = s.rstrip("/")
    if not s.endswith("/mcp"):
        s += "/mcp"
    return s


def base_of(url):
    return url[:-4] if url.endswith("/mcp") else url.rstrip("/")


# ------------------------------------------------ 地址采信前的防投毒校验
def host_of(url):
    """从 http(s)://host:port/mcp 取 host（小写、不含端口）。失败返回 ""。"""
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def reg_suffix(host):
    """取注册域（末两段）：a.b.trycloudflare.com -> trycloudflare.com。

    IP 地址原样返回（本来就精确到主机）。
    """
    h = (host or "").lower()
    if not h:
        return ""
    if re.match(r"^[\d.]+$", h) or ":" in h:
        return h
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def domain_ok(cur_url, new_url):
    """新地址的域名是否「配得上」旧地址 —— 防地址目录投毒。

    同 host 直接通过；换了 host 但注册域相同也算通过（quick tunnel 重建是常态：
    xxx.trycloudflare.com → yyy.trycloudflare.com）。注册域都变了
    （例如跳到 evil.example.com）则拒绝，需人工确认。
    设 FLEET_ALLOW_DOMAIN_CHANGE=1 可显式放行。
    """
    ch, nh = host_of(cur_url), host_of(new_url)
    if not ch or not nh:
        return True
    if ch == nh:
        return True
    same = reg_suffix(ch) == reg_suffix(nh)
    if same:
        return True
    return os.environ.get("FLEET_ALLOW_DOMAIN_CHANGE") == "1"


def probe_anonymous(url, timeout=6):
    """**不带任何凭据**探活：True 表示对面看起来确实是个 MCP 副端。

    存在的意义是「先确认对面像副端，再把令牌发出去」—— 原实现把节点令牌
    直接发给目录给的新地址，投毒者只要回一个 {"status":"ok"} 就能收走令牌。
    这里连 401 都算通过：那恰恰说明对面是个开了鉴权的副端。
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "fleet-probe", "version": VERSION}}}
    try:
        req = urllib.request.Request(
            url, method="POST", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(8192).decode("utf-8", "ignore")
        return ("jsonrpc" in body) or ("serverInfo" in body)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            return False
        try:
            detail = e.read(1024).decode("utf-8", "ignore")
        except Exception:
            detail = ""
        # 副端 401 响应体里有辨识度很高的提示语
        return ("auth_token" in detail) or ("Bearer" in detail) or ("mcp" in detail.lower())
    except Exception:
        return False


def parse_exec(text):
    """副端 exec 返回格式：[exit N]\\n<stdout>[\\n--- stderr ---\\n<err>]
    返回 (exit_code, stdout, stderr)。"""
    m = re.match(r"\s*\[exit (-?\d+)\]\s*\n?", text or "")
    code = int(m.group(1)) if m else 0
    body = text[m.end():] if m else (text or "")
    out, _, errtxt = body.partition("\n--- stderr ---\n")
    return code, out.strip(), errtxt.strip()


# ---------------------------------------------------------------- 配置发现

def _appdata(*parts):
    root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(root, *parts)


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def agent_config_paths():
    """返回 {agent名: [候选 mcp 配置路径...]}。存在的排前面。"""
    home = os.path.expanduser("~")
    cand = {
        "workbuddy": [_home(".workbuddy", "mcp.json"),
                      _home(".workbuddy-ai", "mcp.json")],
        "claude": [_appdata("Claude", "claude_desktop_config.json"),
                   _home("Library", "Application Support", "Claude", "claude_desktop_config.json"),
                   _home(".claude.json"),
                   _home(".claude", "mcp.json")],
        "cursor": [_home(".cursor", "mcp.json")],
        "windsurf": [_home(".codeium", "windsurf", "mcp_config.json")],
        "vscode": [_home(".vscode", "mcp.json"),
                   _appdata("Code", "User", "mcp.json")],
        "cline": [_appdata("Code", "User", "globalStorage", "saoudrizwan.claude-dev",
                           "settings", "cline_mcp_settings.json"),
                  _home("Library", "Application Support", "Code", "User", "globalStorage",
                        "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json")],
        "trae": [_home(".trae", "mcp.json")],
    }
    out = {}
    for k, paths in cand.items():
        exist = [p for p in paths if os.path.isfile(p)]
        out[k] = exist + [p for p in paths if p not in exist]
    _ = home
    return out


class RegistryCorruptError(Exception):
    """登记表已损坏：拒绝用空表覆盖（否则一次误写就把所有节点抹掉）。"""


def _read_registry_file(path):
    """读一个登记表文件，返回 dict 或抛异常（不吞错，由调用方决定降级）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("登记表根节点不是对象")
    return data


def load_registry():
    """本地登记表：即使 agent 不支持 MCP，也能用本脚本操控副端。

    损坏时先回退到 `.bak`（上一份好数据），再不行才返回空表；
    空表只用于「读」，写路径由 save_registry 拒绝覆盖损坏文件。
    """
    if os.path.isfile(REGISTRY):
        try:
            return _read_registry_file(REGISTRY)
        except Exception:
            pass
        try:
            reg = _read_registry_file(REGISTRY + ".bak")
            sys.stderr.write(
                "⚠️  登记表 %s 已损坏，已自动回退到备份 %s.bak\n"
                "   修复后建议执行：fleet doctor\n" % (REGISTRY, REGISTRY))
            return reg
        except Exception:
            pass
    return {"nodes": {}}


def save_registry(reg):
    """原子写登记表：临时文件 + fsync + os.replace，并保留一份 .bak。

    写入前先确认现有文件可解析：登记表已损坏时**拒绝覆盖**（抛
    RegistryCorruptError），否则一次「读到空表→写回」就把所有节点抹掉了。
    """
    d = os.path.dirname(REGISTRY)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.isfile(REGISTRY):
        try:
            _read_registry_file(REGISTRY)
        except Exception:
            raise RegistryCorruptError(
                "登记表 %s 已损坏，拒绝覆盖。\n"
                "  1) 先备份：cp %s %s.broken\n"
                "  2) 再修复或删除它，然后重新 add/pair\n"
                "  3) 也可以试 .bak：%s.bak"
                % (REGISTRY, REGISTRY, REGISTRY, REGISTRY))
        try:                          # 轮转上一份好数据
            with open(REGISTRY, encoding="utf-8") as src:
                old = src.read()
            with open(REGISTRY + ".bak", "w", encoding="utf-8") as dst:
                dst.write(old)
            os.chmod(REGISTRY + ".bak", 0o600)
        except Exception:
            pass
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    # 登记表里有各副端的明文 Bearer 令牌，必须收紧权限；
    # 原来既不 chmod 也不设 umask，Linux 上默认 0644 = 同机任何用户可读。
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, REGISTRY)


def mask_token(tok):
    """令牌掩码：日志/JSON 输出默认不暴露完整令牌。

    保留前 4 后 4 便于人工核对是哪一个；太短的令牌整段打码。
    """
    t = tok or ""
    if not t:
        return ""
    if len(t) <= 8:
        return "***"
    return t[:4] + "…" + t[-4:]


def load_json_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_config(path, cfg):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def servers_key(cfg):
    """不同 agent 的顶层 key 不一样。"""
    for k in ("mcpServers", "servers", "mcp"):
        if isinstance(cfg.get(k), dict):
            return k
    return "mcpServers"


def discover_nodes(agent=None):
    """汇总所有 agent 配置 + 本地登记表 → {name: {"url","agent","config","token"...}}"""
    found = {}
    for ag, paths in agent_config_paths().items():
        if agent and agent != "auto" and ag != agent:
            continue
        for p in paths:
            if not os.path.isfile(p):
                continue
            try:
                cfg = load_json_config(p)
            except Exception:
                continue
            for name, v in (cfg.get(servers_key(cfg)) or {}).items():
                if not isinstance(v, dict):
                    continue
                url = v.get("url")
                if url and str(url).startswith(("http://", "https://")):
                    found.setdefault(name, {"url": url, "agent": ag, "config": p,
                                            "description": v.get("description", ""),
                                            "token": _token_from_headers(v)})
            break                     # 每个 agent 只取第一个存在的配置
    for name, v in (load_registry().get("nodes") or {}).items():
        found.setdefault(name, {"url": v.get("url"), "agent": "registry",
                                "config": REGISTRY,
                                "description": v.get("description", ""),
                                "token": v.get("token", "")})
    return found


def _token_from_headers(entry):
    """从 mcp.json 的 headers 里把 token 抠出来（Bearer / X-Fleet-Token）。"""
    h = entry.get("headers") or {}
    if not isinstance(h, dict):
        return ""
    for k, v in h.items():
        kl = str(k).lower()
        if kl == "x-fleet-token":
            return str(v).strip()
        if kl == "authorization" and str(v).lower().startswith("bearer "):
            return str(v)[7:].strip()
    return ""


IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$")
HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*(\.[A-Za-z0-9\-]+)+(:\d+)?$")


# ------------------------------------------------- 地址目录（隧道地址自动跟随）
# 副端用 cloudflared 快隧时地址会变（重启/重建就换域名）。副端保活守护会把当前地址
# 上报到「地址目录」（fleet-directory.py），这里负责：地址不通时问一次目录、自动换成新地址。
# 关掉：--no-sync，或 FLEET_NO_SYNC=1。

AUTO_SYNC = True


def registry_directory():
    """登记表里的地址目录配置，环境变量优先。返回 (url, token)。"""
    d = load_registry().get("directory") or {}
    url = os.environ.get("FLEET_DIRECTORY_URL") or d.get("url") or ""
    tok = os.environ.get("FLEET_DIRECTORY_TOKEN") or d.get("token") or ""
    return url.rstrip("/"), tok


def fetch_directory(url=None, token=None, timeout=6):
    """GET <目录>/nodes → {name: {...}}。失败抛异常，由调用方决定怎么处理。"""
    if url is None:
        url, token = registry_directory()
    if not url:
        raise RuntimeError("未配置地址目录。设一个：fleet.py directory --url http://<主端>:8790 "
                           "（或环境变量 FLEET_DIRECTORY_URL）")
    req = urllib.request.Request(url + "/nodes", method="GET")
    if token:
        req.add_header("X-Fleet-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore") or "{}")
    return data.get("nodes") or {}


def sync_from_directory(name, cur_url=None, timeout=8, quiet=False, verbose=True, token=""):
    """地址不通时问一次目录并回写登记表。返回新 url；没变化/拿不到返回 None。

    注意：这里必须自己吞掉网络异常 —— 探测一个死地址会抛 URLError，
    如果让它冒出去，调用方看到的就是「连不上」，而不是「自动纠正后成功了」。
    """
    durl, dtok = registry_directory()
    if not durl:
        return None
    if cur_url:
        try:
            if http_probe(cur_url, timeout=min(timeout, 5), token=token):
                return None         # 还通着就别折腾
        except Exception:
            pass                    # 不通 → 继续去问目录
    try:
        nodes = fetch_directory(durl, dtok, timeout)
    except Exception as e:
        if not quiet:
            print(warn("地址目录查询失败：%s" % e), file=sys.stderr)
        return None
    v = nodes.get(name) or {}
    new = (v.get("url") or "").strip().rstrip("/")
    if not new:
        if not quiet:
            print(warn("地址目录里没有 %s 的记录" % name), file=sys.stderr)
        return None
    new_mcp = normalize_url(new)
    if new_mcp == cur_url:
        return None
    # ---- 防投毒闸门 1：域名必须「配得上」旧地址 ----
    # 否则投毒者 POST /register 把 name 指向自己的地址，主端就会把该副端的
    # Bearer 令牌送过去，并把假地址永久写进登记表。
    if not domain_ok(cur_url, new_mcp):
        if not quiet:
            print(warn("目录给的地址换了域名：%s → %s"
                       % (host_of(cur_url) or "(空)", host_of(new_mcp))), file=sys.stderr)
            print(dim("    出于防投毒考虑不自动采信（否则会把副端令牌发给未知地址）。"),
                  file=sys.stderr)
            print(dim("    确认无误后手工改：fleet.py add %s %s --token <原令牌>"
                      % (name, new_mcp)), file=sys.stderr)
        return None
    # ---- 防投毒闸门 2：先不带凭据确认对面像副端，令牌最后才发出 ----
    if not probe_anonymous(new_mcp, timeout=min(timeout, 6)):
        if not quiet:
            print(warn("目录给的 %s 看起来不是 MCP 副端（无凭据探活未通过）" % new),
                  file=sys.stderr)
        return None
    # ---- 防投毒闸门 3：匿名探活返回的节点身份必须就是期望节点 ----
    # 闸门 2 只证明「对面是个 MCP 服务」，不证明「对面是这台设备」。少了这一道，
    # 投毒的目录条目就能把真令牌骗到冒名地址。拿不到身份一律拒绝（fail closed）。
    try:
        anon = http_probe(new_mcp, timeout=min(timeout, 6), token="", retries=0)
    except Exception:
        anon = {}
    if (anon or {}).get("server") != name:
        if not quiet:
            print(warn("目录给的 %s 节点身份不符（期望 %s，实测 %s），拒绝采信"
                       % (new_mcp, name, (anon or {}).get("server") or "(无)")),
                  file=sys.stderr)
        return None
    try:
        if not http_probe(new_mcp, timeout=min(timeout, 6), token=token):
            raise RuntimeError("无响应")
    except Exception:
        if not quiet:
            print(warn("目录给的 %s 也不通（副端可能正忙着重连）" % new), file=sys.stderr)
        return None
    reg = load_registry()
    node = (reg.get("nodes") or {}).get(name)
    if node is not None:
        old = node.get("url")
        node["url"] = new_mcp
        node["url_prev"] = old
        node["synced_at"] = int(time.time())
        save_registry(reg)
    if verbose:
        print(ok("地址已自动更新：%s → %s" % (cur_url or "(空)", new_mcp)), file=sys.stderr)
    return new_mcp


def resolve_target(target, agent=None, port=3100, sync=None):
    """名字 → url；也接受直接给地址（URL / IPv4 / 带点的主机名）。返回 (url, meta)。"""
    t = (target or "").strip()
    if not t:
        raise ValueError("缺少 target")
    nodes = discover_nodes(agent)
    want_sync = AUTO_SYNC if sync is None else sync
    if t in nodes:
        m = nodes[t]
        url = m["url"]
        if want_sync and url:
            new = sync_from_directory(t, url, token=m.get("token", ""))
            if new:
                url = new
                m = dict(m, url=new)
        return url, m
    for n, m in nodes.items():        # 允许 http://... 直接当 key 写进过配置
        if m["url"] == t:
            return m["url"], m
    if t.startswith(("http://", "https://")) or IPV4.match(t) or HOSTNAME.match(t):
        return normalize_url(t, port), {"agent": "direct", "config": "", "token": ""}
    raise KeyError("找不到副端 %r。已注册的：%s\n（先跑 `fleet.py list`；"
                   "要直接给地址请写 http://ip:端口/mcp）"
                   % (t, ", ".join(sorted(nodes)) or "无"))


def client_for(target, agent=None, port=3100, timeout=30, sync=None):
    """一步到位：target → 带鉴权的 MCPClient。返回 (client, url, meta)。"""
    url, meta = resolve_target(target, agent, port, sync=sync)
    return MCPClient(url, timeout, token=meta.get("token", "")), url, meta


# ---------------------------------------------------------------- 命令实现

def cmd_list(a):
    nodes = discover_nodes(a.agent)
    if a.json:
        # 默认掩码令牌：list --json 的输出经常被贴进 issue / 日志 / 截图。
        # 需要真值（脚本消费）显式 --show-tokens。
        if not getattr(a, "show_tokens", False):
            nodes = dict((k, dict(v, token=mask_token(v.get("token", ""))))
                         for k, v in nodes.items())
        print(json.dumps(nodes, ensure_ascii=False, indent=2))
        return 0
    if not nodes:
        print(warn("没有发现任何副端。"))
        print("  装一台：  python fleet.py install-cmd --name pi")
        print("  再注册：  python fleet.py add --name pi --ip 192.168.1.9 --port 3100")
        return 0
    print("已注册副端（%d 个）：\n" % len(nodes))
    w = max(len(n) for n in nodes)
    for name in sorted(nodes):
        m = nodes[name]
        tag = "登记表" if m["agent"] == "registry" else m["agent"]
        lock = " 🔒" if m.get("token") else ""
        print("  %-*s  %-38s  [%s]%s" % (w, name, m["url"], tag, lock))
        if m.get("config"):
            print("  %-*s  %s" % (w, "", dim(m["config"])))
    print()
    print(dim("提示：agent 侧的 MCP 配置改完必须重启 agent 才生效；"
              "本脚本（exec/call）改完立即可用。"))
    if any(not n.get("token") for n in nodes.values()):
        print(dim("      🔒 = 已配访问令牌；缺令牌的副端若开了鉴权会 401，"
                  "用 add --token 补上。"))
    return 0


def cmd_targets(a):
    nodes = discover_nodes(a.agent)
    for name in sorted(nodes):
        print(name)
    return 0


def cmd_tools(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    c = MCPClient(url, a.timeout, token=meta.get("token", ""))
    c.handshake()
    tools = c.list_tools()
    if a.json:
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return 0
    print("%s  →  %s v%s，%d 个工具\n" % (a.target, c.info.get("name"), c.info.get("version"), len(tools)))
    for t in tools:
        req = (t.get("inputSchema") or {}).get("required") or []
        print("  %-16s %s" % (t["name"], (t.get("description") or "").split("\n")[0]))
        if req:
            print("  %-16s %s" % ("", dim("必填: " + ", ".join(req))))
    return 0


def cmd_probe(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    print("目标：%s" % url)
    t0 = time.time()
    try:
        info = http_probe(url, token=meta.get("token", ""))
    except Exception as e:
        return die("GET / 失败：%r\n    排查：确认副端在跑、端口放行、主端网络可达。" % (e,))
    print(ok("[+] ") + "HTTP 可达（%.0f ms）  server=%s v%s，工具 %s 个"
          % ((time.time() - t0) * 1000, info.get("server"), info.get("version"), info.get("tools")))
    c = MCPClient(url, a.timeout, token=meta.get("token", ""))
    try:
        c.handshake()
        tools = c.list_tools()
    except MCPError as e:
        return die("MCP 握手/列工具失败：%s" % e)
    print(ok("[+] ") + "MCP 握手成功，tools/list 返回 %d 个：%s"
          % (len(tools), ", ".join(t["name"] for t in tools[:6]) + (" ..." if len(tools) > 6 else "")))
    try:
        text, iserr = c.call("exec", {"command": "hostname; uname -srm"}, timeout=a.timeout)
        code, out, _ = parse_exec(text)
        print((err if (iserr or code) else ok)("[%s] " % ("!" if (iserr or code) else "+"))
              + "exec 试调：exit=%d  %s" % (code, out.replace("\n", " / ")[:150]))
        if iserr or code:
            print(dim("      probe 判定失败：HTTP 通了但命令没跑通，"
                      "通常是执行策略拦截或副端权限/工作目录问题。"))
            return 1
    except MCPError as e:
        print(warn("[!] ") + "exec 试调失败：%s" % e)
        return 1
    return 0


def cmd_shell(a):
    """v5.3: 交互式远程 shell（PTY）——这是「替代 ssh」的核心。

    为什么不能拿 exec 拼：ssh 能跑 vim/top/less，是因为远端 stdin 是真终端。
    exec 每次都是新的非交互进程，`tty` 会告诉你 "not a tty"，全屏程序直接罢工。
    所以远端用 pty.fork 起一个登录 shell，这边把终端设成 raw 模式，
    按键原样送过去、输出原样刷出来，中间的换行/信号/窗口大小都由远端处理。

    传输仍然是请求/响应，所以读用长轮询（wait=0.2s）：一次请求最多等 200ms，
    有数据立刻返回。这样既不会 50ms 打一次空转，也不会让按键延迟明显。
    """
    if os.name == "nt":
        return die("Windows 暂不支持交互式 shell；请用 fleet exec / exec_async")

    try:
        import termios
        import tty
        import fcntl
        import struct
    except ImportError:
        return die("当前系统缺少 termios，无法进入交互式 shell")

    sync = False if getattr(a, "no_sync", False) else None
    try:
        url, meta = resolve_target(a.target, a.agent, a.port, sync=sync)
    except Exception as e:
        return die("连不上 %s：%s" % (a.target, e))

    client = MCPClient(url, timeout=max(30, a.timeout), token=meta.get("token", ""),
                       insecure=getattr(a, "insecure", False))
    # 读和写各用一条连接：读线程会阻塞在长轮询上（最多 200ms），
    # 共用一条连接的话按键就得排队等它回来，手感立刻变粘。
    wclient = MCPClient(url, timeout=max(30, a.timeout), token=meta.get("token", ""),
                        insecure=getattr(a, "insecure", False))

    def winsize():
        try:
            sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols = struct.unpack("HHHH", sz)[:2]
            return max(20, cols), max(5, rows)
        except Exception:
            return 80, 24

    cols, rows = winsize()
    try:
        text, is_err = client.call("shell_open", {
            "cols": cols, "rows": rows,
            "term": os.environ.get("TERM") or "xterm-256color",
            "workDir": getattr(a, "cwd", "") or ""}, timeout=30)
    except MCPError as e:
        return die("打开会话失败：%s" % e)
    if is_err:
        print(err("[x] ") + text)
        return 1
    try:
        sid = json.loads(text)["session_id"]
    except Exception:
        return die("副端返回异常：%s" % text[:200])

    print(dim("已连接 %s（%s）· 退出：Ctrl-D 或 exit" % (a.target, url)))
    print(dim("─" * 60))

    stdin_fd = sys.stdin.fileno()
    out = sys.stdout.buffer
    stop = threading.Event()
    offset = [0]
    state = {"alive": True}

    def pump():
        """把远端输出刷到本地终端。"""
        while not stop.is_set():
            try:
                t, e = client.call("shell_read", {"session_id": sid,
                                                  "offset": offset[0], "wait": 0.2},
                                   timeout=30)
            except MCPError:
                break
            if e:
                break
            try:
                d = json.loads(t)
            except Exception:
                break
            offset[0] = d.get("offset", offset[0])
            chunk = d.get("data_b64") or ""
            if chunk:
                try:
                    out.write(base64.b64decode(chunk))
                    out.flush()
                except Exception:
                    pass
            if not d.get("alive", True):
                state["alive"] = False
                stop.set()
                break

    def on_resize(_signum, _frame):
        c, r = winsize()
        try:
            wclient.call("shell_resize", {"session_id": sid, "cols": c, "rows": r},
                         timeout=10)
        except Exception:
            pass

    old_winch = None
    try:
        old_winch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, on_resize)
    except Exception:
        pass

    reader = threading.Thread(target=pump, daemon=True)
    old_attrs = None
    try:
        old_attrs = termios.tcgetattr(stdin_fd)
    except Exception as e:
        # stdin 不是终端（管道、/dev/null、CI）：raw 模式没法开，但会话已经建好了，
        # 必须在这里关掉，否则远端会留一个孤儿 shell。
        try:
            wclient.call("shell_close", {"session_id": sid}, timeout=10)
        except Exception:
            pass
        client.close()
        wclient.close()
        return die("当前 stdin 不是终端（%s），交互式 shell 需要真终端。"
                   "非交互场景请用：fleet exec %s \"<命令>\"" % (e, a.target))
    try:
        reader.start()
        tty.setraw(stdin_fd)
        import select as _select
        while state["alive"]:
            # 不能直接阻塞在 os.read(stdin)：远端 `exit` 之后本地没人按键盘，
            # 这里会一直卡着，用户以为连接没断。用 select 带超时轮询，
            # 每 0.2 秒检查一次远端会话是否还活着。
            r, _, _ = _select.select([stdin_fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(stdin_fd, 4096)
            except OSError:
                break
            if not data:
                break
            try:
                wclient.call("shell_write", {
                    "session_id": sid,
                    "data_b64": base64.b64encode(data).decode("ascii")}, timeout=30)
            except MCPError:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass
        if old_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, old_winch)
            except Exception:
                pass
        try:
            wclient.call("shell_close", {"session_id": sid}, timeout=10)
        except Exception:
            pass
        client.close()
        wclient.close()
        print()
        print(dim("会话已结束。"))
    return 0


# ---------------------------------------------------------------------------
# 端口转发（v5.4）：ssh -L / -R 的等价物
# ---------------------------------------------------------------------------
def _parse_fwd(spec, default_bind, is_local):
    """解析转发规格。

      -L 8080:db.internal:3306          本地 8080 → 副端视角的 db.internal:3306
      -L 127.0.0.1:8080:db.internal:3306  同上，但只绑回环
      -R 9000:127.0.0.1:3000            副端 9000 → 本地 3000

    返回 {bind, port, host, hport}；host/hport 永远是「另一端要连的目标」。
    """
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("转发规格为空")
    parts = raw.split(":")
    if len(parts) == 3:
        bind, port, host, hport = default_bind, parts[0], parts[1], parts[2]
    elif len(parts) == 4:
        bind, port, host, hport = parts
    elif raw.count(":") > 4:
        # ::1 / 2001:db8::1 这类地址一拆就散了，先说清楚不支持，别让人对着
        # 「看不懂的转发规格」自己猜
        raise ValueError(
            "绑定地址不支持 IPv6 字面量（%s）。\n"
            "     改用 IPv4：%s 127.0.0.1:<端口>:<目标主机>:<目标端口>"
            % (raw, "-L" if is_local else "-R"))
    else:
        raise ValueError(
            "看不懂的转发规格：%s\n"
            "     正确写法：%s <端口>:<目标主机>:<目标端口>\n"
            "     或        %s <绑定地址>:<端口>:<目标主机>:<目标端口>"
            % (raw, "-L" if is_local else "-R", "-L" if is_local else "-R"))
    if ":" in bind:
        raise ValueError("绑定地址不支持 IPv6 字面量（%s），请用 IPv4 或留空" % bind)
    try:
        port = int(port)
    except Exception:
        raise ValueError("端口不是数字：%s" % parts[1])
    if not (0 < port < 65536):
        raise ValueError("端口超出范围（1-65535）：%d" % port)
    if not host:
        host = "127.0.0.1"
    try:
        hport = int(hport)
    except Exception:
        raise ValueError("目标端口不是数字：%s" % parts[-1])
    if not (0 < hport < 65536):
        raise ValueError("目标端口超出范围（1-65535）：%d" % hport)
    return {"bind": bind or default_bind, "port": port, "host": host,
            "hport": hport, "spec": raw}


def _fwd_label(f):
    return "%s:%d → %s:%d" % (f["bind"], f["port"], f["host"], f["hport"])


def _pump_remote_to_sock(client, tid, sock, gstop, lst, stats):
    """把隧道里的字节刷到本地 socket。

    gstop 是整个转发器的总开关（Ctrl-C），lst 是**这一条连接**的开关。
    两者必须分开：一条连接正常结束就把 gstop 置位的话，整个转发器会跟着退出。
    """
    offset = 0
    while not gstop.is_set() and not lst.is_set():
        try:
            text, is_err = client.call(
                "tunnel_read", {"tunnel_id": tid, "offset": offset, "wait": 0.2},
                timeout=30)
        except MCPError:
            break
        if is_err:
            break
        try:
            d = json.loads(text)
        except Exception:
            break
        offset = d.get("offset", offset)
        chunk = d.get("data_b64") or ""
        if chunk:
            try:
                raw = base64.b64decode(chunk)
                sock.sendall(raw)
                stats["rx"] += len(raw)
            except Exception:
                break
        if not d.get("alive", True):
            break
    lst.set()
    try:
        sock.shutdown(socket.SHUT_WR)
    except Exception:
        pass


def _pump_sock_to_remote(client, tid, sock, gstop, lst, stats):
    """把本地 socket 收到的字节写进隧道。"""
    while not gstop.is_set() and not lst.is_set():
        try:
            data = sock.recv(65536)
        except Exception:
            break
        if not data:
            break
        try:
            _, is_err = client.call(
                "tunnel_write", {"tunnel_id": tid,
                                 "data_b64": base64.b64encode(data).decode("ascii")},
                timeout=30)
        except MCPError:
            break
        if is_err:
            break
        stats["tx"] += len(data)
    lst.set()
    try:
        sock.shutdown(socket.SHUT_RD)
    except Exception:
        pass


def cmd_forward(a):
    """v5.4: 端口转发 —— 没有它，「替代 ssh」还是半句话。

    ssh 除了给终端，还给 -L / -R：远端内网里只有那台机器能碰的数据库、面板、
    私有 API，全靠它接出来。fleet 之前的工具都是「一次请求一次响应」，
    拿不到一条能持续双向流动的连接，所以这块一直是缺的。

    实现上仍然是请求/响应，只是把「持续」拆成了「按需拉增量」：
    副端持有 socket 并缓冲最近 512KB，主端长轮询（wait=0.2s）把新增字节拉回来。
    代价是每次往返多一个 RTT 的延迟，好处是不需要任何新协议、任何端口映射。
    """
    if os.name == "nt":
        return die("Windows 暂不支持端口转发（缺 select/信号支持），请在 macOS/Linux 上用")

    sync = False if getattr(a, "no_sync", False) else None
    try:
        url, meta = resolve_target(a.target, a.agent, a.port, sync=sync)
    except Exception as e:
        return die("连不上 %s：%s" % (a.target, e))

    token = meta.get("token", "")
    insecure = getattr(a, "insecure", False)

    if getattr(a, "list", False):
        c = MCPClient(url, timeout=30, token=token, insecure=insecure)
        try:
            text, is_err = c.call("tunnel_list", {}, timeout=30)
        except MCPError as e:
            return die("查询失败：%s" % e)
        finally:
            c.close()
        print(text if not is_err else err(text))
        return 0 if not is_err else 1

    default_bind = getattr(a, "bind", "") or "127.0.0.1"
    locals_, remotes = [], []
    try:
        for spec in (getattr(a, "local", None) or []):
            locals_.append(_parse_fwd(spec, default_bind, True))
        for spec in (getattr(a, "remote", None) or []):
            remotes.append(_parse_fwd(spec, "127.0.0.1", False))
    except ValueError as e:
        return die(str(e))
    if not locals_ and not remotes:
        return die("至少要给一个 -L 或 -R。例：\n"
                   "     fleet forward -L 8080:127.0.0.1:80 %s\n"
                   "     fleet forward -R 9000:127.0.0.1:3000 %s"
                   % (a.target, a.target))

    stop = threading.Event()
    listeners = []        # 本地监听 socket（-L）
    remote_ls = []        # 副端 listener 的 tunnel_id（-R）
    clients = set()       # 活着的客户端；关掉就摘掉，不然长跑必然涨内存
    lock = threading.Lock()
    stats = {"conn": 0, "tx": 0, "rx": 0}

    def _mk_clients():
        """一条转发连接配两条 HTTP 通道（读/写各一）。"""
        rc = MCPClient(url, timeout=30, token=token, insecure=insecure)
        wc = MCPClient(url, timeout=30, token=token, insecure=insecure)
        with lock:
            clients.add(rc)
            clients.add(wc)
        return rc, wc

    def _drop_clients(rc, wc):
        """关掉并**从清单里摘掉**。

        只在退出时统一关是不够的：连接被拒/开隧道失败也要还回去，
        否则每失败一次就漏两条 HTTP 连接，转发器跑一天就废了。
        """
        with lock:
            clients.discard(rc)
            clients.discard(wc)
        for c in (rc, wc):
            try:
                c.close()
            except Exception:
                pass

    def _pipe(tid, sock, rc, wc, label):
        """一条连接的双向泵：跑完就收尾。"""
        st = {"tx": 0, "rx": 0}
        lst = threading.Event()
        t1 = threading.Thread(target=_pump_remote_to_sock,
                              args=(rc, tid, sock, stop, lst, st), daemon=True)
        t2 = threading.Thread(target=_pump_sock_to_remote,
                              args=(wc, tid, sock, stop, lst, st), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        # 远端先结束：叫醒可能还卡在 recv 上的另一半，别把线程留在那
        try:
            sock.shutdown(socket.SHUT_RD)
        except Exception:
            pass
        t2.join(timeout=3)
        try:
            wc.call("tunnel_close", {"tunnel_id": tid}, timeout=10)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        _drop_clients(rc, wc)
        with lock:
            stats["tx"] += st["tx"]
            stats["rx"] += st["rx"]
        print(dim("  - %s 关闭（↑%d ↓%d 字节）" % (label, st["tx"], st["rx"])))

    # ---- -L：本地监听，每条进来的连接开一条隧道 ----
    def serve_local(f):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ls.bind((f["bind"], f["port"]))
            ls.listen(32)
        except Exception as e:
            ls.close()
            print(err("[x] ") + "绑定 %s:%d 失败：%s" % (f["bind"], f["port"], e))
            return None
        real = ls.getsockname()[1]
        print(ok("[L] ") + "%s:%d → %s（经 %s）:%d"
              % (f["bind"], real, f["host"], a.target, f["hport"]))

        def handle_local(conn, peer, f):
            label = "%s:%d" % (peer[0], peer[1])
            rc, wc = _mk_clients()
            with lock:
                stats["conn"] += 1
            try:
                text, is_err = rc.call("tunnel_open",
                                       {"host": f["host"], "port": f["hport"]},
                                       timeout=40)
            except MCPError as e:
                text, is_err = str(e), True
            if is_err:
                print(err("[x] ") + "%s 连不上 %s:%d：%s"
                      % (label, f["host"], f["hport"], text))
                try:
                    conn.close()
                except Exception:
                    pass
                _drop_clients(rc, wc)
                return
            try:
                tid = json.loads(text)["tunnel_id"]
            except Exception:
                print(err("[x] ") + "副端返回异常：%s" % str(text)[:120])
                try:
                    conn.close()
                except Exception:
                    pass
                _drop_clients(rc, wc)
                return
            print(dim("  + %s ↔ %s" % (label, tid)))
            _pipe(tid, conn, rc, wc, label)

        def accept_loop():
            while not stop.is_set():
                try:
                    conn, peer = ls.accept()
                except Exception:
                    break
                threading.Thread(target=handle_local, args=(conn, peer, f),
                                 daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        return ls

    # ---- -R：副端监听，新连接接回本地 ----
    def serve_remote(f):
        ctl = MCPClient(url, timeout=30, token=token, insecure=insecure)
        with lock:
            clients.add(ctl)

        def open_listener():
            """在副端建 listener。失败返回 (None, 原因)。"""
            try:
                text, is_err = ctl.call(
                    "tunnel_listen", {"host": f["bind"], "port": f["port"]},
                    timeout=40)
            except MCPError as e:
                return None, str(e)
            if is_err:
                return None, text
            try:
                return json.loads(text)["tunnel_id"], ""
            except Exception:
                return None, "副端返回异常：%s" % str(text)[:120]

        lid, why = open_listener()
        if not lid:
            print(err("[x] ") + "副端监听 %s:%d 失败：%s" % (f["bind"], f["port"], why))
            with lock:
                clients.discard(ctl)
            try:
                ctl.close()
            except Exception:
                pass
            return None
        remote_ls.append(lid)
        print(ok("[R] ") + "副端 %s:%d → 本地 %s:%d"
              % (f["bind"], f["port"], f["host"], f["hport"]))

        def accept_loop():
            """取新连接。

            这里**不能一遇错就退出**：网络抖一下、副端重启一次，整个 -R 就
            静默死掉，用户只会觉得「转发莫名其妙不工作了」。所以分两种错处理：
              * 副端不认识这个 listener（重启后内存态没了）→ 重新建一个
              * 网络类错误 → 退避重试，连续失败才提示
            """
            cur = [lid]
            fails = 0
            while not stop.is_set():
                try:
                    text, is_err = ctl.call(
                        "tunnel_accept", {"tunnel_id": cur[0], "wait": 0.2},
                        timeout=30)
                except MCPError as e:
                    fails += 1
                    if fails == 3:
                        print(warn("[!] ") + "取连接连续失败（%s），继续重试…" % e)
                    stop.wait(min(5.0, 0.3 * fails))
                    continue
                if is_err:
                    if "未知" in str(text):
                        # 副端重启/被清理：listener 没了，重建一个
                        new, why = open_listener()
                        if new:
                            cur[0] = new
                            remote_ls[:] = [new]
                            print(ok("[R] ") + "副端 listener 已重建（%s）" % new)
                            fails = 0
                            continue
                        print(err("[x] ") + "副端 listener 重建失败：%s" % why)
                    fails += 1
                    stop.wait(min(5.0, 0.3 * fails))
                    continue
                fails = 0
                try:
                    d = json.loads(text)
                except Exception:
                    break
                for tid in (d.get("tunnels") or []):
                    threading.Thread(target=handle_remote, args=(tid, f),
                                     daemon=True).start()
                if not d.get("alive", True):
                    break

        def handle_remote(tid, f):
            try:
                conn = socket.create_connection((f["host"], f["hport"]), timeout=10)
                conn.settimeout(None)
            except Exception as e:
                print(err("[x] ") + "本地 %s:%d 连不上：%s" % (f["host"], f["hport"], e))
                try:
                    ctl.call("tunnel_close", {"tunnel_id": tid}, timeout=10)
                except Exception:
                    pass
                return
            rc, wc = _mk_clients()
            with lock:
                stats["conn"] += 1
            print(dim("  + %s ↔ 本地 %s:%d" % (tid, f["host"], f["hport"])))
            _pipe(tid, conn, rc, wc, tid)

        threading.Thread(target=accept_loop, daemon=True).start()
        return ctl

    for f in locals_:
        ls = serve_local(f)
        if ls:
            listeners.append(ls)
    for f in remotes:
        serve_remote(f)          # 成功时 ctl 已进 clients，统一收尾

    if not listeners and not remote_ls:
        return die("所有转发都没起来")

    print(dim("─" * 60))
    print(dim("转发已就绪，Ctrl-C 结束。本地可先用 lsof -i :<端口> 确认监听"))
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for ls in listeners:
            try:
                ls.close()
            except Exception:
                pass
        for lid in remote_ls:
            try:
                MCPClient(url, timeout=10, token=token,
                          insecure=insecure).call("tunnel_close",
                                                  {"tunnel_id": lid}, timeout=10)
            except Exception:
                pass
        for c in list(clients):
            try:
                c.close()
            except Exception:
                pass
        with lock:
            clients.clear()
        print()
        print(dim("已断开全部转发（累计 %d 条连接，↑%d ↓%d 字节）"
                  % (stats["conn"], stats["tx"], stats["rx"])))
    return 0


def cmd_ping(a):
    """轻探活：只 GET /，不握手、不列工具、不 exec。
    给健康看门/脚本用。握手要三次往返，探活只需要知道「HTTP 层活着」；
    分清楚这一点，看门才能在 5 分钟一轮的频率下不把节点压垮。
    退出码：0 可达 / 1 不可达（脚本可直接判）。
    """
    sync = False if getattr(a, "no_sync", False) else None
    try:
        url, meta = resolve_target(a.target, a.agent, a.port, sync=sync)
    except Exception as e:
        print(err("不可达") + "  %s：%s" % (a.target, e))
        return 1
    t0 = time.time()
    try:
        info = http_probe(url, timeout=a.timeout, token=meta.get("token", ""), retries=0)
    except Exception as e:
        msg = str(e)
        if "鉴权" in msg or "401" in msg or "403" in msg:
            print(warn("仅可达") + "  %s  HTTP 层有响应但鉴权失败（%s）" % (a.target, msg))
            print(dim("     令牌不对：fleet add --name %s --url %s --token <令牌>" % (a.target, url)))
        else:
            print(err("不可达") + "  %s：%s" % (a.target, msg))
        return 1
    ms = (time.time() - t0) * 1000
    print(ok("可达") + "  %s  %s  %.0f ms  %s v%s"
          % (a.target, url, ms, info.get("server") or "?", info.get("version") or "?"))
    if a.json:
        print(json.dumps({"target": a.target, "url": url, "reachable": True,
                          "rtt_ms": round(ms, 1), "server": info.get("server"),
                          "version": info.get("version")},
                         ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(a):
    if not getattr(a, "target", None):
        got = _local_mcp(a)
        if not got:
            return die("本机没有运行中的节点；请给 target，或先安装节点后运行 fleet doctor")
        me, client = got
        try:
            text, is_err = client.call("fleet_doctor", {}, timeout=a.timeout)
        except MCPError as e:
            return die("本机自检失败：%s" % e)
        print(text)
        return 1 if is_err else 0
    url, meta = resolve_target(a.target, a.agent, a.port)
    base = base_of(url)
    host, _, port = base.split("//", 1)[-1].partition("/")[0].partition(":")
    port = int(port or (443 if base.startswith("https") else 80))
    print("== doctor: %s ==" % a.target)
    print("  端点     : %s" % url)
    print("  来源     : %s %s" % (meta.get("agent"), dim(meta.get("config") or "")))
    fails = []

    # 1 TCP
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=a.timeout)
        s.close()
        print(ok("  [1/7] TCP ") + "%s:%d 通（%.0f ms）" % (host, port, (time.time() - t0) * 1000))
    except Exception as e:
        print(err("  [1/7] TCP ") + "%s:%d 不通：%r" % (host, port, e))
        fails.append("TCP 不通：云主机/容器要在安全组或平台侧放行该端口；内网确认 IP 与防火墙。")

    # 2 HTTP
    try:
        info = http_probe(url, token=meta.get("token", ""))
        print(ok("  [2/7] HTTP") + " 200，server=%s v%s，tools=%s，root=%s"
              % (info.get("server"), info.get("version"), info.get("tools"), info.get("root")))
    except Exception as e:
        print(err("  [2/7] HTTP") + " 失败：%r" % (e,))
        fails.append("HTTP 探活失败：副端进程可能没起。"
                     "容器里跑 `bash 副端安装目录/mcp-ctl.sh status`；"
                     "systemd 机器跑 `systemctl status <name>-mcp`。")

    # 3 MCP
    c = MCPClient(url, a.timeout, token=meta.get("token", ""))
    try:
        c.handshake()
        tools = c.list_tools()
        print(ok("  [3/7] MCP ") + "握手 OK，%d 个工具" % len(tools))
    except MCPError as e:
        print(err("  [3/7] MCP ") + "失败：%s" % e)
        fails.append("MCP 协议层失败：用 `python scripts/probe_fleet.py %s` 交叉验证；"
                     "若它通而 agent 不通，是 agent 侧配置/版本问题。" % url)
        tools = []

    # 4 真实调用
    if tools:
        try:
            text, iserr = c.call("exec", {"command": "echo MCP_E2E_OK; hostname"}, timeout=a.timeout)
            code, out, _ = parse_exec(text)
            mark = err if (iserr or code) else ok
            print(mark("  [4/7] E2E ") + "exit=%d  %s" % (code, out.replace("\n", " / ")[:110]))
            if iserr or code:
                fails.append("exec 返回非 0：看上面输出。命令超时就把 mcp_agent_config.json "
                             "里的 command_timeout 调大。")
        except MCPError as e:
            print(err("  [4/7] E2E ") + "失败：%s" % e)
            fails.append("工具调用失败：检查副端日志（mcp-ctl.sh log 50 / journalctl -u %s-mcp）。" % a.target)

        # 5 环境
        inst = a.dir or "/opt/%s_mcp" % a.target
        try:
            text, _ = c.call("exec", {"command": "id -u; command -v docker || echo no-docker"},
                             timeout=a.timeout)
            _, out, _ = parse_exec(text)
            lines = [x.strip() for x in out.splitlines() if x.strip()]
            uid = lines[0] if lines else "?"
            docker = lines[1] if len(lines) > 1 else "?"
            print(ok("  [5/7] 环境") + " uid=%s，docker=%s" % (uid, docker))
            if uid != "0":
                fails.append("副端不是 root 运行：sudo 类工具会失败。配置里填 sudo_password，"
                             "或用 root 重装。")
            if docker == "no-docker":
                fails.append("副设备没有 docker：把配置里的 enable_docker 设为 false，"
                             "否则 docker 类工具会一直报错。")
        except MCPError:
            pass

        # 6 配置（用 read 工具直读副端配置）
        try:
            raw, _ = c.call("read", {"filePath": inst + "/mcp_agent_config.json"}, timeout=a.timeout)
            cfg = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            print(ok("  [6/7] 配置") + " %s" % inst)
            ls = cfg.get("login_shell", True)
            print("        login_shell=%s%s  enable_docker=%s  allowed_roots=%s  timeout=%ss"
                  % (ls, dim("（缺省即 true）") if "login_shell" not in cfg else "",
                     cfg.get("enable_docker"), cfg.get("allowed_roots"), cfg.get("command_timeout")))
            if ls and a.check_banner:
                banner, _ = c.call("exec", {"command": "echo BANNER_PROBE"}, timeout=a.timeout)
                _, bout, _ = parse_exec(banner)
                extra = [l for l in bout.splitlines() if l.strip() and l.strip() != "BANNER_PROBE"]
                if extra:
                    print(warn("        [!] 输出前有多余内容（%d 行），疑似登录横幅" % len(extra)))
                    print("            设 login_shell=false 可让每次工具返回都干净：")
                    print("            python fleet.py exec %s \"sed -i 's/\\\"login_shell\\\": *true/\\\"login_shell\\\": false/' "
                          "%s/mcp_agent_config.json\" --sudo" % (a.target, inst))
                    fails.append("登录 shell 在每次返回前打印横幅：把配置 login_shell 改成 false 再重启服务。")
        except Exception:
            pass

        # 7 协议层（v5.0.3：邮箱积压 / 死信 / peer 熔断冷却——链路体检全绿
        #   但「消息发不出去」时，答案通常在这里）
        try:
            req = urllib.request.Request(base.rstrip("/") + "/fleet/ui/data")
            if meta.get("token"):
                req.add_header("Authorization", "Bearer " + meta["token"])
            with urllib.request.urlopen(req, timeout=a.timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            mb = d.get("mailbox") or {}
            print(ok("  [7/7] 协议") + " inbox=%s 待投=%s 死信=%s 邮箱=%.0fKB 版本=%s"
                  % (mb.get("inbox_total"), mb.get("outbox_pending"),
                     mb.get("outbox_dead"), (mb.get("inbox_bytes") or 0) / 1024.0,
                     d.get("version")))
            cds = [p for p in (d.get("peers") or []) if p.get("cooldown")]
            if cds:
                names = ", ".join("%s(%ss)" % (p.get("name"), p["cooldown"]) for p in cds[:5])
                print(warn("        [!] %d 个 peer 熔断冷却中: %s" % (len(cds), names)))
                fails.append("有 peer 处于熔断冷却（近期连接连续失败）：正常离线设备会自动"
                             "探测恢复；持续冷却请到该设备上排查服务状态。")
            if (mb.get("outbox_dead") or 0) > 0:
                print(warn("        [!] 死信 x%s（超重试上限已放弃，详见 outbox.jsonl 的 dead 记录）"
                           % mb.get("outbox_dead")))
            if (mb.get("outbox_pending") or 0) > 50:
                fails.append("待投递消息积压 %s 条：多数是目标节点长期离线；上线后会自动补投，"
                             "死信可在 outbox.jsonl 审计。" % mb.get("outbox_pending"))
        except Exception as e:
            print(err("  [7/7] 协议") + " 拉取失败: %r（v5.0.3 以下旧节点无此端点属正常）" % (e,))

    print()
    if fails:
        print(warn("发现 %d 个问题：" % len(fails)))
        for i, f in enumerate(fails, 1):
            print("  %d) %s" % (i, f))
        print("\n" + dim("更多排查：references/pitfalls.md"))
        return 2
    print(ok("链路健康，没有发现问题。"))
    return 0


def cmd_exec(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    # argparse 的 REMAINDER 会把写在 target 之后的 --sudo 也吞进 command，
    # 这里把开头的已知开关捞出来，让 `exec nas --sudo "cmd"` 与
    # `exec --sudo nas "cmd"` 两种写法都成立。
    toks = list(a.command)
    sudo, workdir, timeout_ms = a.sudo, a.workdir, a.timeout_ms
    while toks and toks[0].startswith("--"):
        if toks[0] == "--sudo":
            sudo = True
            toks.pop(0)
        elif toks[0] == "--workdir" and len(toks) > 1:
            workdir = toks[1]
            del toks[:2]
        elif toks[0].startswith("--workdir="):
            workdir = toks[0].split("=", 1)[1]
            toks.pop(0)
        elif toks[0] == "--timeout-ms" and len(toks) > 1:
            timeout_ms = int(toks[1])
            del toks[:2]
        elif toks[0].startswith("--timeout-ms="):
            timeout_ms = int(toks[0].split("=", 1)[1])
            toks.pop(0)
        else:
            break
    cmd = " ".join(toks)
    if not cmd.strip():
        return die("命令为空")
    args = {"command": cmd}
    if sudo:
        args["sudo"] = True
    if workdir:
        args["workDir"] = workdir
    if timeout_ms:
        args["timeoutMs"] = timeout_ms
    c = MCPClient(url, max(a.timeout, 120), token=meta.get("token", ""))
    c.handshake()
    try:
        text, iserr = c.call("exec", args, timeout=max(a.timeout, 120))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def cmd_call(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    if a.tool == "list":
        return cmd_tools(a)
    arguments = {}
    if a.args:
        try:
            arguments = json.loads(a.args)
        except ValueError as e:
            return die("--args 不是合法 JSON：%s" % e)
    if a.arg:                         # 允许 -D key=value 简写
        for kv in a.arg:
            k, _, v = kv.partition("=")
            if not k:
                return die("--arg 需要 key=value 形式，收到 %r" % kv)
            if v.lower() in ("true", "false"):
                arguments[k] = (v.lower() == "true")
            else:
                try:
                    arguments[k] = json.loads(v)
                except ValueError:
                    arguments[k] = v
    c = MCPClient(url, max(a.timeout, 120), token=meta.get("token", ""))
    c.handshake()
    try:
        text, iserr = c.call(a.tool, arguments, timeout=max(a.timeout, 120))
    except MCPError as e:
        return die("调用 %s 失败：%s" % (a.tool, e))
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def _mcp_client(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    c = MCPClient(url, max(a.timeout, 120), token=meta.get("token", ""))
    c.handshake()
    return c


def cmd_exec_async(a):
    c = _mcp_client(a)
    cmd = " ".join(a.command).strip()
    if not cmd:
        return die("命令为空")
    args = {"command": cmd}
    if a.sudo:
        args["sudo"] = True
    if a.workdir:
        args["workDir"] = a.workdir
    try:
        text, iserr = c.call("exec_async", args, timeout=max(a.timeout, 30))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def cmd_exec_poll(a):
    c = _mcp_client(a)
    args = {"job_id": a.job_id, "offset": a.offset}
    try:
        text, iserr = c.call("exec_poll", args, timeout=max(a.timeout, 30))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def cmd_exec_result(a):
    c = _mcp_client(a)
    args = {"job_id": a.job_id, "timeoutMs": int(a.wait * 1000)}
    try:
        text, iserr = c.call("exec_result", args, timeout=max(a.timeout, int(a.wait) + 30))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def cmd_exec_kill(a):
    c = _mcp_client(a)
    try:
        text, iserr = c.call("exec_kill", {"job_id": a.job_id}, timeout=max(a.timeout, 30))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def cmd_exec_jobs(a):
    c = _mcp_client(a)
    try:
        text, iserr = c.call("exec_jobs", {}, timeout=max(a.timeout, 30))
    except MCPError as e:
        return die("调用失败：%s" % e)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if iserr else 0


def _seen_path():
    return os.path.join(os.path.expanduser("~"), ".mcp-fleet", "seen.json")


def _load_seen():
    try:
        with open(_seen_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_seen(d):
    try:
        p = _seen_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        pass                    # 记账失败不该影响体检本身


def _node_uptime(url, token="", timeout=3):
    """问副端要 uptime。拿不到就返回 None，不当作故障。

    为什么单独问 /fleet/health：`initialize` 只回版本和工具数，
    回答不了「这台机器到底连续跑了多久」——而「长时间在线」正是要看的那个东西。
    """
    base = (url or "").rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    if not base.startswith("http"):
        return None
    try:
        d = _http_json(base + "/fleet/health", token, timeout=timeout, insecure=True)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _fmt_age(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd%02dh" % (sec // 86400, (sec % 86400) // 3600)


def cmd_health(a):
    nodes = discover_nodes(a.agent)
    if not nodes:
        print(warn("没有发现任何副端。"))
        return 1
    rows, bad = [], 0
    seen = _load_seen()
    now = time.time()
    for name in sorted(nodes):
        url = nodes[name]["url"]
        ntok = nodes[name].get("token", "")
        row = {"name": name, "url": url}
        t0 = time.time()
        try:
            info = http_probe(url, timeout=a.timeout, token=ntok)
            row.update(status="up", version=info.get("version"),
                       tools=info.get("tools"), ms=int((time.time() - t0) * 1000))
            # 在线时长与「什么时候开始见到它」——这是长在线的证据，不是装饰
            hp = _node_uptime(url, ntok)
            if hp:
                row["uptime_s"] = hp.get("uptime_s")
                row["health"] = {k: hp.get(k) for k in
                                 ("inbox", "outbox", "peers", "read_only")}
            if a.deep:
                c = MCPClient(url, a.timeout, token=ntok)
                c.handshake()
                text, iserr = c.call("exec", {"command": "echo OK"}, timeout=a.timeout)
                code, out, _ = parse_exec(text)
                row["deep"] = "OK" if (not iserr and not code and "OK" in out) else "FAIL"
                if row["deep"] == "FAIL":
                    row["status"] = "degraded"
        except Exception as e:
            row.update(status="down", error=str(e)[:80], ms=int((time.time() - t0) * 1000))
        if row["status"] != "up":
            bad += 1

        # 记账：首次见到 / 最后一次可达 / 累计次数。掉线不清零，才能回答
        # 「这台是不是长期在线」——只看当次 UP 是看不出稳定性的。
        s = seen.setdefault(name, {})
        s.setdefault("first_seen", int(now))
        if row["status"] == "up":
            s["last_ok"] = int(now)
            s["up_count"] = int(s.get("up_count") or 0) + 1
        else:
            s["last_down"] = int(now)
            s["down_count"] = int(s.get("down_count") or 0) + 1
        row["first_seen"] = s.get("first_seen")
        row["last_ok"] = s.get("last_ok")
        row["up_count"] = s.get("up_count")
        rows.append(row)
    _save_seen(seen)

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 1 if bad else 0

    w = max(len(r["name"]) for r in rows)
    print("副端体检（%d 台，%s）\n" % (len(rows), time.strftime("%Y-%m-%d %H:%M:%S")))
    for r in rows:
        if r["status"] == "up":
            extra = ("  deep=%s" % r["deep"]) if "deep" in r else ""
            up = r.get("uptime_s")
            # 进程在线时长 + 本地观测到的连续可达天数，两个一起看才有意义：
            # 前者说明服务没重启，后者说明链路一直通。
            if up is not None:
                watched = int(time.time() - (r.get("first_seen") or time.time()))
                span = "  连续 %s（已观测 %s）" % (_fmt_age(up), _fmt_age(watched))
            else:
                span = ""
            print("  %s %-*s  v%-6s %3s 工具  %5d ms%s%s"
                  % (ok("UP  "), w, r["name"], r.get("version") or "?", r.get("tools") or 0,
                     r["ms"], span, extra))
        elif r["status"] == "degraded":
            print("  %s %-*s  HTTP 通但工具调用失败" % (warn("DEGR"), w, r["name"]))
        else:
            last = r.get("last_ok")
            tail = ("（最后可达 %s 前）" % _fmt_age(time.time() - last)) if last else ""
            print("  %s %-*s  %s%s" % (err("DOWN"), w, r["name"],
                                       r.get("error", ""), tail))
    print()
    print(ok("全部在线。") if not bad else warn("%d 台异常。" % bad))
    if any(r["status"] == "up" for r in rows):
        print(dim("记账文件：%s（首次见到 / 最后可达 / 累计次数）" % _seen_path()))
    if bad:
        print(dim("逐台排查：python fleet.py doctor <名字>"))
    return 1 if bad else 0


MIRRORS = {
    "github": "https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/install.sh",
    "jsdelivr": "https://cdn.jsdelivr.net/gh/guoxpeng/skill-mcp-fleet@main/install.sh",
    "ghfast": "https://ghfast.top/https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/install.sh",
    "ghproxy": "https://ghproxy.net/https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/install.sh",
    "ghproxycom": "https://gh-proxy.com/https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/install.sh",
}


def cmd_install_cmd(a):
    # 一律走 shlex.quote：原来用单引号手工拼接，值里只要含 ' 就会破坏引号
    # （命令注入），而 --name / --prefix 干脆没加引号。
    q = shlex.quote
    opts = "--name %s --port %d" % (q(str(a.name)), a.port)
    if a.prefix:
        opts += " --prefix %s" % q(str(a.prefix))
    if a.sudo_pass:
        opts += " --sudo-pass %s" % q(str(a.sudo_pass))
    if a.tunnel:
        opts += " --tunnel"
    if a.mode and a.mode != "auto":
        opts += " --mode %s" % q(str(a.mode))
    if getattr(a, "auth_token", None):
        opts += " --auth-token %s" % q(str(a.auth_token))
    if getattr(a, "allow_ips", None):
        opts += " --allow-ips %s" % q(str(a.allow_ips))
    if getattr(a, "directory", None):
        opts += " --directory %s" % q(str(a.directory))
    if getattr(a, "directory_token", None):
        opts += " --directory-token %s" % q(str(a.directory_token))
    if getattr(a, "no_keepalive", False):
        opts += " --no-keepalive"
    if getattr(a, "generate_token", False):
        opts += " --generate-token"
    if getattr(a, "require_auth", False):
        opts += " --require-auth"
    if a.uninstall:
        opts += " --uninstall"

    local = os.path.join(HERE, "install.sh")
    if a.offline:
        b64 = os.path.join(HERE, "install-offline.b64")
        sfx = os.path.join(HERE, "install-offline.sh")
        print("# 离线安装（设备出不了外网 / github 被墙时首选）")
        print("# 1) 把安装文件拷到副设备（U 盘 / scp / 内网 http 都行），三选一：")
        print("#      %s   ← 普通安装脚本（最简单）" % local)
        if os.path.isfile(sfx):
            print("#      %s  ← 自解压脚本（只需这一个文件）" % sfx)
        if os.path.isfile(b64):
            print("#      %s ← 纯文本 base64（没法传二进制时用）" % b64)
        print("# 2) 在副设备上执行")
        if os.path.isfile(sfx):
            print("sudo bash install-offline.sh %s" % opts)
        print("sudo bash install.sh %s" % opts)
        if os.path.isfile(b64):
            print()
            print("# 完全断网也能装：install-offline.b64 = base64(zlib(install.sh))，只需 python3")
            print("python3 -c \"import base64,zlib;open('/tmp/mcp-install.sh','wb').write("
                  "zlib.decompress(base64.b64decode(open('install-offline.b64','rb').read())))\" \\")
            print("  && sudo bash /tmp/mcp-install.sh %s" % opts)
            print()
            print("# 校验（解出来应与 install.sh 完全一致）")
            print("md5sum /tmp/mcp-install.sh")
        print()
        print("# 不想手工拷文件？用 SSH 直推（技能自带 install.sh，副设备不用联网）：")
        print("python fleet.py bootstrap --host <副端IP> --user root --name %s" % a.name)
        return 0

    if a.mirror == "auto":
        print("# 先试官方源，失败再依次换镜像（内容一致）")
        for i, (k, u) in enumerate(MIRRORS.items()):
            if i == 0:
                print("curl -fsSL %s -o install.sh \\" % u)
            else:
                print("  || curl -fsSL %s -o install.sh \\" % u)
        print("  && sudo bash install.sh %s" % opts)
        print()
        print("# 最简（官方源可用时）：")
        print("curl -fsSL %s -o install.sh && sudo bash install.sh %s" % (MIRRORS["github"], opts))
        print()
        print("# 一条命令（管道方式，需 root）：")
        print("curl -fsSL %s | sudo bash -s -- %s" % (MIRRORS["ghfast"], opts))
    else:
        url = MIRRORS.get(a.mirror, a.mirror)
        print("curl -fsSL %s -o install.sh && sudo bash install.sh %s" % (url, opts))
    print()
    print(dim("# Termux 用户（无 sudo）：bash install.sh %s" % opts))
    print(dim("装完看输出里的「服务状态: active」+「本机自检通过」。"))
    print(dim("然后在主端：python fleet.py add --name %s --ip <副端IP> --port %d" % (a.name, a.port)))
    return 0


def cmd_bootstrap(a):
    """SSH 推安装：优先用技能自带的 install.sh（离线、不依赖外网）。"""
    local = os.path.join(HERE, "install.sh")
    use_local = os.path.isfile(local) and not a.online
    if not shutil.which("ssh") or not shutil.which("scp"):
        return die("本机没有 ssh / scp，无法远程推送。改用 install-cmd 打印命令手工执行。")

    pre = []
    if a.password or a.password_file:
        pw = a.password
        if a.password_file:
            with open(a.password_file, encoding="utf-8") as f:
                pw = f.read().strip()
        if not shutil.which("sshpass"):
            return die("设备只接受密码登录，但本机没有 sshpass。\n"
                       "    三条出路：\n"
                       "      1) 装 sshpass（Linux/macOS: brew/apt install sshpass）\n"
                       "      2) 配一把公钥：ssh-copy-id -p %d %s@%s\n"
                       "      3) 别用 bootstrap，改用 install-cmd 打印命令，手工在设备上跑"
                       % (a.ssh_port, a.user, a.host))
        pre = ["sshpass", "-p", pw]

    dest = "%s@%s" % (a.user, a.host)
    keyopt = ["-i", a.key] if a.key else []
    # accept-new：首次连接仍自动接受（保住「一条命令装完」的体验），
    # 但主机密钥一旦记录就严格校验。原 StrictHostKeyChecking=no 等于完全放弃
    # MITM 防护，而这条通道推的正是以 root 执行的安装脚本。
    hostkey = "StrictHostKeyChecking=accept-new"
    ssh_base = pre + ["ssh"] + keyopt + [
        "-p", str(a.ssh_port), "-o", hostkey,
        "-o", "ConnectTimeout=%d" % a.timeout, dest]
    scp_base = pre + ["scp"] + keyopt + [
        "-P", str(a.ssh_port), "-o", hostkey]

    # 一律走 shlex.quote：原来用单引号手工拼接，值里只要含 ' 就会破坏引号
    # （命令注入），而 --name / --prefix 干脆没加引号。
    q = shlex.quote
    opts = "--name %s --port %d" % (q(str(a.name)), a.node_port)
    if a.prefix:
        opts += " --prefix %s" % q(str(a.prefix))
    if a.sudo_pass:
        opts += " --sudo-pass %s" % q(str(a.sudo_pass))
    if a.tunnel:
        opts += " --tunnel"
    if a.mode and a.mode != "auto":
        opts += " --mode %s" % q(str(a.mode))
    if getattr(a, "auth_token", None):
        opts += " --auth-token %s" % q(str(a.auth_token))
    if getattr(a, "allow_ips", None):
        opts += " --allow-ips %s" % q(str(a.allow_ips))
    if getattr(a, "directory", None):
        opts += " --directory %s" % q(str(a.directory))
    if getattr(a, "directory_token", None):
        opts += " --directory-token %s" % q(str(a.directory_token))
    if getattr(a, "no_keepalive", False):
        opts += " --no-keepalive"
    if getattr(a, "generate_token", False):
        opts += " --generate-token"
    if getattr(a, "require_auth", False):
        opts += " --require-auth"

    print("== bootstrap %s ==" % dest)
    try:
        r = subprocess.run(ssh_base + ["echo SSH_OK; id -u; uname -srm"],
                           capture_output=True, text=True, timeout=a.timeout + 20)
    except subprocess.TimeoutExpired:
        return die("SSH 连接超时")
    if r.returncode != 0:
        return die("SSH 登录失败：%s" % (r.stderr.strip()[:300] or r.stdout.strip()[:300]))
    print(ok("[+] ") + "SSH 通：" + r.stdout.strip().replace("\n", " / "))

    tmp = "/tmp/mcp-fleet-install.sh"
    if use_local:
        print("[.] 上传本地 install.sh（离线，不依赖副设备外网）…")
        r = subprocess.run(scp_base + [local, "%s:%s" % (dest, tmp)],
                           capture_output=True, text=True, timeout=a.timeout + 60)
        if r.returncode != 0:
            return die("上传失败：%s" % r.stderr.strip()[:300])
        remote = "sudo bash %s %s" % (tmp, opts)
    else:
        print("[.] 副设备在线下载（github → 镜像依次重试）…")
        dl = ("cd /tmp && (curl -fsSL %s -o mcp-fleet-install.sh "
              "|| curl -fsSL %s -o mcp-fleet-install.sh "
              "|| curl -fsSL %s -o mcp-fleet-install.sh) && sudo bash /tmp/mcp-fleet-install.sh %s"
              % (MIRRORS["github"], MIRRORS["ghfast"], MIRRORS["jsdelivr"], opts))
        remote = dl

    if a.dry_run:
        print(warn("[dry-run] ") + "将执行：ssh %s %r" % (dest, remote))
        return 0

    print("[.] 远程安装中（可能要 1-2 分钟）…")
    r = subprocess.run(ssh_base + [remote], capture_output=True, text=True, timeout=a.timeout + 300)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-18:]
    for l in tail:
        print("    " + l)
    if r.returncode != 0:
        return die("安装返回非 0（%d）。上面是末尾日志。" % r.returncode)

    print()
    print(ok("[+] ") + "副端已安装。下一步在主端注册：")
    print("    python fleet.py add --name %s --ip %s --port %d" % (a.name, a.host, a.node_port))
    return 0


def cmd_tunnel(a):
    url, meta = resolve_target(a.target, a.agent, a.port)
    inst = a.dir or "/opt/%s_mcp" % a.target
    c = MCPClient(url, max(a.timeout, 120), token=meta.get("token", ""))
    c.handshake()

    def run(cmd, timeout=90):
        text, _ = c.call("exec", {"command": cmd}, timeout=timeout)
        return text

    if a.stop:
        print(run("if [ -f %s/tunnel.pid ]; then kill $(cat %s/tunnel.pid) 2>/dev/null && echo 已停止隧道; "
                  "else echo 没有 tunnel.pid; fi" % (inst, inst)))
        return 0

    if a.start:
        print("[.] 启动隧道（复用已有实例；地址不变）…")
        run("nohup bash %s/mcp-tunnel.sh >/dev/null 2>&1 & echo started" % inst, timeout=30)
        deadline = time.time() + a.wait
        seen = ""
        while time.time() < deadline:
            seen = run("grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' %s/tunnel.log 2>/dev/null | tail -1" % inst)
            if seen.strip():
                break
            time.sleep(5)
        seen = seen.strip()
        if seen:
            print(ok("[+] ") + "隧道地址：%s" % seen)
            print("    主端注册：python fleet.py add --name %s --url %s/mcp" % (a.target, seen))
            print("    注意：地址是临时的，隧道重启就变。")
        else:
            print(warn("[!] ") + "%ds 内没在 tunnel.log 里看到地址。" % a.wait)
            print("    看日志：python fleet.py exec %s \"tail -30 %s/tunnel.log\"" % (a.target, inst))
            print("    trycloudflare 有建速限制，别反复重开（等 10~30 分钟）。")
        return 0

    # 默认：查状态
    print("== %s 隧道状态 ==" % a.target)
    text = run("echo '安装目录: %s'; ls -l %s/mcp-tunnel.sh 2>/dev/null || echo '没有 mcp-tunnel.sh'; "
               "if [ -f %s/tunnel.pid ] && kill -0 $(cat %s/tunnel.pid) 2>/dev/null; then "
               "echo \"隧道进程: 运行中 pid=$(cat %s/tunnel.pid)\"; else echo '隧道进程: 未运行'; fi; "
               "echo '--- tunnel.log 末尾 ---'; tail -5 %s/tunnel.log 2>/dev/null || echo '(无日志)'; "
               "echo '--- 地址 ---'; grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' %s/tunnel.log 2>/dev/null | tail -1 || echo '(未找到)'"
               % (inst, inst, inst, inst, inst, inst, inst))
    _, out, _ = parse_exec(text)
    print(out)
    print(dim("启动：python fleet.py tunnel %s --start      停止：--stop" % a.target))
    return 0


def cmd_add(a):
    if not a.name:
        return die("需要 --name")
    if not a.ip and not a.url:
        return die("需要 --ip（内网）或 --url（完整地址/公网隧道）")
    url = normalize_url(a.url or a.ip, a.port)
    token = (getattr(a, "token", "") or "").strip()

    if not a.no_test:
        try:
            info = http_probe(url, timeout=a.timeout, token=token)
            print(ok("[+] ") + "副端在线：%s v%s，%d 个工具"
                  % (info.get("server"), info.get("version"), info.get("tools")))
            if info.get("auth") and not token:
                print(warn("[!] ") + "副端开了鉴权，但没给 --token —— 工具调用会被 401 拒绝。")
                print(dim("    取令牌：在副端跑  grep auth_token 副端安装目录/mcp_agent_config.json"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(err("[x] ") + "副端在线但拒绝访问（HTTP %d）—— 需要 --token" % e.code)
                print(dim("    取令牌：在副端跑  grep auth_token 副端安装目录/mcp_agent_config.json"))
                if not a.force:
                    return 2
                print(warn("[force] ") + "仍然写入配置。")
            else:
                print(err("[x] ") + "连不上 %s（HTTP %d）" % (url, e.code))
                if not a.force:
                    return 2
        except Exception as e:
            print(err("[x] ") + "连不上 %s（%r）" % (url, e))
            print("\n排查：")
            print("  内网机型 → 确认副端装好了，且主端能访问该 IP:端口")
            print("  容器/云主机 → 内网 IP 主端够不到，到副端跑：bash 副端安装目录/mcp-tunnel.sh")
            print("  装副端     → python fleet.py install-cmd --name %s --port %d" % (a.name, a.port))
            if not a.force:
                return 2
            print(warn("[force] ") + "仍然写入配置。")
        else:
            try:
                c = MCPClient(url, a.timeout, token=token)
                c.handshake()
                text, _ = c.call("exec", {"command": "hostname; uname -srm"}, timeout=a.timeout)
                print(ok("[+] ") + "工具测试：" + text.strip().replace("\n", " / ")[:120])
            except MCPError as e:
                print(warn("[!] ") + "工具测试失败：%s" % e)

    # 写 agent 配置
    written = []
    targets = [a.agent] if a.agent and a.agent != "auto" else ["workbuddy"]
    if a.mcp:
        targets = [os.path.abspath(a.mcp)]
    for t in targets:
        path = t if os.path.sep in t or t.endswith(".json") else None
        if path is None:
            cands = agent_config_paths().get(t, [])
            if not cands:
                print(warn("[!] ") + "未知 agent：%s，跳过" % t)
                continue
            path = cands[0]
        try:
            cfg = load_json_config(path) if os.path.isfile(path) else {}
        except Exception as e:
            print(warn("[!] ") + "%s 读取失败（%s），跳过" % (path, e))
            continue
        k = servers_key(cfg)
        cfg.setdefault(k, {})
        entry = {
            "type": "http", "url": url,
            "description": "%s 副端 MCP（远程操控该设备：exec/文件/Docker/systemd/系统信息）" % a.name,
        }
        if token:
            entry["headers"] = {"Authorization": "Bearer " + token}
        cfg[k][a.name] = entry
        save_json_config(path, cfg)
        written.append(path)
        print(ok("[+] ") + "已写入 %s" % path)

    # 写本地登记表（让不支持 MCP 的 agent 也能用 fleet.py 操控）
    reg = load_registry()
    reg.setdefault("nodes", {})[a.name] = {"url": url,
                                           "description": "mcp-fleet 副端",
                                           "token": token}
    save_registry(reg)
    print(ok("[+] ") + "已登记到 %s" % REGISTRY)

    print()
    if token:
        print("  \"%s\": { \"type\": \"http\", \"url\": \"%s\","
              " \"headers\": { \"Authorization\": \"Bearer %s\" } }" % (a.name, url, token))
    else:
        print("  \"%s\": { \"type\": \"http\", \"url\": \"%s\" }" % (a.name, url))
    if written:
        print("\n下一步：重启 agent（或重载 MCP 配置）才能看到 %s__* 工具。" % a.name)
    print(dim("不想重启？直接调：python fleet.py exec %s \"uname -a\"" % a.name))
    return 0


def cmd_remove(a):
    removed = []
    paths = [os.path.abspath(a.mcp)] if getattr(a, "mcp", None) else []
    if not paths:
        for ag, cands in agent_config_paths().items():
            if a.agent and a.agent != "auto" and ag != a.agent:
                continue
            for p in cands:
                if os.path.isfile(p):
                    paths.append(p)
                    break                  # 每个 agent 只取第一个存在的配置
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            cfg = load_json_config(p)
        except Exception:
            continue
        k = servers_key(cfg)
        if a.name in (cfg.get(k) or {}):
            del cfg[k][a.name]
            save_json_config(p, cfg)
            removed.append(p)
    reg = load_registry()
    if a.name in (reg.get("nodes") or {}):
        del reg["nodes"][a.name]
        save_registry(reg)
        removed.append(REGISTRY)
    if removed:
        for p in removed:
            print(ok("[+] ") + "已移除 %s ← %s" % (a.name, p))
    else:
        print(warn("[i] ") + "哪里都没找到 %s，无需移除" % a.name)
    return 0


def cmd_sync(a):
    """把登记表里的副端地址，按地址目录刷新一遍。"""
    durl, dtok = registry_directory()
    if not durl:
        print(warn("[i] ") + "还没配地址目录。先设一个：")
        print("    python fleet.py directory --url http://<跑目录服务的机器>:8790"
              + (" --token <token>" if not dtok else ""))
        print(dim("    然后副端安装时加 --directory <同一个 url>，副端保活守护就会持续上报地址。"))
        return 1

    names = list(a.names or [])
    if not names:
        names = sorted(discover_nodes(a.agent))
    if not names:
        print(warn("没有副端可同步。"))
        return 0

    try:
        dn = fetch_directory(durl, dtok, a.timeout)
    except Exception as e:
        print(err("[x] ") + "地址目录不可达：%s" % e)
        print(dim("    目录服务在跑吗？  python3 fleet-directory.py --port 8790"))
        return 2

    print("地址目录 %s：%d 条记录\n" % (durl, len(dn)))
    reg = load_registry()
    reg.setdefault("nodes", {})
    changed = 0
    for name in names:
        v = dn.get(name)
        if not v:
            print("  %-16s %s" % (name, dim("目录里没有（副端还没上报过？）")))
            continue
        new = normalize_url((v.get("url") or "").strip())
        cur = ((load_registry().get("nodes") or {}).get(name) or {}).get("url") \
            or (discover_nodes(a.agent).get(name) or {}).get("url") or ""
        age = v.get("age")
        # 采信前同样要过防投毒校验：cmd_sync 原本比 sync_from_directory 更宽松
        # （完全不探测、直接落盘），是最容易被投毒利用的入口。
        if cur and cur != new:
            if not domain_ok(cur, new):
                print("  %-16s %s" % (name, warn("跳过：域名由 %s 变为 %s，需人工确认"
                                                % (host_of(cur), host_of(new)))))
                continue
            if not probe_anonymous(new, timeout=min(a.timeout, 6)):
                print("  %-16s %s" % (name, warn("跳过：%s 不像 MCP 副端" % new)))
                continue
            # SEC-03：匿名探活拿到的 server 名必须等于期望节点名，否则拒绝。
            # 少了这道闸门，投毒的目录条目能把真令牌骗到冒名地址去——
            # 上面两道只证明「对面是个 MCP 服务」，不证明「对面是这台设备」。
            anon = {}
            try:
                anon = http_probe(new, timeout=min(a.timeout, 6), token="", retries=0)
            except Exception:
                anon = {}
            if (anon or {}).get("server") != name:
                print("  %-16s %s" % (name, warn(
                    "跳过：节点身份不符（期望 %s，实测 %s），拒绝采信"
                    % (name, (anon or {}).get("server") or "(无)"))))
                continue
        flag = ok("NEW ") if cur and cur != new else dim("ok  ")
        if cur != new:
            changed += 1
        if name in reg["nodes"]:
            reg["nodes"][name]["url"] = new
            reg["nodes"][name]["synced_at"] = int(time.time())
        print("  %-16s %s%s%s" % (name, flag, new,
                                  dim("  (上报于 %ss 前)" % age if age is not None else "")))
    if changed:
        save_registry(reg)
    print()
    print(dim("说明：地址目录里没有的副端不会被改动；登记表里的地址已更新 %d 条。" % changed))
    print(dim("      agent 自己的 mcp.json 不会被动 —— 那边改完要重启 agent，"
              "用 `fleet.py exec` 则立即生效。"))
    return 0


def cmd_directory(a):
    """查看 / 设置地址目录。"""
    reg = load_registry()
    if a.url:
        reg["directory"] = {"url": a.url.rstrip("/"),
                            "token": a.token if a.token is not None
                            else (reg.get("directory") or {}).get("token", "")}
        save_registry(reg)
        print(ok("[+] ") + "地址目录已保存：%s" % reg["directory"]["url"])
    elif a.clear:
        reg.pop("directory", None)
        save_registry(reg)
        print(ok("[+] ") + "已清除地址目录配置")
        return 0

    durl, dtok = registry_directory()
    if not durl:
        print(warn("[i] ") + "未配置地址目录。")
        print("    作用：副端隧道地址变了以后，主端自动跟随，不用手工改 mcp.json。")
        print("    起服务： python3 fleet-directory.py --port 8790 --token <随机串>")
        print("    配主端： python fleet.py directory --url http://<主机>:8790 --token <同一串>")
        print("    配副端： install.sh ... --directory http://<主机>:8790 --directory-token <同一串>")
        return 0

    print("地址目录: %s" % durl)
    print("鉴权    : %s" % ("已配置 token" if dtok else dim("无（公开可读）")))
    print("来源    : %s" % ("环境变量" if os.environ.get("FLEET_DIRECTORY_URL") else "登记表 "
                          + REGISTRY))
    try:
        dn = fetch_directory(durl, dtok, a.timeout)
    except Exception as e:
        print(err("[x] ") + "连不上：%s" % e)
        return 2
    print("\n已上报的副端（%d 个）：" % len(dn))
    for name in sorted(dn):
        v = dn[name]
        stale = "  " + warn("陈旧") if v.get("stale") else ""
        print("  %-16s %-46s %s%s" % (name, v.get("url", ""),
                                      dim("%ss 前" % v.get("age", "?")), stale))
    return 0


# ---------------------------------------------------------------- 对等配对（v3.5）

def _is_cgnat(ip):
    """100.64.0.0/10：Tailscale/CGNAT 用的地址段，局域网里不可路由。"""
    try:
        a, b = [int(x) for x in ip.split(".")[:2]]
    except Exception:
        return False
    return a == 100 and 64 <= b <= 127


def _iface_ipv4_fallback():
    """从系统接口表里挑一个**局域网可路由**的 IPv4。

    只在路由探测拿到 CGNAT/tailnet 地址时才用得上：那种地址对 tailnet 内
    可达，但同一个局域网里的设备连不上，广播出去等于给了个错地址。
    """
    cmds = [["ifconfig"], ["ip", "-4", "-o", "addr"]]
    for cmd in cmds:
        rc, out = _sh(cmd, timeout=5)
        if rc != 0 or not out:
            continue
        for ip in re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
            if (ip.startswith("127.") or ip.startswith("169.254.")
                    or _is_cgnat(ip)):
                continue
            return ip
    return ""


def _lan_ip():
    """本机局域网 IPv4。

    macOS 上不能只用「连 10.255.255.255 看源地址」这一招：开了 Tailscale
    出口节点（exit node）或全局 VPN 时，默认路由会走 utun*，拿到的是隧道
    地址，于是节点对外广播一个局域网里谁都连不上的 URL。
    所以 darwin 先问 en0/en1（Wi-Fi / 有线），拿不到再退回路由探测；
    探测结果落在 100.64/10（tailnet）时，再从接口表里挑一个真实局域网地址。
    """
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2"):
            rc, out = _sh(["ipconfig", "getifaddr", iface], timeout=5)
            ip = (out or "").strip()
            if (rc == 0 and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip)
                    and not ip.startswith("127.")
                    # 169.254.x.x 是 DHCP 失败后的自配地址，广播出去谁也连不上
                    and not ip.startswith("169.254.")):
                return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    if _is_cgnat(ip):
        real = _iface_ipv4_fallback()
        if real:
            return real
    return ip


def local_node(self_url=None):
    """本机如果也装了节点 agent，返回它的 {name,url,token,dir}；否则 None。"""
    d = os.environ.get("FLEET_NODE_DIR") or \
        (HERE if os.path.isfile(os.path.join(HERE, "mcp_agent_config.json")) else "")
    if not d or not os.path.isfile(os.path.join(d, "mcp_agent_config.json")):
        return None
    try:
        cfg = load_json_config(os.path.join(d, "mcp_agent_config.json"))
    except Exception:
        return None

    def _read(fn):
        try:
            return open(os.path.join(d, fn), encoding="utf-8").read().strip()
        except Exception:
            return ""
    _scheme = "https" if cfg.get("tls_cert") else "http"
    url = (self_url or cfg.get("self_url") or _read("current_url.txt") or "").strip()
    if url.startswith("http://") and _scheme == "https":
        url = "https://" + url[len("http://"):]
    if not url:
        _port = _read("port.txt") or str(cfg.get("port") or "3100")
        url = "%s://%s:%s" % (_scheme, _lan_ip(), _port)
    url = url.rstrip("/")
    if not re.search(r"/(mcp|sse)$", url):
        url += "/mcp"
    ident = node_identity(d)
    return {"name": cfg.get("name") or socket.gethostname(), "url": url,
            "token": cfg.get("auth_token") or "", "dir": d,
            "public_key": ident["public_key"], "node_id": ident["node_id"],
            "fingerprint": ident["fingerprint"]}


def node_identity(dirpath=None):
    """读取本机节点身份（fleet_core identity.json）；没有则返回空字段。

    兼容两种布局：identity.json 在安装目录，或在配置的 data_dir/ 下。
    """
    d = dirpath
    if not d:
        env = os.environ.get("FLEET_NODE_DIR") or ""
        d = env if env else HERE
    candidates = [os.path.join(d, "identity.json")]
    try:
        with open(os.path.join(d, "mcp_agent_config.json"), encoding="utf-8") as cf:
            _cfg = json.load(cf)
        if _cfg.get("data_dir"):
            candidates.insert(0, os.path.join(_cfg["data_dir"], "identity.json"))
    except Exception:
        pass
    for cand in candidates:
        try:
            with open(cand, encoding="utf-8") as f:
                ident = json.load(f)
            break
        except Exception:
            ident = None
    if not ident:
        return {"public_key": "", "node_id": "", "fingerprint": ""}
    try:
        return {"public_key": ident.get("public_key_b64", ""),
                "node_id": ident.get("node_id", ""),
                "fingerprint": ident.get("fingerprint", "")}
    except Exception:
        return {"public_key": "", "node_id": "", "fingerprint": ""}


def pair_sas(pub_a, pub_b):
    """双方公钥排序后派生 6 位 SAS；两边算出来必须一致。"""
    if not pub_a or not pub_b:
        return ""
    basis = "fleet-sas|" + "|".join(sorted([pub_a, pub_b]))
    digest = hashlib.sha256(basis.encode("utf-8")).digest()
    return "%06d" % (int.from_bytes(digest[:4], "big") % 1000000)


def _node_peer_add(node_dir, name, url, token, ident):
    """把对端写入本机节点的 peers.json（0600），供节点验签/peer_call 使用。"""
    if not node_dir:
        return False
    path = os.path.join(node_dir, "peers.json")
    try:
        data = {"nodes": {}}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded.get("nodes"), dict):
                data = loaded
        data["nodes"][name] = {
            "url": url, "token": token or "", "added_by": "pair",
            "updated": int(time.time()),
            "public_key": ident.get("public_key", ""),
            "node_id": ident.get("node_id", ""),
            "fingerprint": ident.get("fingerprint", ""),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def cmd_pair(a):
    """双向配对：把对方登记到本机，再让对方把本机登记过去，最后从对方回连本机验证。"""
    # v4.3: 支持 CODE@URL 格式（如 nas-a3f2@http://192.168.1.10:3100/mcp）
    pair_name = None
    target = a.target
    if "@" in target and not target.startswith("http"):
        parts = target.split("@", 1)
        pair_name = parts[0].strip()
        target = parts[1].strip()
        print(ok("[+] ") + "设备码: %s" % pair_name)
    if pair_name:
        a.name = pair_name
    url, meta = resolve_target(target, port=a.port)
    token = (a.token or meta.get("token") or "").strip()
    c = MCPClient(url, a.timeout, token=token)
    try:
        info = c.handshake()
    except MCPError as e:
        return die("连不上对方：%s" % e)
    rname = a.name or info.get("name") or host_of(url)
    print(ok("[+] ") + "对方在线：%s v%s  %s" % (rname, info.get("version", "?"), url))

    reg = load_registry()
    reg.setdefault("nodes", {})[rname] = {"url": url, "token": token, "description": "对等节点"}
    save_registry(reg)
    print(ok("[+] ") + "本机 → %s：已登记（%s）" % (rname, REGISTRY))

    me = local_node(a.self_url)
    if a.one_way:
        print(dim("    --one-way：只登记单向。"))
        return 0
    if not me:
        print(warn("[i] ") + "本机没有装节点 agent，所以对方没法反过来控制本机（单向）。")
        print("    要双向：先在本机装节点（install.sh / install-offline.paste.txt），再用节点自带的 fleet 跑一次 pair。")
        return 0
    if me["name"] == rname:
        print(warn("[!] ") + "两边名字都叫 %s，会互相覆盖。用 --name 给对方换个名字，或改一边的 name。" % rname)
        return 2

    # v5.2 首选：调用对端 /fleet/pair 交换公钥（双方互存，后续请求可签名）
    base = url[:-4] if url.endswith("/mcp") else url.rstrip("/")
    peer_ident = {}
    registered = False
    try:
        import urllib.request as _ur
        payload = {"name": me["name"], "url": me["url"], "token": me["token"],
                   "public_key": me.get("public_key", ""),
                   "node_id": me.get("node_id", ""),
                   "fingerprint": me.get("fingerprint", "")}
        req = _ur.Request(base + "/fleet/pair",
                          data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with _ur.urlopen(req, timeout=a.timeout) as resp:
            pair_resp = json.loads(resp.read().decode("utf-8"))
        if pair_resp.get("ok"):
            peer_ident = {"public_key": pair_resp.get("my_public_key", ""),
                          "node_id": pair_resp.get("my_node_id", ""),
                          "fingerprint": pair_resp.get("my_fingerprint", "")}
            registered = True
            _node_peer_add(me["dir"], rname, url, token, peer_ident)
            print(ok("[+] ") + "%s → 本机：已交换身份公钥（%s）" %
                  (rname, peer_ident.get("fingerprint") or "v5 节点"))
    except Exception as _e:
        registered = False

    if not registered:
        # 兼容 v5 节点：退回 peer_add（无公钥，仅 token）
        try:
            text, is_err = c.call("peer_add", {
                "name": me["name"], "url": me["url"], "token": me["token"]}, timeout=a.timeout)
        except MCPError as e:
            text, is_err = str(e), True
        if is_err:
            print(err("[x] ") + "对方登记本机失败：%s" % text.strip()[:300])
            if "未知工具" in text:
                print("    对方节点版本太旧（< v3.5），先在对方重跑一次安装命令升级。")
            return 2
        print(ok("[+] ") + "%s → 本机：%s" % (rname, text.strip()))

    # 取对方身份公钥（v5.2 端点或 MCP peer_info），并写入本机 registry
    if not peer_ident.get("public_key"):
        try:
            info_text, info_err = c.call("peer_info", {}, timeout=a.timeout)
            if not info_err:
                peer_ident = json.loads(info_text)
                if peer_ident.get("public_key"):
                    _node_peer_add(me["dir"], rname, url, token, peer_ident)
        except Exception:
            peer_ident = peer_ident or {}
    if peer_ident.get("public_key"):
        reg = load_registry()
        node = reg.setdefault("nodes", {}).get(rname, {})
        node.update({"public_key": peer_ident.get("public_key", ""),
                     "node_id": peer_ident.get("node_id", ""),
                     "fingerprint": peer_ident.get("fingerprint", "")})
        save_registry(reg)

    # 回连验证：让对方用刚登记的地址调本机一次
    try:
        text, is_err = c.call("peer_call", {"peer": me["name"], "tool": "peer_info"},
                              timeout=max(a.timeout, 40))
    except MCPError as e:
        text, is_err = str(e), True
    if is_err:
        print(warn("[!] ") + "对方回连本机失败：%s" % text.strip()[:300])
        print("    对方够不着本机的 %s。常见原因：本机在 NAT/容器里。" % me["url"])
        print("    办法：本机开隧道（bash %s/mcp-tunnel.sh），或用 --self-url 指定对方能访问的地址后重跑 pair。"
              % me["dir"])
        return 3
    sas = pair_sas(me.get("public_key", ""),
                   peer_ident.get("public_key", "") or info.get("public_key", ""))
    print(ok("[+] ") + "配对成功！%s ↔ %s 双向通讯已建立" % (me["name"], rname))
    if sas:
        print(ok("[+] ") + "v5.2 SAS 校验码：%s（两端显示必须一致）" % sas)
        print("    对方指纹：%s" % (peer_ident.get("fingerprint")
                                     or info.get("fingerprint") or "未提供（v5 节点）"))
    print("")
    print("  本机控制对方： fleet exec %s 'uptime'" % rname)
    print("  对方控制本机： 在 %s 上 fleet exec %s 'uptime'" % (rname, me["name"]))
    print("  AI 互发消息：  msg_send to=%s text='你好'" % rname)
    return 0


def cmd_invite(a):
    """v4.4: 生成邀请码 — 其他设备用此码一键安装+自动连接。"""
    import base64 as _b64
    me = local_node(a.self_url)
    if not me:
        return die("本机没有装节点 agent。先安装：bash install.sh --port 3100")
    # v5.2 优先：节点工具 fleet_invite 生成签名 URI（带公钥/过期/nonce）
    got = _local_mcp(a)
    if got:
        _me, client = got
        try:
            text, is_err = client.call("fleet_invite", {"ttl": a.ttl})
            data = json.loads(text) if not is_err else {}
        except Exception:
            data = {}
        uri = (data or {}).get("uri", "")
        if uri:
            print(ok("[+] ") + "邀请已生成（%s 过期）" % data.get("expires", "?"))
            print("  设备指纹: %s" % data.get("fingerprint", ""))
            print("  邀请 URI: %s" % uri)
            print("")
            print("  对方执行：sh scripts/install.sh --join '%s'" % uri)
            print("           或：fleet join '%s'" % uri)
            if getattr(a, "qr", False):
                print("")
                _show_qr(uri)
            print("")
            print("  ⚠ 只分享给信任的人；首次配对请核对两端 SAS。")
            return 0
    # 兼容 v5.0 节点：走旧邀请码端点
    url = me["url"].rstrip("/").replace("/mcp", "/fleet/invite")
    token = me["token"]
    try:
        import urllib.request as _ur
        req_data = json.dumps({"ttl": a.ttl}).encode()
        req = _ur.Request(url, data=req_data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        resp = _ur.urlopen(req, timeout=a.timeout)
        result = json.loads(resp.read().decode())
    except Exception as e:
        return die("生成邀请失败：%s" % e)
    if not result.get("ok"):
        return die("生成邀请失败：%s" % result.get("error", "unknown"))
    invite_code = result["invite_code"]
    device_code = result["device_code"]
    expires_at = result.get("expires_at", "?")
    install_cmd = result.get("install_cmd", "")
    # 显示
    _bar = "═" * 60
    print("╔%s╗" % _bar)
    print("║  邀请码已生成%-46s║" % "")
    print("║%-60s║" % "")
    print("║  设备码 : %-50s║" % device_code)
    print("║  过期   : %-50s║" % expires_at)
    print("║%-60s║" % "")
    print("║  把以下命令发给要连接的设备：                       ║")
    # 邀请命令可能很长，截断显示
    invite_line = "  %s" % install_cmd
    print("║" + invite_line[:58] + " " * max(1, 58 - len(invite_line)) + "║")
    if len(invite_line) > 58:
        # 多行显示剩余部分
        remaining = invite_line[58:]
        while remaining:
            chunk = remaining[:58]
            remaining = remaining[58:]
            print("║  " + chunk + " " * max(1, 56 - len(chunk)) + "║")
    print("║%-60s║" % "")
    print("║  ⚠  仅分享给信任的人！邀请码包含连接凭据。           ║")
    print("║  对方安装后将自动连接本机，可远程操控。             ║")
    print("╚%s╝" % _bar)
    print("")
    print("  邀请码 (单行，方便复制):")
    print("  %s" % invite_code)
    return 0


def cmd_accept(a):
    """v4.4: 接受邀请码 → 解析 → 确权 → 自动配对。"""
    import base64 as _b64
    # 解析邀请码
    try:
        invite_data = json.loads(_b64.b64decode(a.invite_code).decode())
    except Exception as e:
        return die("邀请码格式错误：%s" % e)
    device_code = invite_data.get("code", "")
    url = invite_data.get("url", "").rstrip("/")
    token = invite_data.get("token", "")
    invite_id = invite_data.get("invite_id", "")
    expires = invite_data.get("expires", 0)
    # 检查过期
    if int(time.time()) > expires:
        return die("邀请码已过期（过期时间: %s）" %
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)))
    # 安全警告
    _bar = "!" * 60
    print("╔%s╗" % _bar)
    print("║  ⚠  安全警告%-48s║" % "")
    print("║%-60s║" % "")
    print("║  你即将连接到以下设备：                               ║")
    print("║  设备码 : %-50s║" % device_code)
    print("║  地址   : %-50s║" % url)
    print("║%-60s║" % "")
    print("║  连接后，对方可以远程操控本机（执行命令、读写文件）。 ║")
    print("║  请确认你信任设备 %s 的主人！%-20s║" % (device_code, ""))
    print("╚%s╝" % _bar)
    print("")
    if not a.yes:
        try:
            ans = input("确认连接？(输入 yes 继续): ").strip()
        except Exception:
            ans = ""
        if ans != "yes":
            print("已取消。")
            return 1
    # 确权：向对方发送 invite/verify
    verify_url = url.replace("/mcp", "/fleet/invite/verify")
    me = local_node(a.self_url)
    my_name = me["name"] if me else (a.name or "guest-%s" % uuid.uuid4().hex[:6])
    my_url = me["url"] if me else (a.self_url or "")
    my_token = me["token"] if me else ""
    try:
        import urllib.request as _ur
        my_ident = node_identity()
        req_data = json.dumps({
            "invite_id": invite_id,
            "name": my_name,
            "url": my_url,
            "token": my_token,
            "public_key": my_ident.get("public_key", ""),
            "node_id": my_ident.get("node_id", ""),
            "fingerprint": my_ident.get("fingerprint", "")
        }).encode()
        req = _ur.Request(verify_url, data=req_data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        resp = _ur.urlopen(req, timeout=a.timeout)
        result = json.loads(resp.read().decode())
    except Exception as e:
        return die("确权失败：%s" % e)
    if not result.get("ok"):
        return die("确权失败：%s" % result.get("error", "unknown"))
    # 对方已登记本机，本机也登记对方
    if me:
        reg = load_registry()
        reg.setdefault("nodes", {})[device_code] = {
            "url": url, "token": token, "description": "邀请配对",
            "public_key": result.get("my_public_key", ""),
            "node_id": result.get("my_node_id", ""),
            "fingerprint": result.get("my_fingerprint", "")}
        save_registry(reg)
    print(ok("[+] ") + "确权通过！已与 %s 建立连接" % device_code)
    _sas = pair_sas(node_identity().get("public_key", ""),
                    result.get("my_public_key", ""))
    if _sas:
        print(ok("[+] ") + "v5.2 SAS 校验码：%s（与对方屏幕核对）" % _sas)
    print("  对方设备码 : %s" % device_code)
    print("  对方地址   : %s" % url)
    print("  本机设备码 : %s" % my_name)
    if me:
        print("")
        print("  控制对方： fleet exec %s 'uptime'" % device_code)
        print("  发消息：   fleet msg send to=%s text='你好'" % device_code)
    return 0


# ---------------------------------------------------------------- 入口


def cmd_mesh(a):
    """打印 mesh 拓扑图：本节点 + 所有 peers 的在线状态和能力摘要。"""
    reg = load_registry()
    nodes = reg.get("nodes", {})
    if not nodes:
        print(warn("[!] ") + "没有已注册的节点。用 fleet.py add / pair 添加。")
        return 1
    print(ok("=" * 60))
    print(ok("  Fleet Mesh 拓扑"))
    print(ok("=" * 60))
    failed = 0
    for name, info in sorted(nodes.items()):
        url = info.get("url", "")
        token = info.get("token", "")
        # 探活并获取 mesh 信息
        try:
            c = MCPClient(url, a.timeout, token=token)
            c.handshake()
            text, is_err = c.call("mesh", {})
            if is_err:
                print(err("  [X] %-16s %s" % (name, url)))
                failed += 1
                continue
            mesh = json.loads(text)
            self_info = mesh.get("self", {})
            caps = self_info.get("caps", {})
            cap_str = []
            if caps.get("docker"): cap_str.append("Docker")
            if caps.get("gpu"): cap_str.append("GPU")
            if caps.get("tailscale"): cap_str.append("TS:%s" % caps.get("tailscale_ip", "?"))
            if caps.get("systemd"): cap_str.append("systemd")
            cap_str.append("%dCPU" % caps.get("cpu_count", 1))
            if caps.get("mem_mb"): cap_str.append("%dMB" % caps["mem_mb"])
            # v4.2: 连接模式 + 控制模式标记
            conn = self_info.get("caps", {}).get("connection_mode", "permanent")
            ctrl = self_info.get("caps", {}).get("control_mode", "direct")
            mode_tag = "[T]" if conn == "temporary" else "[P]"
            ctrl_tag = "[A]" if ctrl == "assisted" else "[D]"
            print(ok("  [*] %-16s %s  [%s] %s%s" % (name, url, ", ".join(cap_str), mode_tag, ctrl_tag)))
            # Peers
            peers = mesh.get("peers", {})
            for pname, pinfo in sorted(peers.items()):
                online = pinfo.get("online", False)
                ts = "tailnet" if pinfo.get("tailnet") else ""
                if online:
                    print("      " + ok("|- ") + "%-14s 在线 %s" % (pname, ts))
                else:
                    print("      " + err("|- ") + "%-14s 离线 %s" % (pname, ts))
        except Exception as e:
            print(err("  [X] %-16s %s  (%s)" % (name, url, e)))
            failed += 1
    print(ok("=" * 60))
    if failed:
        print(err("  有 %d 个节点异常（拓扑不完整）" % failed))
        return 1
    return 0

def cmd_msg(a):
    """fleet msg send/recv/history -- 通过 peer_call 调远程节点的 msg 工具。

    v5.0.9 修复：原来调用的 _resolve_target() 从未定义（该函数全仓库
    无 def，fleet msg 一执行即 NameError 崩溃，功能自 v4.1 起完全不可用）。
    改用与 cmd_call/cmd_probe 相同的 resolve_target()（返回 (url, meta)，
    token 在 meta 里）；target 是 argparse 必需位置参数，无需防御性取值。
    """
    target = getattr(a, "target", None)
    if not target:
        return die("需要指定 target 节点")
    url, meta = resolve_target(target, a.agent, a.port)
    c = MCPClient(url, a.timeout, token=meta.get("token", ""))
    c.handshake()
    if a.msg_cmd == "send":
        text, is_err = c.call("msg_send", {
            "from": a.frm or "fleet-cli",
            "to": a.to,
            "text": a.text,
            "topic": a.topic or "",
        })
    elif a.msg_cmd == "recv":
        text, is_err = c.call("msg_recv", {
            "me": a.me or "fleet-cli",
            "wait": a.wait or 0,
            "topic": a.topic or "",
        })
    elif a.msg_cmd == "history":
        text, is_err = c.call("msg_history", {
            "lines": a.lines or 20,
            "topic": a.topic or "",
        })
    else:
        return die("未知 msg 子命令: %s" % a.msg_cmd)
    if is_err:
        return die(text)
    print(text)
    return 0


def cmd_discover_proto(a):
    """fleet discover -- 主动 UDP 广播 + 扫描已知网段，发现 fleet 节点。"""
    import socket as _sock
    port = a.discover_port or 3199
    print(ok("[*] ") + "UDP 广播发现 (端口 %d)..." % port)
    # 发广播
    sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_BROADCAST, 1)
    sock.settimeout(a.timeout or 10)
    hello = json.dumps({"proto": "fleet", "ver": 1, "name": "discover",
                        "port": 0, "ts": time.time()}).encode("utf-8")
    for addr in ["255.255.255.255", "192.168.1.255", "192.168.0.255",
                 "10.0.0.255", "172.16.0.255"]:
        try:
            sock.sendto(hello, (addr, port))
        except Exception:
            pass
    # 收应答
    found = {}
    deadline = time.time() + (a.timeout or 10)
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
            pkt = json.loads(data.decode("utf-8"))
            if pkt.get("proto") == "fleet" and pkt.get("name", "") != "discover":
                name = pkt["name"]
                found[name] = {"ip": addr[0], "port": pkt.get("port", 3100),
                               "caps": pkt.get("caps", {})}
                print(ok("  [+] %-16s http://%s:%d  caps=%s" % (
                    name, addr[0], pkt.get("port", 3100),
                    json.dumps(pkt.get("caps", {})))))
        except _sock.timeout:
            break
        except Exception:
            continue
    sock.close()
    if not found:
        print(warn("[!] ") + "未发现任何 fleet 节点")
    else:
        print(ok("[*] ") + "发现 %d 个节点" % len(found))
    return 0



# ---------------------------------------------------------------- v5.2 命令

def _local_mcp(a):
    """返回本机节点的 (name, MCPClient) 或 None。"""
    me = local_node(getattr(a, "self_url", None))
    if not me:
        return None
    return me, MCPClient(me["url"], getattr(a, "timeout", 30),
                         token=me.get("token") or "", insecure=True)


def _http_json(url, token="", timeout=10, insecure=False):
    import urllib.request as _ur
    req = _ur.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with _opener_for(url, insecure=insecure).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_join(a):
    """v5.2: 解析 fleet://join/<...> 邀请并配对（节点未装则给安装命令）。"""
    uri = (getattr(a, "uri", "") or "").strip()
    if not uri.startswith("fleet://join/"):
        return die("邀请格式不对，应为 fleet://join/...（可用 fleet invite 生成）")
    try:
        from fleet_core import Invite
    except Exception as e:
        return die("缺少 fleet_core，无法校验邀请：%s" % e)
    try:
        inv = Invite.parse(uri)
    except Exception as e:
        return die("邀请无效或已过期：%s" % e)
    print(ok("[+] ") + "邀请来自 %s（指纹 %s）" % (
        getattr(inv, "name", "?"), getattr(inv, "fingerprint", "?")))
    me = local_node(getattr(a, "self_url", None))
    if not me and getattr(a, "auto_install", False):
        installer = _installer_path()
        if not installer:
            return die("找不到 fleet_install.py；请先安装节点再 join")
        print(dim("    本机没有节点，自动安装并加入 ..."))
        rc = subprocess.call([sys.executable, installer, "--join", uri])
        if rc != 0:
            return die("自动安装失败（退出码 %s）" % rc)
        me = local_node(getattr(a, "self_url", None))
    if not me:
        print(warn("[i] ") + "本机还没有节点。先安装再加入：")
        print("    sh scripts/install.sh --join '%s'" % uri)
        print("    或：fleet join '%s' --auto-install" % uri)
        return 1
    target = "%s@http://%s:%s/mcp" % (inv.name, inv.host, inv.port)
    ns = argparse.Namespace(target=target, name=inv.name,
                            token=getattr(inv, "token", ""),
                            port=inv.port, timeout=getattr(a, "timeout", 30),
                            self_url=getattr(a, "self_url", None), one_way=False)
    print(dim("    正在配对 %s ..." % target))
    return cmd_pair(ns)


def cmd_nodes(a):
    """列出本机节点认识的所有对等节点。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点；先 sh scripts/install.sh --name <名字>")
    me, client = got
    try:
        text, is_err = client.call("peers", {"check": bool(getattr(a, "check", False))})
    except MCPError as e:
        return die("读取节点失败：%s" % e)
    if is_err:
        return die(text.strip())
    print(text)
    return 0


def cmd_status(a):
    """本机节点健康 + 身份摘要。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, _client = got
    base = me["url"].rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    health = _http_json(base + "/fleet/health", me.get("token") or "",
                        insecure=True)
    ident = _http_json(base + "/fleet/identity", me.get("token") or "",
                       insecure=True)
    print("节点      : %s (%s)" % (health.get("name"), health.get("version")))
    print("节点 ID   : %s" % ident.get("node_id"))
    print("指纹      : %s" % ident.get("fingerprint"))
    print("信任等级  : %s" % ident.get("trust"))
    print("对端数量  : %s" % health.get("peers"))
    print("收件箱    : %s 条 / 待投递 %s" % (health.get("inbox"), health.get("outbox")))
    print("运行时长  : %ss" % health.get("uptime_s"))
    return 0


def cmd_metrics(a):
    """打印本机节点 Prometheus 指标。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, _client = got
    base = me["url"].rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    import urllib.request as _ur
    req = _ur.Request(base + "/fleet/metrics")
    if me.get("token"):
        req.add_header("Authorization", "Bearer " + me["token"])
    try:
        with _opener_for(base, insecure=True).open(req, timeout=10) as resp:
            sys.stdout.write(resp.read().decode("utf-8"))
    except Exception as e:
        return die("读取指标失败：%s" % e)
    return 0


def cmd_audit(a):
    """校验本机节点审计哈希链。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, client = got
    try:
        text, is_err = client.call("fleet_audit_verify",
                                   {"tail": int(getattr(a, "tail", 20))})
    except MCPError as e:
        return die("审计校验失败：%s" % e)
    print(text)
    return 1 if is_err else 0


def cmd_token(a):
    """轮换本机节点访问 token（旧 token 立即失效）。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, client = got
    if not getattr(a, "yes", False):
        try:
            ans = input("轮换后旧 token 立即失效，确认？(输入 yes 继续): ").strip()
        except Exception:
            ans = ""
        if ans != "yes":
            print("已取消。")
            return 1
    try:
        text, is_err = client.call("fleet_token_rotate", {"confirm": me["name"]})
    except MCPError as e:
        return die("轮换失败：%s" % e)
    print(text)
    return 1 if is_err else 0



def cmd_send(a):
    """v5.2: 通过本机节点给另一个节点/agent 发消息（在线直投或离线信箱）。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, client = got
    try:
        text, is_err = client.call("msg_send", {
            "from": getattr(a, "sender", "") or "cli-" + me["name"],
            "to": a.to, "text": a.text,
            "topic": getattr(a, "topic", "") or ""}, timeout=max(a.timeout, 30))
    except MCPError as e:
        return die("发送失败：%s" % e)
    print(text)
    return 1 if is_err else 0


def cmd_logs(a):
    """v5.2: 查看本机节点日志尾部（安装器日志或 systemd journal）。"""
    me = local_node(getattr(a, "self_url", None))
    if not me:
        return die("本机没有安装节点")
    n = int(getattr(a, "tail", 100) or 100)
    candidates = [
        os.path.join(me["dir"], "data", "logs", "agent.log"),
        os.path.join(me["dir"], "data", "node.log"),
        os.path.join(me["dir"], "agent.log"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-n:]
                sys.stdout.write("".join(lines))
                return 0
            except Exception as e:
                return die("读取 %s 失败：%s" % (path, e))
    # 没有日志文件时尝试 journalctl
    try:
        return subprocess.call(["journalctl", "-u", "mcp-fleet-" + me["name"],
                                "-n", str(n), "--no-pager"])
    except Exception:
        return die("未找到日志文件（%s）" % ", ".join(candidates))



# ---------------------------------------------------------------- v5.2 体验命令

def _show_qr(uri):
    """终端二维码：优先 qrencode，缺失时给出安装提示。"""
    exe = shutil.which("qrencode")
    if not exe:
        print(warn("[i] ") + "未安装 qrencode，无法显示二维码；可复制上面的 URI，"
              "或安装：brew install qrencode / apt install qrencode / pkg install qrencode")
        return False
    try:
        subprocess.call([exe, "-t", "ANSIUTF8", uri])
        return True
    except Exception as e:
        print(warn("[i] ") + "二维码渲染失败：%s" % e)
        return False


def _installer_path():
    """定位 fleet_install.py：优先同目录（仓库/安装目录），其次节点目录。"""
    local = os.path.join(HERE, "fleet_install.py")
    if os.path.isfile(local):
        return local
    me = local_node()
    if me:
        cand = os.path.join(me["dir"], "fleet_install.py")
        if os.path.isfile(cand):
            return cand
    return ""


def _local_node_install_info(me):
    """从本机节点配置/manifest 推出升级所需的 name/dir/port/data-dir/service。"""
    d = me.get("dir") or ""
    cfg = {}
    try:
        cfg = load_json_config(os.path.join(d, "mcp_agent_config.json"))
    except Exception:
        cfg = {}
    manifest = {}
    candidates = []
    if cfg.get("data_dir"):
        candidates.append(os.path.join(cfg["data_dir"], "manifest.json"))
    candidates.append(os.path.join(d, "data", "manifest.json"))
    candidates.append(os.path.join(d, ".fleet-manifest.json"))
    for cand in candidates:
        try:
            with open(cand, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("manifest_path"), str):   # pointer 文件
                with open(data["manifest_path"], encoding="utf-8") as f2:
                    data = json.load(f2)
            if isinstance(data, dict) and (data.get("install_dir") or data.get("version")):
                manifest = data
                break
        except Exception:
            continue
    port = cfg.get("port") or manifest.get("port") or 3100
    data_dir = cfg.get("data_dir") or manifest.get("data_dir") or os.path.join(d, "data")
    service = manifest.get("service") or ""
    return {"name": me.get("name") or cfg.get("name") or "",
            "dir": d, "port": port, "data_dir": data_dir, "service": service}


# refs/heads/main 路径可绕过 raw.githubusercontent 的 CDN 旧缓存
REMOTE_INSTALL_SH = ("https://raw.githubusercontent.com/guoxpeng/"
                     "skill-mcp-fleet/refs/heads/main/scripts/install.sh")
REMOTE_API_COMMIT = ("https://api.github.com/repos/guoxpeng/"
                     "skill-mcp-fleet/commits/main")


def _fetch_remote_installer():
    """下载最新 install.sh 到临时文件（后续它自己下载并校验 release 包）。

    返回路径或空串；不打印 URL 内容。失败时调用方回退到本地安装器。
    """
    data = b""
    urls = []
    # 1) 先问 GitHub API 要 main 的 commit SHA，再按 SHA 取 install.sh：
    #    immutable URL 不受 raw CDN 旧缓存影响（/main/ 与 /refs/heads/main/ 都可能旧）
    try:
        req = urllib.request.Request(
            REMOTE_API_COMMIT, headers={"User-Agent": "mcp-fleet-update",
                                        "Accept": "application/vnd.github+json"})
        with _opener_for(REMOTE_API_COMMIT).open(req, timeout=15) as resp:
            sha = json.loads(resp.read().decode("utf-8")).get("sha") or ""
        if sha and all(c in "0123456789abcdef" for c in sha.lower()) and len(sha) >= 7:
            urls.append("https://raw.githubusercontent.com/guoxpeng/"
                        "skill-mcp-fleet/%s/scripts/install.sh" % sha)
    except Exception:
        pass
    urls.extend([REMOTE_INSTALL_SH,
                 REMOTE_INSTALL_SH.replace("/refs/heads/main/", "/main/")])
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mcp-fleet-update"})
            with _opener_for(url).open(req, timeout=20) as resp:
                candidate = resp.read()
            if len(candidate) >= 1000 and b"fleet_install.py" in candidate:
                data = candidate
                break
        except Exception:
            continue
    if not data:
        return ""
    try:
        # Termux/精简系统可能没有可写的 /tmp：优先落到用户 HOME
        try:
            fd, path = tempfile.mkstemp(prefix="fleet-install-", suffix=".sh",
                                        dir=os.path.expanduser("~"))
        except OSError:
            fd, path = tempfile.mkstemp(prefix="fleet-install-", suffix=".sh")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path
    except Exception:
        return ""


def cmd_update(a):
    """v5.2.0: 升级本机节点（默认从远端拉最新发布包；--offline 用本地）。"""
    installer = _installer_path()
    if not installer:
        return die("找不到 fleet_install.py；请重新下载安装包，或手工执行 "
                   "python3 scripts/fleet_install.py --upgrade")
    me = local_node(getattr(a, "self_url", None))
    if not me:
        return die("本机没有已安装的节点；fleet update 只用于升级/回滚本机节点")
    info = _local_node_install_info(me)
    if not info.get("name") or not info.get("dir"):
        return die("无法确定本机节点信息（name/dir）；请检查安装目录与配置")
    common = ["--name", info["name"], "--port", str(info["port"]),
              "--dir", info["dir"], "--data-dir", info["data_dir"]]
    if info.get("service"):
        common += ["--service", info["service"]]
    if getattr(a, "json", False):
        common.append("--json")

    remote = ""
    if (not getattr(a, "rollback", False) and not getattr(a, "offline", False)
            and os.name != "nt" and shutil.which("sh")):
        remote = _fetch_remote_installer()
    if remote:
        argv = ["sh", remote, "--upgrade"] + common
    else:
        argv = [sys.executable, installer,
                "--rollback" if getattr(a, "rollback", False) else "--upgrade"
                ] + common
    print(dim("    目标节点: %s (%s, port %s, service %s)"
              % (info["name"], info["dir"], info["port"],
                 info.get("service") or "auto")))
    print(dim("    方式: %s" % ("远端最新发布包（SHA256 校验）"
                                if remote else "本地安装器（离线/下载失败回退）")))
    print(dim("    执行: %s" % " ".join(argv)))
    env = dict(os.environ)
    if not env.get("TMPDIR"):
        tmp = os.path.expanduser("~/.cache/fleet-tmp")
        try:
            os.makedirs(tmp, exist_ok=True)
            env["TMPDIR"] = tmp
        except OSError:
            pass
    try:
        return subprocess.call(argv, env=env)
    finally:
        if remote:
            try:
                os.remove(remote)
            except OSError:
                pass


# ------------------------------------------------ 脚本自升级（fleet up）
# 与 `fleet update` 的分工：
#   fleet update  = 升级**节点运行时**（走安装器 + Release 包 + SHA256）
#   fleet up      = 升级**本地源码**（走 git 或 tarball），装的是 CLI 本体
# 为什么两个都要：`fleet` 是用户每天敲的那个命令，它必须能自己更新；
# 而节点是常驻服务，升级要停服务、要校验、要回滚，风险等级不一样。
REPO_SLUG = "guoxpeng/skill-mcp-fleet"

# 只替换这些代码文件。**永不触碰**配置/身份/数据——升级不能把人家的节点升没了。
CODE_FILES = [
    "mcp_agent.py", "fleet_proto.py", "fleet_core.py", "fleet_agent.py",
    "fleet_install.py", "fleet.py", "dashboard_html.py", "fleet_watch.py",
    "install.sh", "mcp_agent_config.example.json",
]
# 这些即使出现在升级包里也拒绝覆盖（双保险，白名单本来就不会碰）
RUNTIME_FILES = [
    "mcp_agent_config.json", "identity.json", "nodes.json", "peers.json",
    "audit.log", "port.txt", "current_url.txt", "mailbox.key", "mailbox.json",
]


def parse_version(s):
    """'5.2.1' → (5, 2, 1)；不是三段式返回 None。

    必须按数字比，不能按字符串比：字符串比较会认为 "5.10.0" < "5.9.0"，
    于是「远端有新版」被判定成「本地更新」，升级功能直接失效。
    """
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)\s*$", s or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def ver_cmp(a, b):
    """比较两个版本串，返回 -1/0/1；任一侧非法返回 None。"""
    ta, tb = parse_version(a), parse_version(b)
    if ta is None or tb is None:
        return None
    return (ta > tb) - (ta < tb)


def _code_dir(root):
    """源码仓库里代码在 scripts/；安装后的节点目录里代码在顶层。"""
    if os.path.isfile(os.path.join(root, "scripts", "mcp_agent.py")):
        return os.path.join(root, "scripts")
    if os.path.isfile(os.path.join(root, "mcp_agent.py")):
        return root
    return ""


def _repo_root():
    """fleet.py 所在目录的上一级（仓库根）；安装目录下则就是该目录本身。"""
    d = HERE
    if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(
            os.path.join(d, "..", "SKILL.md")):
        return os.path.dirname(d) if os.path.basename(d) == "scripts" else d
    if os.path.isfile(os.path.join(d, "mcp_agent.py")):
        return d
    return os.path.dirname(d)


def _git_run(root, *args, **kw):
    """跑一条 git 命令，返回 (rc, stdout, stderr)。找不到 git 返回 rc=127。"""
    exe = shutil.which("git")
    if not exe:
        hint = ("未安装 git。macOS 上执行：xcode-select --install"
                if sys.platform == "darwin"
                else "未安装 git（Debian/Ubuntu: apt install git；"
                     "OpenWrt: opkg install git-http）")
        return 127, "", hint
    env = dict(os.environ)
    # 避免 git 在无 tty 时卡住等密码 / 弹编辑器
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "true")
    env.setdefault("GIT_PAGER", "cat")
    try:
        r = subprocess.run([exe] + list(args), cwd=root, capture_output=True,
                           text=True, timeout=kw.get("timeout", 90), env=env)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "超时"
    except Exception as e:
        return 1, "", str(e)


def _is_git_repo(root):
    return os.path.isdir(os.path.join(root, ".git")) and bool(shutil.which("git"))


def _version_in_source(text):
    """从 fleet.py 源码里抠出 VERSION。"""
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text or "", re.M)
    return m.group(1) if m else ""


def _mirror_urls(path, ref="main"):
    """给同一个仓库文件生成多条下载地址（国内直连 GitHub 经常不通）。"""
    raw = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO_SLUG, ref, path)
    return [
        raw,
        "https://cdn.jsdelivr.net/gh/%s@%s/%s" % (REPO_SLUG, ref, path),
        "https://ghfast.top/" + raw,
        "https://ghproxy.net/" + raw,
        "https://gh-proxy.com/" + raw,
    ]


def _http_get(url, timeout=20, limit=4 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": "mcp-fleet-up/%s" % VERSION})
    with _opener_for(url).open(req, timeout=timeout) as resp:
        return _read_capped(resp, limit)


def _remote_version_git(root, ref):
    """git 路线：fetch 后直接读 FETCH_HEAD 里的 fleet.py，返回 (版本, sha, 说明)。

    这里**不能**用 `--depth=1`：浅拉取会给 FETCH_HEAD 造一个没有共同祖先的
    历史，紧接着的 `merge --ff-only` 必然报 "refusing to merge unrelated
    histories"，升级 100% 失败。仓库总共 1MB 出头，全量 fetch 的代价可以忽略。
    """
    rc, _, err = _git_run(root, "fetch", "--quiet", "origin", ref, timeout=120)
    if rc != 0:
        return "", "", err or ("git fetch 失败（rc=%d）" % rc)
    # 浅克隆（`git clone --depth=1`，慢网/CI 常见）的仓库没有共同祖先，
    # 后面的 `merge --ff-only` 一定失败。这里先补全历史，一次修好不再复发。
    rc_s, shallow, _ = _git_run(root, "rev-parse", "--is-shallow-repository")
    if rc_s == 0 and shallow.strip() == "true":
        _git_run(root, "fetch", "--quiet", "--unshallow", "origin", ref, timeout=300)
        _git_run(root, "fetch", "--quiet", "origin", ref, timeout=120)
    rc, sha, _ = _git_run(root, "rev-parse", "FETCH_HEAD")
    sha = sha.strip()
    if rc != 0 or not sha:
        return "", "", "拿不到 FETCH_HEAD"
    rel = os.path.relpath(os.path.join(HERE, "fleet.py"), root).replace(os.sep, "/")
    rc, blob, err = _git_run(root, "show", "%s:%s" % (sha, rel))
    if rc != 0:
        return "", sha, "远端没有 %s（%s）" % (rel, err)
    ver = _version_in_source(blob)
    return ver, sha, "" if ver else "远端 fleet.py 里没有 VERSION"


def _remote_version_http(ref):
    """非 git 路线：先按 commit sha 取（不受 CDN 旧缓存影响），再按 ref 取。"""
    path = "scripts/fleet.py"
    candidates = []
    try:
        api = "https://api.github.com/repos/%s/commits/%s" % (REPO_SLUG, ref)
        data = json.loads(_http_get(api, timeout=15, limit=512 * 1024).decode("utf-8"))
        sha = (data.get("sha") or "").strip()
        if sha and all(c in "0123456789abcdef" for c in sha.lower()):
            candidates.extend(_mirror_urls(path, sha))
    except Exception:
        pass
    candidates.extend(_mirror_urls(path, ref))
    last = ""
    for url in candidates:
        try:
            body = _http_get(url, timeout=20, limit=2 * 1024 * 1024)
            ver = _version_in_source(body.decode("utf-8", "ignore"))
            if ver:
                return ver, "", ""
            last = "下载到的 fleet.py 里没有 VERSION"
        except Exception as e:
            last = str(e)
    return "", "", last or "所有下载地址都失败"


def _up_state_path():
    return os.path.join(os.path.expanduser("~"), ".mcp-fleet", "up-state.json")


def _load_up_state():
    try:
        with open(_up_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_up_state(state):
    path = _up_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


#: 升级备份保留份数。每次升级整目录复制（约 1MB），不清理会一直涨——
#: 真机上一台设备升了 9 次就占了 9.1MB，软路由的 overlay 经不起这么堆。
UP_BACKUP_KEEP = 5


def _prune_backups(base, keep=None):
    """只保留最近 ``keep`` 份备份，其余删掉（按 mtime 排序）。

    ``keep`` 默认值在**运行时**取 :data:`UP_BACKUP_KEEP`——写成默认参数
    ``keep=UP_BACKUP_KEEP`` 会在函数定义时就把值绑死，之后改模块变量不生效。
    """
    if keep is None:
        keep = UP_BACKUP_KEEP
    try:
        items = []
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isdir(path):
                try:
                    items.append((os.path.getmtime(path), path))
                except OSError:
                    continue
        if len(items) <= keep:
            return 0
        items.sort(reverse=True)
        removed = 0
        for _, path in items[keep:]:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        return removed
    except Exception:
        return 0


def _backup_code_dir(cdir, stamp, tag=""):
    """把当前代码目录整份复制到仓库**外面**的备份区，返回备份路径。

    为什么不能放在代码目录旁边：备份目录会立刻出现在 `git status` 里，
    下一次 `fleet up` 就会被「工作区有未提交改动」挡住，永远升不了级；
    而且整目录复制会把自己也复制进去，越滚越大。
    所以统一放到 ~/.mcp-fleet/up-backups/ 下，与仓库彻底隔离。
    """
    base = os.path.join(os.path.expanduser("~"), ".mcp-fleet", "up-backups")
    name = "%s-%s" % (tag or os.path.basename(os.path.dirname(cdir)), stamp)
    dst = os.path.join(base, name)
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(base, exist_ok=True)
        shutil.copytree(cdir, dst, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _prune_backups(base)
        return dst
    except Exception:
        return ""


def _install_from_dir(src_dir, cdir):
    """把 src_dir 里的代码文件原子替换进 cdir。返回 (替换数, 跳过数, 错误)。"""
    done = skipped = 0
    for name in CODE_FILES:
        if name in RUNTIME_FILES:
            skipped += 1
            continue
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            skipped += 1
            continue
        dst = os.path.join(cdir, name)
        tmp = dst + ".new"
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)      # 原子替换：断电也不会留半个文件
            done += 1
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return done, skipped, "替换 %s 失败：%s" % (name, e)
    return done, skipped, ""


def _verify_code(cdir, expect):
    """升级后用子进程跑一次 --version，确认新代码真的能跑。"""
    target = os.path.join(cdir, "fleet.py")
    if not os.path.isfile(target):
        return False, "找不到 %s" % target
    try:
        r = subprocess.run([sys.executable, target, "--version"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, "自检无法执行：%s" % e
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        return False, "新版本 --version 退出码 %d：%s" % (r.returncode, out[:200])
    if expect and expect not in out:
        return False, "自检版本不符：期望 %s，实际 %s" % (expect, out[:120])
    return True, out


def cmd_up(a):
    """v5.2.1: 从 git 拉最新代码升级本地脚本。

    三条硬规则：
      1. 只升代码，不碰配置/身份/数据（CODE_FILES 白名单）。
      2. 升级前整目录备份，升级后跑一次 --version 自检；自检不过自动回滚。
      3. 有未提交改动就拒绝升级，而不是替用户 stash/reset —— 那是开发者的代码，
         不是我们能替他决定去留的东西（--force 才强推）。
    """
    root = _repo_root()
    cdir = _code_dir(root)
    if not cdir:
        return die("找不到源码目录（既没有 scripts/mcp_agent.py 也没有 mcp_agent.py）")
    ref = (getattr(a, "ref", "") or "main").strip()
    check_only = bool(getattr(a, "check", False))
    force = bool(getattr(a, "force", False))
    restart = not bool(getattr(a, "no_restart", False))

    print("=" * 60)
    print("  MCP Fleet 自升级 · 当前 v%s" % VERSION)
    print("=" * 60)
    print(dim("    代码目录: %s" % cdir))
    print(dim("    目标分支: %s" % ref))

    is_git = _is_git_repo(root)
    print(dim("    升级方式: %s" % ("git（可回滚到指定提交）" if is_git
                                    else "下载源码包（非 git 安装）")))

    # 1) 问远端
    remote_ver, remote_sha, why = ("", "", "")
    if is_git:
        remote_ver, remote_sha, why = _remote_version_git(root, ref)
        if not remote_ver:
            # git 路线失败（常见于没配 remote / 无外网），退回 HTTP 探一下版本
            print(warn("    git 取版本失败：%s" % why))
            remote_ver, _, why = _remote_version_http(ref)
    else:
        remote_ver, _, why = _remote_version_http(ref)

    if not remote_ver:
        print(err("[x] ") + "取不到远端版本：%s" % why)
        print(dim("    可能原因：没联网、GitHub 不可达、分支名不对。"))
        print(dim("    手动升级：git pull  或  sh scripts/install.sh --offline ."))
        return 1

    cmp = ver_cmp(VERSION, remote_ver)
    if cmp is None:
        return die("版本号不是三段式，无法比较：本地 %r 远端 %r" % (VERSION, remote_ver))

    print(dim("    远端版本: v%s%s" % (remote_ver,
                                      ("  (%s)" % remote_sha[:8]) if remote_sha else "")))
    if cmp == 0 and not force:
        print(ok("[+] ") + "已是最新（v%s），不用升级。" % VERSION)
        if getattr(a, "json", False):
            print(json.dumps({"current": VERSION, "remote": remote_ver,
                              "updated": False}, ensure_ascii=False))
        return 0
    if cmp > 0 and not force:
        print(warn("[!] ") + "本地 v%s 比远端 v%s 还新（可能你在自己开发）。"
              % (VERSION, remote_ver))
        print(dim("    确实要用远端覆盖本地，加 --force。"))
        return 0

    if check_only:
        print()
        print(ok("有新版：v%s → v%s" % (VERSION, remote_ver)))
        print(dim("    执行升级：fleet up"))
        if getattr(a, "json", False):
            print(json.dumps({"current": VERSION, "remote": remote_ver,
                              "updated": False, "available": True},
                             ensure_ascii=False))
        return 0

    # 2.5) 上次这个版本升级失败过？别再自动重试一遍折磨用户
    state = _load_up_state()
    if (not force and state.get("bad_version") == remote_ver
            and state.get("bad_sha") in ("", None, remote_sha)):
        print(warn("[!] ") + "远端 v%s 上次升级后自检失败，已自动跳过。" % remote_ver)
        why2 = (state.get("reason") or "未知").replace("\n", " ")
        print(dim("    原因：%s" % (why2[:120] + ("…" if len(why2) > 120 else ""))))
        print(dim("    等上游修好，或确认要重试：fleet up --force"))
        return 1

    # 2) 未提交改动保护（仅 git 路线）
    if is_git:
        rc, dirty, _ = _git_run(root, "status", "--porcelain")
        if rc == 0 and dirty.strip() and not force:
            print(err("[x] ") + "工作区有未提交改动，拒绝升级（避免覆盖你的代码）：")
            for line in dirty.strip().splitlines()[:10]:
                print("      " + line)
            print(dim("    先提交/暂存，或加 --force 用远端覆盖。"))
            return 1

    # 3) 备份
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = _backup_code_dir(cdir, stamp)
    if not backup:
        return die("备份失败（%s 不可写？），已中止升级" % cdir)
    print(dim("    已备份: %s" % backup))

    old_head = ""
    if is_git:
        rc, old_head, _ = _git_run(root, "rev-parse", "HEAD")

    # 4) 落地
    if is_git:
        rc, out, err2 = _git_run(root, "merge", "--ff-only", "FETCH_HEAD", timeout=120)
        if rc != 0:
            print(err("[x] ") + "git 合并失败（本地有分叉提交？）：%s" % (err2 or out))
            print(dim("    代码未改动。需要强推可执行：git reset --hard origin/%s" % ref))
            return 1
    else:
        import tarfile
        tmpdir = tempfile.mkdtemp(prefix="fleet-up-")
        try:
            tar_path = os.path.join(tmpdir, "src.tar.gz")
            url = "https://github.com/%s/archive/refs/heads/%s.tar.gz" % (REPO_SLUG, ref)
            got = False
            for cand in [url, "https://ghfast.top/" + url, "https://ghproxy.net/" + url]:
                try:
                    data = _http_get(cand, timeout=60, limit=40 * 1024 * 1024)
                    with open(tar_path, "wb") as f:
                        f.write(data)
                    got = True
                    break
                except Exception:
                    continue
            if not got:
                return die("源码包下载失败（GitHub 及镜像都不通）")
            with tarfile.open(tar_path, "r:gz") as tf:
                # 只允许普通文件，防止畸形包用绝对路径/软链写到目录外
                for m in tf.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        return die("源码包含非法路径：%s" % m.name)
                try:
                    # Python 3.12+ 默认会对不安全成员告警；显式 data 过滤更稳。
                    # 3.6–3.11 没有 filter 参数，TypeError 回退。
                    tf.extractall(tmpdir, filter="data")
                except TypeError:
                    tf.extractall(tmpdir)
            subs = [d for d in os.listdir(tmpdir)
                    if os.path.isdir(os.path.join(tmpdir, d))]
            if not subs:
                return die("源码包结构异常（没有顶层目录）")
            src_cdir = _code_dir(os.path.join(tmpdir, subs[0]))
            if not src_cdir:
                return die("源码包里找不到代码文件")
            done, skipped, err3 = _install_from_dir(src_cdir, cdir)
            if err3:
                return die(err3)
            print(dim("    已替换 %d 个代码文件（跳过 %d）" % (done, skipped)))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # 5) 自检 + 自动回滚
    okv, detail = _verify_code(cdir, remote_ver)
    if not okv:
        print(err("[x] ") + "新版本自检失败，正在回滚…（%s）" % detail)
        if is_git and old_head:
            _git_run(root, "reset", "--hard", old_head)
        else:
            _install_from_dir(backup, cdir)
        ok2, d2 = _verify_code(cdir, VERSION)
        print((ok("[+] ") if ok2 else err("[x] ")) +
              ("已回滚到 v%s（%s）" % (VERSION, d2) if ok2
               else "回滚后仍异常：%s，请手工处理 %s" % (d2, backup)))
        if ok2:
            st = _load_up_state()
            st["bad_version"] = remote_ver
            st["bad_sha"] = remote_sha
            st["reason"] = detail[:300]
            st["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_up_state(st)
            print(dim("    已记录：v%s 自检不通过，下次 fleet up 会自动跳过。" % remote_ver))
        return 1

    print(ok("[+] ") + "升级完成：v%s → v%s" % (VERSION, remote_ver))
    print(dim("    备份保留在 %s（确认无误后可删）" % backup))
    st = _load_up_state()
    if st.pop("bad_version", None):
        _save_up_state(st)

    # 6) 节点在跑就重启，让新代码真正生效
    if restart:
        me = local_node()
        if me and _local_health(me, timeout=1.5):
            print(dim("    本机节点在运行，重启以加载新代码…"))
            rc = cmd_restart(None)
            if rc != 0:
                print(warn("    节点重启失败，稍后手动：fleet restart"))
        elif me:
            print(dim("    本机节点未运行；下次 fleet 会自动拉起。"))

    if getattr(a, "json", False):
        print(json.dumps({"current": VERSION, "remote": remote_ver,
                          "updated": True, "backup": backup},
                         ensure_ascii=False))
    return 0


def cmd_identity(a):
    """v5.2: 身份备份/恢复（加密导出，避免重新配对全网）。"""
    action = getattr(a, "action", "show") or "show"
    me = local_node(getattr(a, "self_url", None))
    if not me:
        return die("本机没有安装节点")
    ident_dir = me["dir"]
    ident = node_identity(ident_dir)
    if action == "show":
        print("节点    : %s" % me.get("name"))
        print("node_id : %s" % ident.get("node_id"))
        print("指纹    : %s" % ident.get("fingerprint"))
        print("公钥    : %s" % (ident.get("public_key") or "")[:24] + "...")
        return 0
    id_path = os.path.join(ident_dir, "identity.json")
    try:
        cfg = load_json_config(os.path.join(ident_dir, "mcp_agent_config.json"))
        if cfg.get("data_dir"):
            id_path = os.path.join(cfg["data_dir"], "identity.json")
    except Exception:
        pass
    if action == "export":
        return _identity_export(a, id_path, ident)
    if action == "import":
        return _identity_import(a, id_path)
    return die("未知 identity 动作：%s（可用 show/export/import）" % action)


def _identity_passphrase(a, confirm=False):
    p = getattr(a, "passphrase", "") or ""
    if p:
        return p
    try:
        import getpass
        p = getpass.getpass("请输入备份口令: ")
        if confirm and p != getpass.getpass("再次输入口令: "):
            raise ValueError("两次口令不一致")
        return p
    except Exception as e:
        raise SystemExit("读取口令失败：%s" % e)


def _identity_export(a, id_path, ident):
    try:
        import fleet_core as _core
    except Exception as e:
        return die("缺少 fleet_core，无法加密导出：%s" % e)
    if not os.path.isfile(id_path):
        return die("找不到身份文件：%s" % id_path)
    passphrase = _identity_passphrase(a)
    if len(passphrase) < 6:
        return die("口令至少 6 位")
    with open(id_path, encoding="utf-8") as f:
        payload = f.read()
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 200000)
    blob = _core.seal(key, payload.encode("utf-8"), aad=b"fleet-identity")
    out = {
        "v": 1, "kdf": "pbkdf2-sha256-200000",
        "salt": base64.b64encode(salt).decode("ascii"),
        "blob": blob,
        "node_id": ident.get("node_id"), "fingerprint": ident.get("fingerprint"),
        "exported_at": int(time.time()),
    }
    dest = getattr(a, "out", "") or os.path.join(
        os.path.expanduser("~"), "fleet-identity-%s.json" % ident.get("node_id", "node"))
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    print(ok("[+] ") + "身份已加密导出：%s（0600）" % dest)
    print("    口令丢失将无法恢复；请与备份文件分开保存。")
    return 0


def _identity_import(a, id_path):
    try:
        import fleet_core as _core
    except Exception as e:
        return die("缺少 fleet_core，无法解密：%s" % e)
    src = getattr(a, "from_file", "") or ""
    if not src or not os.path.isfile(src):
        return die("请用 --from <导出的 json> 指定备份文件")
    try:
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
        salt = base64.b64decode(data["salt"])
        key = hashlib.pbkdf2_hmac("sha256", _identity_passphrase(a).encode("utf-8"),
                                  salt, 200000)
        payload = _core.open_sealed(key, data["blob"], aad=b"fleet-identity")
        ident_obj = json.loads(payload.decode("utf-8"))
        assert ident_obj.get("private_key_b64") and ident_obj.get("public_key_b64")
    except SystemExit:
        raise
    except Exception:
        return die("导入失败：口令错误或文件损坏")
    if os.path.isfile(id_path):
        bak = id_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(id_path, bak)
            print(dim("    旧身份已备份：%s" % bak))
        except OSError:
            pass
    tmp = id_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ident_obj, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, id_path)
    print(ok("[+] ") + "身份已恢复：%s（%s）"
          % (ident_obj.get("node_id"), ident_obj.get("fingerprint")))
    print("    ⚠ 请重启节点；旧配对关系可能需重新建立（公钥已变/未变均建议核对指纹）。")
    return 0


INTENTS = [
    ("disk", ("磁盘", "空间", "硬盘", "存储", "满了", "占用", "df"),
     "检查磁盘占用并找出大目录",
     [("sysinfo", {}), ("exec", {"command": "df -h"})]),
    ("network", ("网络", "慢", "不通", "断", "延迟", "dns", "丢包", "网速"),
     "定位网络瓶颈：接口/路由/延迟/DNS",
     [("net_check", {"action": "listen"}),
      ("exec", {"command": "ip route" if sys.platform != "darwin" else "netstat -rn | head"}),
      ("net_check", {"action": "dns", "host": "www.baidu.com"})]),
    ("docker", ("docker", "容器", "镜像", "compose"),
     "查看容器状态与异常日志",
     [("docker_ps", {"all": True})]),
    ("service", ("服务", "挂了", "重启", "systemd", "进程"),
     "检查服务/进程状态",
     [("ps", {"all": True})]),
    ("backup", ("备份", "归档", "同步"),
     "制定备份计划（写操作，需人工确认）",
     [("sysinfo", {}), ("exec", {"command": "df -h"})]),
    ("health", ("健康", "体检", "概览", "状态", "怎么样"),
     "系统整体健康检查",
     [("sysinfo", {}), ("fleet_doctor", {})]),
]


def _classify_ask(text):
    low = (text or "").lower()
    for name, keys, goal, steps in INTENTS:
        if any(k.lower() in low for k in keys):
            return name, goal, steps
    return "general", "先收集系统事实，再决定下一步", [("sysinfo", {}), ("fleet_doctor", {})]


def cmd_ask(a):
    """v5.2: 自然语言入口：给普通用户生成计划/证据/可直接粘贴的提示词。"""
    text = (getattr(a, "text", "") or "").strip()
    if not text:
        return die("用法：fleet ask \"帮我看下 NAS 磁盘为什么满了\" [--run]")
    me = local_node(getattr(a, "self_url", None))
    if not me:
        return die("本机没有运行中的节点；先安装：sh scripts/install.sh --name <名字>")
    intent, goal, steps = _classify_ask(text)
    print("任务    : %s" % text)
    print("意图    : %s" % intent)
    print("目标    : %s" % goal)
    print("计划    :")
    for index, (tool, args) in enumerate(steps, 1):
        detail = args.get("command") or args.get("host") or ""
        print("  %d. %s %s" % (index, tool, ("（%s）" % detail) if detail else ""))
    print("验收    : 输出包含可核对的事实（容量/延迟/容器状态/错误行），结论引用证据")
    print("")
    prompt = (
        "你在节点 %s 上执行任务：%s。\n"
        "先只读观察（sysinfo/ps/docker_ps/net_check/df），列出证据再给结论；\n"
        "任何删除/重启/改配置前先向我确认；最后按“结论/证据/建议”三段式回复。"
        % (me.get("name"), text))
    print("可直接粘给 AI 客户端：")
    print("  " + prompt)
    if not getattr(a, "run", False):
        print("")
        print(dim("    加 --run 会立即执行上面的只读步骤并汇总证据。"))
        return 0
    print("")
    print("执行结果：")
    client = MCPClient(me["url"], getattr(a, "timeout", 30),
                       token=me.get("token") or "", insecure=True)
    failed = 0
    safe_exec = ("df ", "df", "uptime", "free", "ip route", "ip addr", "netstat",
                 "cat /proc/loadavg", "nproc", "uname")
    for tool, args in steps:
        if tool == "exec" and not str(args.get("command", "")).startswith(safe_exec):
            print("  - 跳过非白名单命令：%r" % args.get("command"))
            continue
        try:
            result, is_err = client.call(tool, args)
        except Exception as e:
            print("  - %s 失败：%s" % (tool, e))
            failed += 1
            continue
        text_out = (result or "").strip()
        print("\n[%s] %s" % (tool, "错误" if is_err else "ok"))
        print("\n".join(text_out.splitlines()[:12]))
        if is_err:
            failed += 1
    return 1 if failed and failed == len(steps) else 0


# ------------------------------------------------ 文件搬运（fleet cp）
# 远程改代码的最小闭环是「拉下来 → 改 → 推回去」。之前只能
# `fleet call --tool read/write --args '{...base64...}'`，没人愿意这么用。
# 这里按 scp 的手感包一层，并且分块传输以适配三处硬上限：
#   主控读响应 2MB、副端收请求 1MB、副端 exec 输出 200KB（max_output_bytes）。
# base64 膨胀 4/3，所以拉取块取 128KB（→174KB 文本）才稳；推送块可以大些。
CP_PULL_CHUNK = 128 * 1024
CP_PUSH_CHUNK = 512 * 1024


def _cp_exec(client, cmd, timeout=120):
    """在副端跑一条命令并返回 stdout；非零退出抛 MCPError。"""
    text, iserr = client.call("exec", {"command": cmd}, timeout=timeout)
    code, out, err = parse_exec(text)
    if iserr or code != 0:
        raise MCPError("远端命令失败（exit %s）：%s"
                       % (code, (err or out).strip()[:300] or "无输出"))
    return out


def _remote_size(client, path):
    """远端文件字节数。GNU stat 与 BSD stat 都试一次。"""
    q = shlex.quote(path)
    out = _cp_exec(client, "stat -c %s " + q + " 2>/dev/null || stat -f %z " + q + " 2>/dev/null")
    line = (out or "").strip().splitlines()
    if not line:
        raise MCPError("读不到远端文件大小：%s" % path)
    try:
        return int(line[0].strip())
    except ValueError:
        raise MCPError("远端 stat 输出异常：%r" % line[0][:100])


def _cp_pull_mode(client, remote, size, timeout):
    """探测副端的 ``download`` 是否支持 offset/length（5.2.2 起）。

    为什么值得多花一次调用：老的拉取路径是 `dd if=... skip=N count=1 | base64`，
    依赖副端同时有 `dd` 和 `base64` 两个命令。busybox 常常没编译 base64 applet，
    于是 OpenWrt / iStoreOS 上 `fleet cp` 拉文件直接报「对端返回被截断」——
    报错还完全指不到真正原因。新的 download 分块走副端 Python 自己读，
    零外部命令依赖。
    """
    if size <= 0:
        return "download"
    want = min(16, size)
    try:
        text, iserr = client.call(
            "download", {"filePath": remote, "offset": 0, "length": want},
            timeout=timeout)
    except Exception:
        return "dd"
    if iserr:
        return "dd"
    try:
        raw = base64.b64decode(re.sub(r"[^A-Za-z0-9+/=]", "", text or ""))
    except Exception:
        return "dd"
    # 老版本会忽略 offset/length 直接返回整个文件
    return "download" if len(raw) == want else "dd"


def _cp_pull_chunk(client, remote, offset, want, timeout, mode):
    """取一块远端数据（已解码的原始字节）。"""
    if mode == "download":
        text, iserr = client.call(
            "download", {"filePath": remote, "offset": offset, "length": want},
            timeout=timeout)
        if iserr:
            raise MCPError("读取分块失败：%s" % str(text)[:300])
        clean = re.sub(r"[^A-Za-z0-9+/=]", "", text or "")
        return base64.b64decode(clean) if clean else b""
    # 兼容老副端：dd 按块取，再交给副端的 base64 编码
    cmd = ("dd if=" + shlex.quote(remote) + " bs=" + str(CP_PULL_CHUNK)
           + " skip=" + str(offset // CP_PULL_CHUNK) + " count=1 2>/dev/null | base64")
    b64 = _cp_exec(client, cmd, timeout=timeout)
    # 只留 base64 字母表：副端的截断提示/告警是中文，混进来会让
    # b64decode 直接抛「non-ASCII」而不是给出可读的错因。
    clean = re.sub(r"[^A-Za-z0-9+/=]", "", b64 or "")
    return base64.b64decode(clean) if clean else b""


def _cp_pull_file(client, remote, local, timeout, quiet=False):
    """分块拉取，任意大小都不会撞上限。"""
    size = _remote_size(client, remote)
    tmp = local + ".fleetpart"
    d = os.path.dirname(os.path.abspath(local))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    got = 0
    mode = _cp_pull_mode(client, remote, size, timeout) if size else "download"
    with open(tmp, "wb") as f:
        while got < size:
            expect = min(CP_PULL_CHUNK, size - got)
            raw = _cp_pull_chunk(client, remote, got, expect, timeout, mode)
            if len(raw) != expect:
                raise MCPError(
                    "对端返回被截断：本块期望 %d 字节，实得 %d 字节。\n"
                    "    副端 max_output_bytes 太小或代理截断了响应；"
                    "调大副端 mcp_agent_config.json 的 max_output_bytes 后重试。"
                    % (expect, len(raw)))
            f.write(raw)
            got += len(raw)
            if not quiet and size > CP_PULL_CHUNK:
                sys.stderr.write("\r  拉取 %d%%（%d/%d 字节）" % (got * 100 // max(size, 1), got, size))
                sys.stderr.flush()
    if not quiet and size > CP_PULL_CHUNK:
        sys.stderr.write("\n")
    if got != size:
        os.unlink(tmp)
        raise MCPError("拉取不完整：期望 %d 字节，实际 %d 字节（对端文件在传输中变了？）"
                       % (size, got))
    os.replace(tmp, local)
    return size


def _cp_push_file(client, local, remote, timeout, sudo=False, quiet=False):
    """分块推送：每块 base64 后走 upload 落成 .fleetpart.N，最后分批 cat 拼回。

    这里有三处不能省，都是踩过或必然会踩的：
      * **流式读**，不整个 `f.read()`：几个 G 的文件会先把客户端撑爆
      * **分批拼装**，不拼成一条超长命令：512KB 一块，1GB 就是 2048 个参数，
        一条 cat 下去撞 ARG_MAX，远端报 "argument list too long"
      * **拼完核对大小**：少一个字节就是坏文件，不能靠「命令没报错」当成功
    """
    parts = []
    total = 0
    try:
        with open(local, "rb") as f:
            idx = 0
            while True:
                chunk = f.read(CP_PUSH_CHUNK)
                if not chunk:
                    break
                part = "%s.fleetpart.%d" % (remote, idx)
                args = {"filePath": part,
                        "content": base64.b64encode(chunk).decode("ascii")}
                if sudo:
                    # 只在真的要提权时才带 sudo 键：执行策略会把参数文本里的
                    # "sudo" 当成提权操作，带上 sudo:false 反而会触发「需要确认」。
                    # 命令行显式给了 --sudo，即视为用户已确认（confirm=true）。
                    args["sudo"] = True
                    args["confirm"] = True
                text, iserr = client.call("upload", args, timeout=timeout)
                if iserr:
                    raise MCPError("上传分块失败：%s" % (text or "")[:300])
                parts.append(part)
                total += len(chunk)
                idx += 1
                if not quiet and total > CP_PUSH_CHUNK:
                    sys.stderr.write("\r  推送 %d 字节（%d 块）" % (total, idx))
                    sys.stderr.flush()
    except Exception:
        # 半路失败要把已经传上去的分块清掉，别在别人的目录里留一堆垃圾
        _cp_clean_parts(client, remote, timeout)
        raise
    if not quiet and total > CP_PUSH_CHUNK:
        sys.stderr.write("\n")

    if total == 0:
        # 空文件走 upload 没有内容可传，只能 shell 里截断；但目标目录未必存在，
        # 不先 mkdir 的话「推空文件」会比「推非空文件」更容易失败——很反直觉。
        _cp_exec(client, "mkdir -p " + shlex.quote(os.path.dirname(remote) or ".")
                 + " && : > " + shlex.quote(remote), timeout=timeout)
        return 0

    batch = 128
    first = True
    for i in range(0, len(parts), batch):
        q = " ".join(shlex.quote(p) for p in parts[i:i + batch])
        _cp_exec(client, "cat " + q + (" > " if first else " >> ")
                 + shlex.quote(remote), timeout=timeout)
        first = False
    _cp_clean_parts(client, remote, timeout)

    got = _remote_size(client, remote)
    if got != total:
        raise MCPError(
            "推送后大小不符：本地 %d 字节，远端 %d 字节。\n"
            "    文件很可能被截断（副端磁盘满 / upload 被限流），不要当成成功。"
            % (total, got))
    # 保留本地权限位：推上去的可执行脚本不该变成 0644
    try:
        mode = os.stat(local).st_mode & 0o777
        _cp_exec(client, "chmod %o %s" % (mode, shlex.quote(remote)), timeout=timeout)
    except Exception:
        pass
    return total


def _cp_clean_parts(client, remote, timeout):
    """删掉 `<remote>.fleetpart.*`。用通配而不是逐个列名——上千个分块逐个
    列出来同样会撞 ARG_MAX。"""
    try:
        _cp_exec(client, "rm -f " + shlex.quote(remote) + ".fleetpart.*",
                 timeout=timeout)
    except Exception:
        pass                    # 清理失败不该盖住真正的错因


def _remote_tar(client, path, timeout):
    """把远端目录打成临时 tar，返回远端 tar 路径。"""
    base = path.rstrip("/")
    parent = os.path.dirname(base) or "/"
    name = os.path.basename(base)
    tmp = "/tmp/.fleet-cp-%s.tgz" % uuid.uuid4().hex[:8]
    _cp_exec(client, "tar czf " + shlex.quote(tmp) + " -C " + shlex.quote(parent)
             + " " + shlex.quote(name), timeout=timeout)
    return tmp


def cmd_cp(a):
    """fleet cp —— 本机与节点之间搬文件/目录（scp 手感）。

    用法：
      fleet cp ./main.py nas:/opt/app/main.py      # 推上去
      fleet cp nas:/opt/app/main.py ./main.py      # 拉下来
      fleet cp -r ./src nas:/opt/app/src           # 目录
    """
    src, dst = a.src, a.dst
    known = discover_nodes(a.agent)
    src_node, src_path = _split_cp_spec(src, known)
    dst_node, dst_path = _split_cp_spec(dst, known)
    if src_node and dst_node:
        return die("两侧都是远端（%s / %s），暂不支持节点之间直接搬运。\n"
                   "    先拉到本机再推上去：fleet cp %s:%s ./tmp && fleet cp ./tmp %s:%s"
                   % (src_node, dst_node, src_node, src_path, dst_node, dst_path))
    if not src_node and not dst_node:
        return die("至少一侧要写成 <节点名>:<路径>，例如 nas:/opt/app/main.py\n"
                   "    已注册节点：%s" % (", ".join(sorted(known)) or "（无，先 fleet list）"))
    node = src_node or dst_node
    try:
        client, url, meta = client_for(node, a.agent, a.port, timeout=a.timeout,
                                       sync=False if a.no_sync else None)
        client.handshake()
    except Exception as e:
        return die("连不上 %s：%s" % (node, e))

    t0 = time.time()
    try:
        if src_node:                                  # 拉：远端 → 本机
            if a.recursive:
                rtmp = _remote_tar(client, src_path, a.timeout)
                tmpdir = tempfile.mkdtemp(prefix="fleet-cp-")
                tarpath = os.path.join(tmpdir, "payload.tgz")
                size = _cp_pull_file(client, rtmp, tarpath, a.timeout, quiet=a.quiet)
                _cp_exec(client, "rm -f " + shlex.quote(rtmp), timeout=a.timeout)
                os.makedirs(dst_path, exist_ok=True)
                rc = subprocess.call(["tar", "xzf", tarpath, "-C", dst_path])
                shutil.rmtree(tmpdir, ignore_errors=True)
                if rc != 0:
                    return die("本地解包失败（tar exit %d）" % rc)
                print(ok("[+] ") + "已拉取目录 %s:%s → %s（%.1f KB，%.1fs）"
                      % (node, src_path, dst_path, size / 1024.0, time.time() - t0))
            else:
                size = _cp_pull_file(client, src_path, dst_path, a.timeout, quiet=a.quiet)
                print(ok("[+] ") + "已拉取 %s:%s → %s（%d 字节，%.1fs）"
                      % (node, src_path, dst_path, size, time.time() - t0))
        else:                                         # 推：本机 → 远端
            if a.recursive:
                if not os.path.isdir(src_path):
                    return die("本地不是目录：%s（-r 只用于目录）" % src_path)
                tmpdir = tempfile.mkdtemp(prefix="fleet-cp-")
                tarpath = os.path.join(tmpdir, "payload.tgz")
                base = src_path.rstrip("/")
                rc = subprocess.call(["tar", "czf", tarpath, "-C",
                                      os.path.dirname(os.path.abspath(base)) or ".",
                                      os.path.basename(base)])
                if rc != 0:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    return die("本地打包失败（tar exit %d）" % rc)
                rtmp = "/tmp/.fleet-cp-%s.tgz" % uuid.uuid4().hex[:8]
                _cp_push_file(client, tarpath, rtmp, a.timeout, sudo=a.sudo, quiet=a.quiet)
                shutil.rmtree(tmpdir, ignore_errors=True)
                _cp_exec(client, "mkdir -p " + shlex.quote(dst_path)
                         + " && tar xzf " + shlex.quote(rtmp) + " -C " + shlex.quote(dst_path)
                         + " && rm -f " + shlex.quote(rtmp), timeout=a.timeout)
                print(ok("[+] ") + "已推送目录 %s → %s:%s（%.1fs）"
                      % (src_path, node, dst_path, time.time() - t0))
            else:
                if not os.path.isfile(src_path):
                    return die("本地文件不存在：%s" % src_path)
                size = _cp_push_file(client, src_path, dst_path, a.timeout,
                                     sudo=a.sudo, quiet=a.quiet)
                print(ok("[+] ") + "已推送 %s → %s:%s（%d 字节，%.1fs）"
                      % (src_path, node, dst_path, size, time.time() - t0))
    except MCPError as e:
        return die("搬运失败：%s" % e)
    return 0


def _split_cp_spec(spec, known):
    r"""把 `nas:/path` 拆成 (节点名, 路径)；本地路径返回 (None, 原样)。

    只有「冒号前是已注册节点名」才算远端，避免把 `a:b.txt` 这类
    合法本地文件名误判成远端路径；Windows 盘符 `C:\x` 同理排除。
    """
    m = re.match(r"^([A-Za-z0-9_.\-]+):(.*)$", spec or "")
    if not m:
        return None, spec
    node, path = m.group(1), m.group(2)
    if node in known:
        return node, path              # 已注册节点名优先：单字母节点名也要能用
    if len(node) == 1 and node.isalpha():
        return None, spec              # Windows 盘符 C:\x
    return None, spec


# ------------------------------------------------ Tailscale 搭配层
# 判断：**不重写 Tailscale 内核，只做深度搭配。**
# 理由三条，都是硬的：
#   1) WireGuard 数据面 + DERP 中继 + 控制面是数万行安全关键代码。纯标准库
#      用 Python 重写数据面，吞吐量会低一到两个数量级（手机/NAS 上尤其明显），
#      而 AGENTS.md 的硬约束是零第三方依赖——两者直接冲突。
#   2) NAT 穿透、中继、密钥轮换这类东西的正确性靠长期对抗性打磨，不是
#      「写完能跑」就算数。自己造一个，等于把用户丢进一个未经验证的隧道。
#   3) tailscaled 已经在本机暴露了状态与控制接口。我们真正缺的不是隧道，
#      而是「知道隧道当前什么状态、地址漂了没有、掉了能不能自愈」——
#      这些用 `tailscale status --json` 就能全部拿到。
# 所以这里只做四件事：看状态、自动纠正地址漂移、保活、把 tailnet 对端
# 直接纳管成 fleet 节点。

def _ts_probe(exe, timeout=4):
    """这个二进制到底能不能当 CLI 用？

    macOS 的 App 包里同时有 `Tailscale`（GUI 主程序）和 `tailscale`（真 CLI）。
    只判断「文件存在」会先撞上 GUI 那个：它收到 `status --json` 既不输出也不报错，
    于是 fleet 判定「未登录」，而实际上 Tailscale 好好跑着。
    所以这里实际敲一下 `version`，有输出才算数。
    """
    try:
        r = subprocess.run([exe, "version"], capture_output=True, text=True,
                           timeout=timeout)
    except Exception:
        return False
    return bool((r.stdout or "").strip())


def _tailscale_bin():
    """找可用的 tailscale CLI。

    顺序有意义：先 PATH，再 Homebrew 的两个前缀，最后才进 App 包——
    且 App 包里**小写** `tailscale` 优先（那是 CLI），大写 `Tailscale` 是 GUI。
    每个候选都实测一次，都不响应时返回第一个找到的，让上层给出具体错误。
    """
    cand = ["tailscale",
            "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/opt/bin/tailscale", "/usr/sbin/tailscale"]
    first_found = None
    for c in cand:
        p = shutil.which(c) if os.path.sep not in c else (c if os.path.isfile(c) else None)
        if not p:
            continue
        if first_found is None:
            first_found = p
        if _ts_probe(p):
            return p
    return first_found


def _tailscale_status(timeout=8):
    """返回 (status_dict, err)。未安装/未登录时 err 是人话。"""
    exe = _tailscale_bin()
    if not exe:
        return None, "未安装 tailscale（macOS 可装 App，Linux 用 curl -fsSL https://tailscale.com/install.sh | sh）"
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "tailscale status 超时（tailscaled 可能没在跑）"
    except Exception as e:
        return None, "调用 tailscale 失败：%s" % e
    out = (r.stdout or "").strip()
    if not out:
        # 三种情况都长这样，得分开说，否则用户会去反复 tailscale up
        return None, ("tailscale 没有输出。常见原因：\n"
                      "      1) 未登录：tailscale up\n"
                      "      2) 用的是 GUI 主程序而非 CLI（macOS App 包里小写的\n"
                      "         tailscale 才是 CLI）：%s\n"
                      "      3) 当前进程被沙盒/权限挡住，读不到 tailscaled（换到终端里跑一次对比）"
                      % exe)
    try:
        data = json.loads(out)
    except ValueError:
        return None, "tailscale 输出不是 JSON（版本过旧？）"
    if data.get("BackendState") and data["BackendState"] != "Running":
        return None, "tailscaled 状态为 %s（需要 tailscale up）" % data["BackendState"]
    return data, ""


def _ts_self_ips(status):
    self_info = (status or {}).get("Self") or {}
    return [ip for ip in (self_info.get("TailscaleIPs") or []) if ip]


def _ts_peer_map(status):
    """{hostname_lower: {"ips": [...], "online": bool, "dns": str, "relay": str, "direct": bool}}

    同时把 DNSName 的首段也当主机名（MagicDNS 里 a.b.ts.net 的首段就是主机名）。
    """
    out = {}
    for key, p in ((status or {}).get("Peer") or {}).items():
        if not isinstance(p, dict):
            continue
        names = set()
        hn = (p.get("HostName") or "").strip().lower()
        if hn:
            names.add(hn)
        dns = (p.get("DNSName") or "").strip().rstrip(".")
        if dns:
            names.add(dns.lower())
            names.add(dns.split(".")[0].lower())
        rec = {"ips": [ip for ip in (p.get("TailscaleIPs") or []) if ip],
               "online": bool(p.get("Online")),
               "dns": dns,
               "relay": p.get("Relay") or "",
               "direct": bool(p.get("CurAddr")),
               "os": p.get("OS") or "",
               "key": key}
        for n in names:
            out.setdefault(n, rec)
    return out


def cmd_tailnet(a):
    """fleet tailnet —— 看状态 / 纠正地址漂移 / 起隧道。"""
    action = getattr(a, "action", None) or "status"
    if action == "up":
        exe = _tailscale_bin()
        if not exe:
            return die("未安装 tailscale。安装后执行：sudo tailscale up")
        print("执行 tailscale up（首次会给出登录链接）…")
        rc = subprocess.call([exe, "up"])
        if rc != 0:
            return die("tailscale up 失败（exit %d）。常见原因：需要 sudo，或需要先 tailscale login" % rc)
        print(ok("[+] ") + "tailnet 已连接")
        return 0
    if action == "keepalive":
        return _tailnet_keepalive_hint()

    status, err = _tailscale_status()
    if not status:
        print(warn("[!] ") + err)
        print(dim("    fleet 不依赖 tailscale 也能用（局域网 UDP 发现 / 邀请直连）；"))
        print(dim("    tailscale 只是让跨网段与手机弱网更稳的那一层。"))
        return 1

    self_ips = _ts_self_ips(status)
    peers = _ts_peer_map(status)
    nodes = discover_nodes(a.agent)

    if action == "status":
        print(ok("=" * 60))
        print(ok("  Tailnet 状态"))
        print(ok("=" * 60))
        print("  本机 tailnet IP : %s" % (", ".join(self_ips) or "(无)"))
        online = sum(1 for k, v in ((status.get("Peer") or {}).items()) if v.get("Online"))
        total = len(status.get("Peer") or {})
        print("  tailnet 对端   : %d 台（在线 %d）" % (total, online))
        print()
        for key, p in sorted((status.get("Peer") or {}).items(),
                             key=lambda kv: (not kv[1].get("Online"),
                                             (kv[1].get("HostName") or "").lower())):
            if not isinstance(p, dict):
                continue
            hn = p.get("HostName") or key[:12]
            ips = [ip for ip in (p.get("TailscaleIPs") or []) if ip]
            path = "直连" if p.get("CurAddr") else ("中继 %s" % (p.get("Relay") or "?"))
            state = ok("在线") if p.get("Online") else dim("离线")
            mark = ""
            if ips and any((nodes.get(n) or {}).get("url", "").find(ips[0]) >= 0
                           for n in nodes):
                mark = ok("  ← fleet 已纳管")
            print("  %-18s %-16s %-6s %-10s %s%s"
                  % (hn, (ips[0] if ips else "-"), state, path,
                     p.get("OS") or "", mark))
        print()
        if not nodes:
            print(dim("  还没有 fleet 节点。把 tailnet 对端纳管进来：fleet tailnet sync"))
        return 0

    if action == "sync":
        return _tailnet_sync(a, status, peers)

    return die("未知动作：%s（可用 status / sync / up / keepalive）" % action)


def _tailnet_sync(a, status, peers):
    """用 tailscale 的现状纠正登记表里的地址漂移（不需要 API key）。

    只对**能证明身份**的节点动手：新地址必须匿名探活通过、且 server 名
    等于登记名，才写回登记表。否则宁可不改——投毒的 tailnet 对端同样
    能出现在 status 里。
    """
    reg = load_registry()
    nodes = reg.get("nodes") or {}
    if not nodes:
        print(warn("没有已登记的节点。"))
        return 1
    changed = 0
    for name in sorted(nodes):
        info = nodes.get(name) or {}
        cur = info.get("url") or ""
        host = host_of(cur)
        # 找出这个节点在 tailnet 里的当前地址：优先用登记的 tailnet 主机名，
        # 否则用「当前地址正好是 tailnet IP」反查。
        peer = None
        hint = (info.get("tailnet") or "").strip().lower()
        if hint:
            peer = peers.get(hint) or peers.get(hint.split(".")[0])
        if peer is None and _is_tailnet_host(host):
            for _k, v in peers.items():
                if host in v["ips"]:
                    peer = v
                    break
        if not peer or not peer["ips"]:
            print("  %-16s %s" % (name, dim("tailnet 里找不到（跳过）")))
            continue
        new_ip = peer["ips"][0]
        if new_ip == host:
            print("  %-16s %s" % (name, dim("地址一致 %s" % new_ip)))
            continue
        new_url = cur.replace(host, new_ip) if host else normalize_url(new_ip, a.port)
        if not new_url.startswith("http"):
            new_url = normalize_url(new_ip, a.port)
        # 身份闸门：匿名探活必须报出同一个节点名，才认这是同一台设备
        try:
            anon = http_probe(new_url, timeout=min(a.timeout, 6), token="", retries=0)
        except Exception as e:
            print("  %-16s %s" % (name, warn("新地址不可达（%s），保持原地址" % e)))
            continue
        if (anon or {}).get("server") != name:
            print("  %-16s %s" % (name, warn(
                "新地址身份不符（期望 %s，实测 %s），拒绝改写"
                % (name, (anon or {}).get("server") or "(无)"))))
            continue
        info["url_prev"] = cur
        info["url"] = new_url
        info["synced_at"] = int(time.time())
        info["synced_by"] = "tailnet"
        nodes[name] = info
        changed += 1
        print("  %-16s %s → %s" % (name, ok("已纠正"), new_url))
    if changed:
        save_registry(reg)
    print()
    print(dim("  已纠正 %d 个节点地址（身份校验通过才写回）" % changed))
    return 0


def _tailnet_keepalive_hint():
    """保活：不同平台给不同的落地方式，不假装能一键全平台。"""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "..", "tailscale-keepalive.sh")
    print("tailscaled 保活（掉线/掉登录态时自动拉起）：")
    if os.path.isfile(script):
        print("  bash %s --once      # 跑一轮检查（配 cron/systemd timer）" % os.path.normpath(script))
        print("  bash %s             # 常驻模式" % os.path.normpath(script))
    else:
        print("  仓库根目录 tailscale-keepalive.sh（--once 配 cron，或常驻）")
    print()
    print("安卓 Termux 上还要给节点进程本身保活：")
    print("  bash termux-watchdog.sh        # 常驻；配合 Termux:Boot 开机自启")
    print(dim("  两者分工：tailscale 保网络通路，watchdog 保 fleet 节点进程。"))
    return 0


def cmd_scan(a):
    """fleet scan —— 在可达网段里找 fleet 节点（跨网段/云 VPC 没有广播时用）。

    内网设备互相访问的前提是「先知道彼此在哪」。UDP 广播只在同一广播域
    有效，云主机/VPC/多 VLAN 里完全没用，所以补一条主动探测：
    并发探 3100 端口 → GET / 验明正身 → 可选直接登记。
    """
    import socket as _socket
    from concurrent.futures import ThreadPoolExecutor

    cidr = a.cidr or _default_scan_cidr()
    try:
        import ipaddress
        net = ipaddress.ip_network(cidr, strict=False)
    except Exception as e:
        return die("网段不合法：%s（%s）" % (cidr, e))
    if net.num_addresses > 4096:
        return die("网段太大（%d 个地址）。请缩小到 /20 以内。" % net.num_addresses)
    port = a.port
    print("扫描 %s 的 %d 端口（%d 个地址）…" % (cidr, port, net.num_addresses))
    hosts = [str(ip) for ip in net.hosts()]
    found = []

    def probe(ip):
        s = _socket.socket()
        s.settimeout(a.timeout / 1000.0 if a.timeout < 1000 else 0.35)
        try:
            if s.connect_ex((ip, port)) != 0:
                return None
        finally:
            s.close()
        url = "http://%s:%d/mcp" % (ip, port)
        try:
            info = http_probe(url, timeout=3, token="", retries=0)
        except Exception:
            return None
        if not isinstance(info, dict) or not (info.get("server") or info.get("status")):
            return None
        return (ip, url, info)

    with ThreadPoolExecutor(max_workers=min(128, max(8, len(hosts)))) as ex:
        for r in ex.map(probe, hosts):
            if r:
                found.append(r)
    if not found:
        print(warn("没有发现 fleet 节点。"))
        print(dim("  同网段建议用 UDP 广播发现：fleet discover"))
        return 1
    print()
    print(ok("发现 %d 个节点：" % len(found)))
    known = set((v or {}).get("url") for v in (load_registry().get("nodes") or {}).values())
    for ip, url, info in sorted(found):
        tag = dim("（已登记）") if url in known else ""
        print("  %-15s %-22s %s v%s%s" % (ip, info.get("server") or "?", info.get("server") or "?",
                                          info.get("version") or "?", tag))
    if a.add:
        reg = load_registry()
        reg.setdefault("nodes", {})
        added = 0
        for ip, url, info in found:
            name = info.get("server") or ("node-" + ip.split(".")[-1])
            if name in reg["nodes"]:
                continue
            # 新节点一律不带令牌登记：令牌必须由用户显式提供，
            # 否则一个冒名节点就能把「空令牌」写进登记表并让后续调用失败得莫名其妙。
            reg["nodes"][name] = {"url": url, "token": "", "added_by": "scan",
                                  "updated": int(time.time())}
            added += 1
        if added:
            save_registry(reg)
            print()
            print(ok("[+] ") + "已登记 %d 个节点（未带令牌）。补令牌：" % added)
            print("    fleet add --name <名字> --url http://IP:%d/mcp --token <令牌>" % port)
        else:
            print(dim("  没有新节点需要登记。"))
    else:
        print()
        print(dim("  加 --add 可直接登记（不带令牌，需要时再 fleet add 补上）"))
    return 0


def _default_scan_cidr():
    """本机某个私网网卡的 /24 —— 不知道扫哪时给个合理默认。"""
    try:
        ip = _lan_ip()
        parts = ip.split(".")
        if len(parts) == 4 and ip != "127.0.0.1":
            return ".".join(parts[:3]) + ".0/24"
    except Exception:
        pass
    return "192.168.1.0/24"


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleet.py",
        description="mcp-fleet 统一命令行：主端操控副端集群（零依赖，任意 agent 可用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("子命令")[1] if "子命令" in __doc__ else None)
    p.add_argument("--version", action="version", version="mcp-fleet " + VERSION)
    sub = p.add_subparsers(dest="cmd", metavar="<子命令>")

    def common(sp, need_target=True):
        if need_target:
            sp.add_argument("target", help="副端名字（见 list）或完整地址 http://ip:3100/mcp")
        sp.add_argument("--agent", default="auto",
                        help="指定 agent 配置（workbuddy/claude/cursor/...，默认 auto 全扫）")
        sp.add_argument("--port", type=int, default=3100, help="target 只给 IP 时的端口（默认 3100）")
        sp.add_argument("--timeout", type=int, default=30, help="超时秒数（默认 30）")
        sp.add_argument("--json", action="store_true", help="输出 JSON")
        sp.add_argument("--no-sync", action="store_true",
                        help="不查地址目录、不自动跟随隧道新地址")
        return sp

    sp = sub.add_parser("list", help="列出已注册的副端")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--show-tokens", action="store_true",
                    help="JSON 里输出完整令牌（默认掩码；贴日志/截图时保持默认）")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("targets", help="只输出副端名字（供脚本消费）")
    sp.add_argument("--agent", default="auto")
    sp.set_defaults(func=cmd_targets)

    sub.add_parser("start", help="启动本机节点（已在跑则直接报告）").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="停止本机节点").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="重启本机节点").set_defaults(func=cmd_restart)

    common(sub.add_parser("tools", help="列副端工具清单")).set_defaults(func=cmd_tools)
    common(sub.add_parser("probe", help="连通性 + 握手 + 试调（只读）")).set_defaults(func=cmd_probe)
    common(sub.add_parser("ping", help="轻探活：只 GET /，不握手不 exec（看门/脚本用）")
           ).set_defaults(func=cmd_ping)
    sp = common(sub.add_parser("shell", help="v5.3: 交互式远程 shell（PTY，替代 ssh）"))
    sp.add_argument("--cwd", default="", help="远端起始目录")
    sp.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")
    sp.set_defaults(func=cmd_shell)
    sp = common(sub.add_parser(
        "forward", help="v5.4: 端口转发（等价 ssh -L / -R）"))
    sp.add_argument("-L", "--local", action="append", metavar="[绑定:]端口:主机:端口",
                    help="本地监听并转发到副端可达的地址，可重复")
    sp.add_argument("-R", "--remote", action="append", metavar="[绑定:]端口:主机:端口",
                    help="副端监听并转发回本地可达的地址，可重复")
    sp.add_argument("--bind", default="127.0.0.1",
                    help="-L 的默认绑定地址（默认只绑回环，要用 0.0.0.0 显式给）")
    sp.add_argument("--list", action="store_true", help="列出副端当前的隧道")
    sp.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")
    sp.set_defaults(func=cmd_forward)
    sp = common(sub.add_parser("doctor", help="完整链路体检；不带 target 时做本机自检"),
                need_target=False)
    sp.add_argument("target", nargs="?", default=None,
                    help="副端名字/地址；省略则检查本机节点")
    sp.add_argument("--check-banner", action="store_true",
                    help="额外检测登录 shell 是否往每次返回里塞欢迎横幅")
    sp.add_argument("--dir", help="副端安装目录（默认 /opt/<name>_mcp）")
    sp.set_defaults(func=cmd_doctor)

    sp = common(sub.add_parser("exec", help="在副端执行 shell 命令"))
    sp.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令")
    sp.add_argument("--sudo", action="store_true", help="用 sudo 执行")
    sp.add_argument("--workdir", help="工作目录")
    sp.add_argument("--timeout-ms", type=int, help="命令超时（毫秒）")
    sp.set_defaults(func=cmd_exec)

    sp = common(sub.add_parser("call", help="调任意工具"))
    sp.add_argument("--tool", required=True, help="工具名（list 表示列工具）")
    sp.add_argument("--args", help="参数 JSON")
    sp.add_argument("--arg", action="append", metavar="k=v",
                    help="参数简写，可重复，值按 JSON 解析")
    sp.set_defaults(func=cmd_call)

    sp = sub.add_parser("tailnet", help="Tailscale 搭配层：状态 / 地址自愈 / 起隧道 / 保活")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["status", "sync", "up", "keepalive"],
                    help="status（默认）看状态；sync 纠正地址漂移；up 起隧道；keepalive 保活方案")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--timeout", type=int, default=10)
    sp.set_defaults(func=cmd_tailnet)

    sp = sub.add_parser("scan", help="在网段里主动找 fleet 节点（跨网段/云 VPC 没有广播时用）")
    sp.add_argument("cidr", nargs="?", default=None, help="网段，如 192.168.1.0/24；默认取本机私网 /24")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--timeout", type=int, default=350, help="单地址超时毫秒（默认 350）")
    sp.add_argument("--add", action="store_true", help="把发现的节点直接登记（不带令牌）")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("cp", help="本机与节点之间搬文件/目录（scp 手感，支持大文件分块）")
    sp.add_argument("src", help="源：./main.py 或 nas:/opt/app/main.py")
    sp.add_argument("dst", help="目标：nas:/opt/app/main.py 或 ./main.py")
    sp.add_argument("-r", "--recursive", action="store_true", help="目录（自动打包/解包）")
    sp.add_argument("--sudo", action="store_true", help="推送时用 sudo 落盘")
    sp.add_argument("-q", "--quiet", action="store_true", help="不显示分块进度")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--timeout", type=int, default=120, help="单次请求超时秒数（默认 120）")
    sp.add_argument("--no-sync", action="store_true", help="不查地址目录")
    sp.set_defaults(func=cmd_cp)

    sp = common(sub.add_parser("exec-async", help="后台启动命令，立即返回 job_id"))
    sp.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令")
    sp.add_argument("--sudo", action="store_true", help="用 sudo 执行")
    sp.add_argument("--workdir", help="工作目录")
    sp.set_defaults(func=cmd_exec_async)

    sp = common(sub.add_parser("exec-poll", help="取后台任务的增量输出"))
    sp.add_argument("job_id", help="exec-async 返回的 job_id")
    sp.add_argument("--offset", type=int, default=0, help="从第几个字节开始取（默认 0）")
    sp.set_defaults(func=cmd_exec_poll)

    sp = common(sub.add_parser("exec-result", help="等待后台任务完成并取全量输出"))
    sp.add_argument("job_id", help="exec-async 返回的 job_id")
    sp.add_argument("--wait", type=int, default=120, help="最长等待秒数（默认 120）")
    sp.set_defaults(func=cmd_exec_result)

    sp = common(sub.add_parser("exec-kill", help="终止一个后台任务"))
    sp.add_argument("job_id", help="exec-async 返回的 job_id")
    sp.set_defaults(func=cmd_exec_kill)

    sp = common(sub.add_parser("exec-jobs", help="列出副端所有后台任务"))
    sp.set_defaults(func=cmd_exec_jobs)

    sp = sub.add_parser("health", help="批量体检所有副端")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--deep", action="store_true", help="额外真实调用一次 exec")
    sp.add_argument("--timeout", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("install-cmd", help="打印副端安装命令")
    sp.add_argument("--name", required=True, help="副端标识（决定服务名与安装目录）")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--prefix", help="工具名前缀（一般不用填）")
    sp.add_argument("--sudo-pass", help="副端 sudo 密码（会明文写进配置）")
    sp.add_argument("--mode", default="auto", choices=["auto", "systemd", "nohup"])
    sp.add_argument("--tunnel", action="store_true", help="装完顺带起公网隧道")
    sp.add_argument("--auth-token", help="副端访问令牌（开了隧道务必设；不设则由隧道脚本自动生成）")
    sp.add_argument("--allow-ips", help="副端 IP 白名单，逗号分隔 CIDR，如 '10.0.0.0/8,203.0.113.7'")
    sp.add_argument("--directory", help="地址目录地址，副端保活守护会把隧道地址上报到这里")
    sp.add_argument("--directory-token", help="地址目录的令牌（目录开了鉴权时填）")
    sp.add_argument("--no-keepalive", action="store_true", help="不装保活守护（默认装）")
    sp.add_argument("--generate-token", action="store_true",
                    help="副端还没令牌时自动生成一个强随机 auth_token（推荐，内网也建议）")
    sp.add_argument("--require-auth", action="store_true",
                    help="把「必须鉴权」写进副端配置：没有令牌就拒绝启动")
    sp.add_argument("--uninstall", action="store_true", help="打印卸载命令")
    sp.add_argument("--mirror", default="auto",
                    choices=["auto", "github", "jsdelivr", "ghfast", "ghproxy", "ghproxycom"],
                    help="下载通道（默认 auto=官方+镜像依次重试）")
    sp.add_argument("--offline", action="store_true", help="打印离线安装方式（用技能自带的 install.sh）")
    sp.set_defaults(func=cmd_install_cmd)

    sp = sub.add_parser("bootstrap", help="SSH 把副端装到远程设备（离线优先）")
    sp.add_argument("--host", required=True, help="设备 IP / 域名")
    sp.add_argument("--user", default="root")
    sp.add_argument("--ssh-port", type=int, default=22)
    sp.add_argument("--key", help="私钥路径")
    sp.add_argument("--password", help="SSH 密码（需本机有 sshpass；不建议写进历史记录）")
    sp.add_argument("--password-file", help="从文件读 SSH 密码（更安全）")
    sp.add_argument("--name", required=True, help="副端标识")
    sp.add_argument("--node-port", type=int, default=3100, help="副端监听端口")
    sp.add_argument("--prefix", help="工具名前缀")
    sp.add_argument("--sudo-pass", help="sudo 密码")
    sp.add_argument("--mode", default="auto", choices=["auto", "systemd", "nohup"])
    sp.add_argument("--tunnel", action="store_true")
    sp.add_argument("--auth-token", help="副端访问令牌（开隧道务必设）")
    sp.add_argument("--allow-ips", help="副端 IP 白名单（逗号分隔 CIDR）")
    sp.add_argument("--directory", help="地址目录地址（副端保活上报隧道地址用）")
    sp.add_argument("--directory-token", help="地址目录令牌")
    sp.add_argument("--no-keepalive", action="store_true", help="不装保活守护")
    sp.add_argument("--generate-token", action="store_true",
                    help="副端还没令牌时自动生成一个强随机 auth_token（推荐）")
    sp.add_argument("--require-auth", action="store_true",
                    help="把「必须鉴权」写进副端配置：没有令牌就拒绝启动")
    sp.add_argument("--online", action="store_true", help="不用本地 install.sh，让副设备自己下载")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--timeout", type=int, default=20)
    sp.set_defaults(func=cmd_bootstrap)

    sp = common(sub.add_parser("tunnel", help="查看/启动/停止副端公网隧道"))
    sp.add_argument("--start", action="store_true")
    sp.add_argument("--stop", action="store_true")
    sp.add_argument("--dir", help="副端安装目录（默认 /opt/<name>_mcp）")
    sp.add_argument("--wait", type=int, default=60, help="等隧道地址的秒数（默认 60）")
    sp.set_defaults(func=cmd_tunnel)

    sp = sub.add_parser("add", help="注册副端")
    sp.add_argument("--name", required=True)
    sp.add_argument("--ip")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--url", help="完整端点，如 https://xxx.trycloudflare.com/mcp")
    sp.add_argument("--token", help="副端访问令牌（副端开了鉴权必填，会写进 mcp.json 的 headers）")
    sp.add_argument("--agent", default="auto", help="写入哪个 agent 的配置（默认 workbuddy）")
    sp.add_argument("--mcp", help="直接指定 mcp.json 路径")
    sp.add_argument("--no-test", action="store_true", help="跳过连通性测试")
    sp.add_argument("--force", action="store_true", help="测试失败也写入")
    sp.add_argument("--timeout", type=int, default=10)
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="注销副端")
    sp.add_argument("name")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--mcp", help="直接从指定的 mcp.json 路径移除（配合 add --mcp 用）")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("sync", help="按地址目录刷新副端地址（隧道换域名后自动跟随）")
    sp.add_argument("names", nargs="*", help="要同步的副端名字（默认全部）")
    sp.add_argument("--agent", default="auto")
    sp.add_argument("--timeout", type=int, default=10)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("pair", help="双向配对：输入对方设备码@地址，两边自动互连（v4.3）")
    sp.add_argument("target", help="设备码@API 地址（如 nas-a3f2@http://192.168.1.10:3100/mcp），或纯地址，或已登记的名字")
    sp.add_argument("--token", help="对方的 auth_token（对方开了鉴权必填）")
    sp.add_argument("--name", help="给对方起的名字（默认用对方自报的 name）")
    sp.add_argument("--self-url", help="对方回连本机用的地址（默认：本机隧道地址 > 内网 IP:端口）")
    sp.add_argument("--one-way", action="store_true", help="只把对方登记到本机，不让对方反向登记")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--timeout", type=int, default=20)
    sp.set_defaults(func=cmd_pair)

    sp = sub.add_parser("invite", help="v4.4: 生成邀请码，其他设备用它一键安装+自动连接")
    sp.add_argument("--ttl", type=int, default=3600, help="邀请码有效期（秒，默认 3600）")
    sp.add_argument("--self-url", help="本机对外地址（默认自动检测）")
    sp.add_argument("--timeout", type=int, default=10)
    sp.add_argument("--qr", action="store_true", help="在终端显示二维码（需 qrencode）")
    sp.set_defaults(func=cmd_invite)

    def local(sp):
        common(sp, need_target=False)
        sp.add_argument("--self-url", dest="self_url", default=None,
                        help="本机节点回连地址（NAT/容器场景显式指定）")
        return sp

    sp = local(sub.add_parser("join", help="v5.2: 用 fleet://join/... 邀请加入并配对"))
    sp.add_argument("uri", help="fleet://join/...")
    sp.add_argument("--auto-install", action="store_true", dest="auto_install",
                    help="本机未安装节点时自动安装后再加入")
    sp.set_defaults(func=cmd_join)
    sp = local(sub.add_parser("ask", help="v5.2: 自然语言入口（生成计划/证据/提示词）"))
    sp.add_argument("text", help="你想做什么，例如：帮我看下磁盘为什么满了")
    sp.add_argument("--run", action="store_true", help="立即执行只读诊断步骤")
    sp.set_defaults(func=cmd_ask)
    sp = local(sub.add_parser("selfcheck", help="v5.2: 本机只读自检（finding + 修复计划）"))
    sp.set_defaults(func=cmd_selfcheck)
    sp = local(sub.add_parser("repair", help="v5.2: 自修复（默认只给计划；--auto 有界自愈）"))
    sp.add_argument("--auto", action="store_true", help="执行有界自愈循环（最多 2/3 次）")
    sp.add_argument("--attempts", type=int, default=2)
    sp.add_argument("--restart", action="store_true", help="只重启服务")
    sp.add_argument("--fix-permissions", action="store_true", dest="fix_permissions")
    sp.add_argument("--clear-stale-pid", action="store_true", dest="clear_stale_pid")
    sp.add_argument("--rollback", action="store_true", help="回滚最近备份（高风险）")
    sp.set_defaults(func=cmd_repair)
    sp = sub.add_parser("prompt", help="v5.2: 查看内置 AI 提示词")
    sp.add_argument("topic", nargs="?", default="",
                    help="self_repair/diagnose/collaborate/pair/upgrade")
    sp.set_defaults(func=cmd_prompt)
    sp = local(sub.add_parser("update", help="v5.2: 升级/回滚本机节点"))
    sp.add_argument("--rollback", action="store_true", help="回滚到最近一次升级备份")
    sp.add_argument("--offline", action="store_true",
                    help="不联网，使用节点目录里的本地安装器")
    sp.set_defaults(func=cmd_update)
    sp = sub.add_parser("up", help="v5.2.1: 从 git 升级本地脚本（有新版才动，失败自动回滚）")
    sp.add_argument("--check", action="store_true", help="只检查有没有新版，不改文件")
    sp.add_argument("--ref", default="main", help="分支或标签（默认 main）")
    sp.add_argument("--force", action="store_true",
                    help="同版本也重装；工作区有改动也用远端覆盖")
    sp.add_argument("--no-restart", action="store_true", help="升级后不重启本机节点")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(func=cmd_up)
    sp = sub.add_parser("identity", help="v5.2: 身份查看/加密导出/恢复")
    sp.add_argument("action", nargs="?", default="show",
                    choices=["show", "export", "import"])
    sp.add_argument("--out", default="", help="导出路径（默认 ~/fleet-identity-<id>.json）")
    sp.add_argument("--from", dest="from_file", default="", help="导入的备份文件")
    sp.add_argument("--passphrase", default="", help="备份口令（不传则交互输入）")
    sp.add_argument("--self-url", dest="self_url", default=None)
    sp.add_argument("--timeout", type=int, default=30)
    sp.set_defaults(func=cmd_identity)
    sp = local(sub.add_parser("nodes", help="v5.2: 列出本机节点认识的对等节点"))
    sp.add_argument("--check", action="store_true", help="顺带探活")
    sp.set_defaults(func=cmd_nodes)
    local(sub.add_parser("status", help="v5.2: 本机节点健康与身份摘要")).set_defaults(func=cmd_status)
    local(sub.add_parser("metrics", help="v5.2: 打印节点 Prometheus 指标")).set_defaults(func=cmd_metrics)
    sp = local(sub.add_parser("send", help="v5.2: 通过本机节点给其他节点/agent 发消息"))
    sp.add_argument("to", help="接收方节点/agent 名，* 为广播")
    sp.add_argument("text", help="消息正文")
    sp.add_argument("--from", dest="sender", default="", help="发送方名字（默认 cli-<本机名>）")
    sp.add_argument("--topic", default="", help="消息主题")
    sp.set_defaults(func=cmd_send)
    sp = local(sub.add_parser("logs", help="v5.2: 查看本机节点日志尾部"))
    sp.add_argument("--tail", type=int, default=100)
    sp.set_defaults(func=cmd_logs)
    sp = local(sub.add_parser("audit", help="v5.2: 校验本机节点审计哈希链"))
    sp.add_argument("--tail", type=int, default=20)
    sp.set_defaults(func=cmd_audit)
    sp = local(sub.add_parser("token", help="v5.2: 轮换本机节点访问 token"))
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_token)
    sp = sub.add_parser("accept", help="v4.4: 接受邀请码 → 确权 → 自动配对")
    sp.add_argument("invite_code", help="邀请码（base64 编码字符串）")
    sp.add_argument("--name", help="本机设备名（默认用配置中的 name）")
    sp.add_argument("--self-url", help="本机对外地址（默认自动检测）")
    sp.add_argument("--yes", action="store_true", help="跳过确认提示（危险）")
    sp.add_argument("--timeout", type=int, default=15)
    sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("directory", help="查看/设置地址目录（副端上报地址、主端自动跟随）")
    sp.add_argument("--url", help="地址目录服务地址，如 http://192.168.1.10:8790")
    sp.add_argument("--token", help="目录的 X-Fleet-Token（可选）")
    sp.add_argument("--clear", action="store_true", help="清除已配置的地址目录")
    sp.add_argument("--timeout", type=int, default=8)
    sp.set_defaults(func=cmd_directory)

    sp = sub.add_parser("mesh", help="打印 mesh 拓扑图（节点在线状态 + 能力 + peer 关系）")
    sp.add_argument("--timeout", type=int, default=15)
    sp.set_defaults(func=cmd_mesh)

    # v4.1: fleet msg -- 通过 peer_call 调远程节点的 msg 工具
    sp = common(sub.add_parser("msg", help="消息总线：send/recv/history（通过 peer_call 调远程节点）"))
    sp.add_argument("msg_cmd", choices=["send", "recv", "history"], help="子命令")
    sp.add_argument("--from", dest="frm", help="发送者")
    sp.add_argument("--to", help="接收者")
    sp.add_argument("--text", help="消息内容")
    sp.add_argument("--me", help="我的名字（recv）")
    sp.add_argument("--topic", help="主题")
    sp.add_argument("--wait", type=int, default=0, help="等待秒数（recv）")
    sp.add_argument("--lines", type=int, default=20, help="历史行数（history）")
    sp.set_defaults(func=cmd_msg)

    # v4.1: fleet discover -- UDP 广播发现
    sp = sub.add_parser("discover", help="UDP 广播发现同网段 fleet 节点（v4.1 自研协议）")
    sp.add_argument("--deep", action="store_true", help="主动 ping 所有 254 个 IP（慢但全）")

    # ==========================================================================
    # v5.0.6: 中文交互菜单（手机/桌面通用，无参数时默认进入）
    # ==========================================================================
    sp = sub.add_parser("menu", help="中文交互菜单（无参数时默认进入）")
    sp.add_argument("--no-clear", action="store_true", help="不清屏（调试用）")
    sp.set_defaults(func=cmd_menu)

    return p


def _tool_text(a, tool, args=None):
    got = _local_mcp(a)
    if not got:
        return None, "本机没有运行中的节点"
    me, client = got
    try:
        text, is_err = client.call(tool, args or {})
        return (text, is_err), me
    except Exception as e:
        return None, "调用 %s 失败：%s" % (tool, e)


def cmd_selfcheck(a):
    """v5.2: 只读自检，输出可机读 findings 与修复计划。"""
    res, me = _tool_text(a, "fleet_selfcheck")
    if res is None:
        return die(me)
    text, is_err = res
    if getattr(a, "json", False):
        print(text)
        return 1 if is_err else 0
    try:
        data = json.loads(text)
    except Exception:
        print(text)
        return 1 if is_err else 0
    print("自检结果：%s" % ("通过" if data.get("ok") else "有严重问题"))
    print("  %s" % data.get("summary", ""))
    for f in data.get("findings") or []:
        print("  [%-8s] %-20s %s" % (f.get("severity"), f.get("code"),
                                     f.get("title")))
        if f.get("detail"):
            print("             %s" % f.get("detail"))
    plan = data.get("repair_plan") or []
    print("  修复计划：%s" % (", ".join(plan) if plan else "无自动修复项"))
    if plan:
        print("  执行：fleet repair --auto（有界，最多 2 次）")
    return 1 if is_err else 0


def cmd_repair(a):
    """v5.2: 自修复。默认只给计划；--auto 走有界自愈循环。"""
    got = _local_mcp(a)
    if not got:
        return die("本机没有运行中的节点")
    me, client = got
    name = me.get("name") or ""
    if getattr(a, "auto", False):
        args = {"confirm": name, "max_attempts": getattr(a, "attempts", 2),
                "allow_rollback": bool(getattr(a, "rollback", False))}
        tool = "fleet_repair_loop"
    else:
        action = None
        if getattr(a, "restart", False):
            action = "restart_service"
        elif getattr(a, "fix_permissions", False):
            action = "fix_permissions"
        elif getattr(a, "clear_stale_pid", False):
            action = "clear_stale_pid"
        elif getattr(a, "rollback", False):
            action = "rollback"
        if not action:
            # 默认只读：先自检并展示计划
            return cmd_selfcheck(a)
        args = {"action": action, "confirm": name}
        tool = "fleet_repair"
    try:
        text, is_err = client.call(tool, args)
    except Exception as e:
        return die("修复调用失败：%s" % e)
    if getattr(a, "json", False):
        print(text)
        return 1 if is_err else 0
    try:
        data = json.loads(text)
    except Exception:
        print(text)
        return 1 if is_err else 0
    print("修复结果：%s" % ("成功" if data.get("ok") else "仍有问题"))
    if data.get("attempts"):
        for item in data["attempts"]:
            print("  - %s: %s" % (item.get("action"),
                                  "ok" if item.get("ok") else "失败"))
            print("    %s" % (item.get("detail") or ""))
    if data.get("remaining"):
        print("  未解决（需人工）：")
        for f in data["remaining"]:
            print("    [%s] %s：%s" % (f.get("severity"), f.get("code"),
                                       f.get("title")))
    elif data.get("next"):
        print("  %s" % data.get("next"))
    return 1 if is_err else 0


def cmd_prompt(a):
    """v5.2: 打印内置提示词（可原样粘给 AI 客户端）。"""
    topic = (getattr(a, "topic", "") or "").strip()
    if topic and topic not in PROMPT_PACK:
        return die("未知主题：%s（可选 %s）" % (topic, ", ".join(PROMPT_PACK)))
    print(prompt_text(topic or None))
    return 0


def _first_run_guide():
    """第一次打开：没有本机节点、也没有任何对端时，只给下一步该做什么。

    空白状态是最容易劝退的地方。菜单里塞 20 条命令只会让人关掉窗口，
    所以这里只讲三件事：装一台、连起来、然后能干什么。
    """
    print("=" * 66)
    print("  MCP Fleet v%s · 第一次使用" % VERSION)
    print("=" * 66)
    print("""
  还没有接入任何设备。三分钟可以跑通一台。

  ── 第 1 步：在这台机器上装一个节点 ──────────────────────────
    sh scripts/install.sh --name %s --port 3100

    安装器会自动识别系统（Linux/macOS/NAS/Termux/OpenWrt）、
    生成 Ed25519 身份、配置开机自启，并打印访问令牌与 Dashboard 地址。

  ── 第 2 步：把第二台设备连进来 ─────────────────────────────
    fleet invite --qr              # 本机生成邀请（手机扫码即可）
    fleet join 'fleet://join/...'  # 或在对端执行这一行

    配对时会显示 6 位 SAS 校验码，两边一致才确认 —— 防中间人。

  ── 第 3 步：开始用 ─────────────────────────────────────────
    fleet nodes                      看全网在线状态
    fleet exec <节点> "df -h"         在别的设备上跑命令
    fleet cp ./main.py nas:/opt/app/main.py   把代码推到设备上改
    fleet ping <节点>                 一秒判断通不通

  只想先当控制端、不装节点？
    fleet add --name nas --url http://192.168.1.9:3100/mcp --token <令牌>

  装好之后回到 `fleet` 就是完整的控制台。
""" % _default_node_name())
    print("=" * 66)
    return 0


def _default_node_name():
    """给个像样的默认节点名：<主机名简化>，拿不到就用 mynode。"""
    try:
        h = socket.gethostname().split(".")[0].lower()
        h = re.sub(r"[^a-z0-9\-]", "", h)
        return h or "mynode"
    except Exception:
        return "mynode"


# ------------------------------------------------ macOS / launchd 适配
# 为什么单独做一层：macOS 上用 launchd 装节点后，`fleet stop` 直接 kill pid 会
# 被 KeepAlive 立刻拉回来（用户看到的是「停不掉」）；而 `fleet start` 手动
# spawn 出来的进程又不受 launchd 管，重启机器就没了。
# 所以只要发现 plist，就必须走 launchctl，而不是自己 fork。

def _sh(argv, timeout=30):
    """跑一条外部命令，返回 (rc, stdout+stderr)。不抛异常。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def _launchd_dirs():
    return [os.path.expanduser("~/Library/LaunchAgents"),
            "/Library/LaunchDaemons",
            "/Library/LaunchAgents"]


def _launchd_plist(name=""):
    """找本机节点对应的 launchd plist；没有返回空串。

    只认 label 含 mcpfleet 的 plist，避免误抓别的服务。
    """
    if sys.platform != "darwin":
        return ""
    hits = []
    for d in _launchd_dirs():
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for f in entries:
            if f.endswith(".plist") and "mcpfleet" in f.lower():
                hits.append(os.path.join(d, f))
    if not hits:
        return ""
    if name:
        low = name.lower()
        for p in hits:
            if low in os.path.basename(p).lower():
                return p
    return sorted(hits)[0]


def _plist_label(path):
    """从 plist 里读 Label（不引 plistlib 也能读，简单正则足够）。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
    return m.group(1).strip() if m else ""


def _launchd_domain(plist):
    """LaunchAgents 属于 gui/<uid>，LaunchDaemons 属于 system。"""
    if "/LaunchDaemons/" in plist:
        return "system"
    return "gui/%d" % os.getuid()


def _launchd_start(plist):
    """注册并启动。返回 (ok, 说明)。

    优先用 bootstrap/kickstart（10.11+ 的正式接口），失败再退回 load -w。
    先 enable：上次 `fleet stop` 会把它 disable 掉，不 enable 是拉不起来的。
    """
    label = _plist_label(plist)
    domain = _launchd_domain(plist)
    if not label:
        return False, "plist 里没有 Label"
    _sh(["launchctl", "enable", "%s/%s" % (domain, label)])
    rc, out = _sh(["launchctl", "bootstrap", domain, plist], timeout=60)
    if rc != 0:
        rc2, out2 = _sh(["launchctl", "load", "-w", plist], timeout=60)
        if rc2 != 0:
            return False, out or out2
    _sh(["launchctl", "kickstart", "-k", "%s/%s" % (domain, label)], timeout=30)
    return True, label


def _launchd_stop(plist):
    """停掉并**禁止自动拉起**。返回 (ok, 说明)。

    只 kill 进程没用：KeepAlive=true 会在 10 秒内重启它。
    必须先 bootout（或 unload -w）把服务本身摘掉，再 disable 记住「别再起来」。
    """
    label = _plist_label(plist)
    domain = _launchd_domain(plist)
    if not label:
        return False, "plist 里没有 Label"
    rc, out = _sh(["launchctl", "bootout", "%s/%s" % (domain, label)], timeout=30)
    if rc != 0:
        rc, out = _sh(["launchctl", "unload", "-w", plist], timeout=30)
    _sh(["launchctl", "disable", "%s/%s" % (domain, label)])
    return True, label


def _launchd_status(plist):
    """返回 (已注册, 进程是否活着, 原文)。"""
    label = _plist_label(plist)
    if not label:
        return False, False, ""
    rc, out = _sh(["launchctl", "list"], timeout=15)
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            pid = parts[0]
            return True, pid not in ("-", "0"), line.strip()
    return False, False, ""


def _macos_advice(me=None):
    """macOS 专属的「为什么不好使」提示。只列真的会咬人的那几条。

    这些都是实测踩过的：不是理论风险，是用户会直接来问「为什么不行」的那种。
    """
    tips = []
    if sys.platform != "darwin":
        return tips
    if not shutil.which("git"):
        tips.append(("warn", "没装 git，fleet up 用不了。装：xcode-select --install"))
    py = (sys.executable or "")
    if py.startswith(("/opt/homebrew", "/usr/local/Cellar")):
        tips.append(("info", "当前 python 来自 Homebrew（%s）。brew 升级大版本后路径会变，"
                             "节点可能起不来；届时重跑 fleet start 或重装即可。" % py))
    if me:
        plist = _launchd_plist(me.get("name", ""))
        if plist:
            reg, alive, _line = _launchd_status(plist)
            if not reg:
                tips.append(("warn", "装了 launchd 服务但当前未注册；fleet start 可恢复开机自启。"))
        else:
            tips.append(("info", "本机节点不是由 launchd 托管的：重启机器后不会自动起来。"
                                 "要开机自启请用安装器重装（sh scripts/install.sh ...）。"))
    return tips



def _node_pid_path(d):
    return os.path.join(d, "run", "node.pid")


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_pid(d):
    try:
        return int(open(_node_pid_path(d), encoding="utf-8").read().strip())
    except Exception:
        return 0


def _local_health(me, timeout=2):
    """本机节点 HTTP 是否活着。比 pid 判断更可信（pid 可能是僵尸）。"""
    if not me:
        return False
    try:
        http_probe(me["url"], timeout=timeout, token=me.get("token", ""), retries=0)
        return True
    except Exception:
        return False


def cmd_start(a):
    """启动本机节点（已在跑则直接报告，不重复拉起）。"""
    me = local_node()
    if not me:
        return die("本机还没装节点。安装：\n"
                   "    sh scripts/install.sh --name %s --port 3100\n"
                   "    只做控制端则不用装：fleet add --name x --url http://IP:3100/mcp --token T"
                   % _default_node_name())
    if _local_health(me):
        print(ok("[+] ") + "本机节点已在运行：%s（%s）" % (me["name"], me["url"]))
        return 0
    d = me["dir"]
    agent = os.path.join(d, "mcp_agent.py")
    if not os.path.isfile(agent):
        return die("找不到 %s（节点目录不完整，建议重装）" % agent)

    # macOS：装了 launchd 服务就必须走 launchctl。
    # 自己 fork 一个进程虽然也能起来，但它不受 launchd 管：
    # 重启机器不会自启、崩溃不会重拉，还和 KeepAlive 抢同一个端口。
    plist = _launchd_plist(me.get("name", ""))
    if plist:
        okv, detail = _launchd_start(plist)
        if okv:
            for _ in range(24):
                time.sleep(0.25)
                if _local_health(me, timeout=1):
                    print(ok("[+] ") + "本机节点已启动（launchd %s）：%s  %s"
                          % (detail, me["name"], me["url"]))
                    print(dim("    开机自启 + 崩溃重拉已生效；停止用 fleet stop"))
                    return 0
            print(err("[x] ") + "launchd 已注册但健康检查没通，看日志：")
            print("    tail -30 %s" % os.path.join(d, "agent.log"))
            return 1
        print(warn("[!] ") + "launchctl 启动失败（%s），退回直接启动。" % detail)

    # 监听地址：有令牌才敢对外，否则只绑本机（与安装器的安全默认一致）
    host = "0.0.0.0" if me.get("token") else "127.0.0.1"
    try:
        port = int(open(os.path.join(d, "port.txt"), encoding="utf-8").read().strip())
    except Exception:
        port = 3100
    logdir = os.path.join(d, "run")
    os.makedirs(logdir, exist_ok=True)
    logf = open(os.path.join(logdir, "node.log"), "ab")
    try:
        p = subprocess.Popen([sys.executable, agent, "--host", host, "--port", str(port)],
                             cwd=d, stdout=logf, stderr=logf,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return die("启动失败：%s" % e)
    try:
        os.makedirs(logdir, exist_ok=True)
        with open(_node_pid_path(d), "w", encoding="utf-8") as f:
            f.write(str(p.pid))
    except Exception:
        pass
    # 等它真的能响应，而不是「进程起来了」就算成功
    for _ in range(20):
        time.sleep(0.25)
        if _local_health(me, timeout=1):
            print(ok("[+] ") + "本机节点已启动：%s  %s（pid %d）" % (me["name"], me["url"], p.pid))
            print(dim("    日志：%s" % os.path.join(logdir, "node.log")))
            return 0
        if p.poll() is not None:
            break
    print(err("[x] ") + "启动后没能响应健康检查。看看日志：")
    print("    tail -30 %s" % os.path.join(logdir, "node.log"))
    return 1


def _find_listener_pid(port):
    """按端口找监听进程（没有 pidfile 时的兜底）。"""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=5)
        else:
            r = subprocess.run(["sh", "-c",
                                "ss -lntp 2>/dev/null | grep ':%d ' | grep -o 'pid=[0-9]*' | head -1" % port],
                               capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        if not out:
            return 0
        if "pid=" in out:
            return int(out.split("pid=")[1].split()[0])
        return int(out.splitlines()[0].strip())
    except Exception:
        return 0


def cmd_stop(a):
    me = local_node()
    if not me:
        return die("本机没有节点。")
    d = me["dir"]

    # macOS：先摘 launchd 服务，否则 KeepAlive 会在 10 秒内把进程重新拉起来，
    # 用户看到的现象就是「fleet stop 说停了，过一会儿又活了」。
    plist = _launchd_plist(me.get("name", ""))
    if plist:
        registered, alive, line = _launchd_status(plist)
        okv, detail = _launchd_stop(plist)
        if not okv:
            print(warn("[!] ") + "launchctl 停止失败（%s），退回按 pid 停止。" % detail)
        else:
            for _ in range(20):
                time.sleep(0.2)
                if not _local_health(me, timeout=1):
                    break
            try:
                os.unlink(_node_pid_path(d))
            except Exception:
                pass
            print(ok("[+] ") + "本机节点已停止（launchd %s，已禁止自动拉起）" % detail)
            print(dim("    重新启动：fleet start"))
            return 0

    pid = _read_pid(d) or _find_listener_pid(int(me["url"].rsplit(":", 1)[-1].split("/")[0]
                                               or 3100))
    if not pid or not _pid_alive(pid):
        print(warn("[i] ") + "本机节点没有在运行。")
        return 0
    try:
        os.kill(pid, 15)
    except Exception as e:
        return die("停止失败：%s" % e)
    for _ in range(20):
        time.sleep(0.2)
        if not _pid_alive(pid):
            break
    if _pid_alive(pid):
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    try:
        os.unlink(_node_pid_path(d))
    except Exception:
        pass
    print(ok("[+] ") + "本机节点已停止（pid %d）" % pid)
    return 0


def cmd_restart(a):
    cmd_stop(a)
    return cmd_start(a)


def _peer_summary(agent=None, timeout=2):
    """并行探活所有已登记节点 → (在线数, 总数, [(名字, 在线, rtt_ms)])。

    菜单要一眼看到「连了几台」。串行 ping 在 5 台设备上就要 5×超时，
    所以这里并发，并且超时压到 2 秒。
    """
    from concurrent.futures import ThreadPoolExecutor
    nodes = discover_nodes(agent)
    if not nodes:
        return 0, 0, []

    def one(item):
        name, m = item
        t0 = time.time()
        try:
            http_probe(m["url"], timeout=timeout, token=m.get("token", ""), retries=0)
            return (name, True, (time.time() - t0) * 1000)
        except Exception:
            return (name, False, 0)

    rows = []
    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as ex:
        for r in ex.map(one, sorted(nodes.items())):
            rows.append(r)
    return sum(1 for _, up, _ in rows if up), len(rows), rows


def show_fleet_status():
    """v5.2.1: 无参数 = 打开控制台。

    三件事一次做完：本机节点没跑就拉起来 → 报告连了几台 → 给菜单。
    用户敲一个 `fleet` 就该知道「现在什么状态、下一步能做什么」。
    """
    me = local_node()
    try:
        _peer_count = len(load_registry().get("nodes", {}))
    except Exception:
        _peer_count = 0
    if not me and _peer_count == 0:
        return _first_run_guide()

    # 装了节点但没在跑：直接启动（这就是「输入 fleet 就能开启运行」）
    if me and not _local_health(me, timeout=1.5):
        print(dim("  本机节点未运行，正在启动…"))
        rc = cmd_start(None)
        if rc != 0:
            print(warn("  自动启动失败，可手动：fleet start（看 fleet logs）"))
        print()

    print("=" * 66)
    print("  MCP Fleet v%s · AI 对等协作网 · 主控 CLI" % VERSION)
    print("=" * 66)

    # 本机状态
    print("\n  【本机节点】")
    if not me:
        print("    未安装（当前只作控制端）")
        print(dim("    安装：sh scripts/install.sh --name %s" % _default_node_name()))
    else:
        up = _local_health(me, timeout=1.5)
        print("    %-14s %s  %s" % (me.get("name"),
                                    ok("运行中") if up else warn("未响应"),
                                    me.get("url")))
        print("    指纹 %s · 鉴权 %s" % (me.get("fingerprint") or "-",
                                        "已开启" if me.get("token") else "未开启"))

    # macOS 上真正会咬人的那几条，直接说清楚，别让人去猜
    for level, text in _macos_advice(me):
        print(("    " + warn("[!] ") if level == "warn" else "    " + dim("· ")) + text)

    # 对端：一眼看到连了几台
    online, total, rows = _peer_summary(timeout=2)
    print("\n  【对端】已连接 %s / %d 台" % (ok(str(online)) if online else warn(str(online)), total))
    if total:
        for name, up, ms in rows:
            dot = ok("●") if up else err("○")
            print("    %s %-16s %s" % (dot, name, dim("%.0f ms" % ms) if up else dim("不可达")))
    else:
        print(dim("    还没有对端。`fleet invite` 生成邀请，或 `fleet scan` 扫同网段。"))

    # tailnet（装了才显示，没装不打扰）
    try:
        st, ts_err = _tailscale_status(timeout=3)
    except Exception:
        st, ts_err = None, "unavailable"
    if st:
        p = st.get("Peer") or {}
        ts_on = sum(1 for v in p.values() if isinstance(v, dict) and v.get("Online"))
        print("\n  【Tailnet】本机 %s · 对端 %d/%d 在线"
              % (", ".join(_ts_self_ips(st)) or "-", ts_on, len(p)))
        print(dim("    地址漂移自愈：fleet tailnet sync"))
    elif ts_err and "未安装" not in (ts_err or ""):
        print("\n  【Tailnet】%s" % dim(ts_err))

    print("""
  【常用命令】
    fleet start / stop / restart         启停本机节点
    fleet status / nodes / doctor        节点状态、对端、体检
    fleet ping <节点>                    一秒探活（只 GET /，不握手）
    fleet shell <节点>                   交互式 shell（PTY，可跑 vim/top，替代 ssh）
    fleet forward -L 8080:h:80 <节点>    端口转发（等价 ssh -L / -R）
    fleet exec <节点> "<命令>"            在别的设备上执行
    fleet cp ./f.py nas:/opt/f.py        推代码；反向即拉回来
    fleet scan [网段]                    主动找同网段节点（--add 直接登记）
    fleet tailnet [status|sync|up]       Tailscale 状态 / 地址自愈 / 起隧道
    fleet selfcheck                      只读自检（可机读故障列表）
    fleet repair [--auto]                自修复：先给计划；--auto 有界自动修复
    fleet up [--check]                   升级本地脚本（git 有新版才动，失败自动回滚）
    fleet update [--rollback]            升级 / 回滚本机节点服务
    fleet ask "<自然语言>" [--run]        生成计划 + 只读诊断
    fleet prompt [主题]                  查看内置提示词（可直接粘给 AI）
    fleet invite --qr / join <URI>       邀请配对
    fleet identity show/export/import    身份查看/备份/恢复
    fleet menu                           交互式菜单

  【可直接粘给 AI 的提示词】（完整版：fleet prompt）
    · 自检修复：先 fleet_selfcheck；有可修项再 fleet_repair_loop，失败即停并报告。
    · 排障：先只读收集证据，再给“结论/证据/建议”，变更前先确认。
    · 协作：fleet_plan 拆步 + 验收标准，按能力分给 self/peer，失败两次停止。

  安全：fleet repair 只做重启/权限/pid 等白名单动作；rollback 需显式开启；
        不会删除数据、轮换 token 或执行任意命令。
""")
    print("=" * 66)
    return 0


def cmd_menu(a):
    """中文交互菜单：零依赖纯 input，手机 Termux / 桌面终端都能跑。"""
    try:
        import readline  # 行编辑（历史上下键），无副作用
    except Exception:
        pass  # 某些环境缺失 readline 时忽略，不影响功能
    clear = (not a.no_clear)

    def _banner():
        if clear:
            os.system("clear" if os.name != "nt" else "cls")
        print("=" * 56)
        print("  MCP Fleet v%s · AI 对等协作网 · 主控 CLI" % VERSION)
        print("=" * 56)

    def _node_list():
        ns = load_registry().get("nodes", {})
        if not ns:
            print("  还没有任何副端。")
            return []
        print("\n  已登记副端：")
        for i, (n, m) in enumerate(sorted(ns.items()), 1):
            url = m.get("url", "?")
            desc = m.get("description", "")
            print(f"    [{i:>2}] {n:<15} {url:<40} {desc}")
        return list(sorted(ns.keys()))

    def _probe(name, timeout=5):
        url, meta = resolve_target(name)
        token = meta.get("token", "")
        try:
            info = http_probe(url, timeout=timeout, token=token)
            return True, info
        except Exception as e:
            return False, str(e)

    def _pick_node(prompt="选择副端编号: "):
        ns = load_registry().get("nodes", {})
        if not ns:
            print("  没有副端。先用 [11] 添加一个。")
            input("  按回车继续...")
            return None
        names = list(sorted(ns.keys()))
        for i, n in enumerate(names, 1):
            print(f"    [{i:>2}] {n}")
        try:
            c = int(input("\n  " + prompt).strip() or "0")
            return names[c - 1] if 1 <= c <= len(names) else None
        except (ValueError, IndexError):
            return None

    while True:
        _banner()
        print("""
  请选择操作：

  【节点管理】
   [1] 列出已登记的副端          [2] 添加副端
   [3] 移除副端                  [4] 双向配对（pair）

  【诊断与体检】
   [5] 连通性体检（probe）       [6] 完整链路体检（doctor）
   [7] 批量体检全部节点（health）

  【远程操控】
   [8] 执行命令（exec）          [9] 调用工具（call）
  [20] 交互式 shell（替代 ssh） [21] 端口转发（-L / -R）
  [10] 列远端工具清单（tools）

  【消息与拓扑】
  [11] 发送消息（msg send）     [12] 查看消息历史
  [13] 打印 mesh 拓扑图

  【安装与发现】
  [14] 一键安装副端命令（install-cmd）
  [15] SSH 远程装副端（bootstrap）
  [16] 局域网发现（discover）

  【自愈与提示词】
  [17] 本机自检 / 自修复（selfcheck + repair）
  [18] 查看 AI 提示词（prompt）
  [19] 升级本地脚本（up）

  [0] 退出""")
        print("")
        print("  提示词速用：fleet prompt self_repair  然后原样粘给 AI 客户端。")
        print()
        try:
            choice = input("  请选择 (0-21): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  再见！")
            return 0

        if choice == "0":
            print("  再见！")
            return 0

        # ---- 1: list ----
        elif choice == "1":
            _banner()
            _node_list()
            input("\n  按回车继续...")

        # ---- 2: add ----
        elif choice == "2":
            _banner()
            print("\n  添加副端")
            print("  " + "-" * 40)
            name = input("  副端名字: ").strip()
            if not name:
                input("  名字不能为空，按回车..."); continue
            url = input("  地址 (如 http://192.168.1.10:3100/mcp): ").strip()
            if not url:
                input("  地址不能为空，按回车..."); continue
            token = input("  令牌 (没有则留空): ").strip()
            no_test = input("  跳过连通测试? (y/N): ").strip().lower() == "y"
            args = argparse.Namespace(name=name, url=url, ip=None, port=3100,
                                      token=token, no_test=no_test, timeout=5,
                                      force=False)
            r = cmd_add(args)
            if r == 0:
                print(f"\n  已添加 {name}")
            else:
                print(f"\n  添加失败，返回码 {r}")
            input("  按回车继续...")

        # ---- 3: remove ----
        elif choice == "3":
            _banner()
            name = _pick_node("要删除的副端编号: ")
            if name:
                args = argparse.Namespace(name=name)
                cmd_remove(args)
                print(f"  已移除 {name}")
            input("  按回车继续...")

        # ---- 4: pair ----
        elif choice == "4":
            _banner()
            print("\n  双向配对")
            print("  格式：设备码@地址  或  直接地址")
            target = input("  输入对方地址: ").strip()
            if not target:
                input("  不能为空，按回车..."); continue
            token = input("  对方令牌 (没有则留空): ").strip() or None
            port = 3100
            args = argparse.Namespace(target=target, port=port, token=token)
            r = cmd_pair(args)
            if r == 0:
                print("  配对成功！")
            else:
                print(f"  配对失败，返回码 {r}")
            input("  按回车继续...")

        # ---- 5: probe ----
        elif choice == "5":
            _banner()
            name = _pick_node("要体检的副端编号: ")
            if name:
                args = argparse.Namespace(target=name, timeout=5)
                r = cmd_probe(args)
            input("\n  按回车继续...")

        # ---- 6: doctor ----
        elif choice == "6":
            _banner()
            name = _pick_node("要体检的副端编号: ")
            if name:
                args = argparse.Namespace(target=name, timeout=10, deep=False)
                r = cmd_doctor(args)
            input("\n  按回车继续...")

        # ---- 7: health ----
        elif choice == "7":
            _banner()
            args = argparse.Namespace(deep=False)
            cmd_health(args)
            input("\n  按回车继续...")

        # ---- 8: exec ----
        elif choice == "8":
            _banner()
            name = _pick_node("要执行的副端编号: ")
            if name:
                cmd = input("  命令: ").strip()
                if cmd:
                    args = argparse.Namespace(target=name, command=[cmd], sudo=False,
                                              timeout=None, work_dir=None, stdin=None)
                    r = cmd_exec(args)
            input("\n  按回车继续...")

        # ---- 9: call ----
        elif choice == "9":
            _banner()
            name = _pick_node("要调用的副端编号: ")
            if name:
                tool = input("  工具名: ").strip()
                args_json = input("  参数 JSON (如 {\"path\":\"/\"}): ").strip() or "{}"
                if tool:
                    args = argparse.Namespace(target=name, tool=tool, args=args_json,
                                              timeout=30, agent=None, port=3100)
                    r = cmd_call(args)
            input("\n  按回车继续...")

        # ---- 10: tools ----
        elif choice == "10":
            _banner()
            name = _pick_node("要列工具的副端编号: ")
            if name:
                args = argparse.Namespace(target=name)
                r = cmd_tools(args)
            input("\n  按回车继续...")

        # ---- 11: msg send ----
        elif choice == "11":
            _banner()
            name = _pick_node("发送给副端编号: ")
            if name:
                text = input("  消息内容: ").strip()
                if text:
                    args = argparse.Namespace(target=name, text=text, file=None)
                    r = cmd_msg(args)
            input("\n  按回车继续...")

        # ---- 12: msg history ----
        elif choice == "12":
            _banner()
            name = _pick_node("要查看消息历史的副端编号: ")
            if name:
                args = argparse.Namespace(target=name, limit=20, text="", file=None)
                r = cmd_msg(args)
            input("\n  按回车继续...")

        # ---- 13: mesh ----
        elif choice == "13":
            _banner()
            args = argparse.Namespace(deep=False)
            cmd_mesh(args)
            input("\n  按回车继续...")

        # ---- 14: install-cmd ----
        elif choice == "14":
            _banner()
            name = input("  副端名字 (默认 pi): ").strip() or "pi"
            args = argparse.Namespace(name=name, port=3100, prefix=None,
                                      sudo_pass=None, tunnel=False, mode=None,
                                      auth_token=None, allow_ips=None,
                                      directory=None, directory_token=None,
                                      no_keepalive=False, generate_token=False,
                                      require_auth=False, mirror="auto",
                                      offline=False, uninstall=False)
            cmd_install_cmd(args)
            input("\n  按回车继续...")

        # ---- 15: bootstrap ----
        elif choice == "15":
            _banner()
            print(warn("  bootstrap 需要较多参数（SSH 用户/密钥/密码等），"))
            print(warn("  请直接在终端用命令行执行："))
            print()
            print("  python3 fleet.py bootstrap --help")
            print()
            print("  示例：")
            print("  python3 fleet.py bootstrap pi@192.168.1.10 --name pi --generate-token")
            input("\n  按回车继续...")

        # ---- 16: discover ----
        elif choice == "16":
            _banner()
            deep = input("  深度扫描（慢）? (y/N): ").strip().lower() == "y"
            args = argparse.Namespace(deep=deep)
            cmd_discover_proto(args)
            input("\n  按回车继续...")

        elif choice == "17":
            _banner()
            args = argparse.Namespace(json=False, timeout=30, self_url=None)
            cmd_selfcheck(args)
            try:
                yn = input("\n  尝试自动修复可修项? (y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                yn = ""
            if yn == "y":
                args = argparse.Namespace(json=False, auto=True, attempts=2,
                                          rollback=False, timeout=120,
                                          self_url=None)
                cmd_repair(args)
            input("\n  按回车继续...")

        elif choice == "18":
            _banner()
            print(prompt_text())
            input("\n  按回车继续...")

        elif choice == "19":
            _banner()
            args = argparse.Namespace(check=False, ref="main", force=False,
                                      no_restart=False, json=False)
            cmd_up(args)
            input("\n  按回车继续...")

        elif choice == "20":
            _banner()
            name = _pick_node("要连接哪台设备: ")
            if name:
                args = argparse.Namespace(target=name, agent="auto", port=3100,
                                          timeout=60, json=False, no_sync=False,
                                          cwd="", insecure=False)
                cmd_shell(args)

        # ---- 21: forward ----
        elif choice == "21":
            _banner()
            print("\n  端口转发（等价 ssh -L / -R）")
            print("  " + "-" * 40)
            print("  格式：[绑定地址:]端口:目标主机:目标端口")
            print("  例：8080:127.0.0.1:80   或   127.0.0.1:8080:db.internal:3306")
            print()
            name = _pick_node("要经哪台设备转发: ")
            if not name:
                continue
            try:
                spec = input("  转发规格: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not spec:
                continue
            try:
                direction = input("  方向 [L=本地监听/R=副端监听，默认 L]: ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                continue
            args = argparse.Namespace(target=name, agent="auto", port=3100,
                                      timeout=30, json=False, no_sync=False,
                                      local=[spec] if direction != "R" else None,
                                      remote=[spec] if direction == "R" else None,
                                      bind="127.0.0.1", list=False, insecure=False)
            cmd_forward(args)

        else:
            print("  无效选择，按回车继续...")
            input()


def main():
    global AUTO_SYNC
    p = build_parser()
    a = p.parse_args()
    if not getattr(a, "cmd", None):
        # v5.0.6.3: 无参数显示本机状态 + 中文命令说明
        return show_fleet_status()
    if a.cmd == "exec" and not a.command:
        return die("命令为空")
    # 地址自动跟随：--no-sync 或 FLEET_NO_SYNC=1 关掉（离线/隔离网络里更省事）
    if getattr(a, "no_sync", False) or os.environ.get("FLEET_NO_SYNC") == "1":
        AUTO_SYNC = False
    try:
        return a.func(a)
    except KeyError as e:
        # str(KeyError) 返回的是 repr，会把 \n 显示成字面量，取 args[0] 才对
        msg = e.args[0] if e.args else e
        return die(str(msg).strip("'\""))
    except MCPError as e:
        # 远端/协议错误本身就是给人看的，别再套一层 "MCPError:"
        return die(str(e))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        return die("%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    sys.exit(main())
