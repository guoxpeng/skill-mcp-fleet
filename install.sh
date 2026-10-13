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
#      --tunnel           装完顺便起公网隧道（云容器/无公网入口时用）
#      --tunnel-backend <b> 隧道后端：cf（默认，cloudflared 快隧）|
#                          frp（经你 VPS 的 frps，地址恒定）|
#                          ssh（autossh 反向隧道经你 VPS，地址恒定）
#      --frp-server <host> --frp-port <p> --frp-token <串> --frp-remote-port <p>
#                          frp 后端参数（VPS 地址 / frps 端口默认 7000 /
#                          与 frps 一致的 token / VPS 上暴露的端口默认 13100）
#      --ssh-dest <user@host> --ssh-port <p> --ssh-remote-port <p>
#                          ssh 后端参数（你的 VPS / SSH 端口默认 22 /
#                          VPS 上暴露的端口默认 13100；VPS 需开 GatewayPorts yes）
#      --print-frps        只打印 VPS 端 frps 的一键配置，不安装
#      --require-auth      把「必须鉴权」写进配置：没有 auth_token 时副端拒绝启动
#                          （公网/隧道场景建议加；内网也推荐）
#      --generate-token    若还没有 auth_token，自动生成一个强随机值写进配置
#      --uninstall         卸载（停服务 + 删单元 + 删目录）
#
#  依赖：python3 + curl（只用标准库，无需 pip）。脚本会自动识别
#        apt-get/dnf/yum/apk/pacman/pkg（Termux）并安装缺失的依赖。
#        systemd 有则用服务托管；没有（Docker/容器/Termux）自动降级为
#        守护进程 + mcp-ctl.sh 控制脚本，功能一致，只是开机不自启。
# ============================================================================
set -euo pipefail

# Termux 的环境变量 $PREFIX 会被下面的 PREFIX=""（工具名前缀参数）清掉，先存下来
_TERMUX_PREFIX="${PREFIX:-}"

NAME=""
PORT="3100"
DIR=""
PREFIX=""
SUDO_PASS=""
UNINSTALL="0"
MODE="auto"
DO_TUNNEL="0"
TUNNEL_BACKEND="cf"       # cf | frp | ssh
FRP_SERVER=""
FRP_PORT="7000"
FRP_TOKEN=""
FRP_REMOTE_PORT="13100"
SSH_DEST=""               # user@host（你的 VPS）
SSH_PORT="22"
SSH_REMOTE_PORT="13100"
PRINT_FRPS="0"
AUTH_TOKEN=""
ALLOW_IPS=""
ALLOW_IPS_JSON="[]"
DIRECTORY_URL=""
DIRECTORY_TOKEN=""
KEEPALIVE="1"
REQUIRE_AUTH="0"
GEN_TOKEN="0"

# 先存一份原始参数：下面的解析会把 $@ shift 光，非 root 自动 sudo 重执行时要用
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --name)       NAME="${2:-}"; shift 2 ;;
    --port)       PORT="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --prefix)     PREFIX="${2:-}"; shift 2 ;;
    --sudo-pass)  SUDO_PASS="${2:-}"; shift 2 ;;
    --mode)       MODE="${2:-}"; shift 2 ;;
    --tunnel)     DO_TUNNEL="1"; shift ;;
    --tunnel-backend) TUNNEL_BACKEND="${2:-cf}"; shift 2 ;;
    --frp-server) FRP_SERVER="${2:-}"; shift 2 ;;
    --frp-port)   FRP_PORT="${2:-7000}"; shift 2 ;;
    --frp-token)  FRP_TOKEN="${2:-}"; shift 2 ;;
    --frp-remote-port) FRP_REMOTE_PORT="${2:-13100}"; shift 2 ;;
    --ssh-dest)   SSH_DEST="${2:-}"; shift 2 ;;
    --ssh-port)   SSH_PORT="${2:-22}"; shift 2 ;;
    --ssh-remote-port) SSH_REMOTE_PORT="${2:-13100}"; shift 2 ;;
    --print-frps) PRINT_FRPS="1"; shift ;;
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

# --print-frps：只打印 VPS 端 frps 的配置与启动命令，不安装
if [ "$PRINT_FRPS" = "1" ]; then
  FRPS_TOK="${FRP_TOKEN:-$(python3 -c 'import secrets;print(secrets.token_hex(16))' 2>/dev/null || echo 'CHANGE_ME')}"
  cat <<FRPS_EOF
# ============ 在你的 VPS 上执行（一次即可） ============
# 1) 下载 frps（按架构选其一）
#    x86_64: curl -fsSL https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_amd64.tar.gz | tar xz
#    aarch64: curl -fsSL https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_arm64.tar.gz | tar xz
# 2) 写配置 /etc/frps.ini：
cat > /etc/frps.ini <<'INI'
[common]
bind_port = ${FRP_PORT:-7000}
token = ${FRPS_TOK}
INI
chmod 600 /etc/frps.ini
# 3) 放行防火墙：${FRP_PORT:-7000}/tcp 与 ${FRP_REMOTE_PORT:-13100}/tcp
# 4) 启动（systemd）：
cat > /etc/systemd/system/frps.service <<'SVC'
[Unit]
Description=frp server
After=network.target
[Service]
ExecStart=/usr/local/bin/frps -c /etc/frps.ini
Restart=always
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload && systemctl enable --now frps
# 5) 副端安装命令（token 已填好）：
#    bash install.sh --name <名字> --port 3100 --tunnel --tunnel-backend frp \\
#      --frp-server <VPS地址> --frp-port ${FRP_PORT:-7000} --frp-token ${FRPS_TOK} \\
#      --frp-remote-port ${FRP_REMOTE_PORT:-13100} --generate-token --require-auth
# ============ 结束 ============
FRPS_EOF
  exit 0
fi

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
# ---------- Stage 0：平台识别 ----------
# Termux（Android）：uname -o 返回 Android，或 $PREFIX/bin/pkg 存在
is_termux() {
  [ "$(uname -o 2>/dev/null || echo '')" = "Android" ] && return 0
  [ -n "${_TERMUX_PREFIX:-}" ] && [ -x "$_TERMUX_PREFIX/bin/pkg" ] && return 0
  return 1
}
if is_termux; then
  echo "[+] 检测到 Termux（Android），启用手机模式"
  # root 和普通用户分目录：root 用 $PREFIX/opt（整机权限），普通用户用家目录（沙盒权限），互不打架
  if [ -z "$DIR" ]; then
    if [ "$(id -u 2>/dev/null || echo 999)" = "0" ]; then
      DIR="${_TERMUX_PREFIX:-/data/data/com.termux/files/usr}/opt/${NAME}_mcp"
    else
      DIR="$HOME/.fleet/${NAME}_mcp"
    fi
  fi
  # 手机上 systemd 不存在，强制 nohup 模式
  if [ "$MODE" = "auto" ]; then MODE="nohup"; fi
