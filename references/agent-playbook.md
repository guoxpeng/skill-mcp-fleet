# Agent Playbook · MCP Fleet 操作手册

> 读者：接入 MCP Fleet 的任意 AI agent（Claude Desktop / Cursor / VSCode / 自研）。
> 目标：读完这一篇，你会**自己规划、反思、委派、验收**，而不是只会调一个 `exec`。
> 配套：协议细节见 `self-dialogue-protocol.md`；可直接粘贴的提示词见 `prompt-pack.md`；
> 机器可读版会由 `scripts/fleet_agent.py::build_instructions()` 注入 MCP `initialize`。

---

## 0. 30 秒速查

| 你现在想干什么 | 先做这个 | 再做这个 |
|---|---|---|
| 不知道有哪些能力 | `caps` / `mesh_caps` | 决定本机还是远端 |
| 看本机状态 | `sysinfo` / `ps` / `docker_ps` | 有异常再查日志 |
| 在另一台设备执行 | `peers(check=true)` | `peer_call(peer=…, tool="exec")` |
| 多步任务 | `fleet_plan` | 逐步执行 + `fleet_review` |
| 拿不准 / 高风险 | `fleet_think` 记录事实 | `fleet_handoff` 请人确认 |
| 失败了 | `fleet_reflect` 记录 | 重试 1 次 → 换节点 → 交人 |
| 要通知别的 agent | `fleet_handoff` 生成信封 | `msg_send(topic="fleet-task")` |

三条铁律：

1. **先观察再动手**：任何变更之前，先用只读工具拿到事实（`sysinfo` / 日志 / 配置）。
2. **每步都有验收标准**：`fleet_plan` 的 `acceptance` 必须可判定（exit code、状态字符串、HTTP 码），
   不允许「看看是否正常」这种不可判定写法。
3. **不确定就问人**：策略 `ask` 时返回待确认，不要换工具绕过去。

---

## 1. 这个节点是什么，什么时候用它

fleet 节点 = 一台真实设备上的 MCP 服务器。它同时是：

- **本机执行代理**：shell / 文件读写 / systemd / Docker / 进程 / 网络诊断；
- **mesh 网络入口**：`peers`、`peer_call` 可一跳或多跳访问内网其他节点；
- **任务与消息中枢**：邮箱（`msg_send` / `msg_recv`）+ 任务日志（`fleet_think` / `fleet_plan`）。

**该用它的场景**：需要「真实机器上」的执行结果（服务状态、磁盘、容器、日志），
或者需要跨多台设备协同完成一个任务（一台诊断、一台执行、一台验证）。

**不该用它的场景**：纯文本写作、数学计算、查公开资料——不要为了用工具而用工具。
**职责外**：绕过鉴权、改别人配置、无边界删除/格式化——直接拒绝并说明原因。

节点之间**不分主副**：任何节点都能发起任务、委派、汇总。谁是「主」取决于这次任务由谁规划。

---

## 2. 决策树：遇到任务先问这四个问题

```
收到任务
│
├─ Q1 目标是否已经可判定？ ──否──▶ fleet_help / 向调用方澄清
│                                  （把「弄好」改成「nginx 状态 running 且 /health 返回 200」）
├─ Q2 需要变更吗？
│    ├─ 否（只读）──▶ Observe 工具链：sysinfo/ps/docker_ps/net_check/read
│    └─ 是 ──▶ 先 fleet_plan（含备份/回滚/验收）→ 高风险走 ask/handoff
│
├─ Q3 在哪台机器做？
│    ├─ 本机 ──▶ 直接 exec/systemctl/docker_*
│    └─ 远端 ──▶ peers(check=true) → mesh_caps → peer_call(peer=节点名, …)
│                   （多跳：A→B→C，B 上再 peer_call C；记录每一跳）
│
└─ Q4 失败/超时怎么退？
     ├─ 同语义重试 1 次（只改超时/参数）
     ├─ 换同能力备用节点（caps_index）
     ├─ 降级为「可确认的局部结论」
     └─ fleet_reflect + fleet_handoff 交人；说明「未完成/需要什么」
```

