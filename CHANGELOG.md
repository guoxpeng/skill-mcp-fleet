# Changelog · MCP Fleet

所有版本的更新说明。版本号与 `scripts/mcp_agent.py` 内 `DEFAULT_CONFIG["version"]` 对齐，
每个里程碑版本对应一个 git tag 与一次华为云技能市场提交（submit_id）。
参赛文档中的 "v4.6" / "v5.0" 等简称为对应语义化版本的里程碑标记。

---

## [5.0.0] — 2026-10-17 · 当前参赛版本（git tag: v5.0.0）

**安全大版本：加密、防伪、可视化、跨平台、测试护栏。**

### 新增
- **邮箱落盘加密**：节点间消息（inbox/outbox/sent）以 `enc1$` 前缀密文落盘，密钥由配对 token
  经 PBKDF2-HMAC-SHA256（100k 轮）派生，采用 HMAC-SHA256 keystream 的 CTR 结构加密 text 字段；
  to/from/topic 保持明文供路由。物理拿到硬盘无法读到明文，v4.x 明文邮箱自动兼容读取。
- **UDP hello 防伪签名**：已配对节点的发现包必须携带 HMAC 签名且验签通过，冒充已知节点直接
  拒收并计数（`_rejected`）；对 v4.x 无签名旧节点保持兼容放行，混部不破坏网络。
- **Web Dashboard**：`/fleet/ui` 单文件页面（零 CDN 依赖，离线可用），KPI 卡片 + mesh 拓扑图 +
  最近消息流，5 秒自动刷新；token 经 URL fragment（`#token`）传入，不发送到服务器。
- **跨平台安装器** `scripts/install.py`：纯标准库，支持 Windows（自动防火墙放行 + Startup
  文件夹开机自启）/ Linux / macOS；`--uninstall` 卸载时保留邮箱数据。Linux 仍推荐含 systemd
  的 `install.sh`。
- **测试套件** `tests/`：61 用例 — 加密 12 / 邮箱读写与并发 24（含投递三分支）/ hello 验签 10
  （含真实 UDP socket 场景）/ 工具层 7 / 双节点端到端集成 8。`python3 -m pytest tests/ -q`
  约 1 秒全量回归。

### 修复
- **gossip 同步死锁**：`MailEngine.sync_summary()` 持 `threading.Lock` 期间调用
  `outbox_pending()`（内部再取同一把锁）→ 非重入死锁。v4.1 引入，潜伏 6 个版本，
  由本版新增测试套件首跑暴露（180s 超时），修复为 `RLock`。证据链见
  `references/安全审计报告.md` 第七节。
- `demo.sh` 演示节点拷贝列表缺失 `dashboard_html.py`（静默失去 Dashboard）。

### 变更
- `mcp_agent_config.example.json` 版本字段与代码对齐（4.0.0 → 5.0.0）。
- 文档：SKILL.md / README.md 升 v5.0；安全审计报告新增「自发现自修复记录」。

**提交**：华为云技能市场 submit_id=204 起；GitHub Release v5.0.0 附 `mcp-fleet-v5.0.0.zip`。

---

## [4.6] — 2026-10-17（git tag: v4.6）

- 新增 `scripts/demo.sh` 一键演示：本机 30 秒拉起双节点（demo-nas/demo-cloud），
  自动演示 UDP 互发现、跨节点操控、消息投递、mesh 拓扑。
- 修复 `mesh` / `mesh_caps` 工具遍历 peers 的层级 bug（拓扑中对等节点显示异常）。
- SKILL.md：新增「谁需要它」六类用户画像、兼容性清单、触发句式。
- 新增 `references/传播度证据.md`（GitHub 747 克隆 / 281 独立访客，市场浏览量并列第一，附复现命令）。
- 移除静态 `fleet-http.service` 模板（平台兼容性）。

**提交**：华为云技能市场 submit_id=199。

---

## [4.5] — 2026-10-17（git tag: v4.5）

- **TLS 加密**：`--tls-cert/--tls-key` 启动 HTTPS，跨网段通讯加密。
- **审计日志**：每次工具调用记录（时间/IP/工具/参数）到 `audit.log`。
- **dryRun 预览**：`edit` 工具支持预览修改效果而不实际写入。
- **6 个新工具**：`search`（内容/文件名搜索）、`upload`/`download`（跨节点文件传输）、
  `ps`/`kill`（进程管理）、`net_check`（网络连通诊断）— 工具总数达 41。
- 参数命名统一 `filePath`（兼容旧名）；Python 3.6 完全兼容。

---

## [4.4] — 2026-10-17（git tag: v4.4）

- **邀请链接配对**：邀请码 = base64(json{设备码, API, token, invite_id, 过期时间})，
  支持 TTL 过期（默认 1 小时）、单次有效（防重放）、接收方显式信任确认。
- 标准 AgentSkill 提交包打包。

---

## [4.3] — 2026-10-17（git tag: v4.3）

- **设备码自动生成**：hostname 前缀 + 4 位随机（如 `nas-a3f2`），安装输出醒目展示。
- 配对一步化：`fleet.py pair <设备码>@<地址>`。

---

## [4.2] — 2026-10-17（git tag: v4.2）

- **连接模式**：`--mode temporary`（TTL 自动退出，不装自启）/ `permanent`。
- **控制模式**：`--control assisted`（远程 agent 需本机 agent 审查批准）/ `direct`。
- 新增工具：`node_mode` / `request_assist` / `assist_queue`。

---

## [4.1] — 2026-10-17（git tag: v4.1）· 自研通讯协议层

- **UDP 广播自动发现**：同网段 fleet 节点自动互相发现，零配置，不依赖任何外部服务。
- **Store-and-forward 离线邮箱**：目标离线时消息存 outbox，上线后自动投递，不丢消息。
- **HTTP 消息端点**：`POST /fleet/mail`、`GET /fleet/poll`、`POST /fleet/sync`、`/fleet/discover`。
- **Gossip 邮箱同步**：节点间定期交换邮箱摘要，消息跨节点可达。
- Tailscale 转为可选加速（不再依赖）。

---

## [4.0] — 2026-10-17（git tag: v4.0）

- Tailscale auto-mesh（已装则自动组网）。
- Agent 协作：能力声明（caps）、任务委派（delegate）、临时群组（group_*）。
- 消息总线：`msg_send` / `msg_recv` / `msg_history`。

---

## [3.5] — 2026-10-13（git tag: v3.5）

- 节点不分主副，可双向互控（架构去中心化的第一步）。
- 离线安装包（分卷粘贴即装，适配无法直连外网的设备）。

---

## [3.4.1] — 2026-10-13（git tag: v3.4.1）

- Python 3.6 兼容（CentOS 7 等老系统）。
- tailnet 地址绕过代理修复。

---

## [3.1] — 2026-09-19

- 安全审计（13 项风险）后修复 12 项：token 鉴权、IP 白名单、保活、目录服务加固、
  `hmac.compare_digest` 等，另修 1 个审计未列出的跨平台路径 bug。
  详见 `references/安全审计报告.md` 与 `references/加固操作手册.md`。

---

## [3.0] — 2026-09-19 之前

- 初始版本：MCP 工具挂载、安装器、隧道、保活、地址目录服务。

---

## 版本号对齐说明

| 对齐点 | 值 |
|---|---|
| 代码内版本（`DEFAULT_CONFIG["version"]`） | `5.0.0` |
| git tag（当前版本） | `v5.0.0` |
| GitHub Release | `v5.0.0` |
| 华为云技能市场提交 | v5.0.0 内容（submit_id=204 起） |
| 参赛文档简称 | v5.0 |