else
  if [ -z "$DIR" ]; then DIR="/opt/${NAME}_mcp"; fi
fi
# 目录已存在但写不进（比如之前 root 建的），给一句人话而不是 Permission denied
if [ -e "$DIR" ] && [ ! -w "$DIR" ]; then
  printf '\033[31m[x]\033[0m 目录 %s 已存在但当前用户写不进去（可能是之前用 root/tsu 建的）。\n' "$DIR" >&2
  printf '    解决：用 tsu 重跑，或删掉重来：rm -rf %s\n' "$DIR" >&2
  exit 1
fi
UNIT="${NAME}-mcp"

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 参数占位符拦截 ----------
# 有人会照抄文档里的 <名字> / <你的VPS>，bash 会把 < > 当重定向，
# 在这里提前拦下来给一句人话，而不是报莫名其妙的 "No such file"。
for _pv in NAME DIR SSH_DEST FRP_SERVER FRP_TOKEN DIRECTORY_URL AUTH_TOKEN; do
  _vv="${!_pv}"
  case "$_vv" in
    "<"*">"|*"<"*)
      _pn="$_pv"; [ "$_pv" = "NAME" ] && _pn="name"
      die "参数 $_pn 的值 '$_vv' 像个占位符：请换成真实值（把 < > 去掉）。例如：--name phone   --ssh-dest root@38.64.56.15" ;;
  esac
done
# SSH_DEST 格式顺手验一下
if [ -n "$SSH_DEST" ]; then
  case "$SSH_DEST" in
    *"@"*) ;;
    *) die "--ssh-dest '$SSH_DEST' 格式不对，应为 user@host，例如 --ssh-dest root@38.64.56.15" ;;
  esac
fi
# ---------- 权限 ----------
if [ "$(id -u)" -ne 0 ]; then
  if is_termux; then
    warn "当前非 root：agent 只能操作 Termux 沙盒内的文件。如需整机权限，请先 pkg install tsu 再用 tsu 运行本脚本。"
  elif [ -r "$0" ] && command -v sudo >/dev/null 2>&1; then
    echo "[i] 需要 root，自动用 sudo 重新执行"
    # 原来这里用 "$@"，但参数早已被 shift 空 → sudo 重执行后 --name/--port 全丢，
    # 装成了默认名、默认端口（3100），和主端配置对不上，表现为「连不上/掉线」
    exec sudo -E bash "$0" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
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
  for _fb in /usr/local/bin/fleet "${_TERMUX_PREFIX:-/nonexistent}/bin/fleet"; do
    grep -q "$DIR/fleet.py" "$_fb" 2>/dev/null && rm -f "$_fb"
  done
  rm -rf "$DIR"
  log "已卸载（目录 $DIR 已删除）"
  exit 0
fi

# ---------- Stage 0：依赖自举（自动识别包管理器并安装） ----------
detect_pm() {
  # 输出包管理器：pkg（Termux）/ apt-get / dnf / yum / apk / pacman，或空
  if is_termux && command -v pkg >/dev/null 2>&1; then echo pkg; return; fi
  for _pm in apt-get dnf yum apk pacman; do
    if command -v "$_pm" >/dev/null 2>&1; then echo "$_pm"; return; fi
  done
  echo ""
}
pm_install() {  # $1=包管理器 $2..=包名；返回 0 表示成功
  # 全程非交互：用户跑一键脚本就是同意安装，不弹任何确认/配置文件选择
  local _pm="$1"; shift
  case "$_pm" in
    pkg)     DEBIAN_FRONTEND=noninteractive pkg install -y -o Dpkg::Options::="--force-confold" "$@" >/dev/null 2>&1 ;;
    apt-get) run_priv env DEBIAN_FRONTEND=noninteractive "$_pm" update -qq >/dev/null 2>&1
             run_priv env DEBIAN_FRONTEND=noninteractive "$_pm" install -y -qq \
               -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" "$@" >/dev/null 2>&1 ;;
    dnf|yum) run_priv "$_pm" install -y -q "$@" >/dev/null 2>&1 ;;
    apk)     run_priv "$_pm" add --no-cache "$@" >/dev/null 2>&1 ;;
    pacman)  run_priv "$_pm" -Sy --noconfirm "$@" >/dev/null 2>&1 ;;
    *)       return 1 ;;
  esac
}
# 包管理器升级也压掉交互（修 curl 这类依赖错位时用）
pm_upgrade() {
  local _pm="$1"
  case "$_pm" in
    pkg)     DEBIAN_FRONTEND=noninteractive pkg upgrade -y -o Dpkg::Options::="--force-confold" >/dev/null 2>&1 ;;
    apt-get) run_priv env DEBIAN_FRONTEND=noninteractive "$_pm" upgrade -y -qq \
               -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" >/dev/null 2>&1 ;;
    *)       return 1 ;;
  esac
}
# run_priv：root 直接跑，非 root 试 sudo，Termux 下 pkg 不需要提权
run_priv() {
  if [ "$(id -u)" = "0" ]; then "$@"
  elif command -v sudo >/dev/null 2>&1; then sudo "$@"
  else "$@"; fi
}
ensure_cmd() {  # $1=命令名 $2=包名(apt/dnf/yum/apk/pacman) $3=包名(termsux pkg)
  local _cmd="$1" _pkg="$2" _tpkg="${3:-$2}"
  command -v "$_cmd" >/dev/null 2>&1 && return 0
  local _pm; _pm="$(detect_pm)"
  [ -z "$_pm" ] && { warn "未找到 $_cmd，且识别不出包管理器，请手动安装"; return 1; }
  log "未找到 $_cmd，用 $_pm 自动安装…"
  if [ "$_pm" = "pkg" ]; then pm_install "$_pm" "$_tpkg" || pm_install "$_pm" "$_pkg"; else pm_install "$_pm" "$_pkg"; fi
  command -v "$_cmd" >/dev/null 2>&1 && { log "$_cmd 安装成功"; return 0; }
  warn "$_cmd 自动安装失败，请手动安装后重试"
  return 1
}
PM="$(detect_pm)"
log "包管理器: ${PM:-未知}"
ensure_cmd python3 python3 python || die "没有 python3，agent 跑不起来"
ensure_cmd curl curl curl || warn "没有 curl：隧道下载与自检会受影响"
# ---- curl 能跑≠好用：Termux 常见 curl 与 nghttp2 版本错位导致链接失败 ----
# 先真跑一次，坏了就地修，修不好就全程用 python urllib 兜底下载。
CURL_OK=1
if command -v curl >/dev/null 2>&1; then
  if ! curl --version >/dev/null 2>&1; then
    warn "curl 已损坏（常见于 Termux 依赖错位），尝试修复…"
    if [ "$PM" = "pkg" ] || [ "$PM" = "apt-get" ]; then
      log "执行包管理器升级同步依赖（全自动，不弹确认）…"
      pm_upgrade "$PM" || true
    else
      pm_install "$PM" curl >/dev/null 2>&1 || true
    fi
    curl --version >/dev/null 2>&1 || CURL_OK=0
  fi
