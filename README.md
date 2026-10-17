# MCP Fleet · 节点互控系统 v4.0

> 零依赖 · 纯 Python 标准库 · 兼容 Python 3.6+ · 32 个工具

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器 / 手机…）每台装一个「节点」，即可被**任意支持 MCP 的 AI agent / 客户端**远程操控 —— WorkBuddy、Claude Desktop、Claude Code、Cursor、Cline、Cherry Studio、Codex 或你自己写的 agent 都行，不绑定某一家。

**v4.0 不分主端副端**：每个节点既能被控制，也能去控制别的节点。谁发起连接，谁就是这一次的控制方；两台节点 `fleet pair` 一下就能**双向互控**。

## v4.0 五大进化

| # | 进化方向 | 核心能力 |
|---|---------|----------|
| 1 | **Tailscale 自动组网** | `--tailnet` 一键加入 tailnet，新设备上线自动发现全网节点，零配置互连 |
| 2 | **Agent 协作协议** | 节点自动声明能力（Docker/GPU/网络），按能力委派任务，多节点临时组队 |
| 3 | **消息总线** | pub/sub 广播 + topic 过滤 + 持久化邮箱，agent 间异步通信不丢消息 |
| 4 | **安全加固** | tailnet 内自动互信（端到端加密）、审计日志、令牌桶限流 |
| 5 | **Mesh 拓扑可视化** | `fleet mesh` 一键打印全网拓扑图（谁连谁、谁在线、各节点能力） |

## 架构

```
控制方（任意 MCP 客户端 / agent，或任意一个节点自带的 fleet）
  └── MCP 配置
        ├── nas      → http://100.64.1.10:3100/mcp   (Tailscale)
        ├── cloud    → http://100.64.2.5:3100/mcp    (Tailscale)
        └── pi       → http://100.64.3.20:3100/mcp   (Tailscale)

节点（各台设备，各跑一个 mcp_agent.py，开机自启）
  ├── 命令在设备本机执行，不经 SSH —— 更快、更稳
  ├── 节点之间 peer_call 互控，A ⇄ B 双向，A → B → C 多跳进内网
  ├── msg_send/msg_recv 异步消息总线，topic 广播
  ├── caps 能力声明，delegate 按能力委派任务
  └── Tailscale tailnet 端到端加密，自动发现自动互信
```

## 节点工具清单（32 个）

| 类别 | 工具 | 作用 |
|------|------|------|
| **执行** | `exec` | 执行 shell 命令，`sudo:true` 可提权 |
| | `exec_async` / `exec_poll` / `exec_result` / `exec_kill` / `exec_jobs` | 后台任务：启动→取增量→等待完成→终止→列表 |
| **文件** | `read` / `write` / `edit` / `list_dir` | 读文件（可按行）/ 写追加 / 文本块替换 / 列目录 |
| **Docker** | `docker_ps` / `docker_logs` / `docker_restart` | 容器列表 / 日志 / 重启 |
| **系统** | `systemctl` / `service_logs` / `sysinfo` / `http_get` | 服务启停 / 日志 / 系统概览 / 本机 HTTP |
| **Peer Mesh** | `peers` / `peer_info` | 列出对等节点（探活）/ 本节点信息 |
| | `peer_call` | 经本节点调另一个节点的任意工具，可多跳转发 |
| | `peer_add` / `peer_remove` | 增删对等节点 |
| **消息总线** | `msg_send` | 发消息（to=* 广播，topic=主题过滤） |
| | `msg_recv` | 收消息（wait 阻塞等待，topic 过滤，peek 只看） |
| | `msg_history` | 查看邮箱历史（可按 topic 过滤） |
| **Agent 协作** | `caps` | 返回本节点能力清单（Docker/GPU/Tailscale/网络/工具） |
| | `mesh_caps` | 查询 mesh 内所有节点能力（发现谁有某能力） |
| | `delegate` | 按能力委派任务（自动找匹配节点执行） |
| | `group_form` / `group_list` / `group_disband` | 多节点工作群组 |
| **Mesh 拓扑** | `mesh` | 全网拓扑：本节点 + 所有 peers 的在线状态和能力 |

## 一、安装

### 方式 A：一键安装 + Tailscale 自动组网（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --name nas --port 3100 --tailnet my-tailnet --tailnet-key tskey-xxx
```

装完后自动完成：安装 Tailscale → 加入 tailnet → 启动 MCP agent → 发现其他 fleet 节点 → 自动互连

### 方式 B：普通安装（内网，不需要 Tailscale）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --name nas --port 3100
```

### 方式 C：公网隧道

```bash
sudo bash install.sh --name phone --port 3100 --tunnel --generate-token --require-auth
```

### 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--name` | 节点标识 | 主机名 |
| `--port` | 监听端口 | `3100` |
| `--tailnet` | Tailscale tailnet 名，自动安装并加入 | 空（不组网） |
| `--tailnet-key` | Tailscale auth key（免交互登录） | 空（交互登录） |
| `--tunnel` | 起公网隧道 | 关 |
| `--auth-token` | 访问令牌 | 空 |
| `--generate-token` | 自动生成令牌 | 关 |
| `--require-auth` | 强制鉴权 | 关 |
| `--allow-ips` | IP/CIDR 白名单 | 空 |
| `--uninstall` | 卸载 | — |

