# Changelog · MCP Fleet

所有版本的更新说明。版本号与 `scripts/mcp_agent.py` 内 `DEFAULT_CONFIG["version"]` 对齐，
每个里程碑版本对应一个 git tag 与一次华为云技能市场提交（submit_id）。
参赛文档中的 "v4.6" / "v5.0" 等简称为对应语义化版本的里程碑标记。

---

## [5.0.4] — 2026-10-17 · 三平台守护对齐版本（git tag: v5.0.4）

**补齐 Windows / macOS 的开机自启与进程保护，三平台守护矩阵对齐 Linux 既有水位。**
**开发中发现并修复一个看门狗致命误判（鉴权节点匿名探活 401 → 无限重启风暴）。**

### 新增
- **macOS launchd 守护**：`install.py --auto-start` 生成
  `~/Library/LaunchAgents/com.mcp-fleet.<name>.plist`——`RunAtLoad`（登录自启）
  + `KeepAlive`（崩溃自动重启）+ `ThrottleInterval` 10s（防崩溃风暴），
  一个系统机制同时覆盖「开机启动」与「进程保护」，`launchctl load -w` 托管；
  卸载自动 unload 并清理（marker 文件定位，同 install.sh 的 unit.txt 模式）。
- **Windows schtasks 双任务守护**：`ONLOGON` 登录自启（pythonw 无窗口，
  替代 v5.0.3 及以前会闪窗的 Startup .bat，升级时自动清理旧启动项防双启动）
  + `MINUTE` 每分钟看门狗（探活 `/health`，异常清残留端口并拉起）——
  与 Linux 的 systemd + watchdog timer 架构对称，用户级零提权零依赖。
- **跨平台看门狗**：`install.py --watchdog` 单次模式（schtasks 每分钟调用）：
  探活通过零动作退出；不通过 → netstat 定位 + tasklist 二次确认只杀 python
  系进程（防误杀占端口的其他服务）→ DETACHED/新会话拉起 agent。
- **`install.py` 自拷贝**：安装时把 install.py 一并部署到安装目录（watchdog
  任务长期引用它，升级不失效）。

### 修复
- **[开发中实锤] 看门狗对鉴权节点必然误判（无限重启风暴）**：`/health` 与其他
  端点一样过 `_guard()` 统一鉴权（v5 审计的安全收敛：不向未认证者暴露指纹），
  开了 `auth_token` 的节点匿名探活恒 401 → 看门狗每分钟判定宕机 → 无限重拉。
  修复：watchdog 自动读安装目录 `mcp_agent_config.json` 的 `auth_token` 带凭据
  探活（本机进程持 token 天经地义），裸奔模式自动匿名；端到端回归确认匿名
  `/health` 仍 401（安全收敛未被破坏）。
- 探活防误判加固：只认 `{status:ok, version:...}` 结构——别的服务恰好占了
  端口返回 200 也不会触发重启。

### 测试
- **83 → 94 用例**：平台守护 11 项（launchd plist 结构与 XML 转义 / schtasks
  命令构造 / netstat IPv4+IPv6 PID 解析 / tasklist python 映像过滤 / 真实 HTTP
  探活含防误判与 401 场景 / watchdog 健康零动作 / token 自动携带 / 异常重启）。
- **端到端**（真实安装 + 真实 agent）：冷启动 0.5s 拉起到健康；运行中零动作；
  `kill -9` 后复活；匿名探活 401 回归。

---

## [5.0.3] — 2026-10-17 · 长期运行可靠性版本（git tag: v5.0.3）

**第四轮审查聚焦长期运行：NAS/软路由常年挂机场景下，资源只增不减的地方全部加上界。**
**邮箱 compaction、入站消息防护（限流+容量）、审计日志轮转、doctor 协议层体检。**

