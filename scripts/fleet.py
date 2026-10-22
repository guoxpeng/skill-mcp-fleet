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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------- 基础

VERSION = "1.2.0"
PROTOCOL = "2024-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))
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


def _opener_for(url):
    """内网/本机地址不走 http_proxy：机器上配了代理时，局域网节点会被代理成 404/超时。"""
    import ipaddress
    import urllib.parse
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host == "localhost" or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    import ssl
    handlers = []
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    if urllib.parse.urlsplit(url).scheme == "https":
        if direct:
            # 局域网节点常为自签证书：仅私网/回环豁免校验
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        # 公网 HTTPS（GitHub 等）保持系统默认证书校验，防中间人
    return urllib.request.build_opener(*handlers)


class MCPError(Exception):
    pass


class MCPClient:
    """Streamable HTTP 客户端：initialize → notifications/initialized → tools/*"""

    def __init__(self, url, timeout=30, token=""):
        self.url = url
        self.timeout = timeout
        self.sid = None
        self.info = {}
        self.token = (token or "").strip()

    # -- 传输层 --------------------------------------------------
    def _post(self, payload, timeout=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        try:
            with _opener_for(self.url).open(req, timeout=timeout or self.timeout) as r:
                if r.headers.get("Mcp-Session-Id"):
                    self.sid = r.headers["Mcp-Session-Id"]
                body = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                hint = ("副端开了鉴权，但本地没有它的 token。\n"
                        "     补一个： python fleet.py add --name <名字> --url %s --token <令牌>\n"
                        "     （令牌在副端 /opt/<名字>_mcp/mcp_agent_config.json 的 auth_token，"
                        "或跑 `bash mcp-ctl.sh` 时打印过）" % self.url) if not self.token else \
                       ("副端拒绝了令牌（HTTP %d）。确认 auth_token 与主端填的一致，"
                        "或本机 IP 不在 allow_ips 白名单里。" % e.code)
                raise MCPError("HTTP %s %s\n     %s" % (e.code, e.reason, hint))
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

    # -- 协议层 --------------------------------------------------
    def handshake(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp-fleet", "version": VERSION}}})
        self.info = r.get("result", {}).get("serverInfo", {}) or {}
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized",
                        "params": {}}, timeout=15)
        except MCPError:
            pass                      # 通知失败不影响后续
        return self.info

    def list_tools(self):
        r = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        if "error" in r:
            raise MCPError(str(r["error"]))
        return r.get("result", {}).get("tools", [])

    def call(self, tool, arguments=None, timeout=None):
        r = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments or {}}},
                       timeout=timeout)
        if "error" in r:
            raise MCPError(str(r["error"]))
        res = r.get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) or [])
        if not text:
            text = json.dumps(res, ensure_ascii=False, indent=2)
        return text, bool(res.get("isError"))