**判断原则**：证据不足时宁可多一次只读调用，也不要猜；一次只做一个变更，
变更后立刻验证（`http_get` 健康检查、`systemctl is-active`、`docker_ps` 状态）。

---

## 3. 工具选择矩阵

### 3.1 本机观察（只读，优先做）

| 工具 | 用途 | 关键参数 | 备注 |
|---|---|---|---|
| `sysinfo` | 主机/负载/内存/磁盘/CPU TOP5 | 无 | 每次任务的第一步 |
| `ps` | 进程列表（按 CPU） | `all=true` | 找异常进程/内存占用 |
| `docker_ps` | 容器列表 | `all=true` | 看状态与重启次数 |
| `docker_logs` | 容器日志尾部 | `name`, `lines` | 先日志后重启 |
| `service_logs` | systemd 日志 | `unit`, `lines` | `journalctl -u` |
| `list_dir` / `read` | 目录/文件 | `filePath` | 读配置前先列目录 |
| `net_check` | 端口/DNS/监听 | `action=port|dns|listen` | 排查连通性 |
| `http_get` | 本机 HTTP 探测 | `url` | 健康检查（127.0.0.1） |
| `search` | 内容/文件名搜索 | `filePath`, `pattern`, `type` | 找配置位置 |

### 3.2 本机变更（写/重启，先计划与备份）

| 工具 | 用途 | 风险 | 前置动作 |
|---|---|---|---|
| `exec` | 任意命令 | 中-高 | 先 `fleet_plan`；危险命令策略会 deny |
| `exec_async` + `exec_poll`/`exec_result` | 长任务 | 中 | 不要阻塞；用 job_id 轮询 |
| `write` / `upload` | 写文件 | 中 | `read` 备份原文件 |
| `edit` | 局部替换（`dryRun` 预览） | 低-中 | 先 dryRun，再实际写 |
| `systemctl` | 服务管理 | 高 | 先 `status`/`is-active`，变更后验证 |
| `docker_restart` | 容器重启 | 高 | 先 `docker_logs` 确认原因 |
| `kill` | 进程终止 | 高 | 确认 pid 与用户，优先 TERM |
| `node_mode` | 节点控制/临时退出 | 高 | 通常不应由 agent 主动调用 |

### 3.3 跨设备（mesh）

| 工具 | 用途 | 说明 |
|---|---|---|
| `peers` | 列出对等节点 | `check=true` 顺带探活（有 RTT） |
| `peer_info` | 本节点回连地址 | 配对时对方调用 |
| `peer_call` | 在其他节点调工具 | `peer` 用节点名；`tool` 如 `exec`/`sysinfo` |
| `peer_add` / `peer_remove` | 配对管理 | 需要对方地址与 token（密钥，勿外泄） |
| `mesh_caps` | 全 mesh 能力清单 | 找「谁有 docker/gpu」 |
| `delegate` | 按能力自动选节点执行 | 简单委派；复杂任务用 `fleet_plan` + `peer_call` |
| `mesh` | 拓扑与在线状态 | 汇总前的全局视图 |

### 3.4 协作与自对话（v5.2 新增）

| 工具 | 用途 | 何时用 |
|---|---|---|
| `fleet_help` | 操作手册 + 下一步建议 | 不确定流程时 |
| `fleet_think` | 把观察写进任务日志 | 每步 Observe 之后 |
| `fleet_plan` | 校验并保存计划 | 任何 ≥2 步任务 |
| `fleet_reflect` | 记录结果与教训 | 每步 Act 之后 / 失败时 |
| `fleet_review` | 按验收标准逐条判定 | 任务收尾 |
| `fleet_handoff` | 生成/投递 handoff 信封 | 委派或交人 |
| `msg_send` / `msg_recv` | 邮箱收发 | 跨 agent 异步协作 |

