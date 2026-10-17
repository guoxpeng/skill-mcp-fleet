#!/usr/bin/env bash
# Tailscale 守护保活：daemon 挂掉 / 掉登录态时自动恢复
#
# 背景：2026-10-12 十堰机 tailscaled 反复断连、手机 Tailscale 被杀，
# Fleet 跟着一起断。tailscale 在 UDP 受限网络会自动走 DERP TCP 中继，
# 不需要手工配"TCP 兜底"；真正要盯的是两件事：
#   1. tailscaled 进程还在不在
#   2. 登录态还在不在（tailscale status 会报 Logged out）
#
# 用法：
#   bash tailscale-keepalive.sh --once    跑一轮就退出（配 systemd timer / cron / Termux 定时）
#   bash tailscale-keepalive.sh           常驻循环（无 systemd 的环境）
#   CHECK_INTERVAL=120 bash tailscale-keepalive.sh   改检查间隔（默认 120 秒）
#
# 注意：掉登录态后 `tailscale up` 无需 authkey 即可恢复（机器已授权过）；
# 如果是全新登录（需要 key），脚本只告警不自动执行，避免把 key 写进脚本。
# 需要无人值守重登录时，用环境变量 TS_AUTHKEY 传入（one-time key，用完即焚）。
#
set -u
LOG="${TS_KEEPALIVE_LOG:-$HOME/tailscale-keepalive.log}"
PIDF="${TS_KEEPALIVE_PID:-$HOME/tailscale-keepalive.pid}"
INTERVAL="${CHECK_INTERVAL:-120}"
TS="${TS_BIN:-$(command -v tailscale 2>/dev/null || echo tailscale)}"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

ts_ok() {
  # 返回 0=在线。tailscale status 非 0 或报 Logged out / NoState 都算掉线。
  local out
  out="$("$TS" status 2>&1 | head -5)"
  local rc=$?
  [ $rc -ne 0 ] && return 1
  echo "$out" | grep -qiE "logged out|NoState|stopped" && return 1
  return 0
}

daemon_alive() {
  # tailscaled 进程 / systemd 服务其一在即可
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet tailscaled 2>/dev/null && return 0
  fi
  for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
    if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "tailscaled"; then
      return 0
    fi
  done
  return 1
}

recover() {
  log "tailscale 异常，尝试恢复"
  if ! daemon_alive; then
    log "tailscaled 未运行 → 尝试启动"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl start tailscaled >>"$LOG" 2>&1 || \
      systemctl --user start tailscaled >>"$LOG" 2>&1 || true
    fi
    sleep 5
  fi
  # 重连（已授权机器不需要 key）
  if [ -n "${TS_AUTHKEY:-}" ]; then
    "$TS" up --authkey="$TS_AUTHKEY" >>"$LOG" 2>&1 && log "tailscale up（带 key）成功"
  else
    if "$TS" up >>"$LOG" 2>&1; then
      log "tailscale up 成功"
    else
      log "WARN: tailscale up 失败 —— 可能需要重新登录（authkey），请手工处理"
    fi
  fi
  sleep 10
  if ts_ok; then
    log "恢复成功，当前 IP: $("$TS" ip -4 2>/dev/null | head -1)"
    return 0
  fi
  log "恢复失败，下轮重试"
  return 1
}

round() {
  if ts_ok; then
    return 0
  fi
  recover
}

if [ "${1:-}" = "--once" ]; then
  round
  exit 0
fi
if [ -f "$PIDF" ]; then
  _p="$(cat "$PIDF" 2>/dev/null)"
  if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
    echo "keepalive 已在运行 (pid $_p)"
    exit 0
  fi
fi
echo $$ > "$PIDF"
trap 'rm -f "$PIDF"; exit 0' INT TERM EXIT
log "tailscale keepalive 启动 (pid=$$, interval=${INTERVAL}s)"
while true; do
  round
  sleep "$INTERVAL"
done
