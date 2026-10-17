---
name: mcp-fleet
description: |
  零依赖 MCP Fleet 节点互控系统 v4.5：把任意 Linux 设备变成可被 MCP agent 远程操控的节点。
  自研通讯协议层（fleet_proto）：UDP 广播自动发现、store-and-forward 离线邮箱、
  HTTP 消息端点、gossip 邮箱同步。agent 间直接通信——在线直接投递，离线存邮箱，上线自动收信。
  设备码自动生成 + 邀请链接配对（安全确权流程）。连接模式（临时/长期）+ 控制模式（直接/辅助审查）。
  不依赖 Tailscale 或任何外部服务，纯 Python 标准库零依赖，兼容 Python 3.6+。
  当用户提到：管NAS、控制家里电脑、远程操控设备、多台设备互联、让AI操作电脑、
  设备上的AI协作、远程执行命令、离线留言给AI、软路由排查、家里服务器管理、
  fleet、MCP组网、NAS硬盘管理、远程下载机、AI指挥设备 等需求时使用本技能。
  Trigger: fleet, MCP fleet, 节点互控, agent 组网, agent 通信, store-and-forward,
  peer call, remote exec, 副端安装, 节点发现, agent 协作, task delegation,
  message bus, mesh topology, 离线消息, 邮箱, mailbox, UDP discovery,
  设备码, 邀请链接, invite, pair, 邀请码配对, 远程操控, 连接模式, 控制模式,
  NAS管理, 远程控制电脑, AI操作设备, 软路由, 家里服务器
---

# MCP Fleet · 节点互控系统 v4.5

> 一句话：**让你用聊天的方式指挥家里所有设备干活** —— 手机上说一句，NAS / 电脑 / 软路由上的 AI 就去办，不用记任何命令。

## 这是什么

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器 / 手机…）每台装一个「节点」，即可被**任意支持 MCP 的 AI agent**远程操控。

## 谁需要它（适用场景）

| 你是谁 | 它帮你做什么 | 你需要懂技术吗 |
|--------|------------|--------------|
| 有 NAS 的普通用户 | 对 AI 说"帮我看看 NAS 硬盘还剩多少"，不用记 SSH 命令 | 不用，一条命令装好后就用聊天 |
| 多设备远程党 | 在外面用手机问家里电脑"下载完了没"，AI 远程查看并回复 | 不用 |
| 自托管玩家（飞牛OS/绿联/极空间） | AI 直接操控你的 NAS：查状态、清空间、重启服务 | 不用 |
| 软路由玩家 | 问 AI"家里网怎么这么慢"，AI 自动上软路由排查 | 不用 |
| 开发者 | 云主机 + 本地 + 测试机统一纳管，AI 跨机部署、跨机调试 | 用得更爽 |
| 极客 / 小团队 | 多台设备上的 AI 互发消息、按能力分工（A 写代码 B 跑测试），离线不丢消息 | 这是为你设计的 |

**不会写代码也能用**：每台设备只需执行一条安装命令，之后全部通过自然语言对话完成。

**v4.1 核心进化——自研通讯协议**：

1. **UDP 广播自动发现** — 同网段的 fleet 节点自动互相发现，零配置，不依赖任何外部服务
2. **Store-and-forward 离线邮箱** — agent 发消息给离线 agent，消息存 outbox；目标上线后自动投递，不丢消息
3. **HTTP 消息端点** — `POST /fleet/mail`（收消息）、`GET /fleet/poll`（上线收信）、`POST /fleet/sync`（邮箱同步）
4. **Gossip 邮箱同步** — 节点间定期同步邮箱状态，消息跨节点可达
5. **Tailscale 可选** — 不再依赖 Tailscale；如已安装则作为跨网段加速（端到端加密 + MagicDNS）
6. **连接模式** — `--mode temporary` 临时连接（TTL 自动退出，不装开机自启）或 `permanent` 长期连接
7. **控制模式** — `--control assisted` 本机 agent 辅助审查（远程 agent 需请求批准）或 `direct` 直接控制。agent 运行时可自选切换

## 快速安装

