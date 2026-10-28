# 同类项目调研：我们不是独创，但组合是

> 调研日期：2026-10-28 ｜ 对应版本：v5.2.5
> 目的：回答两个问题——「这套东西别人做过吗」和「别人有什么我们没有」。

---

## 0. 一句话结论

**功能清单上 0 项独创。** 远程执行、PTY shell、端口转发、节点配对、审计链、mesh 拓扑，
每一条都能在别处找到成熟实现，而且不少比我们做得更久更全。

**但作为一个组合，没找到完全对应的项目。** 同时满足这三条的，一个都没有：

1. 零第三方依赖（单文件 Python，`python3` 在就能跑）；
2. 节点之间**对等**（不是「一个中心管一堆」）；
3. 能装进 OpenWrt / 128MB 内存的设备。

竞品全部至少违反其中两条。**不过「组合独特」不等于「有壁垒」**——护城河只能来自
「在别人装不上的设备上稳定跑够久」，也就是 `references/test-results.md` 里 iStoreOS
那一行还空着的实测。

---

## 1. 五类同类项目

### A. SSH 桥（最常见，也最容易和我们混淆）

| 项目 | 做法 | 工具 |
|---|---|---|
| `idletoaster/ssh-mcp-server` | MCP → 本机 Node 进程 → `ssh2` → 远端 | `remote-ssh` + 4 个省 token 的文件工具（`ssh-edit-block` / `ssh-read-lines` / `ssh-search-code` / `ssh-write-chunk`） |
| `classfang/ssh-mcp-server` | 同上，npm 一行装 | `execute-command` / `upload` / `download` |
| `Ron-dn/mcp-ssh-server` | 同上，号称 13 个工具 | 命令执行 + 文件操作 + 网络设备管理 |

共同点：**每台机器都要在客户端配一次，凭据是本地私钥文件。**
`ssh-mcp-server` 明确只有私钥认证，**没有只读模式、没有命令白名单、没有审计**——
README 里建议用户自己靠 `~/.ssh/config` 和系统日志兜。

它们的价值是把 ssh 变成工具；代价是多一跳、多一套凭据、**没有集群概念**。
我们和它们的区别不是「工具更多」，而是「对端本身就是 MCP server，不需要中间那层桥」。

### B. 网关 + 设备节点（和我们结构最像）

**OpenClaw**（网关 + nodes）是这一类的标杆，值得逐条看：

- 节点以 `role: "node"` 连 Gateway 的 WebSocket，通过 `node.invoke` 暴露命令；
- **13 个命名空间**：`canvas.*`（WebView 控制/截图）、`camera.*`、`screen.record`、
  `location.*`、`sms.*`、`device.*`、`notifications.*`、`photos.*`、`contacts.*`、
  `calendar.*`、`callLog.*`、`motion.*`、`system.*`（`system.run` / `which` / `notify` /
  `execApprovals`）；
- 配对是**人工批准**：`devices list` → `devices approve <requestId>`；
- exec 有**按节点的 allowlist**：`approvals allowlist add --node X /usr/bin/uname`，
  记录存在节点上的 `~/.openclaw/exec-approvals.json`；
- 手机端 `canvas.*` / `camera.*` **要求 App 在前台**，后台直接返回
  `NODE_BACKGROUND_UNAVAILABLE`；相机片段和录屏都限 60 秒（避免 base64 载荷过大）；
- 位置默认关闭，要设备上手动开；短信要接受 SMS 权限提示。

**netclaw**（.NET / Akka.NET，daemon + CLI）：

- default-deny：没显式配置就不执行工具、不连频道、不接受远程连接；
- **audiences**（按频道/用户划权限）+ approval gates + tool policies；
- 密钥加密存盘（`secrets set`，不让手改文件）；
- 远程设备通过 **Tailscale 或 Cloudflare Tunnel** 配对，exposure modes 三选一
  （local / tailscale / cloudflare）；
- 接 Slack / Discord；有 reminders、webhooks、OpenTelemetry health checks；
- 缺口：macOS 上守护进程自启还没做（issue #1015）。

**PocketMCP**：把 Android 手机变成 MCP server，LAN 或 Tailscale 直连。

