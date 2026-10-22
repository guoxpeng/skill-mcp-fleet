---
name: mcp-fleet
version: 5.2.0
description: 把手机、NAS、云服务器、软路由、电脑与 Docker 容器组成一张 AI 对等协作网：一条命令安装节点、设备码/邀请链接安全配对，任意 MCP Agent 即可跨设备执行命令、读写与传输文件、查看容器日志、离线留言、按能力委派任务，并自带只读自检与有界自修复。默认安全（Ed25519 身份、请求签名防重放、认证加密信箱、哈希链审计），零第三方依赖、不依赖任何中心服务。当用户需要管理 NAS、远程控制家中电脑、多设备互联、让 AI 操作设备、远程执行命令、软路由排查、云主机纳管、离线留言、设备监控、MCP 组网或多 Agent 协作时使用；即使用户未提 "fleet"，只要涉及「用聊天指挥另一台设备干活」也应主动使用。
license: MIT
compatibility: Python 3.6+，纯标准库；Linux / macOS / Windows / Android(Termux) / OpenWrt。可选 openssl（自动生成 TLS 证书）与 tailscale（跨网段加速）。
---

# MCP Fleet v5.2 · 让所有设备成为一支 AI 团队

MCP Fleet 把每台设备变成一个**节点**，节点之间对等互联：自动发现、在线直投、离线信箱、
按能力委派。设备上的 AI Agent 可以直接对话与协作，而不是"人拿遥控器"。

**v5.2 的四件事**：

1. **一条命令接入**——安装器自动识别 Android/NAS/软路由/云主机/PC，生成服务、开机自启、健康检查，并打印邀请链接。
2. **默认安全**——每节点 Ed25519 身份、请求签名与防重放、认证加密信箱、执行策略、哈希链审计、供应链校验。
3. **Agent 自带说明书**——MCP `initialize.instructions` + 7 个 prompts + 6 个自对话工具，让 agent 会观察、计划、执行、反思。
4. **自检与自修复**——`fleet_selfcheck` 返回可机读 findings；`fleet_repair_loop` 在安全白名单内自动重启/修权限/清 pid，失败即停并报告。

---

## 一、90 秒建立第一张网

### 设备 A：安装节点

```bash
# 推荐：下载后校验再执行（不要盲目 curl | bash）
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o fleet-install.sh
sh fleet-install.sh --name nas --port 3100
```

安装完成会打印：设备码、节点身份指纹、邀请链接、Dashboard 地址。把它保存到安全位置。

### 设备 B：用邀请链接加入

在 A 上生成一次性邀请（默认 1 小时、单次有效）：

```bash
fleet invite --ttl 3600          # 输出 fleet://join/... 与 6 位 SAS 校验码
```

在 B 上执行：

```bash
sh fleet-install.sh --join 'fleet://join/...'
# 或节点已安装时：
fleet join 'fleet://join/...'
```

安装器会校验邀请签名与有效期，显示 **SAS 校验码**；与 A 屏幕上的 6 位数字一致再确认，
防止中间人。配对后双方互信，B 的 Agent 即可被 A 调度。

### 验证

```bash
fleet status                 # 本机身份、服务状态、端口、证书指纹
fleet nodes                  # 全网节点、在线状态、RTT、能力
fleet doctor                 # 12 项自检：时钟偏差/端口/防火墙/磁盘/权限/对端可达/TLS
fleet send nas-a3f2 "hello"  # 给节点上的 agent 发消息
fleet ask "帮我看下磁盘为什么满了" --run   # 自然语言入口（只读诊断）
fleet selfcheck               # 只读自检 + 修复计划
fleet repair --auto           # 有界自修复（最多 2 次，失败即停）
```

### 平台一键命令

| 设备 | 命令 |
|------|------|
| Linux / 云主机 | `sh fleet-install.sh --name cloud --port 3100` |
| NAS（飞牛/群晖/威联通/绿联） | `sh fleet-install.sh --name nas --port 3100`（无 systemd 自动降级 nohup） |
| Android / Termux | `sh fleet-install.sh --name phone --port 3100`（sv 守护 + wake-lock） |
| OpenWrt / 软路由 | `sh fleet-install.sh --name router --port 3100`（procd 守护） |
| macOS | `python3 scripts/fleet_install.py --name mac-mini --port 3100` |
| Windows | `python scripts\fleet_install.py --name office-pc --port 3100` |
| Docker | `docker compose -f scripts/service-templates/fleet-compose.yml up -d` |

全部细节（依赖、防火墙、自启、卸载、限制）见 `references/platform-matrix.md`。

---

