#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 副端 Agent（零依赖 · 纯 Python 标准库）
============================================
在任意 Linux 设备（NAS / 飞牛 OS / 树莓派 / 云主机…）本机运行，
把「本机 shell + 文件读写 + Docker + systemd」暴露为 MCP 工具，
供 WorkBuddy（MCP 主端）通过 HTTP 远程调用。

特点：
  * 纯标准库，不需要 pip install 任何东西
  * 命令在设备本机直接执行，不经 SSH
  * 双传输：Streamable HTTP (POST /mcp) + SSE (GET /sse)
  * 工具名可加前缀（tool_prefix），多设备共用一套代码
  * 兼容旧名（nas_exec 等）作为别名

启动：
  python3 mcp_agent.py --host 0.0.0.0 --port 3100

工具（默认无前缀）：
  exec           执行 shell 命令（支持 sudo 提权）
  read           读文件（支持行范围）
  write          写 / 追加文件
  edit           替换文件中文本块
  list_dir       列目录
  docker_ps      列出容器
  docker_logs    查看容器日志
  docker_restart 重启容器
  systemctl      管理 systemd 服务
  service_logs   查看 systemd 服务日志（journalctl）
  sysinfo        系统概览
  http_get       本机发起 HTTP 请求
"""

import argparse
import base64
import json
import os
import re
import hmac
import ipaddress
import locale
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import io
import urllib.parse
import urllib.request
import urllib.error
try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:
    # Python 3.6 没有 ThreadingHTTPServer（3.7+ 才有），手动构造
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        pass
try:
    from fleet_proto import (MailEngine, DiscoveryLayer, MessageDelivery,
                             SyncEngine, PeerHealth,
                             DEFAULT_DISCOVER_PORT, PROTO_VERSION)
    _HAS_PROTO = True
except ImportError:
    _HAS_PROTO = False

# v5.0: Web Dashboard 单文件页面（零 CDN 依赖）
try:
    from dashboard_html import DASHBOARD_HTML
    _HAS_DASHBOARD = True
except ImportError:
    _HAS_DASHBOARD = False

# v5.2: 安全核心与 Agent 智能层（可选依赖，缺失时优雅降级）
try:
    import fleet_core as _core
    _HAS_CORE = True
except Exception:                       # pragma: no cover - 文件缺失/导入失败
    _core = None
    _HAS_CORE = False
try:
    import fleet_agent as _agent_layer
    _HAS_AGENT = True
except Exception:                       # pragma: no cover
    _agent_layer = None
    _HAS_AGENT = False

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mcp_agent_config.json")

# 日志行缓冲：副端是被 nohup/systemd 拉起的常驻进程，stdout 重定向到文件时
# 默认是块缓冲，启动横幅和诊断信息会一直卡在缓冲区里；进程被 kill 时直接丢失，
# 表现为 agent.log 一直是空的。这里强制行缓冲。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

DEFAULT_CONFIG = {
    "name": "node",             # 副端标识（显示在状态页与 serverInfo）
    "tool_prefix": "",          # 工具名前缀，如 "nas_"；留空则用裸名（exec/read/...）
    "version": "5.2.0",
    "sudo_password": "",        # 留空且非 root 时会尝试免密 sudo
    "work_dir": "/",            # 默认工作目录
    "command_timeout": 120,     # 秒
    "max_output_bytes": 200000, # 单次输出上限，防止刷爆上下文
    "allowed_roots": ["/"],     # 文件读写允许的路径前缀（安全边界）
    "enable_docker": True,      # 无 Docker 的设备可关掉
    "login_shell": True,        # True=bash -lc（会 source /etc/profile）；
                                # 容器里 profile 打了欢迎横幅（PAI-DSW 等）时设 False，
                                # 改成 bash -c，输出干净且 PATH 通常够用
    # ---- 访问控制（v3.0）--------------------------------------------------
    # 非空则所有请求必须带 Authorization: Bearer <token>（或 X-Fleet-Token）。
    # 副端以 root 运行、exec 等于 root shell，走公网隧道时**必须**设置。
    "auth_token": "",
    # 可选来源 IP 白名单，支持单个 IP 或 CIDR。留空 = 不限制。
    # 例：["192.168.1.0/24", "10.8.0.2"]
    "allow_ips": [],
    # ---- 加固项（v3.1）----------------------------------------------------
    # True 时空 auth_token 直接拒绝启动（隧道/公网场景必开，内网建议开）。
    "require_auth": False,
    # CORS 白名单。留空 = 完全不发 CORS 响应头（默认，最安全）。
    # MCP 客户端是程序不是浏览器，本就不需要 CORS；
    # 只有确有浏览器端调用需求时，才填具体来源，如 ["http://localhost:3000"]。
    "cors_allow_origins": [],
    # v5.0.7: 只读模式（审计观察节点）。True 时 exec/write/edit 等变更类
    # 工具调用全部拒绝；tools/list 仍返回完整清单（工具数声明不变，拒绝
    # 发生在调用层并计入审计日志）。命令行 --read-only 可直接开启。
    "read_only": False,
    # 单请求体上限（字节），防止 Content-Length 撑爆内存/线程。
    "max_body_bytes": 1048576,       # 1 MB
    # SSE 并发连接上限，防止线程被常驻连接打满。
    "max_sse_conns": 16,
    # v5.0.7: 并发连接总数上限（信号量，连接级）——ThreadingHTTPServer 每连接
    # 一线程且无上限，网络扫描/并发洪水可打满线程。SSE 常驻连接也占槽位，
    # 默认 64 保证 SSE(16) 之外仍有 48 个普通请求额度。超限连接直接 503。
    "max_http_threads": 64,
    # ---- 对等消息（v5.0.3）------------------------------------------------
    # /fleet/mail 入站消息限流（每分钟令牌数）。与工具层限流独立——节点间
    # 消息（广播一条 = 每个 peer 一次 POST + 投递重试）天然比人工调用高频，
    # 默认 600/min 足够 10 节点规模，配对的失控节点也塞不爆磁盘。
    "mail_rate_per_min": 600,
    # inbox 容量硬上限（条数）。超出后拒收并返回 507，防止消息 flooding
    # 耗尽磁盘（正常使用远达不到；配合 maintenance compaction 长期稳定）。
    "max_inbox_msgs": 5000,
    # ---- 稳定性（v3.4）----------------------------------------------------
    # 单连接空闲多少秒后断开。原来没有超时：隧道/NAT 留下的半开连接会永久占住线程，
    # 日积月累把 agent 拖到无响应（进程还在，保活守护看不出来）。
    "socket_timeout": 300,
    # ---- 对等互控（v3.5）--------------------------------------------------
    # 不再区分主端/副端：每个节点既能被连，也能通过 peer_* 工具去连别的节点。
    "peer_tools": True,         # False = 不暴露 peers/peer_call/peer_add/peer_remove
    "peer_max_hops": 3,         # A→B→C… 最多转几跳，防止互相转发成环
    "self_url": "",             # 别的节点回连本机用的地址；留空=自动（隧道地址 > 内网 IP）
    # ---- Tailscale 自动组网（v4.0）-----------------------------------------
    "tailnet_auto_mesh": True,  # 启动时自动发现 tailnet 上的 fleet 节点并 peer_add
    "tailnet_name": "",         # tailscale up --login-server 或 tailnet 名（留空=用已登录的）
    "tailnet_scan_interval": 60,# 每 N 秒扫一次 tailnet 发现新节点
    "tailnet_auto_trust": True, # 同 tailnet 的节点自动互信（Tailscale 端到端加密），省去逐对配 token
    # ---- Agent 协作（v4.0）------------------------------------------------
    "collab_tools": True,       # 暴露 caps/mesh_caps/delegate/group_* 工具
    # ---- 安全加固 + 可观测性（v4.0）----------------------------------------
    "audit_log": True,          # 记录 peer_call/msg_send 到 audit.log
    "rate_limit_per_min": 120,  # 每节点入站 peer_call 每分钟上限（令牌桶）
    # ---- v4.2: 连接模式 + 控制模式 -----------------------------------------
    # connection_mode: permanent=长期(开机自启), temporary=临时(TTL 到自动退出)
    "connection_mode": "permanent",
    # ttl: 临时模式自动退出秒数（0=不自动退出，手动 Ctrl-C）
    "ttl": 0,
    # control_mode: direct=远程 agent 直接控制, assisted=本机 agent 辅助审查
    "control_mode": "direct",
    # assisted_allow: assisted 模式下远程 agent 可直接调的工具（安全白名单）
    "assisted_allow": ["sysinfo", "list_dir", "read", "msg_send", "msg_recv",
                       "msg_history", "peers", "peer_info", "caps", "mesh",
                       "node_mode", "request_assist"],
    # assisted_queue: 本机 agent 待处理的辅助请求队列文件
    "assisted_queue": "",
    # ---- v5.2: 身份 / 签名 / 策略 / 审计 / Agent 智能层 -------------------
    # 数据目录：身份、任务、审计、指标都放这里；留空 = 配置所在目录。
    "data_dir": "",
    # 要求所有跨节点请求必须携带 v5.2 签名（公网/多用户场景建议 True）。
    # 旧 token 鉴权在 compat_v5=True 时仍接受，便于与 v5 节点混跑。
    "require_signature": False,
    "compat_v5": True,
    # TLS 证书/私钥路径（installer --tls auto 写入）
    "tls_cert": "",
    "tls_key": "",
    # 运行模式：off=不启用自对话工具；local=启用提示词与任务记录；
    # assist=允许本机编排器执行只读任务（默认最保守）
    "agent_autonomy": "local",
    # 自对话任务记录上限（条），防磁盘膨胀
    "max_task_events": 2000,
    # 审计哈希链与旧日志的最大字节数
    "max_audit_bytes": 5242880,
    # 中继模式：本节点可为无公网入口的节点转发消息（需显式开启）
    "relay_enabled": False,
    # 低资源模式（手机/软路由）：关闭 Docker 工具、缩小限制
    "lite": False,
    # v5.2: 非 loopback 监听且无 token 时自动生成并写入（安全默认）
    "auto_secure": True,
}


_CONFIG_LOAD_ERROR = ""


def load_config():
    """读配置。解析失败时记下原因，由 die_if_config_corrupt 决定是否拒绝启动。

    损坏的配置**不能静默用默认值继续跑**：默认值里没有 auth_token，节点会
    以「无鉴权」姿态监听，而用户以为自己还开着鉴权——这比直接挂掉危险得多。
    """
    global _CONFIG_LOAD_ERROR
    _CONFIG_LOAD_ERROR = ""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            _CONFIG_LOAD_ERROR = str(e)
            print("[warn] 读配置失败，用默认值: %s" % e, file=sys.stderr)
    return cfg


def die_if_config_corrupt():
    """配置损坏则拒绝启动（退出码 2），并打印可操作的修复指引。"""
    if not _CONFIG_LOAD_ERROR:
        return
    print("", file=sys.stderr)
    print("[FATAL] 配置文件解析失败，拒绝启动：%s" % CONFIG_PATH, file=sys.stderr)
    print("        原因：%s" % _CONFIG_LOAD_ERROR, file=sys.stderr)
    print("", file=sys.stderr)
    print("        用默认值继续跑会让本节点以「无鉴权」姿态监听，", file=sys.stderr)
    print("        所以这里选择直接停下，而不是带病上岗。", file=sys.stderr)
    print("", file=sys.stderr)
    print("        修复：", file=sys.stderr)
    print("          1) 看文件哪里坏了：  python3 -c \"import json;json.load(open('%s'))\""
          % CONFIG_PATH, file=sys.stderr)
    print("          2) 备份后重装节点：  cp %s %s.broken && sh install.sh --name <名字>"
          % (CONFIG_PATH, CONFIG_PATH), file=sys.stderr)
    print("          3) 已装好的节点也可先手工补一个合法 JSON（至少保留 auth_token）",
          file=sys.stderr)
    sys.exit(2)


def save_config(cfg):
    """写回配置文件，并**无条件**收紧权限为 0600。

    配置里有 auth_token（可能还有 sudo_password）。0644 意味着同机任何用户
    都能读到它 —— 所以权限不能只在「首次安装」时设一次，必须每次写都收紧。
    """
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as e:
        print("[warn] 写配置失败: %s" % e, file=sys.stderr)
        return False
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    return True


def gen_token():
    """生成 32 字节 urlsafe 随机令牌（约 43 字符），不含引号/特殊字符。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


CFG = load_config()
SERVER_NAME = CFG.get("name", "node")
# 版本以代码为准：配置文件里的 version 是安装当时写的，重装时不覆盖，会显示成旧版本
AGENT_VERSION = DEFAULT_CONFIG["version"]
SERVER_VERSION = AGENT_VERSION
_STARTED_AT = time.time()       # 进程启动时刻：/health 报 uptime，看门可判「刚重启过」
PREFIX = CFG.get("tool_prefix", "") or ""
PROCESS_START = time.time()

# 无 fleet_agent 模块时的最小操作手册（保证任何客户端都能拿到指引）
FALLBACK_INSTRUCTIONS = """你是 MCP Fleet 节点「%s」上的操作 Agent。

工作方式：先观察（sysinfo/ps/docker_ps/service_logs/net_check/peers），再计划，
然后执行，最后自检。查询类工具优先；变更类操作要先说明影响并取得用户确认。

工具：exec/exec_async（命令）、read/write/edit/list_dir/search（文件）、
docker_*（容器）、systemctl/service_logs（服务）、ps/kill/net_check（诊断）、
peers/peer_call/mesh（跨节点）、msg_*（消息）、caps/delegate（委派）、
fleet_plan/fleet_reflect/fleet_review（自对话）。

安全红线：不执行 rm -rf /、mkfs、dd of=/dev/*、关机等危险命令；不泄露 token/
私钥/邀请码；跨设备操作前确认目标节点与验收标准。""" % (SERVER_NAME)


# 容器/自定义部署可用环境变量覆盖数据目录（Docker compose 传 /data）
DATA_DIR = os.environ.get("MCP_FLEET_DATA") or CFG.get("data_dir") or HERE
try:
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
except Exception:
    pass

IDENTITY = None
REPLAY_GUARD = None
AUDIT_CHAIN = None
METRICS = None
EXEC_POLICY = None
TASK_STORE = None

if _HAS_CORE:
    try:
        IDENTITY = _core.Identity.load_or_create(os.path.join(DATA_DIR, "identity.json"))
    except Exception as _e:
        print("[warn] 节点身份初始化失败（降级为 token 模式）: %s" % _e, file=sys.stderr)
    REPLAY_GUARD = _core.ReplayGuard()
    METRICS = _core.Metrics()
    try:
        EXEC_POLICY = _core.ExecPolicy.with_defaults(CFG.get("exec_policy") or {})
    except Exception as _e:
        print("[warn] 执行策略初始化失败，使用默认策略: %s" % _e, file=sys.stderr)
        EXEC_POLICY = _core.ExecPolicy.default()
    try:
        AUDIT_CHAIN = _core.AuditChain(os.path.join(DATA_DIR, "audit.log"))
    except Exception as _e:
        print("[warn] 审计链初始化失败，降级为旧审计: %s" % _e, file=sys.stderr)

if _HAS_AGENT:
    try:
        TASK_STORE = _agent_layer.TaskStore(os.path.join(DATA_DIR, "agent"))
    except Exception as _e:
        print("[warn] 任务存储初始化失败，自对话降级: %s" % _e, file=sys.stderr)



def _is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:      # Windows 下没有 geteuid
        return False


def _truncate(s):
    limit = int(CFG.get("max_output_bytes", 200000))
    b = s.encode("utf-8", "ignore")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore") + "\n...[输出已截断，共 %d 字节]" % len(b)


# ---------------------------------------------------------------------------
# 访问控制（v3.0）
#   副端以 root 运行，exec 等于一个 root shell。内网裸奔尚可接受，
#   一旦通过 cloudflared 之类的公网隧道暴露，**必须**开 token 鉴权。
# ---------------------------------------------------------------------------
AUTH_FAILS = {}          # {ip: [失败次数, 首次失败时间戳]}
AUTH_FAIL_LIMIT = 10     # 60 秒内失败超过这个数就拖慢响应
AUTH_FAIL_WINDOW = 60


def _norm_net(item):
    """把 "192.168.1.0/24" / "10.0.0.5" 归一成 ipaddress 网络对象；非法返回 None。"""
    s = str(item or "").strip()
    if not s:
        return None
    try:
        if "/" in s:
            return ipaddress.ip_network(s, strict=False)
        return ipaddress.ip_network(s + ("/32" if ":" not in s else "/128"), strict=False)
    except ValueError:
        return None


def _ip_allowed(ip):
    """allow_ips 为空 → 放行；否则必须在白名单内。"""
    nets = CFG.get("allow_ips") or []
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in nets:
        net = _norm_net(item)
        if net is None:
            continue
        # 只比较同族，避免 IPv4 地址去匹配 IPv6 网段
        if net.version == addr.version and addr in net:
            return True
    return False


def _note_auth_fail(ip):
    now = time.time()
    cnt, first = AUTH_FAILS.get(ip, [0, now])
    if now - first > AUTH_FAIL_WINDOW:
        cnt, first = 0, now
    cnt += 1
    AUTH_FAILS[ip] = [cnt, first]
    if cnt > AUTH_FAIL_LIMIT:
        time.sleep(1.0)          # 简单拖慢暴力破解，不阻塞其他来源
    if cnt == AUTH_FAIL_LIMIT:
        print("[warn] 来自 %s 的鉴权失败已达 %d 次，可能存在探测/爆破" % (ip, cnt),
              file=sys.stderr)


def _token_of(headers):
    """从请求头取 token：优先 Authorization: Bearer，其次 X-Fleet-Token。"""
    raw = (headers.get("Authorization") or "").strip()
    if raw:
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if len(parts) == 1 and parts[0].lower() not in ("bearer",):
            return parts[0].strip()
    return (headers.get("X-Fleet-Token") or "").strip()


def check_access(ip, headers):
    """返回 None 表示放行；否则返回 (http_code, 给用户看的说明)。"""
    if not _ip_allowed(ip):
        _note_auth_fail(ip)
        return 403, "来源 IP %s 不在 allow_ips 白名单内" % ip
    tok = CFG.get("auth_token") or ""
    if not tok:
        return None
    got = _token_of(headers)
    # 常数时间比较，避免按字符猜测。
    # 必须比 bytes：HTTP 头按 latin-1 解码，请求头里塞非 ASCII 字节会让
    # compare_digest(str, str) 抛 TypeError —— 一条可被外部触发的未捕获异常路径。
    if got and hmac.compare_digest(got.encode("utf-8", "surrogateescape"),
                                   tok.encode("utf-8", "surrogateescape")):
        AUTH_FAILS.pop(ip, None)
        return None
    _note_auth_fail(ip)
    if not got:
        return 401, "缺少凭据。请在请求头带 Authorization: Bearer <token>"
    return 401, "token 不正确"


# ---------------------------------------------------------------------------
# v5.2: 公钥查表 + 请求签名校验
# ---------------------------------------------------------------------------
def _peer_public_key(node_id):
    """按 node_id / fingerprint 在 peers.json 中查已配对公钥；查不到返回 ""。"""
    if not node_id:
        return ""
    try:
        nodes = load_peers().get("nodes", {})
    except Exception:
        return ""
    for _name, info in nodes.items():
        if not isinstance(info, dict):
            continue
        if info.get("node_id") == node_id:
            return info.get("public_key") or info.get("pubkey") or ""
        if (info.get("fingerprint") or "") == node_id:
            return info.get("public_key") or info.get("pubkey") or ""
    return ""


def _request_path(raw_path):
    """签名基串里的路径规范：去掉 query、末尾斜杠；根路径为 "/"。"""
    return (raw_path or "/").split("?")[0].rstrip("/") or "/"


def _sign_outbound_headers(method, path, body):
    """给跨节点请求附加 v5.2 签名头；没有身份时返回空 dict（降级 token）。"""
    if not (_HAS_CORE and IDENTITY):
        return {}
    try:
        return _core.sign_request(IDENTITY, method, _request_path(path), body or b"")
    except Exception:
        return {}



def _check_path(p):
    """安全边界：只允许 allowed_roots 下的路径。

    用 realpath 而非 abspath —— abspath 不解析软链，在允许目录里放一个
    指向 /etc/shadow 的软链即可逃逸（cat/sed 会跟随软链），等于没拦。
    另请注意：本边界只作用于 read/write/edit/list_dir，**不约束 exec**，
    默认 allowed_roots=["/"] 时它提供零实际保护。
    """
    roots = CFG.get("allowed_roots") or ["/"]
    ap = os.path.realpath(p)
    for r in roots:
        rr = "/" if str(r) == "/" else os.path.realpath(r)
        # 用 os.sep 拼后缀：硬写 "/" 在 Windows 上拼不出 "\"，前缀判断会永远失败
        # （Linux 下 os.sep 就是 "/"，与原行为完全等价）
        if rr == "/" or ap == rr or ap.startswith(rr.rstrip("/\\") + os.sep):
            return ap
    raise ValueError("Path not in allowed roots / 路径不在允许范围内: %s" % ap)


