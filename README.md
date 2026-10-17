# MCP Fleet · 参赛作品提交包（v5.0）

![Release](https://img.shields.io/github/v/release/guoxpeng/skill-mcp-fleet) ![Stars](https://img.shields.io/github/stars/guoxpeng/skill-mcp-fleet) ![License](https://img.shields.io/badge/license-MIT-blue) ![Python](https://img.shields.io/badge/python-3.6%2B-blue) ![Tests](https://img.shields.io/badge/tests-83%20passed-brightgreen)

> **2026 华为开发者大赛 · 小红书 Skill 创新赛**
> 赛道：**通用赛道 · 开发工具**
> 作者：guoxpeng ｜ 开源仓库：https://github.com/guoxpeng/skill-mcp-fleet
>
> **软件包下载**：[GitHub Release v5.0.3](https://github.com/guoxpeng/skill-mcp-fleet/releases/tag/v5.0.3)（`mcp-fleet-v5.0.3.zip`）
> **版本历史**：[CHANGELOG.md](https://github.com/guoxpeng/skill-mcp-fleet/blob/main/CHANGELOG.md)（16 个版本演进，git tag 与参赛版本号对齐）
>
> 觉得有用？欢迎去仓库点一个 **Star** ⭐ —— 你的star会让更多人看到这张「AI 协作网」。

---

## 一、核心叙事

**别人做的是 AI 和 AI 聊天，我做的是 AI 和 AI 协作干活。**

MCP Fleet 是一张 **AI 对等协作网络**：每台设备装一个「节点」（NAS / 云主机 / 电脑 / 软路由 / 手机…），节点间用**自研通讯协议**互联 —— **UDP 广播自动发现 + HTTP 消息端点 + store-and-forward 离线邮箱 + gossip 邮箱同步**，零外部依赖，纯 Python 标准库。

设备上的 **AI Agent 之间可直接通信**：在线直投、离线存邮箱、上线自动收信；按能力委派任务、临时组队协作。不再是"遥控器-接收器"的单向操控，而是**设备与设备之间平等的 agent 协作网**。

## 二、核心亮点（v5.0）

| # | 亮点 | 说明 |
|---|------|------|
| 1 | **自研通讯协议** | UDP 发现 + HTTP 消息端点 + 离线邮箱 + gossip 同步，不依赖 Tailscale 等任何外部服务 |
| 2 | **41 个工具** | exec/文件/搜索/传输/Docker/systemd/网络诊断 + peer 互控 + 消息总线 + agent 协作 + mesh 拓扑 |
| 3 | **安全确权** | 设备码自动生成 + 邀请链接配对（TTL 过期 + 单次有效 + 显式信任确认） |
| 4 | **v5.0 消息加密** | 邮箱落盘密文（PBKDF2 派生密钥 + AES-CTR 结构流加密），物理拿到硬盘也读不到明文 |
| 5 | **v5.0 hello 防伪** | 已配对节点 UDP 发现包强制 HMAC 验签，冒充已知节点直接拒收（兼容 v4.x 混部） |
| 6 | **v5.0 Web Dashboard** | 浏览器实时看全网拓扑/在线状态/消息流，token 走 URL fragment 不外发，零 CDN 依赖 |
| 7 | **v5.0 跨平台安装** | `install.py` 支持 Windows（含防火墙 + 开机自启），Linux 保留 systemd 版 `install.sh` |
| 8 | **v5.0 测试套件** | 83 用例（含真实 UDP socket 验签），`pytest tests/ -q` 2.6s 全量回归，还抓出一个历史死锁 |
| 9 | **运行模式** | 连接模式（temporary/permanent）+ 控制模式（direct / assisted 辅助审查） |
| 10 | **Agent 协作协议** | 能力声明（caps）、任务委派（delegate）、临时群组（group_*）、消息总线（msg_*） |
| 11 | **TLS + 审计日志 + dryRun** | HTTPS 加密通讯、全工具调用审计留痕、edit 预览不落盘 |
| 12 | **零依赖** | 纯标准库、Python 3.6+（ThreadingHTTPServer 自动降级兼容）、单文件安装 |

**v5.0 变更**（相对 v4.6，安全大版本）：
- **邮箱落盘加密**：配对节点间消息自动以 `enc1$` 密文落盘，密钥由 token 经 PBKDF2(100k 轮) 派生；旧明文邮箱自动兼容读取
- **UDP hello 防伪签名**：已知节点发现包必须 HMAC 验签通过，防冒充；无签名旧节点照常兼容
- **Web Dashboard**：`/fleet/ui` 单文件页面，KPI 卡片 + mesh 拓扑 + 消息流，5s 自动刷新
- **Windows 安装器**：`scripts/install.py` 纯标准库跨平台，自动配防火墙与 Startup 自启
- **测试套件**：6 文件 61 用例全绿；**测试过程中发现并修复 gossip 同步死锁**（v4.1 引入的 `Lock` 重入问题，改 `RLock`）
- `demo.sh` 修复：演示节点补齐 `dashboard_html.py` 依赖拷贝

**v4.6 变更**（相对 v4.5）：
- 新增 `scripts/demo.sh` 一键演示：本机 30 秒拉起双节点，自动演示互发现、跨节点操控、消息投递、mesh 拓扑（端到端实测通过）
- 修复 `mesh` / `mesh_caps` 工具遍历 peers 的层级 bug（此前拓扑里对等节点显示异常）
- SKILL.md 新增「谁需要它」六类用户画像与兼容性清单，触发句式改造便于 AI 客户端自动匹配

🎬 **20 秒演示视频**：https://mcp-fleet-demo.obs.cn-north-4.myhuaweicloud.com/fleet-demo.mp4
（云上项目远程查询 + 出门在外管理家里设备两大场景，1080p）

## 三、评审五维自评

| 维度 | 亮点 | 证据 |
|---|---|---|
| **场景真实性** | 全设备管家 / 双 agent 协作 / 离线消息送达，三个真实场景；六类用户画像（NAS 用户/远程党/自托管/软路由/开发者/极客团队） | `references/vision-and-scenarios.md`、SKILL.md「谁需要它」 |
| **作品可用性** | 一条命令安装 + 设备码/邀请码配对 + 一键演示 + Windows 安装器 + Web Dashboard，端到端实测 | `bash scripts/demo.sh` 30 秒复现全部核心能力；41 工具实测、双节点互联 + 在线直投 + 密文落盘 + 审计日志验证 |
| **Skill 复用率** | 任意设备接入、任意 MCP 客户端可用（标准 MCP JSON-RPC，CodeArts / Claude Code / Cursor 均可）、节点间可协作 | 对等网络架构，天然可扩展；Linux/Windows/macOS 三平台安装路径；兼容性见 SKILL.md |
| **技术完成度** | 自研协议层（含加密与防伪）+ 安全确权 + 运行模式 + 41 工具 + TLS/审计/dryRun + 83 用例测试护栏 | 协议级创新，绝非配置型工具；v5.0 测试套件**真实抓出并修复历史死锁**（自发现自修复闭环，见 `references/安全审计报告.md`） |
| **内容传播力** | 华为云 × 小红书 × 开源社区三方通道，数据可查证 | `references/传播度证据.md`：GitHub 747 次克隆 / 281 独立访客，市场浏览量 23 次并列全场 114 作品第一；小红书笔记 + Skill 市场双通道 |

### 与华为云的结合

真实部署拓扑即「华为云 ECS 节点 ↔ 家里 NAS 节点」跨网段互联：ECS 上跑常驻节点（云服务器一条命令安装见 `references/云服务器-副端安装-一条命令.md`），家里的 NAS/软路由跑本地节点，两端通过 fleet 协议互通。本作品"不依赖云服务"的架构与华为云**互补而非竞争**——用户可以把算力型任务放在 ECS、把存储/家庭任务放在本地设备，AI agent 跨端调度。

## 四、提交包结构

```text
mcp-fleet/                               # 参赛提交包（AgentSkill 规范，30 个文件）
├── SKILL.md                             # Skill 入口（frontmatter + 适用场景 + 协议架构 + 工具清单 + 兼容性）
├── README.md                            # 本文件（参赛说明）
├── pytest.ini                           # 测试配置（faulthandler 超时保护）
├── scripts/
│   ├── mcp_agent.py                     # 节点 MCP 服务器（核心，41 个工具，TLS+审计+Dashboard）
│   ├── fleet_proto.py                   # 自研通讯协议层（UDP+gossip+离线邮箱+加密+签名）
│   ├── dashboard_html.py                # ★ v5.0 Web Dashboard 单文件页面
│   ├── install.py                       # ★ v5.0 跨平台安装器（Windows 一键装）
│   ├── demo.sh                          # ★ 一键演示：30 秒双节点互联全流程
│   ├── install.sh                       # 一键安装（Linux，自包含含 systemd）
│   ├── fleet.py                         # 统一 CLI（配对/巡检/指令/消息/mesh）
│   ├── fleet_chat.py / fleet_watch.py   # AI 间消息 / 保活守护
│   ├── fleet-directory.py               # 地址目录服务
│   ├── add_fleet_node.py / probe_fleet.py / call_node.py
│   ├── make-offline.py                  # 离线安装包（systemd 单元由 install.sh 运行时生成）
│   └── mcp_agent_config.example.json    # 配置模板
├── tests/                               # ★ v5.0 测试套件（83 用例，pytest 一键回归）
│   ├── conftest.py + test_crypto.py（12）
│   ├── test_mail_engine.py（24，含投递三分支）
│   ├── test_hello.py（10，含真实 UDP socket 验签）
│   ├── test_agent_tools.py（7）
│   └── test_agent_integration.py（8，双节点端到端）
└── references/
    ├── vision-and-scenarios.md          # ★ 场景故事 + 演进蓝图 + 生态结合点
    ├── 传播度证据.md                     # ★ 三层传播渠道数据（附复现命令）
    ├── 加固操作手册.md / 安全审计报告.md  # 安全审计含 v5.0 自发现自修复记录
    └── 云服务器-副端安装-一条命令.md
```

## 五、快速体验

```bash
# 0. 30 秒演示（可选）：本机自动拉起双节点，看懂它怎么工作
bash scripts/demo.sh

# 0.5 跑测试套件（可选）：83 个用例一键回归
python3 -m pytest tests/ -q

# 1. 在设备 A 安装（输出设备码 nas-a3f2 和 API）
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o install.sh
sudo bash install.sh --port 3100

# 1W. Windows 设备：管理员 PowerShell
python install.py --name office-pc --port 3100

# 2. 在设备 B 安装
sudo bash install.sh --port 3100

# 3. 在 B 上配对 A（或 A 生成邀请码发给 B 一键安装）
fleet.py pair nas-a3f2@http://192.168.1.10:3100/mcp

# 4. 浏览器打开 Dashboard 看全网状态
#    http://192.168.1.10:3100/fleet/ui#<节点token>
```

## 六、合规说明

- 提交包文件数：**30**（≤ 30 ✓）
- 提交包大小：约 **1 MB**（≤ 40MB ✓）
- 全部代码基于 MIT 许可开源，纯标准库零第三方依赖。
- **版本对齐**：代码 `version=5.0.3` ＝ git tag `v5.0.3` ＝ GitHub Release `v5.0.3` ＝ 本提交包；全版本演进见仓库 [CHANGELOG.md](https://github.com/guoxpeng/skill-mcp-fleet/blob/main/CHANGELOG.md)（v3.0 → v5.0.3 共 16 个里程碑，每个版本附更新说明）。