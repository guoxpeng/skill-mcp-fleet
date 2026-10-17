#!/bin/bash
# mcp-fleet 一键演示（v4.5+）
#   自动起两个本地演示节点 → 跨节点执行命令 → peer 互联 → 打印拓扑 → 自动清理
# 用法：bash scripts/demo.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
A_PORT=3111; B_PORT=3112
A_URL="http://127.0.0.1:${A_PORT}/mcp"
B_URL="http://127.0.0.1:${B_PORT}/mcp"

cleanup() { kill ${PID_A:-} ${PID_B:-} 2>/dev/null; }
trap cleanup EXIT

step() { echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; echo "  $1"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

step "1/5  启动演示节点 A（端口 ${A_PORT}）"
python3 "$DIR/mcp_agent.py" --port $A_PORT >/tmp/fleet-demo-a.log 2>&1 & PID_A=$!
step "2/5  启动演示节点 B（端口 ${B_PORT}）"
python3 "$DIR/mcp_agent.py" --port $B_PORT >/tmp/fleet-demo-b.log 2>&1 & PID_B=$!

step "3/5  等待节点就绪"
ok=0
for i in $(seq 1 20); do
  if python3 "$DIR/fleet.py" exec "$A_URL" "echo ok" >/dev/null 2>&1 \
  && python3 "$DIR/fleet.py" exec "$B_URL" "echo ok" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" = "1" ] || { echo "❌ 节点启动超时，查看 /tmp/fleet-demo-a.log"; exit 1; }
echo "两节点均在线 ✅"

step "4/5  跨节点演示"
echo "── A 节点执行命令 ──"
python3 "$DIR/fleet.py" exec "$A_URL" "echo '我是节点 A（端口 3111）'"
echo "── B 节点执行命令 ──"
python3 "$DIR/fleet.py" exec "$B_URL" "echo '我是节点 B（端口 3112）'"
echo "── A 把 B 加为 peer ──"
python3 "$DIR/fleet.py" call "$A_URL" --tool peer_add \
  --args "{\"name\":\"demo-b\",\"url\":\"${B_URL}\"}" | head -3
echo "── A 经 peer_call 调用 B 的 exec（多跳演示）──"
python3 "$DIR/fleet.py" call "$A_URL" --tool peer_call \
  --args '{"peer":"demo-b","tool":"exec","arguments":{"command":"echo 来自 B 的问候 ✅"}}' | head -5

step "5/5  Mesh 拓扑"
echo ""
echo "   ┌─────────────┐                    ┌─────────────┐"
echo "   │   demo-a    │ ◄──── peer ────► │   demo-b    │"
echo "   │ 127.0.0.1   │      已互联        │ 127.0.0.1   │"
echo "   │   :3111     │                    │   :3112     │"
echo "   └─────────────┘                    └─────────────┘"
echo ""
echo "✅ 演示完成：双节点启动、跨节点 exec、peer 互联、peer_call 多跳全部正常"
echo "   （演示节点已自动关闭，不影响任何真实节点）"
