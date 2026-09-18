#!/usr/bin/env bash
# ============================================================================
#  MCP 副端 Agent · 一键安装脚本（自包含，内嵌服务器代码）
# ----------------------------------------------------------------------------
#  在任意 Linux 设备上执行即可：
#      sudo bash install.sh --name fnos --port 3100
#  或直接管道安装：
#      curl -fsSL <URL> | sudo bash -s -- --name fnos --port 3100
#
#  参数（都可省略）：
#      --name    <标识>    副端名字，用于服务名与显示（默认取主机名）
#      --port    <端口>    监听端口（默认 3100）
#      --dir     <目录>    安装目录（默认 /opt/<name>_mcp）
#      --prefix  <前缀>    工具名前缀（默认空，工具名即 exec/read/...）
#      --sudo-pass <密码>  可选：写入配置，供 sudo 提权使用
#      --mode    <模式>    auto(默认) | systemd | nohup
#      --tunnel           装完顺便起 cloudflared 快速隧道（云容器/无公网入口时用）
#      --uninstall         卸载（停服务 + 删单元 + 删目录）
#
#  依赖：python3（只用标准库，无需 pip）。
#        systemd 有则用服务托管；没有（Docker/PAI-DSW 等容器）自动降级为
#        守护进程 + mcp-ctl.sh 控制脚本，功能一致，只是开机不自启。
# ============================================================================
set -euo pipefail

NAME=""
PORT="3100"
DIR=""
PREFIX=""
SUDO_PASS=""
UNINSTALL="0"
MODE="auto"
DO_TUNNEL="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --name)       NAME="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --prefix)     PREFIX="${2:-}"; shift 2 ;;
    --sudo-pass)  SUDO_PASS="${2:-}"; shift 2 ;;
    --mode)       MODE="${2:-}"; shift 2 ;;
    --tunnel)     DO_TUNNEL="1"; shift ;;
    --uninstall)  UNINSTALL="1"; shift ;;
    -h|--help)    sed -n '2,30p' "$0" 2>/dev/null || echo "(管道方式不支持 --help，请见 README)"; exit 0 ;;
    *) echo "[!] 未知参数: $1"; exit 1 ;;
  esac
done

# ---------- 基本信息 ----------
if [ -z "$NAME" ]; then
  NAME="$(hostname 2>/dev/null | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '-' | sed 's/-\+$//')"
  [ -z "$NAME" ] && NAME="node"
fi
if [ -z "$DIR" ]; then DIR="/opt/${NAME}_mcp"; fi
UNIT="${NAME}-mcp"

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 权限 ----------
if [ "$(id -u)" -ne 0 ]; then
  if [ -r "$0" ] && command -v sudo >/dev/null 2>&1; then
    echo "[i] 需要 root，自动用 sudo 重新执行"
    exec sudo -E bash "$0" "$@"
  elif [ ! -r "$0" ]; then
    die "检测到管道方式执行（curl | bash）且当前非 root。
    请改用：curl -fsSL <URL> | sudo bash -s -- --name <名字> --port 3100
    或先下载再装：curl -fsSL <URL> -o install.sh && sudo bash install.sh --name <名字> --port 3100"
  else
    die "请用 root 运行：sudo bash $0 --name <名字> --port 3100"
  fi
fi

# ---------- systemd 真实性探测（关键修复）----------
# 容器里往往装了 systemctl 二进制，但 PID1 不是 systemd，此时 systemctl 必定报
# "System has not been booted with systemd as init system"。必须看 /run/systemd/system。
SYSTEMD_OK="0"
if command -v systemctl >/dev/null 2>&1 \
   && [ -d /run/systemd/system ] \
   && [ "$(cat /proc/1/comm 2>/dev/null | tr -d '[:space:]')" = "systemd" ]; then
  SYSTEMD_OK="1"
fi

if [ "$MODE" = "auto" ]; then
  if [ "$SYSTEMD_OK" = "1" ]; then MODE="systemd"; else MODE="nohup"; fi
fi
if [ "$MODE" = "systemd" ] && [ "$SYSTEMD_OK" != "1" ]; then
  warn "要求 systemd 模式，但当前 PID1 是 $(cat /proc/1/comm 2>/dev/null || echo unknown)，不是 systemd"
  warn "（Docker / PAI-DSW 等容器很常见），自动降级为 nohup 守护模式"
  MODE="nohup"