和我们像的地方：都是「设备上装个东西，暴露一组工具」。
**不像的地方：它们全是星型**——一个 gateway 管一堆节点，节点之间不互通。
我们是 `peer_call` 多跳 + `mesh` + 群组 + `delegate`，节点平权。

### C. 访问网关（「替代 ssh」的真正对手，不是 MCP 圈）

| 项目 | 关键能力 |
|---|---|
| **Tailscale SSH** | 无密钥、ACL 决定谁能连谁、check mode 二次确认、2026 年补上 session recording |
| **Teleport** | 短时证书 + mTLS、RBAC、**会话录制与回放**、不可变审计日志、NAT 后打隧道、统一 RBAC |
| **Octelium** | 自托管零信任，MCP gateway + A2A，OAuth2 client credentials，L7 policy-as-code |

这一类才是我们验收标准的对标物。它们的共同底牌是**合规证据**：谁、什么时候、
对哪台机器、敲了什么、屏幕上出现了什么，全都有录像。
我们的审计链是**命令级**的，没有字节流——这是最硬的一个缺口。

### D. 编排 / 元编排

- **Omnigent**（Databricks 出品，5k+ star）：不造新 agent，把 Claude Code / Codex /
  Cursor / Pi 塞进同一个策略与沙箱层，K8s 式「只做编排、策略、可观测」。
- **Ansible / pdsh / Salt**：批量执行的祖宗——inventory + 并行 + 幂等 + facts 采集。

我们的 `delegate` / `fleet_plan` / 结构化信封是**任务级**编排（有验收标准和超时），
但**没有批量并行执行**：`health` 能并行扫全部节点，`exec` 只能一台。

### E. 沙箱运行时

- **E2B / Daytona / microsandbox**：毫秒级启动、环境模板、自动端口暴露、
  computer-use 虚拟桌面（Linux/Windows/macOS）。
- 它们的假设是「代码不可信，必须隔离」；我们的假设是「设备是你自己的，隔离靠策略」。
  两条路不同，**不建议硬碰**。

---

## 2. 能力对照矩阵

| 维度 | fleet | ssh-mcp | OpenClaw | netclaw | Tailscale SSH | Teleport | Ansible |
|---|---|---|---|---|---|---|---|
| 部署形态 | 单文件 py，零依赖 | Node + ssh2 | 网关 + App/无头节点 | .NET daemon | 系统守护进程 | 单二进制 | 无 agent（走 SSH） |
| 能装进 OpenWrt | ✅ 设计目标 | ❌ 要 node | ❌ | ❌ | ⚠️ 有包但重 | ❌ | ❌ |
| 多机拓扑 | **对等 mesh** | 星型（客户端配 N 次） | 星型 | 星型 | 网状（三层） | 星型 | 拉模式 |
| 交互式 shell | ✅ PTY | ❌ | 部分 | ❌ | ✅ | ✅ | ❌ |
| 端口转发 | ✅ `-L` / `-R` | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| 文件传输 | ✅ `cp`（整包） | ✅ upload/download | 有限 | ❌ | ✅ scp/sftp | ✅ | ✅ |
| **会话录制回放** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| 命令级审计 | ✅ 哈希链 | ❌ | ⚠️ 审批记录 | ✅ | ✅ | ✅ | ⚠️ |
| 只读模式 | ✅ `--read-only` | ❌ | ⚠️ allowlist | ✅ policy | ACL | RBAC | `--check` |
| 危险命令拦截 | ✅ 正则 + 路径 jail | ❌ | ✅ allowlist | ✅ | ❌ | ✅ | ❌ |
| 审批工作流 | ⚠️ 规则命中即 ask | ❌ | ✅ 按节点 | ✅ gates | ✅ check mode | ✅ access request | ❌ |
| **设备原生能力** | ❌ 只有 shell | ❌ | ✅ 13 命名空间 | ❌ | ❌ | ⚠️ desktop | ❌ |
| **批量并行** | ⚠️ 仅 `health` | ❌ | ⚠️ 逐节点 | ❌ | ❌ | ⚠️ | ✅ |
| **单 endpoint 聚合** | ❌ 每节点一条 URL | ❌ | ✅ 一个 gateway | ✅ 一个 daemon | n/a | ✅ | n/a |
| 自升级 / 回滚 | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | n/a |
| 自修复 | ✅ `repair --auto` | ❌ | ❌ | ⚠️ `doctor --fix` | ❌ | ❌ | ❌ |
| 节点间消息 / 委派 | ✅ | ❌ | ⚠️ 会话 | ⚠️ channels | ❌ | ❌ | ❌ |
| 多用户 RBAC | ❌ 单 token | ❌ | ⚠️ audiences | ✅ audiences | ✅ ACL | ✅ | ⚠️ |
| 告警 / 可观测 | ⚠️ Prometheus 文本 | ❌ | ⚠️ | ✅ OTel | ✅ | ✅ | ⚠️ |