def http_probe(base_url, timeout=8, token="", retries=3):
    """GET 副端根路径，拿 {status, server, version, tools, ...}。

    隧道刚起来 / 网络抖动时首次请求可能超时，默认重试 3 次（指数退避
    1s / 2s / 4s），都失败才抛异常。retries=0 可关闭重试。
    """
    base = base_url[:-4] if base_url.endswith("/mcp") else base_url.rstrip("/")
    req = urllib.request.Request(base + "/", headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    last_err = None
    for attempt in range(max(1, retries + 1)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore") or "{}")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s ...
    raise last_err


def normalize_url(host_or_url, port=3100):
    s = (host_or_url or "").strip()
    if not s:
        raise ValueError("空的地址")
    if not s.startswith(("http://", "https://")):
        s = "http://%s:%d" % (s, port)
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


def load_registry():
    """本地登记表：即使 agent 不支持 MCP，也能用本脚本操控副端。"""
    if os.path.isfile(REGISTRY):
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"nodes": {}}


def save_registry(reg):
    d = os.path.dirname(REGISTRY)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 登记表里有各副端的明文 Bearer 令牌，必须收紧权限；
    # 原来既不 chmod 也不设 umask，Linux 上默认 0644 = 同机任何用户可读。
    try:
        os.chmod(REGISTRY, 0o600)
    except Exception:
        pass


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
    except MCPError as e:
        print(warn("[!] ") + "exec 试调失败：%s" % e)
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


def cmd_health(a):
    nodes = discover_nodes(a.agent)
    if not nodes:
        print(warn("没有发现任何副端。"))
        return 1
    rows, bad = [], 0
    for name in sorted(nodes):
        url = nodes[name]["url"]
        ntok = nodes[name].get("token", "")
        row = {"name": name, "url": url}
        t0 = time.time()
        try:
            info = http_probe(url, timeout=a.timeout, token=ntok)
            row.update(status="up", version=info.get("version"),
                       tools=info.get("tools"), ms=int((time.time() - t0) * 1000))
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
        rows.append(row)

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 1 if bad else 0

    w = max(len(r["name"]) for r in rows)
    print("副端体检（%d 台，%s）\n" % (len(rows), time.strftime("%Y-%m-%d %H:%M:%S")))
    for r in rows:
        if r["status"] == "up":
            extra = ("  deep=%s" % r["deep"]) if "deep" in r else ""
            print("  %s %-*s  v%-6s %3s 工具  %5d ms%s"
                  % (ok("UP  "), w, r["name"], r.get("version") or "?", r.get("tools") or 0,
                     r["ms"], extra))
        elif r["status"] == "degraded":
            print("  %s %-*s  HTTP 通但工具调用失败" % (warn("DEGR"), w, r["name"]))
        else:
            print("  %s %-*s  %s" % (err("DOWN"), w, r["name"], r.get("error", "")))
    print()
    print(ok("全部在线。") if not bad else warn("%d 台异常。" % bad))
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

def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


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
    print(ok("=" * 60))
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
    return me, MCPClient(me["url"], getattr(a, "timeout", 30), token=me.get("token") or "")


def _http_json(url, token="", timeout=10):
    import urllib.request as _ur
    req = _ur.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with _opener_for(url).open(req, timeout=timeout) as resp:
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
    health = _http_json(base + "/fleet/health", me.get("token") or "")
    ident = _http_json(base + "/fleet/identity", me.get("token") or "")
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
        with _opener_for(base).open(req, timeout=10) as resp:
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
    """v5.2.6: 升级本机节点（默认从远端拉最新发布包；--offline 用本地）。"""
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
    client = MCPClient(me["url"], getattr(a, "timeout", 30), token=me.get("token") or "")
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
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("targets", help="只输出副端名字（供脚本消费）")
    sp.add_argument("--agent", default="auto")
    sp.set_defaults(func=cmd_targets)

    common(sub.add_parser("tools", help="列副端工具清单")).set_defaults(func=cmd_tools)
    common(sub.add_parser("probe", help="连通性 + 握手 + 试调（只读）")).set_defaults(func=cmd_probe)
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
    sp = local(sub.add_parser("update", help="v5.2: 升级/回滚本机节点"))
    sp.add_argument("--rollback", action="store_true", help="回滚到最近一次升级备份")
    sp.add_argument("--offline", action="store_true",
                    help="不联网，使用节点目录里的本地安装器")
    sp.set_defaults(func=cmd_update)
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

    return p


def show_fleet_status():
    """v5.0.6.3: 无参数时显示本机状态 + 中文命令说明。"""
    import urllib.request
    # 清屏
    os.system("clear" if os.name != "nt" else "cls")
    print("=" * 60)
    print("  MCP Fleet · AI 对等协作网 · 主控 CLI v5.0.6")
    print("=" * 60)

    # ---- 本机状态 ----
    nodes = load_registry().get("nodes", {})
    print("\n  【本机状态】")
    if not nodes:
        print("    还没有任何副端。")
    else:
        print("    已登记副端：%d 个" % len(nodes))
        for i, (n, m) in enumerate(sorted(nodes.items()), 1):
            url = m.get("url", "?")
            has_token = bool(m.get("token", ""))
            # 探活
            status = "  在线" if probe_anonymous(url, timeout=3) else "  离线"
            print("    [%d] %-15s %-40s %s %s" % (
                i, n, url[:40], status, "(已配令牌)" if has_token else "(无令牌)"))

    # ---- 本机 agent 信息（如果有） ----
    for cfg_path in agent_config_paths().get("workbuddy", []):
        if os.path.isfile(cfg_path):
            try:
                cfg = load_json_config(cfg_path)
                servers = cfg.get(servers_key(cfg), {})
                if servers:
                    print("\n  【本机 agent】")
                    for name, v in list(servers.items())[:3]:
                        url = v.get("url", "")
                        token = _token_from_headers(v)
                        print("    %s  %s  %s" % (
                            name, url[:45], "已配令牌" if token else "未配令牌"))
                    break
            except Exception:
                pass
            break

    # ---- 常用命令 ----
    print("\n  【常用命令】")
    print("    fleet list                    列出已登记的副端")
    print("    fleet add --name x --url ...  添加副端")
    print("    fleet probe <名字>            连通性体检")
    print("    fleet doctor <名字>           完整链路体检")
    print("    fleet exec <名字> <命令>      在副端执行命令")
    print("    fleet tools <名字>            列副端工具清单")
    print("    fleet health                  批量体检全部节点")
    print("    fleet menu                    进入中文交互菜单")
    print("    fleet install-cmd --name pi   打印副端安装命令")
    print("    fleet pair <地址> --token T   双向配对")

    print("\n  【快速开始】")
    print("    1. 在副端安装：bash <(curl -fsSL <URL>) --name my-pc")
    print("    2. 在主端注册：fleet add --name my-pc --url http://IP:3100/mcp --token <令牌>")
    print("    3. 远程操控：fleet exec my-pc \"hostname; df -h\"")
    print()
    print("  输入 fleet menu 进入中文交互菜单")
    print("=" * 60)
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
        print("  MCP Fleet · AI 对等协作网 · 主控 CLI v5.0.6")
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
  [10] 列远端工具清单（tools）

  【消息与拓扑】
  [11] 发送消息（msg send）     [12] 查看消息历史
  [13] 打印 mesh 拓扑图

  【安装与发现】
  [14] 一键安装副端命令（install-cmd）
  [15] SSH 远程装副端（bootstrap）
  [16] 局域网发现（discover）

  [0] 退出""")
        print()
        try:
            choice = input("  请选择 (0-16): ").strip()
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
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        return die("%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    sys.exit(main())