### 修复
- **[高危] 邮箱文件只增不减（B10）**：`inbox_read` 只推进 cursor 不移出消息、
  `sent.jsonl` 投递成功永久累积、`dead` 死信永久保留——目标设备是常年挂机的
  NAS/软路由，挂机半年 = 数 MB 邮箱 + 每次读取秒级延迟（`inbox_read` 全量加载
  逐条解密，线性变慢）。修复：`MailEngine.maintenance()` compaction——inbox
  总量超窗口（500 条）时从最旧清理「全员已读」消息（未读永远保留：agent 长期
  未读的数据不是垃圾）；sent 截断保留 200 条；dead 截断保留 200 条。由投递
  循环每 3600s 顺带触发（零新增线程），持锁原子写复用加密回写路径。
  端到端实测：36000 条已读 4.9MB → 0.07MB，`inbox_read` 0.456s → 0.007s。
- **[中危] /fleet/mail 无限流无容量上限（S1）**：工具层令牌桶不覆盖 fleet 端点，
  配对的失控节点（或 token 泄露）可用消息 flooding 耗尽磁盘。修复：独立令牌桶
  `rate_limit_mail()`（默认 600/min，与工具层桶分离——节点间消息天然高频）+
  inbox 容量硬上限 `max_inbox_msgs`（默认 5000，超出 507 拒收）。

### 新增
- **doctor 第 7 项协议层体检（U1）**：链路体检（TCP/HTTP/MCP/调用/环境/配置）
  全绿但「消息发不出去」时，答案在协议层——新增展示 inbox 积压/待投/死信/
  邮箱体积/版本 + peer 熔断冷却列表，数据源 `/fleet/ui/data`（零新增攻击面）。
- **audit.log 轮转（S2）**：超 10MB 轮转保留一份 `audit.log.1`（持锁检查防并发
  竞争），append-only 日志不再常年只增不减。
- **stats() 扩展**：`outbox_dead` / `inbox_bytes` 字段——死信数与邮箱体积可观测
  （Dashboard 与 doctor 消费）。
- **测试 70 → 83 用例**：maintenance 7 项（超窗清理/小邮箱不动/未读永留/加密
  保持交互/sent 截断/dead 截断/stats 字段）+ mail 限流桶 3 项（放行拒绝/禁用/
  与工具桶独立）+ audit 轮转 3 项（超限轮转/小文件不动/完整写入轮转路径）。

---

## [5.0.2] — 2026-10-17 · 连接稳定性版本（git tag: v5.0.2）

**第三轮审查聚焦连接稳定性：一个下线/挂死的 peer 不再拖垮全网投递与同步。**
**新增 per-peer 熔断器、消息级退避、重试上限三层防护。**

### 修复
- **[核心] 死节点拖垮投递循环**：原实现每 15s 串行重试全部 pending 消息、无退避无熔断，
  超时 10s。一个「挂起不响应」的 peer 配 2 条消息 = 每轮白等 12s，循环实际周期被拖到
  27s+，给健康节点的消息也被堵在后面。修复：① per-peer 熔断器（`PeerHealth`）——
  连续失败 2 次进入冷却期（90s 起指数翻倍，封顶 30min），冷却期内投递/gossip 一律
  跳过、零 HTTP 等待，到期放行一次探测，成功即恢复；② 消息级退避——失败后按
  attempts 退避（15s → 60s → 240s → 960s → 1h 封顶），短暂离线快速重试、长期离线
  降频省资源；③ 客户端超时 10s → 6s；④ 投递循环单轮批量上限 50 条，防超大
  outbox 挤占周期。端到端实测：3 peer 场景（1 健康 + 1 死端口 + 1 挂起主机），
  第 2 轮投递从 12s → 0.000s。
- **[堆积] outbox 无限膨胀**：原实现投递失败的消息永不放弃（attempts 无限增长、
  文件只增不减）。修复：重试上限 250 次（按退避节奏约 2 天），到顶标 `status=dead`
  停止重试，记录保留供审计；`outbox_pending()`（全量统计语义，Dashboard 用）与
  `outbox_retryable()`（本轮可试，退避过滤）分离。
- **[同步] gossip 被死 peer 拖慢**：`sync_loop` 串行遍历 peers，每个 sync 超时 10s，
  一个死 peer 每轮 gossip 白等 10s（poll 序列再放大）。修复：`SyncEngine` 共享同一
  `PeerHealth` 实例（投递与同步的健康状态全局部一致），冷却中的 peer 直接跳过。

