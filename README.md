# MCP Fleet · 节点互控系统

> 零依赖 · 纯 Python 标准库 · 兼容 Python 3.6+ · 38 个工具（32 原有 + 6 新增增强） · 自研通讯协议

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器 / 手机…）每台装一个「节点」，即可被**任意支持 MCP 的 AI agent / 客户端**远程操控 —— WorkBuddy、Claude Desktop、Claude Code、Cursor、Cline、Cherry Studio、Codex 或你自己写的 agent 都行，不绑定某一家。

**v4.1 核心进化——自研通讯协议**：agent 间可以直接通信，不依赖 Tailscale 或任何外部服务。在线 agent 消息直接投递，离线 agent 消息存邮箱（store-and-forward），上线后自动收信。UDP 广播自动发现同网段节点，gossip 协议同步邮箱。

## v4.1 进化

| # | 进化方向 | 核心能力 |
|---|---------|----------|
| 1 | **自研通讯协议** | UDP 广播发现 + HTTP 消息端点 + store-and-forward 离线邮箱 + gossip 同步 |
| 2 | **Agent 协作协议** | 节点自动声明能力（Docker/GPU/网络），按能力委派任务，多节点临时组队 |
| 3 | **消息总线** | msg_send/msg_recv/msg_history，topic 广播，离线不丢消息 |
| 4 | **安全加固** | 审计日志、令牌桶限流、Tailscale 可选端到端加密 |
| 5 | **Mesh 拓扑可视化** | `fleet mesh` 一键打印全网拓扑图（谁连谁、谁在线、各节点能力） |
| 6 | **连接模式** | `--mode temporary`（TTL 自动退出）/ `permanent`（长期在线） |
| 7 | **控制模式** | `--control assisted`（本机 agent 审查）/ `direct`（直接控制），运行时可切换 |
| 8 | **设备码配对** | 安装自动生成设备码，`fleet pair CODE@URL` 一键配对，A 有 agent 操控 B，双 agent 协作 |
| 9 | **邀请链接** | `fleet invite` 生成邀请码，对方 `install.sh --invite CODE` 一键安装+自动连接+确权 |

## 架构

```
控制方（任意 MCP 客户端 / agent，或任意一个节点自带的 fleet）
  └── MCP 配置
        ├── nas      → http://192.168.1.10:3100/mcp
        ├── cloud    → http://10.0.0.5:3100/mcp
        └── pi       → http://192.168.1.20:3100/mcp

节点（各台设备，各跑一个 mcp_agent.py，开机自启）
  ├── 命令在设备本机执行，不经 SSH —— 更快、更稳
  ├── 节点之间 peer_call 互控，A ⇄ B 双向，A → B → C 多跳进内网
  ├── UDP :3199 广播自动发现同网段节点
  ├── HTTP /fleet/* 端点：消息投递、上线收信、邮箱同步
  ├── mailbox/ 邮箱：inbox + outbox + sent（store-and-forward）
  └── 38 个工具（32 原有 + 6 新增增强）：exec/read/write/edit/docker/systemctl + peer + msg + collab + mesh
```

## 快速开始（3 步）

```
1. 设备 A 安装：  sudo bash install.sh --port 3100
   → 显示设备码 nas-a3f2 和 API http://192.168.1.10:3100/mcp

2. 设备 B 安装：  sudo bash install.sh --port 3100
   → 显示设备码 win-b7k9 和 API http://192.168.1.20:3100/mcp

3. 在 B 上配对：  fleet.py pair nas-a3f2@http://192.168.1.10:3100/mcp
   → 双向通讯建立！
```

**然后**：
- A 有 Claude/Cursor → A 的 AI agent 可以 `exec`、`read`、`write` 操控 B
- B 有 Claude/Cursor → B 的 AI agent 也可以操控 A
- 两个 agent 可以 `msg_send` 互发消息、分工协作

### 邀请链接配对

更简单的配对方式 — A 生成邀请码，B 一键安装自动连接：

```bash
# A 生成邀请码
fleet.py invite
# → 输出: bash install.sh --invite eyJjb2RlIjoi...

# B 用邀请码安装（自动确权 + 自动连接）
bash install.sh --invite eyJjb2RlIjoi...
```