else
  CURL_OK=0
fi
[ "$CURL_OK" = "1" ] || warn "curl 不可用，下载改用 python3 urllib 兜底"
# dl <url> <输出文件>：curl 优先，坏了自动切 python
dl() {
  if [ "$CURL_OK" = "1" ]; then
    curl -fsSL --connect-timeout 12 -o "$2" "$1" && return 0
  fi
  "$PY3" -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2" 2>/dev/null
}
if [ "$TUNNEL_BACKEND" = "ssh" ]; then
  ensure_cmd ssh openssh-client openssh || die "ssh 后端需要 ssh 客户端"
  command -v autossh >/dev/null 2>&1 || ensure_cmd autossh autossh autossh || warn "没装上 autossh，将用 ssh + 保活参数代替（断线重连稍弱）"
fi
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
    "version": "3.5.0",
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
    # ---- 稳定性（v3.4）----------------------------------------------------
    # 单连接空闲多少秒后断开。原来没有超时：隧道/NAT 留下的半开连接会永久占住线程，
    # 日积月累把 agent 拖到无响应（进程还在，保活守护看不出来）。
    "socket_timeout": 300,
    # ---- 对等互控（v3.5）--------------------------------------------------
    # 不再区分主端/副端：每个节点既能被连，也能通过 peer_* 工具去连别的节点。
    "peer_tools": True,         # False = 不暴露 peers/peer_call/peer_add/peer_remove
    "peer_max_hops": 3,         # A→B→C… 最多转几跳，防止互相转发成环
    "self_url": "",             # 别的节点回连本机用的地址；留空=自动（隧道地址 > 内网 IP）
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
# 版本以代码为准：配置文件里的 version 是安装当时写的，重装时不覆盖，会显示成旧版本
AGENT_VERSION = DEFAULT_CONFIG["version"]
SERVER_VERSION = AGENT_VERSION
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
        # POSIX 下放进独立进程组：超时时整组杀掉。原来只杀 bash，
        # 它拉起的子孙进程（如 `ping`、`tail -f`、后台服务）会成孤儿继续跑，
        # 还可能攥着管道让 communicate 迟迟不返回，请求线程被拖住。
        p = subprocess.Popen(
            argv,
            cwd=wd if os.path.isdir(wd) else None,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=not _is_windows(),
        )
        out, err = p.communicate(
            input=stdin.encode("utf-8") if stdin is not None else None,
            timeout=t)
        return (p.returncode, _decode_output(out), _decode_output(err))
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
            return "[exit 0] 写入成功: " + p, False
        except Exception as e:
            return "[exit 1]\n[写入失败] %r" % (e,), True
    op = ">>" if a.get("mode", "overwrite") == "append" else ">"
    cmd = "cat " + op + " " + shlex.quote(p)
    code, out, err = run_local(cmd, sudo=bool(a.get("sudo")), stdin=a.get("content", ""))
    if code == 0:
        return "[exit 0] 写入成功: " + p, False
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
        open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
        return "[exit 0]\nOK", False
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
# 对等互控（v3.5）：节点之间不分主副，任何一台都能经 MCP 调用另一台
# ---------------------------------------------------------------------------
# 对等节点表与本机 fleet 命令共用同一个文件：<安装目录>/peers.json（0600）
PEERS_PATH = os.path.join(HERE, "peers.json")
PEERS_LOCK = threading.Lock()
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


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def self_url():
    """别的节点回连本机的地址：配置 self_url > 当前隧道地址 > 内网 IP:端口。"""
    u = (CFG.get("self_url") or "").strip()
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
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "X-Fleet-Hops": str(hops),
                                          "X-Fleet-From": SERVER_NAME})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        with _peer_opener(url).open(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id") or sid
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise ValueError("对端返回 HTTP %s %s%s" % (e.code, e.reason,
                         "（令牌不对或本机 IP 不在对端 allow_ips 里）" if e.code in (401, 403) else ""))
    except Exception as e:
        raise ValueError("连不上 %s：%s" % (url, getattr(e, "reason", e)))
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
                    req.add_header("Authorization", "Bearer " + v["token"])
                with _peer_opener(base).open(req, timeout=5) as r:
                    st = "  在线" if r.status == 200 else "  HTTP %s" % r.status
            except Exception as e:
                st = "  不通(%s)" % (getattr(e, "reason", None) or type(e).__name__)
        lines.append("- %s  %s  %s%s" % (n, v.get("url"), "带令牌" if v.get("token") else "无令牌", st))
    return "\n".join(lines), False


def t_peer_call(a):
    name, v = _peer_entry(a["peer"])
    if _norm_mcp_url(v.get("url")) == self_url():
        return "目标就是本机，直接调本机工具即可", True
    args = a.get("arguments") or {}
    if isinstance(args, str):
        args = json.loads(args or "{}")
    t = int(a.get("timeoutMs") or 130000) / 1000.0
    text, is_err = peer_call(_norm_mcp_url(v["url"]), v.get("token", ""), a["tool"], args, t)
    return "[%s → %s] %s\n%s" % (SERVER_NAME, name, a["tool"], text), is_err


def t_peer_add(a):
    name = (a.get("name") or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", name):
        return "name 只能是字母数字和 _ . -（1~64 位）", True
    url = _norm_mcp_url(a["url"])
    with PEERS_LOCK:
        d = load_peers()
        old = d["nodes"].get(name)
        d["nodes"][name] = {"url": url, "token": (a.get("token") or "").strip(),
                            "added_by": a.get("_from") or "", "updated": int(time.time())}
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
    """配对用：告诉对方「回连我用哪个地址」。令牌不在这里返回（对方本来就持有）。"""
    return json.dumps({"name": SERVER_NAME, "url": self_url(), "version": SERVER_VERSION,
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
]

BASE_FUNCS = {
    "exec": t_exec, "read": t_read, "write": t_write, "edit": t_edit,
    "list_dir": t_list_dir, "docker_ps": t_docker_ps, "docker_logs": t_docker_logs,
    "docker_restart": t_docker_restart, "systemctl": t_systemctl,
    "service_logs": t_service_logs, "sysinfo": t_sysinfo, "http_get": t_http_get,
    "exec_async": t_exec_async, "exec_poll": t_exec_poll,
    "exec_result": t_exec_result, "exec_kill": t_exec_kill,
    "exec_jobs": t_exec_jobs,
}
if CFG.get("peer_tools", True):
    BASE_TOOLS += PEER_TOOLS
    BASE_FUNCS.update(PEER_FUNCS)

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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        # 空闲超时：读不到下一个请求就关连接，防半开连接永久占线程
        self.timeout = float(CFG.get("socket_timeout", 300) or 300)
        super().setup()
        _enable_tcp_keepalive(self.connection)

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, socket.timeout, TimeoutError, OSError):
            # 客户端断开/超时属正常现象，静默收尾，别把 traceback 刷进日志
            self.close_connection = True

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

    global LISTEN_PORT
    LISTEN_PORT = args.port
    httpd = FleetHTTPServer((args.host, args.port), Handler)
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
    except Exception as e:
        # 主循环意外退出：以非零码退出，交给 systemd Restart=always / 守护壳重启
        print("[FATAL] 服务主循环异常退出: %r" % (e,), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

MCP_AGENT_PY_EOF

# ---------- v3.5：对等互控 —— 本节点也带控制端，装成 `fleet` 命令 ----------
# 节点目录里的 fleet.py 与 agent 共用 $DIR/peers.json：
#   在本机 `fleet pair/exec` 登记的对方，AI 经 MCP 调 peers/peer_call 也能用，反之亦然。
cat > "$DIR/fleet.py" <<'FLEET_PY_EOF'
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
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


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
    url = (self_url or cfg.get("self_url") or _read("current_url.txt") or "").strip()
    if not url:
        url = "http://%s:%s" % (_lan_ip(), _read("port.txt") or "3100")
    url = url.rstrip("/")
    if not re.search(r"/(mcp|sse)$", url):
        url += "/mcp"
    return {"name": cfg.get("name") or socket.gethostname(), "url": url,
            "token": cfg.get("auth_token") or "", "dir": d}


def cmd_pair(a):
    """双向配对：把对方登记到本机，再让对方把本机登记过去，最后从对方回连本机验证。"""
    url, meta = resolve_target(a.target, port=a.port)
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

    try:
        text, is_err = c.call("peer_add", {"name": me["name"], "url": me["url"], "token": me["token"]},
                              timeout=a.timeout)
    except MCPError as e:
        text, is_err = str(e), True
    if is_err:
        print(err("[x] ") + "对方登记本机失败：%s" % text.strip()[:300])
        if "未知工具" in text:
            print("    对方节点版本太旧（< v3.5），先在对方重跑一次安装命令升级。")
        return 2
    print(ok("[+] ") + "%s → 本机：%s" % (rname, text.strip()))

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
    print(ok("[+] ") + "双向验证通过：%s 能控制本机，本机也能控制 %s" % (rname, rname))
    print(dim("    本机控制对方： fleet exec %s 'uptime'" % rname))
    print(dim("    对方控制本机： 在 %s 上 fleet exec %s 'uptime'，或经 MCP 调 peer_call" % (rname, me["name"])))
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

    sp = sub.add_parser("pair", help="双向配对：互相登记，之后两边都能控制对方（v3.5）")
    sp.add_argument("target", help="对方地址 http(s)://ip:3100/mcp，或已登记的名字")
    sp.add_argument("--token", help="对方的 auth_token（对方开了鉴权必填）")
    sp.add_argument("--name", help="给对方起的名字（默认用对方自报的 name）")
    sp.add_argument("--self-url", help="对方回连本机用的地址（默认：本机隧道地址 > 内网 IP:端口）")
    sp.add_argument("--one-way", action="store_true", help="只把对方登记到本机，不让对方反向登记")
    sp.add_argument("--port", type=int, default=3100)
    sp.add_argument("--timeout", type=int, default=20)
    sp.set_defaults(func=cmd_pair)

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

FLEET_PY_EOF
FLEET_BIN=""
if is_termux; then
  FLEET_BIN="${_TERMUX_PREFIX:-/data/data/com.termux/files/usr}/bin/fleet"
elif [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
  FLEET_BIN="/usr/local/bin/fleet"
fi
if [ -n "$FLEET_BIN" ] \
   && printf '#!/usr/bin/env bash\n# mcp-fleet 节点 %s 的控制端（install.sh 生成）\nexec "%s" "%s/fleet.py" "$@"\n' \
        "$NAME" "$PY3" "$DIR" > "$FLEET_BIN" 2>/dev/null; then
  chmod +x "$FLEET_BIN" 2>/dev/null || true
  log "控制端命令: fleet（$FLEET_BIN → $DIR/fleet.py）"
else
  FLEET_BIN=""
  warn "没装上 fleet 命令（无写权限），用 $PY3 $DIR/fleet.py 代替"
fi

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
  "version": "3.5.0",
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
d["version"] = "3.5.0"
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
  # Termux：申请唤醒锁，防止 Android 息屏/省电时冻结或杀掉后台进程（手机副端掉线主因）
  if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock >/dev/null 2>&1 || true
  fi
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
cat > "$DIR/mcp-tunnel-cf.sh" <<'TUN_CF_EOF'
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
# 下载兜底：curl 坏了就用 python urllib
tunnel_dl() {
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 12 -o "$2" "$1" && return 0
  fi
  "$PY" -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2" 2>/dev/null
}
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
    if tunnel_dl "$M" "$BIN"; then
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
TUN_CF_EOF
# ============================================================================
# 隧道分发器 + frp/ssh 后端（v3.3）
# ----------------------------------------------------------------------------
# mcp-tunnel.sh 按 $DIR/.tunnel_backend 选择后端：cf | frp | ssh（默认 cf）。
# 各后端共用约定：pid 记 tunnel.pid、日志 tunnel.log、
# 公网地址写 .tunnel_url 与 current_url.txt（保活守护与主端 sync 都读它）。
# ============================================================================
cat > "$DIR/mcp-tunnel.sh" <<'TUN_DISP_EOF'
#!/usr/bin/env bash
# 隧道分发器：按 .tunnel_backend 选择后端（cf | frp | ssh），默认 cf。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cat "$DIR/.tunnel_backend" 2>/dev/null || echo cf)"
case "$BACKEND" in
  frp) exec bash "$DIR/mcp-tunnel-frp.sh" "$@" ;;
  ssh) exec bash "$DIR/mcp-tunnel-ssh.sh" "$@" ;;
  cf|"") exec bash "$DIR/mcp-tunnel-cf.sh" "$@" ;;
  *) echo "[x] 未知隧道后端: $BACKEND（.tunnel_backend 可选 cf | frp | ssh）" >&2; exit 1 ;;
