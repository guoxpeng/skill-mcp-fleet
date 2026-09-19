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
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    "version": "3.1.0",
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
    # 单请求体上限（字节），防止 Content-Length 撑爆内存/线程。
    "max_body_bytes": 1048576,       # 1 MB
    # SSE 并发连接上限，防止线程被常驻连接打满。
    "max_sse_conns": 16,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("[warn] 读配置失败，用默认值: %s" % e, file=sys.stderr)
    return cfg


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
SERVER_VERSION = CFG.get("version", "1.0.0")
PREFIX = CFG.get("tool_prefix", "") or ""


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
    raise ValueError("路径不在允许范围内: %s" % ap)


def _which(cmd):
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        f = os.path.join(d, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return None


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------
def _wrap(cmd, sudo=False):
    """把命令包成最终交给 bash 执行的字符串。全部用字符串拼接，避免 % 吃掉花括号。"""
    flag = "-lc" if CFG.get("login_shell", True) else "-c"
    if not sudo or _is_root():
        return "bash " + flag + " " + shlex.quote(cmd)
    pw = CFG.get("sudo_password") or ""
    if pw:
        return ("printf '%s\\n' " + shlex.quote(pw)
                + " | sudo -S bash " + flag + " " + shlex.quote(cmd))
    # 没配密码：尝试免密 sudo（-n），失败会明确报错，不会卡住
    return "sudo -n bash " + flag + " " + shlex.quote(cmd)


def run_local(command, sudo=False, timeout=None, work_dir=None, stdin=None):
    """在设备本机执行命令，返回 (exit_code, stdout, stderr)。"""
    full = _wrap(command, sudo=sudo)
    t = timeout or CFG.get("command_timeout", 120)
    wd = work_dir or CFG.get("work_dir") or "/"
    try:
        p = subprocess.run(
            ["bash", "-c", full],
            cwd=wd if os.path.isdir(wd) else None,
            input=stdin.encode("utf-8") if stdin is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=t,
        )
        return (p.returncode,
                p.stdout.decode("utf-8", "ignore"),
                p.stderr.decode("utf-8", "ignore"))
    except subprocess.TimeoutExpired:
        return -1, "", "[超时 %ss 被终止]" % t
    except FileNotFoundError:
        return -1, "", "[执行失败] 找不到 bash，请确认设备已安装 bash"
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
    op = ">>" if a.get("mode", "overwrite") == "append" else ">"
    cmd = "cat " + op + " " + shlex.quote(p)
    code, out, err = run_local(cmd, sudo=bool(a.get("sudo")), stdin=a.get("content", ""))
    if code == 0:
        return "[exit 0] 写入成功: " + p, False
    return fmt_result(code, out, err), True


def t_edit(a):
    p = _check_path(a["filePath"])
    b_old = base64.b64encode(a.get("oldText", "").encode()).decode()
    b_new = base64.b64encode(a.get("newText", "").encode()).decode()
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
        "open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))\n"
        "print('OK')\n"
        "__MCP_EDIT_EOF__"
    )
    code, out, err = run_local(script, sudo=bool(a.get("sudo")))
    return fmt_result(code, out, err), code != 0


def t_list_dir(a):
    p = _check_path(a.get("path", "/"))
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
    ("edit", "替换文件中指定的文本块（oldText -> newText），无需整文件重写。",
     {"type": "object", "properties": {
         "filePath": {"type": "string"}, "oldText": {"type": "string"},
         "newText": {"type": "string"}, "sudo": {"type": "boolean"}},
         "required": ["filePath", "oldText", "newText"]}),
    ("list_dir", "列出目录内容。",
     {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
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
]

BASE_FUNCS = {
    "exec": t_exec, "read": t_read, "write": t_write, "edit": t_edit,
    "list_dir": t_list_dir, "docker_ps": t_docker_ps, "docker_logs": t_docker_logs,
    "docker_restart": t_docker_restart, "systemctl": t_systemctl,
    "service_logs": t_service_logs, "sysinfo": t_sysinfo, "http_get": t_http_get,
}

# 对外暴露的工具（含前缀）
TOOLS = []
for _base, _desc, _schema in BASE_TOOLS:
    TOOLS.append({"name": PREFIX + _base, "description": _desc, "inputSchema": _schema})

# 查找表：带前缀名、裸名、以及旧版 nas_ 前缀名都接受（向后兼容）
TOOL_FUNCS = {}
for _base, _fn in BASE_FUNCS.items():
    TOOL_FUNCS[PREFIX + _base] = _fn
    TOOL_FUNCS[_base] = _fn
    TOOL_FUNCS["nas_" + _base] = _fn


def call_tool(name, args):
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}
    try:
        text, is_err = fn(args or {})
        return {"content": [{"type": "text", "text": text}], "isError": bool(is_err)}
    except KeyError as e:
        return {"content": [{"type": "text", "text": "缺少参数: %s" % e}], "isError": True}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": "错误: %r" % (e,)}], "isError": True}


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
def handle_rpc(msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
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
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


SESSIONS = {}

# SSE 并发计数：每个 /sse 连接会常驻一个 while True 线程，无上限时可被打满。
SSE_LOCK = threading.Lock()
SSE_ACTIVE = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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

    def _guard(self):
        """鉴权闸门。返回 True 表示已拦截（响应也发完了）。"""
        ip = self.client_address[0] if self.client_address else "?"
        denied = check_access(ip, self.headers)
        if denied is None:
            # 只有「配了 token 且校验通过」才算可信；裸奔模式下不算，
            # 状态页据此收敛输出，避免向未认证者暴露指纹。
            self._trusted = bool(CFG.get("auth_token") or "")
            return False
        code, why = denied
        body = json.dumps({"error": why, "hint": "auth_token 配置见 mcp_agent_config.json"},
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
        if self._guard():
            return
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
        if self._guard():
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
                last = idle = 0
                while True:
                    q = SESSIONS.get(sid, [])
                    while last < len(q):
                        payload = json.dumps(q[last], ensure_ascii=False)
                        self.wfile.write(("event: message\ndata: %s\n\n" % payload).encode("utf-8"))
                        self.wfile.flush()
                        last += 1
                    time.sleep(0.2)
                    idle += 1
                    if idle % 100 == 0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                SESSIONS.pop(sid, None)
                with SSE_LOCK:
                    SSE_ACTIVE -= 1
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self._guard():
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
    args = ap.parse_args()

    # ---- 鉴权前置校验（v3.1）---------------------------------------------
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

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("MCP 副端 Agent 已启动: %s v%s" % (SERVER_NAME, SERVER_VERSION))
    print("  Streamable HTTP : http://%s:%d/mcp" % (args.host, args.port))
    print("  SSE             : http://%s:%d/sse" % (args.host, args.port))
    print("  状态页          : http://%s:%d/" % (args.host, args.port))
    print("  工具数          : %d (%s)" % (len(TOOLS), ", ".join(t["name"] for t in TOOLS[:4]) + " ..."))
    print("  工作目录        : %s" % CFG.get("work_dir"))
    print("  运行身份        : %s" % ("root" if _is_root() else "非 root"))

    tok = CFG.get("auth_token") or ""
    nets = CFG.get("allow_ips") or []
    if tok:
        print("  鉴权            : 已开启（Bearer token，%d 字符）" % len(tok))
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
