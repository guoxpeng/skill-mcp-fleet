#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 mcp_agent.py 内嵌进 install.sh，生成「单文件自包含」安装脚本。
这样副设备上只需要一个 install.sh 就能装完，不用额外传文件。

用法： python build_installer.py
产出： fleet-agent/install.sh

v2.0 变化：
  * 修正 systemd 探测：不再只看 systemctl 是否存在（容器里通常装了二进制但
    PID1 不是 systemd），改看 /run/systemd/system + /proc/1/comm。
  * 无 systemd 时不再只是「后台跑一下就算」，而是落一套自包含控制脚本：
      mcp-ctl.sh      start/stop/restart/status/log
      _supervisor.sh  崩溃自动重启的守护壳
      _portkill.py    零依赖按端口清理僵尸进程（替代 fuser）
      port.txt        端口记录
  * 新增 mcp-tunnel.sh：cloudflared 快速隧道，把副端暴露到公网（云容器场景必需）。
  * 新增 --mode auto|systemd|nohup 与 --tunnel 参数。
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "mcp_agent.py")
OUT = os.path.join(HERE, "install.sh")

agent_src = io.open(SRC, encoding="utf-8").read()

# 安全检查：内嵌用的定界符绝不能出现在源码行首
for line in agent_src.splitlines():
    if line.strip() == "MCP_AGENT_PY_EOF":
        raise SystemExit("源码里出现了与内嵌定界符冲突的行，请更换定界符")