（✅ 有 ／ ⚠️ 部分 ／ ❌ 无）

---

## 3. 我们真正的差异（能对外辩护的 4 条）

1. **零依赖单文件**。有 `python3` 就能跑。竞品最低门槛是 node、.NET 或 Go 二进制——
   这决定了「能不能装进 128MB 的软路由」，而不是「好不好用」。
2. **对等 mesh**。`peer_call` 多跳、`mesh` 拓扑、群组、`delegate`。竞品全是中心化。
   对等的好处是断网也能局部自治；代价是没有全局一致视图（这个代价我们还没正面处理）。
3. **MCP 原生**。不是「给 MCP 客户端套一层 SSH」，对端本身就是 MCP server。
   少一跳、少一套凭据、工具清单是活的。
4. **自修复 + 自升级**。`repair --auto` 会自己重启、改权限、清 pid；`up` 失败自动回滚、
   坏版本记进 `up-state.json` 下次跳过。竞品里只有 netclaw 的 `doctor --fix` 沾一点。

---

## 4. 缺口（按「是否挡住验收标准」排序）

### P0 —— 直接影响「替代 ssh」和「集群可用」

1. **会话录制 / 回放**。`fleet shell` 的字节流没有留证，审计链只到命令级。
   Teleport 和 Tailscale SSH 都有。做法很便宜：`t_shell_read` 旁路写 asciinema cast
   （JSONL，`[time, "o", "bytes"]`），`fleet shell --record` 默认开，`fleet replay <id>` 回放。
   我们本来就在 base64 传输出流，只是没落盘。
2. **批量并行执行**。`fleet run --all` / `--group web "uptime"`，复用 `health` 的
   ThreadPool 骨架 + 结果聚合表（按退出码分组）。这是「管一堆机器」的第一反应，
   现在只能写 `for n in $(fleet targets); do fleet exec $n ...; done`。
3. **单 endpoint 聚合（gateway 模式）**。一个 MCP URL 暴露全集群，工具名 `node__tool`。
   否则接 5 台设备要在客户端配 5 次——这正是 Docker MCP Gateway / `mcp-gateway`
   已经解决的事，我们没做。

### P1 —— 影响「AI 借用设备环境」和团队场景

4. **设备原生能力（手机节点）**。通知、剪贴板、电量、位置、相机、前台 App。
   OpenClaw 的 13 个命名空间证明这才是「AI 借设备环境」的关键。
   我们目前只有 shell，手机节点等于一台小 Linux。
5. **增量同步**。`cp` 是整包 tar，没有 rsync 语义（`--exclude` / 只传 diff / `--delete`）。
   改一个文件传整个目录，在软路由上很贵。
6. **RBAC / 短时凭据**。单 token 对一个人够，对团队不够；Teleport 的短时证书是标准答案。
7. **告警外发**。`health` 能判 UP/DOWN，但只能人来看。要 webhook / Slack / Telegram。

### P2 —— 可能不该做

8. 幂等配置管理（Ansible 的地盘，和「轻」的定位冲突）
9. microVM 沙箱（E2B/Daytona 的地盘，成本高、假设不同）
10. 技能 / 插件市场（OpenClaw、netclaw 在做，我们没有 LLM，做不了闭环）

---

## 5. 可以立刻抄的 8 个细节

1. **文件指纹绑定**（OpenClaw）：直接执行文件时绑定具体文件操作数，执行前文件变了就拒绝。
   防 TOCTOU——我们的 `t_exec` 现在是「拿到字符串就跑」。