⚠ **安全警告**：邀请码包含连接凭据，仅分享给信任的人。安装后对方可远程操控你的设备。

邀请码有过期时间（默认 1 小时），使用一次后失效。

## 快速安装

最快的方式就是把本项目地址丢发给ai，让本地AI使用ssh连接一次云服务器，让AI自己安装本项目。

### 一键安装（自研协议，零依赖）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --name nas --port 3100
```

装完后自动：启动 MCP agent → UDP 广播发现同网段其他 fleet 节点 → 自动互连 → 邮箱同步

### 临时连接 + 本机辅助

```bash
# 临时连接：TTL 3600s 后自动退出
sudo bash install.sh --name temp-pc --port 3100 --mode temporary --ttl 3600

# 本机辅助：远程 agent 需本机 agent 审查批准
sudo bash install.sh --name win-pc --port 3100 --control assisted
```

**连接模式**：
- `permanent`（默认）：装 systemd 开机自启，永远在线。适合 NAS、服务器。
- `temporary`：不装自启，TTL 到了自动退出，退出时发 goodbye 通知其他节点。适合借来的电脑、临时工位。

**控制模式**：
- `direct`（默认）：远程 agent 直接调 exec/read/write 等工具，完全控制。
- `assisted`：远程 agent 只能用安全工具（sysinfo/read/list_dir/msg_*），危险操作需通过 `request_assist` 提交请求，本机 agent 用 `assist_queue` 审查批准后才执行。agent 运行时可用 `node_mode` 切换。

### 可选：Tailscale 跨网段加速

```bash
sudo bash install.sh --name nas --port 3100 --tailnet my-tailnet --tailnet-key tskey-xxx
```

Tailscale 不是必需的。如果你只需要同网段通信，不需要安装 Tailscale。如果你需要跨网段（比如 NAS 在家、云主机在公网），Tailscale 提供端到端加密的虚拟网络，fleet 在其上自动构建 agent mesh。

## 通讯协议

### UDP 广播发现

每个节点启动后，每 30 秒在 UDP 端口 3199 广播一个 hello 包：
```json
{"proto": "fleet", "ver": 1, "name": "nas", "port": 3100, "caps": {"docker": true, "tools": 32}}
```
同网段的其他 fleet 节点收到后，自动 `peer_add`，零配置组网。

### Store-and-forward 消息投递

```
msg_send(from="agent-a", to="agent-b", text="hello")
  │
  ├─ agent-b 在本节点? → 存 inbox ✓（本地投递）
  ├─ agent-b 在已知 peer? → HTTP POST /fleet/mail → 直接投递 ✓
  └─ agent-b 离线/未知? → 存 outbox → delivery daemon 每 15s 重试
                                    │
                                    └─ agent-b 上线 → UDP 发现 → peer_add → 投递成功 ✓
```

### 上线收信

```
agent-b 节点启动 → UDP 广播 → 发现其他节点 → peer_add
  → msg_recv(me="agent-b") → 向所有 peer 发 GET /fleet/poll?name=agent-b
  → 各 peer 把给 agent-b 的消息返回 → 存入本地 inbox → 收到所有离线消息 ✓
```

### HTTP 端点

| 端点 | 方法 | 作用 |
|------|------|------|
| `/fleet/mail` | POST | 接收消息，存入本地 inbox |
| `/fleet/poll?name=X` | GET | 拉取给 agent X 的待收消息（上线收信） |
| `/fleet/sync` | POST | 邮箱同步 gossip：交换邮箱摘要 |
| `/fleet/discover` | POST | 节点握手：交换信息，自动互加 peer |
| `/fleet/register_agent` | POST | agent 注册到本节点 |
| `/fleet/status` | GET | 邮箱状态 |

## 工具清单（38 个）

| 类别 | 工具 | 作用 |
|------|------|------|
| **执行** | `exec` / `exec_async` / `exec_poll` / `exec_result` / `exec_kill` / `exec_jobs` | 同步/后台命令执行 |
| **文件** | `read` / `write` / `edit` / `list_dir` | 文件读写编辑 |
| **Docker** | `docker_ps` / `docker_logs` / `docker_restart` | 容器管理 |
| **系统** | `systemctl` / `service_logs` / `sysinfo` / `http_get` | 服务管理 + 系统信息 |
| **Peer Mesh** | `peers` / `peer_call` / `peer_add` / `peer_remove` / `peer_info` | 节点互控（多跳转发） |
| **消息总线** | `msg_send` / `msg_recv` / `msg_history` | store-and-forward + topic + 离线邮箱 |
| **Agent 协作** | `caps` / `mesh_caps` / `delegate` / `group_form` / `group_list` / `group_disband` | 能力声明 + 任务委派 + 群组 |
| **Mesh 拓扑** | `mesh` | 全网拓扑与在线状态 |

## CLI 用法

```bash
# 节点管理
python scripts/fleet.py add --name nas --ip 192.168.1.10 --port 3100
python scripts/fleet.py list                         # 列出所有节点
python scripts/fleet.py mesh                         # 打印 mesh 拓扑图
python scripts/fleet.py discover                     # UDP 广播发现同网段节点
python scripts/fleet.py pair nas                     # 双向配对
python scripts/fleet.py health --deep                # 全网体检

