---
name: mcp-fleet
description: |
  零依赖 MCP Fleet 节点互控系统：把任意 Linux 设备变成可被 MCP agent 远程操控的节点。
  v4.0 新增 Tailscale 自动组网（多设备装完即自动发现互连）、Agent 协作协议（能力声明/任务委派/多节点群组）、
  消息总线（pub/sub + 持久化）、安全加固（tailnet 自动信任/审计日志/限流）和 mesh 拓扑可视化。
  每个节点 32 个工具：exec/read/write/edit/docker/systemctl/sysinfo + peer_call/peers + msg_send/recv + caps/delegate/group + mesh。
  纯 Python 标准库，零依赖，兼容 Python 3.6+。
  Trigger: fleet, MCP fleet, 节点互控, agent 组网, tailscale mesh, multi-device agent, peer call, 
  remote exec, 副端安装, 节点发现, agent 协作, task delegation, message bus, mesh topology
tags: [mcp, fleet, mesh, tailscale, peer-to-peer, agent-collaboration, remote-control, zero-dependency]
version: 4.0.0
---

# MCP Fleet · 节点互控系统 v4.0

## 这是什么

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器 / 手机…）每台装一个「节点」，即可被**任意支持 MCP 的 AI agent**远程操控。

**v4.0 核心进化**：

1. **Tailscale 自动组网** — 装完 skill 自动加入 tailnet，新设备一上线，mesh 里所有节点自动发现它，零配置
2. **Agent 协作协议** — 节点自动声明能力（Docker/GPU/网络/工具），可按能力委派任务，多节点临时组队
3. **消息总线** — pub/sub 广播 + 持久化邮箱 + topic 过滤，agent 间异步通信
4. **安全加固** — tailnet 内自动互信（端到端加密）、审计日志、令牌桶限流
5. **Mesh 拓扑可视化** — `fleet mesh` 一键打印全网拓扑

## 快速安装

### 一键安装 + Tailscale 自动组网

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --name nas --port 3100 --tailnet my-tailnet --tailnet-key tskey-xxx
```

装完后自动：安装 Tailscale → 加入 tailnet → 启动 MCP agent → 发现其他 fleet 节点 → 自动互连

### 普通安装（内网，不需要 Tailscale）

```bash
sudo bash install.sh --name nas --port 3100
```

## 节点工具清单（32 个）

| 类别 | 工具 | 作用 |
|------|------|------|
| **执行** | `exec` / `exec_async` / `exec_poll` / `exec_result` / `exec_kill` / `exec_jobs` | 同步/后台命令执行 |
| **文件** | `read` / `write` / `edit` / `list_dir` | 文件读写编辑 |
| **Docker** | `docker_ps` / `docker_logs` / `docker_restart` | 容器管理 |
| **系统** | `systemctl` / `service_logs` / `sysinfo` / `http_get` | 服务管理 + 系统信息 |
| **Peer Mesh** | `peers` / `peer_call` / `peer_add` / `peer_remove` / `peer_info` | 节点互控（多跳转发） |
| **消息总线** | `msg_send` / `msg_recv` / `msg_history` | 异步消息 + topic 广播 + 持久化 |
| **Agent 协作** | `caps` / `mesh_caps` / `delegate` / `group_form` / `group_list` / `group_disband` | 能力声明 + 任务委派 + 群组 |
| **Mesh 拓扑** | `mesh` | 全网拓扑与在线状态 |

## 主端 CLI

```bash
python scripts/fleet.py add --name nas --ip 192.168.1.10 --port 3100
python scripts/fleet.py mesh                    # 打印 mesh 拓扑图
python scripts/fleet.py exec nas "docker ps"   # 在节点执行命令
python scripts/fleet.py pair nas               # 双向配对
python scripts/fleet.py health --deep          # 全网体检
```

## 目录结构

```
mcp-fleet/
├── SKILL.md                          # 本文件（AgentSkill 规范）
├── mcp_agent_config.example.json     # 配置模板
├── scripts/
│   ├── mcp_agent.py                  # 节点 MCP 服务器（核心，32 个工具）
│   ├── fleet.py                      # 主端统一 CLI
│   ├── fleet_chat.py                 # AI 间消息 CLI
│   ├── fleet_watch.py                # 保活守护
│   ├── fleet-directory.py            # 地址目录服务
│   ├── install.sh                    # 一键安装脚本
│   ├── install-fleet-node.sh         # 节点安装辅助
│   ├── add_fleet_node.py             # 注册节点到 MCP 配置
│   ├── call_node.py                  # 免重启直接调工具
│   ├── probe_fleet.py                # 连通性诊断
│   ├── make-offline.py               # 生成离线安装包
│   └── fleet-http.service            # systemd 单元
└── references/
    ├── 加固操作手册.md
    ├── 安全审计报告.md
    └── 云服务器-副端安装-一条命令.md
```

## v4.0 新增配置

```json
{
  "tailnet_auto_mesh": true,
  "tailnet_auto_trust": true,
  "tailnet_scan_interval": 60,
  "collab_tools": true,
  "audit_log": true,
  "rate_limit_per_min": 120
}
```

## 架构

```
Agent A (NAS)          Agent B (云主机)        Agent C (树莓派)
  ├── tailscale 100.x     ├── tailscale 100.y     ├── tailscale 100.z
  ├── 32 tools            ├── 32 tools            ├── 32 tools
  ├── peer_call → B,C     ├── peer_call → A,C     ├── peer_call → A,B
  ├── msg_send → B,C      ├── delegate → A(GPU)   ├── caps: GPIO
  └── caps: Docker,GPU    └── caps: 8CPU,32GB     └── group: deploy-team
```

Tailscale 提供端到端加密的 tailnet，fleet 在其上自动构建 agent mesh。