esac
TUN_DISP_EOF

# ----------------------------------------------------------------------------
# frp 后端：经你自己的 VPS（frps）把 agent 端口映射出去。
# 需要安装参数：--frp-server <VPS地址> --frp-token <与frps一致> [--frp-port 7000] [--frp-remote-port 13100]
# VPS 端一键配置：bash install.sh --print-frps
# 地址恒定：http://<VPS>:<remote-port>，不受 cf 限流影响。
# ----------------------------------------------------------------------------
cat > "$DIR/mcp-tunnel-frp.sh" <<'TUN_FRP_EOF'
#!/usr/bin/env bash
# frp 后端：把本机 agent 端口经自建 frps（你的 VPS）暴露到公网。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
BIN="$DIR/frpc"
LOG="$DIR/tunnel.log"
PIDF="$DIR/tunnel.pid"
PY="$(command -v python3 || echo /usr/bin/python3)"
[ -f "$DIR/.tunnel_env" ] && . "$DIR/.tunnel_env"
# 下载兜底：curl 坏了就用 python urllib
tunnel_dl() {
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 12 -o "$2" "$1" && return 0
  fi
  "$PY" -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2" 2>/dev/null
}
NAME="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json'))['name'])" 2>/dev/null || echo node)"

# ---- 安全闸门：开公网隧道前必须有鉴权（与 cf 后端同逻辑） ----
TOKEN="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"
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
  echo "[+] 已为公网隧道生成鉴权 token，写入 mcp_agent_config.json 并重启副端 ..."
  if [ -f "/etc/systemd/system/${NAME}-mcp.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart "${NAME}-mcp" >/dev/null 2>&1 || true
  else
    bash "$DIR/mcp-ctl.sh" restart >/dev/null 2>&1 || true
  fi
  sleep 2
fi

# ---- 参数 ----
[ -n "${FRP_SERVER:-}" ] || { echo "[x] 缺少 frp 服务器地址。用 --frp-server 指定，或改 .tunnel_env"; exit 1; }
[ -n "${FRP_TOKEN:-}" ] || { echo "[x] 缺少 frp token。用 --frp-token 指定（与 VPS 上 frps.ini 的 token 一致）"; exit 1; }
FRP_PORT="${FRP_PORT:-7000}"
FRP_REMOTE_PORT="${FRP_REMOTE_PORT:-13100}"

# ---- 下载 frpc ----
if [ ! -x "$BIN" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) FRP_ARCH="amd64" ;;
    aarch64|arm64) FRP_ARCH="arm64" ;;
    *) echo "[x] 不支持的 CPU 架构: $ARCH"; exit 1 ;;
  esac
  FRP_VER="0.61.1"
  TGZ="/tmp/frp_${FRP_VER}_linux_${FRP_ARCH}.tar.gz"
  echo "[+] 下载 frpc v$FRP_VER ($FRP_ARCH) ..."
  ok=0
  for M in \
    "https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_${FRP_ARCH}.tar.gz" \
    "https://ghfast.top/https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_${FRP_ARCH}.tar.gz" \
    "https://ghproxy.net/https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_${FRP_ARCH}.tar.gz" ; do
    if tunnel_dl "$M" "$TGZ"; then ok=1; break; fi
    echo "[!] 通道失败，试下一个"
  done
  [ "$ok" = "1" ] || { echo "[x] frpc 下载失败，请手动放到 $BIN"; exit 1; }
  tar xzf "$TGZ" -C /tmp || { echo "[x] 解压失败"; exit 1; }
  cp "/tmp/frp_${FRP_VER}_linux_${FRP_ARCH}/frpc" "$BIN" && chmod +x "$BIN"
  rm -rf "$TGZ" "/tmp/frp_${FRP_VER}_linux_${FRP_ARCH}"