## 二、接入主端

### 用 fleet.py（零依赖统一 CLI）

```bash
python scripts/fleet.py add --name nas --ip 100.64.1.10 --port 3100    # Tailscale 地址
python scripts/fleet.py list                    # 列已注册节点
python scripts/fleet.py mesh                    # 打印 mesh 拓扑图
python scripts/fleet.py exec nas "uptime"       # 在节点执行命令
python scripts/fleet.py call nas --tool caps    # 查节点能力
python scripts/fleet.py call nas --tool mesh_caps  # 查全网能力
python scripts/fleet.py pair nas                # 双向配对
python scripts/fleet.py health --deep           # 全网体检
```

### 手动配置 MCP 客户端

```json
{
  "mcpServers": {
    "nas": {
      "type": "http",
      "url": "http://100.64.1.10:3100/mcp",
      "headers": { "Authorization": "Bearer <令牌>" }
    }
  }
}
```

## 三、Agent 协作示例

### 按能力委派任务

```
Agent A (无 Docker):
  → delegate(capability="docker", tool="docker_ps", arguments={})
  → 自动发现 Agent B 有 Docker
  → Agent B 执行 docker_ps
  → 结果返回给 Agent A
```

### 多节点组队

```
Agent A:
  → group_form(name="deploy-team", members=["nas", "cloud", "pi"], purpose="蓝绿部署")
  → 三个节点收到邀请
  → A 委派编译给 cloud（8CPU），测试给 pi，部署给 nas
  → group_disband(name="deploy-team")
```

### 消息总线

```
Agent A:
  → msg_send(from="a", to="*", topic="alert", text="磁盘空间不足")
  → 所有节点收到 alert 主题消息

Agent B:
  → msg_recv(me="b", topic="alert", wait=60)
  → 收到 A 的广播
```

## 四、Mesh 拓扑可视化

```bash
python scripts/fleet.py mesh
```

输出示例：
```
============================================================
  Fleet Mesh 拓扑
============================================================
  [*] nas              http://100.64.1.10:3100/mcp  [Docker, GPU, TS:100.64.1.10, systemd, 4CPU, 8192MB]
      |- cloud           在线 tailnet
      |- pi              在线 tailnet
  [*] cloud             http://100.64.2.5:3100/mcp   [Docker, TS:100.64.2.5, systemd, 8CPU, 32768MB]
      |- nas             在线 tailnet
      |- pi              在线 tailnet
============================================================
```

## 五、安全

### Tailscale 自动信任

同一 tailnet 的节点自动互信——Tailscale 提供 WireGuard 端到端加密，不需要逐对配 token。跨 tailnet 的连接仍需显式 token。

### 审计日志

每次 `peer_call` / `delegate` 自动记录到 `audit.log`：
```
2026-10-17 08:30:15 | peer_call | nas -> cloud | docker_ps | ok
2026-10-17 08:30:20 | delegate | nas -> cloud | exec | delegated
```

### 限流

每节点入站 `peer_call` 默认限 120 次/分钟（令牌桶），防过载。可配置 `rate_limit_per_min`。

## 六、配置

配置文件：`/opt/<name>_mcp/mcp_agent_config.json`

```json
{
  "name": "nas",
  "version": "4.0.0",
  "auth_token": "",
  "peer_tools": true,
  "tailnet_auto_mesh": true,
  "tailnet_auto_trust": true,
  "collab_tools": true,
  "audit_log": true,
  "rate_limit_per_min": 120
}
```

## 七、目录结构（标准 Skill 格式）

```
skill-mcp-fleet/
├── SKILL.md                          # AgentSkill 规范
├── README.md                         # 本文件
├── mcp_agent_config.example.json     # 配置模板
├── scripts/
│   ├── mcp_agent.py                  # 节点 MCP 服务器（核心）
│   ├── fleet.py                      # 主端 CLI
│   ├── fleet_chat.py                 # 消息 CLI
│   ├── fleet_watch.py                # 保活守护
│   ├── fleet-directory.py            # 地址目录
│   ├── install.sh                    # 安装脚本
│   └── ...
└── references/
    ├── 加固操作手册.md
    ├── 安全审计报告.md
    └── 云服务器-副端安装-一条命令.md
```

## 八、日常运维

```bash
# systemd 机型
systemctl status <name>-mcp
journalctl -u <name>-mcp -f

# 容器机型
bash /opt/<name>_mcp/mcp-ctl.sh status

# 查审计日志
tail -f /opt/<name>_mcp/audit.log

# 查消息邮箱
cat /opt/<name>_mcp/mailbox.jsonl
```

## 从旧版本升级

重跑安装命令即可，配置文件保留不会被覆盖：

```bash
sudo bash install.sh --name nas --port 3100 --tailnet my-tailnet
```

## 变更日志

| 版本 | 变更 |
|------|------|
| v4.0 | Tailscale 自动组网 + Agent 协作协议 + 消息总线 + 安全加固 + Mesh 拓扑 |
| v3.5 | 节点不分主副，双向互控，peer_call/peer_add/peer_remove |
| v3.4 | 保活真实探活 + 连接超时 + 孤儿进程清理 |
| v3.1 | 默认值改安全 + 请求体限制 + SSE 并发限制 |
| v3.0 | Bearer 鉴权 + IP 白名单 + 暴力破解节流 |