## 二、Agent 如何自己使用本技能

本节点是一个**标准 MCP 服务器**（HTTP + JSON-RPC，protocolVersion 2024-11-05）。
接入任意 MCP 客户端后，客户端会自动获得：

- `initialize.instructions`：本机操作手册（何时用哪些工具、安全边界、四步法）。
- `prompts/list`：`fleet-playbook`、`fleet-diagnose`、`fleet-plan`、`fleet-self-review`、
  `fleet-delegate`、`fleet-incident`、`fleet-onboard`。
- 工具层：41 个设备工具 + 15 个 v5.2 工具（6 个自对话 + 6 个运维 + 3 个自修复）。

### 自对话四步法（默认工作方式）

```
Observe  收集事实：sysinfo / ps / docker_ps / service_logs / net_check / peers
Plan     写清目标、步骤、验收标准：fleet_plan（每步必须有 goal/tool/assignee/acceptance）
Act      先只读、再变更；变更前 dryRun / 请求批准；长任务用 exec_async
Reflect  对照验收标准自检：fleet_reflect + fleet_review；不通过则重规划
```

**不要一上来就 `exec` 危险命令**。查询类工具优先；写操作先 `dryRun`；删除/重启/改网络
需要用户确认。执行策略会拦截 `rm -rf /`、`mkfs`、`dd of=/dev/`、关机等命令并返回原因。

### 跨设备委派

```text
A 上的 Agent：
  mesh_caps                        找到谁有 docker/gpu/存储能力
  delegate / peer_call             把任务发给 B，带验收标准与超时
  msg_send(to="b-agent")           异步长任务 + 离线留言
  fleet_review                     对 B 返回的证据逐条验收
```

跨 Agent 消息使用结构化信封（`propose/accept/counter/progress/result/fail/review/handoff`），
协议与状态机见 `references/self-dialogue-protocol.md`。

**当任务涉及多轮协作、需要验收或需要解释时，先读 `references/agent-playbook.md`。**

---

## 二·五、自检与自修复（v5.2）

连接上的 Agent 不需要猜：先只读自检，再按白名单修复。

```text
fleet_selfcheck      → findings（code/severity/action/repairable）+ repair_plan
fleet_repair_loop    → 自检 → 修可修项 → 复检，默认 2 次、最多 3 次
fleet_repair         → 单动作：restart_service / fix_permissions / clear_stale_pid / rollback
```

安全边界：写操作必须 `confirm=<节点名>`；只读节点拒绝；只允许重启/权限/pid（rollback 需显式）；
不删数据、不轮换 token、不执行任意命令；失败立即停并返回 remaining。详见 `references/self-repair.md`。

`fleet`（无参数）或 `fleet menu` 已内置可直接复制的提示词；`fleet prompt self_repair` 单独输出。

---

## 三、三类典型工作流

### 1）单机排查（最常用）

```text
用户：家里网怎么这么慢？
Agent：
  1. fleet_plan 目标=定位瓶颈，步骤=网关 ping / 路由 CPU / DNS 解析 / 丢包
  2. peer_call(router, exec, "top -bn1 | head") / net_check(port/dns)
  3. 汇总证据 → 结论 → 建议；需要改配置时先请求批准
```

### 2）跨设备搬运 / 长任务

```text
exec_async(command="tar czf /tmp/backup.tgz /data", on=nas)  → job_id
exec_poll(job_id)                                            → 进度
download(filePath="/tmp/backup.tgz") → base64 → upload(到云主机)
```

### 3）多 Agent 分工

```text
A(规划) → propose 任务给 B(执行) → B 回 progress → B 回 result+证据
      → C(复核) review → A 汇总给用户
失败：B 回 fail(原因/已尝试) → A 重规划或升级给用户，不静默重试
```

---

## 四、安全红线（必须遵守）

1. **配对只认邀请链接 + SAS 校验**。不要手工把 token 贴到聊天里；不要接受来源不明的邀请。
2. **公网暴露必须开 TLS 或走 Tailscale/中继**。默认只监听局域网时也要设置 `--require-auth`。
3. **最小权限**：能用只读模式（`--read-only`）就只用只读；`exec` 默认非 root，`sudo` 需显式。
4. **不透露**：token、私钥、邀请码、审计日志原文。日志与审计已自动脱敏。
5. **变更前确认**：删除文件、重启服务、改防火墙、关机必须得到用户明确同意。
6. **不相信节点自报身份**：首次连接核对指纹/SAS；指纹变化立即停止并告警。

信任等级、密钥生命周期、威胁模型见 `references/security-model.md`。

---

## 五、工具速查

