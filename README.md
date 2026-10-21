# MCP Fleet v5.1 · 让所有设备成为一支 AI 团队

> **2026 华为开发者大赛 · 小红书 Skill 创新赛** ｜ 通用赛道 · 开发工具
> 作者：guoxpeng ｜ 开源仓库：https://github.com/guoxpeng/skill-mcp-fleet ｜ 许可：MIT

![CI](https://github.com/guoxpeng/skill-mcp-fleet/actions/workflows/ci.yml/badge.svg)
![CodeQL](https://github.com/guoxpeng/skill-mcp-fleet/actions/workflows/codeql.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/guoxpeng/skill-mcp-fleet)
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.6%2B-blue)

---

## 一、核心叙事

**别人做的是 AI 和 AI 聊天，我做的是 AI 和 AI 协作干活。**

你家里有一台 NAS、一台软路由、一台闲置电脑、一台云主机、一部旧手机。它们各自很强，
但彼此孤立，只有你一个人能操作。MCP Fleet 给每台设备装一个「节点」，让它们组成一张
**AI 对等协作网**：

- 你在手机上说一句「NAS 硬盘还剩多少？顺便看看路由器为什么卡」，家里的设备自己动起来；
- 云主机上的 Agent 可以把编译任务委派给性能更强的办公室电脑，离线了也不丢任务；
- 设备上的 Agent 之间可以发消息、谈验收标准、互相复核结果；
- 整套系统**零第三方依赖、不依赖任何中心服务、一条命令安装**。

v5.1 把这件事从"能连"推进到**"敢连、好连、会自己想"**。

## 二、v5.1 五大升级

| # | 升级 | 一句话 | 证据 |
|---|------|--------|------|
| 1 | **标准 Agent Skill** | 严格遵循 Agent Skills 规范：frontmatter 仅 4 个标准字段、正文 <300 行、渐进披露到 `references/` | `SKILL.md`、`scripts/build_package.py` 生成合规提交包 |
| 2 | **默认安全** | Ed25519 身份 + 请求签名 + 防重放 + 认证加密信箱 + 执行策略 + 哈希链审计 + 供应链校验（六层） | `references/security-model.md`、`tests/test_core_51.py`、`SECURITY.md` |
| 3 | **全平台一条命令** | Android/Termux、飞牛/群晖/威联通/绿联、OpenWrt、Linux、macOS、Windows、Docker、云主机统一安装器，自动识别 init 与守护方式 | `scripts/fleet_install.py`、`references/platform-matrix.md`、`tests/test_installer_51.py` |
| 4 | **自愈连接** | 对端状态机 + 指数退避/抖动 + 熔断 + 消息去重/退避/死信 + 多路径（局域网/直连/Tailscale，中继配置预留）+ `fleet doctor` | `scripts/fleet_proto.py`、`tests/test_integration_51.py` |
| 5 | **Agent 自对话** | MCP `initialize.instructions` + 7 个 prompts + 6 个自对话工具 + 结构化委派信封 + 确定性编排器 | `scripts/fleet_agent.py`、`references/agent-playbook.md`、`references/self-dialogue-protocol.md` |

## 三、评审五维自评

| 维度 | 本作品的回答 |
|------|-------------|
| **场景真实性** | 六类真实用户：NAS 用户、远程党、自托管玩家、软路由玩家、开发者、极客团队。三个完整工作流：单机排查、跨设备长任务、多 Agent 分工。不是 demo 玩具。 |
| **作品可用性** | 一条命令安装/加入；`fleet doctor` 12 项自检；`scripts/demo.sh` 30 秒双节点演示；测试套件一键回归；卸载/升级/回滚都有路径。 |
| **Skill 复用率** | 标准 MCP 服务器，Claude Code / Cursor / CodeArts / 自研 Agent 均可接入；工具、平台、提示词、传输均为插件式扩展；Skill 本身可被任意 Agent 复用。 |
| **技术完成度** | 自研协议层 + 纯标准库密码学（RFC 7748/8032 向量测试）+ 多平台守护 + 自对话协议 + 哈希链审计。不是配置型工具，是完整系统。 |
| **内容传播力** | 华为云 × 小红书 × 开源社区三通道；历史数据：GitHub 747 次克隆 / 281 独立访客，技能市场浏览 23 次并列全场 114 作品第一（见 `references/传播度证据.md`）。 |

## 四、与华为云的结合

真实部署拓扑：**华为云 ECS 节点 ↔ 家里 NAS 节点**跨网段互联。ECS 跑常驻节点（一条命令安装），
家里 NAS/软路由跑本地节点，两端通过 fleet 协议互通；算力型任务放 ECS，存储/家庭任务放本地，
Agent 跨端调度。架构**"不依赖云服务"**，与华为云互补而非竞争——云是可选的中继/算力节点，
而不是单点依赖。

## 五、快速体验（3 分钟）

```bash
# 0. 30 秒双节点演示（本机自动拉起两个节点，自动清理）
bash scripts/demo.sh

# 0.5 全量测试
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q

# 1. 安装第一个节点（Linux/macOS/NAS/Termux/OpenWrt/WSL）
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o fleet-install.sh
sh fleet-install.sh --name nas --port 3100

# 2. 生成邀请，在第二台设备加入（自动校验签名 + SAS 人工核对）
fleet invite --ttl 3600
sh fleet-install.sh --join 'fleet://join/...'

# 3. 看全网状态
fleet nodes

# 4. 浏览器 Dashboard
#    http://<节点IP>:3100/fleet/ui#<token>
```

## 六、核心能力

### 6.1 设备能力（41 个设备工具 + 12 个 v5.1 工具）

执行/后台任务、文件读写编辑搜索/二进制传输、进程、Docker、systemd、网络诊断、
系统信息、节点互控（多跳）、消息总线、能力声明与任务委派、群组、临时/长期连接、
直接/辅助控制、只读审计模式。

### 6.2 安全（v5.1 六层）

- **身份**：Ed25519 节点身份 + 指纹；邀请签名 + 公钥交换 + 6 位 SAS 防中间人（X25519 原语已提供，CLI 尚未接入）。
- **请求**：签名 + 时间窗 + nonce 防重放；可选 TLS 自签。
- **数据**：邮箱认证加密 `enc2$`；旧 `enc1$` 兼容。
- **策略**：危险命令默认拒绝；路径 jail；超时/输出上限；`--read-only`；审批模式。
- **审计**：哈希链，可定位篡改行；安全事件计数。
- **供应链**：SHA256 校验、固定版本、离线包、CI/CodeQL。

### 6.3 Agent（自对话）

- `initialize.instructions`：节点操作手册（工具选择、四步法、安全红线）。
- `prompts/list`：7 个提示词（playbook/diagnose/plan/self-review/delegate/incident/onboard）。
- 自对话工具：`fleet_help` `fleet_think` `fleet_plan` `fleet_reflect` `fleet_review` `fleet_handoff`。
- 结构化信封：propose/accept/counter/progress/result/fail/review/handoff，带 thread、
  验收标准、超时；编排器确定性执行，可接 LLM。

### 6.4 可复现的验证证据

```bash
.venv/bin/python -m pytest tests/ -q        # 578 passed, 1 skipped
sh scripts/install.sh --dry-run --json --name demo   # 安装器零副作用预演
bash scripts/demo.sh                        # 本机双节点 30 秒演示
```

**真机实测（2026-10-22）**：Android 14 / arm64 手机（Termux 0.119 + Python 3.14）
经 `fleet_install.py --json` 安装为节点，Mac 节点与其双向配对后：

- Mac → 手机 `exec` 返回 `hello-from-phone` / `aarch64`；
- 手机 → Mac `exec` 返回 `hello-from-mac` / macOS 27.0.1；
- 邮箱消息在线投递并被 `msg_recv` 拉取；
- Ed25519 签名请求通过，重放与篡改均 401；
- `fleet_audit_verify` 哈希链 `ok=true`，SAS 校验码两端一致。

详情见 `references/platform-matrix.md` 第 15 节。

## 七、提交包与合规

```bash
python3 scripts/build_package.py
# → dist/mcp-fleet-v5.1.0.zip + SHA256SUMS + MANIFEST.json
```

- 提交包文件数 **≤ 30**、大小 **≤ 40 MB**，由脚本自动裁剪并以清单/SHA256 固化。
- 纯标准库、零第三方运行时依赖、MIT 许可、可离线部署。
- 版本：代码 `SERVER_VERSION=5.1.0` ＝ git tag `v5.1.0` ＝ GitHub Release。
  完整演进见 `CHANGELOG.md`。

## 八、仓库结构

```text
mcp-fleet/
├── SKILL.md                     # Agent Skill 入口（标准 frontmatter + 渐进披露）
├── README.md                    # 本文件（参赛说明）
├── AGENTS.md                    # 仓库内 AI 协作约定
├── SECURITY.md                  # 六层安全模型与披露流程
├── CHANGELOG.md                 # v3.0 → v5.1 演进
├── LICENSE                      # MIT
├── docs/V5.1-CONTRACT.md          # 模块契约（并行开发/二次开发）
├── scripts/
│   ├── mcp_agent.py             # 节点 MCP 服务器（41 工具）
│   ├── fleet_proto.py           # 协议层（发现/信箱/投递/gossip）
│   ├── fleet_core.py            # v5.1 安全核心（身份/签名/加密/策略/审计/指标）
│   ├── fleet_agent.py           # v5.1 Agent 智能层（提示词/自对话/编排）
│   ├── fleet_install.py         # v5.1 统一安装器（全平台）
│   ├── fleet.py                 # 统一 CLI
│   ├── install.sh               # POSIX 薄引导（SHA256 校验）
│   ├── dashboard_html.py        # Web Dashboard
│   ├── service-templates/       # systemd/openrc/procd/launchd/compose/Dockerfile
│   ├── demo.sh                  # 30 秒双节点演示
│   └── build_package.py         # 参赛包生成
├── tests/                       # v5.1 测试套件
└── references/                  # 安全模型/平台矩阵/Agent 手册/协议/FAQ/架构
```

## 九、诚实声明

- 纯 Python 实现的 Ed25519/X25519 未做严格常数时间优化，适合家庭/局域网/小团队，
  不用于高价值长期密钥；需要更高保证时叠加 TLS/Tailscale。
- 节点进程被完全控制后，攻击者拥有该进程权限；本项目的目标是缩小远程攻击面，
  而不是在宿主被攻陷后提供隔离。
- 不依赖任何外部服务是默认路径；跨 NAT/公网时建议 Tailscale 或带公网地址的邀请直连。
- 测试数量、覆盖率、平台实测结果以 CI 与 `references/` 中可复现命令为准。

## 十、致谢

感谢所有安全研究者、早期用户与评审意见。欢迎 Issue / PR / 安全披露。
如果这个项目对你有用，欢迎 Star ⭐。