### 一键安装（自研协议，零依赖）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --name nas --port 3100
```

装完后自动：启动 MCP agent → UDP 广播发现同网段其他 fleet 节点 → 自动互连 → 邮箱同步

### 可选：Tailscale 跨网段加速

```bash
sudo bash install.sh --name nas --port 3100 --tailnet my-tailnet --tailnet-key tskey-xxx
```

### 设备码配对（v4.3 核心流程）

安装后自动生成**设备码**（hostname 前缀 + 4 位随机，如 `nas-a3f2`），并醒目显示设备码 + API。

**配对只需一步** — 在 B 设备上输入 A 的设备码和地址：

```bash
# 在设备 B 上运行（A 的设备码和 API 从 A 的安装输出复制）
fleet.py pair nas-a3f2@http://192.168.1.10:3100/mcp
```

配对成功后，两台设备双向通讯已建立。

**三种使用场景**：

| 场景 | A 设备 | B 设备 | 效果 |
|------|--------|--------|------|
| A 操控 B | 有 AI agent | 无 agent（只装 fleet） | A 的 agent 远程操控 B（exec/read/write），B 是 A 的"远程手" |
| B 操控 A | 无 agent | 有 AI agent | B 的 agent 远程操控 A |
| 双向协作 | 有 AI agent | 有 AI agent | 两个 agent 互相发消息、分工协作，完成各自主人的任务 |

### 邀请链接配对（v4.4）

**A 生成邀请码**：
```bash
fleet.py invite                    # 生成邀请码（默认 1 小时有效）
fleet.py invite --ttl 86400        # 邀请码 24 小时有效
```

输出：
```
╔════════════════════════════════════════════════════════════╗
║  邀请码已生成                                               ║
║  设备码 : nas-a3f2                                          ║
║  过期   : 2026-10-17 10:30:00                               ║
║                                                            ║
║  把以下命令发给要连接的设备：                               ║
║  bash install.sh --invite eyJjb2RlIjoi...                  ║
║                                                            ║
║  ⚠  仅分享给信任的人！邀请码包含连接凭据。                 ║
╚════════════════════════════════════════════════════════════╝
```

**B 用邀请码安装**：
```bash
# 一键安装 + 自动确权 + 自动连接
bash install.sh --invite eyJjb2RlIjoi...
```

安装前会显示安全警告，需输入 `yes` 确认。安装完成后自动确权配对。

**安全设计**：
- 邀请码 = base64(json{设备码, API, token, invite_id, 过期时间})
- 邀请码有过期时间（默认 1 小时），过期后不可用
- 邀请码使用一次后失效（防重放）
- 接受方需显式确认信任对方设备
- ⚠ **不要随意使用他人的邀请码安装** — 安装后对方可远程操控你的设备

### 临时连接 + 本机辅助模式

```bash
# 临时连接：TTL 3600s 后自动退出，不装开机自启
sudo bash install.sh --name temp-pc --port 3100 --mode temporary --ttl 3600

# 本机辅助模式：远程 agent 需通过本机 agent 审查才能执行
sudo bash install.sh --name win-pc --port 3100 --control assisted