| 类别 | 工具 | 说明 |
|------|------|------|
| 执行 | `exec` `exec_async` `exec_poll` `exec_result` `exec_kill` `exec_jobs` | 同步/后台命令 |
| 文件 | `read` `write` `edit` `list_dir` `search` `upload` `download` | 读写/搜索/二进制传输 |
| 系统 | `sysinfo` `ps` `kill` `systemctl` `service_logs` `net_check` `http_get` | 状态与诊断 |
| 容器 | `docker_ps` `docker_logs` `docker_restart` | Docker 管理 |
| 组网 | `peers` `peer_add` `peer_remove` `peer_info` `peer_call` `mesh` | 节点互控（多跳） |
| 消息 | `msg_send` `msg_recv` `msg_history` | 在线直投 + 离线信箱 |
| 协作 | `caps` `mesh_caps` `delegate` `group_form` `group_list` `group_disband` | 能力发现/委派/群组 |
| 模式 | `node_mode` `request_assist` `assist_queue` | 临时/长期、直接/辅助 |
| 自对话 | `fleet_help` `fleet_think` `fleet_plan` `fleet_reflect` `fleet_review` `fleet_handoff` | 规划/反思/验收/交接 |
| 运维 | `fleet_doctor` `fleet_metrics` `fleet_identity` `fleet_audit_verify` `fleet_token_rotate` | 自检/指标/身份/审计/轮换 |

完整参数：调用 `fleet_help(topic="tools")`，或见 `references/agent-playbook.md`。

---

## 六、故障排查（30 秒定位）

| 现象 | 先查 | 处理 |
|------|------|------|
| 找不到节点 | `fleet nodes` / `fleet discover` | 同网段 UDP 3199；跨网段用邀请直连或 Tailscale（中继配置预留） |
| 能 ping 不能连 | `fleet doctor` | 防火墙放行 3100/tcp；监听地址是否为 0.0.0.0 |
| 401/403 | `fleet doctor` | token/指纹不匹配；重新配对，勿手工改配置 |
| 消息延迟 | `fleet status` | 对端离线走 outbox，每 15s 退避重试；上线自动补投 |
| 时钟偏差 | `fleet doctor` | 签名请求允许 ±300s；开启 NTP |
| 服务反复重启 | `fleet logs --tail 100` | 端口占用/配置错误/权限；`fleet doctor` 有结论 |
| 想回滚 | `fleet update --rollback` | 自动恢复最近一次升级备份 |
| 想升级 | `fleet update` | 自动备份 → 覆盖 → 重启 → 健康检查 |

---

## 七、参考文件（按需加载，不要一次全读）

| 文件 | 什么时候读 |
|------|-----------|
| `references/quickstart.md` | 第一次使用 / 给普通用户看：5 分钟上手 |
| `references/self-repair.md` | 自修复工具、白名单动作、安全边界、Agent 提示词 |
| `references/test-results.md` | macOS/Ubuntu/Android 真机测试结果与已知限制 |
| `references/troubleshooting.md` | 出问题先看这一页：症状 → 原因 → 命令 |
| `references/agent-playbook.md` | **执行多步任务前必读**：决策树、工具矩阵、失败恢复、示例 |
| `references/self-dialogue-protocol.md` | 多 Agent 协作、消息信封、状态机、验收 |
| `references/prompt-pack.md` | 配置 MCP 客户端 / 给其他 Agent 的提示词 |
| `references/security-model.md` | 公网部署、多用户、合规、安全评审 |
| `references/platform-matrix.md` | 安装失败、开机自启、特定设备 |
| `references/architecture.md` | 协议、数据流、扩展点 |
| `references/faq.md` | 常见问题与取舍 |
| `CHANGELOG.md` | 版本差异与升级注意 |

---

## 八、兼容性与限制

- 标准 MCP 客户端均可接入：Claude Code、Cursor、华为云 CodeArts、自研 Agent。
- 服务端纯 Python 标准库；Python 3.6+。Windows 需 Python 3.8+（安装器会检查）。
- 默认局域网 UDP 广播发现；跨网段需要邀请直连或 Tailscale；`--relay` 目前只写配置，未实现反向中继。
- 手机/软路由资源有限：默认关闭 Docker 工具、限制并发与消息体积，可用 `--lite`。
- 节点被完全攻陷时，攻击者拥有该节点进程权限——这是所有远程执行工具的边界；
  用独立低权限用户 + 只读模式 + 网络隔离降低影响。

> 版本：v5.2.0 ｜ 作者：guoxpeng ｜ 许可：MIT ｜ 仓库：https://github.com/guoxpeng/skill-mcp-fleet