fi

# ---- 写 frpc.ini ----
cat > "$DIR/frpc.ini" <<FRPC_EOF
[common]
server_addr = ${FRP_SERVER}
server_port = ${FRP_PORT}
token = ${FRP_TOKEN}

[mcp-${NAME}]
type = tcp
local_ip = 127.0.0.1
local_port = ${PORT}
remote_port = ${FRP_REMOTE_PORT}
FRPC_EOF
chmod 600 "$DIR/frpc.ini"

# ---- 启动 ----
touch "$DIR/.want_tunnel" 2>/dev/null || true
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  echo "[i] frp 隧道已在运行（pid $(cat "$PIDF")），复用。"
else
  [ -f "$PIDF" ] && { kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"; }
  : > "$LOG"
  setsid nohup "$BIN" -c "$DIR/frpc.ini" >> "$LOG" 2>&1 < /dev/null &
  sleep 2
  REAL="$(pgrep -f "$BIN -c $DIR/frpc.ini" 2>/dev/null | head -1 || true)"
  if [ -n "$REAL" ]; then echo "$REAL" > "$PIDF"; else echo $! > "$PIDF"; fi
fi

URL="http://${FRP_SERVER}:${FRP_REMOTE_PORT}"
printf '%s' "$URL" > "$DIR/.tunnel_url" 2>/dev/null || true
printf '%s' "$URL" > "$DIR/current_url.txt" 2>/dev/null || true
# 同步写进 agent 配置的 tunnel_url（主端 sync 用）
"$PY" - "$DIR/mcp_agent_config.json" "$URL" <<'URL_PY_EOF' 2>/dev/null || true
import json, sys
p, url = sys.argv[1], sys.argv[2]
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["tunnel_url"] = url
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
URL_PY_EOF

# ---- 自检 ----
HTTP_CODE=""
if [ -n "$TOKEN" ]; then
  HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$URL/" 2>/dev/null || true)"
else
  HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$URL/" 2>/dev/null || true)"
fi

echo
echo "============================================================"
echo " frp 隧道地址：$URL  （经你的 VPS ${FRP_SERVER}，地址恒定）"
echo " MCP 端点    ：$URL/mcp"
if [ -n "$TOKEN" ]; then echo " 鉴权 token  ：$TOKEN"; else echo " 鉴权 token  ：(未设置 —— 公网裸奔，请尽快补上)"; fi
echo "------------------------------------------------------------"
echo " 公网访问    ：${HTTP_CODE:-未检测}$([ "$HTTP_CODE" = "200" ] && echo " (通)" || echo " (不通：检查 VPS 防火墙是否放行 ${FRP_REMOTE_PORT}/tcp，frps 是否在跑)")"
echo "------------------------------------------------------------"
echo " 主端 ~/.workbuddy/mcp.json 增加："
echo
echo "   "$NAME": {"
echo "     "type": "http","
echo "     "url": "$URL/mcp"$([ -n "$TOKEN" ] && printf ',')"
if [ -n "$TOKEN" ]; then
  echo "     "headers": { "Authorization": "Bearer $TOKEN" }"
fi
echo "   }"
echo "============================================================"
echo " 停止隧道：kill \$(cat "$PIDF")      日志：$LOG"
TUN_FRP_EOF

# ----------------------------------------------------------------------------
# ssh 后端：autossh/ssh 反向隧道，经你的 VPS（sshd）把 agent 端口映射出去。
# 需要安装参数：--ssh-dest <user@host> [--ssh-port 22] [--ssh-remote-port 13100]
# 首次运行会生成专用密钥并打印公钥，按提示加到 VPS 的 authorized_keys 即可。
# 地址恒定：http://<VPS>:<remote-port>。VPS 的 sshd 需开 GatewayPorts yes 才能被外网访问。
# ----------------------------------------------------------------------------
cat > "$DIR/mcp-tunnel-ssh.sh" <<'TUN_SSH_EOF'
#!/usr/bin/env bash
# ssh 反向隧道后端：经你的 VPS 把本机 agent 端口暴露到公网。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
LOG="$DIR/tunnel.log"
PIDF="$DIR/tunnel.pid"
PY="$(command -v python3 || echo /usr/bin/python3)"
[ -f "$DIR/.tunnel_env" ] && . "$DIR/.tunnel_env"
# 下载兜底：curl 坏了就用 python urllib（ssh 后端暂无下载项，留作一致性）
tunnel_dl() {
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 12 -o "$2" "$1" && return 0
  fi
  "$PY" -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$1" "$2" 2>/dev/null
}
[ -f "$DIR/.tunnel_env" ] && . "$DIR/.tunnel_env"
NAME="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json'))['name'])" 2>/dev/null || echo node)"