### 新增
- **Dashboard 熔断可视化**：peer 卡片显示「冷却 Ns」徽标（`/fleet/ui/data` 的
  `cooldown` 字段），节点连接状态一目了然。
- **测试 61 → 70 用例**：新增 9 个连接稳定性用例（退避曲线/退避过滤/新消息立即
  可试/上限标 dead/熔断状态机/独立键/冷却投递零 HTTP/冷却同步零请求/真实不可达
  端口计数）。

---

## [5.0.1] — 2026-10-17 · 稳定性版本（git tag: v5.0.1）

**二轮代码审查捕获的四个 bug：投递正确性、加密正确性、性能、跨平台各一，全部实锤复现后修复。**

### 修复
- **[严重] 邮箱明文回写（加密静默失效）**：`_load_jsonl` 读取时解密 → 修改（投递重试计数 /
  成功移出 / poll 拉取）→ `_save_jsonl` 写回时未重新加密，一次 outbox 重试后整个邮箱文件
  退化为明文，落盘加密形同虚设（投递循环每 15s 触发一次）。修复：`_save_jsonl` 写盘前对
  每条消息过 `_encrypt_field`（幂等：已有 `enc1$` 前缀不会二次加密），inbox/outbox/sent
  全路径覆盖。实锤：修复前 `outbox_increment_attempt` 一次后文件全明文；修复后任意读写
  轮次密文保持。
- **[架构] 远程 agent 单播永远投不出去**：`deliver_now` 只匹配 `to in peers`（节点名），
  单播给远程 agent（agent 名）永不命中 → 返回 False → 消息滞留 outbox 无法自动投递
  （仅 `to=*` 广播可经 poll 到达），且 `/fleet/sync` 收到对端 `local_agents` 直接丢弃、
  归属信息不传播。修复三件套：① `deliver_now` 新增 agent 归属匹配——`to` 非 peer 名且
  非 `*` 时遍历 peers 的 `agents` 列表，命中则投给宿主节点；② `SyncEngine` 新增
  `on_peer_agents` 回调，gossip 响应携带对端 `local_agents` 时上报；③
  `mcp_agent._note_peer_agents` 把对端 agent 记入 peers 条目（有变化才写盘，上限 64
  防御）。端到端验证（双节点 HTTP 实测）：归属同步前投递 False → 同步后 True。
- **[性能] PBKDF2 每条消息重复派生**：单次派生 ~18ms（100k 轮），`_load_jsonl` 逐条解密
  N 条消息 ~N×18ms（50 条实测 0.89s），投递循环(15s)/Dashboard 轮询(5s)/gossip(60s)
  每轮重复支付。修复：`_derive_key` LRU 缓存（模块级，上限 128，锁保护）+ 邮箱固定
  salt（`mailbox/.salt`，初始化时加载/创建）→ 同一邮箱共享派生密钥，写读均 O(1) 派生；
  nonce 仍每条随机，安全性等价；旧格式（每条独立 salt）解密自动兼容。实测 50 条
  0.89s → 0.00s。
- **[Windows] 中文路径启动脚本乱码**：`install.py` 以 utf-8 写 `start-fleet.bat`，
  cmd 按 ANSI 代码页（中文系统 = GBK）解码 → 中文用户名安装路径乱码（`张三` →
  `寮犱笁`），`cd /d` 失败节点起不来。修复：新增 `_write_bat` 按系统 ANSI 代码页写入
  （ASCII 路径下字节与旧版完全一致，极端不可编码字符退回 utf-8），Startup 开机自启
  bat 同步修复。

### 验证
- 61 用例全量回归通过（`python3 -m pytest tests/ -q`，约 2s）。
- B3 端到端：模拟双节点（真实 HTTP `/fleet/mail` + `/fleet/sync`），单播远程 agent 实收、
  未知 agent 滞留、gossip 归属回调触发、同步前 False → 同步后 True 闭环。
- B4：GBK 代码页模拟验证（中文路径 GBK 落盘读回一致 / 旧版乱码复现 / ASCII 字节零回归 /
  极端字符不崩溃）。

---

## [5.0.0] — 2026-10-17 · 安全大版本（git tag: v5.0.0）

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