2. **环境变量白名单**（OpenClaw）：它剥离 `DYLD_*` / `LD_*` / `NODE_OPTIONS` / `PYTHON*` /
   `PERL*` / `RUBYOPT` / `SHELLOPTS` / `PS4`，且忽略 `PATH` 覆盖，只放行 `TERM` / `LANG` /
   `LC_*` / `COLORTERM` / `NO_COLOR`。我们**干脆不接受 env 参数**，天然免疫，但也就没法注入——
   想给命令带环境时只能 `FOO=1 cmd` 写进命令行。抄它的白名单键是个折中。
3. **明确的错误码**（OpenClaw）：`NODE_BACKGROUND_UNAVAILABLE`、`*_PERMISSION_REQUIRED`。
   我们现在失败一律是文本，机器不好分支判断。
4. **平台能力表**（OpenClaw）：未知平台 → 保守默认（连 `system.run` 都不给）。
   我们的 `caps` 可以升级成这个语义：`caps` 自报之后，主端据此决定能调什么。
5. **audiences**（netclaw）：按「谁在什么上下文」划权限，而不是按 token。
   我们可以按 agent 身份划（同一个节点，不同 agent 给不同工具面）。
6. **exposure modes**（netclaw）：local / tailscale / cloudflare 三选一，一个开关。
   我们已经有 `tunnel` + `tailnet`，可以合成 `fleet expose --mode tailscale`。
7. **会话录制格式**（Teleport 生态）：直接用 asciinema cast，`asciinema play` 就能放，
   不用自己写播放器。
8. **端口自动发现**（E2B/Daytona）：列出副端在监听的端口 → 一键给 `forward` 命令。
   我们现在要人先 `netstat` 再手写 `-L 8080:127.0.0.1:3100`。

---

## 6. 定位建议

对外一句话：

> **给 AI 用的、能装在任何 Linux 设备（含 OpenWrt）上的对等 MCP 节点网络。**

不要自称「又一个 SSH MCP」——那是 A 类，我们比它们重（有 shell/转发/审计）；
也不要自称「又一个 AI 运维 agent」——那是 B 类，我们比它们轻（没有 LLM、没有频道、没有技能市场）。

按「中心化 ↔ 对等」和「只要 shell ↔ 要设备原生能力」两轴画一下：

```
                 要设备原生能力
                        ▲
        OpenClaw ●      │
          netclaw ●     │
                        │
  中心化 ◀──────────────┼──────────────▶ 对等
                        │            ● fleet
        Teleport ●      │
   Tailscale SSH ●      │        ● Ansible（拉模式）
      ssh-mcp ●         │
                        ▼
                  只要 shell
```

我们独占的格子是**右下角**：对等 + 只要 shell + 极轻。
这个格子小，但里面没有别人——问题只在于这个格子够不够大。
答案取决于「有多少设备装不上 node/.NET」：软路由、NAS、老 ARM 盒子、嵌入式 Linux。
这是真实存在的一类设备，而目前没有一个 MCP 方案认真对待它们。

---

## 7. 参考

- OpenClaw nodes 文档：<https://www.studynil.com/openclaw/nodes/index.html>
- OpenClaw 仓库：<https://github.com/openclaw/openclaw>
- netclaw：<https://github.com/netclaw-dev/netclaw>
- ssh-mcp-server（idletoaster）：<https://github.com/idletoaster/ssh-mcp-server>
- ssh-mcp-server（classfang）：<https://github.com/classfang/ssh-mcp-server>
- Teleport：<https://github.com/gravitational/teleport>
- Tailscale：<https://github.com/tailscale/tailscale>
- Docker MCP Gateway：<https://www.docker.com/blog/docker-mcp-gateway-secure-infrastructure-for-agentic-ai/>
- mcp-gateway：<https://github.com/aarora79/mcp-gateway>
- Omnigent（元编排）：<https://developer.aliyun.com/article/1744155>
- E2B / Daytona：<https://daytona.io/>、<https://github.com/restyler/awesome-sandbox>
- PocketMCP：<https://www.mcpworld.com/en/detail/4b92e3199560e6332db5f86425caaf0a>