def _which(cmd):
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        f = os.path.join(d, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------
def _is_windows():
    return os.name == "nt"


def _decode_output(b):
    """解码子进程字节输出：先试 UTF-8，失败再试系统编码。

    中文 Windows 上 cmd 默认输出 GBK，直接 decode("utf-8", "ignore")
    会把中文吞成乱码。这里按 utf-8 -> 系统编码 -> utf-8(ignore) 顺序试。
    """
    if isinstance(b, str):
        return b
    b = bytes(b or b"")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        enc = locale.getpreferredencoding(False) or "gbk"
        return b.decode(enc)
    except Exception:
        return b.decode("utf-8", "ignore")


RUN_BUF_MAX = 1048576      # 单次同步 exec 输出上限 1MB（超出截断并提示）


def _wrap(cmd, sudo=False):
    """把命令包成最终交给 bash 执行的字符串。全部用字符串拼接，避免 % 吃掉花括号。

    SEC-05：密码**绝不**出现在命令行字符串里。命令行对同机所有用户可见
    （`ps aux` 一行就看完），把密码 echo 进去等于把 sudo 密码写在公告板上。
    这里只生成 `sudo -S bash -c <cmd>`，密码由调用方经 stdin 第一行喂进去
    （见 _sudo_stdin_prefix）——`sudo -S` 只读第一行当密码，剩下的 stdin
    仍然流向命令本身，所以带 stdin 的命令不受影响。
    """
    flag = "-lc" if CFG.get("login_shell", True) else "-c"
    if not sudo or _is_root():
        return "bash " + flag + " " + shlex.quote(cmd)
    if CFG.get("sudo_password"):
        return "sudo -S bash " + flag + " " + shlex.quote(cmd)
    # 没配密码：尝试免密 sudo（-n），失败会明确报错，不会卡住
    return "sudo -n bash " + flag + " " + shlex.quote(cmd)


def _sudo_stdin_prefix():
    """SEC-05：`sudo -S` 用的 stdin 前缀（密码行）。

    调用方把它拼在原 stdin 之前。`sudo -S` 只消费第一行当密码，余下字节
    原样转给子命令，所以 `cat > f` 这类吃 stdin 的命令语义不变。
    """
    pw = CFG.get("sudo_password") or ""
    if not pw:
        return b""
    return (pw + "\n").encode("utf-8")


def _kill_tree(p):
    """杀掉子进程及其整个进程组（POSIX）；Windows 退化为 kill。"""
    if p is None:
        return
    try:
        if not _is_windows():
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_local(command, sudo=False, timeout=None, work_dir=None, stdin=None):
    """在设备本机执行命令，返回 (exit_code, stdout, stderr)。"""
    t = timeout or CFG.get("command_timeout", 120)
    wd = work_dir or CFG.get("work_dir") or "/"
    if _is_windows():
        # Windows 没有 bash（System32 下的 bash.exe 是 WSL 桩，调它只会吐 wsl 帮助），
        # 直接走 cmd /c；sudo 在 Windows 下无意义，忽略。
        argv = ["cmd", "/c", command]
    else:
        argv = ["bash", "-c", _wrap(command, sudo=sudo)]
    p = None
    try:
        # SEC-05：sudo 密码经 stdin 第一行喂给 `sudo -S`，不进命令行字符串。
        # 没有密码时 stdin 保持原样（None → DEVNULL，不阻塞读 stdin 的命令）。
        stdin_bytes = stdin.encode("utf-8") if stdin is not None else b""
        if sudo and not _is_windows():
            stdin_bytes = _sudo_stdin_prefix() + stdin_bytes
        # POSIX 下放进独立进程组：超时时整组杀掉。原来只杀 bash，
        # 它拉起的子孙进程（如 `ping`、`tail -f`、后台服务）会成孤儿继续跑，
        # 还可能攥着管道让 communicate 迟迟不返回，请求线程被拖住。
        p = subprocess.Popen(
            argv,
            cwd=wd if os.path.isdir(wd) else None,
            stdin=subprocess.PIPE if stdin_bytes else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=not _is_windows(),
        )
        out, err = p.communicate(
            input=stdin_bytes or None,
            timeout=t)
        code, so, se = p.returncode, _decode_output(out), _decode_output(err)
        # 单次同步 exec 输出上限：一个 `cat /dev/urandom` 或 `yes` 就能让
        # agent 吃满内存、把整台设备拖死。截断而不是丢弃，并明确告诉调用方。
        if len(so) > RUN_BUF_MAX:
            so = so[:RUN_BUF_MAX]
            se += "\n[输出超 1MB 已截断；大输出请用 exec_async + exec_poll 分页取]"
        return (code, so, se)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        return (-1, _decode_output(out or b""),
                (_decode_output(err or b"") + "\n[超时 %ss 被终止]" % t).lstrip())
    except FileNotFoundError:
        return -1, "", "[执行失败] 找不到 shell（Windows 需 cmd，POSIX 需 bash）"
    except Exception as e:
        return -1, "", "[执行异常] %r" % (e,)


def fmt_result(code, out, err):
    txt = "[exit %s]\n%s" % (code, out)
    if err.strip():
        txt += "\n--- stderr ---\n" + err
    if code != 0 and "sudo" in err and "password" in err.lower():
        txt += "\n[提示] sudo 需要密码，请在 mcp_agent_config.json 配置 sudo_password"
    return _truncate(txt)


# ---------------------------------------------------------------------------
# 异步命令（v3.2）：exec_async 启动后台任务，exec_poll 取增量输出，
# exec_result 等待完成，exec_kill 终止。主端不用吊死在长任务上。
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_BUF_MAX = 1048576      # 单任务输出上限 1MB，超了丢弃最老内容
JOB_TTL = 3600             # 已完成任务保留 1 小时


def _job_sweep():
    now = time.time()
    with JOBS_LOCK:
        for jid in [k for k, j in JOBS.items()
                    if j.get("end") and now - j["end"] > JOB_TTL]:
            JOBS.pop(jid, None)


def _job_reader(job):
    """后台线程：持续读子进程输出，结束时记录 exit code。

    用非阻塞读 + 短轮询，保证输出增量可见（read(65536) 在管道上会
    一直阻塞到攒够 64KB 或 EOF，不适合做增量）。
    """
    import os as _os
    fd = job["proc"].stdout.fileno()
    try:
        _os.set_blocking(fd, False)
    except Exception:
        pass
    try:
        while True:
            try:
                chunk = job["proc"].stdout.read(65536)
            except (BlockingIOError, InterruptedError):
                chunk = None
            except Exception:
                chunk = b""
            if chunk:
                with job["lock"]:
                    job["buf"] += chunk
                    if len(job["buf"]) > JOB_BUF_MAX:
                        job["buf"] = job["buf"][-JOB_BUF_MAX:]
                        job["dropped"] = True
                continue
            if chunk == b"" or job["proc"].poll() is not None:
                # EOF 或进程已结束：把剩余的一次读完
                try:
                    rest = job["proc"].stdout.read()
                    if rest:
                        with job["lock"]:
                            job["buf"] += rest
                except Exception:
                    pass
                break
            time.sleep(0.05)
    except Exception:
        pass
    finally:
        try:
            job["proc"].wait()
        except Exception:
            pass
        with job["lock"]:
            job["exit"] = job["proc"].returncode
            job["end"] = time.time()


def t_exec_async(a):
    _job_sweep()
    cmd = a.get("command", "")
    wd = a.get("workDir") or CFG.get("work_dir") or "/"
    if _is_windows():
        argv = ["cmd", "/c", cmd]
    else:
        argv = ["bash", "-c", _wrap(cmd, sudo=bool(a.get("sudo")))]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=wd if os.path.isdir(wd) else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=not _is_windows())
    except FileNotFoundError:
        return "错误: 找不到 shell（Windows 需 cmd，POSIX 需 bash）", True
    except Exception as e:
        return "错误: 启动失败 %r" % (e,), True
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "proc": proc, "buf": bytearray(), "lock": threading.Lock(),
           "start": time.time(), "end": None, "exit": None, "dropped": False,
           "cmd": cmd[:120]}
    with JOBS_LOCK:
        JOBS[jid] = job
    th = threading.Thread(target=_job_reader, args=(job,), daemon=True)
    th.start()
    return ("job_id=%s\n用 exec_poll %s 取输出，exec_result %s 等待完成，exec_kill %s 终止。"
            % (jid, jid, jid, jid)), False


def _job_get(jid):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if not job:
        return None
    return job


