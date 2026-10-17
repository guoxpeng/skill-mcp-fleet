#!/usr/bin/env bash
# ============================================================
#  MCP Fleet 一键演示
#  在本机拉起两个节点（demo-nas :4100 / demo-cloud :4200）：
#  UDP 自动互发现 → 工具清单 → 本机执行 → 跨节点操控
#  → 节点间消息 → mesh 拓扑，30 秒看懂设备互联。
#
#  用法:  bash scripts/demo.sh
#  退出时自动清理演示进程与临时文件；演示使用独立发现端口
#  34199，不影响本机已部署的 fleet 节点。
# ============================================================
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d /tmp/fleet-demo-XXXXXX)"
PIDS=""

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; }

cleanup() {
  [ -n "$PIDS" ] && kill $PIDS 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

call() { # call <mcp-url> <tool> [json-args]
  python3 "$BASE/call_node.py" "$1" --tool "$2" "${3:-}"
}

# ---------- 1. 准备两个节点目录（节点名从脚本同目录配置读取） ----------
say "准备演示节点 demo-nas(:4100) / demo-cloud(:4200)"
for n in nas cloud; do
  mkdir -p "$WORK/$n"
  cp "$BASE/mcp_agent.py" "$BASE/fleet_proto.py" "$BASE/dashboard_html.py" "$WORK/$n/"
done
printf '{\n  "name": "demo-nas",\n  "work_dir": "/",\n  "audit_log": true,\n  "discover_port": 34199,\n  "self_url": "http://127.0.0.1:4100/mcp"\n}\n'  > "$WORK/nas/mcp_agent_config.json"
printf '{\n  "name": "demo-cloud",\n  "work_dir": "/",\n  "audit_log": true,\n  "discover_port": 34199,\n  "self_url": "http://127.0.0.1:4200/mcp"\n}\n' > "$WORK/cloud/mcp_agent_config.json"

# ---------- 2. 启动两个节点 ----------
python3 "$WORK/nas/mcp_agent.py"   --host 127.0.0.1 --port 4100 > "$WORK/nas.log"   2>&1 &
PIDS="$PIDS $!"
python3 "$WORK/cloud/mcp_agent.py" --host 127.0.0.1 --port 4200 > "$WORK/cloud.log" 2>&1 &
PIDS="$PIDS $!"

A=http://127.0.0.1:4100/mcp
B=http://127.0.0.1:4200/mcp

say "等待节点就绪（最多 10s）"
ready=0
for _ in $(seq 1 20); do
  if python3 "$BASE/call_node.py" "$A" --list >/dev/null 2>&1 \
     && python3 "$BASE/call_node.py" "$B" --list >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 0.5
done
if [ "$ready" != 1 ]; then
  fail "节点未就绪。日志：$WORK/nas.log 与 $WORK/cloud.log（退出时会自动清理）"
  exit 1
fi
ok "两个节点已启动（标准 MCP 服务器，HTTP + JSON-RPC）"

# ---------- 3. 节点互联（UDP 自动发现 + 显式配对） ----------
say "等待 UDP 广播自动互发现（3s，零配置）"
sleep 3
peers_out="$(call "$A" peers '{"check":true}')"
echo "$peers_out"
if echo "$peers_out" | grep -q "demo-cloud"; then
  ok "demo-nas 已自动发现 demo-cloud —— 无需任何配置"
else
  echo "（本机 UDP 广播受限未自动发现，改用显式配对 —— 与真实跨网段场景相同）"
  call "$A" peer_add '{"name":"demo-cloud","url":"http://127.0.0.1:4200/mcp"}'
  ok "配对完成：demo-nas ↔ demo-cloud"
fi

# ---------- 4. 工具清单 ----------
say "节点工具清单（标准 MCP tools/list）"
n_tools="$(python3 "$BASE/call_node.py" "$A" --list 2>/dev/null | wc -l)"
ok "共 ${n_tools} 个工具：exec / read / write / edit / search / upload / peers / peer_call / msg_send / mesh …"

# ---------- 5. 演示 1：本机执行 ----------
say "【演示 1】对 demo-nas 说：看看这台设备的状态"
call "$A" sysinfo '{}' | head -12

# ---------- 6. 演示 2：跨节点操控 ----------
say "【演示 2】跨节点操控：经 demo-nas 在 demo-cloud 上执行命令"
call "$A" peer_call '{"peer":"demo-cloud","tool":"exec","arguments":{"command":"echo hello-from-demo-cloud && date"}}'

# ---------- 7. 演示 3：节点间消息 ----------
say "【演示 3】节点间消息：demo-nas → demo-cloud（store-and-forward 邮箱）"
call "$A" msg_send '{"from":"demo-nas","to":"demo-cloud","text":"云主机兄弟，帮我盯着下载任务","topic":"demo"}'
call "$B" msg_recv '{"me":"demo-cloud"}'

# ---------- 8. 演示 4：全网拓扑 ----------
say "【演示 4】全网拓扑 mesh"
call "$A" mesh '{}' | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
s = d.get("self", {})
print("本节点 : %s（在线, %d 个工具）" % (s.get("name"), len(s.get("caps", {}).get("tools", []))))
for name, p in (d.get("peers") or {}).items():
    if isinstance(p, dict) and name:
        print("对等点 : %-12s %s" % (name, "在线" if p.get("online") else "离线"))
'

say "演示完成，正在清理演示进程与临时文件……"
ok "MCP Fleet 演示结束 —— 装到你自己的设备上，见 SKILL.md「快速安装」"