fi

# ---------- 卸载 ----------
if [ "$UNINSTALL" = "1" ]; then
  log "卸载 $UNIT ..."
  if [ -f "/etc/systemd/system/${UNIT}.service" ]; then
    [ "$SYSTEMD_OK" = "1" ] && systemctl disable --now "$UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/${UNIT}.service"
    [ "$SYSTEMD_OK" = "1" ] && systemctl daemon-reload 2>/dev/null || true
  fi
  if [ -f "$DIR/tunnel.pid" ]; then kill "$(cat "$DIR/tunnel.pid")" 2>/dev/null || true; fi
  pkill -f "$DIR/cloudflared" 2>/dev/null || true
  if [ -f "$DIR/agent.pid" ]; then kill -TERM -"$(cat "$DIR/agent.pid")" 2>/dev/null || kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true; fi
  pkill -f "$DIR/_supervisor.sh" 2>/dev/null || true
  pkill -f "$DIR/mcp_agent.py" 2>/dev/null || true
  if [ -x "$DIR/_portkill.py" ] || [ -f "$DIR/_portkill.py" ]; then
    command -v python3 >/dev/null 2>&1 && python3 "$DIR/_portkill.py" "$PORT" >/dev/null 2>&1 || true
  fi
  rm -rf "$DIR"
  log "已卸载（目录 $DIR 已删除）"
  exit 0
fi

# ---------- 依赖检查 ----------
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装：apt install -y python3（或 yum/dnf install -y python3）"
PY3="$(command -v python3)"
PYVER="$($PY3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
log "python3: $PY3 (v$PYVER)"
command -v bash >/dev/null 2>&1 || warn "未找到 bash，exec 工具会失败"
log "运行模式: $MODE（PID1=$(cat /proc/1/comm 2>/dev/null || echo unknown)）"

# ---------- 落盘 ----------
log "安装目录: $DIR"
mkdir -p "$DIR"
printf '%s\n' "$PORT" > "$DIR/port.txt"

cat > "$DIR/mcp_agent.py" <<'MCP_AGENT_PY_EOF'
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
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mcp_agent_config.json")

