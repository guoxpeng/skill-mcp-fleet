# MCP Fleet v5.2.9 · 让所有设备成为一支 AI 团队

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

v5.2 把这件事从"能连"推进到**"敢连、好连、会自己想、坏了能自修"**；
这一版用 `fleet shell` 给了终端，用 `fleet forward` 给了端口转发——
**公网设备上 fleet 至此可以完全替代 ssh，内网靠 Tailscale 达到同样手感**。

## 二、v5.2 六大升级

| # | 升级 | 一句话 | 证据 |
|---|------|--------|------|
| 1 | **标准 Agent Skill** | 严格遵循 Agent Skills 规范：frontmatter 仅 4 个标准字段 + 1 个市场版本字段（version）、正文 <300 行、渐进披露到 `references/` | `SKILL.md`、`scripts/build_package.py` 生成合规提交包 |
| 2 | **默认安全** | Ed25519 身份 + 请求签名 + 防重放 + 认证加密信箱 + 执行策略 + 哈希链审计 + 供应链校验（六层） | `references/security-model.md`、`tests/test_core_51.py`、`SECURITY.md` |
| 3 | **全平台一条命令** | Android/Termux、飞牛/群晖/威联通/绿联、OpenWrt、Linux、macOS、Windows、Docker、云主机统一安装器，自动识别 init 与守护方式 | `scripts/fleet_install.py`、`references/platform-matrix.md`、`tests/test_installer_51.py` |
| 4 | **自愈连接** | 对端状态机 + 指数退避/抖动 + 熔断 + 消息去重/退避/死信 + 多路径（局域网/直连/Tailscale，中继配置预留）+ `fleet doctor` | `scripts/fleet_proto.py`、`tests/test_integration_51.py` |
| 5 | **Agent 自对话** | MCP `initialize.instructions` + 7 个 prompts + 6 个自对话工具 + 结构化委派信封 + 确定性编排器 | `scripts/fleet_agent.py`、`references/agent-playbook.md`、`references/self-dialogue-protocol.md` |
| 6 | **自检与自修复** | `fleet_selfcheck` 只读诊断；`fleet_repair_loop` 有界自愈（重启/权限/pid，失败即停）；审计可复核 | `scripts/mcp_agent.py`、`scripts/fleet.py`、`references/self-repair.md` |

## 三、评审五维自评

| 维度 | 本作品的回答 |
|------|-------------|
| **场景真实性** | 六类真实用户：NAS 用户、远程党、自托管玩家、软路由玩家、开发者、极客团队。三个完整工作流：单机排查、跨设备长任务、多 Agent 分工。不是 demo 玩具。 |
| **作品可用性** | 一条命令安装/加入；`fleet doctor` 12 项自检；`scripts/demo.sh` 30 秒双节点演示；测试套件一键回归；卸载/升级/回滚都有路径。 |
| **Skill 复用率** | 标准 MCP 服务器，Claude Code / Cursor / CodeArts / 自研 Agent 均可接入；工具、平台、提示词、传输均为插件式扩展；Skill 本身可被任意 Agent 复用。 |
| **技术完成度** | 自研协议层 + 纯标准库密码学（RFC 7748/8032 向量测试）+ 多平台守护 + 自对话协议 + 哈希链审计。不是配置型工具，是完整系统。 |
| **内容传播力** | 华为云 × 小红书 × 开源社区三通道。GitHub 真实克隆峰值单日 248 次（14 天 747 次 / 281 独立访客，快照 10-17）；技能市场上架并进入社区可见列表；小红书笔记 + 市场双通道分发（见 `references/传播度证据.md`）。 |

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

### 6.1 设备能力（41 个设备工具 + 15 个 v5.2 工具）

执行/后台任务、文件读写编辑搜索/二进制传输、进程、Docker、systemd、网络诊断、
系统信息、节点互控（多跳）、消息总线、能力声明与任务委派、群组、临时/长期连接、
直接/辅助控制、只读审计模式。

### 6.2 安全（v5.2 六层）

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

### 6.4 v5.2 菜单、提示词与自修复