# ---- 安全闸门：开公网隧道前必须有鉴权（与 cf 后端同逻辑） ----
TOKEN="$("$PY" -c "import json;print(json.load(open('$DIR/mcp_agent_config.json')).get('auth_token',''))" 2>/dev/null || echo '')"
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
  echo "[+] 已为公网隧道生成鉴权 token，写入 mcp_agent_config.json 并重启副端 ..."
  if [ -f "/etc/systemd/system/${NAME}-mcp.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart "${NAME}-mcp" >/dev/null 2>&1 || true
  else
    bash "$DIR/mcp-ctl.sh" restart >/dev/null 2>&1 || true
  fi
  sleep 2
fi

# ---- 参数 ----
[ -n "${SSH_DEST:-}" ] || { echo "[x] 缺少 SSH 目标。用 --ssh-dest user@host 指定（你的 VPS）"; exit 1; }
SSH_PORT="${SSH_PORT:-22}"
SSH_REMOTE_PORT="${SSH_REMOTE_PORT:-13100}"
SSH_HOST="${SSH_DEST##*@}"

# ---- 密钥（放安装目录，不依赖 $HOME：tsu 会改 HOME 导致路径错乱） ----
KEY="$DIR/ssh_tunnel_key"
if [ ! -f "$KEY" ]; then
  echo "[+] 生成隧道专用密钥 $KEY ..."
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "mcp-fleet-tunnel" >/dev/null 2>&1 || \
    ssh-keygen -t rsa -b 3072 -N "" -f "$KEY" -C "mcp-fleet-tunnel" >/dev/null 2>&1
  chmod 600 "$KEY" 2>/dev/null || true
fi
# ---- 密钥部署检查：测免密通不通，不通就给清晰指引（不在安装器里嵌套交互式密码） ----
_ssh_base() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
      -i "$KEY" -p "$SSH_PORT" "$@"
}
if ! _ssh_base "$SSH_DEST" true 2>/dev/null; then
  echo ""
  echo "============================================================"
  echo " 还差一步：把公钥部署到 VPS（只需做一次）"
  echo "------------------------------------------------------------"
  echo " 在**本机终端**直接跑下面这行，按提示输一次 VPS 密码："
  echo ""
  echo "   ssh-copy-id -i ${KEY}.pub -p $SSH_PORT $SSH_DEST"
  echo ""
  echo " （Termux 若无 ssh-copy-id，用这行代替，直接输密码）"
  echo "   cat ${KEY}.pub | ssh -p $SSH_PORT $SSH_DEST \\"
  echo "     'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'"
  echo ""
  echo " 搞定后跑下面这行启动隧道："
  echo ""
  echo "   bash $DIR/mcp-tunnel.sh"
  echo "============================================================"
  echo ""
else
  echo "[+] 密钥已部署，免密登录正常"
fi

# ---- 启动反向隧道 ----
touch "$DIR/.want_tunnel" 2>/dev/null || true
# 密钥没打通就先不硬起隧道，等用户部署完密钥后跑 mcp-tunnel.sh
URL=""
if ! _ssh_base "$SSH_DEST" true 2>/dev/null; then
  echo "[i] 跳过隧道启动（等密钥部署好后跑 bash $DIR/mcp-tunnel.sh 即可）"