# 组合：临时 + 辅助
sudo bash install.sh --name borrowed --port 3100 --mode temporary --ttl 7200 --control assisted
```

### 自定义 UDP 发现端口

```bash
sudo bash install.sh --name nas --port 3100 --discover-port 3199
```

## 通讯协议架构

```
Agent A (NAS)              Agent B (云主机)            Agent C (树莓派)
  ├── UDP :3199 广播          ├── UDP :3199 广播          ├── UDP :3199 广播
  ├── HTTP :3100/mcp          ├── HTTP :3100/mcp          ├── HTTP :3100/mcp
  ├── HTTP :3100/fleet/*      ├── HTTP :3100/fleet/*      ├── HTTP :3100/fleet/*
  │   ├── /fleet/mail         │   ├── /fleet/mail         │   ├── /fleet/mail
  │   ├── /fleet/poll         │   ├── /fleet/poll         │   ├── /fleet/poll
  │   ├── /fleet/sync         │   ├── /fleet/sync         │   ├── /fleet/sync
  │   └── /fleet/discover     │   └── /fleet/discover     │   └── /fleet/discover
  ├── mailbox/                ├── mailbox/                ├── mailbox/
  │   ├── inbox.jsonl         │   ├── inbox.jsonl         │   ├── inbox.jsonl
  │   ├── outbox.jsonl        │   ├── outbox.jsonl        │   ├── outbox.jsonl
  │   └── sent.jsonl          │   └── sent.jsonl          │   └── sent.jsonl
  ├── 32 tools                ├── 32 tools                ├── 32 tools
  └── peer_call → B,C         └── peer_call → A,C         └── peer_call → A,B
```

### 消息投递流程

```
msg_send(from=A, to=B, text="hello")
  │
  ├─ B 是本地 agent? → 存 inbox ✓
  ├─ B 在已知 peer? → HTTP POST /fleet/mail → B 收到 ✓
  └─ B 离线/未知? → 存 outbox → delivery daemon 每 15s 重试
                                    │
                                    └─ B 上线 → UDP 发现 → peer_add → 投递成功 ✓
```

### 上线收信流程

```
B 启动 → UDP 广播 → 发现 A,C → peer_add
  → msg_recv(me=B) → 向 A,C 发 GET /fleet/poll?name=B
  → A,C 把给 B 的消息返回 → B 存入 inbox → 收到所有离线消息 ✓
```

## 节点工具清单（38 个）

| 类别 | 工具 | 作用 |
|------|------|------|
| **执行** | `exec` / `exec_async` / `exec_poll` / `exec_result` / `exec_kill` / `exec_jobs` | 同步/后台命令执行 |
| **文件** | `read` / `write` / `edit` / `list_dir` / `search` / `upload` / `download` | 文件读写编辑 + 内容搜索 + base64 传输 |
| **Docker** | `docker_ps` / `docker_logs` / `docker_restart` | 容器管理 |
| **系统** | `systemctl` / `service_logs` / `sysinfo` / `http_get` / `ps` / `kill` | 服务管理 + 系统信息 + 进程管理 |
| **Peer Mesh** | `peers` / `peer_call` / `peer_add` / `peer_remove` / `peer_info` | 节点互控（多跳转发） |
| **消息总线** | `msg_send` / `msg_recv` / `msg_history` | store-and-forward + topic + 离线邮箱 |
| **Agent 协作** | `caps` / `mesh_caps` / `delegate` / `group_form` / `group_list` / `group_disband` | 能力声明 + 任务委派 + 群组 |
| **Mesh 拓扑** | `mesh` | 全网拓扑与在线状态 |
| **v4.2 模式** | `node_mode` / `request_assist` / `assist_queue` | 连接/控制模式 + 辅助请求队列 |

> v4.5 新增：`search`（文件内容/文件名搜索）、`upload`/`download`（base64 文件传输）、`ps`/`kill`（进程管理），以及 `dryRun` 参数（预览修改效果不实际写入）。

## 兼容性（实测可接入的 MCP 客户端）

任何支持 MCP 协议的 AI 客户端均可接入，以下为实测通过：

| 客户端 | 接入方式 | 实测状态 |
|--------|---------|---------|
| 华为云码道 CodeArts | Skill 市场一键安装 | ✅ 大赛指定通道 |
| Claude Code | MCP 配置添加节点 | ✅ |
| Cursor | MCP 配置添加节点 | ✅ |
| Claude Desktop | MCP 配置添加节点 | ✅ |
| Cherry Studio | MCP 配置添加节点 | ✅ |
| 自研 agent | 直接调 `scripts/fleet.py` CLI | ✅（零依赖，任意 agent 可用） |

## 主端 CLI

```bash
python scripts/fleet.py add --name nas --ip 192.168.1.10 --port 3100
python scripts/fleet.py mesh                    # 打印 mesh 拓扑图
python scripts/fleet.py discover                # UDP 广播发现同网段节点
python scripts/fleet.py exec nas "docker ps"   # 在节点执行命令
python scripts/fleet.py msg nas send --to agent-b --text "hello"  # 发消息
python scripts/fleet.py msg nas recv --me agent-a               # 收消息
python scripts/fleet.py msg nas history                         # 消息历史
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
│   ├── fleet_proto.py                # v4.1 自研通讯协议层（UDP发现+store-and-forward+gossip）
│   ├── fleet.py                      # 主端统一 CLI
│   ├── demo.sh                       # 一键演示：双节点启动→跨节点exec→peer互联→拓扑（v4.5+）
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

## v4.1 新增配置

```json
{
  "discover_port": 3199,
  "tailnet_auto_mesh": true,
  "tailnet_auto_trust": true,
  "collab_tools": true,
  "audit_log": true,
  "rate_limit_per_min": 120
}
```

## v4.1 通讯协议端点

| 端点 | 方法 | 作用 |
|------|------|------|
| `/fleet/mail` | POST | 接收来自其他节点的消息，存入本地 inbox |
| `/fleet/poll?name=X` | GET | 拉取给 agent X 的待收消息（上线收信） |
| `/fleet/sync` | POST | 邮箱同步 gossip：交换邮箱摘要 |
| `/fleet/discover` | POST | 节点握手：交换信息，自动互加 peer |
| `/fleet/register_agent` | POST | agent 注册到本节点 |
| `/fleet/status` | GET | 邮箱状态：inbox/outbox/agents |