- 默认生成访问 token（`--no-auth` 才关闭）、全局 `fleet` 命令；
- `fleet update / --rollback` 原子升级回滚、`fleet identity export/import`；
- `fleet ask "..." [--run]` 自然语言入口、`fleet invite --qr` 扫码配对；
- Dashboard 2.0：设备指纹/移除、投递队列、Agent 任务时间线、安全状态；
- `fleet` 无参数菜单重做：状态 + 命令 + 可直接复制的 AI 提示词；`fleet prompt [主题]`；
- `fleet selfcheck` / `fleet repair --auto`：只读诊断 + 有界自修复（重启/权限/pid）；
- 普通用户文档：[5 分钟上手](references/quickstart.md)、[一页排障](references/troubleshooting.md)、
  [自修复](references/self-repair.md)、[真机测试结果](references/test-results.md)。

### 6.5 替代 ssh：交互式 shell 与自升级

目标很具体：**普通公网设备上，fleet 能替代 ssh；内网配合 Tailscale 达到同样的手感。**

- **`fleet shell <节点>`**：远端 `pty.fork()` 起真终端登录 shell，本地 raw 模式直通。
  `tty` 返回 `/dev/ttysNNN`，vim / top / less 正常；`SIGWINCH` 自动同步窗口大小。
  传输仍是请求/响应，用长轮询（`wait` 最长 5s）避免空转，输出走 base64 防截断。
- **`fleet up`**：检查远端版本，有新版才升级。git 仓库走 `fetch + merge --ff-only`，
  非 git 安装走源码包（GitHub + 镜像回退）。**只升代码不碰数据**（白名单），
  升级前备份到 `~/.mcp-fleet/up-backups/`，升级后 `--version` 自检，失败自动回滚；
  远端版本坏了会记进 `up-state.json`，下次自动跳过。
- **`fleet ping` / `cp` / `scan` / `tailnet` / `start|stop|restart`**：探活、双向搬文件、
  扫同网段、地址漂移自愈、节点启停。
- **macOS**：launchd plist 显式写 PATH（否则 Homebrew 的命令在 `exec` 里全部找不到）、
  `stop` 走 `bootout + disable`（否则 KeepAlive 十秒内把进程拉回来）、
  `start` 走 `bootstrap + kickstart`（否则不受 launchd 管、重启不自启）、
  `_lan_ip` 按 en0 取址（否则开 Tailscale 出口节点后广播的是隧道地址）。

### 6.6 连接管理（v5.2.6）

ssh 的连接管理是三件散装工具：`who`、`ss -tp`、`pkill`，而且**断线就全丢**。
这一版把它们收成一条线，并补上 ssh 做不到的那条。

```bash
fleet sessions                 # 一张表：shell / 隧道 / 后台任务，带时长、空闲、字节、是否在录
fleet shell                    # 进去之后按 Ctrl-] 是「挂起」，不是断开
fleet attach sh-1a2b3c4d       # 接管回来，顺带重放最近 512KB 屏幕
fleet kill sh-1a2b3c4d         # 按前缀自动判断类型，不用记是 shell 还是隧道
fleet recordings               # 有哪些录像
fleet replay sh-1a2b3c4d       # 回放（asciinema v2 cast）
```

- **看得见**：以前要敲 `shell_list` + `tunnel_list` + `exec_jobs` 三次再自己拼。
  每条会话带归属（`谁@哪台机器`）与标签，多端连同一台机器不用靠时间猜。
- **接得回**：Ctrl-] 挂起后远端 shell 继续跑、录制继续写；`attach` 回来时
  屏幕恢复成离开时的样子。空闲回收 30 分钟 → 2 小时（`shell_idle_sec` 可配）。
- **查得到**：默认开录，格式是 asciinema v2 cast，`asciinema play` 直接能放。
  **只录输出不录键盘**——录制的目的是「屏幕上出现了什么」，而键盘里可能有密码。
  单会话 64MB 封顶。
- 三个真会翻车的细节：多字节字符跨 `os.read` 边界（增量解码器）、
  默认 8KB 缓冲丢最后几十秒（改行缓冲）、录制目录不可写时不能拖垮会话本身。

### 6.7 批量执行与分组（v5.2.8）