# 远程执行
python scripts/fleet.py exec nas "docker ps"         # 在节点执行命令
python scripts/fleet.py call nas peer_call --peer cloud --tool sysinfo  # 多跳调用

# 消息总线
python scripts/fleet.py msg nas send --to agent-b --text "hello"   # 发消息
python scripts/fleet.py msg nas recv --me agent-a                  # 收消息
python scripts/fleet.py msg nas history                             # 消息历史

# Agent 协作
python scripts/fleet.py call nas caps               # 查看节点能力
python scripts/fleet.py call nas mesh_caps           # 查看全网能力
python scripts/fleet.py call nas delegate --capability gpu --task "train model"  # 委派任务
```

## 配置

```json
{
  "node_name": "nas",
  "listen_port": 3100,
  "auth_token": "",
  "discover_port": 3199,
  "enable_docker": true,
  "collab_tools": true,
  "audit_log": true,
  "rate_limit_per_min": 120,
  "tailnet_auto_mesh": false,
  "tailnet_auto_trust": false
}
```

## 目录结构

```
mcp-fleet/
├── SKILL.md                          # AgentSkill 规范
├── mcp_agent_config.example.json     # 配置模板
├── scripts/
│   ├── mcp_agent.py                  # 节点 MCP 服务器（核心，38 个工具（32 原有 + 6 新增增强））
│   ├── fleet_proto.py                # v4.1 自研通讯协议层
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

## 设计哲学

- **零依赖**：纯 Python 3 标准库，不需要 pip install 任何东西
- **自研协议**：不依赖 Tailscale / 任何外部服务，agent 间直接通信
- **Store-and-forward**：离线 agent 不丢消息，上线自动收信
- **不分主副**：每个节点既能被控制，也能控制别的节点
- **兼容 Tailscale**：如已安装则作为跨网段加速（可选，非必需）

## License

MIT


---

## v4.5 增强更新（社区贡献）

### Bug 修复
- **P0: Python 3.6 兼容性** — `ThreadingHTTPServer` 在 Python 3.6 上不存在，添加 try/except fallback
- **P0: 参数命名统一** — `list_dir` 的 `path` 参数统一为 `filePath`，保留 `path` 作为兼容别名
- **P0: 工具数量修正** — README 工具数与实际代码同步

### 安全增强
- **P1: TLS 支持** — 新增 `--tls-cert` / `--tls-key` 参数，支持 HTTPS 加密传输
- **P1: 操作审计日志** — `exec`/`write`/`edit`/`docker_restart`/`systemctl` 调用自动记录到 `audit.log`
- **P1: 安全边界文档化** — `_check_path` 的限制范围在代码注释中明确说明

### 功能增强
- **P2: `search` 工具** — 文件内容搜索(grep)和文件名搜索(find)，结构化输出
- **P2: `upload` / `download` 工具** — base64 编码文件传输，支持二进制文件
- **P2: `ps` / `kill` 工具** — 进程列表和终止进程，带信号选择
- **P2: `net_check` 工具** — 端口探测、DNS 解析、列出本机监听端口
- **P2: `edit` dryRun** — `dryRun=true` 预览修改效果但不实际写入
- **P2: 错误消息 i18n** — 错误提示改为中英双语