### 3.5 能力 → 工具速查

| 需求 | 首选 | 备选 |
|---|---|---|
| 容器 | `docker_ps` / `docker_logs` / `docker_restart` | `exec docker …` |
| 服务 | `systemctl` / `service_logs` | `exec systemctl …` |
| 磁盘 | `exec df -h` / `du -sh` | `sysinfo` |
| 网络 | `net_check` | `exec ping/curl` |
| 文件 | `read` / `edit` / `write` | `exec sed`（不推荐，难回滚） |
| 依赖/端口 | `net_check(action=listen)` | `exec ss -lntp` |
| 批量多机 | `fleet_plan` + `peer_call` | `delegate` |

---

## 4. 自对话四步法 Observe → Plan → Act → Reflect

这不是仪式，是**降低误操作概率的检查点**。每一步都落到具体工具调用上。

### 4.1 Observe（只收集事实，不解释）

```json
{"tool": "fleet_think", "args": {
  "task_id": "t-nginx-01",
  "thought": "nas 上 nginx 容器 5 分钟内重启 3 次，日志末尾 OOMKilled",
  "evidence": ["docker ps: nginx Restarting(137)", "docker logs: OOMKilled"]
}}
```

要点：

- 观察原文不改写，带证据（命令 + 关键输出）；
- `fleet_think` 返回结构化的 `next_action`（plan/act/reflect/review/handoff）；
- 多个假设都保留，不要过早收敛。

### 4.2 Plan（每步 goal + tool + assignee + acceptance，缺一不可）

```json
{"tool": "fleet_plan", "args": {
  "task_id": "t-nginx-01",
  "goal": "确认 nginx 重启根因并恢复服务",
  "steps": [
    {"goal": "确认容器状态与内存限制", "tool": "docker_ps", "assignee": "nas",
     "acceptance": "返回 nginx 状态与内存限额，且记录重启次数"},
    {"goal": "读取容器内存相关配置", "tool": "read", "assignee": "nas",
     "acceptance": "输出含 mem_limit 或 resources.limits 的配置片段"},
    {"goal": "调整内存上限并重启容器", "tool": "exec", "assignee": "nas",
     "acceptance": "exit=0，随后 60s 内状态为 running 且无新 OOM",
     "args": {"command": "docker update --memory 1g nginx && docker restart nginx"}},
    {"goal": "健康检查", "tool": "http_get", "assignee": "nas",
     "acceptance": "HTTP 200，响应时间 < 2s"}
  ]
}}
```

`fleet_plan` 会**拒绝**缺字段的步骤并给出可操作错误（哪个步骤、缺哪个字段、示例）。
计划落盘到任务日志，`resume` 时可据此跳过已完成步骤。

### 4.3 Act（一次一步，验证后再下一步）

```json
{"tool": "peer_call", "args": {"peer": "nas", "tool": "docker_ps",
                               "arguments": {"all": true}}}
```

- 高风险操作（重启/删除/改网络）先 `dryRun` 或 `fleet_handoff` 请人确认；
- 命令输出保留关键行（不要只留「成功」）；
- 每步后 `fleet_think` 记结果；失败先 `fleet_reflect`，不要连续重试同一命令。

### 4.4 Reflect（记录结果与教训，再验收）

```json
{"tool": "fleet_reflect", "args": {
  "task_id": "t-nginx-01", "outcome": "success",
  "what_happened": "内存上限从 256m 调到 1g 后容器稳定运行 10 分钟",
  "lessons": ["容器 OOM 先看 docker logs 的 OOMKilled", "改配置前导出 inspect"]
}}
```

然后 `fleet_review`：

```json
{"tool": "fleet_review", "args": {
  "task_id": "t-nginx-01",
  "acceptance": ["nginx 状态 running", "健康检查返回 200", "无新 OOM 日志"]
}}
```

`fleet_review` 对每条标准给出 `passed / failed / unknown` + 证据；
**没有证据就是 unknown，不会因为「看起来好了」判 passed**。

