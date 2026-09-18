#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 mcp_agent.py 内嵌进 install.sh，生成「单文件自包含」安装脚本。
这样副设备上只需要一个 install.sh 就能装完，不用额外传文件。

用法： python build_installer.py
产出： fleet-agent/install.sh
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
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
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
  if command -v sudo >/dev/null 2>&1; then
    echo "[i] 需要 root，自动用 sudo 重新执行"
    exec sudo -E bash "$0" "$@"
  else
    die "请用 root 运行：sudo bash $0 ..."
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
'''

content = TEMPLATE.replace("__AGENT_SOURCE__", agent_src)
with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write(content)

print("已生成: %s" % OUT)
print("大小: %.1f KB" % (len(content.encode("utf-8")) / 1024.0))
print("内嵌 agent 行数: %d" % len(agent_src.splitlines()))