ssh 还有一件事做不了：**一次管一堆机器**。以前只能自己写循环——

```bash
for n in $(fleet targets); do fleet exec $n "uptime"; done
```

串行、出错不停、输出糊成一片、也看不出哪台失败。v5.2.8 把 pdsh / clush /
ansible ad-hoc 干的那件事补上：

```bash
fleet run --all "uptime"                      # 全部副端，并发 8
fleet run --group web "systemctl restart nginx"
fleet run nas,pi "df -h"                      # 点名
fleet run --all --short "apt-get -s upgrade"  # 每台只留一行，看哪台不对
fleet run --all --quiet "reboot"              # 只要结论，脚本里判退出码
```

- **不串行**：`ThreadPoolExecutor` + 每台一个独立客户端（`http.client` 的连接不是
  线程安全的，共用会 `CannotSendRequest`）。默认并发 8，`--jobs` 可调。
- **一台离线不拖住整批**：建连用短超时、不重试（和 `health` 同一条理由），
  离线那台单独记一行错误，其余照常出结果。
- **输出顺序稳定**：结果按节点名排序打印，同一批跑两次排版一致，能直接 `diff`。
  进度（`3/5 完成`）只写 stderr 且仅在终端里显示，重定向时不会掺进结果。
- **退出码能当判据**：全成功 `0` / 有失败 `1` / 没匹配到任何节点 `2`。
- **开关前后都能写**：`fleet run nas --short "df -h"` 里的 `--short` 不会被当成
  命令发给设备（argparse 的 `REMAINDER` 会吞掉它，这里自己捞回来）。
- **`--all "uptime"` 不歧义**：唯一的那个位置参数是命令，不是节点名。

分组信息存在登记表每个节点的 `groups` 字段里，跟着节点走——`fleet remove`
删节点时不会留下幽灵成员：

```bash
fleet group                                   # 有哪些组、各几个成员
fleet group web                               # 这个组里是谁
fleet group add web istoreos nas              # 加成员
fleet group rm web nas
fleet group del web
fleet add --name pi --ip 192.168.1.9 --group web   # 登记时就归组
```

重新 `add` 同一台（换地址、换令牌）不会把它踢出原来的组；`groups` 写成
`"web,db"` 这种走形格式也认。

### 6.8 端口转发（`ssh -L / -R / -D` 的等价物）

5.3 给了终端，但 ssh 还有一半用处是端口转发：远端内网里只有那台机器能碰的数据库、
面板、私有 API，全靠它接出来。

```bash
fleet forward -L 8080:db.internal:3306 nas   # 本地 8080 → 副端视角的 db:3306
fleet forward -R 9000:127.0.0.1:3000 nas     # 副端 9000 → 本地 3000
fleet forward -D 1080 nas                    # 本地 SOCKS5，从 nas 出网（v5.2.9）
fleet forward --list nas                      # 看副端当前隧道
```

`-D` 是 ssh 的最后一项传输能力，也是「AI 借用设备网络环境」最直接的形式：
本地起一个 SOCKS5，目标由客户端每次连接时给，**域名原样交给副端解析**——
DNS 在哪边解析，决定了你看到的是哪边的内网。

```bash
fleet forward -D 1080 istoreos &
curl --socks5-hostname 127.0.0.1:1080 http://127.0.0.1:80/   # 设备本机的 LuCI
```

- 副端新增 7 个工具（`tunnel_open/listen/accept/read/write/close/list`），总数 62 → **69**；
- `-L` / `-R` / `-D` 底层是同一条通道，区别只在「谁决定去哪」：`-L` 命令行写死，
  `-D` 客户端每次说。所以 `-D` 不需要副端开任何端口，也不用新协议；
- 一条命令可叠加多个 `-L` / `-R` / `-D`；默认只绑回环，要暴露到局域网需显式 `--bind 0.0.0.0`；
- 只做 SOCKS5 的 CONNECT（TCP）——`ssh -D` 的语义也是这个，UDP ASSOCIATE 不实现；
  三种地址类型（IPv4 / 域名 / IPv6）都认，失败时回明确的 SOCKS 码（4/5/7/8）
  而不是让客户端干等；