TEMPLATE = r'''#!/usr/bin/env bash
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
#      --auth-token <串>   可选：访问令牌。设了之后所有请求都要带
#                          Authorization: Bearer <串>。公网隧道场景必填
#                          （--tunnel 时若为空会自动生成一个）
#      --allow-ips <列表>  可选：来源 IP 白名单，逗号分隔，支持 CIDR
#                          例：--allow-ips '192.168.1.0/24,10.8.0.2'
#      --directory <URL>   可选：地址目录地址。副端会把当前隧道地址上报过去，
#                          主端据此自动更新，不用手工改 mcp.json
#      --directory-token <串> 可选：地址目录的访问令牌
#      --no-keepalive      关闭保活守护（默认开启：agent 挂了拉起、隧道断了重建、
#                          地址持续上报）
#      --tunnel           装完顺便起 cloudflared 快速隧道（云容器/无公网入口时用）
#      --require-auth      把「必须鉴权」写进配置：没有 auth_token 时副端拒绝启动
#                          （公网/隧道场景建议加；内网也推荐）
#      --generate-token    若还没有 auth_token，自动生成一个强随机值写进配置
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
AUTH_TOKEN=""
ALLOW_IPS=""
ALLOW_IPS_JSON="[]"
DIRECTORY_URL=""
DIRECTORY_TOKEN=""
KEEPALIVE="1"
REQUIRE_AUTH="0"
GEN_TOKEN="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --name)       NAME="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --prefix)     PREFIX="${2:-}"; shift 2 ;;
    --sudo-pass)  SUDO_PASS="${2:-}"; shift 2 ;;
    --mode)       MODE="${2:-}"; shift 2 ;;
    --tunnel)     DO_TUNNEL="1"; shift ;;
    --auth-token) AUTH_TOKEN="${2:-}"; shift 2 ;;
    --allow-ips)  ALLOW_IPS="${2:-}"; shift 2 ;;
    --directory)  DIRECTORY_URL="${2:-}"; shift 2 ;;
    --directory-token) DIRECTORY_TOKEN="${2:-}"; shift 2 ;;
    --no-keepalive) KEEPALIVE="0"; shift ;;
    --require-auth) REQUIRE_AUTH="1"; shift ;;
    --generate-token) GEN_TOKEN="1"; shift ;;
    --uninstall)  UNINSTALL="1"; shift ;;
    -h|--help)    sed -n '2,44p' "$0" 2>/dev/null || echo "(管道方式不支持 --help，请见 README)"; exit 0 ;;
    *) echo "[!] 未知参数: $1"; exit 1 ;;
  esac
done

# --allow-ips 收 "192.168.1.0/24,10.8.0.2" 这种逗号分隔串，转成 JSON 数组
if [ -n "$ALLOW_IPS" ]; then
  ALLOW_IPS_JSON="$(printf '%s' "$ALLOW_IPS" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
    | grep -v '^$' | sed 's/.*/"&"/' | paste -sd, - 2>/dev/null || true)"
  [ -n "$ALLOW_IPS_JSON" ] && ALLOW_IPS_JSON="[$ALLOW_IPS_JSON]" || ALLOW_IPS_JSON="[]"
fi

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
  # 保活单元必须**先**停。踩过的坑：早先只删 ${UNIT}.service，漏了
  # ${UNIT}-watchdog.timer —— timer 是 enabled+active 的，删掉安装目录后它仍会
  # 每 60 秒触发一次 oneshot，而单元里的 WorkingDirectory=$DIR 已经不存在，
  # journal 里会永远刷 `Failed at step CHDIR spawning /bin/bash`。
  if [ -f "/etc/systemd/system/${UNIT}-watchdog.timer" ] \
     || [ -f "/etc/systemd/system/${UNIT}-watchdog.service" ]; then
    [ "$SYSTEMD_OK" = "1" ] && systemctl disable --now "${UNIT}-watchdog.timer" 2>/dev/null || true
    [ "$SYSTEMD_OK" = "1" ] && systemctl stop "${UNIT}-watchdog.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/${UNIT}-watchdog.timer" \
          "/etc/systemd/system/${UNIT}-watchdog.service"
  fi
  if [ -f "/etc/systemd/system/${UNIT}.service" ]; then
    [ "$SYSTEMD_OK" = "1" ] && systemctl disable --now "$UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/${UNIT}.service"
  fi
  [ "$SYSTEMD_OK" = "1" ] && systemctl daemon-reload 2>/dev/null || true
  if [ -f "$DIR/tunnel.pid" ]; then kill "$(cat "$DIR/tunnel.pid")" 2>/dev/null || true; fi
  pkill -f "$DIR/cloudflared" 2>/dev/null || true
  if [ -f "$DIR/agent.pid" ]; then kill -TERM -"$(cat "$DIR/agent.pid")" 2>/dev/null || kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true; fi
  pkill -f "$DIR/_supervisor.sh" 2>/dev/null || true
  pkill -f "$DIR/mcp_agent.py" 2>/dev/null || true
  pkill -f "$DIR/mcp-watchdog.sh" 2>/dev/null || true
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
__AGENT_SOURCE__
MCP_AGENT_PY_EOF

# 配置文件（已存在则不覆盖，避免冲掉你的设置）
CFG_PREEXIST="0"
if [ -f "$DIR/mcp_agent_config.json" ]; then
  warn "配置文件已存在，保留原文件（如需重置请手动删除）"
  CFG_FILE="$DIR/mcp_agent_config.json"
  CFG_PREEXIST="1"
else
  CFG_FILE="$DIR/mcp_agent_config.json"
  cat > "$CFG_FILE" <<CFG_EOF
{
  "name": "$NAME",
  "tool_prefix": "$PREFIX",
  "version": "3.1.0",
  "sudo_password": "$SUDO_PASS",
  "work_dir": "/",
  "command_timeout": 120,
  "max_output_bytes": 200000,
  "allowed_roots": ["/"],
  "enable_docker": true,
  "login_shell": true,
  "auth_token": "$AUTH_TOKEN",
  "allow_ips": $ALLOW_IPS_JSON,
  "require_auth": $( [ "$REQUIRE_AUTH" = "1" ] && echo true || echo false ),
  "cors_allow_origins": [],
  "directory_url": "$DIRECTORY_URL",
  "directory_token": "$DIRECTORY_TOKEN",
  "tunnel_url": ""
}
CFG_EOF
  chmod 600 "$CFG_FILE" 2>/dev/null || true
fi

# 已存在的配置不整体覆盖（免得冲掉你的调参），但**命令行明确给了的参数必须生效** ——
# 否则重装时 --auth-token / --allow-ips / --directory 会被静默忽略，
# 表现为「我明明传了 token，副端却还在用旧的」。
if [ "$CFG_PREEXIST" = "1" ] \
   && { [ -n "$AUTH_TOKEN" ] || [ -n "$ALLOW_IPS" ] || [ -n "$DIRECTORY_URL" ] \
        || [ -n "$DIRECTORY_TOKEN" ] || [ "$REQUIRE_AUTH" = "1" ]; }; then
  "$PY3" - "$CFG_FILE" "$AUTH_TOKEN" "$ALLOW_IPS" "$DIRECTORY_URL" "$DIRECTORY_TOKEN" "$REQUIRE_AUTH" <<'MERGE_PY_EOF'
import json, sys
p, tok, ips, durl, dtok, reqauth = sys.argv[1:7]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
if tok:
    d["auth_token"] = tok
if ips:
    d["allow_ips"] = [x.strip() for x in ips.split(",") if x.strip()]
if durl:
    d["directory_url"] = durl
if dtok:
    d["directory_token"] = dtok
if reqauth == "1":
    d["require_auth"] = True
d["version"] = "3.1.0"
d.setdefault("name", "")
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("已合并命令行参数：%s" % ", ".join(
    k for k, v in (("auth_token", tok), ("allow_ips", ips),
                   ("directory_url", durl), ("directory_token", dtok),
                   ("require_auth", "1" if reqauth == "1" else "")) if v))
MERGE_PY_EOF
  chmod 600 "$CFG_FILE" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 无条件收紧配置权限（v3.1 加固）
# 配置里有 auth_token / sudo_password。原实现只在「新建配置」和「带参数重装」
# 两个分支 chmod 600，**重装不带参数时旧权限原样保留** —— 实测常见 0644 root:root，
# 意味着同机任何用户都能读到这个 root shell 的令牌。这里一律收紧。
# ---------------------------------------------------------------------------
chmod 600 "$CFG_FILE" 2>/dev/null || true

# ---------------------------------------------------------------------------
# --generate-token：还没有令牌就现场生成一个
# ---------------------------------------------------------------------------
if [ "$GEN_TOKEN" = "1" ]; then
  CUR_TOK="$("$PY3" -c "import json;print(json.load(open('$CFG_FILE')).get('auth_token',''))" 2>/dev/null || echo '')"
  if [ -n "$CUR_TOK" ]; then
    log "已有 auth_token，--generate-token 跳过（不覆盖既有令牌）"
  else
    NEW_TOK="$("$PY3" -c 'import secrets;print(secrets.token_urlsafe(32))')"
    "$PY3" - "$CFG_FILE" "$NEW_TOK" <<'GENTOK_PY_EOF'
import json, sys
p, tok = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
d["auth_token"] = tok
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
    f.write("\n")
GENTOK_PY_EOF
    chmod 600 "$CFG_FILE" 2>/dev/null || true
    log "已生成随机 auth_token（已写入配置，权限 0600）"
    log "令牌: $NEW_TOK"
    warn "主端 mcp.json 必须同步加：\"headers\": { \"Authorization\": \"Bearer $NEW_TOK\" }"
  fi
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
  "$PY" -u "$DIR/mcp_agent.py" --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1
  code=$?
  echo "[$(date '+%F %T')] agent 退出 (code=$code)，3 秒后重启" >> "$LOG"
  sleep 3
done
SUP_SH_EOF

# 控制脚本（无 systemd 环境的手动开关，比记 nohup 命令稳）
cat > "$DIR/mcp-ctl.sh" <<'CTL_SH_EOF'
#!/usr/bin/env bash
# MCP 副端控制脚本： start | stop | restart | status | log | tunnel | watchdog
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
PY="${PY:-$(command -v python3 || echo /usr/bin/python3)}"
PIDF="$DIR/agent.pid"
LOG="$DIR/agent.log"
WDPIDF="$DIR/watchdog.pid"
# systemd 单元名（安装时写入 unit.txt），保活开关需要用它定位 watchdog.timer
NAME="$(cat "$DIR/unit.txt" 2>/dev/null || echo '')"
[ -z "$NAME" ] && NAME="mcp-agent"
WD_UNIT="$NAME-watchdog.timer"

running() {
  [ -f "$PIDF" ] || return 1
  local p; p="$(cat "$PIDF" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

wd_running() {
  [ -f "$WDPIDF" ] || return 1
  local p; p="$(cat "$WDPIDF" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

# 保活守护：让副端「随时可连」。已有 systemd timer 的环境由 timer 负责，
# 这里只管没有 systemd 的容器场景（常驻循环）。
wd_start() {
  [ -x "$DIR/mcp-watchdog.sh" ] || return 0
  [ -f "$DIR/.no_keepalive" ] && return 0
  if wd_running; then return 0; fi
  setsid nohup bash "$DIR/mcp-watchdog.sh" >> "$DIR/watchdog.log" 2>&1 < /dev/null &
  sleep 1
  wd_running && echo "保活守护已启动 (pid $(cat "$WDPIDF"))" || echo "保活守护启动失败（见 watchdog.log）"
}

wd_stop() {
  if [ -f "$WDPIDF" ]; then
    local p; p="$(cat "$WDPIDF" 2>/dev/null)"
    [ -n "${p:-}" ] && kill -TERM "$p" 2>/dev/null || true
    rm -f "$WDPIDF"
  fi
  pkill -f "$DIR/mcp-watchdog.sh" 2>/dev/null || true
  echo "保活守护已停止"
}

start() {
  if [ -f "/etc/systemd/system/${NAME}.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl start "$NAME" 2>/dev/null && echo "已启动（systemd: $NAME）" || echo "systemctl start 失败"
    wd_start
    return 0
  fi
  if running; then echo "已在运行 (pid $(cat "$PIDF"))"; wd_start; return 0; fi
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
    wd_start
  else
    echo "启动失败，见 $LOG"; tail -n 15 "$LOG" 2>/dev/null; return 1
  fi
}

stop() {
  if [ -f "/etc/systemd/system/${NAME}.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl stop "$NAME" 2>/dev/null || true
  fi
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
  # systemd 模式下没有 agent.pid，只看 pid 文件会误报「未运行」，先问 systemd
  local SYS_UNIT=""
  if [ -f "/etc/systemd/system/${NAME}.service" ] && command -v systemctl >/dev/null 2>&1; then
    SYS_UNIT="$NAME"
  fi
  if [ -n "$SYS_UNIT" ]; then
    local st; st="$(systemctl is-active "$SYS_UNIT" 2>/dev/null || echo unknown)"
    echo "运行中（systemd: $SYS_UNIT，状态 $st）"
    [ "$st" = "active" ] || echo "  [!] 服务未激活，用 systemctl status $SYS_UNIT 看原因"
  elif running; then
    echo "运行中 (守护 pid $(cat "$PIDF"), 端口 $PORT)"
  else
    echo "未运行"
    return 1
  fi
  if command -v curl >/dev/null 2>&1; then
    # 开了鉴权就必须带 token，否则这里只会打印一个 401，看不出副端到底活没活
    TOK="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"
    if [ -n "$TOK" ]; then
      CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/" || true)"
      BODY="$(curl -s -m 5 -H "Authorization: Bearer $TOK" "http://127.0.0.1:$PORT/" || echo '(无响应)')"
    else
      CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)"
      BODY="$(curl -s -m 5 "http://127.0.0.1:$PORT/" || echo '(无响应)')"
    fi
    echo "健康检查: HTTP ${CODE:-000} $BODY"
  fi
  # 保活与隧道状态：一眼看出「随时可连」还成不成立
  if [ -n "$SYS_UNIT" ] && command -v systemctl >/dev/null 2>&1 \
     && [ -f "/etc/systemd/system/$WD_UNIT" ]; then
    local wst; wst="$(systemctl is-active "$WD_UNIT" 2>/dev/null || echo unknown)"
    echo "保活定时器: $wst（$WD_UNIT，每 60s）"
  elif wd_running; then
    echo "保活守护: 运行中 (pid $(cat "$WDPIDF"))"
  elif [ -f "$DIR/.no_keepalive" ]; then
    echo "保活守护: 已关闭（mcp-ctl.sh keepalive on 可恢复）"
  else
    echo "保活守护: 未运行（用 mcp-ctl.sh start 拉起）"
  fi
  if [ -f "$DIR/.want_tunnel" ]; then
    if [ -f "$DIR/tunnel.pid" ] && kill -0 "$(cat "$DIR/tunnel.pid" 2>/dev/null)" 2>/dev/null; then
      echo "公网隧道: 运行中 pid=$(cat "$DIR/tunnel.pid")"
    else
      echo "公网隧道: 未运行（保活守护会在一分钟内拉起）"
    fi
    [ -f "$DIR/current_url.txt" ] && echo "当前地址: $(cat "$DIR/current_url.txt")"
  fi
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart)
    # ★ 先把「延迟 start」脱离进程组挂到后台，再执行 stop。
    #   原因：若本次调用来自副端自己的 exec 工具，stop 会把调用者（也就是这条命令）一起杀掉，
    #   导致后面的 start 永远执行不到 —— 表现为重启后 agent 再也没起来（隧道 502、端口无响应）。
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup bash -c "sleep 2; bash '$DIR/mcp-ctl.sh' start" >>"$DIR/ctl.log" 2>&1 </dev/null &
    else
      nohup bash -c "sleep 2; bash '$DIR/mcp-ctl.sh' start" >>"$DIR/ctl.log" 2>&1 </dev/null &
    fi
    stop ;;
  status)  status ;;
  log)     tail -n "${2:-40}" "$LOG" ;;
  tunnel)  shift; bash "$DIR/mcp-tunnel.sh" "$@" ;;
  watchdog)
    # 跑一轮保活检查（等价 systemd timer 的动作），并回报结果
    bash "$DIR/mcp-watchdog.sh" --once && echo "保活检查完成"
    wd_running && echo "常驻守护: 运行中 (pid $(cat "$WDPIDF"))" || echo "常驻守护: 未运行（容器环境建议 start 一次）"
    ;;
  wdlog)   tail -n "${2:-40}" "$DIR/watchdog.log" ;;
  keepalive)
    # 开关保活：keepalive on / off / status
    case "${2:-status}" in
      on)
        rm -f "$DIR/.no_keepalive"
        wd_start
        if [ -f "/etc/systemd/system/$WD_UNIT" ]; then
          systemctl enable --now "$WD_UNIT" >/dev/null 2>&1 \
            && echo "systemd 保活定时器已启用" || echo "systemd 保活定时器启用失败（可忽略，常驻守护已接管）"
        fi
        ;;
      off)
        touch "$DIR/.no_keepalive"
        wd_stop
        if [ -f "/etc/systemd/system/$WD_UNIT" ]; then
          systemctl disable --now "$WD_UNIT" >/dev/null 2>&1 || true
          echo "systemd 保活定时器已停用"
        fi
        ;;
      *)
        [ -f "$DIR/.no_keepalive" ] && echo "保活: 已关闭（.no_keepalive）" || echo "保活: 已开启"
        wd_running && echo "常驻守护: 运行中 (pid $(cat "$WDPIDF"))" || echo "常驻守护: 未运行"
        ;;
    esac
    ;;
  *) echo "用法: bash $DIR/mcp-ctl.sh {start|stop|restart|status|log [n]|tunnel|watchdog|wdlog [n]|keepalive on|off|status}"; exit 1 ;;
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
PROTOF="$DIR/.tunnel_proto"
PY="$(command -v python3 || echo /usr/bin/python3)"
CREATED="0"
# 上次自动降级过就记住，省掉每次 12 秒的探测等待
if [ -z "${CF_PROTO:-}" ] && [ -f "$PROTOF" ]; then
  CF_PROTO="$(cat "$PROTOF" 2>/dev/null || true)"
  [ -n "$CF_PROTO" ] && echo "[i] 沿用上次选定的隧道协议: $CF_PROTO"
fi
NAME="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json'))['name'])" 2>/dev/null || echo node)"

# ============================================================================
# 安全闸门（v3.0）：开公网隧道之前，必须先把鉴权配上
# ----------------------------------------------------------------------------
# 副端以 root 运行，exec 等于一个 root shell。隧道一开，任何拿到这个 URL 的人
# 都能在你设备上以 root 执行任意命令 —— 而 trycloudflare 的地址是可以被扫描到的。
# 所以这里默认强制：没有 auth_token 就自动生成一个强随机值写进配置并重启副端。
# 已有 token 则复用（幂等）。确实想裸奔：FLEET_NO_AUTH=1 bash mcp-tunnel.sh
# ============================================================================
TOKEN="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"
if [ -z "$TOKEN" ] && [ "${FLEET_NO_AUTH:-0}" = "1" ]; then
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "[!] FLEET_NO_AUTH=1 —— 隧道将**不带任何鉴权**暴露到公网。"
  echo "[!] 任何拿到该 URL 的人都能以 root 身份在你设备上执行任意命令。"
  echo "[!] 仅在你完全清楚后果、并且马上就会关掉隧道时才这么做。"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
fi
if [ -z "$TOKEN" ] && [ "${FLEET_NO_AUTH:-0}" != "1" ]; then
  TOKEN="$("$PY" -c "import secrets;print(secrets.token_urlsafe(32))")"
  "$PY" - "$DIR/mcp_agent_config.json" "$TOKEN" <<'TOK_PY_EOF'
import json, sys
p, tok = sys.argv[1], sys.argv[2]
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["auth_token"] = tok
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
TOK_PY_EOF
  chmod 600 "$DIR/mcp_agent_config.json" 2>/dev/null || true
  echo "[+] 已为公网隧道生成鉴权 token（32 字节随机），写入 mcp_agent_config.json。"
  echo "[+] 重启副端使配置生效 ..."
  if [ -f "/etc/systemd/system/${NAME}-mcp.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart "${NAME}-mcp" >/dev/null 2>&1 || true
  else
    bash "$DIR/mcp-ctl.sh" restart >/dev/null 2>&1 || true
  fi
  sleep 2
  echo "[+] 完成。"
fi

if [ ! -x "$BIN" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64)  CF="cloudflared-linux-amd64" ;;
    aarch64|arm64) CF="cloudflared-linux-arm64" ;;
    *) echo "[x] 不支持的 CPU 架构: $ARCH"; exit 1 ;;
  esac
  echo "[+] 下载 cloudflared ($CF) ..."
  # 顺序：官方源优先（供应链更可信），失败再退到第三方反代。
  # 反代有能力篡改内容，而 cloudflared 是以 root 常驻运行的 —— 所以下载后
  # 一律做「ELF 魔数 + 最小体积」校验；要更严就 export CF_SHA256=<官方哈希>。
  ok=0
  for M in \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
    "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
    "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" ; do
    if curl -fsSL --connect-timeout 12 -o "$BIN" "$M"; then
      # 校验 1：必须是 ELF（镜像/网关出错时经常返回一个 HTML 错误页）
      if ! "$PY" -c "import sys;sys.exit(0 if open(sys.argv[1],'rb').read(4)==b'\x7fELF' else 1)" "$BIN" 2>/dev/null; then
        echo "[!] 下载物不是 ELF 可执行文件，丢弃该通道"
        rm -f "$BIN"; continue
      fi
      # 校验 2：体积下限（真实二进制数十 MB，明显偏小说明被截断或替换）
      SZ="$(wc -c < "$BIN" 2>/dev/null || echo 0)"
      if [ "${SZ:-0}" -lt 5000000 ]; then
        echo "[!] 下载物体积异常（${SZ:-0} 字节），丢弃该通道"
        rm -f "$BIN"; continue
      fi
      # 校验 3：可选的固定哈希（export CF_SHA256=<官方 sha256>）
      if [ -n "${CF_SHA256:-}" ]; then
        GOT="$("$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$BIN" 2>/dev/null || echo '')"
        if [ "$GOT" != "$CF_SHA256" ]; then
          echo "[!] sha256 不匹配（期望 $CF_SHA256，实际 $GOT），丢弃该通道"
          rm -f "$BIN"; continue
        fi
        echo "[+] cloudflared sha256 校验通过"
      fi
      ok=1; break
    fi
    echo "[!] 通道失败，试下一个"
  done
  [ "$ok" = "1" ] || { echo "[x] cloudflared 下载失败或校验未通过，请手动放置到 $BIN"; exit 1; }
  chmod +x "$BIN"
fi

# 声明「本副端要用隧道」——保活守护据此决定要不要盯着 cloudflared
touch "$DIR/.want_tunnel" 2>/dev/null || true

# ---- 固定隧道（named tunnel）：给了 CF_TOKEN 就走这条，地址恒定 ----
# 快隧的地址每次重启都变、还会撞限流；有 Cloudflare 账号的话用固定隧道最省心：
#   cloudflared tunnel create <名字>          # 一次性，在任意机器上做
#   cloudflared tunnel route dns <名字> mcp.你的域名
#   CF_TOKEN=<tunnel token> bash mcp-tunnel.sh
# 地址写进 mcp_agent_config.json 的 "tunnel_url"，保活守护就不会再去猜地址。
NAMED=""
URL=""
if [ -n "${CF_TOKEN:-}" ]; then
  NAMED="1"
  echo "[i] 使用固定隧道（named tunnel）：地址恒定，不受 trycloudflare 限流影响"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
    echo "[i] 固定隧道已在运行（pid $(cat "$PIDF")），复用。"
  else
    : > "$LOG"
    setsid nohup "$BIN" tunnel --no-autoupdate run --token "$CF_TOKEN" >> "$LOG" 2>&1 < /dev/null &
    sleep 2
    REAL="$(pgrep -f "$BIN tunnel --no-autoupdate run --token" 2>/dev/null | head -1 || true)"
    if [ -n "$REAL" ]; then echo "$REAL" > "$PIDF"; else echo $! > "$PIDF"; fi
  fi
  URL="${FLEET_TUNNEL_URL:-$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('tunnel_url',''))" 2>/dev/null || echo '')}"
  if [ -z "$URL" ]; then
    echo "[!] 固定隧道地址未知。请在 mcp_agent_config.json 里补："
    echo "      \"tunnel_url\": \"https://mcp.你的域名\""
    echo "    或本次临时指定：FLEET_TUNNEL_URL=https://mcp.你的域名 bash $DIR/mcp-tunnel.sh"
  fi
fi

# ---- 复用已有隧道：避免撞 trycloudflare 创建频率限制，也让公网地址保持稳定 ----
if [ -z "$NAMED" ] && [ "${CF_FORCE:-0}" != "1" ] && [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  URL="$(grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1)"
  if [ -n "$URL" ]; then
    echo "[i] 已有隧道在运行（pid $(cat "$PIDF")），复用既有地址，不新建。"
    echo "[i] 要强制重建：CF_FORCE=1 bash $DIR/mcp-tunnel.sh"
    echo "[i] 要改协议：  kill \$(cat $PIDF); CF_PROTO=http2 bash $DIR/mcp-tunnel.sh"
  fi
fi

if [ -z "$NAMED" ] && [ -z "$URL" ]; then
if [ -f "$PIDF" ]; then kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"; sleep 1; fi
: > "$LOG"
CREATED="1"
# 偶发 502 时可试：CF_PROTO=http2 bash mcp-tunnel.sh  （QUIC 被限速的网络下更稳）
PROTO_ARGS=""
[ -n "${CF_PROTO:-}" ] && PROTO_ARGS="--protocol ${CF_PROTO}"
# shellcheck disable=SC2086
setsid nohup "$BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate $PROTO_ARGS >> "$LOG" 2>&1 < /dev/null &
# 记真实 cloudflared pid：setsid 可能 fork，$! 未必是最终进程
sleep 1
REAL="$(pgrep -f "$BIN tunnel --url http://127.0.0.1:$PORT" 2>/dev/null | head -1 || true)"
if [ -n "$REAL" ]; then echo "$REAL" > "$PIDF"; else echo $! > "$PIDF"; fi

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
fi

# ---- QUIC 被网络设备干扰时，自动降级到 http2 重建 ----
# 症状：日志反复刷 "no recent network activity"，域名能解析，但主端访问一律 502。
# 这在不少家宽/路由设备上都会出现，属于环境问题而非隧道本身坏了。
# 与其让用户读文档手工重跑，这里自动探测一次并换协议重建；选定后记下来，
# 下次直接沿用（CF_NO_FALLBACK=1 可关闭该行为）。
if [ -z "$NAMED" ] && [ "$CREATED" = "1" ] && [ "${CF_PROTO:-}" != "http2" ] && [ "${CF_NO_FALLBACK:-0}" != "1" ]; then
  echo "[i] 探测隧道连通性（最多 36s；QUIC 被干扰会自动切 http2）..."
  _i=0
  while [ "$_i" -lt 12 ]; do
    if grep -qE 'no recent network activity|failed to accept QUIC stream|datagram manager error' "$LOG" 2>/dev/null; then
      echo
      echo "[!] 检测到 QUIC(UDP 7844) 被网络设备干扰："
      echo "    $(grep -oE 'no recent network activity|failed to accept QUIC stream|datagram manager error' "$LOG" | tail -1)"
      echo "[!] 自动改用 http2 重建隧道（公网地址会变），以后默认沿用 http2 ..."
      kill "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null || true
      rm -f "$PIDF"
      echo "http2" > "$PROTOF"
      exec env CF_PROTO=http2 CF_NO_FALLBACK=1 bash "$0"
    fi
    sleep 3
    _i=$((_i + 1))
  done
  echo "[i] 隧道连接正常（quic）。"
fi

# ---- 自检 1：域名是否解析 ----
# 把最终地址落盘，保活守护和主端都读它
if [ -n "$URL" ]; then
  printf '%s' "$URL" > "$DIR/.tunnel_url" 2>/dev/null || true
  printf '%s' "$URL" > "$DIR/current_url.txt" 2>/dev/null || true
fi
HOST="${URL#https://}"
DNS_OK="0"
for _ in $(seq 1 15); do
  if getent hosts "$HOST" >/dev/null 2>&1 || $PY -c "import socket;socket.getaddrinfo('$HOST',443)" >/dev/null 2>&1; then
    DNS_OK="1"; break
  fi
  sleep 2
done

# ---- 自检 2：公网是否真的能访问到本机 agent（开了鉴权就带上 token）----
HTTP_CODE=""
if command -v curl >/dev/null 2>&1; then
  if [ -n "$TOKEN" ]; then
    HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$URL/" 2>/dev/null || true)"
  else
    HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$URL/" 2>/dev/null || true)"
  fi
fi

if [ -n "$TOKEN" ]; then
  LOCAL_AGENT="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/" 2>/dev/null || echo 000)"
else
  LOCAL_AGENT="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null || echo 000)"
fi

echo
echo "============================================================"
echo " 公网隧道地址：$URL"
echo " MCP 端点    ：$URL/mcp"
if [ -n "$TOKEN" ]; then
  echo " 鉴权 token  ：$TOKEN"
else
  echo " 鉴权 token  ：(未设置 —— 公网裸奔，请尽快补上)"
fi
echo "------------------------------------------------------------"
echo " 本地 agent  ：$([ "$LOCAL_AGENT" = "200" ] && echo 正常 || echo "异常(HTTP ${LOCAL_AGENT})")"
echo " 域名解析    ：$([ "$DNS_OK" = "1" ] && echo 已解析 || echo "未解析(异常)")"
echo " 公网访问    ：${HTTP_CODE:-未检测}$([ "$HTTP_CODE" = "200" ] && echo " (通)" || echo " (仅本机视角，见下方说明)")"
echo "------------------------------------------------------------"
echo " 主端 ~/.workbuddy/mcp.json 增加："
echo
echo "   \"$NAME\": {"
echo "     \"type\": \"http\","
echo "     \"url\": \"$URL/mcp\"$([ -n "$TOKEN" ] && printf ',')"
if [ -n "$TOKEN" ]; then
  echo "     \"headers\": { \"Authorization\": \"Bearer $TOKEN\" }"
fi
echo "   }"
echo "------------------------------------------------------------"
echo " 或直接用主端脚本一行搞定（自动带上 token）："
if [ -n "$TOKEN" ]; then
  echo "   python fleet.py add --name $NAME --url $URL/mcp --token $TOKEN"
else
  echo "   python fleet.py add --name $NAME --url $URL/mcp"
fi
echo "============================================================"

if [ -z "$TOKEN" ]; then
  echo
  echo "[!] 当前隧道**没有任何鉴权**。强烈建议立即补上："
  echo "    bash $DIR/mcp-tunnel.sh      # 重新跑一次会自动生成 token 并重启副端"
fi

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
  echo "       → 本脚本默认复用已有隧道，正常不会撞上；万一是被限流，等 10~30 分钟再"
  echo "         用 CF_FORCE=1 bash $DIR/mcp-tunnel.sh 重建。"
  echo "    3) QUIC(UDP) 被网络设备干扰：日志出现 'no recent network activity' 时改用"
  echo "       CF_PROTO=http2 bash $DIR/mcp-tunnel.sh"
  echo "    4) 要长期稳定：换固定隧道（自有域名 + cloudflared tunnel create）或端口映射/frp。"
fi
echo " 停止隧道：kill \$(cat "$PIDF")      日志：$LOG"
TUN_SH_EOF

# ============================================================================
# 保活守护（v3.0）：让副端「随时可连」
# ----------------------------------------------------------------------------
# 三件事：
#   1) agent 挂了 → 拉起来
#   2) 隧道进程没了 → 重建（**只在真的没了时重建**）
#   3) 把当前可用地址持续写进 current_url.txt，并上报「地址目录」
#
# 铁律：能复用就不重建。trycloudflare 快隧每重建一次就换一次地址，而且短时间
# 重建多次会撞限流（表现为「隧道连上了但域名一直不解析」，要等 10~30 分钟）。
# 所以这里只守护、不折腾 —— 进程还活着就绝不动它。
# ============================================================================
cat > "$DIR/mcp-watchdog.sh" <<'WATCHDOG_SH_EOF'
#!/usr/bin/env bash
# MCP 副端保活守护。两种用法：
#   bash mcp-watchdog.sh --once    跑一轮就退出（配 systemd timer / cron）
#   bash mcp-watchdog.sh           常驻循环（无 systemd 的容器环境）
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
PY="$(command -v python3 || echo /usr/bin/python3)"
LOG="$DIR/watchdog.log"
CUR="$DIR/current_url.txt"
PIDF="$DIR/watchdog.pid"
CFG="$DIR/mcp_agent_config.json"
INTERVAL="${WATCHDOG_INTERVAL:-60}"
ONCE="0"
[ "${1:-}" = "--once" ] && ONCE="1"

# systemd 单元名（安装时写入 unit.txt）
UNIT="$(cat "$DIR/unit.txt" 2>/dev/null || echo '')"
[ -z "$UNIT" ] && UNIT="mcp-agent"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

cfg() { "$PY" -c "import json;print(json.load(open('$CFG')).get('$1',''))" 2>/dev/null || echo ''; }

alive_agent()  { pgrep -f "$DIR/mcp_agent.py" >/dev/null 2>&1; }
alive_tunnel() { [ -f "$DIR/tunnel.pid" ] && kill -0 "$(cat "$DIR/tunnel.pid" 2>/dev/null)" 2>/dev/null; }

# 把当前地址上报到「地址目录」，主端据此自动更新，不必手工改 mcp.json
report() {
  local url="$1" durl dtok name
  durl="$(cfg directory_url)"
  [ -z "$durl" ] && return 0
  dtok="$(cfg directory_token)"
  name="$(cfg name)"
  # 成功不写日志（否则每 60s 一条把 watchdog.log 撑爆），失败才留痕
  "$PY" - "$durl" "$name" "$url" "$PORT" "$dtok" <<'REPORT_PY_EOF' >/dev/null 2>>"$LOG"
import json, sys, time, urllib.request
base, name, url, port, dtok = sys.argv[1:6]
body = json.dumps({"name": name or "node", "url": url,
                   "port": int(port or 0), "ts": int(time.time())}).encode("utf-8")
req = urllib.request.Request(base.rstrip("/") + "/register", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
if dtok:
    req.add_header("X-Fleet-Token", dtok)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
except Exception as e:
    sys.stderr.write("[report] 上报失败 %r\n" % (e,))
REPORT_PY_EOF
}

# 拉起 agent：systemd 环境交给 systemctl（避免和 Restart=always 抢），否则用 mcp-ctl.sh
ensure_agent() {
  if alive_agent; then return 0; fi
  if [ -f "/etc/systemd/system/${UNIT}.service" ] && command -v systemctl >/dev/null 2>&1; then
    log "agent 未运行 → systemctl restart $UNIT"
    systemctl restart "$UNIT" >> "$LOG" 2>&1 || log "systemctl restart 失败"
  else
    log "agent 未运行 → mcp-ctl.sh start"
    bash "$DIR/mcp-ctl.sh" start >> "$LOG" 2>&1 || log "拉起 agent 失败"
  fi
  sleep 3
}

round() {
  # --- 1) agent 保活 ---
  ensure_agent

  # --- 2) 隧道保活（只对声明过要用隧道的副端）---
  if [ ! -f "$DIR/.want_tunnel" ]; then
    return 0
  fi
  if ! alive_tunnel; then
    log "隧道进程不在 → 重建（地址会变）"
    CF_NO_FALLBACK=0 bash "$DIR/mcp-tunnel.sh" >> "$LOG" 2>&1 || log "隧道重建失败"
    sleep 3
  fi

  # --- 3) 记录并上报当前地址 ---
  local url=""
  if [ -f "$DIR/.tunnel_url" ]; then
    url="$(cat "$DIR/.tunnel_url" 2>/dev/null)"
  fi
  if [ -z "$url" ]; then
    url="$(grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' "$DIR/tunnel.log" 2>/dev/null | tail -1)"
  fi
  [ -z "$url" ] && { log "还没拿到隧道地址"; return 0; }

  if [ "$(cat "$CUR" 2>/dev/null)" != "$url" ]; then
    echo "$url" > "$CUR"
    log "地址变更 → $url"
  fi
  report "$url"
}

# 保活总开关：关掉后连 --once 也不做事（systemd timer 同时会被 disable）
if [ -f "$DIR/.no_keepalive" ]; then
  exit 0
fi

if [ "$ONCE" = "1" ]; then
  round
  exit 0
fi

# 常驻模式：单实例 + 每 INTERVAL 秒跑一轮
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  echo "[i] watchdog 已在运行 (pid $(cat "$PIDF"))"
  exit 0
fi
echo $$ > "$PIDF"
trap 'rm -f "$PIDF"; exit 0' INT TERM EXIT
log "watchdog 启动 (pid=$$, interval=${INTERVAL}s, port=$PORT)"
while true; do
  round
  sleep "$INTERVAL"
done
WATCHDOG_SH_EOF

chmod +x "$DIR/_supervisor.sh" "$DIR/mcp-ctl.sh" "$DIR/mcp-tunnel.sh" "$DIR/mcp-watchdog.sh" 2>/dev/null || true

$PY3 -c "import ast,sys; ast.parse(open('$DIR/mcp_agent.py',encoding='utf-8').read())" \
  || die "服务器代码语法校验失败"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="<本机IP>"

# ---------- 保活开关落盘 ----------
# mcp-ctl.sh 是静态脚本（不知道安装参数），用一个标记文件让它知道要不要拉保活守护。
printf '%s\n' "$UNIT" > "$DIR/unit.txt" 2>/dev/null || true
if [ "$KEEPALIVE" = "1" ]; then
  rm -f "$DIR/.no_keepalive" 2>/dev/null || true
else
  touch "$DIR/.no_keepalive" 2>/dev/null || true
fi

# ---------- 清掉可能残留的旧实例 ----------
# 最常见的坑：先用 nohup 模式装过，再用 systemd 模式装（或反过来）。
# 老的 _supervisor.sh 会一直每 3 秒把 mcp_agent.py 拉起来，和新实例抢端口，
# 日志里只看到「Address already in use」反复刷，服务状态却像是好的。
# 所以这里先把老进程清干净，再进入安装/启动。
if [ "$UNINSTALL" != "1" ]; then
  # 先停 systemd 单元（含保活定时器），免得它在我们清理时又把 agent 拉起来
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "${UNIT}-watchdog.timer" >/dev/null 2>&1 || true
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
  fi
  if [ -x "$DIR/mcp-ctl.sh" ]; then
    # 有控制脚本时优先用它（会顺带清 pid 文件、按端口收尾）
    bash "$DIR/mcp-ctl.sh" stop >/dev/null 2>&1 || true
  fi
  # 再兜底：按安装目录精确匹配（不会误伤别的副端，也不会匹配到本安装脚本自己）
  pkill -f "$DIR/_supervisor.sh" 2>/dev/null || true
  pkill -f "$DIR/mcp_agent.py" 2>/dev/null || true
  pkill -f "$DIR/mcp-watchdog.sh" 2>/dev/null || true
  rm -f "$DIR/agent.pid" "$DIR/watchdog.pid" "$DIR/tunnel.pid" 2>/dev/null || true
  sleep 1
fi

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
ExecStart=$PY3 -u $DIR/mcp_agent.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3
StandardOutput=append:$DIR/agent.log
StandardError=append:$DIR/agent.log

[Install]
WantedBy=multi-user.target
UNIT_EOF

  # ---- 保活守护：systemd timer 每 60s 跑一轮 watchdog ----
  # 不用常驻进程，交给 systemd 托管：崩了自动重跑，开机自动启动，零额外依赖。
  if [ "$KEEPALIVE" = "1" ]; then
    log "写入保活单元: /etc/systemd/system/${UNIT}-watchdog.{service,timer}"
    cat > "/etc/systemd/system/${UNIT}-watchdog.service" <<WD_UNIT_EOF
[Unit]
Description=MCP Agent ($NAME) 保活检查
After=${UNIT}.service

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=/bin/bash $DIR/mcp-watchdog.sh --once
TimeoutStartSec=180
WD_UNIT_EOF

    cat > "/etc/systemd/system/${UNIT}-watchdog.timer" <<WD_TIMER_EOF
[Unit]
Description=MCP Agent ($NAME) 保活定时器（每 60s）

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=5s
Unit=${UNIT}-watchdog.service

[Install]
WantedBy=timers.target
WD_TIMER_EOF
  else
    log "已按 --no-keepalive 跳过保活单元注册"
  fi

  systemctl daemon-reload || warn "daemon-reload 失败"
  # 端口若被旧进程占用，先清掉（按端口杀，避免 pkill 关键字误伤）
  "$PY3" "$DIR/_portkill.py" "$PORT" >/dev/null 2>&1 || true
  sleep 1
  systemctl enable "$UNIT" >/dev/null 2>&1 || warn "enable 失败"
  # 重装场景：单元可能本来就在跑，enable --now 不会重启，这里必须 restart 才能加载新代码
  systemctl restart "$UNIT" >/dev/null 2>&1 || warn "restart 失败，请手动 systemctl start $UNIT"
  sleep 2
  STATUS="$(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
  log "服务状态: $STATUS"
  if [ "$STATUS" != "active" ]; then
    warn "服务未激活，最近日志："
    journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null || tail -20 "$DIR/agent.log" 2>/dev/null || true
  fi

  # 保活定时器：先跑一轮（立刻校验一次），再 enable 常驻
  if [ "$KEEPALIVE" = "1" ]; then
    systemctl enable "$UNIT-watchdog.timer" >/dev/null 2>&1 || warn "watchdog timer enable 失败"
    systemctl restart "$UNIT-watchdog.timer" >/dev/null 2>&1 || warn "watchdog timer 启动失败"
    sleep 1
    systemctl start "$UNIT-watchdog.service" >/dev/null 2>&1 || true
    WD_STATE="$(systemctl is-active "$UNIT-watchdog.timer" 2>/dev/null || echo unknown)"
    log "保活定时器: $WD_STATE（每 60s 检查 agent 与隧道）"
    [ "$WD_STATE" = "active" ] || warn "保活定时器未激活，可手动 systemctl start $UNIT-watchdog.timer"
  fi

  VERIFY_CMD="systemctl status $UNIT"
else
  if [ "$SYSTEMD_OK" = "1" ]; then
    log "按参数要求使用守护进程模式（不注册 systemd 服务，用 mcp-ctl.sh 管理）"
  else
    log "本机 PID1 是 $(cat /proc/1/comm 2>/dev/null || echo unknown)，非 systemd（容器环境），使用守护进程模式"
  fi
  # 重装场景：老实例还在跑，直接 start 会跳过去，新代码不生效 → 改用 restart
  if bash "$DIR/mcp-ctl.sh" status >/dev/null 2>&1; then
    warn "检测到旧实例在运行，重启以加载本次安装的代码"
    bash "$DIR/mcp-ctl.sh" restart || true
  else
    bash "$DIR/mcp-ctl.sh" start || true
  fi
  VERIFY_CMD="bash $DIR/mcp-ctl.sh status"
fi

# ---------- 自检 ----------
# 重装时配置里的 token 会保留，所以自检要用「配置里实际生效的 token」，
# 不能只看命令行是否传了 --auth-token，否则会误报自检失败。
EFF_TOKEN="$AUTH_TOKEN"
if [ -z "$EFF_TOKEN" ] && [ -f "$DIR/mcp_agent_config.json" ]; then
  EFF_TOKEN="$($PY3 -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"
fi
sleep 1
if command -v curl >/dev/null 2>&1; then
  if [ -n "$EFF_TOKEN" ]; then
    HEALTH_CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $EFF_TOKEN" "http://127.0.0.1:${PORT}/" || true)"
    HEALTH="$(curl -s -m 5 -H "Authorization: Bearer $EFF_TOKEN" "http://127.0.0.1:${PORT}/" || true)"
  else
    HEALTH_CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
    HEALTH="$(curl -s -m 5 "http://127.0.0.1:${PORT}/" || true)"
  fi
  if [ "$HEALTH_CODE" = "200" ]; then
    log "本机自检通过: $HEALTH"
  else
    warn "本机自检失败（HTTP ${HEALTH_CODE:-无响应}），请查看 $DIR/agent.log"
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

AUTH_TOK="$($PY3 -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"

cat <<EOF

============================================================================
 安装完成！
----------------------------------------------------------------------------
 副端标识 : $NAME
 监听端口 : $PORT
 安装目录 : $DIR
 运行模式 : $MODE$([ "$MODE" = "systemd" ] && echo "（服务单元 ${UNIT}.service）" || echo "（守护进程，用 mcp-ctl.sh 管理）")
 鉴权     : $(if [ -n "$AUTH_TOK" ]; then echo "已开启（Bearer token）"; else echo "未开启 —— 仅限内网；开公网隧道前请先设 auth_token"; fi)
 状态查看 : $VERIFY_CMD
 实时日志 : tail -f $DIR/agent.log
 重启服务 : $(if [ "$MODE" = "systemd" ]; then echo "systemctl restart $UNIT"; else echo "bash $DIR/mcp-ctl.sh restart"; fi)
 控制脚本 : bash $DIR/mcp-ctl.sh {start|stop|restart|status|log|tunnel}
 手动前台 : $PY3 -u $DIR/mcp_agent.py --host 0.0.0.0 --port $PORT

----------------------------------------------------------------------------
 在主端（WorkBuddy 所在电脑）的 ~/.workbuddy/mcp.json 里加这一段：

  "mcpServers": {
    "$NAME": {
      "type": "http",
      "url": "$MCP_URL"$(if [ -n "$AUTH_TOK" ]; then echo ","; fi)
$(if [ -n "$AUTH_TOK" ]; then echo "      \"headers\": { \"Authorization\": \"Bearer $AUTH_TOK\" }"; fi)
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

# ---------------------------------------------------------------------------
# 未设令牌时的加固提示（v3.1）
# 审计实测：内网安装默认不带 token，等于把 root shell 对同网段裸奔。
# ---------------------------------------------------------------------------
if [ -z "$AUTH_TOK" ]; then
cat <<NOAUTH_HINT

============================================================================
 [!] 本次安装**没有设置 auth_token**
     任何能访问 $PORT 端口的人都能以 root 身份操作这台设备（同网段里的其它
     设备、被挂马的网页都能打进来）。"在内网所以没事"是不成立的。

 一条命令加固（生成强随机令牌 → 写入配置 → 重启）：
   bash $DIR/mcp-ctl.sh stop; \\
   python3 -c "import json,secrets;p='$DIR/mcp_agent_config.json';d=json.load(open(p));d['auth_token']=secrets.token_urlsafe(32);d['require_auth']=True;json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)"; \\
   chmod 600 "$DIR/mcp_agent_config.json"; bash $DIR/mcp-ctl.sh start; \\
   python3 -c "import json;print('token =',json.load(open('$DIR/mcp_agent_config.json'))['auth_token'])"

 拿到令牌后，在**主端** mcp.json 的 "$NAME" 条目里补上：
   "headers": { "Authorization": "Bearer <令牌>" }
 再重启 WorkBuddy。漏了 headers 的话，加完令牌主端就会一直收到 401。

 或者重装时直接带上参数： --generate-token --require-auth
============================================================================
NOAUTH_HINT
fi
'''

content = TEMPLATE.replace("__AGENT_SOURCE__", agent_src)
with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)

print("已生成: %s" % OUT)
print("大小: %.1f KB" % (len(content.encode("utf-8")) / 1024.0))
print("内嵌 agent 行数: %d" % len(agent_src.splitlines()))