---

## 5. 跨设备任务编排

### 5.1 能力路由

```
caps_index = {"docker": ["nas", "cloud-1"], "gpu": ["workstation"]}
                                │
任务需要 docker ────────────────┘
        │
        ├─ 有 caps_index → 取第一个候选（失败自动升级到下一个）
        └─ 无 caps_index → 先 mesh_caps 自查；查不到则本机降级并记录 reflection
```

调用方（集成 `fleet_agent.Orchestrator` 的代码）可以把 `mesh_caps` 的结果
用 `build_caps_index()` 转成 `{能力: [节点]}` 再传入，即可获得自动备用节点选择。

### 5.2 一次完整的跨设备任务（文字时序）

```
调用方       本节点(mac)                  远端(nas)              其他 agent
  │              │                            │                     │
  │ run(goal)    │                            │                     │
  │─────────────▶│ 1. sysinfo（本机观察）      │                     │
  │              │ 2. 路由：caps_index→nas     │                     │
  │              │ 3. peer_call(exec docker ps)│                     │
  │              │───────────────────────────▶│ 执行                │
  │              │◀───────────────────────────│ 容器列表 + 状态      │
  │              │ 4. 汇总 result/review       │                     │
  │◀─────────────│ {status: completed, …}      │                     │
  │              │                            │                     │
  │ 复杂协作：    │ 5. fleet_handoff → 信封     │                     │
  │              │ 6. msg_send(topic=fleet-task)──────────────────▶│
  │              │                            │   7. msg_recv 收进度 │
  │              │◀────────────────────────────────── progress/result│
```

### 5.3 委派 ≠ 免责

- 发出 handoff 的节点仍是**验收责任人**：对方返回 result 后必须 `fleet_review`；
- 对方超时未回：先 `msg_recv(wait=…)`，再 `fleet_reflect(outcome="partial")`，
  然后换节点或交人；
- 委派消息里必须写清：目标、验收标准、截止时间、失败时找谁。

### 5.4 多跳内网

```json
{"tool": "peer_call", "args": {
  "peer": "nas", "tool": "peer_call",
  "arguments": {"peer": "printer", "tool": "sysinfo", "arguments": {}}
}}
```

每一跳都要确认「谁在执行」，汇总时写清链路 `mac → nas → printer`。

---

## 6. 失败恢复手册

| 现象 | 先检查 | 处理动作 | 升级条件 |
|---|---|---|---|
| 连接超时 | `peers(check=true)` | 重试 1 次（+超时）；换备用节点 | 两节点都不通 → 交人 |
| 命令 exit≠0 | stderr 前 20 行 | 修正参数后重试 1 次 | 同错复现 → `fleet_reflect` |
| 服务起不来 | `service_logs` / `docker_logs` | 回滚配置 → 重启 → 验证 | 回滚也失败 → ask/handoff |
| 磁盘满 | `df -h` / `du -sh` | 清理自己产生的临时文件 | 需要删业务数据 → 必须问人 |
| 权限不足 | `id` / `ls -l` | 换有权限的路径；不要盲目 sudo | 需要提权 → `fleet_handoff` |
| 策略 deny | 返回的 rule | 换允许的工具/路径 | 业务必须 → 请人调整策略 |
| 策略 ask | 待确认项 | 展示理由，等确认 | 无人确认 → 记录并停止 |
| 对端版本旧 | 错误信息 / `peer_info` | 降级到旧工具/纯文本消息 | 不兼容 → 升级对端 |
| 返回内容过长 | — | 只取关键行，落盘到文件再 read | — |

**重试规则**：同语义最多 1 次（只允许改超时、并发、分批大小）。
连续失败时**改收集证据**，不要用不同参数「撞」结果。

**回滚模板**：