def t_exec_poll(a):
    job = _job_get(a.get("job_id", ""))
    if not job:
        return "未知 job_id（可能已过期清理）", True
    try:
        offset = max(0, int(a.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    with job["lock"]:
        data = bytes(job["buf"])
        running = job["exit"] is None
        exit_code = job["exit"]
        dropped = job["dropped"]
    text = _decode_output(data[offset:])
    head = "[job %s] %s" % (job["id"], "运行中" if running else "已结束 exit=%s" % exit_code)
    if dropped and offset == 0:
        head += "（早期输出因超 1MB 被丢弃）"
    return "%s\n--- offset %d -> %d ---\n%s" % (head, offset, len(data), text), False


def t_exec_result(a):
    job = _job_get(a.get("job_id", ""))
    if not job:
        return "未知 job_id（可能已过期清理）", True
    try:
        timeout = float(a.get("timeoutMs", 120000)) / 1000.0
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        job["proc"].wait(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired:
        pass
    # 复用 poll 逻辑返回全量输出
    return t_exec_poll({"job_id": job["id"], "offset": 0})


def t_exec_kill(a):
    job = _job_get(a.get("job_id", ""))
    if not job:
        return "未知 job_id（可能已过期清理）", True
    try:
        p = job["proc"]
        try:
            if not _is_windows():
                os.killpg(p.pid, signal.SIGTERM)   # 连同子孙进程一起
            else:
                p.terminate()
        except Exception:
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
        return "[job %s] 已终止" % job["id"], False
    except Exception as e:
        return "终止失败: %r" % (e,), True


def t_exec_jobs(a):
    _job_sweep()
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    if not jobs:
        return "当前无后台任务", False
    lines = []
    for j in sorted(jobs, key=lambda x: x["start"]):
        with j["lock"]:
            st = "运行中" if j["exit"] is None else "已结束 exit=%s" % j["exit"]
            sz = len(j["buf"])
        lines.append("%s  %s  %d字节  %s" % (j["id"], st, sz, j["cmd"]))
    return "\n".join(lines), False


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def t_exec(a):
    to = a.get("timeoutMs")
    code, out, err = run_local(
        a.get("command", ""),
        sudo=bool(a.get("sudo")),
        timeout=(to / 1000.0) if to else None,
        work_dir=a.get("workDir"),
    )
    return fmt_result(code, out, err), code != 0


def t_read(a):
    p = _check_path(a["filePath"])
    start, end = a.get("startLine"), a.get("endLine")
    if _is_windows():
        # Windows 无 sed/cat，直接用 Python 读（行号与 sed 一致：1 起始）
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if start and end:
                sel = lines[start - 1:end]
            elif start:
                sel = lines[start - 1:]
            else:
                sel = lines
            return "[exit 0]\n" + "".join(sel), False
        except Exception as e:
            return "[exit 1]\n[读取失败] %r" % (e,), True
    if start and end:
        cmd = "sed -n '%d,%dp' %s" % (start, end, shlex.quote(p))
    elif start:
        cmd = "sed -n '%d,$p' %s" % (start, shlex.quote(p))
    else:
        cmd = "cat " + shlex.quote(p)
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0


def t_write(a):
    p = _check_path(a["filePath"])
    if _is_windows():
        # Windows 无 cat 重定向，直接用 Python 写；sudo 忽略
        try:
            mode = "a" if a.get("mode", "overwrite") == "append" else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(a.get("content", ""))
            return "[exit 0] Write OK / 写入成功: " + p, False
        except Exception as e:
            return "[exit 1]\n[写入失败] %r" % (e,), True
    op = ">>" if a.get("mode", "overwrite") == "append" else ">"
    cmd = "cat " + op + " " + shlex.quote(p)
    code, out, err = run_local(cmd, sudo=bool(a.get("sudo")), stdin=a.get("content", ""))
    if code == 0:
        return "[exit 0] Write OK / 写入成功: " + p, False
    return fmt_result(code, out, err), True


def t_edit(a):
    p = _check_path(a["filePath"])
    if _is_windows():
        # Windows 不支持 heredoc，直接用 Python 做替换
        try:
            s = open(p, encoding="utf-8").read()
        except Exception as e:
            return "[exit 2]\nREAD_FAIL %r" % (e,), True
        old, new = a.get("oldText", ""), a.get("newText", "")
        if old not in s:
            return "[exit 3]\nNOT_FOUND", True
        if a.get("dryRun"):
            preview = s.replace(old, new, 1)
            return "[exit 0] dryRun preview / 预览修改（未实际写入）:\n" + preview, False
        open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
        return "[exit 0]\nOK", False
    b_old = base64.b64encode(a.get("oldText", "").encode()).decode()
    b_new = base64.b64encode(a.get("newText", "").encode()).decode()
    _dry_run = bool(a.get("dryRun"))
    script = (
        "python3 - <<'__MCP_EDIT_EOF__'\n"
        "import base64, sys\n"
        "p = " + repr(p) + "\n"
        "old = base64.b64decode('" + b_old + "').decode('utf-8')\n"
        "new = base64.b64decode('" + b_new + "').decode('utf-8')\n"
        "try:\n"
        "    s = open(p, encoding='utf-8').read()\n"
        "except Exception as e:\n"
        "    print('READ_FAIL', e); sys.exit(2)\n"
        "if old not in s:\n"
        "    print('NOT_FOUND'); sys.exit(3)\n"
        "if " + repr(_dry_run) + ":\n"
        "    print('DRYRUN_PREVIEW:'); print(s.replace(old, new, 1)); sys.exit(0)\n"
        "open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))\n"
        "print('OK')\n"
        "__MCP_EDIT_EOF__"
    )
    code, out, err = run_local(script, sudo=bool(a.get("sudo")))
    return fmt_result(code, out, err), code != 0


def t_list_dir(a):
    p = _check_path(a.get("filePath") or a.get("path", "/"))
    if _is_windows():
        # Windows 无 ls，直接用 os.scandir
        try:
            rows = []
            for e in os.scandir(p):
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = -1
                rows.append("%s %12d %s" % ("d" if e.is_dir(follow_symlinks=False) else "-", sz, e.name))
            return "[exit 0]\n" + "\n".join(sorted(rows)), False
        except Exception as e:
            return "[exit 1]\n[列目录失败] %r" % (e,), True
    code, out, err = run_local("ls -la --color=never " + shlex.quote(p))
    return fmt_result(code, out, err), code != 0


def _need_docker():
    if not CFG.get("enable_docker", True):
        return "本设备配置中已禁用 Docker 工具"
    if not _which("docker"):
        return "本设备未安装 docker（或不在 PATH 中）"
    return None


def t_docker_ps(a):
    miss = _need_docker()
    if miss:
        return miss, True
    allflag = "-a" if a.get("all") else ""
    cmd = "docker ps " + allflag + " --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}'"
    code, out, err = run_local(cmd, sudo=True)
    return fmt_result(code, out, err), code != 0


def t_docker_logs(a):
    miss = _need_docker()
    if miss:
        return miss, True
    n = int(a.get("lines", 100))
    code, out, err = run_local(
        "docker logs --tail " + str(n) + " " + shlex.quote(a["name"]) + " 2>&1", sudo=True)
    return fmt_result(code, out, err), code != 0


def t_docker_restart(a):
    miss = _need_docker()
    if miss:
        return miss, True
    code, out, err = run_local("docker restart " + shlex.quote(a["name"]), sudo=True)
    return fmt_result(code, out, err), code != 0


def t_systemctl(a):
    action = a.get("action", "status")
    unit = a.get("unit", "")
    if action not in ("status", "restart", "start", "stop", "is-active", "is-enabled", "enable", "disable"):
        return "不支持的 action: " + action, True
    if not re.match(r"^[\w.@-]+$", unit):
        return "非法 unit 名称", True
    if not _which("systemctl"):
        return "本设备无 systemd（找不到 systemctl）", True
    sudo = action not in ("status", "is-active", "is-enabled")
    code, out, err = run_local("systemctl " + action + " " + unit + " --no-pager", sudo=sudo)
    return fmt_result(code, out, err), code != 0


def t_service_logs(a):
    unit = a.get("unit", "")
    if not re.match(r"^[\w.@-]+$", unit):
        return "非法 unit 名称", True
    n = int(a.get("lines", 100))
    code, out, err = run_local(
        "journalctl -u " + unit + " -n " + str(n) + " --no-pager 2>&1", sudo=True)
    return fmt_result(code, out, err), code != 0


def t_sysinfo(a):
    cmd = (
        "echo '=== 主机 ==='; hostname; uname -srm; uptime; "
        "echo; echo '=== 内存 ==='; free -h; "
        "echo; echo '=== 磁盘 ==='; df -h | grep -vE 'tmpfs|overlay|udev'; "
        "echo; echo '=== CPU 前5 ==='; ps -eo pcpu,pmem,comm --sort=-pcpu | head -6"
    )
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0


def t_http_get(a):
    url = a["url"]
    if not (url.startswith("http://") or url.startswith("https://")):
        return "url 必须以 http(s):// 开头", True
    t = int(a.get("timeout", 10))
    cmd = "curl -sS -m %d -w '\\n[HTTP %%{http_code}]' " % t + shlex.quote(url)
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0




# ---------------------------------------------------------------------------
# P2 增强工具
# ---------------------------------------------------------------------------
def t_search(a):
    """P2-1: 文件内容/文件名搜索，结构化输出。"""
    path = _check_path(a.get("filePath") or a.get("path", "/"))
    pattern = a["pattern"]
    search_type = a.get("type", "content")  # content or filename
    max_results = int(a.get("maxResults", 50))
    if search_type == "filename":
        cmd = "find %s -name %s -type f 2>/dev/null | head -%d" % (
            shlex.quote(path), shlex.quote("*" + pattern + "*"), max_results)
    else:
        cmd = "grep -rn --include='*' %s %s 2>/dev/null | head -%d" % (
            shlex.quote(pattern), shlex.quote(path), max_results)
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0


def t_upload(a):
    """P2-2: base64 内容写入文件（用于二进制文件传输）。"""
    p = _check_path(a["filePath"])
    b64data = a["content"]  # base64 编码的内容
    try:
        raw = base64.b64decode(b64data)
    except Exception as e:
        return "[exit 1] base64 decode failed: %r" % (e,), True
    if _is_windows():
        try:
            with open(p, "wb") as f:
                f.write(raw)
            return "[exit 0] Upload OK / 上传成功: %s (%d bytes)" % (p, len(raw)), False
        except Exception as e:
            return "[exit 1] Write failed: %r" % (e,), True
    # Linux: 用 base64 -d 写入
    cmd = "base64 -d > " + shlex.quote(p)
    code, out, err = run_local(cmd, sudo=bool(a.get("sudo")), stdin=b64data)
    if code == 0:
        return "[exit 0] Upload OK / 上传成功: %s (%d bytes)" % (p, len(raw)), False
    return fmt_result(code, out, err), True


def t_download(a):
    """P2-2: 读取文件返回 base64 内容（用于二进制文件传输）。"""
    p = _check_path(a["filePath"])
    if _is_windows():
        try:
            with open(p, "rb") as f:
                raw = f.read()
            return base64.b64encode(raw).decode(), False
        except Exception as e:
            return "[exit 1] Read failed: %r" % (e,), True
    cmd = "base64 " + shlex.quote(p)
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0


def t_ps(a):
    """P2-5: 进程列表，结构化输出。"""
    all_procs = bool(a.get("all", False))
    cmd = "ps -eo pid,ppid,user,pcpu,pmem,comm,args --sort=-pcpu"
    if not all_procs:
        cmd += " | head -20"
    code, out, err = run_local(cmd)
    return fmt_result(code, out, err), code != 0


def t_kill(a):
    """P2-5: 终止进程。"""
    pid = str(a["pid"])
    signal_name = a.get("signal", "TERM")
    # 只允许数字 PID，防止注入
    if not pid.isdigit():
        return "[exit 1] Invalid PID / 无效的 PID: %s" % pid, True
    valid_signals = {"TERM", "KILL", "HUP", "INT", "STOP", "CONT", "USR1", "USR2"}
    if signal_name not in valid_signals:
        return "[exit 1] Invalid signal / 无效信号: %s" % signal_name, True
    cmd = "kill -%s %s" % (signal_name, pid)
    code, out, err = run_local(cmd, sudo=bool(a.get("sudo")))
    return fmt_result(code, out, err), code != 0


def t_net_check(a):
    """P2-5: 网络诊断工具 - 端口探测/DNS 解析。"""
    action = a.get("action", "port")
    if action == "port":
        host = a["host"]
        port = int(a["port"])
        timeout = float(a.get("timeout", 5))
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return "[exit 0] Port %s:%d is OPEN / 端口开放" % (host, port), False
            else:
                return "[exit 1] Port %s:%d is CLOSED (errno=%d) / 端口关闭" % (host, port, result), True
        except Exception as e:
            return "[exit 1] Connection error: %r" % (e,), True
    elif action == "dns":
        domain = a["host"]
        try:
            addrs = socket.getaddrinfo(domain, None)
            unique = sorted(set(addr[4][0] for addr in addrs))
            return "[exit 0] DNS %s -> %s" % (domain, ", ".join(unique)), False
        except Exception as e:
            return "[exit 1] DNS resolve failed: %r" % (e,), True
    elif action == "listen":
        # 列出本机监听端口
        cmd = "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"
        code, out, err = run_local(cmd)
        return fmt_result(code, out, err), code != 0
    return "[exit 1] Unknown action: %s" % action, True


# ---------------------------------------------------------------------------
# 对等互控（v3.5）：节点之间不分主副，任何一台都能经 MCP 调用另一台
# ---------------------------------------------------------------------------
# 对等节点表与本机 fleet 命令共用同一个文件：<安装目录>/peers.json（0600）
PEERS_PATH = os.path.join(HERE, "peers.json")
PEERS_LOCK = threading.Lock()

# v4.1: 自研通讯协议层初始化
_mail_engine = None
_discovery_layer = None
_delivery_engine = None
_sync_engine = None

def _note_peer_agents(peer_name, agents):
    """v5.0.1: 记录对端节点的 agent 归属（gossip 上报），供投递匹配。

    对端 sync 时上报它的 local_agents；本节点把它们记进 peers 条目，
    deliver_now 遇到 to=远程 agent 时据此找到宿主节点直投。
    只在有变化时写盘，避免 60s 一次的 gossip 产生无谓 IO。
    """
    if not peer_name or not isinstance(agents, list):
        return
    agents = sorted(set(str(a) for a in agents))[:64]   # 上限防御
    with PEERS_LOCK:
        d = load_peers()
        entry = d["nodes"].get(peer_name)
        if not entry:
            return
        if entry.get("agents") != agents:
            entry["agents"] = agents
            entry["updated"] = int(time.time())
            save_peers(d)



def _mailbox_secret():
    """邮箱落盘密钥：使用 DATA_DIR/mailbox.key（0600），与 auth_token 解耦。

    v5.2.0 起不再用 auth_token 派生邮箱密钥：token 轮换/重新生成不会让旧邮件
    变成不可读密文。旧版本（enc1$/token 派生）的数据由 MailEngine 的
    fallback_secret 兼容读取，并在下次写回时自动迁移到 mailbox.key。
    """
    path = os.path.join(DATA_DIR, "mailbox.key")
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except (OSError, IOError):
        pass
    key = uuid.uuid4().hex + uuid.uuid4().hex
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return key


def _init_proto():
    """初始化自研通讯协议层（在 main() 中配置加载后调用）。"""
    global _mail_engine, _delivery_engine, _sync_engine
    if not _HAS_PROTO:
        return
    # v5.2.0: 邮箱固定使用 mailbox.key；auth_token 只作为旧数据回退密钥，
    # 避免 token 轮换把历史邮件变成不可读密文。
    _proto_secret = _mailbox_secret()
    _mail_engine = MailEngine(HERE, SERVER_NAME, secret=_proto_secret,
                              fallback_secret=(CFG.get("auth_token") or None))

    def _get_peers():
        return load_peers().get("nodes", {})

    def _verify_peer_token(name):
        """v5.0: 供 UDP hello 验签——返回已配对 peer 的 token。"""
        try:
            with PEERS_LOCK:
                return load_peers().get("nodes", {}).get(name, {}).get("token", "") or None
        except Exception:
            return None

    # v5.0.2: 投递与同步共享同一熔断器——一个 peer 的健康状态在两条链路一致
    _peer_health = PeerHealth()
    _delivery_engine = MessageDelivery(_mail_engine, _get_peers, self_url(),
                                       health=_peer_health)
    _sync_engine = SyncEngine(_mail_engine, _get_peers, self_url(),
                              on_peer_agents=_note_peer_agents,
                              health=_peer_health)


def _on_discover(name, ip, port, caps):
    """UDP 发现回调：自动 peer_add。"""
    url = "http://%s:%d/mcp" % (ip, port)
    try:
        with PEERS_LOCK:
            d = load_peers()
            if name not in d["nodes"]:
                d["nodes"][name] = {"url": url, "token": "",
                                    "added_by": "udp-discover",
                                    "updated": int(time.time())}
                save_peers(d)
                if _mail_engine:
                    _mail_engine.register_agent(name)
    except Exception:
        pass


def _dashboard_data():
    """v5.2: Web Dashboard 数据聚合（/fleet/ui/data）。

    只读、轻量：不跑 shell，纯标准库取数；消息文本已由 MailEngine 自动解密。
    新增：身份/安全状态、投递队列、Agent 任务时间线、对端指纹（设备管理）。
    """
    now = time.time()
    known = {}
    if _discovery_layer:
        try:
            known = _discovery_layer.known_nodes() or {}
        except Exception:
            pass
    with PEERS_LOCK:
        peers = load_peers().get("nodes", {})
    peer_list = []
    for name in sorted(peers):
        info = peers[name] or {}
        last_seen = known.get(name)
        peer_list.append({
            "name": name,
            "url": info.get("url", ""),
            "token": bool(info.get("token", "")),
            "added_by": info.get("added_by", ""),
            "online": bool(last_seen and (now - last_seen) < 90),
            "last_seen_ago": int(now - last_seen) if last_seen else None,
            "fingerprint": info.get("fingerprint", ""),
            "node_id": info.get("node_id", ""),
            "has_pubkey": bool(info.get("public_key")),
            "cooldown": (_delivery_engine.health.cooldown_remaining(info.get("url", ""))
                         if _delivery_engine and info.get("url") else 0),
        })
    stats = _mail_engine.stats() if _mail_engine else {}
    recent = []
    if _mail_engine:
        try:
            msgs = _mail_engine._load_jsonl(_mail_engine.inbox_path)[-10:]
            for m in reversed(msgs):
                recent.append({
                    "from": m.get("from", ""), "to": m.get("to", ""),
                    "text": (m.get("text", "") or "")[:120],
                    "ts": m.get("ts", ""), "topic": m.get("topic", ""),
                })
        except Exception:
            pass
    tasks = []
    if TASK_STORE is not None:
        try:
            for t in TASK_STORE.list_tasks()[-12:]:
                tasks.append({
                    "task_id": t.get("task_id", ""),
                    "goal": (t.get("goal") or t.get("title") or "")[:100],
                    "status": t.get("status", ""),
                    "steps": (t.get("event_count")
                              or len(t.get("plan") or [])
                              or len(t.get("steps") or [])),
                    "updated": t.get("updated_ms") or t.get("created_ms"),
                })
        except Exception:
            pass
    sys_lite = {"platform": sys.platform,
                "python": sys.version.split()[0]}
    try:
        sys_lite["load"] = "%.2f" % (os.getloadavg()[0],)
    except Exception:
        sys_lite["load"] = "-"
    try:
        import shutil as _sh
        _du = _sh.disk_usage(HERE)
        sys_lite["disk_free_gb"] = round(_du.free / 1073741824.0, 1)
    except Exception:
        pass
    audit_ok = None
    try:
        if AUDIT_CHAIN is not None:
            audit_ok = AUDIT_CHAIN.verify()[0]
    except Exception:
        audit_ok = None
    return {
        "node": SERVER_NAME, "version": SERVER_VERSION,
        "tools": len(TOOLS),
        "encryption": bool(CFG.get("auth_token") or ""),
        "identity": {
            "node_id": IDENTITY.node_id if IDENTITY else "",
            "fingerprint": IDENTITY.fingerprint if IDENTITY else "",
        },
        "security": {
            "audit_ok": audit_ok,
            "require_signature": bool(CFG.get("require_signature")),
            "read_only": bool(CFG.get("read_only")),
            "tls": bool(CFG.get("tls_cert")),
            "agents": bool(_HAS_AGENT),
        },
        "delivery": {
            "pending": stats.get("outbox_pending", 0),
            "dead": stats.get("outbox_dead", 0),
        },
        "peers": peer_list,
        "tasks": tasks,
        "mailbox": stats,
        "recent": recent,
        "sys": sys_lite,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


_REQ = threading.local()          # 当前请求带进来的跳数（X-Fleet-Hops）
try:
    LISTEN_PORT = int(open(os.path.join(HERE, "port.txt")).read().strip())
except Exception:
    LISTEN_PORT = None                # main() 里会按实际监听端口覆盖


def load_peers():
    try:
        with open(PEERS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("nodes"), dict):
            return d
    except Exception:
        pass
    return {"nodes": {}}


def save_peers(d):
    tmp = PEERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)          # 里面有别的节点的令牌
    except Exception:
        pass
    os.replace(tmp, PEERS_PATH)


# ---------------------------------------------------------------------------
# 邀请码：文件位置 / 原子写 / 并发消费
# ---------------------------------------------------------------------------
INVITE_LOCK = threading.Lock()     # 同一邀请码只能被消费一次


def _invite_file_path():
    """invites.json 与配置同目录（动态算，测试会改 CONFIG_PATH）。"""
    return os.path.join(os.path.dirname(CONFIG_PATH), "invites.json")


def _load_invites(path=None):
    try:
        with open(path or _invite_file_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_invites_atomic(invites, path=None):
    """临时文件 + fsync + os.replace：写一半断电不会留下空 invites 文件。

    原来直接 json.dump 覆盖，进程被 Termux 杀掉时会把「已消费」标记丢掉，
    同一个邀请码就能被用两次。
    """
    p = path or _invite_file_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(invites, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, p)


def _verify_invite(payload):
    """消费一个邀请码并完成双向配对。返回 (http_code, body_dict)。

    整段在 INVITE_LOCK 内完成「校验 → 标记已用 → 落盘」，
    并发请求只有一个能拿到 200；标记写失败返回 500（而不是假装成功），
    否则对端以为配好了、本端其实没记下来。
    """
    invite_id = (payload or {}).get("invite_id", "")
    peer_name = (payload or {}).get("name", "")
    peer_url = (payload or {}).get("url", "")
    peer_token = (payload or {}).get("token", "")
    peer_pub = (payload or {}).get("public_key", "")
    peer_nid = (payload or {}).get("node_id", "")
    peer_fp = (payload or {}).get("fingerprint", "")
    if not invite_id or not peer_name or not peer_url:
        return 400, {"error": "need invite_id, name, url"}
    with INVITE_LOCK:
        invites = _load_invites()
        inv = invites.get(invite_id)
        if not inv:
            return 403, {"error": "邀请码不存在"}
        if inv.get("used"):
            return 403, {"error": "邀请码已使用"}
        if int(time.time()) > inv.get("expires", 0):
            return 403, {"error": "邀请码已过期"}
        with PEERS_LOCK:
            d = load_peers()
            d.setdefault("nodes", {})[peer_name] = {
                "url": peer_url, "token": peer_token, "added_by": "invite",
                "updated": int(time.time()), "public_key": peer_pub,
                "node_id": peer_nid, "fingerprint": peer_fp}
            save_peers(d)
        inv["used"] = True
        inv["used_by"] = peer_name
        inv["used_at"] = int(time.time())
        try:
            _save_invites_atomic(invites)
        except Exception as e:
            # 配对已写入 peers，但「已用」没落盘：明确报错，让对端重试而不是
            # 以为邀请码还能用一次。
            return 500, {"error": "已配对，但标记邀请码失败：%s（可重试）" % e}
    return 200, {
        "ok": True,
        "verified": True,
        "my_name": SERVER_NAME,
        "my_url": self_url(),
        "my_token": CFG.get("auth_token", ""),
        "my_public_key": IDENTITY.public_key_b64 if IDENTITY else "",
        "my_node_id": IDENTITY.node_id if IDENTITY else "",
        "my_fingerprint": IDENTITY.fingerprint if IDENTITY else "",
        "my_version": SERVER_VERSION,
        "message": "确权通过，已与 %s 建立连接" % peer_name,
    }


def _verify_agent(url, timeout=5):
    """判断一个地址后面是不是真的 MCP 副端（配对/发现前的验真）。

    只认「GET / 返回 JSON 且带 server 或 status」——一个静态网页、
    一个报 404 的端口、一个死地址都不算。用于避免把「随便什么 HTTP 服务」
    当成设备收进登记表。
    """
    base = url[:-4] if url.endswith("/mcp") else url.rstrip("/")
    try:
        req = urllib.request.Request(base + "/",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(65536).decode("utf-8", "ignore")
        data = json.loads(raw)
        return isinstance(data, dict) and bool(data.get("server") or data.get("status"))
    except Exception:
        return False


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


_TS_IP_CACHE = {"at": 0.0, "ip": ""}


def _tailscale_ip():
    """本机 tailnet IP（`tailscale ip -4`），失败返回空串。

    60 秒缓存：self_url() 会在 mesh / info / invite 等热路径上被反复调用，
    每次都 fork 一个 tailscale 进程在手机/NAS 上是明显的浪费。
    """
    now = time.time()
    if _TS_IP_CACHE["at"] and now - _TS_IP_CACHE["at"] < 60:
        return _TS_IP_CACHE["ip"]
    ip = ""
    try:
        p = subprocess.Popen(["tailscale", "ip", "-4"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = p.communicate(timeout=3)
        if p.returncode == 0:
            ip = (out or b"").decode("utf-8", "ignore").strip().splitlines()[0].strip()
    except Exception:
        ip = ""
    _TS_IP_CACHE["at"] = now
    _TS_IP_CACHE["ip"] = ip
    return ip


def self_url():
    """别的节点回连本机的地址。

    优先级：配置 self_url > tailnet IP > 当前隧道地址 > 内网 IP。

    tailnet 排在内网 IP 前面，是因为**内网地址对跨网段的对端毫无意义**：
    主端在云端/沙盒时够不着 192.168.x.x，而 tailnet 地址两边都能走。
    节点自报一个「只有同网段能连」的地址，是配对后第一次调用就失败的最常见原因。
    """
    u = (CFG.get("self_url") or "").strip()
    if not u:
        ts = _tailscale_ip()
        if ts:
            u = "http://%s:%s" % (ts, LISTEN_PORT or 3100)
    if not u:
        try:
            u = open(os.path.join(HERE, "current_url.txt"), encoding="utf-8").read().strip()
        except Exception:
            u = ""
    if not u:
        u = "http://%s:%s" % (_lan_ip(), LISTEN_PORT or 3100)
    u = u.rstrip("/")
    return u if u.endswith("/mcp") else u + "/mcp"


def _norm_mcp_url(u):
    u = (u or "").strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    if not re.search(r"/(mcp|sse)$", u):
        u += "/mcp"
    return u


def _strip_suffix(s, suffix):
    """v5.0.8: str.removesuffix 的 3.6 兼容版（3.9 前无此方法，
    老 NAS/软路由常驻 3.6/3.7）。"""
    if suffix and s.endswith(suffix):
        return s[:-len(suffix)]
    return s


def _peer_opener(url):
    """内网/本机地址不走 http_proxy（设备上配了代理时，局域网对等节点会被代理成 404/超时）。"""
    import urllib.request
    import urllib.parse
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host == "localhost" or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _peer_rpc(url, token, payload, sid, hops, timeout):
    import urllib.request
    import urllib.error
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "X-Fleet-Hops": str(hops),
               "X-Fleet-From": SERVER_NAME}
    # v5.2: 对端已配对公钥时附加 Ed25519 签名（token 仍保留，兼容 v5）
    try:
        _spath = urllib.parse.urlsplit(url).path or "/"
        headers.update(_sign_outbound_headers("POST", _spath, body))
    except Exception:
        pass
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        with _peer_opener(url).open(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id") or sid
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        _hint = ""
        if e.code in (401, 403):
            _hint = ("。下一步：检查双方 token/公钥是否匹配（fleet pair 重新配对），"
                     "或确认本机 IP 在对端 allow_ips 内")
        raise ValueError("对端返回 HTTP %s %s%s" % (e.code, e.reason, _hint))
    except Exception as e:
        raise ValueError("连不上 %s：%s。下一步：确认对端在线、端口/防火墙、"
                         "是否在同一网段或 Tailscale；可运行 fleet doctor"
                         % (url, getattr(e, "reason", e)))
    for line in body.splitlines():
        if line.startswith("data:"):
            body = line[5:].strip()
            break
    return (json.loads(body) if body.strip() else {}), sid


def peer_call(url, token, tool, arguments, timeout=130):
    """对另一个节点做一次完整 MCP 调用：initialize → tools/call。返回 (text, is_err)。"""
    hops = int(getattr(_REQ, "hops", 0) or 0) + 1
    if hops > int(CFG.get("peer_max_hops", 3)):
        raise ValueError("转发跳数超过上限 peer_max_hops=%s，疑似互相转发成环，已拒绝"
                         % CFG.get("peer_max_hops", 3))
    r, sid = _peer_rpc(url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "mcp-fleet-peer:" + SERVER_NAME, "version": SERVER_VERSION}}},
        None, hops, 20)
    try:
        _peer_rpc(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  sid, hops, 10)
    except ValueError:
        pass
    r, _ = _peer_rpc(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments or {}}},
                     sid, hops, timeout)
    if "error" in r:
        return "对端错误: %s" % json.dumps(r["error"], ensure_ascii=False), True
    res = r.get("result") or {}
    text = "\n".join(c.get("text", "") for c in res.get("content") or [])
    return text, bool(res.get("isError"))


def _peer_entry(name):
    nodes = load_peers()["nodes"]
    if name in nodes:
        return name, nodes[name]
    if name.startswith(("http://", "https://")):
        return name, {"url": name, "token": ""}
    raise ValueError("没有叫 %r 的对等节点。已知：%s（先用 peer_add 添加，或在本机跑 fleet pair）"
                     % (name, ", ".join(sorted(nodes)) or "无"))


def t_peers(a):
    nodes = load_peers()["nodes"]
    lines = ["本机: %s  回连地址: %s" % (SERVER_NAME, self_url()), ""]
    if not nodes:
        lines.append("（还没有对等节点。用 peer_add 添加，或在本机跑：fleet pair <对方地址> --token <令牌>）")
    for n in sorted(nodes):
        v = nodes[n]
        st = ""
        if a.get("check"):
                try:
                    import urllib.request
                    base = re.sub(r"/(mcp|sse)$", "", v.get("url", ""))
                    req = urllib.request.Request(base + "/health")
                    if v.get("token"):
                        req.add_header("Authorization", "Bearer " + v.get("token", ""))
                    try:
                        with _peer_opener(base).open(req, timeout=5) as r:
                            st = "  在线" if r.status == 200 else "  HTTP %s" % r.status
                    except urllib.error.HTTPError as e:
                        # v5.0.6.1: 401/403 = 在线但需鉴权，不算「不通」
                        if e.code in (401, 403):
                            st = "  在线(需令牌)"
                        else:
                            st = "  HTTP %s" % e.code
                except Exception as e:
                    st = "  不通(%s)" % (getattr(e, "reason", None) or type(e).__name__)
        lines.append("- %s  %s  %s%s" % (n, v.get("url"), "带令牌" if v.get("token") else "无令牌", st))
    return "\n".join(lines), False


def t_peer_call(a):
    name, v = _peer_entry(a["peer"])
    if _norm_mcp_url(v.get("url")) == self_url():
        return "目标就是本机，直接调本机工具即可", True
    # v4.0: 限流
    if not rate_limit_allow():
        return "限流：本节点入站 peer_call 超过每分钟上限 %d，请稍后重试" % CFG.get("rate_limit_per_min", 120), True
    args = a.get("arguments") or {}
    if isinstance(args, str):
        args = json.loads(args or "{}")
    t = int(a.get("timeoutMs") or 130000) / 1000.0
    my_name = CFG.get("name", "node")
    audit_log("peer_call", my_name, name, a["tool"], "calling")
    text, is_err = peer_call(_norm_mcp_url(v["url"]), v.get("token", ""), a["tool"], args, t)
    audit_log("peer_call", my_name, name, a["tool"], "ok" if not is_err else "error")
    return "[%s → %s] %s\n%s" % (SERVER_NAME, name, a["tool"], text), is_err


def t_peer_add(a):
    # v4.0: tailnet 自动信任
    _url = a.get("url", "")
    _is_tailnet = False
    try:
        _h = urllib.parse.urlsplit(_url).hostname or ""
        if _h.endswith(".ts.net") or _h.startswith("100."):
            _is_tailnet = True
    except Exception:
        pass
    if _is_tailnet and CFG.get("tailnet_auto_trust", True) and not a.get("token"):
        a = dict(a)  # shallow copy, don't require token for tailnet peers
    name = (a.get("name") or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", name):
        return "name 只能是字母数字和 _ . -（1~64 位）", True
    url = _norm_mcp_url(a["url"])
    with PEERS_LOCK:
        d = load_peers()
        old = d["nodes"].get(name)
        d["nodes"][name] = {"url": url, "token": (a.get("token") or "").strip(),
                            "added_by": a.get("_from") or "", "updated": int(time.time()),
                            "public_key": (a.get("public_key") or "").strip(),
                            "node_id": (a.get("node_id") or "").strip(),
                            "fingerprint": (a.get("fingerprint") or "").strip()}
        save_peers(d)
    return "%s对等节点 %s → %s" % ("已更新" if old else "已添加", name, url), False


def t_peer_remove(a):
    with PEERS_LOCK:
        d = load_peers()
        if d["nodes"].pop(a["name"], None) is None:
            return "没有叫 %r 的对等节点" % a["name"], True
        save_peers(d)
    return "已移除对等节点 %s" % a["name"], False


def t_peer_info(a):
    """配对用：告诉对方「回连我用哪个地址 + 我的 v5.2 身份公钥」。

    公钥是公开信息；token 不在这里返回（对方本来就持有或走签名）。
    """
    return json.dumps({
        "name": SERVER_NAME, "url": self_url(), "version": SERVER_VERSION,
        "node_id": IDENTITY.node_id if IDENTITY else "",
        "fingerprint": IDENTITY.fingerprint if IDENTITY else "",
        "public_key": IDENTITY.public_key_b64 if IDENTITY else "",
        "peers": sorted(load_peers()["nodes"])}, ensure_ascii=False), False


PEER_TOOLS = [
    ("peers", "列出本节点认识的对等节点（check=true 顺带探活）。节点之间不分主副，可互相调用。",
     {"type": "object", "properties": {"check": {"type": "boolean"}}}),
    ("peer_call", "经本节点调用另一个节点的工具（如 peer=nas, tool=exec, arguments={command:'uptime'}）。可多跳转发进内网。",
     {"type": "object", "properties": {
         "peer": {"type": "string", "description": "对等节点名字（见 peers），或完整 http(s)://…/mcp 地址"},
         "tool": {"type": "string", "description": "对端工具名，如 exec / read / sysinfo / peer_call"},
         "arguments": {"type": "object", "description": "传给对端工具的参数"},
         "timeoutMs": {"type": "number"}}, "required": ["peer", "tool"]}),
    ("peer_add", "添加/更新一个对等节点（名字 + 地址 + 令牌），之后即可用 peer_call 控制它。",
     {"type": "object", "properties": {
         "name": {"type": "string"}, "url": {"type": "string"},
         "token": {"type": "string", "description": "对端的 auth_token，未开鉴权可留空"}},
      "required": ["name", "url"]}),
    ("peer_remove", "移除一个对等节点。",
     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    ("peer_info", "返回本节点名字与回连地址（配对时由对方调用）。",
     {"type": "object", "properties": {}}),
]
PEER_FUNCS = {"peers": t_peers, "peer_call": t_peer_call, "peer_add": t_peer_add,
              "peer_remove": t_peer_remove, "peer_info": t_peer_info}



# ---------------------------------------------------------------------------
# 节点间对话邮箱（msg_send / msg_recv / msg_history）
# AI 之间通过一个都能访问的节点互发消息；也可做 pub/sub 广播（to="*"）
# ---------------------------------------------------------------------------
MAIL_PATH = os.path.join(HERE, "mailbox.jsonl")
MAIL_CUR = os.path.join(HERE, "mailbox.cursor.json")
_MAIL_COND = threading.Condition()
_MAIL = {"msgs": None, "cur": None}
_MAIL_MAX_TEXT = 64000
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")


def _mail_load():
    if _MAIL["msgs"] is None:
        msgs = []
        if os.path.exists(MAIL_PATH):
            with io.open(MAIL_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            msgs.append(json.loads(line))
                        except ValueError:
                            pass
        _MAIL["msgs"] = msgs[-5000:]
        cur = {}
        if os.path.exists(MAIL_CUR):
            try:
                with io.open(MAIL_CUR, encoding="utf-8") as f:
                    cur = json.load(f)
            except ValueError:
                cur = {}
        _MAIL["cur"] = cur


def _mail_name(v, field):
    v = (v or "").strip()
    if field == "to" and v == "*":
        return v                      # 广播：投递给所有已配对节点
    if not _NAME_RE.match(v):
        raise ValueError("%s 只能是 1-32 位字母/数字/_.-（to 可用 * 广播）" % field)
    return v


def t_msg_send(a):
    """v4.1: store-and-forward 消息发送。
    在线 agent -> 直接 HTTP 投递；离线 -> 存 outbox，上线自动收信。"""
    frm = _mail_name(a.get("from"), "from")
    to = _mail_name(a.get("to"), "to")
    text = a.get("text") or ""
    topic = a.get("topic") or ""
    if not text.strip():
        return "text 不能为空", True
    if len(text) > _MAIL_MAX_TEXT:
        return "text 过长（上限 %d 字符）" % _MAIL_MAX_TEXT, True

    msg = {"from": frm, "to": to, "text": text,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if topic:
        msg["topic"] = topic

    # v4.1: 使用自研通讯协议
    if _mail_engine:
        # 本地 agent？直接存 inbox
        if _mail_engine.is_local_agent(to) or to == _mail_engine.node_name:
            _mail_engine.inbox_append(dict(msg))
            return "已投递(本地) -> %s" % to, False

        # 远程 agent：尝试直接投递
        if _delivery_engine:
            ok = _delivery_engine.deliver_now(msg)
            if ok:
                return "已投递(在线) -> %s" % to, False
            # 投递失败：存 outbox，等目标上线后自动投递
            _mail_engine.outbox_append(dict(msg))
            return "已存邮箱(离线) -> %s，对方上线后自动投递" % to, False

    # fallback: 旧逻辑（无 fleet_proto）
    with _MAIL_COND:
        _mail_load()
        mid = (_MAIL["msgs"][-1]["id"] + 1) if _MAIL["msgs"] else 1
        m = {"id": mid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
             "from": frm, "to": to, "text": text}
        if topic:
            m["topic"] = topic
        _MAIL["msgs"].append(m)
        with io.open(MAIL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        _MAIL_COND.notify_all()
    return "已发送 #%d -> %s" % (mid, to), False


def t_msg_recv(a):
    """v4.1: 收消息。读本地 inbox + 向所有 peer poll 拉取离线消息。"""
    me = _mail_name(a.get("me"), "me")
    wait = max(0, min(int(a.get("wait", 0) or 0), 300))
    peek = bool(a.get("peek"))
    topic = a.get("topic")

    # v4.1: 使用自研通讯协议
    if _mail_engine:
        # 先向所有 peer poll 拉取给我的消息（上线收信）
        if _sync_engine:
            try:
                peers = load_peers().get("nodes", {})
                for pname, pinfo in peers.items():
                    url = pinfo.get("url", "")
                    token = pinfo.get("token", "")
                    if url:
                        # v5.0.8: 3.6 兼容 —— removesuffix 是 3.9+，老 NAS/
                        # 软路由常驻 3.6/3.7，README 承诺 3.6+ 必须兑现
                        base = _strip_suffix(
                            _strip_suffix(url.rstrip("/"), "/mcp"), "/sse")
                        polled = _sync_engine._poll_peer(base, token, me)
                        for m in polled:
                            _mail_engine.inbox_append(m)
            except Exception:
                pass

        # 读本地 inbox
        deadline = time.time() + wait
        while True:
            cursor = _mail_engine.inbox_cursor(me)
            got = _mail_engine.inbox_read(me, after_id=cursor, topic=topic,
                                           mark_read=not peek)
            if got or (deadline - time.time()) <= 0:
                break
            time.sleep(min(2, deadline - time.time()))

        if not got:
            return "（没有新消息）", False
        return "\n\n".join("#%s [%s] %s -> %s:\n%s" % (
            m.get("id", "?"), m.get("ts", ""), m.get("from", ""),
            m.get("to", ""), m.get("text", "")) for m in got), False

    # fallback: 旧逻辑
    deadline = time.time() + wait
    with _MAIL_COND:
        _mail_load()
        while True:
            after = a.get("after_id")
            after = int(after) if after is not None else int(_MAIL["cur"].get(me, 0))
            got = [m for m in _MAIL["msgs"]
                   if m["id"] > after and m["to"] in (me, "*") and m["from"] != me]
            if topic:
                got = [m for m in got if m.get("topic") == topic]
            left = deadline - time.time()
            if got or left <= 0:
                break
            _MAIL_COND.wait(min(left, 30))
        if got and not peek and a.get("after_id") is None:
            _MAIL["cur"][me] = got[-1]["id"]
            tmp = MAIL_CUR + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(_MAIL["cur"]))
            os.replace(tmp, MAIL_CUR)
    if not got:
        return "（没有新消息）", False
    return "\n\n".join("#%d [%s] %s -> %s:\n%s" % (m["id"], m["ts"], m["from"], m["to"], m["text"]) for m in got), False


def t_msg_history(a):
    """v4.1: 查看邮箱历史。"""
    n = max(1, min(int(a.get("lines", 20) or 20), 200))
    topic = a.get("topic")

    if _mail_engine:
        stats = _mail_engine.stats()
        inbox_msgs = _mail_engine._load_jsonl(_mail_engine.inbox_path)
        got = inbox_msgs[-n:]
        if topic:
            got = [m for m in got if m.get("topic") == topic]
        if not got:
            return "（邮箱为空）inbox=%d outbox_pending=%d agents=%s" % (
                stats["inbox_total"], stats["outbox_pending"],
                ",".join(stats["local_agents"])), False
        lines = ["#%s [%s] %s -> %s: %s" % (
            m.get("id", "?"), m.get("ts", ""), m.get("from", ""),
            m.get("to", ""), _truncate(m.get("text", ""))[:300]) for m in got]
        lines.append("--- inbox=%d outbox_pending=%d agents=%s" % (
            stats["inbox_total"], stats["outbox_pending"],
            ",".join(stats["local_agents"])))
        return "\n".join(lines), False

    # fallback: 旧逻辑
    with _MAIL_COND:
        _mail_load()
        got = _MAIL["msgs"][-n:]
        if topic:
            got = [m for m in got if m.get("topic") == topic]
    if not got:
        return "（邮箱为空）", False
    return "\n".join("#%d [%s] %s -> %s: %s" % (m["id"], m["ts"], m["from"], m["to"], _truncate(m["text"])[:300]) for m in got), False


# ===========================================================================
# v4.0 进化：Tailscale 自动组网 + Agent 协作 + 审计日志 + 限流 + Mesh 拓扑
# ===========================================================================

# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------
AUDIT_PATH = os.path.join(HERE, "audit.log")
_audit_lock = threading.Lock()


AUDIT_ROTATE_BYTES = 10 * 1024 * 1024   # v5.0.3: audit.log 轮转阈值


def _audit_rotate_if_needed(max_bytes=None):
    """v5.0.3: audit.log 超过阈值时轮转保留一份（audit.log.1，旧的覆盖）。

    append-only 无轮转的日志在常年挂机设备上只增不减；单份备份已足够
    排障用途，避免引入文件序列无限增长。
    """
    if max_bytes is None:
        max_bytes = AUDIT_ROTATE_BYTES
    try:
        if os.path.exists(AUDIT_PATH) and os.path.getsize(AUDIT_PATH) > max_bytes:
            os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
    except OSError:
        pass


def audit_log(action, caller, target, tool, status="ok", detail=""):
    """记录 peer_call / msg_send / delegate 等操作到审计日志。"""
    if not CFG.get("audit_log", True):
        return
    line = "%s | %-6s | %s -> %s | %s | %s | %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), action, caller, target, tool, status, detail[:200])
    try:
        with _audit_lock:
            _audit_rotate_if_needed()   # v5.0.3: 持锁检查，避免并发轮转竞争
            with io.open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, IOError):
        pass


# ---------------------------------------------------------------------------
# 限流：令牌桶（每分钟 N 次）
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_bucket = {"tokens": 0, "last": 0}
# v5.0.3: /fleet/mail 独立令牌桶（与工具层限流分离，见 DEFAULT_CONFIG 注释）
_mail_rate_lock = threading.Lock()
_mail_rate_bucket = {"tokens": 0, "last": 0}


def rate_limit_allow():
    """检查是否允许一次入站 peer_call。令牌桶：每分钟补充 rate_limit_per_min 个。"""
    cap = int(CFG.get("rate_limit_per_min", 120))
    if cap <= 0:
        return True
    now = time.time()
    with _rate_lock:
        elapsed = now - _rate_bucket["last"]
        refill = int(elapsed / 60.0 * cap)
        if refill > 0:
            _rate_bucket["tokens"] = min(cap, _rate_bucket["tokens"] + refill)
            _rate_bucket["last"] = now
        if _rate_bucket["tokens"] > 0:
            _rate_bucket["tokens"] -= 1
            return True
        return False


def rate_limit_mail():
    """v5.0.3: /fleet/mail 入站限流（独立令牌桶，mail_rate_per_min/分钟）。"""
    cap = int(CFG.get("mail_rate_per_min", 600))
    if cap <= 0:
        return True
    now = time.time()
    with _mail_rate_lock:
        elapsed = now - _mail_rate_bucket["last"]
        refill = int(elapsed / 60.0 * cap)
        if refill > 0:
            _mail_rate_bucket["tokens"] = min(cap, _mail_rate_bucket["tokens"] + refill)
            _mail_rate_bucket["last"] = now
        if _mail_rate_bucket["tokens"] > 0:
            _mail_rate_bucket["tokens"] -= 1
            return True
        return False


# ---------------------------------------------------------------------------
# Tailscale 自动组网
# ---------------------------------------------------------------------------
def _tailscale_status():
    """检测 tailscale 是否安装且在线。返回 dict 或 None。"""
    ts = _which("tailscale")
    if not ts:
        return None
    try:
        r = subprocess.run([ts, "status", "--json"], capture_output=True, timeout=10)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _tailscale_ip():
    """返回本机的 tailscale IP（100.64.0.0/10），没有则 None。"""
    st = _tailscale_status()
    if not st:
        return None
    self_key = st.get("Self", {}).get("UserID")
    for peer_id, peer in st.get("Peer", {}).items():
        if peer.get("UserID") == self_key:
            for ip in peer.get("TailscaleIPs", []):
                if ip.startswith("100."):
                    return ip
    # Fallback: check Self directly
    for ip in st.get("Self", {}).get("TailscaleIPs", []):
        if ip.startswith("100."):
            return ip
    return None


def _tailscale_peers_online():
    """列出 tailnet 上在线的其他设备。返回 [(hostname, ip), ...]。"""
    st = _tailscale_status()
    if not st:
        return []
    self_ip = _tailscale_ip()
    result = []
    for peer_id, peer in st.get("Peer", {}).items():
        if not peer.get("Online"):
            continue
        ips = peer.get("TailscaleIPs", [])
        if not ips:
            continue
        ip = ips[0]
        if ip == self_ip:
            continue
        host = peer.get("HostName", "") or peer.get("DNSName", "").rstrip(".")
        result.append((host, ip))
    return result


def _auto_discover_peers(port=3100):
    """扫描 tailnet，发现其他 fleet 节点并自动 peer_add。
    用 TCP connect 探测 port，通的就把对方登记为 peer。"""
    if not CFG.get("tailnet_auto_mesh", True):
        return []
    peers = _tailscale_peers_online()
    if not peers:
        return []
    discovered = []
    my_name = CFG.get("name", "node")
    existing = load_peers()
    for host, ip in peers:
        # 探测对方 fleet 端口
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            sock.close()
            if result != 0:
                continue
        except Exception:
            continue
        # 用 hostname 做 peer 名（去掉重复）
        pname = host.split(".")[0] if host else ip
        if pname == my_name:
            continue
        peer_url = "http://%s:%d/mcp" % (ip, port)
        # 已登记就跳过
        if pname in existing:
            continue
        # 自动信任：同 tailnet 不需要 token（Tailscale 端到端加密）
        existing[pname] = {"url": peer_url, "token": "", "tailnet": True}
        discovered.append((pname, peer_url))
        audit_log("auto_mesh", my_name, pname, "peer_add", "discovered")
    if discovered:
        save_peers(existing)
    return discovered


def _broadcast_join():
    """新节点上线时广播 fleet_join 事件，让已有节点发现自己。"""
    my_name = CFG.get("name", "node")
    my_url = CFG.get("self_url", "") or self_url()
    ts_ip = _tailscale_ip()
    join_msg = json.dumps({"node": my_name, "url": my_url, "tailscale_ip": ts_ip or "",
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    peers = load_peers()
    for pname, pinfo in peers.items():
        try:
            peer_call(pinfo["url"], pinfo.get("token", ""),
                      "msg_send", {"from": my_name, "to": "*", "topic": "fleet-join",
                                   "text": join_msg}, timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 能力声明（capability detection）
# ---------------------------------------------------------------------------
def _detect_capabilities():
    """探测本节点能力，返回 dict。"""
    caps = {"node": CFG.get("name", "node"), "version": AGENT_VERSION}
    # Docker
    caps["docker"] = _which("docker") is not None
    # GPU
    caps["gpu"] = os.path.exists("/dev/nvidia0") or os.path.exists("/dev/dri/renderD128")
    # systemd
    caps["systemd"] = _which("systemctl") is not None and os.path.exists("/run/systemd/system")
    # Tailscale
    ts = _tailscale_status()
    caps["tailscale"] = ts is not None
    caps["tailscale_ip"] = _tailscale_ip() or ""
    # 内网段
    try:
        r = subprocess.run(["ip", "-4", "addr"], capture_output=True, timeout=5)
        if r.returncode == 0:
            import re as _re
            nets = _re.findall(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", r.stdout.decode("utf-8", "replace"))
            caps["networks"] = [n for n in nets if not n.startswith("127.")]
    except Exception:
        caps["networks"] = []
    # CPU 核数
    caps["cpu_count"] = os.cpu_count() or 1
    # 内存
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    caps["mem_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        caps["mem_mb"] = 0
    # 已有 peers 数
    caps["peer_count"] = len(load_peers())
    # 工具列表
    caps["tools"] = [t["name"] for t in TOOLS]
    return caps


_NODE_CAPS = None


def get_caps():
    global _NODE_CAPS
    if _NODE_CAPS is None:
        _NODE_CAPS = _detect_capabilities()
    return _NODE_CAPS


def t_caps(a):
    """返回本节点的能力清单。"""
    return json.dumps(get_caps(), ensure_ascii=False), False


def t_mesh_caps(a):
    """查询 mesh 内所有节点的能力清单（本机 + 所有 peers）。"""
    result = {"self": get_caps(), "peers": {}}
    peers = load_peers().get("nodes", {})
    for pname, pinfo in peers.items():
        try:
            text, is_err = peer_call(pinfo["url"], pinfo.get("token", ""),
                                     "caps", {}, timeout=15)
            if not is_err:
                result["peers"][pname] = json.loads(text)
            else:
                result["peers"][pname] = {"error": text}
        except Exception as e:
            result["peers"][pname] = {"error": str(e), "online": False}
    return json.dumps(result, ensure_ascii=False), False


# ---------------------------------------------------------------------------
# Agent 协作：任务委派
# ---------------------------------------------------------------------------
def t_delegate(a):
    """在 mesh 中广播 who_can <capability>，有能力匹配的节点应答，
    把任务委派给它执行。"""
    cap_query = a.get("capability", "")
    tool = a.get("tool", "exec")
    arguments = a.get("arguments", {})
    timeout = int(a.get("timeout", 120))
    my_name = CFG.get("name", "node")

    # 先查本地能力
    local_caps = get_caps()
    if local_caps.get(cap_query):
        # 本地就能干，直接执行
        fn = TOOL_FUNCS.get(tool)
        if fn:
            return fn(arguments)
        return "本地有 %s 但工具 %s 不存在" % (cap_query, tool), True

    # 查 peers 的能力，找匹配的
    peers = load_peers()
    candidates = []
    for pname, pinfo in peers.items():
        try:
            text, is_err = peer_call(pinfo["url"], pinfo.get("token", ""),
                                     "caps", {}, timeout=15)
            if not is_err:
                pcaps = json.loads(text)
                if pcaps.get(cap_query):
                    candidates.append((pname, pinfo, pcaps))
        except Exception:
            pass

    if not candidates:
        return "mesh 中没有节点具备 %s 能力" % cap_query, True

    # 委派给第一个匹配的节点
    pname, pinfo, pcaps = candidates[0]
    audit_log("delegate", my_name, pname, tool, "delegated")
    try:
        text, is_err = peer_call(pinfo["url"], pinfo.get("token", ""),
                                 tool, arguments, timeout=timeout)
        audit_log("delegate", my_name, pname, tool, "ok" if not is_err else "error")
        return "【委派给 %s】\n%s" % (pname, text), is_err
    except Exception as e:
        audit_log("delegate", my_name, pname, tool, "failed", str(e))
        return "委派给 %s 失败: %s" % (pname, e), True


# ---------------------------------------------------------------------------
# Agent 协作：多节点工作群组
# ---------------------------------------------------------------------------
GROUPS_PATH = os.path.join(HERE, "groups.json")
_groups_lock = threading.Lock()


def _load_groups():
    try:
        with open(GROUPS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_groups(d):
    tmp = GROUPS_PATH + ".tmp"
    with _groups_lock:
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, GROUPS_PATH)


def t_group_form(a):
    """创建一个多节点工作群组。"""
    gname = a.get("name", "")
    members = a.get("members", [])
    purpose = a.get("purpose", "")
    if not gname:
        return "name 不能为空", True
    my_name = CFG.get("name", "node")
    if my_name not in members:
        members.append(my_name)
    groups = _load_groups()
    groups[gname] = {"members": members, "purpose": purpose,
                     "created": time.strftime("%Y-%m-%d %H:%M:%S"), "leader": my_name}
    _save_groups(groups)
    # 通知其他成员
    peers = load_peers()
    for m in members:
        if m == my_name:
            continue
        if m in peers:
            try:
                peer_call(peers[m]["url"], peers[m].get("token", ""),
                          "msg_send", {"from": my_name, "to": m, "topic": "group-invite",
                                       "text": json.dumps({"group": gname, "purpose": purpose})},
                          timeout=10)
            except Exception:
                pass
    return "群组 %s 已创建，成员: %s" % (gname, ", ".join(members)), False


def t_group_list(a):
    """列出所有工作群组。"""
    groups = _load_groups()
    if not groups:
        return "（没有群组）", False
    lines = []
    for gname, info in groups.items():
        lines.append("%s [%s]: %s (成员: %s)" % (gname, info.get("leader", "?"),
                     info.get("purpose", ""), ", ".join(info.get("members", []))))
    return "\n".join(lines), False


def t_group_disband(a):
    """解散一个工作群组。"""
    gname = a.get("name", "")
    groups = _load_groups()
    if gname not in groups:
        return "群组 %s 不存在" % gname, True
    del groups[gname]
    _save_groups(groups)
    return "群组 %s 已解散" % gname, False


# ---------------------------------------------------------------------------
# Mesh 拓扑
# ---------------------------------------------------------------------------
def t_mesh(a):
    """返回 mesh 拓扑信息：本节点 + 所有 peers 的在线状态和能力摘要。"""
    my_name = CFG.get("name", "node")
    peers = load_peers().get("nodes", {})
    topology = {"self": {"name": my_name, "caps": get_caps(), "online": True}}
    topology["peers"] = {}
    for pname, pinfo in peers.items():
        entry = {"url": pinfo.get("url", ""), "tailnet": pinfo.get("tailnet", False)}
        # 探活
        try:
            text, is_err = peer_call(pinfo["url"], pinfo.get("token", ""),
                                     "peer_info", {}, timeout=8)
            entry["online"] = not is_err
            if not is_err:
                try:
                    entry["info"] = json.loads(text)
                except ValueError:
                    entry["info"] = text
        except Exception:
            entry["online"] = False
        topology["peers"][pname] = entry
    return json.dumps(topology, ensure_ascii=False), False


# ---------------------------------------------------------------------------
# v4.2: 连接模式 + 控制模式工具
# ---------------------------------------------------------------------------

def t_node_mode(a):
    """v4.2: 查询或切换节点的连接模式和控制模式。
    - connection_mode: permanent(长期) / temporary(临时)
    - control_mode: direct(远程直接控制) / assisted(本机agent辅助)
    - 临时模式显示 TTL 倒计时
    """
    action = a.get("action", "query")
    my_name = CFG.get("name", "node")

    if action == "query":
        info = {
            "node": my_name,
            "connection_mode": CFG.get("connection_mode", "permanent"),
            "control_mode": CFG.get("control_mode", "direct"),
            "ttl": CFG.get("ttl", 0),
            "version": AGENT_VERSION,
        }
        if _ttl_deadline:
            remaining = max(0, int(_ttl_deadline - time.time()))
            info["ttl_remaining"] = remaining
        return json.dumps(info, ensure_ascii=False), False

    if action == "set_control":
        new_mode = a.get("control_mode", "")
        if new_mode not in ("direct", "assisted"):
            return "control_mode 必须是 direct 或 assisted", True
        CFG["control_mode"] = new_mode
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(CFG, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return "控制模式已切换为: %s" % new_mode, False

    if action == "set_connection":
        new_mode = a.get("connection_mode", "")
        if new_mode not in ("permanent", "temporary"):
            return "connection_mode 必须是 permanent 或 temporary", True
        CFG["connection_mode"] = new_mode
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(CFG, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return "连接模式已切换为: %s" % new_mode, False

    if action == "shutdown":
        """临时节点主动退出：发 goodbye，然后关闭。"""
        _send_goodbye()
        return "节点 %s 正在退出..." % my_name, False

    return "未知 action: %s (可用: query/set_control/set_connection/shutdown)" % action, True


def t_request_assist(a):
    """v4.2: assisted 模式下，远程 agent 向本机 agent 发辅助请求。
    请求存入 assisted 队列，本机 agent（如 Claude/Cursor）可读取并处理。
    本机 agent 有本地知识，能审查危险操作、加上下文、决定是否执行。
    """
    request_text = a.get("request", "")
    tool = a.get("tool", "")
    args = a.get("args", {})
    priority = a.get("priority", "normal")

    if not request_text and not tool:
        return "需要提供 request(自然语言描述) 或 tool(要执行的工具名)", True

    req = {
        "id": uuid.uuid4().hex[:12],
        "from": a.get("from", "remote-agent"),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
        "request": request_text,
        "tool": tool,
        "args": args,
        "priority": priority,
    }

    # 存入辅助请求队列
    queue_path = CFG.get("assisted_queue", "") or os.path.join(HERE, "assisted_queue.jsonl")
    with io.open(queue_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")

    # 同时发一条消息到邮箱，让本机 agent 能 msg_recv 收到通知
    if _mail_engine:
        _mail_engine.inbox_append({
            "from": req["from"],
            "to": _mail_engine.node_name,
            "text": "[辅助请求] %s" % (request_text or ("执行 %s" % tool)),
            "topic": "assist-request",
            "request_id": req["id"],
            "ts": req["ts"],
        })

    return "辅助请求已提交 (id=%s)。本机 agent 将审查并处理。\n用 node_mode action=query 查看状态。" % req["id"], False


def t_assist_queue(a):
    """v4.2: 本机 agent 查看/处理辅助请求队列。
    - action=list: 列出待处理请求
    - action=approve: 批准并执行某个请求
    - action=deny: 拒绝某个请求
    - action=result: 查看已处理请求的结果
    """
    action = a.get("action", "list")
    queue_path = CFG.get("assisted_queue", "") or os.path.join(HERE, "assisted_queue.jsonl")

    # 加载队列
    queue = []
    if os.path.exists(queue_path):
        with io.open(queue_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        queue.append(json.loads(line))
                    except ValueError:
                        pass

    if action == "list":
        pending = [q for q in queue if q.get("status") == "pending"]
        if not pending:
            return "没有待处理的辅助请求", False
        lines = []
        for q in pending:
            lines.append("[%s] %s from=%s priority=%s\n  %s" % (
                q["id"], q["ts"], q.get("from", ""), q.get("priority", ""),
                q.get("request", "") or ("执行 %s(%s)" % (q.get("tool", ""), json.dumps(q.get("args", {}))))))
        return "\n".join(lines), False

    if action == "approve":
        req_id = a.get("id", "")
        reason = a.get("reason", "")
        for q in queue:
            if q["id"] == req_id and q.get("status") == "pending":
                q["status"] = "approved"
                q["approved_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                q["approved_reason"] = reason
                # 执行请求的工具
                if q.get("tool"):
                    result = call_tool(q["tool"], q.get("args", {}), _bypass_assisted=True)
                    q["result"] = result.get("content", [{}])[0].get("text", "")
                    q["result_error"] = result.get("isError", False)
                else:
                    q["result"] = "已批准(无工具执行)"
                _save_assist_queue(queue_path, queue)
                return "请求 %s 已批准并执行。\n结果: %s" % (req_id, q.get("result", "")), False
        return "未找到待处理请求 %s" % req_id, True

    if action == "deny":
        req_id = a.get("id", "")
        reason = a.get("reason", "")
        for q in queue:
            if q["id"] == req_id and q.get("status") == "pending":
                q["status"] = "denied"
                q["denied_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                q["denied_reason"] = reason
                _save_assist_queue(queue_path, queue)
                return "请求 %s 已拒绝: %s" % (req_id, reason), False
        return "未找到待处理请求 %s" % req_id, True

    if action == "result":
        req_id = a.get("id", "")
        for q in queue:
            if q["id"] == req_id:
                return json.dumps(q, ensure_ascii=False, indent=2), False
        return "未找到请求 %s" % req_id, True

    return "未知 action: %s" % action, True


def _save_assist_queue(path, queue):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        for q in queue:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# 全局 TTL 倒计时
_ttl_deadline = None

def _send_goodbye():
    """v4.2: 临时节点退出时发 goodbye 通知。"""
    try:
        peers = load_peers().get("nodes", {})
        for name, info in peers.items():
            url = info.get("url", "").rstrip("/")
            for suffix in ["/mcp", "/sse", "/rpc"]:
                if url.endswith(suffix):
                    url = url[:-len(suffix)]
                    break
            try:
                payload = json.dumps({
                    "proto": "fleet", "type": "goodbye",
                    "name": CFG.get("name", "node"), "ts": time.time(),
                }).encode("utf-8")
                req = urllib.request.Request(url + "/fleet/discover", data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                # v5.0.5: 走 _peer_opener（内网禁代理），与投递/gossip 同规则
                with _peer_opener(url).open(req, timeout=5):
                    pass
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v4.0 工具注册
# ---------------------------------------------------------------------------
COLLAB_TOOLS = [
    ("caps", "返回本节点的能力清单（Docker/GPU/Tailscale/网络/工具列表等）。",
     {"type": "object", "properties": {}}),
    ("mesh_caps", "查询 mesh 内所有节点的能力清单（本机 + 所有 peers），用于发现谁有某能力。",
     {"type": "object", "properties": {}}),
    ("delegate", "在 mesh 中委派任务：指定需要的能力（如 docker/gpu），自动找到匹配的节点执行。",
     {"type": "object", "properties": {
         "capability": {"type": "string", "description": "需要的能力，如 docker / gpu / tailscale"},
         "tool": {"type": "string", "description": "在对端调用的工具名，如 exec / docker_ps"},
         "arguments": {"type": "object", "description": "传给工具的参数"},
         "timeout": {"type": "number", "description": "超时秒数，默认 120"}},
      "required": ["capability", "tool"]}),
    ("group_form", "创建多节点工作群组，成员收到邀请消息。",
     {"type": "object", "properties": {
         "name": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}},
         "purpose": {"type": "string"}},
      "required": ["name"]}),
    ("group_list", "列出所有工作群组。",
     {"type": "object", "properties": {}}),
    ("group_disband", "解散一个工作群组。",
     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    ("mesh", "返回 mesh 拓扑：本节点 + 所有 peers 的在线状态和能力摘要。",
     {"type": "object", "properties": {}}),
]

COLLAB_FUNCS = {
    "caps": t_caps, "mesh_caps": t_mesh_caps, "delegate": t_delegate,
    "group_form": t_group_form, "group_list": t_group_list,
    "group_disband": t_group_disband, "mesh": t_mesh,
}

# ---------------------------------------------------------------------------
# 工具清单（base 名，暴露时按 tool_prefix 加前缀）
# ---------------------------------------------------------------------------
BASE_TOOLS = [
    ("exec", "在设备本机执行 shell 命令（支持多行脚本/管道/重定向）。sudo=true 可提权。",
     {"type": "object", "properties": {
         "command": {"type": "string", "description": "要执行的命令"},
         "sudo": {"type": "boolean", "description": "是否用 sudo 执行"},
         "timeoutMs": {"type": "number", "description": "超时毫秒"},
         "workDir": {"type": "string"}}, "required": ["command"]}),
    ("read", "读取设备上的文件，可按行号范围读取。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}, "startLine": {"type": "number"}, "endLine": {"type": "number"}},
         "required": ["filePath"]}),
    ("write", "写入设备文件（overwrite 覆盖 / append 追加）。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}, "content": {"type": "string"},
         "mode": {"type": "string", "enum": ["overwrite", "append"]},
         "sudo": {"type": "boolean"}}, "required": ["filePath", "content"]}),
    ("edit", "替换文件中指定的文本块（oldText -> newText），无需整文件重写。dryRun=true 只预览不实际修改。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}, "oldText": {"type": "string"},
         "newText": {"type": "string"}, "sudo": {"type": "boolean"},
         "dryRun": {"type": "boolean", "description": "预览修改效果但不实际写入"}},
         "required": ["filePath", "oldText", "newText"]}),
    ("list_dir", "列出目录内容（filePath 指定目录，兼容旧参数 path）。",
     {"type": "object", "properties": {"filePath": {"type": "string"}, "path": {"type": "string", "description": "deprecated, use filePath"}}, "required": ["filePath"]}),
    ("docker_ps", "列出 Docker 容器（all=true 含已停止）。",
     {"type": "object", "properties": {"all": {"type": "boolean"}}}),
    ("docker_logs", "查看指定容器日志尾部。",
     {"type": "object", "properties": {"name": {"type": "string"}, "lines": {"type": "number"}},
      "required": ["name"]}),
    ("docker_restart", "重启指定 Docker 容器。",
     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    ("systemctl", "管理 systemd 服务（status/start/stop/restart/is-active/is-enabled/enable/disable）。",
     {"type": "object", "properties": {
         "action": {"type": "string",
                    "enum": ["status", "start", "stop", "restart", "is-active", "is-enabled", "enable", "disable"]},
         "unit": {"type": "string"}}, "required": ["action", "unit"]}),
    ("service_logs", "查看 systemd 服务日志（journalctl -u）。",
     {"type": "object", "properties": {"unit": {"type": "string"}, "lines": {"type": "number"}},
      "required": ["unit"]}),
    ("sysinfo", "设备系统概览：主机/负载/内存/磁盘/CPU TOP5。",
     {"type": "object", "properties": {}}),
    ("http_get", "在设备本机发起 HTTP GET（用于测本机服务，如 http://127.0.0.1:8000/v1/models）。",
     {"type": "object", "properties": {"url": {"type": "string"}, "timeout": {"type": "number"}},
      "required": ["url"]}),
    ("exec_async", "后台启动一个 shell 命令，立即返回 job_id（长任务不阻塞）。",
     {"type": "object", "properties": {
         "command": {"type": "string", "description": "要执行的命令"},
         "sudo": {"type": "boolean", "description": "是否用 sudo 执行"},
         "workDir": {"type": "string"}}, "required": ["command"]}),
    ("exec_poll", "取后台任务的增量输出（offset=上次返回的字节数）。",
     {"type": "object", "properties": {
         "job_id": {"type": "string"},
         "offset": {"type": "number", "description": "从第几个字节开始取，默认 0"}},
      "required": ["job_id"]}),
    ("exec_result", "等待后台任务完成（最多 timeoutMs），返回全量输出。",
     {"type": "object", "properties": {
         "job_id": {"type": "string"},
         "timeoutMs": {"type": "number", "description": "最长等待毫秒，默认 120000"}},
      "required": ["job_id"]}),
    ("exec_kill", "终止一个后台任务。",
     {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
    ("exec_jobs", "列出当前所有后台任务（job_id / 状态 / 输出大小）。",
     {"type": "object", "properties": {}}),
    ("search", "P2: 在指定目录下搜索文件内容(grep)或文件名(find)，结构化输出。",
     {"type": "object", "properties": {
         "filePath": {"type": "string", "description": "搜索目录"},
         "pattern": {"type": "string", "description": "搜索关键词"},
         "type": {"type": "string", "enum": ["content", "filename"], "description": "content=搜内容, filename=搜文件名"},
         "maxResults": {"type": "number", "description": "最大返回条数，默认50"}},
      "required": ["filePath", "pattern"]}),
    ("upload", "P2: 上传 base64 编码的文件内容到指定路径（用于二进制文件传输）。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}, "content": {"type": "string", "description": "base64 编码的内容"},
         "sudo": {"type": "boolean"}}, "required": ["filePath", "content"]}),
    ("download", "P2: 读取文件并返回 base64 编码内容（用于二进制文件传输）。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}}, "required": ["filePath"]}),
    ("ps", "P2: 列出进程（按 CPU 排序），all=true 列出全部。",
     {"type": "object", "properties": {"all": {"type": "boolean"}}}),
    ("kill", "P2: 终止指定进程。signal 默认 TERM，可选 KILL/HUP/INT 等。",
     {"type": "object", "properties": {
         "pid": {"type": "number"}, "signal": {"type": "string"}, "sudo": {"type": "boolean"}},
      "required": ["pid"]}),
    ("net_check", "P2: 网络诊断。action=port(端口探测) dns(DNS解析) listen(列出本机监听端口)。",
     {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["port", "dns", "listen"]},
         "host": {"type": "string", "description": "目标主机/域名"},
         "port": {"type": "number", "description": "目标端口(action=port时)"},
         "timeout": {"type": "number", "description": "超时秒数(action=port时)"}},
      "required": ["action"]}),
    ("msg_send", "节点间消息：给另一个 AI/节点发消息（存进本节点邮箱）。from=你的名字, to=对方名字或 *(广播), topic=可选主题。",
     {"type": "object", "properties": {
         "from": {"type": "string"}, "to": {"type": "string"}, "text": {"type": "string"},
         "topic": {"type": "string", "description": "可选主题/频道，如 fleet-join, task-request"}},
      "required": ["from", "to", "text"]}),
    ("msg_recv", "节点间消息：收发给我的新消息；wait=秒（最多300）没有新消息时阻塞等待。topic=只收该主题。",
     {"type": "object", "properties": {
         "me": {"type": "string"}, "wait": {"type": "number"},
         "after_id": {"type": "number", "description": "只取 id 大于它的消息（不改已读位置）"},
         "peek": {"type": "boolean", "description": "只看不标记已读"},
         "topic": {"type": "string", "description": "只收该主题的消息"}},
      "required": ["me"]}),
    ("msg_history", "节点间消息：查看邮箱最近 N 条（可按主题过滤）。",
     {"type": "object", "properties": {"lines": {"type": "number"}, "topic": {"type": "string"}}}),
    ("node_mode", "v4.2: 查询/切换节点模式。action=query(查状态) set_control(切换控制模式) set_connection(切换连接模式) shutdown(临时节点退出)。",
     {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["query", "set_control", "set_connection", "shutdown"]},
         "control_mode": {"type": "string", "enum": ["direct", "assisted"]},
         "connection_mode": {"type": "string", "enum": ["permanent", "temporary"]}},
      "description": "action=query 不需要其他参数"}),
    ("request_assist", "v4.2: assisted 模式下向本机 agent 发辅助请求（本机 agent 审查后执行）。",
     {"type": "object", "properties": {
         "request": {"type": "string", "description": "自然语言描述想做什么"},
         "tool": {"type": "string", "description": "想执行的工具名(可选)"},
         "args": {"type": "object", "description": "工具参数(可选)"},
         "from": {"type": "string", "description": "你的名字"},
         "priority": {"type": "string", "enum": ["low", "normal", "high"]}},
      "description": "request 和 tool 至少给一个"}),
    ("assist_queue", "v4.2: 本机 agent 查看/处理辅助请求队列。action=list/approve/deny/result。",
     {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "approve", "deny", "result"]},
         "id": {"type": "string", "description": "请求ID(approve/deny/result时需要)"},
         "reason": {"type": "string", "description": "批准/拒绝理由"}},
      "description": "action=list 不需要 id"}),
]

BASE_FUNCS = {
    "exec": t_exec, "read": t_read, "write": t_write, "edit": t_edit,
    "list_dir": t_list_dir, "docker_ps": t_docker_ps, "docker_logs": t_docker_logs,
    "docker_restart": t_docker_restart, "systemctl": t_systemctl,
    "service_logs": t_service_logs, "sysinfo": t_sysinfo, "http_get": t_http_get,
    "exec_async": t_exec_async, "exec_poll": t_exec_poll,
    "exec_result": t_exec_result, "exec_kill": t_exec_kill,
    "exec_jobs": t_exec_jobs,
    "search": t_search, "upload": t_upload, "download": t_download,
    "ps": t_ps, "kill": t_kill, "net_check": t_net_check,
    "msg_send": t_msg_send, "msg_recv": t_msg_recv, "msg_history": t_msg_history,
    "node_mode": t_node_mode, "request_assist": t_request_assist,
    "assist_queue": t_assist_queue,
}
if CFG.get("peer_tools", True):
    BASE_TOOLS += PEER_TOOLS
    BASE_FUNCS.update(PEER_FUNCS)
if CFG.get("collab_tools", True):
    BASE_TOOLS += COLLAB_TOOLS
    BASE_FUNCS.update(COLLAB_FUNCS)

# 对外暴露的工具（含前缀）
TOOLS = []
for _base, _desc, _schema in BASE_TOOLS:
    TOOLS.append({"name": PREFIX + _base, "description": _desc, "inputSchema": _schema})

# v5.0.7: read-only 模式禁用的工具（变更/对外副作用类）。只读节点纯观察：
# 本机命令执行/文件写入/服务与容器变更/消息发送/后台任务与外联控制全部拒绝；
# 查询类（read/list_dir/sysinfo/docker_ps/docker_logs/service_logs/http_get/
# exec_poll/exec_result/exec_jobs/search/download/ps/net_check/msg_recv/
# msg_history/node_mode/peers/peer_info/caps/mesh_caps/mesh/group_list）放行。
READ_ONLY_BLOCKED = {
    "exec", "write", "edit", "upload", "docker_restart", "systemctl",
    "exec_async", "exec_kill", "kill", "msg_send", "request_assist",
    "assist_queue",
    # 对端/协作控制类：不主动指使其他节点执行动作
    "peer_call", "peer_add", "peer_remove", "delegate", "group_form", "group_disband",
    # v5.2 运维/自对话中会改变外部状态的
    "fleet_token_rotate", "fleet_handoff",
    "fleet_repair", "fleet_repair_loop",
}

# 查找表：带前缀名、裸名、以及旧版 nas_ 前缀名都接受（向后兼容）
TOOL_FUNCS = {}
for _base, _fn in BASE_FUNCS.items():
    TOOL_FUNCS[PREFIX + _base] = _fn
    TOOL_FUNCS[_base] = _fn
    TOOL_FUNCS["nas_" + _base] = _fn


# ---------------------------------------------------------------------------
# v5.2 运维 / 自对话工具
# ---------------------------------------------------------------------------
_AGENT_CLIENT = None
_AGENT_CTX = None


def _agent_ctx():
    """延迟构造 Agent 工具上下文（依赖 call_tool 已在运行时可用）。"""
    global _AGENT_CLIENT, _AGENT_CTX
    if _AGENT_CTX is not None:
        return _AGENT_CTX
    client = None
    if _HAS_AGENT:
        try:
            client = _agent_layer.FleetClient(call_tool, name=SERVER_NAME,
                                              policy=EXEC_POLICY)
        except Exception:
            client = None
    _AGENT_CLIENT = client
    _AGENT_CTX = {"store": TASK_STORE, "client": client, "policy": EXEC_POLICY,
                  "agent": SERVER_NAME, "cfg": CFG, "identity": IDENTITY,
                  "call_tool": call_tool}
    return _AGENT_CTX


def _json_text(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def t_fleet_identity(a):
    """返回本节点 v5.2 身份与信任状态。"""
    d = {"name": SERVER_NAME, "version": SERVER_VERSION,
         "node_id": IDENTITY.node_id if IDENTITY else "",
         "fingerprint": IDENTITY.fingerprint if IDENTITY else "",
         "public_key": IDENTITY.public_key_b64 if IDENTITY else "",
         "trust": "S2/S3" if IDENTITY else ("S1" if CFG.get("auth_token") else "S0"),
         "require_signature": bool(CFG.get("require_signature")),
         "compat_v5": bool(CFG.get("compat_v5", True)),
         "data_dir": DATA_DIR,
         "peers": len(load_peers().get("nodes", {}))}
    return _json_text(d), False


def t_fleet_metrics(a):
    """返回指标快照；format=prometheus 时返回 Prometheus 文本。"""
    if METRICS is None:
        return "fleet_core 未启用，指标不可用", True
    if (a or {}).get("format") == "prometheus":
        return METRICS.render_prometheus(), False
    return _json_text(METRICS.snapshot()), False


def t_fleet_audit_verify(a):
    """校验哈希链审计是否被篡改，返回最近记录。"""
    if AUDIT_CHAIN is None:
        return "fleet_core 未启用，审计链不可用", True
    ok, bad = AUDIT_CHAIN.verify()
    tail = []
    try:
        tail = AUDIT_CHAIN.tail(int((a or {}).get("tail", 10)))
    except Exception:
        pass
    return _json_text({"ok": ok, "bad_index": bad, "tail": tail}), not ok


def t_fleet_doctor(a):
    """12 项自检：环境、身份、端口、磁盘、审计、策略、对端、TLS 等。"""
    import shutil
    checks = []

    def add(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "检查异常: %r" % (e,)
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("python", lambda: (sys.version_info >= (3, 6),
                           "%s.%s.%s" % sys.version_info[:3]))
    add("identity", lambda: (IDENTITY is not None,
                             IDENTITY.fingerprint if IDENTITY else "缺失（token 降级）"))
    add("config_perm", lambda: (
        not os.path.exists(CONFIG_PATH) or (os.stat(CONFIG_PATH).st_mode & 0o077) == 0,
        oct(os.stat(CONFIG_PATH).st_mode & 0o777) if os.path.exists(CONFIG_PATH) else "无配置"))
    add("auth", lambda: (bool(CFG.get("auth_token") or CFG.get("require_signature")),
                         "token=%s signature=%s" % (
                             "有" if CFG.get("auth_token") else "无",
                             "必须" if CFG.get("require_signature") else "可选")))
    add("policy", lambda: (EXEC_POLICY is not None, "enabled" if EXEC_POLICY else "disabled"))
    add("audit", lambda: (AUDIT_CHAIN is not None and AUDIT_CHAIN.verify()[0],
                          "chain-ok" if AUDIT_CHAIN and AUDIT_CHAIN.verify()[0] else "未启用/损坏"))
    add("disk", lambda: (
        shutil.disk_usage(DATA_DIR).free > 50 * 1024 * 1024,
        "%.1f GB free" % (shutil.disk_usage(DATA_DIR).free / 1024.0 / 1024.0 / 1024.0)))
    add("port", lambda: _doctor_port())
    add("peers", lambda: (True, "%d 个已配对节点" % len(load_peers().get("nodes", {}))))
    add("tls", lambda: (bool(CFG.get("tls_cert")),
                        "已配置" if CFG.get("tls_cert") else "未配置（局域网可接受）"))
    add("time", lambda: _doctor_time())
    add("read_only", lambda: (True, "只读" if CFG.get("read_only") else "可写"))
    # 只有关键项失败才让自检整体失败；权限/鉴权/TLS/对端属于加固建议，
    # 仍逐项展示，避免“内网未配 TLS”被误判为不可用。
    critical = {"python", "identity", "policy", "audit", "disk", "port", "time"}
    ok = all(c["ok"] for c in checks if c["name"] in critical)
    return _json_text({"ok": ok, "checks": checks,
                       "summary": "%d/%d 通过" % (sum(1 for c in checks if c["ok"]),
                                                  len(checks))}), not ok


def _doctor_port():
    """确认本机监听端口可连（不依赖外部网络）。"""
    port = int(CFG.get("port") or LISTEN_PORT or 3100)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True, "127.0.0.1:%d 可连" % port
    except Exception as e:
        return False, "127.0.0.1:%d 不可连: %s" % (port, e)
    finally:
        s.close()


def _doctor_time():
    """检查系统时间是否合理（防止签名时间窗全失败）。"""
    now = time.time()
    if now < 1600000000:
        return False, "系统时间异常（%s），签名校验会失败" % time.ctime(now)
    return True, time.strftime("%F %T")



# ---------------------------------------------------------------------------
# v5.2: 自检 / 自修复（连接上的 agent 可据此发现并修复常见故障）
# ---------------------------------------------------------------------------
_SELFCHECK_ACTIONS = {
    "restart_service": "重启本节点服务（ctl/systemd/launchd/Termux 统一入口）",
    "fix_permissions": "修正配置/数据/mailbox 权限（0600/0700）",
    "clear_stale_pid": "清理残留 pid 文件（进程已不存在时）",
    "rollback": "回滚到最近一次升级备份（高风险，需 allow_rollback）",
}


def _pid_alive_local(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _listener_pid(port):
    """返回监听该端口的 PID（仅当命令行含 mcp_agent.py，避免误杀其它服务）。"""
    import subprocess as _sp
    pid = ""
    try:
        p = _sp.run(["lsof", "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-t"],
                    stdout=_sp.PIPE, stderr=_sp.DEVNULL, timeout=10)
        pid = (p.stdout.decode().strip().splitlines() or [""])[0]
    except Exception:
        pid = ""
    if not pid:
        try:
            p = _sp.run(["ss", "-ltnp"], stdout=_sp.PIPE,
                        stderr=_sp.DEVNULL, timeout=10)
            for line in p.stdout.decode("utf-8", "replace").splitlines():
                if (":%d " % int(port)) in line and "pid=" in line:
                    pid = line.split("pid=")[1].split(",")[0]
                    break
        except Exception:
            pid = ""
    if not pid.isdigit():
        return 0
    # 校验进程确实是我们自己的节点，避免误杀端口上的其它程序
    try:
        if os.path.exists("/proc/%s/cmdline" % pid):
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().decode("utf-8", "replace")
        else:
            p = _sp.run(["ps", "-p", pid, "-o", "command="], stdout=_sp.PIPE,
                        stderr=_sp.DEVNULL, timeout=10)
            cmd = p.stdout.decode("utf-8", "replace")
        return int(pid) if "mcp_agent.py" in cmd else 0
    except Exception:
        return 0


def _selfcheck_findings():
    """返回结构化发现列表（只读，不修改任何状态）。"""
    import shutil
    findings = []

    def add(code, severity, title, detail="", action=None, repairable=False):
        findings.append({"code": code, "severity": severity, "title": title,
                         "detail": str(detail)[:300], "action": action,
                         "repairable": bool(repairable and action)})

    # 配置 / 身份
    if not os.path.exists(CONFIG_PATH):
        add("config_missing", "critical", "配置文件不存在", CONFIG_PATH,
            "restart_service", False)
    else:
        if os.stat(CONFIG_PATH).st_mode & 0o077:
            add("config_perm", "high", "配置权限过宽",
                oct(os.stat(CONFIG_PATH).st_mode & 0o777),
                "fix_permissions", True)
    if IDENTITY is None:
        add("identity_missing", "high", "节点身份缺失（token 降级）",
            "fleet_core 未启用或 identity.json 损坏", None, False)
    if not (CFG.get("auth_token") or CFG.get("require_signature")):
        add("auth_disabled", "medium", "未启用鉴权",
            "token 与签名都未开启；对外监听时风险高", None, False)
    # 审计链
    try:
        if AUDIT_CHAIN is not None and not AUDIT_CHAIN.verify()[0]:
            add("audit_broken", "high", "审计哈希链校验失败",
                "日志可能被篡改/损坏，请保留现场", None, False)
    except Exception as e:
        add("audit_error", "high", "审计链无法校验", repr(e), None, False)
    # 磁盘
    try:
        free = shutil.disk_usage(DATA_DIR).free
        if free < 100 * 1024 * 1024:
            add("disk_low", "critical", "数据目录磁盘不足",
                "%.1f MB free" % (free / 1048576.0), None, False)
    except Exception as e:
        add("disk_error", "high", "磁盘检查失败", repr(e), None, False)
    # 端口
    ok_port, port_detail = _doctor_port()
    if not ok_port:
        add("port_unreachable", "critical", "本机监听端口不可连",
            port_detail, "restart_service", True)
    # 权限目录
    for label, path, mode in (("data_dir", DATA_DIR, 0o700),
                              ("mailbox", os.path.join(HERE, "mailbox"), 0o700)):
        if os.path.exists(path):
            try:
                cur = os.stat(path).st_mode & 0o077
                if cur:
                    add("dir_perm", "medium", "%s 权限过宽" % label,
                        oct(os.stat(path).st_mode & 0o777),
                        "fix_permissions", True)
            except Exception:
                pass
            if not os.access(path, os.W_OK):
                add("dir_unwritable", "high", "%s 不可写" % label, path,
                    "fix_permissions", True)
    # 残留 pid
    pid_path = os.path.join(DATA_DIR, "fleet.pid")
    try:
        if os.path.exists(pid_path):
            pid = open(pid_path, encoding="ascii").read().strip()
            if pid and not _pid_alive_local(pid):
                listener = _listener_pid(int(CFG.get("port") or LISTEN_PORT or 3100))
                if listener:
                    add("stale_pid", "high", "pid 文件与实际进程不一致",
                        "pidfile=%s, listener=%s" % (pid, listener),
                        "restart_service", True)
                else:
                    add("stale_pid", "medium", "残留 pid 文件（进程已退出）",
                        "pid=%s" % pid, "clear_stale_pid", True)
    except Exception:
        pass
    return findings


def t_fleet_selfcheck(a):
    """只读自检：返回可机读的故障发现与修复计划，不修改任何状态。"""
    findings = _selfcheck_findings()
    critical = [f for f in findings if f["severity"] == "critical"]
    plan = []
    for f in findings:
        if f["repairable"] and f["action"] not in plan:
            plan.append(f["action"])
    return _json_text({
        "ok": not critical,
        "summary": "%d 项发现（critical=%d，可自动修复=%d）"
                   % (len(findings), len(critical),
                      sum(1 for f in findings if f["repairable"])),
        "findings": findings,
        "repair_plan": plan,
        "actions": _SELFCHECK_ACTIONS,
        "next": ("先调用 fleet_repair_loop（最多 3 次）自动修复；"
                 "仍失败则把 findings 原样报告给用户，不要猜测。"),
    }), bool(critical)


def _sh_quote_local(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


def _repair_do(action):
    """执行单个白名单修复动作；返回 (ok, detail)。"""
    if action == "restart_service":
        ctl = os.path.join(HERE, "fleet-ctl.sh")
        if not os.path.exists(ctl):
            return False, "找不到 fleet-ctl.sh（请手工 systemctl restart）"
        port = int(CFG.get("port") or LISTEN_PORT or 3100)
        pid_path = os.path.join(DATA_DIR, "fleet.pid")
        listener = _listener_pid(port)
        # 校准 pidfile：ctl restart 依赖它找到旧进程，避免双进程
        if listener:
            try:
                with open(pid_path + ".tmp", "w", encoding="ascii") as f:
                    f.write(str(listener))
                os.replace(pid_path + ".tmp", pid_path)
            except OSError:
                pass
        # 不能在本进程里同步 kill 自己：调度 1 秒后的外部重启，先返回响应
        helper = ("sleep 1; sh %s restart" % _sh_quote_local(ctl))
        try:
            subprocess.Popen(["sh", "-c", helper], cwd=HERE,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as e:
            return False, "调度重启失败: %r" % (e,)
        return True, "重启已调度（约 1 秒后执行；请等待 10 秒后重新自检）"
    if action == "fix_permissions":
        fixed = []
        for path, mode in ((CONFIG_PATH, 0o600),
                           (DATA_DIR, 0o700),
                           (os.path.join(HERE, "mailbox"), 0o700)):
            try:
                if os.path.exists(path):
                    os.chmod(path, mode)
                    fixed.append(path)
            except Exception as e:
                return False, "chmod %s 失败: %r" % (path, e)
        return True, "已修正: %s" % ", ".join(fixed)
    if action == "clear_stale_pid":
        pid_path = os.path.join(DATA_DIR, "fleet.pid")
        try:
            if os.path.exists(pid_path):
                pid = open(pid_path, encoding="ascii").read().strip()
                if pid and _pid_alive_local(pid):
                    return False, "进程 %s 仍存活，拒绝清理 pid" % pid
                os.remove(pid_path)
                return True, "已清理 %s" % pid_path
            return True, "无 pid 文件"
        except Exception as e:
            return False, "清理 pid 失败: %r" % (e,)
    if action == "rollback":
        installer = os.path.join(HERE, "fleet_install.py")
        if not os.path.exists(installer):
            return False, "找不到 fleet_install.py，无法回滚"
        name = str(CFG.get("name") or SERVER_NAME)
        argv = [sys.executable, installer, "--rollback",
                "--name", name, "--dir", HERE, "--data-dir", DATA_DIR]
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=240)
            out = p.stdout.decode("utf-8", "replace").strip()[-1000:]
            return p.returncode == 0, "exit=%d %s" % (p.returncode, out)
        except Exception as e:
            return False, "回滚失败: %r" % (e,)
    return False, "未知修复动作: %s" % action


def t_fleet_repair(a):
    """执行单个白名单修复动作；必须 confirm=节点名，默认拒绝。"""
    a = a or {}
    action = str(a.get("action") or "")
    if CFG.get("read_only"):
        return "只读节点拒绝修复动作", True
    if action not in _SELFCHECK_ACTIONS:
        return "未知动作：%s（可选 %s）" % (action, ", ".join(_SELFCHECK_ACTIONS)), True
    if a.get("confirm") not in (SERVER_NAME, True):
        return ("需要确认：action=%s 且 confirm=%s（防误操作）"
                % (action, SERVER_NAME)), True
    ok, detail = _repair_do(action)
    _audit_call("fleet_repair", {"action": action}, "ok" if ok else "error", detail[:200])
    return _json_text({"ok": ok, "action": action, "detail": detail}), not ok


def t_fleet_repair_loop(a):
    """有界自愈：自检 → 修复可修项 → 复检，最多 max_attempts 次（默认 2，上限 3）。

    安全边界：只允许重启/权限/pid 三类动作；rollback 需 allow_rollback=true；
    绝不动数据、token、代码；只读节点直接拒绝。失败即停并报告。
    """
    a = a or {}
    if CFG.get("read_only"):
        return "只读节点拒绝自愈", True
    if a.get("confirm") not in (SERVER_NAME, True):
        return "需要确认：confirm=%s" % SERVER_NAME, True
    try:
        max_attempts = max(1, min(3, int(a.get("max_attempts") or 2)))
    except Exception:
        max_attempts = 2
    allow_rollback = bool(a.get("allow_rollback"))
    before = _selfcheck_findings()
    attempts = []
    findings = before
    for _ in range(max_attempts):
        todo = [f for f in findings if f["repairable"]]
        if allow_rollback:
            todo += [f for f in findings if f["code"] in ("version_broken", "rollback_needed")]
        if not todo:
            break
        action = todo[0]["action"]
        if action == "rollback" and not allow_rollback:
            break
        ok, detail = _repair_do(action)
        _audit_call("fleet_repair_loop", {"action": action}, "ok" if ok else "error",
                    detail[:200])
        attempts.append({"action": action, "ok": ok, "detail": detail,
                         "findings_left": len(findings)})
        if not ok:
            break
        if action == "restart_service":
            # 重启由外部进程在 1s 后执行；本进程不能立即复检（会先被杀）
            return _json_text({
                "ok": True, "before": before, "after": findings,
                "attempts": attempts, "repaired_actions": [action],
                "remaining": [], "restart_scheduled": True,
                "next": "等待约 10 秒后重新连接并调用 fleet_selfcheck 确认。",
            }), False
        findings = _selfcheck_findings()
    after = _selfcheck_findings()
    repaired = [action for action in _SELFCHECK_ACTIONS
                if not any(f["action"] == action for f in after)]
    return _json_text({
        "ok": not any(f["severity"] == "critical" for f in after),
        "before": before, "after": after, "attempts": attempts,
        "repaired_actions": repaired,
        "remaining": [f for f in after if f["severity"] in ("critical", "high")],
        "next": ("若仍有 critical/high：停止自愈，把 remaining 报告给用户；"
                 "禁止循环重试或执行未列出的命令。"),
    }), any(f["severity"] == "critical" for f in after)


def t_fleet_token_rotate(a):
    """轮换 auth_token（需要 confirm=节点名，防误触发）。"""
    if not (a or {}).get("confirm"):
        return "需要确认：带上 confirm=%s 才会轮换 token" % SERVER_NAME, True
    if (a or {}).get("confirm") != SERVER_NAME and (a or {}).get("confirm") is not True:
        return "确认值不匹配：confirm 必须是 %s" % SERVER_NAME, True
    old = CFG.get("auth_token") or ""
    new = gen_token()
    CFG["auth_token"] = new
    save_config(CFG)
    _audit_call("fleet_token_rotate", {"rotated": bool(old)}, "ok", "token rotated")
    return _json_text({"ok": True, "token": new,
                       "warning": "旧 token 立即失效；请把新 token 配置到对端后重启对端。",
                       "config": CONFIG_PATH}), False


def t_fleet_invite(a):
    """生成 v5.2 邀请 URI（Ed25519 签名 + 过期 + 一次性 nonce）。"""
    if IDENTITY is None:
        return "节点身份不可用（fleet_core 未启用）", True
    host = (a or {}).get("host") or _lan_ip()
    port = int((a or {}).get("port") or LISTEN_PORT or 3100)
    ttl = int((a or {}).get("ttl") or 3600)
    try:
        inv = _core.Invite.create(IDENTITY, SERVER_NAME, host, port, ttl=ttl,
                                  caps=get_caps())
        return _json_text({"ok": True, "uri": inv.to_uri(),
                           "fingerprint": IDENTITY.fingerprint,
                           "expires": time.strftime(
                               "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + ttl)),
                           "next": "在目标设备执行 fleet join '<uri>'，并核对 SAS"}), False
    except Exception as e:
        return "生成邀请失败: %r" % (e,), True


OPERATIONAL_TOOLS = [
    ("fleet_identity", "查询本节点 v5.2 身份：节点 ID、指纹、公钥、信任等级、对端数量。",
     {"type": "object", "properties": {}}),
    ("fleet_metrics", "查看节点指标快照（工具调用/投递/安全事件）；format=prometheus 返回 Prometheus 文本。",
     {"type": "object", "properties": {"format": {"type": "string",
                                                  "enum": ["json", "prometheus"]}}}),
    ("fleet_audit_verify", "校验哈希链审计日志是否被篡改，并返回最近记录。",
     {"type": "object", "properties": {"tail": {"type": "number"}}}),
    ("fleet_doctor", "节点自检：Python/身份/配置权限/鉴权/策略/审计/磁盘/端口/对端/TLS/时间/只读。",
     {"type": "object", "properties": {}}),
    ("fleet_token_rotate", "轮换访问 token（confirm=节点名 才执行）；旧 token 立即失效。",
     {"type": "object", "properties": {"confirm": {"type": "string"}}}),
    ("fleet_invite", "生成 v5.2 邀请 URI（签名+过期），供另一台设备 fleet join 配对。",
     {"type": "object", "properties": {"host": {"type": "string"},
                                       "port": {"type": "number"},
                                       "ttl": {"type": "number"}}}),
    ("fleet_selfcheck", "只读自检：返回可机读故障发现、严重级别、建议动作与修复计划；不修改状态。",
     {"type": "object", "properties": {}}),
    ("fleet_repair", "执行单个白名单修复动作（restart_service/fix_permissions/clear_stale_pid/rollback）；必须 confirm=节点名。",
     {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["restart_service", "fix_permissions",
                                               "clear_stale_pid", "rollback"]},
         "confirm": {"type": "string"}}}),
    ("fleet_repair_loop", "有界自愈：自检→修复可修项→复检，最多 3 次；只允许安全动作，失败即停并报告。",
     {"type": "object", "properties": {
         "max_attempts": {"type": "number"}, "allow_rollback": {"type": "boolean"},
         "confirm": {"type": "string"}}}),
]

OPERATIONAL_FUNCS = {
    "fleet_identity": t_fleet_identity,
    "fleet_metrics": t_fleet_metrics,
    "fleet_audit_verify": t_fleet_audit_verify,
    "fleet_doctor": t_fleet_doctor,
    "fleet_token_rotate": t_fleet_token_rotate,
    "fleet_invite": t_fleet_invite,
    "fleet_selfcheck": t_fleet_selfcheck,
    "fleet_repair": t_fleet_repair,
    "fleet_repair_loop": t_fleet_repair_loop,
}

# 合并自对话工具（fleet_help/think/plan/reflect/review/handoff）
V51_TOOLS = []
V51_FUNCS = {}
for _name, _desc, _schema in OPERATIONAL_TOOLS:
    V51_TOOLS.append({"name": _name, "description": _desc, "inputSchema": _schema,
                     "annotations": {"readOnlyHint": _name in (
                         "fleet_identity", "fleet_metrics", "fleet_audit_verify",
                         "fleet_doctor", "fleet_invite",
                         "fleet_selfcheck")}})
    V51_FUNCS[_name] = OPERATIONAL_FUNCS[_name]
if _HAS_AGENT:
    try:
        for _t in _agent_layer.SELF_DIALOGUE_TOOLS:
            V51_TOOLS.append(_t)
            V51_FUNCS[_t["name"]] = None      # 走 dispatch_tool
    except Exception as _e:
        print("[warn] 自对话工具注册失败: %s" % _e, file=sys.stderr)

for _t in V51_TOOLS:
    TOOLS.append(_t)
    for _prefix in ("", PREFIX, "nas_"):
        TOOL_FUNCS[(_prefix + _t["name"])] = _t["name"]   # 名称映射，call_tool 再分发




# ---------------------------------------------------------------------------
# P1-2: 操作审计日志
# ---------------------------------------------------------------------------
AUDIT_LOG_PATH = os.path.join(HERE, "audit.log")
AUDIT_LOCK = threading.Lock()

def _audit_log(tool_name, args_dict, client_ip=""):
    """记录每次危险工具调用到审计日志。"""
    if not CFG.get("audit_log", True):
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # 参数摘要：截断长值，避免日志爆炸
        summary = {}
        for k, v in (args_dict or {}).items():
            s = str(v)
            summary[k] = s[:200] + "..." if len(s) > 200 else s
        line = "%s | ip=%s | tool=%s | args=%s\n" % (
            ts, client_ip or "-", tool_name, json.dumps(summary, ensure_ascii=False))
        with AUDIT_LOCK:
            with open(AUDIT_LOG_PATH, "a") as f:
                f.write(line)
    except Exception:
        pass  # 审计日志写入失败不应影响主流程

def call_tool(name, args, _bypass_assisted=False):
    _bare = name
    if PREFIX and _bare.startswith(PREFIX):
        _bare = _bare[len(PREFIX):]
    if _bare.startswith("nas_"):
        _bare = _bare[4:]
    # v5.2: 运维工具 + 自对话工具
    if _bare in V51_FUNCS:
        # read-only 优先于分发：fleet_handoff/fleet_token_rotate 等变更类必须被拦
        if CFG.get("read_only") and _bare in READ_ONLY_BLOCKED:
            return {"content": [{"type": "text",
                    "text": "节点处于只读模式（read_only），工具 %s 已禁用（变更类）" % name}],
                    "isError": True}
        if _HAS_AGENT and _bare in getattr(_agent_layer, "SELF_DIALOGUE_FUNCS", {}):
            return _agent_layer.dispatch_tool(_bare, args or {}, _agent_ctx())
        fn6 = OPERATIONAL_FUNCS.get(_bare)
        if fn6 is None:
            return {"content": [{"type": "text", "text": "v5.2 工具不可用: %s" % _bare}],
                    "isError": True}
        try:
            text6, err6 = fn6(args or {})
            return {"content": [{"type": "text", "text": text6}],
                    "isError": bool(err6)}
        except Exception as e:
            return {"content": [{"type": "text", "text": "错误: %r" % (e,)}],
                    "isError": True}
    fn = TOOL_FUNCS.get(name)
    if not fn or not callable(fn):
        return {"content": [{"type": "text", "text":
                "未知工具: %s。下一步：调用 tools/list 查看可用工具，"
                "或 fleet_help(topic=\"tools\") 获取工具选择指引" % name}], "isError": True}
    # v5.0.7: read-only 只读模式 —— 变更类工具直接拒绝（带前缀的调用容错）
    # v5.0.8: 前缀剥离与 assisted 模式对齐（PREFIX 和旧版 nas_ 都剥）——
    # v5.0.7 只剥 PREFIX，用旧版 nas_ 前缀调用（nas_write 等仍为有效
    # 工具名）可完全绕过只读拦截，实测 read_only 下真实写入了文件。
    if CFG.get("read_only"):
        _base_check = name
        if PREFIX and name.startswith(PREFIX):
            _base_check = name[len(PREFIX):]
        if _base_check.startswith("nas_"):
            _base_check = _base_check[4:]
        if _base_check in READ_ONLY_BLOCKED:
            return {"content": [{"type": "text",
                    "text": "节点处于只读模式（read_only），工具 %s 已禁用（变更类）" % name}],
                    "isError": True}
    # v4.2: assisted 模式下，远程 agent 只能用白名单工具
    # node_mode/request_assist/assist_queue 始终可用
    # _bypass_assisted=True 时跳过限制（本机 agent approve 时用）
    _safe_names = {"node_mode", "request_assist", "assist_queue",
                   "msg_send", "msg_recv", "msg_history", "peers", "peer_info",
                   "caps", "mesh", "sysinfo", "list_dir", "read"}
    if CFG.get("control_mode", "direct") == "assisted" and not _bypass_assisted:
        allowed = set(CFG.get("assisted_allow", [])) | _safe_names
        # 去掉前缀比较
        bare_name = name.replace(PREFIX, "").replace("nas_", "")
        if bare_name not in allowed and name not in allowed:
            return {"content": [{"type": "text", "text":
                "[assisted 模式] 工具 %s 需要本机 agent 批准。\n"
                "请用 request_assist 提交辅助请求，或用 node_mode action=set_control control_mode=direct 切换为直接控制。" % name}],
                "isError": True}
    # v5.2: 执行策略（危险命令默认拒绝/需确认）。策略不可用时跳过，保持 v5 行为。
    if EXEC_POLICY is not None:
        try:
            decision, reason, rule_id = EXEC_POLICY.check(_bare, args or {})
        except Exception:
            decision, reason, rule_id = "allow", "", ""
        if decision == "deny":
            _audit_call(name, args, "denied", "%s (%s)" % (reason, rule_id))
            return {"content": [{"type": "text", "text":
                "执行策略拒绝：%s（规则 %s）" % (reason, rule_id)}], "isError": True}
        if decision == "ask" and not (args or {}).get("confirm"):
            _audit_call(name, args, "needs_confirm", "%s (%s)" % (reason, rule_id))
            return {"content": [{"type": "text", "text":
                "该操作需要用户确认：%s（规则 %s）。\n"
                "请先向用户说明影响；获得明确同意后，带 confirm=true 重新调用。" % (reason, rule_id)}],
                "isError": True}
    # P1-2: 审计危险工具（v5.2 改为哈希链，失败自动降级旧日志）
    _dangerous = {"exec", "exec_async", "write", "edit", "docker_restart", "systemctl"}
    if _bare in _dangerous:
        _audit_log(name, args)
    _t0 = time.time()
    try:
        text, is_err = fn(args or {})
        if METRICS is not None:
            try:
                METRICS.inc("fleet_tool_calls_total", tool=_bare,
                            status="error" if is_err else "ok")
                METRICS.observe("fleet_tool_duration_ms", (time.time() - _t0) * 1000.0,
                                tool=_bare)
            except Exception:
                pass
        if AUDIT_CHAIN is not None:
            try:
                AUDIT_CHAIN.append({"tool": _bare, "status": "error" if is_err else "ok",
                                    "args": _redact_args(args),
                                    "peer": getattr(_REQ, "peer_node", "") or "",
                                    "ms": int((time.time() - _t0) * 1000)})
            except Exception:
                pass
        return {"content": [{"type": "text", "text": text}], "isError": bool(is_err)}
    except KeyError as e:
        return {"content": [{"type": "text", "text": "Missing parameter / 缺少参数: %s" % e}], "isError": True}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": "错误: %r" % (e,)}], "isError": True}


def _redact_args(args):
    """审计/日志用参数摘要：截断 + 脱敏，避免把 token/密码写进审计链。"""
    try:
        if _HAS_CORE:
            return json.loads(_core.redact(json.dumps(args or {}, ensure_ascii=False)))
    except Exception:
        pass
    summary = {}
    for k, v in (args or {}).items():
        s = str(v)
        summary[k] = s[:120] + "..." if len(s) > 120 else s
    return summary


def _audit_call(tool, args, status, detail=""):
    """策略拒绝/待确认等安全事件的统一审计入口。"""
    if AUDIT_CHAIN is not None:
        try:
            AUDIT_CHAIN.append({"tool": tool, "status": status, "detail": detail,
                                "args": _redact_args(args),
                                "peer": getattr(_REQ, "peer_node", "") or ""})
            return
        except Exception:
            pass
    _audit_log(tool, args or {})



# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
def handle_rpc(msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    if method == "initialize":
        caps = {"tools": {}, "resources": {}}
        instructions = FALLBACK_INSTRUCTIONS
        if _HAS_AGENT:
            try:
                caps["prompts"] = {}
                instructions = _agent_layer.build_instructions(CFG)
            except Exception as _e:
                print("[warn] Agent 提示词生成失败，使用内置简版: %s" % _e, file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": caps,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": instructions,
        }}
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid,
                "result": call_tool(params.get("name"), params.get("arguments"))}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        prompts = []
        if _HAS_AGENT:
            try:
                prompts = _agent_layer.list_prompts()
            except Exception as _e:
                print("[warn] prompts/list 失败: %s" % _e, file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": prompts}}
    if method == "prompts/get":
        if not _HAS_AGENT:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "prompts unavailable"}}
        try:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": _agent_layer.get_prompt(
                        params.get("name"), params.get("arguments") or {})}
        except Exception as _e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "invalid prompt: %s" % _e}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


SESSIONS = {}

def _enable_tcp_keepalive(sock):
    """TCP 保活：对端（主端/隧道）悄悄消失时，内核能在几分钟内发现并断开。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    except Exception:
        pass


class FleetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        # 单个连接出错不影响整体服务；只记一行，不打整段 traceback
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionError, socket.timeout, TimeoutError)):
            return
        print("[%s] 连接 %s 处理异常: %r" % (time.strftime("%F %T"), client_address, e),
              file=sys.stderr)


# SSE 并发计数：每个 /sse 连接会常驻一个 while True 线程，无上限时可被打满。
SSE_LOCK = threading.Lock()
SSE_ACTIVE = 0

# v5.0.7: 并发连接总数信号量（连接级）。ThreadingHTTPServer 为每个连接创建
# 独立线程且无上限，恶意洪水/网络扫描可瞬间打满线程导致内存激增。在
# Handler.handle() 入口非阻塞抢槽，超限连接直接回 503；SSE 常驻连接
# 同样占用槽位（max_sse_conns=16 < max_http_threads=64，余量充足）。
HTTP_SLOT = threading.Semaphore(int(CFG.get("max_http_threads", 64)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        # 空闲超时：读不到下一个请求就关连接，防半开连接永久占线程
        self.timeout = float(CFG.get("socket_timeout", 300) or 300)
        super().setup()
        _enable_tcp_keepalive(self.connection)

    def handle(self):
        # v5.0.7: 连接级并发限流 —— 抢不到槽直接回 503，不进入业务处理。
        # v5.0.8: 503 改为直接写原始响应字节。原实现调 send_response()，
        # 但它依赖 parse_request() 初始化的 requestline/request_version
        # （handle() 阶段尚未解析请求，属性不存在）必然 AttributeError，
        # 被 except 吞掉后客户端只收到空响应/连接重置，503 从未真正发出。
        if not HTTP_SLOT.acquire(blocking=False):
            try:
                body = (b'{"error":"server busy - too many concurrent connections, '
                        b'retry later"}')
                self.wfile.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" + body)
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return
        try:
            super().handle()
        except (ConnectionError, socket.timeout, TimeoutError, OSError):
            # 客户端断开/超时属正常现象，静默收尾，别把 traceback 刷进日志
            self.close_connection = True
        finally:
            HTTP_SLOT.release()

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        """默认**不发**任何 CORS 响应头。

        原实现对每个响应都回 Access-Control-Allow-Origin: * —— 在无鉴权时，
        你只要访问过任意一个被挂马的网页，该网页就能 fetch 本机 :3100 执行
        命令**并读到返回值**。MCP 客户端是程序不是浏览器，本就不需要 CORS；
        确需跨域时在配置里填 cors_allow_origins，按 Origin 精确回显。
        """
        allow = CFG.get("cors_allow_origins") or []
        if not allow:
            return
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin in allow:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, Mcp-Session-Id, X-Fleet-Token")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS,DELETE")

    def _guard(self, body=b""):
        """鉴权闸门。返回 True 表示已拦截（响应也发完了）。

        v5.2 顺序：IP 白名单 → Dashboard 静态页放行 → Ed25519 签名 →
        require_signature → 旧 token 兼容。签名请求通过后 `_trusted=True`。
        """
        # Dashboard 静态页必须免鉴权：token 走 URL fragment（不会发送到服务器），
        # 否则浏览器首次 GET /fleet/ui 会被 401，页面根本加载不出来。
        # 数据接口 /fleet/ui/data 仍然要求 token/签名。
        if (self.command == "GET"
                and _request_path(self.path) == "/fleet/ui"):
            self._trusted = False
            self._peer_node = ""
            return False
        ip = self.client_address[0] if self.client_address else "?"
        self._trusted = False
        self._peer_node = ""
        if not _ip_allowed(ip):
            _note_auth_fail(ip)
            return self._deny(403, "来源 IP %s 不在 allow_ips 白名单内" % ip)

        sig = (self.headers.get("X-Fleet-Sig") or "").strip()
        if sig:
            if not (_HAS_CORE and REPLAY_GUARD):
                return self._deny(401, "服务端未启用 v5.2 签名校验")
            node_id = (self.headers.get("X-Fleet-Node") or "").strip()
            pub = _peer_public_key(node_id)
            if not pub:
                _note_auth_fail(ip)
                return self._deny(401, "未知节点 %s：未配对或公钥缺失" % (node_id or "?"))
            ok, why = _core.verify_request(
                pub, self.command, _request_path(self.path), body or b"",
                dict(self.headers), REPLAY_GUARD,
                max_skew_s=int(CFG.get("signature_max_skew_s", 300)))
            if ok:
                AUTH_FAILS.pop(ip, None)
                self._trusted = True
                self._peer_node = node_id
                return False
            _note_auth_fail(ip)
            return self._deny(401, "请求签名校验失败: %s" % why)

        if CFG.get("require_signature"):
            return self._deny(401, "已开启 require_signature：所有请求必须带 v5.2 签名")

        denied = check_access(ip, self.headers)
        if denied is None:
            # 只有「配了 token 且校验通过」才算可信；裸奔模式下不算，
            # 状态页据此收敛输出，避免向未认证者暴露指纹。
            self._trusted = bool(CFG.get("auth_token") or "")
            return False
        code, why = denied
        return self._deny(code, why)

    def _deny(self, code, why):
        """统一的鉴权失败响应（不泄露内部细节）。"""
        body = json.dumps({"error": why,
                           "hint": "v5.2 签名见 references/security-model.md；"
                                   "token 配置见 mcp_agent_config.json"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if code == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="mcp-fleet"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, code, obj, sid=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_OPTIONS(self):
        self._empty(204)

    def do_POST(self):
        try:
            _REQ.hops = max(0, int(self.headers.get("X-Fleet-Hops") or 0))
        except ValueError:
            _REQ.hops = 0
        path = self.path.split("?")[0].rstrip("/")
        # 边界校验：length 原来直接取自请求头且无上限 —— -1 会一直读到 EOF，
        # 超大值会一次性吃满内存，单连接即可长时间占住一个线程。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        limit = int(CFG.get("max_body_bytes", 1048576))
        if length < 0 or length > limit:
            self._json(413, {"error": "Content-Length 非法或超过上限 %d 字节" % limit})
            return
        raw = self.rfile.read(length) if length else b""

        # v5.2: 签名校验需要 body，因此放在读取之后（body 已受上限保护）
        if self._guard(raw):
            return

        if path == "/fleet/peer_remove":
            # v5.2: Dashboard 设备管理（需 token/签名；裸奔时仅允许本机回环）
            ip = self.client_address[0] if self.client_address else ""
            _loop = ip in ("127.0.0.1", "::1", "")
            if not (getattr(self, "_trusted", False) or _loop):
                self._json(403, {"error": "需要鉴权后才能移除节点"})
                return
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            pname = str(payload.get("name") or "").strip()
            if not pname:
                self._json(400, {"error": "missing name"})
                return
            with PEERS_LOCK:
                d = load_peers()
                removed = d["nodes"].pop(pname, None)
                if removed:
                    save_peers(d)
            _audit_call("peer_remove", {"name": pname}, "ok" if removed else "not_found")
            self._json(200, {"ok": bool(removed), "name": pname})
            return

        if path == "/messages":
            sid = self.path.split("sessionId=")[-1] if "sessionId=" in self.path else None
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                msg = None
            resp = handle_rpc(msg) if msg else None
            if sid and sid in SESSIONS and resp:
                try:
                    SESSIONS[sid].append(resp)
                except Exception:
                    pass
            self._empty(202)
            return

        # ---- v4.1: 自研通讯协议端点 ----
        if path == "/fleet/mail":
            """接收来自其他节点的消息，存入本地 inbox。"""
            # v5.0.3: 入站限流（独立令牌桶）+ inbox 容量硬上限——
            # 配对的失控节点/token 泄露也无法用消息 flooding 耗尽磁盘
            if not rate_limit_mail():
                self._json(429, {"error": "mail rate limited"})
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            if _mail_engine:
                max_inbox = int(CFG.get("max_inbox_msgs", 5000))
                if _mail_engine.stats()["inbox_total"] >= max_inbox:
                    self._json(507, {"error": "inbox full (%d)" % max_inbox})
                    return
                msg = {
                    "from": payload.get("from", ""),
                    "to": payload.get("to", ""),
                    "text": payload.get("text", ""),
                    "ts": payload.get("ts", time.strftime("%Y-%m-%d %H:%M:%S")),
                }
                if payload.get("topic"):
                    msg["topic"] = payload["topic"]
                if payload.get("msg_id"):
                    msg["msg_id"] = payload["msg_id"]
                if payload.get("source_node"):
                    msg["source_node"] = payload["source_node"]
                _mail_engine.inbox_append(msg)
                self._json(200, {"ok": True, "id": msg.get("id")})
            else:
                self._json(503, {"error": "proto not initialized"})
            return

        if path == "/fleet/discover":
            """节点握手：交换信息，自动互加 peer。"""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            peer_name = payload.get("name", "")
            peer_url = payload.get("url", "")
            if peer_name and peer_url:
                try:
                    with PEERS_LOCK:
                        d = load_peers()
                        d["nodes"][peer_name] = {"url": peer_url, "token": "",
                                                "added_by": "discover", "updated": int(time.time())}
                        save_peers(d)
                except Exception:
                    pass
            self._json(200, {"name": SERVER_NAME, "url": self_url(),
                            "version": SERVER_VERSION, "ok": True})
            return

        if path == "/fleet/sync":
            """邮箱同步 gossip：对方告诉我它的 agent，我返回我的摘要。"""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {}
            if _mail_engine:
                summary = _mail_engine.sync_summary()
                self._json(200, summary)
            else:
                self._json(503, {"error": "proto not initialized"})
            return

        if path == "/fleet/register_agent":
            """agent 注册到本节点。"""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            agent_name = payload.get("name", "")
            if agent_name and _mail_engine:
                _mail_engine.register_agent(agent_name)
                self._json(200, {"ok": True, "agent": agent_name})
            else:
                self._json(400, {"error": "missing name"})
            return

        if path == "/fleet/pair":
            # v4.3: 双向配对端点
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            peer_name = payload.get("name", "")
            peer_url = payload.get("url", "")
            peer_token = payload.get("token", "")
            peer_pub = payload.get("public_key", "")
            peer_nid = payload.get("node_id", "")
            peer_fp = payload.get("fingerprint", "")
            if not peer_name or not peer_url:
                self._json(400, {"error": "need name and url"})
                return
            # 本机 peer_add 对方（v5.2: 一并保存公钥，后续请求走签名）
            with PEERS_LOCK:
                d = load_peers()
                d["nodes"][peer_name] = {"url": peer_url, "token": peer_token,
                                         "added_by": "pair", "updated": int(time.time()),
                                         "public_key": peer_pub, "node_id": peer_nid,
                                         "fingerprint": peer_fp}
                save_peers(d)
            # 回传本机信息（公钥是公开信息；token 仅兼容 v5）
            self._json(200, {
                "ok": True,
                "my_name": SERVER_NAME,
                "my_url": self_url(),
                "my_token": CFG.get("auth_token", ""),
                "my_public_key": IDENTITY.public_key_b64 if IDENTITY else "",
                "my_node_id": IDENTITY.node_id if IDENTITY else "",
                "my_fingerprint": IDENTITY.fingerprint if IDENTITY else "",
                "my_version": SERVER_VERSION,
                "message": "已登记 %s" % peer_name
            })
            return

        if path == "/fleet/invite":
            # v4.4: 创建邀请码
            # 邀请码 = base64(json{code, url, token, invite_id, expires})
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                payload = {}
            ttl = int(payload.get("ttl", 3600))  # 默认 1 小时过期
            invite_id = uuid.uuid4().hex[:12]
            expires = int(time.time()) + ttl
            # 存储邀请记录（用于验证）——原子写，手机被杀进程也不会留半截文件
            with INVITE_LOCK:
                invites = _load_invites()
                invites[invite_id] = {"expires": expires, "used": False,
                                      "created": int(time.time())}
                try:
                    _save_invites_atomic(invites)
                except Exception as e:
                    self._json(500, {"error": "邀请码落盘失败：%s" % e})
                    return
            # 生成邀请码
            invite_data = {
                "code": SERVER_NAME,
                "url": self_url(),
                "token": CFG.get("auth_token", ""),
                "invite_id": invite_id,
                "expires": expires
            }
            import base64 as _b64
            invite_code = _b64.b64encode(
                json.dumps(invite_data).encode()).decode()
            self._json(200, {
                "ok": True,
                "invite_code": invite_code,
                "device_code": SERVER_NAME,
                "url": self_url(),
                "expires_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(expires)),
                "ttl": ttl,
                "install_cmd": "bash install.sh --invite %s" % invite_code
            })
            return

        if path == "/fleet/invite/verify":
            # v4.4: 验证邀请码 + 自动双向配对（并发安全，见 _verify_invite）
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            code, body = _verify_invite(payload)
            self._json(code, body)
            return

        if path not in ("/mcp", "/rpc", ""):
            self._json(404, {"error": "not found"})
            return

        try:
            msg = json.loads(raw.decode("utf-8")) if raw else None
        except Exception as e:
            self._json(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error: %s" % e}})
            return

        if isinstance(msg, dict) and msg.get("id") is None:
            handle_rpc(msg)
            self._empty(202)
            return

        resp = handle_rpc(msg)
        if resp is None:
            self._empty(202)
            return
        sid = self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex
        self._json(200, resp, sid=sid)

    def do_GET(self):
        global SSE_ACTIVE
        if self._guard(b""):
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health", "/status"):
            # 探活本身要留（负载均衡/监控要用），但**不向未认证者** dump
            # 工具清单 / 运行身份 / 是否开鉴权 —— 原输出等于告诉扫描器
            # 「这里有个不带锁的 root shell」。
            body = {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION}
            if getattr(self, "_trusted", False):
                body.update({
                    "tools": len(TOOLS), "tool_names": [t["name"] for t in TOOLS],
                    "tool_prefix": PREFIX, "root": _is_root(),
                    "auth": bool(CFG.get("auth_token") or ""),
                    "allow_ips": CFG.get("allow_ips") or [],
                    "transports": ["POST /mcp (streamable-http)", "GET /sse (sse)"],
                    # 让主端能直接看出「登记地址 vs 实际地址」是否漂移：
                    # 手机/NAS 换网络后 tailnet IP 会变，没有这个字段就只能靠猜。
                    "tailscale_ip": _tailscale_ip(),
                    "self_url": self_url(),
                    "uptime_secs": int(time.time() - _STARTED_AT),
                })
            self._json(200, body)
            return
        if path == "/sse":
            with SSE_LOCK:
                if SSE_ACTIVE >= int(CFG.get("max_sse_conns", 16)):
                    self._json(429, {"error": "SSE 并发连接数已达上限，请稍后重试"})
                    return
                SSE_ACTIVE += 1
            sid = uuid.uuid4().hex
            SESSIONS[sid] = []
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            self.wfile.write(("event: endpoint\ndata: /messages?sessionId=%s\n\n" % sid).encode("utf-8"))
            self.wfile.flush()
            try:
                idle = 0
                while True:
                    q = SESSIONS.get(sid, [])
                    # 发一条删一条：原来按下标读、从不清理，长连接内存只增不减
                    while q:
                        payload = json.dumps(q.pop(0), ensure_ascii=False)
                        self.wfile.write(("event: message\ndata: %s\n\n" % payload).encode("utf-8"))
                        self.wfile.flush()
                    time.sleep(0.2)
                    idle += 1
                    # v5.0.8: keepalive 周期 20s -> 5s。客户端断开后 SSE
                    # 线程要等下一次 keepalive 写失败才能退出、归还
                    # HTTP_SLOT；20s 周期意味着断开的连接最长占槽 20s
                    # （v5.0.7 动态验证实测），缩到 5s 释放更及时。
                    if idle % 25 == 0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                SESSIONS.pop(sid, None)
                with SSE_LOCK:
                    SSE_ACTIVE -= 1
            return
        # v4.1: poll 端点 -- 上线收信
        if path == "/fleet/poll":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            name = (qs.get("name", [""])[0]).strip()
            if not name:
                self._json(400, {"error": "missing name param"})
                return
            if _mail_engine:
                msgs = _mail_engine.poll_for_agent(name)
                self._json(200, {"messages": msgs, "count": len(msgs)})
            else:
                self._json(503, {"error": "proto not initialized"})
            return

        # ---- v5.0: Web Dashboard ----
        if path == "/fleet/ui":
            if _HAS_DASHBOARD:
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(503, {"error": "dashboard_html.py missing"})
            return

        if path == "/fleet/ui/data":
            self._json(200, _dashboard_data())
            return

        if path == "/fleet/health":
            mb = _mail_engine.stats() if _mail_engine else {}
            self._json(200, {
                "status": "ok", "name": SERVER_NAME, "version": SERVER_VERSION,
                "node_id": IDENTITY.node_id if IDENTITY else "",
                "fingerprint": IDENTITY.fingerprint if IDENTITY else "",
                "uptime_s": int(time.time() - PROCESS_START),
                "peers": len(load_peers().get("nodes", {})),
                "inbox": mb.get("inbox_total"), "outbox": mb.get("outbox_pending"),
                "read_only": bool(CFG.get("read_only")),
                "require_signature": bool(CFG.get("require_signature")),
            })
            return

        if path == "/fleet/identity":
            self._json(200, {
                "name": SERVER_NAME, "version": SERVER_VERSION,
                "node_id": IDENTITY.node_id if IDENTITY else "",
                "fingerprint": IDENTITY.fingerprint if IDENTITY else "",
                "public_key": IDENTITY.public_key_b64 if IDENTITY else "",
                "trust": "S2" if IDENTITY else ("S1" if CFG.get("auth_token") else "S0"),
                "relay_enabled": bool(CFG.get("relay_enabled")),
            })
            return

        if path == "/fleet/metrics":
            if METRICS is None:
                self._json(503, {"error": "metrics unavailable"})
                return
            text = METRICS.render_prometheus()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/fleet/tasks":
            tasks = []
            if TASK_STORE is not None:
                try:
                    tasks = TASK_STORE.list_tasks()[-50:]
                except Exception:
                    tasks = []
            self._json(200, {"tasks": tasks, "count": len(tasks)})
            return

        if path == "/fleet/status":
            if _mail_engine:
                self._json(200, _mail_engine.stats())
            else:
                self._json(200, {"proto": False})
            return

        if path in ("/mcp", "/rpc"):
            # MCP 规范：服务端不提供 GET 流时必须回 405。原来回 404，而 404 在规范里
            # 表示「会话已失效」——部分客户端（含带 Mcp-Session-Id 的重连）据此
            # 判定掉线、反复重新 initialize，表现为副端时好时坏。
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE, OPTIONS")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self._guard(b""):
            return
        self._empty(204)


def main():
    ap = argparse.ArgumentParser(description="MCP 副端 Agent（零依赖）")
    # 默认只绑本机：要对外服务必须显式写 --host 0.0.0.0。
    # 原默认即 0.0.0.0，一次手滑启动就等于全网卡裸奔。
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3100)
    ap.add_argument("--require-auth", action="store_true",
                    help="未配置 auth_token 时拒绝启动（公网/隧道场景建议开启）")
    ap.add_argument("--generate-token", action="store_true",
                    help="未配置 auth_token 时自动生成一个并写回配置文件（权限 0600）")
    ap.add_argument("--allow-insecure", action="store_true",
                    help="非 loopback 监听且无 token 时也允许启动（不推荐）")
    ap.add_argument("--no-auto-secure", action="store_true",
                    help="关闭“非 loopback 自动生成 token”的安全默认")
    ap.add_argument("--tls-cert", default=None,
                    help="TLS 证书文件路径（PEM 格式），启用 HTTPS")
    ap.add_argument("--tls-key", default=None,
                    help="TLS 私钥文件路径（PEM 格式），启用 HTTPS")
    ap.add_argument("--read-only", action="store_true",
                    help="只读模式：exec/write/edit/systemctl/docker_restart 等"
                         "变更类工具全部禁用（审计观察节点）")
    args = ap.parse_args()

    # 配置损坏就直接停：带病上岗（无 token 监听）比不启动危险得多。
    die_if_config_corrupt()

    # ---- 鉴权前置校验（v3.1 / v5.2 安全默认）------------------------------
    _loopback = args.host in ("127.0.0.1", "localhost", "::1", "")
    _auto_created = False
    if (not _loopback and not (CFG.get("auth_token") or "")
            and not args.allow_insecure and not args.no_auto_secure
            and CFG.get("auto_secure", True)):
        # 安全默认：对外监听必须鉴权，否则任何邻居都能拿到本机 shell。
        CFG["auth_token"] = gen_token()
        if save_config(CFG):
            _auto_created = True
            print("  [ok] 检测到对外监听且未配置 token：已自动生成并写入 %s（0600）"
                  % CONFIG_PATH)
            print("  [ok] token 不在此处回显；安装器会打印一次，或查看配置文件。")
        else:
            print("  [!] 自动生成 token 后写配置失败，仍以无鉴权启动（不推荐）",
                  file=sys.stderr)
    if (not _loopback and not (CFG.get("auth_token") or "")
            and (args.allow_insecure or args.no_auto_secure)):
        print("  [!] 警告：对外监听且未启用鉴权（--allow-insecure/--no-auto-secure）。",
              file=sys.stderr)

    if args.generate_token and not (CFG.get("auth_token") or ""):
        CFG["auth_token"] = gen_token()
        if save_config(CFG):
            print("  [ok] 已生成随机 auth_token 并写入 %s（权限 0600）" % CONFIG_PATH)
            print("  [ok] 令牌: %s" % CFG["auth_token"])
            print("  [ok] 请填入主端 mcp.json 的 headers：Authorization: Bearer <令牌>")
        else:
            print("  [!] 生成令牌后写配置失败，本次仍以无鉴权启动", file=sys.stderr)
    if (args.require_auth or CFG.get("require_auth")) and not (CFG.get("auth_token") or ""):
        print("[FATAL] 已要求鉴权但 auth_token 为空，拒绝启动。", file=sys.stderr)
        print("        先执行：python3 mcp_agent.py --generate-token --require-auth",
              file=sys.stderr)
        sys.exit(2)

    # v5.0.9 修复：原来缺 _discovery_layer 的 global 声明，此处赋值只创建了
    # 局部变量，模块级引用（_dashboard_data 读取）永远是 None，导致
    # Dashboard 各节点的 last_seen 自 v4.0 起一直为空。
    global LISTEN_PORT, _discovery_layer
    LISTEN_PORT = args.port
    # v5.0.7: --read-only 运行时开启只读模式（不写回配置文件）
    if args.read_only:
        CFG["read_only"] = True
    httpd = FleetHTTPServer((args.host, args.port), Handler)
    # P1-1: TLS 支持
    if args.tls_cert and args.tls_key:
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print("  TLS             : 已启用（证书: %s）" % args.tls_cert)
        _mcp_url = "https://%s:%d/mcp" % (args.host, args.port)
    else:
        _mcp_url = "http://%s:%d/mcp" % (args.host, args.port)
        if not _loopback:
            print("  [!] 当前为明文 HTTP 对外监听：公网/跨网段建议 --tls 或 Tailscale；"
                  "应用层签名与邮箱加密仍生效。")
    # v4.3: 醒目的设备码 + API + 配对命令
    _bar = "═" * 60
    print("╔%s╗" % _bar)
    print("║  MCP Fleet 节点已启动 v%-47s║" % (SERVER_VERSION,))
    print("║%-60s║" % "")
    print("║  设备码 : %-50s║" % SERVER_NAME)
    print("║  API    : %-50s║" % _mcp_url)
    print("║%-60s║" % "")
    print("║  在其他设备上运行以下命令配对：                       ║")
    print("║  fleet.py pair %s@%s" % (SERVER_NAME, _mcp_url) + " " * max(1, 60 - 18 - len(SERVER_NAME) - len(_mcp_url)) + "║")
    print("╚%s╝" % _bar)
    print("  工具数          : %d (%s)" % (len(TOOLS), ", ".join(t["name"] for t in TOOLS[:4]) + " ..."))
    print("  工作目录        : %s" % CFG.get("work_dir"))
    print("  运行身份        : %s" % ("root" if _is_root() else "非 root"))

    tok = CFG.get("auth_token") or ""
    nets = CFG.get("allow_ips") or []
    if tok:
        print("  鉴权            : 已开启（Bearer token，%d 字符%s）"
              % (len(tok), "，自动生成" if _auto_created else ""))
    else:
        print("  鉴权            : [!] 未开启")
        print("  " + "!" * 68)
        print("  [!] 未设置 auth_token：任何能访问本端口的人都等于拿到本机 shell。")
        print("  [!] 仅限内网使用；一旦开公网隧道（mcp-tunnel.sh），请务必先设置：")
        print("  [!]   python3 -c \"import json;p='%s';d=json.load(open(p));"
              "d['auth_token']='<强随机串>';json.dump(d,open(p,'w'),ensure_ascii=False)\"" % CONFIG_PATH)
        print("  [!] 或直接跑 mcp-tunnel.sh，它会自动生成并写入。")
        print("  " + "!" * 68)
    if nets:
        print("  来源白名单      : %s" % ", ".join(str(n) for n in nets))
    # ---- v4.1: 自研通讯协议启动 ----
    _init_proto()
    if _HAS_PROTO and _mail_engine:
        print("  通讯协议        : fleet-proto v%d (store-and-forward)" % PROTO_VERSION)
        # UDP 发现层
        caps_summary = {"docker": CFG.get("enable_docker", True),
                        "tools": len(TOOLS),
                        "connection_mode": CFG.get("connection_mode", "permanent"),
                        "control_mode": CFG.get("control_mode", "direct"),
                        "ttl": int(CFG.get("ttl", 0))}
        _discovery_layer = DiscoveryLayer(
            SERVER_NAME, args.port, caps=caps_summary,
            discover_port=int(CFG.get("discover_port", 3199)),
            on_discover=_on_discover,
            sign_secret=CFG.get("auth_token", "") or None,
            verify_token_func=lambda n: load_peers().get("nodes", {}).get(n, {}).get("token", "") or None)
        _discovery_layer.start()
        print("  UDP 发现        : 端口 %d (每 %ds 广播)" % (
            int(CFG.get("discover_port", 3199)), 30))
        if CFG.get("auth_token", ""):
            print("  hello 签名      : 开启 (HMAC-SHA256, 已配对节点验签防伪造)")
            print("  邮箱加密        : 开启 (PBKDF2+HMAC-SHA256 流加密, 密文落盘)")
        # 消息投递 daemon
        if _delivery_engine:
            _delivery_engine.start_delivery()
            print("  消息投递        : 后台 daemon (每 %ds 重试 outbox)" % 15)
        # 邮箱同步 daemon
        if _sync_engine:
            _sync_engine.start_sync()
            print("  邮箱同步        : 后台 daemon (每 %ds gossip)" % 60)
    else:
        print("  通讯协议        : 旧版邮箱（fleet_proto 不可用）")

    # ---- v4.0: Tailscale 自动组网（可选增强）----
    if CFG.get("tailnet_auto_mesh", True):
        ts_ip = _tailscale_ip()
        if ts_ip:
            print("  Tailscale       : 在线 (%s)" % ts_ip)
            discovered = _auto_discover_peers(args.port)
            if discovered:
                print("  自动发现        : %d 个 fleet 节点 -> %s" %
                      (len(discovered), ", ".join(n for n, _ in discovered)))
            else:
                print("  自动发现        : tailnet 上暂无其他 fleet 节点")
            # 启动后台线程：定期扫描 + 上线广播
            def _mesh_daemon():
                _broadcast_join()
                while True:
                    time.sleep(int(CFG.get("tailnet_scan_interval", 60)))
                    try:
                        _auto_discover_peers(args.port)
                    except Exception:
                        pass
            t = threading.Thread(target=_mesh_daemon, daemon=True)
            t.start()
        else:
            print("  Tailscale       : 未安装或未登录（--tailnet 安装时自动配置）")

    # ---- v4.2: 连接模式 + 控制模式 ----
    conn_mode = CFG.get("connection_mode", "permanent")
    ctrl_mode = CFG.get("control_mode", "direct")
    ttl = int(CFG.get("ttl", 0))
    print("  连接模式        : %s%s" % (
        conn_mode, " (TTL=%ds)" % ttl if conn_mode == "temporary" and ttl else ""))
    print("  控制模式        : %s%s" % (
        ctrl_mode, " [远程 agent 只能用安全工具+request_assist]" if ctrl_mode == "assisted" else ""))

    # 临时模式 TTL 倒计时
    global _ttl_deadline
    if conn_mode == "temporary" and ttl > 0:
        _ttl_deadline = time.time() + ttl
        def _ttl_timer():
            while True:
                remaining = _ttl_deadline - time.time()
                if remaining <= 0:
                    print("\n[TTL] 临时连接已到期 (%ds)，正在退出..." % ttl)
                    _send_goodbye()
                    os._exit(0)
                time.sleep(min(30, remaining))
        threading.Thread(target=_ttl_timer, daemon=True).start()
        print("  TTL 倒计时      : %ds 后自动退出" % ttl)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        if conn_mode == "temporary":
            print("[临时模式] 发送 goodbye 通知...")
            _send_goodbye()
    except Exception as e:
        # 主循环意外退出：以非零码退出，交给 systemd Restart=always / 守护壳重启
        print("[FATAL] 服务主循环异常退出: %r" % (e,), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
