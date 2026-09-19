#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-fleet 统一命令行 —— 主端操作副端集群
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
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 基础

VERSION = "1.1.0"
REGISTRY = os.path.join(os.path.expanduser("~"), ".mcp-fleet", "nodes.json")
PROTOCOL = "2024-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))

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
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
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


def http_probe(base_url, timeout=8, token=""):
    """GET 副端根路径，拿 {status, server, version, tools, ...}。"""
    base = base_url[:-4] if base_url.endswith("/mcp") else base_url.rstrip("/")
    req = urllib.request.Request(base + "/", headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


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
        print(ok("  [1/6] TCP ") + "%s:%d 通（%.0f ms）" % (host, port, (time.time() - t0) * 1000))
    except Exception as e:
        print(err("  [1/6] TCP ") + "%s:%d 不通：%r" % (host, port, e))
        fails.append("TCP 不通：云主机/容器要在安全组或平台侧放行该端口；内网确认 IP 与防火墙。")

    # 2 HTTP
    try:
        info = http_probe(url, token=meta.get("token", ""))
        print(ok("  [2/6] HTTP") + " 200，server=%s v%s，tools=%s，root=%s"
              % (info.get("server"), info.get("version"), info.get("tools"), info.get("root")))
    except Exception as e:
        print(err("  [2/6] HTTP") + " 失败：%r" % (e,))
        fails.append("HTTP 探活失败：副端进程可能没起。容器里跑 "
                     "`bash /opt/%s_mcp/mcp-ctl.sh status`；systemd 机器跑 "
                     "`systemctl status %s-mcp`。" % (a.target, a.target))

    # 3 MCP
    c = MCPClient(url, a.timeout, token=meta.get("token", ""))
    try:
        c.handshake()
        tools = c.list_tools()
        print(ok("  [3/6] MCP ") + "握手 OK，%d 个工具" % len(tools))
    except MCPError as e:
        print(err("  [3/6] MCP ") + "失败：%s" % e)
        fails.append("MCP 协议层失败：用 `python scripts/probe_fleet.py %s` 交叉验证；"
                     "若它通而 agent 不通，是 agent 侧配置/版本问题。" % url)
        tools = []

    # 4 真实调用
    if tools:
        try:
            text, iserr = c.call("exec", {"command": "echo MCP_E2E_OK; hostname"}, timeout=a.timeout)
            code, out, _ = parse_exec(text)
            mark = err if (iserr or code) else ok
            print(mark("  [4/6] E2E ") + "exit=%d  %s" % (code, out.replace("\n", " / ")[:110]))
            if iserr or code:
                fails.append("exec 返回非 0：看上面输出。命令超时就把 mcp_agent_config.json "
                             "里的 command_timeout 调大。")
        except MCPError as e:
            print(err("  [4/6] E2E ") + "失败：%s" % e)
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
            print(ok("  [5/6] 环境") + " uid=%s，docker=%s" % (uid, docker))
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
            print(ok("  [6/6] 配置") + " %s" % inst)
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
                print(dim("    取令牌：在副端跑  grep auth_token /opt/%s_mcp/mcp_agent_config.json" % a.name))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(err("[x] ") + "副端在线但拒绝访问（HTTP %d）—— 需要 --token" % e.code)
                print(dim("    取令牌：在副端跑  grep auth_token /opt/%s_mcp/mcp_agent_config.json" % a.name))
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
            print("  容器/云主机 → 内网 IP 主端够不到，到副端跑：bash /opt/%s_mcp/mcp-tunnel.sh" % a.name)
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


# ---------------------------------------------------------------- 入口

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
    sp = common(sub.add_parser("doctor", help="完整链路体检"))
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

    sp = sub.add_parser("directory", help="查看/设置地址目录（副端上报地址、主端自动跟随）")
    sp.add_argument("--url", help="地址目录服务地址，如 http://192.168.1.10:8790")
    sp.add_argument("--token", help="目录的 X-Fleet-Token（可选）")
    sp.add_argument("--clear", action="store_true", help="清除已配置的地址目录")
    sp.add_argument("--timeout", type=int, default=8)
    sp.set_defaults(func=cmd_directory)
    return p


def main():
    global AUTO_SYNC
    p = build_parser()
    a = p.parse_args()
    if not getattr(a, "cmd", None):
        p.print_help()
        return 0
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