else
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  echo "[i] ssh 隧道已在运行（pid $(cat "$PIDF")），复用。"
else
  [ -f "$PIDF" ] && { kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"; sleep 1; }
  : > "$LOG"
  SSH_OPTS="-o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -i $KEY -p $SSH_PORT"
  # shellcheck disable=SC2086
  if command -v autossh >/dev/null 2>&1; then
    echo "[i] 用 autossh（断线自动重连）"
    setsid nohup autossh -M 0 -N $SSH_OPTS -R "${SSH_REMOTE_PORT}:127.0.0.1:${PORT}" "$SSH_DEST" >> "$LOG" 2>&1 < /dev/null &
  else
    echo "[i] 没找到 autossh，用 ssh（建议装 autossh 获得断线重连）"
    setsid nohup ssh $SSH_OPTS -N -R "${SSH_REMOTE_PORT}:127.0.0.1:${PORT}" "$SSH_DEST" >> "$LOG" 2>&1 < /dev/null &
  fi
  sleep 3
  echo $! > "$PIDF"
fi

URL="http://${SSH_HOST}:${SSH_REMOTE_PORT}"
# 只有隧道真正启动了才落盘地址；密钥没打通时不写，免得主端 sync 到死地址
if _ssh_base "$SSH_DEST" true 2>/dev/null; then
printf '%s' "$URL" > "$DIR/.tunnel_url" 2>/dev/null || true
printf '%s' "$URL" > "$DIR/current_url.txt" 2>/dev/null || true
"$PY" - "$DIR/mcp_agent_config.json" "$URL" <<'URL_PY_EOF' 2>/dev/null || true
import json, sys
p, url = sys.argv[1], sys.argv[2]
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["tunnel_url"] = url
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
URL_PY_EOF
fi
fi

# ---- 自检（隧道没起来就跳过） ----
if [ -z "$URL" ]; then
  echo "[i] 隧道未启动，自检跳过。密钥部署好后跑 bash $DIR/mcp-tunnel.sh"
else
HTTP_CODE=""
if [ -n "$TOKEN" ]; then
  HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$URL/" 2>/dev/null || true)"
else
  HTTP_CODE="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$URL/" 2>/dev/null || true)"
fi

echo
echo "============================================================"
echo " ssh 隧道地址：$URL  （经你的 VPS $SSH_HOST，地址恒定）"
echo " MCP 端点    ：$URL/mcp"
if [ -n "$TOKEN" ]; then echo " 鉴权 token  ：$TOKEN"; else echo " 鉴权 token  ：(未设置 —— 公网裸奔，请尽快补上)"; fi
echo "------------------------------------------------------------"
echo " 公网访问    ：${HTTP_CODE:-未检测}$([ "$HTTP_CODE" = "200" ] && echo " (通)" || echo " (不通)")"
if [ "$HTTP_CODE" != "200" ]; then
  echo " 排查："
  echo "   1) VPS 上 sshd 需开 GatewayPorts：/etc/ssh/sshd_config 加一行 GatewayPorts yes 后 systemctl reload sshd"
  echo "   2) VPS 防火墙放行 ${SSH_REMOTE_PORT}/tcp"
  echo "   3) 密钥是否已加入 VPS 的 ~/.ssh/authorized_keys（见上方公钥）"
  echo "   4) 日志：tail -n 30 $LOG"
fi
echo "------------------------------------------------------------"
echo " 主端 ~/.workbuddy/mcp.json 增加："
echo
echo "   "$NAME": {"
echo "     "type": "http","
echo "     "url": "$URL/mcp"$([ -n "$TOKEN" ] && printf ',')"
if [ -n "$TOKEN" ]; then
  echo "     "headers": { "Authorization": "Bearer $TOKEN" }"
fi
echo "   }"
echo "============================================================"
echo " 停止隧道：kill \$(cat "$PIDF")      日志：$LOG"
fi
TUN_SSH_EOF

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

# ---- v3.4 真实探活 ----------------------------------------------------------
# 原来只看「进程在不在」：agent 线程被半开连接占满/卡死、cloudflared 进程还在但
# 隧道早已断开时，守护都认为一切正常，主端那头却一直连不上。
# 现在对 agent 本机端口、隧道公网地址各发一次真实 HTTP 请求，连续失败才处置，
# 避免网络抖动一次就重启。
AGENT_FAIL_MAX="${WATCHDOG_AGENT_FAILS:-2}"
TUNNEL_FAIL_MAX="${WATCHDOG_TUNNEL_FAILS:-3}"

# 输出 HTTP 状态码（连不上为 000）
http_code() {
  local url="$1" tok c
  tok="$(cfg auth_token)"
  if command -v curl >/dev/null 2>&1 && curl --version >/dev/null 2>&1; then
    if [ -n "$tok" ]; then
      c="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok" "$url" 2>/dev/null || true)"
    else
      c="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    fi
    echo "${c:-000}"
    return 0
  fi
  "$PY" - "$url" "$tok" <<'CODE_PY_EOF' 2>/dev/null || echo 000
import sys, urllib.request, urllib.error
url, tok = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url)
if tok:
    req.add_header("Authorization", "Bearer " + tok)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
CODE_PY_EOF
}

# 失败计数（落盘，--once 模式每轮是新进程也能累计）
fails_get() { cat "$DIR/.wd_$1_fails" 2>/dev/null || echo 0; }
fails_set() { echo "$2" > "$DIR/.wd_$1_fails" 2>/dev/null || true; }

restart_agent() {
  if [ -f "/etc/systemd/system/${UNIT}.service" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl restart "$UNIT" >> "$LOG" 2>&1 || log "systemctl restart 失败"
  else
    bash "$DIR/mcp-ctl.sh" restart >> "$LOG" 2>&1 || log "mcp-ctl.sh restart 失败"
  fi
  sleep 3
}

# 进程在但不响应 → 连续 AGENT_FAIL_MAX 轮后重启
check_agent_health() {
  local code n
  code="$(http_code "http://127.0.0.1:$PORT/health")"
  case "$code" in
    2*|401|403) fails_set agent 0; return 0 ;;
  esac
  n=$(( $(fails_get agent) + 1 ))
  fails_set agent "$n"
  log "agent 探活失败 (HTTP $code，第 $n 次)"
  if [ "$n" -ge "$AGENT_FAIL_MAX" ]; then
    log "agent 无响应 → 重启"
    restart_agent
    fails_set agent 0
  fi
}