DEFAULT_CONFIG = {
    "name": "node",             # 副端标识（显示在状态页与 serverInfo）
    "tool_prefix": "",          # 工具名前缀，如 "nas_"；留空则用裸名（exec/read/...）
    "version": "1.0.0",
    "sudo_password": "",        # 留空且非 root 时会尝试免密 sudo
    "work_dir": "/",            # 默认工作目录
    "command_timeout": 120,     # 秒
    "max_output_bytes": 200000, # 单次输出上限，防止刷爆上下文
    "allowed_roots": ["/"],     # 文件读写允许的路径前缀（安全边界）
    "enable_docker": True,      # 无 Docker 的设备可关掉
    "login_shell": True,        # True=bash -lc（会 source /etc/profile）；
                                # 容器里 profile 打了欢迎横幅（PAI-DSW 等）时设 False，
                                # 改成 bash -c，输出干净且 PATH 通常够用
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


def _check_path(p):
    """安全边界：只允许 allowed_roots 下的路径。"""
    roots = CFG.get("allowed_roots") or ["/"]
    ap = os.path.abspath(p)
    for r in roots:
        if r == "/" or ap == r or ap.startswith(r.rstrip("/") + "/"):
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS,DELETE")

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
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
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
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health", "/status"):
            self._json(200, {
                "status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION,
                "tools": len(TOOLS), "tool_names": [t["name"] for t in TOOLS],
                "tool_prefix": PREFIX, "root": _is_root(),
                "transports": ["POST /mcp (streamable-http)", "GET /sse (sse)"],
            })
            return
        if path == "/sse":
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
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        self._empty(204)


def main():
    ap = argparse.ArgumentParser(description="MCP 副端 Agent（零依赖）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3100)
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("MCP 副端 Agent 已启动: %s v%s" % (SERVER_NAME, SERVER_VERSION))
    print("  Streamable HTTP : http://%s:%d/mcp" % (args.host, args.port))
    print("  SSE             : http://%s:%d/sse" % (args.host, args.port))
    print("  状态页          : http://%s:%d/" % (args.host, args.port))
    print("  工具数          : %d (%s)" % (len(TOOLS), ", ".join(t["name"] for t in TOOLS[:4]) + " ..."))
    print("  工作目录        : %s" % CFG.get("work_dir"))
    print("  运行身份        : %s" % ("root" if _is_root() else "非 root"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

MCP_AGENT_PY_EOF

# 配置文件（已存在则不覆盖，避免冲掉你的设置）
if [ -f "$DIR/mcp_agent_config.json" ]; then
  warn "配置文件已存在，保留原文件（如需重置请手动删除）"
  CFG_FILE="$DIR/mcp_agent_config.json"
else
  CFG_FILE="$DIR/mcp_agent_config.json"
  cat > "$CFG_FILE" <<CFG_EOF
{
  "name": "$NAME",
  "tool_prefix": "$PREFIX",
  "version": "1.0.0",
  "sudo_password": "$SUDO_PASS",
  "work_dir": "/",
  "command_timeout": 120,
  "max_output_bytes": 200000,
  "allowed_roots": ["/"],
  "enable_docker": true
}
CFG_EOF
fi

# 按端口清理僵尸进程（零依赖，替代可能不存在的 fuser）
cat > "$DIR/_portkill.py" <<'PORTKILL_PY_EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按端口号找到监听进程并优雅杀之（纯标准库，替代 fuser/lsof）。"""
import os
import signal
import sys
import time


def listening_pids(port):
    inodes = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            p = ln.split()
            if len(p) < 10:
                continue
            try:
                if int(p[1].split(":")[1], 16) != port:
                    continue
            except (ValueError, IndexError):
                continue
            if p[3] == "0A":            # TCP_LISTEN
                inodes.add(p[9])
    pids = []
    if not inodes:
        return pids
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fddir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue
        for fd in fds:
            try:
                tgt = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                pids.append(int(pid))
                break
    return pids


def main():
    if len(sys.argv) < 2:
        print("用法: _portkill.py <port> [--hard]")
        return 2
    port = int(sys.argv[1])
    hard = "--hard" in sys.argv
    me = os.getpid()
    pids = [p for p in listening_pids(port) if p != me]
    if not pids:
        print("端口 %d 空闲" % port)
        return 0
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL if hard else signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.8)
    left = [p for p in listening_pids(port) if p != me]
    for p in left:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    print("已清理端口 %d 上的进程: %s" % (port, ",".join(str(x) for x in pids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PORTKILL_PY_EOF

# 守护壳：agent 挂掉 3 秒后自动拉起（无 systemd 时的 Restart=always 等价物）
cat > "$DIR/_supervisor.sh" <<'SUP_SH_EOF'
#!/usr/bin/env bash
# MCP agent 守护壳（无 systemd 环境的自动重启）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
PY="${PY:-$(command -v python3 || echo /usr/bin/python3)}"
LOG="$DIR/agent.log"
# 由守护壳自己写 pid（比父 shell 记 $! 靠谱：setsid 可能 fork 过一层）
echo $$ > "$DIR/agent.pid"
echo "[$(date '+%F %T')] supervisor 启动 (pid=$$, port=$PORT, py=$PY)" >> "$LOG"
while true; do
  "$PY" "$DIR/mcp_agent.py" --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1
  code=$?
  echo "[$(date '+%F %T')] agent 退出 (code=$code)，3 秒后重启" >> "$LOG"
  sleep 3
done
SUP_SH_EOF

# 控制脚本（无 systemd 环境的手动开关，比记 nohup 命令稳）
cat > "$DIR/mcp-ctl.sh" <<'CTL_SH_EOF'
#!/usr/bin/env bash
# MCP 副端控制脚本： start | stop | restart | status | log | tunnel
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
PY="${PY:-$(command -v python3 || echo /usr/bin/python3)}"
PIDF="$DIR/agent.pid"
LOG="$DIR/agent.log"

running() {
  [ -f "$PIDF" ] || return 1
  local p; p="$(cat "$PIDF" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

start() {
  if running; then echo "已在运行 (pid $(cat "$PIDF"))"; return 0; fi
  "$PY" "$DIR/_portkill.py" "$PORT" >/dev/null 2>&1 || true
  rm -f "$PIDF"
  cd "$DIR"
  setsid nohup "$DIR/_supervisor.sh" >> "$LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 20); do
    running && break
    sleep 0.3
  done
  if running; then
    echo "已启动 (守护 pid $(cat "$PIDF"), 端口 $PORT)"
  else
    echo "启动失败，见 $LOG"; tail -n 15 "$LOG" 2>/dev/null; return 1
  fi
}

stop() {
  if [ -f "$PIDF" ]; then
    local p; p="$(cat "$PIDF" 2>/dev/null)"
    if [ -n "${p:-}" ]; then
      kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
    fi
    rm -f "$PIDF"
  fi
  pkill -f "$DIR/_supervisor.sh" 2>/dev/null || true
  pkill -f "$DIR/mcp_agent.py" 2>/dev/null || true
  "$PY" "$DIR/_portkill.py" "$PORT" >/dev/null 2>&1 || true
  echo "已停止"
}

status() {
  if running; then
    echo "运行中 (守护 pid $(cat "$PIDF"), 端口 $PORT)"
  else
    echo "未运行"
    return 1
  fi
  if command -v curl >/dev/null 2>&1; then
    echo "健康检查: $(curl -s -m 5 "http://127.0.0.1:$PORT/" || echo '(无响应)')"
  fi
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  log)     tail -n "${2:-40}" "$LOG" ;;
  tunnel)  shift; bash "$DIR/mcp-tunnel.sh" "$@" ;;
  *) echo "用法: bash $DIR/mcp-ctl.sh {start|stop|restart|status|log [n]|tunnel}"; exit 1 ;;
esac
CTL_SH_EOF

# 公网隧道：云容器（PAI-DSW/Colab 等）没有内网可达入口，必须借隧道把端口暴露出去
cat > "$DIR/mcp-tunnel.sh" <<'TUN_SH_EOF'
#!/usr/bin/env bash
# 用 cloudflared 快速隧道把本机 MCP 端口暴露到公网。
# 注意：trycloudflare.com 地址是临时的，重启会变；每次换地址都要更新主端 mcp.json。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
BIN="$DIR/cloudflared"
LOG="$DIR/tunnel.log"
PIDF="$DIR/tunnel.pid"
PY="$(command -v python3 || echo /usr/bin/python3)"
NAME="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json'))['name'])" 2>/dev/null || echo node)"

if [ ! -x "$BIN" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64)  CF="cloudflared-linux-amd64" ;;
    aarch64|arm64) CF="cloudflared-linux-arm64" ;;
    *) echo "[x] 不支持的 CPU 架构: $ARCH"; exit 1 ;;
  esac
  echo "[+] 下载 cloudflared ($CF) ..."
  ok=0
  for M in \
    "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
    "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" ; do
    if curl -fsSL --connect-timeout 20 -o "$BIN" "$M"; then ok=1; break; fi
    echo "[!] 通道失败，试下一个"
  done
  [ "$ok" = "1" ] || { echo "[x] cloudflared 下载失败，请手动放置到 $BIN"; exit 1; }
  chmod +x "$BIN"
fi

if [ -f "$PIDF" ]; then kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"; sleep 1; fi
: > "$LOG"
# 偶发 502 时可试：CF_PROTO=http2 bash mcp-tunnel.sh  （QUIC 被限速的网络下更稳）
PROTO_ARGS=""
[ -n "${CF_PROTO:-}" ] && PROTO_ARGS="--protocol ${CF_PROTO}"
# shellcheck disable=SC2086
setsid nohup "$BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate $PROTO_ARGS >> "$LOG" 2>&1 < /dev/null &
# 记真实 cloudflared pid：setsid 可能 fork，$! 未必是最终进程
sleep 1
REAL="$(pgrep -f "$BIN tunnel --url http://127.0.0.1:$PORT" 2>/dev/null | head -1 || true)"
if [ -n "$REAL" ]; then echo "$REAL" > "$PIDF"; else echo $! > "$PIDF"; fi

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1)"
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "[x] 未取到隧道地址，日志尾部："
  tail -n 20 "$LOG"
  exit 1
fi

# ---- 自检 1：域名是否解析 ----
HOST="${URL#https://}"
DNS_OK="0"
for _ in $(seq 1 15); do
  if getent hosts "$HOST" >/dev/null 2>&1 || $PY -c "import socket;socket.getaddrinfo('$HOST',443)" >/dev/null 2>&1; then
    DNS_OK="1"; break
  fi
  sleep 2
done

# ---- 自检 2：公网是否真的能访问到本机 agent ----
HTTP_CODE=""
if command -v curl >/dev/null 2>&1; then
  HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$URL/" 2>/dev/null || true)"
fi

echo
echo "============================================================"
echo " 公网隧道地址：$URL"
echo " MCP 端点    ：$URL/mcp"
echo "------------------------------------------------------------"
echo " 本地 agent  ：$(curl -s -m 5 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && echo 正常 || echo 无响应)"
echo " 域名解析    ：$([ "$DNS_OK" = "1" ] && echo 已解析 || echo "未解析(异常)")"
echo " 公网访问    ：${HTTP_CODE:-未检测}$([ "$HTTP_CODE" = "200" ] && echo " (通)" || echo " (仅本机视角，见下方说明)")"
echo "------------------------------------------------------------"
echo " 主端 ~/.workbuddy/mcp.json 增加："
echo
echo "   \"$NAME\": {"
echo "     \"type\": \"http\","
echo "     \"url\": \"$URL/mcp\""
echo "   }"
echo "============================================================"

if [ "$DNS_OK" != "1" ] || { [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "200" ]; }; then
  echo
  echo "[i] 本脚本的『公网访问』数值是**从本机发起**的 curl，仅供参考，不等于隧道不可用。"
  echo "    本机 agent 正常 + 域名已解析时，多数情况下隧道其实是通的（从主端一测便知）。"
  echo
  echo "    『公网访问』异常的原因，按可能性排序："
  echo "    1) 容器/云主机出网被限制（只放行白名单域名）：本机连不上 Cloudflare 边缘，"
  echo "       但 cloudflared 自己走的是 UDP 7844 / 已建立的连接，所以隧道照样可用。"
  echo "       → 判定方法：在主端电脑执行  curl -s -o /dev/null -w '%{http_code}' $URL/"
  echo "         返回 200 就是好的，可直接填进主端 mcp.json。"
  echo "    2) trycloudflare 免费快隧被限流：短时间内反复创建隧道会导致域名不再解析。"
  echo "       → 等 10~30 分钟再跑本脚本；同一台设备只保留一条隧道，别反复重开。"
  echo "    3) QUIC(UDP) 被网络设备干扰：日志出现 'no recent network activity' 时改用"
  echo "       CF_PROTO=http2 bash $DIR/mcp-tunnel.sh"
  echo "    4) 要长期稳定：换固定隧道（自有域名 + cloudflared tunnel create）或端口映射/frp。"
fi
echo " 停止隧道：kill \$(cat "$PIDF")      日志：$LOG"
TUN_SH_EOF

chmod +x "$DIR/_supervisor.sh" "$DIR/mcp-ctl.sh" "$DIR/mcp-tunnel.sh" 2>/dev/null || true

$PY3 -c "import ast,sys; ast.parse(open('$DIR/mcp_agent.py',encoding='utf-8').read())" \
  || die "服务器代码语法校验失败"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="<本机IP>"

# ---------- 安装 / 启动 ----------
if [ "$MODE" = "systemd" ]; then
  log "写入 systemd 单元: /etc/systemd/system/${UNIT}.service"
  cat > "/etc/systemd/system/${UNIT}.service" <<UNIT_EOF
[Unit]
Description=MCP Agent ($NAME) - 副端远程操控服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$PY3 $DIR/mcp_agent.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3
StandardOutput=append:$DIR/agent.log
StandardError=append:$DIR/agent.log

[Install]
WantedBy=multi-user.target
UNIT_EOF

  systemctl daemon-reload || warn "daemon-reload 失败"
  # 端口若被旧进程占用，先清掉（按端口杀，避免 pkill 关键字误伤）
  "$PY3" "$DIR/_portkill.py" "$PORT" >/dev/null 2>&1 || true
  sleep 1
  systemctl enable --now "$UNIT" >/dev/null 2>&1 || warn "enable --now 失败，请手动 systemctl start $UNIT"
  sleep 2
  STATUS="$(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
  log "服务状态: $STATUS"
  if [ "$STATUS" != "active" ]; then
    warn "服务未激活，最近日志："
    journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null || tail -20 "$DIR/agent.log" 2>/dev/null || true
  fi
  VERIFY_CMD="systemctl status $UNIT"
else
  if [ "$SYSTEMD_OK" = "1" ]; then
    log "按参数要求使用守护进程模式（不注册 systemd 服务，用 mcp-ctl.sh 管理）"
  else
    log "本机 PID1 是 $(cat /proc/1/comm 2>/dev/null || echo unknown)，非 systemd（容器环境），使用守护进程模式"
  fi
  bash "$DIR/mcp-ctl.sh" start || true
  VERIFY_CMD="bash $DIR/mcp-ctl.sh status"
fi

# ---------- 自检 ----------
sleep 1
if command -v curl >/dev/null 2>&1; then
  HEALTH="$(curl -s -m 5 "http://127.0.0.1:${PORT}/" || true)"
  if [ -n "$HEALTH" ]; then
    log "本机自检通过: $HEALTH"
  else
    warn "本机自检失败，请查看 $DIR/agent.log"
    tail -n 20 "$DIR/agent.log" 2>/dev/null || true
  fi
else
  warn "未找到 curl，跳过自检（可用 python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:${PORT}/').read().decode())\"）"
fi

# ---------- 可选隧道 ----------
TUNNEL_URL=""
if [ "$DO_TUNNEL" = "1" ]; then
  log "启动公网隧道 ..."
  TUN_OUT="$(bash "$DIR/mcp-tunnel.sh" 2>&1 || true)"
  echo "$TUN_OUT"
  TUNNEL_URL="$(printf '%s' "$TUN_OUT" | grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' | head -1 || true)"
fi

# ---------- 输出接入信息 ----------
if [ -n "$TUNNEL_URL" ]; then
  MCP_URL="${TUNNEL_URL}/mcp"
  URL_NOTE="（公网隧道地址，重启后会变；换地址需同步改主端 mcp.json）"
else
  MCP_URL="http://${IP}:${PORT}/mcp"
  URL_NOTE=""
fi

cat <<EOF

============================================================================
 安装完成！
----------------------------------------------------------------------------
 副端标识 : $NAME
 监听端口 : $PORT
 安装目录 : $DIR
 运行模式 : $MODE$([ "$MODE" = "systemd" ] && echo "（服务单元 ${UNIT}.service）" || echo "（守护进程，用 mcp-ctl.sh 管理）")
 状态查看 : $VERIFY_CMD
 实时日志 : tail -f $DIR/agent.log
 重启服务 : $(if [ "$MODE" = "systemd" ]; then echo "systemctl restart $UNIT"; else echo "bash $DIR/mcp-ctl.sh restart"; fi)
 控制脚本 : bash $DIR/mcp-ctl.sh {start|stop|restart|status|log|tunnel}
 手动前台 : $PY3 $DIR/mcp_agent.py --host 0.0.0.0 --port $PORT

----------------------------------------------------------------------------
 在主端（WorkBuddy 所在电脑）的 ~/.workbuddy/mcp.json 里加这一段：

  "mcpServers": {
    "$NAME": {
      "type": "http",
      "url": "$MCP_URL"
    }
  }
 $URL_NOTE

 加完重启 WorkBuddy（或重载 MCP 配置）即可使用。
 工具名形如： ${PREFIX}exec / ${PREFIX}sysinfo / ${PREFIX}docker_ps ...

----------------------------------------------------------------------------
 可达性自查（重要）：
   本机内网 IP 是 $IP。
   * 若主端与副端在同一内网（家里 NAS / 飞牛 OS），用 http://$IP:$PORT/mcp 即可。
   * 若是云容器（PAI-DSW / Docker / 云主机），上面这个内网地址主端连不上，
     需要：bash $DIR/mcp-tunnel.sh   拿到公网地址后再填 mcp.json。
============================================================================
EOF