- 实现要点是**不丢字节**：缓冲区满时读线程挂起等主端取（背压），而不是像终端那样
  丢弃滚屏历史——TCP 丢一个字节，下载的文件就是坏的；
- 验证方式：1MB / 5MB 随机二进制 + SHA256 比对，上行/下行/`-L`/`-R` 四个方向全覆盖。
  本机回环实测 `-L` 5MB ≈ 0.5s、`-R` 5MB ≈ 3s；`-D` 经 iStoreOS 取 LuCI 首页
  664 字节 / 4.2s（Tailscale 链路）。
- 顺带补上「长时间在线」的证据：`fleet health` 现在显示副端自报的**进程在线时长**
  和主端**已观测多久**，并把 `first_seen / last_ok / up_count` 记在
  `~/.mcp-fleet/seen.json`（掉线不清零）——只看当次 UP 看不出稳定性。

### 6.9 可复现的验证证据

```bash
.venv/bin/python -m pytest tests/ -q        # 815 passed, 1 skipped
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

**`fleet cp` 真机推拉（2026-10-27）**：起真 agent 进程（真 socket、真文件系统）后，
推/拉 0 / 1 / 1000 / 4096 / 64K / 600K / 3MB 七档随机二进制 + 一个非 ASCII 路径，
双向 SHA256 全一致、远端无残留分块。600KB 那档抓到过一个致命 bug：执行策略把
`upload` 的 base64 正文当成命令行去匹配，`ufw`（3 个字符）在几十万字符里几乎必然
命中，导致**大文件上传基本都会被判成「防火墙变更」**。修复后策略改为「命令字段」
与「数据载荷」分流，详见 `CHANGELOG.md` 5.2.3「修复」。

## 七、提交包与合规

```bash
python3 scripts/build_package.py
# → dist/mcp-fleet-v5.2.9.zip + SHA256SUMS + MANIFEST.json
```

- 提交包文件数 **≤ 30**、大小 **≤ 40 MB**，由脚本自动裁剪并以清单/SHA256 固化。
- 纯标准库、零第三方运行时依赖、MIT 许可、可离线部署。
- 版本：代码 `SERVER_VERSION=5.2.9` ＝ git tag `v5.2.9` ＝ GitHub Release。
  完整演进见 `CHANGELOG.md`。
- **版本号一律三段式 `X.Y.Z`**：默认只动补丁位（修 bug、稳定性、性能），
  新增能力才动次版本位，破坏性变更才动主版本位。构建脚本会做三段式校验与
  同源对账（6 个脚本/配置 + `SKILL.md` frontmatter，共 7 个落点），
  任何一处漂移直接判失败。
  规则详见 `AGENTS.md` 4.1 节。

## 八、仓库结构

```text
mcp-fleet/
├── SKILL.md                     # Agent Skill 入口（标准 frontmatter + 渐进披露）
├── README.md                    # 本文件（参赛说明）
├── AGENTS.md                    # 仓库内 AI 协作约定
├── SECURITY.md                  # 六层安全模型与披露流程
├── CHANGELOG.md                 # v3.0 → v5.2 演进
├── LICENSE                      # MIT
├── docs/V5.2-CONTRACT.md          # 模块契约（并行开发/二次开发）
├── scripts/
│   ├── mcp_agent.py             # 节点 MCP 服务器（41 工具）
│   ├── fleet_proto.py           # 协议层（发现/信箱/投递/gossip）
│   ├── fleet_core.py            # v5.2 安全核心（身份/签名/加密/策略/审计/指标）
│   ├── fleet_agent.py           # v5.2 Agent 智能层（提示词/自对话/编排）
│   ├── fleet_install.py         # v5.2 统一安装器（全平台）
│   ├── fleet.py                 # 统一 CLI
│   ├── install.sh               # POSIX 薄引导（SHA256 校验）
│   ├── dashboard_html.py        # Web Dashboard
│   ├── service-templates/       # systemd/openrc/procd/launchd/compose/Dockerfile
│   ├── demo.sh                  # 30 秒双节点演示
│   └── build_package.py         # 参赛包生成
├── tests/                       # v5.2 测试套件
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