# 隧道进程在但公网不通 → 连续 TUNNEL_FAIL_MAX 轮后重建
check_tunnel_health() {
  local url="$1" code n
  [ -z "$url" ] && return 0
  code="$(http_code "${url%/}/health")"
  case "$code" in
    2*|401|403) fails_set tunnel 0; return 0 ;;
  esac
  n=$(( $(fails_get tunnel) + 1 ))
  fails_set tunnel "$n"
  log "隧道公网探活失败 (HTTP $code，第 $n 次) $url"
  if [ "$n" -ge "$TUNNEL_FAIL_MAX" ]; then
    # agent 本机都不通时别急着重建隧道（问题在 agent，不在隧道）
    case "$(http_code "http://127.0.0.1:$PORT/health")" in
      2*|401|403) ;;
      *) log "本机 agent 也不通，先不重建隧道"; return 0 ;;
    esac
    log "隧道已断 → 重建"
    [ -f "$DIR/tunnel.pid" ] && kill "$(cat "$DIR/tunnel.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DIR/tunnel.pid"
    CF_NO_FALLBACK=0 bash "$DIR/mcp-tunnel.sh" >> "$LOG" 2>&1 || log "隧道重建失败"
    fails_set tunnel 0
    sleep 3
  fi
}

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
  if alive_agent; then check_agent_health; return 0; fi
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
  local rebuilt=0
  if ! alive_tunnel; then
    log "隧道进程不在 → 重建（地址会变）"
    CF_NO_FALLBACK=0 bash "$DIR/mcp-tunnel.sh" >> "$LOG" 2>&1 || log "隧道重建失败"
    fails_set tunnel 0
    rebuilt=1
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
  [ "$rebuilt" = "0" ] && check_tunnel_health "$url"
  # 重建后地址可能变了，重新读一次
  [ -f "$DIR/.tunnel_url" ] && url="$(cat "$DIR/.tunnel_url" 2>/dev/null || echo "$url")"

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

chmod +x "$DIR/_supervisor.sh" "$DIR/mcp-ctl.sh" "$DIR/mcp-tunnel.sh" "$DIR/mcp-tunnel-cf.sh" "$DIR/mcp-tunnel-frp.sh" "$DIR/mcp-tunnel-ssh.sh" "$DIR/mcp-watchdog.sh" 2>/dev/null || true

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
# ---------- 隧道后端配置落盘 ----------
# mcp-tunnel.sh 是分发器，按 .tunnel_backend 选择 cf | frp | ssh；
# 各后端参数存在 .tunnel_env，后端脚本 source 它。
if [ "$DO_TUNNEL" = "1" ]; then
  case "$TUNNEL_BACKEND" in
    cf) ;;
    frp)
      [ -n "$FRP_SERVER" ] || die "frp 后端需要 --frp-server（你的 VPS 地址）"
      [ -n "$FRP_TOKEN" ] || die "frp 后端需要 --frp-token（与 VPS 上 frps.ini 的 token 一致）"
      ;;
    ssh)
      [ -n "$SSH_DEST" ] || die "ssh 后端需要 --ssh-dest（user@host，你的 VPS）"
      ;;
    *) die "未知的 --tunnel-backend：$TUNNEL_BACKEND（可选 cf | frp | ssh）" ;;
  esac
  printf '%s' "$TUNNEL_BACKEND" > "$DIR/.tunnel_backend"
  cat > "$DIR/.tunnel_env" <<TUN_ENV_EOF
TUNNEL_BACKEND="$TUNNEL_BACKEND"
FRP_SERVER="$FRP_SERVER"
FRP_PORT="$FRP_PORT"
FRP_TOKEN="$FRP_TOKEN"
FRP_REMOTE_PORT="$FRP_REMOTE_PORT"
SSH_DEST="$SSH_DEST"
SSH_PORT="$SSH_PORT"
SSH_REMOTE_PORT="$SSH_REMOTE_PORT"
TUN_ENV_EOF
  chmod 600 "$DIR/.tunnel_env" 2>/dev/null || true
  log "隧道后端: $TUNNEL_BACKEND"
fi

if [ "$DO_TUNNEL" = "1" ]; then
  log "启动公网隧道 ..."
  TUN_OUT="$(bash "$DIR/mcp-tunnel.sh" 2>&1 || true)"
  echo "$TUN_OUT"
  # cf 给 https 临时域名；frp/ssh 给 http://vps:port（固定地址）
  TUNNEL_URL="$(printf '%s' "$TUN_OUT" | grep -oE 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com' | head -1 || true)"
  if [ -z "$TUNNEL_URL" ]; then
    TUNNEL_URL="$(printf '%s' "$TUN_OUT" | grep -oE 'http://[^[:space:]"]+' | grep -v '127\.0\.0\.1' | head -1 || true)"
  fi
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
 想用 AI 直接控制本节点：在任意 MCP 客户端（WorkBuddy / Claude / Cursor …）的配置里加：

  "mcpServers": {
    "$NAME": {
      "type": "http",
      "url": "$MCP_URL"$(if [ -n "$AUTH_TOK" ]; then echo ","; fi)
$(if [ -n "$AUTH_TOK" ]; then echo "      \"headers\": { \"Authorization\": \"Bearer $AUTH_TOK\" }"; fi)
    }
  }
 $URL_NOTE

 加完重启该客户端（或重载 MCP 配置）即可使用。
 工具名形如： ${PREFIX}exec / ${PREFIX}sysinfo / ${PREFIX}peer_call ...

----------------------------------------------------------------------------
 节点互控（v3.5，不分主副）：本机已带控制端 $( [ -n "$FLEET_BIN" ] && echo "fleet 命令" || echo "$PY3 $DIR/fleet.py" )
   和另一个节点双向配对（之后两边都能控制对方）：
     fleet pair http://<对方IP>:3100/mcp --token <对方令牌>
   别的节点要配对本机时，填：
     地址 $MCP_URL$( [ -n "$AUTH_TOK" ] && printf '\n     令牌 %s' "$AUTH_TOK" )
   配好后： fleet exec <对方名字> 'uptime'   或让 AI 调 peer_call

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
 再重启该 MCP 客户端。漏了 headers 的话，加完令牌就会一直收到 401。

 或者重装时直接带上参数： --generate-token --require-auth
============================================================================
NOAUTH_HINT
fi
