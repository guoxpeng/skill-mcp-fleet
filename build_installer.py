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
__AGENT_SOURCE__
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

# ---- 复用已有隧道：避免撞 trycloudflare 创建频率限制，也让公网地址保持稳定 ----
URL=""
if [ "${CF_FORCE:-0}" != "1" ] && [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  URL="$(grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1)"
  if [ -n "$URL" ]; then
    echo "[i] 已有隧道在运行（pid $(cat "$PIDF")），复用既有地址，不新建。"
    echo "[i] 要强制重建：CF_FORCE=1 bash $DIR/mcp-tunnel.sh"
    echo "[i] 要改协议：  kill \$(cat $PIDF); CF_PROTO=http2 bash $DIR/mcp-tunnel.sh"
  fi
fi

if [ -z "$URL" ]; then
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
  echo "       → 本脚本默认复用已有隧道，正常不会撞上；万一是被限流，等 10~30 分钟再"
  echo "         用 CF_FORCE=1 bash $DIR/mcp-tunnel.sh 重建。"
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
'''

content = TEMPLATE.replace("__AGENT_SOURCE__", agent_src)
with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)

print("已生成: %s" % OUT)
print("大小: %.1f KB" % (len(content.encode("utf-8")) / 1024.0))
print("内嵌 agent 行数: %d" % len(agent_src.splitlines()))