1. 备份：`exec cp -a <config> <config>.bak.<ts>`（或 `read` 保存原文）；
2. 变更：`edit` 或 `write`；
3. 验证：`http_get` / `systemctl is-active` / `docker_ps`；
4. 失败回滚：`exec cp -a <config>.bak.<ts> <config>` + 重启 + 再次验证；
5. 记录：`fleet_reflect` 写清回滚原因与证据。

---

## 7. 安全红线与策略

### 7.1 红线（任何情况下不做）

- `rm -rf /`、`rm -rf ~`、`dd of=/dev/sd*`、`mkfs*`、`shred` 磁盘；
- 清空生产数据库/表、无备份覆盖唯一副本；
- 修改 SSH 配置、防火墙默认策略、路由导致设备失联（需要时先确认带外访问）；
- 关闭鉴权、把 token/私钥/邀请码写进日志、消息、代码、提交；
- 在 `read_only` 节点上执行任何变更类工具；
- 用 `deny` 之外的工具绕过策略（例如策略禁 `docker_restart` 却用 `exec docker restart`）。

### 7.2 策略判定（allow / ask / deny）

执行前工具会检查调用方注入的 policy：

| 判定 | 行为 | agent 应该做什么 |
|---|---|---|
| `allow` | 直接执行 | 正常执行并记录证据 |
| `ask` | **不执行**，返回待确认项 | 展示理由与影响面，等人工确认后再执行/调用 resume |
| `deny` | 拒绝执行 | 换允许的替代方案；业务必须则请人调整策略 |

策略对象可能是 dict（`{"deny":["rm -rf"], "ask":["restart"]}`）、callable 或带
`check(tool, args) -> (verdict, reason)` 的对象；策略检查本身异常时按 **deny**（fail-closed）。

### 7.3 数据安全

- 回复/日志里对 token、密码、邀请码只写「已配置」，不写值；
- 需要展示命令时，先确认命令里没有内联密钥（用环境变量或 stdin）；
- 上传/下载二进制走 `upload`/`download`（base64），不要贴到聊天里。

---

## 8. 完整示例对话

### 示例 A：单节点诊断（只读，2 分钟）

**用户**：帮我看看这台机器为什么变慢。

**Agent**：

1. `fleet_plan`
```json
{"goal": "定位本机变慢原因", "steps": [
  {"goal": "看负载/内存/磁盘", "tool": "sysinfo", "assignee": "local",
   "acceptance": "返回 load/内存/磁盘使用率"},
  {"goal": "找 CPU 占用最高的进程", "tool": "ps", "assignee": "local",
   "acceptance": "返回 TOP 进程列表"},
  {"goal": "确认磁盘是否写满", "tool": "exec", "assignee": "local",
   "acceptance": "df -h 输出可用空间 > 10%", "args": {"command": "df -h"}}
]}
```

2. `sysinfo` → `{"load": [8.2, 7.9, 7.1], "mem_used_pct": 91, "disk_used_pct": 88}`
3. `ps` → `python3 backup.py` CPU 380%
4. `exec df -h` → `/data 88%`
5. `fleet_think`：`{"thought": "load 8.2，内存 91%，backup.py 占满 CPU，/data 88%"}`
6. 结论：
   - 现象：load 高、内存紧张、/data 接近满；
   - 根因假设：备份任务与业务争抢 CPU/IO（强）；磁盘满导致 swap/写放大（中）；
   - 建议：备份限速/错峰（`nice`/`ionice`），清理 `/data` 过期临时文件；
   - 未做变更（只读），需要执行请确认。
7. `fleet_reflect(outcome="success")` + `fleet_review`。

### 示例 B：跨设备发布（变更，含确认点）

**用户**：把新版静态站点发到 nas 并验证。

**Agent**：

1. `fleet_plan`（assignee=nas，每步含验收 + 回滚）
2. `peer_call(peer="nas", tool="exec", arguments={"command": "ls -l /srv/site && df -h /srv"})`
3. `read` 当前 `index.html` 前 5 行（留档）
4. `edit(dryRun=true)` 预览 → 展示 diff
5. **ask 确认点**：`fleet_handoff(to="nas", goal="替换 /srv/site/index.html 并重启 nginx")`
   —— 若策略返回 ask，则先向用户展示：
   - 将要执行：覆盖 1 个文件、`systemctl restart nginx`；
   - 回滚：`/srv/site/index.html.bak.<ts>`；
   - 影响：站点 2-3 秒不可用。
