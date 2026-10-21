---
name: mcp-fleet
version: 5.0.9
description: 把闲置的NAS、云主机、办公室电脑、软路由变成一张「AI对等协作网」——设备上的AI Agent互发消息、按能力委派任务、离线不丢消息（store-and-forward邮箱），零依赖一条命令装机。自研通讯协议（UDP广播发现+HTTP消息端点+gossip同步），设备码/邀请链接安全确权，41个工具，支持TLS加密、消息落盘加密（PBKDF2派生密钥）、UDP hello防伪签名、Web Dashboard实时拓扑、**Android Termux一键安装**、Windows/macOS一键安装（Windows计划任务+macOS launchd双守护：开机自启与崩溃自动重启）、125用例测试套件，不依赖任何外部服务。当用户提到：管NAS、控制家里电脑、远程操控设备、多台设备互联、让AI操作电脑、设备上的AI协作、远程执行命令、离线留言给AI、软路由排查、家里服务器管理、设备监控面板、Windows远程管理、fleet、MCP组网等需求时使用本技能。
tags: 设备互联, MCP, 远程操控, 效率工具, 开发工具, 自研协议, agent协作, 离线邮箱
---

# MCP Fleet · 节点互控系统 v5.0

> 软件包下载：[GitHub Release v5.0.3](https://github.com/guoxpeng/skill-mcp-fleet/releases/tag/v5.0.3) ｜
> 全版本更新说明：[CHANGELOG.md](https://github.com/guoxpeng/skill-mcp-fleet/blob/main/CHANGELOG.md)

## 这是什么

> 一句话：**让你用聊天的方式指挥家里所有设备干活** —— 手机上说一句，NAS/电脑/软路由上的 AI 就去办。

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器 / 手机…）每台装一个「节点」，即可被**任意支持 MCP 的 AI agent**远程操控。

**v5.0 安全大版本**（v5.0.7 追加 3 项加固、v5.0.8 当日自查修复其 4 处回归，见 CHANGELOG）：

1. **邮箱落盘加密** — 节点间消息（inbox/outbox/sent）以 `enc1$` 前缀密文落盘，密钥由配对 token 经 PBKDF2 派生，物理拷走硬盘也读不到明文
2. **UDP hello 防伪签名** — 已配对节点的发现包必须携带 HMAC 签名，冒充已知节点直接拒收（对 v4.x 旧节点保持兼容）
3. **Web Dashboard** — 浏览器打开 `http://节点IP:端口/fleet/ui`，实时看全网拓扑、在线状态、消息流（零 CDN 依赖单文件页面）
4. **跨平台安装与守护** — `scripts/install.py` 三平台一键安装：Windows 防火墙+计划任务双守护（登录自启+每分钟探活自动拉起）、macOS launchd（RunAtLoad+KeepAlive 崩溃重启）、Linux systemd（install.sh）
5. **测试套件** — 130 个测试（含真实 UDP socket 验签用例），`python3 -m pytest tests/ -q` 一键回归
6. **只读模式** — `--read-only` 启动：exec/write/edit 等 19 个变更类工具全部拒绝（查询类照常，含旧版 nas_ 前缀容错），即「审计观察节点」；连接级并发限流（`max_http_threads=64`，超限 503）与收件箱 msg_id 去重（防 gossip 双路径重复投递，重启后窗口自动回填）
7. **修复死锁** — 自测发现并修复 gossip 同步路径的重入死锁（v4.1 引入至今的隐蔽 bug，`Lock` → `RLock`）

**v4.6 文档增强**：新增「谁需要它」适用场景、一键演示脚本 `scripts/demo.sh`（30 秒看懂双节点互联）、兼容性清单；修复历史文档中工具数描述（统一为 41 个）。

> 🎬 **20 秒演示视频**：[https://mcp-fleet-demo.obs.cn-north-4.myhuaweicloud.com/fleet-demo.mp4](https://mcp-fleet-demo.obs.cn-north-4.myhuaweicloud.com/fleet-demo.mp4)
> 云上项目远程查询 + 出门在外管理家里设备（下载确认 / 远程关机），两大真实场景一次看懂。

**v4.5 新增能力**：

1. **TLS 加密** — `--tls-cert/--tls-key` 启动 HTTPS，跨网段通讯加密
2. **审计日志** — 每次工具调用自动记录（时间/IP/工具/参数）到 `audit.log`，安全可追溯
3. **dryRun 预览** — `edit` 工具支持 `dryRun=true` 预览修改效果而不实际写入，防误操作
4. **6 个新工具** — `search`（内容/文件名搜索）、`upload`/`download`（跨节点文件传输）、`ps`/`kill`（进程管理）、`net_check`（网络连通诊断）
5. **参数命名统一** — `filePath` 规范参数名（兼容旧名），Python 3.6 完全兼容

**v4.1 核心进化——自研通讯协议**：

1. **UDP 广播自动发现** — 同网段的 fleet 节点自动互相发现，零配置，不依赖任何外部服务
2. **Store-and-forward 离线邮箱** — agent 发消息给离线 agent，消息存 outbox；目标上线后自动投递，不丢消息
3. **HTTP 消息端点** — `POST /fleet/mail`（收消息）、`GET /fleet/poll`（上线收信）、`POST /fleet/sync`（邮箱同步）
4. **Gossip 邮箱同步** — 节点间定期同步邮箱状态，消息跨节点可达
5. **Tailscale 可选** — 不再依赖 Tailscale；如已安装则作为跨网段加速（端到端加密 + MagicDNS）
6. **连接模式** — `--mode temporary` 临时连接（TTL 自动退出，不装开机自启）或 `permanent` 长期连接
7. **控制模式** — `--control assisted` 本机 agent 辅助审查（远程 agent 需请求批准）或 `direct` 直接控制。agent 运行时可自选切换

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

### 一键演示（30 秒看懂它怎么工作）

不想先装？跑一遍自带的演示脚本——自动在本机拉起两个节点（demo-nas / demo-cloud），自动互发现、跨节点执行命令、发消息、打印 mesh 拓扑，全程无需任何配置：

```bash
bash scripts/demo.sh
```

演示结束自动清理进程与临时文件。评测评测、第一次接触本技能的用户，建议从这里开始。

## v5.0 新能力速览

### Web Dashboard（浏览器实时监控）

节点启动后，浏览器打开：

```
http://<节点IP>:<端口>/fleet/ui#<你的节点token>
```

- token 放在 URL `#` 之后，**不会发送到服务器**（仅存于浏览器 sessionStorage）
- 页面 5 秒自动刷新：KPI 卡片（在线节点数 / 消息总数 / 待投递）、mesh 网络拓扑图、最近消息流
- 单文件零 CDN 依赖，内网/离线环境可用

### Windows 一键安装（跨平台安装器）

```powershell
# 下载 scripts/install.py 后，管理员 PowerShell 中执行：
python install.py --name office-pc --port 3100
```

自动完成：文件部署 → 配置生成 → 启动脚本 → 防火墙放行 → 开机自启 → 进程保护。

```bash
# Windows：计划任务双守护（pythonw 无窗口登录自启 + 每分钟探活 /health 异常自动拉起）
python install.py --name office-pc --auto-start

# macOS：launchd（RunAtLoad 登录自启 + KeepAlive 崩溃 10s 自动重启）
python3 install.py --name mac-mini --auto-start
```

卸载用 `python install.py --uninstall`（保留邮箱数据，自动清理计划任务/launchd 配置）。Linux 用户仍推荐 `install.sh`（systemd Restart=always + watchdog timer）。

### Android Termux 一键安装（手机变主控 / 副端）

Termux 两条路径：**主控 CLI**（免 git clone 装 fleet.py 当遥控器）或**完整副端**（手机当被控节点）。

```bash
# 方式一：主控 CLI 一键部署（手机当遥控器，操控家里 NAS/电脑/云主机）
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh | bash -s -- --cli-only

# 装完后直接用中文菜单：
python3 ~/.mcp-fleet-cli/fleet.py menu

# 方式二：完整副端安装（手机被其他设备操控 / 提供算力）
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh | bash -s -- --name my-phone --port 3100 --generate-token --require-auth

# 装完拿令牌（在主端 mcp.json 配对）：
grep auth_token ~/.fleet/my-phone_mcp/mcp_agent_config.json
```

- 自动检测 Termux 环境，pkg 装 python，nohup 守护（supervisor 3s 拉起，kill -9 自动复活）
- `--cli-only` 只装 fleet.py 单文件（零依赖），免 git clone 全仓库，适合手机当遥控器
- 中文交互菜单：`python3 ~/.mcp-fleet-cli/fleet.py menu`，16 项操作，手机/桌面通用

### 邮箱加密与 hello 防伪（默认自动生效）

- 配对成功的节点之间，消息**自动加密落盘**（`enc1$` 前缀），无需任何配置；v4.x 明文邮箱读取时自动兼容
- 已配对节点的 UDP 发现包**自动携带签名**，伪造已知节点名会被拒收并计数（`mesh` 可见）
- 与旧版本混部不破坏网络：无签名的 v4.x 节点照常发现

### 测试套件（130 个用例）

```bash
python3 -m pytest tests/ -q    # 2.6s 全量回归
```

覆盖：加解密往返与防篡改（12）、邮箱读写/并发/投递/维护压缩/msg_id 去重（47）、hello 验签策略与真实 UDP socket（10）、工具层含 mail 限流/audit 轮转/三平台守护等价/只读模式/CLI 完整性（48）、双节点端到端集成（8，含密文落盘断言）、消息投递路由三分支（6）——共 131 个用例（130 通过 + 1 跳过：PREFIX 为空场景）。本套件在开发中真实抓出一个隐蔽死锁（见 `references/安全审计报告.md`）；四轮递进审查与修复全程见 `references/四轮审查-安全稳定易用.md`。

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
  ├── 41 tools                ├── 41 tools                ├── 41 tools
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

## 节点工具清单（41 个，v4.5 实测）

| 类别 | 工具 | 作用 |
|------|------|------|
| **执行** | `exec` / `exec_async` / `exec_poll` / `exec_result` / `exec_kill` / `exec_jobs` | 同步/后台命令执行 |
| **进程** | `ps` / `kill` | 进程查看与终止（v4.5 新增） |
| **文件** | `read` / `write` / `edit`（dryRun 预览）/ `list_dir` / `search` | 文件读写编辑搜索 |
| **传输** | `upload` / `download` | 跨节点文件上传下载（v4.5 新增） |
| **Docker** | `docker_ps` / `docker_logs` / `docker_restart` | 容器管理 |
| **系统** | `systemctl` / `service_logs` / `sysinfo` / `http_get` / `net_check` | 服务管理 + 系统信息 + 网络诊断 |
| **Peer Mesh** | `peers` / `peer_call` / `peer_add` / `peer_remove` / `peer_info` | 节点互控（多跳转发） |
| **消息总线** | `msg_send` / `msg_recv` / `msg_history` | store-and-forward + topic + 离线邮箱 |
| **Agent 协作** | `caps` / `mesh_caps` / `delegate` / `group_form` / `group_list` / `group_disband` | 能力声明 + 任务委派 + 群组 |
| **Mesh 拓扑** | `mesh` | 全网拓扑与在线状态 |
| **v4.2 模式** | `node_mode` / `request_assist` / `assist_queue` | 连接/控制模式 + 辅助请求队列 |

## 兼容性

- 本技能节点是**标准 MCP 服务器**（HTTP + JSON-RPC，protocolVersion 2024-11-05），`initialize` → `tools/list` → `tools/call` 全链路已实测（`scripts/demo.sh` 即协议级自检）
- 任何支持 MCP 协议的 AI 客户端均可直接接入：**华为云 CodeArts、Claude Code、Cursor** 等
- 服务端仅依赖 Python 3.6+ 标准库，Linux / macOS / 容器 / 树莓派均可运行，零第三方依赖

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
├── pytest.ini                        # 测试配置（faulthandler 超时保护）
├── scripts/
│   ├── mcp_agent.py                  # 节点 MCP 服务器（核心，41 个工具，TLS+审计+Dashboard 路由）
│   ├── fleet_proto.py                # 自研通讯协议层（UDP发现+store-and-forward+gossip+加密+签名）
│   ├── dashboard_html.py             # v5.0 Web Dashboard 单文件页面（零 CDN 依赖）
│   ├── install.py                    # v5.0.6 跨平台安装器 + 平台守护 + Termux CLI 一键部署
│   ├── fleet.py                      # 主端统一 CLI
│   ├── fleet_chat.py                 # AI 间消息 CLI
│   ├── fleet_watch.py                # 保活守护
│   ├── fleet-directory.py            # 地址目录服务
│   ├── install.sh                    # 一键安装脚本（Linux，含 systemd）
│   ├── install-fleet-node.sh         # 节点安装辅助
│   ├── add_fleet_node.py             # 注册节点到 MCP 配置
│   ├── call_node.py                  # 免重启直接调工具
│   ├── probe_fleet.py                # 连通性诊断
│   ├── demo.sh                       # 一键演示：双节点互联 30 秒上手
│   ├── make-offline.py               # 生成离线安装包（systemd 单元由 install.sh 运行时生成）
│   └── mcp_agent_config.example.json # 配置模板
├── tests/                            # v5.0 测试套件（130 用例，pytest 一键回归）
│   ├── conftest.py                   # sys.path 公共配置
│   ├── test_crypto.py                # 加解密往返 / 防篡改 / 密钥派生（12）
│   ├── test_mail_engine.py           # 邮箱读写 / 加密落盘 / 并发 / 投递三分支（24）
│   ├── test_hello.py                 # hello 验签策略 + 真实 UDP socket（10）
│   ├── test_agent_tools.py           # 工具参数与安全护栏（7）
│   └── test_agent_integration.py     # 双节点端到端集成（8）
└── references/
    ├── vision-and-scenarios.md       # 场景故事 + 演进蓝图
    ├── 传播度证据.md                  # 三层传播渠道数据（附复现命令）
    ├── 加固操作手册.md
    ├── 安全审计报告.md                # 含 v5.0 自发现自修复记录
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
| `/fleet/ui` | GET | v5.0 Web Dashboard（单文件页面，token 经 URL fragment 传入） |
| `/fleet/ui/data` | GET | Dashboard 数据接口（需 token 鉴权，5s 轮询） |
