#!/usr/bin/env bash
# ============================================================================
#  MCP 副端 Agent · 一键安装脚本（自包含，内嵌服务器代码）
# ----------------------------------------------------------------------------
#  在任意 Linux 设备上执行即可：
#      sudo bash install.sh --name fnos --port 3100
#
#  参数（都可省略）：
#      --name    <标识>    副端名字，用于 systemd 单元名与显示（默认取主机名）
#      --port    <端口>    监听端口（默认 3100）
#      --dir     <目录>    安装目录（默认 /opt/<name>_mcp）
#      --prefix  <前缀>    工具名前缀（默认空，工具名即 exec/read/...）
#      --sudo-pass <密码>  可选：写入配置，供 sudo 提权使用
#      --uninstall         卸载（停服务 + 删单元 + 删目录）
#
#  依赖：python3（只用标准库，无需 pip）；systemd（无 systemd 会自动降级为
#        直接后台运行 + 生成手动启动命令）
# ============================================================================
set -euo pipefail

NAME=""
PORT="3100"
DIR=""
PREFIX=""
SUDO_PASS=""
UNINSTALL="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --name)       NAME="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --prefix)     PREFIX="${2:-}"; shift 2 ;;
    --sudo-pass)  SUDO_PASS="${2:-}"; shift 2 ;;
    --uninstall)  UNINSTALL="1"; shift ;;
    -h|--help)    sed -n '2,20p' "$0" 2>/dev/null || echo "(管道方式不支持 --help，请见 README)"; exit 0 ;;
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

# ---------- 卸载 ----------
if [ "$UNINSTALL" = "1" ]; then
  log "卸载 $UNIT ..."
  command -v systemctl >/dev/null 2>&1 && systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/${UNIT}.service"
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null || true
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  rm -rf "$DIR"
  log "已卸载（目录 $DIR 已删除）"
  exit 0
fi

# ---------- 依赖检查 ----------
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装：apt install -y python3"
PY3="$(command -v python3)"
PYVER="$($PY3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
log "python3: $PY3 (v$PYVER)"
command -v bash >/dev/null 2>&1 || warn "未找到 bash，exec 工具会失败"

# ---------- 落盘 ----------
log "安装目录: $DIR"
mkdir -p "$DIR"

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
    if not sudo:
        return "bash -lc " + shlex.quote(cmd)
    if _is_root():
        return "bash -lc " + shlex.quote(cmd)
    pw = CFG.get("sudo_password") or ""
    if pw:
        return ("printf '%s\\n' " + shlex.quote(pw)
                + " | sudo -S bash -lc " + shlex.quote(cmd))
    # 没配密码：尝试免密 sudo（-n），失败会明确报错，不会卡住
    return "sudo -n bash -lc " + shlex.quote(cmd)


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
    ("exec", "在设备本机执行 shell 命令（bash -lc，支持多行脚本/管道/重定向）。sudo=true 可提权。",
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

$PY3 -c "import ast,sys; ast.parse(open('$DIR/mcp_agent.py',encoding='utf-8').read())" \
  || die "服务器代码语法校验失败"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="<本机IP>"

# ---------- systemd ----------
if command -v systemctl >/dev/null 2>&1; then
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

  systemctl daemon-reload
  # 端口若被旧进程占用，先清掉（按端口杀，避免 pkill 关键字误伤）
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 1
  systemctl enable --now "$UNIT" >/dev/null 2>&1 || true
  sleep 2
  STATUS="$(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
  log "服务状态: $STATUS"
  if [ "$STATUS" != "active" ]; then
    warn "服务未激活，最近日志："
    journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null || tail -20 "$DIR/agent.log" 2>/dev/null || true
  fi
else
  warn "本设备无 systemd，改为直接后台运行（重启后需手动拉起）"
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 1
  cd "$DIR"
  setsid nohup "$PY3" "$DIR/mcp_agent.py" --host 0.0.0.0 --port "$PORT" > "$DIR/agent.log" 2>&1 &
  sleep 2
  log "已在后台启动（pid $!）"
fi

# ---------- 自检 ----------
sleep 1
if command -v curl >/dev/null 2>&1; then
  HEALTH="$(curl -s -m 5 "http://127.0.0.1:${PORT}/" || true)"
  if [ -n "$HEALTH" ]; then
    log "本机自检通过: $HEALTH"
  else
    warn "本机自检失败，请查看 $DIR/agent.log"
  fi
fi

# ---------- 输出接入信息 ----------
cat <<EOF

============================================================================
 安装完成！
----------------------------------------------------------------------------
 副端标识 : $NAME
 监听端口 : $PORT
 安装目录 : $DIR
 服务单元 : $UNIT.service
 状态查看 : systemctl status $UNIT
 实时日志 : journalctl -u $UNIT -f
 改配置后 : systemctl restart $UNIT

 手动启动（无 systemd 时）：
   $PY3 $DIR/mcp_agent.py --host 0.0.0.0 --port $PORT

----------------------------------------------------------------------------
 在主端（WorkBuddy 所在电脑）的 ~/.workbuddy/mcp.json 里加这一段：

  "mcpServers": {
    "$NAME": {
      "type": "http",
      "url": "http://${IP}:${PORT}/mcp",
      "description": "$NAME 副端 MCP（远程操控本机）"
    }
  }

 加完重启 WorkBuddy（或重载 MCP 配置）即可使用。
 工具名形如： ${PREFIX}exec / ${PREFIX}sysinfo / ${PREFIX}docker_ps ...
============================================================================
EOF