6. 用户确认后：`peer_call` 执行备份 + 写入 + 重启。
7. `http_get("http://nas:8080/")` → 200；`peer_call` 检查 nginx `is-active`。
8. `fleet_reflect(outcome="success")`；`fleet_review(["HTTP 200", "nginx active", "文件哈希一致"])`。
9. 汇总：谁（nas）、做了什么、结果、证据（HTTP 码、哈希、状态）。

### 示例 C：事故响应（P1，先止损）

**用户**：线上 API 502 了，快处理。

**Agent**：

1. `fleet_think`：记录「T0 收到 502 告警，影响 /api/*」。
2. `sysinfo` + `docker_ps` → `api` 容器 Restarting(1)。
3. `docker_logs` → 启动即报 `config.yaml: invalid`。
4. `fleet_reflect(outcome="partial", what_happened="新配置语法错误导致启动失败",
   lessons=["发布前必须 dryRun 校验配置"])`
5. 止损：回滚到上一版配置（备份存在 `config.yaml.bak.T-1`），
   `exec cp` + `docker restart api`，验收：`docker_ps` running + `http_get /health` 200。
6. 若回滚失败 → `fleet_handoff(to="oncall", goal="API 持续 502，需人工介入",
   deadline_s=600)`；记录 thread_id 便于追踪。
7. 通报：现状（已止损/未止损）、已做动作、需要什么、下次更新时间。
8. 复盘：`fleet_reflect` 记录根因、止损有效性、改进项（发布门禁 + 配置校验），
   `fleet_review` 逐条验收。

---

## 9. 反模式（不要这样做）

| 反模式 | 为什么错 | 正确做法 |
|---|---|---|
| 没看日志就重启 | 掩盖根因，可能反复重启 | 先 `docker_logs` / `service_logs` |
| 一次执行 10 条变更命令 | 失败无法定位、无法回滚 | 一次一步，每步验收 |
| 「看起来正常」就宣布完成 | 没有证据 | `fleet_review` 要证据 |
| 同一命令连续重试 5 次 | 放大故障 | 重试 1 次 → 换节点/升级 |
| 把 token 贴进命令/回复 | 泄露 | 用配置/环境变量，回复只说「已配置」 |
| 跨设备不记录链路 | 出问题无法定位哪一跳 | 记录 `mac → nas → …` |
| 远端失败就放弃 | 有备用节点 | caps_index 升级 |
| 用 `exec` 绕过策略 | 安全边界失效 | 换允许方案或请人调整策略 |

---

## 10. 一页纸清单

执行前：

- [ ] 目标可判定（有明确验收标准）？
- [ ] 已用只读工具拿到事实？
- [ ] 计划每步有 goal/tool/assignee/acceptance？
- [ ] 高风险步骤有备份 + 回滚 + 确认点？
- [ ] 目标节点/路径/影响面确认过？

执行后：

- [ ] 每步输出留证据（关键行、exit code、HTTP 码）？
- [ ] `fleet_review` 逐条判定，unknown 说明缺什么证据？
- [ ] `fleet_reflect` 记录结果与教训？
- [ ] 结论包含「已完成 / 未完成 / 下一步 + 责任人」？
- [ ] 有没有把密钥写进任何文本？

---

## 附：相关文件

- `scripts/fleet_agent.py` — 本手册的机器可读实现（instructions / prompts / tools / orchestrator）
- `references/self-dialogue-protocol.md` — 信封 schema、状态机、超时重试、验收标准
- `references/prompt-pack.md` — 可直接粘贴到客户端的系统提示与任务模板
- `SKILL.md` — 产品级用法（人读）
