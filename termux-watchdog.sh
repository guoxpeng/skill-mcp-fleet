#!/usr/bin/env bash
# Termux 副端保活脚本（mcp_agent.py 被系统杀掉后自动拉起）
#
# 为什么需要它：Termux 切后台会被 Android 杀掉，mcp_agent.py 进程没了之后
# 没有任何东西会把它拉起来 —— 主端看到的就是"掉线"。Linux 节点有
# install.sh 自带的 _supervisor.sh + mcp-watchdog.sh，手机没有，这个脚本补上。
#
# 依赖：bash、curl（或 python3）、nohup。故意不用 pgrep/ss/which，
# Termux 默认环境里这些经常没有（2026-10-12 实测）。
#
# 用法：
#   bash termux-watchdog.sh                 常驻（推荐，配合 Termux:Boot 自启）
#   bash termux-watchdog.sh --once          跑一轮就退出（配 cron/Tasker 定时调）
#   WATCHDOG_INTERVAL=30 bash termux-watchdog.sh   改检查间隔（默认 60 秒）
#
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="$(cat "$DIR/port.txt" 2>/dev/null || echo 3100)"
PY="$(command -v python3 2>/dev/null || echo python3)"
LOG="$DIR/watchdog-termux.log"
PIDF="$DIR/watchdog-termux.pid"
AGENT_PIDF="$DIR/agent-termux.pid"
INTERVAL="${WATCHDOG_INTERVAL:-60}"
BACKOFF_MAX=300   # 连续失败退避上限 5 分钟

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# ---- 探活：优先 curl，没有就用 python ----
probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -ks --max-time 5 "https://127.0.0.1:${PORT}/" >/dev/null 2>&1 || \
    curl -s --max-time 5 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1
    return $?
  fi
  "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:%s/" % sys.argv[1], timeout=5).read(64)
except Exception:
    sys.exit(1)
PYEOF
  return $?
}

agent_alive() {
  [ -f "$AGENT_PIDF" ] || return 1
  local p; p="$(cat "$AGENT_PIDF" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

start_agent() {
  # 先清掉残留（僵尸 pidfile / 残留进程）
  if [ -f "$AGENT_PIDF" ]; then
    local p; p="$(cat "$AGENT_PIDF" 2>/dev/null)"
    [ -n "$p" ] && kill -9 "$p" 2>/dev/null
    rm -f "$AGENT_PIDF"
  fi
  # 旧的 nohup 残留也清一下（按命令行特征，不用 pgrep）
  for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "mcp_agent.py"; then
      kill -9 "$p" 2>/dev/null
    fi
  done
  cd "$DIR"
  nohup "$PY" -u "$DIR/mcp_agent.py" --host 0.0.0.0 --port "$PORT" \
    >> "$DIR/agent-termux.log" 2>&1 < /dev/null &
  echo $! > "$AGENT_PIDF"
  sleep 3
  if probe; then
    log "agent 已拉起 (pid $(cat "$AGENT_PIDF"), port $PORT)"
    return 0
  fi
  log "agent 拉起后探活仍失败，见 agent-termux.log"
  return 1
}

round() {
  if probe; then
    return 0
  fi
  log "探活失败 → 重启 agent"
  start_agent
}

# ---- 单实例 ----
if [ "${1:-}" = "--once" ]; then
  round
  exit 0
fi
if [ -f "$PIDF" ]; then
  _p="$(cat "$PIDF" 2>/dev/null)"
  if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
    echo "watchdog 已在运行 (pid $_p)"
    exit 0
  fi
fi
echo $$ > "$PIDF"
trap 'rm -f "$PIDF"; exit 0' INT TERM EXIT
log "termux watchdog 启动 (pid=$$, interval=${INTERVAL}s, port=$PORT)"

fail_streak=0
while true; do
  if round; then
    fail_streak=0
  else
    fail_streak=$((fail_streak + 1))
    # 连续失败指数退避：60s, 120s, 240s, 300s(封顶)
    _wait=$((INTERVAL * (2 ** (fail_streak - 1))))
    [ "$_wait" -gt "$BACKOFF_MAX" ] && _wait=$BACKOFF_MAX
    log "连续失败 ${fail_streak} 次，退避 ${_wait}s"
    sleep "$_wait"
    continue
  fi
  sleep "$INTERVAL"
done
