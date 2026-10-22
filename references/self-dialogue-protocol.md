# Self-Dialogue Protocol · 多 Agent 消息信封与协作协议

> 读者：需要**跨 Agent / 跨设备异步协作**的调用方、协调者 Agent、执行者 Agent。
> 范围：消息信封 JSON schema、kind 语义、状态机、thread/task/deadline、验收标准、
> 超时重试、人机确认点，以及它与**邮箱**、**委派**、**编排器**的关系。
> 机器实现以 `scripts/fleet_agent.py`（v5.2.4）为准：`ENVELOPE_KINDS`、
> `make_envelope()`、`validate_envelope()`、`parse_envelope()`、`encode_envelope()`、
> `envelope_expired()`、`review_acceptance()`、`TaskStore`、`Orchestrator`。
> 配套阅读：`agent-playbook.md`（怎么干活）、`prompt-pack.md`（怎么把规则喂给模型）。

示例 JSON 的字段与约束逐字对照代码，并已通过 `validate_envelope()` 校验；示例统一
使用 `fleet_agent.VERSION`（当前 `"5.2.4"`），时间戳只是「某次运行时刻」。

**章节导航**：§0 速查 · §1 传输层 · §2 信封 schema（强校验字段） · §3 各 kind 的
payload 约定 · §4 状态机 · §5 thread/task/deadline/验收 · §6 错误与重试 ·
§7 人机确认点 · §8 与邮箱/委派/编排器的关系 · §9 三个完整示例 · §10 反模式 ·
附录（代码映射与测试）。

---

## 0. 30 秒速查

| 你手里有什么 | 用哪个 kind | 谁发 |
|---|---|---|
| 一个需要对方执行并验收的任务 | `handoff`（委派）或 `propose`（协商） | 发起方 |
| 同意任务、给出预计完成时间 | `accept` | 接收方 |
| 不同意目标/截止/验收，提替代方案 | `counter` | 接收方 |
| 阶段性进展（不必等任务结束） | `progress` | 接收方 |
| 任务完成、附逐条验收证据 | `result` | 接收方 |
| 做不了、失败、超时、缺权限 | `fail` | 任一方 |
| 对 result 的验收结论 | `review` | 发起方/验收方 |
| 自我介绍、能力通报（不进入任务状态机） | `hello` | 任一方 |

九个 kind 的**唯一权威来源**是 `fleet_agent.ENVELOPE_KINDS`：

```python
ENVELOPE_KINDS = ("propose", "accept", "counter", "progress", "result", "fail",
                  "review", "handoff", "hello")
```

三条铁律：

1. **一条消息一个 JSON 对象**，放进邮箱 `msg_send` 的 `text` 字段（单行 JSON）。
2. **同一会话共享 `thread_id`**；用 `task_id` 挂到 `TaskStore` 的任务日志上。
3. **没有证据就不算通过**：`result` 的每条证据要能对上 `acceptance`，`review` 才给 `pass`。

---

## 1. 传输层：信封躺在邮箱的 `text` 里

信封本身不负责投递。投递由邮箱工具完成（`mcp_agent.py` 的 `msg_send` / `msg_recv`，
底层是 `fleet_proto` 的 store-and-forward 信箱：inbox / outbox / sent）。

```text
发送方                                         接收方
  make_envelope(...)                            msg_recv(me="nas", topic="fleet-task")
        │                                              │
  encode_envelope(env)  → 单行 JSON 字符串              │
        │                                              │
  msg_send(from=…, to=…, text=<单行 JSON>,      →  邮箱（在线直投/离线 outbox）
           topic="fleet-task")                         │
                                                       ▼
                                              剥离邮箱头 → parse_envelope(body)
                                                       │
                                    ┌──────────────────┼───────────────────┐
                                    ▼                  ▼                   ▼
                              合法信封 dict      kind="text"          kind="invalid"
                              （按 kind 处理）   （旧版纯文本，兼容）   （带 error，见 §6）
```

要点（全部可在代码里核对）：

- `encode_envelope()` 输出**单行** JSON（`sort_keys=True`、`separators=(",", ":")`、
  `ensure_ascii=False`），适合直接塞进 `text`。
- `msg_send` 的 `text` 上限是 **64000 字符**（`mcp_agent._MAIL_MAX_TEXT`），超长会被拒绝；
  大段日志不要塞信封，写 `evidence` 摘要或先落盘再给路径。
- `msg_recv` 返回的文本带邮箱头，形如 `#17 [2026-10-22 00:54:21] mac -> nas:\n{...}`；
  `parse_envelope()` 只识别**以 `{` 开头**的正文，所以收信后要先去掉头：

  ```python
  for block in raw.split("\n\n"):          # 一次可能收到多条
      header, _, body = block.partition("\n")
      env = parse_envelope(body)           # 或 parse_envelope(block) 后自行容错
  ```

- `parse_envelope()` 有三种结果，**普通文本永远不会被误判成协议错误**：
  - 合法信封 → 原 dict（含 `kind` = 九个之一）；
  - 普通文本 / 非信封 JSON → `{"kind": "text", "text": 原文, "raw": 原文}`；
  - 像信封但非法 → `{"kind": "invalid", "error": 中文原因, "raw": 原文}`。
    “像信封”的判定：`kind` 命中九种之一，或出现 `v/thread_id/payload/deadline_ms/task_id` 任一字段。
- 邮箱层对**旧版纯文本消息**保持兼容：对端不认信封时，`text` 直接写人话即可，不强制升级。
- 信封的机密性由邮箱传输层负责：`fleet_proto` 对邮箱消息的 `text` 字段做 `enc1$`
  对称加密（密钥由节点 `auth_token` 经 PBKDF2 派生），读回时透明解密，旧明文数据原样返回；
  入站请求另有 Ed25519 签名校验（`fleet_core.verify_request`）。信封内部**不要**再放
  token、私钥、邀请码——`fleet_agent.redact()` 只在日志/异常文本里兜底。

> **广播语义**：`to="*"` 表示广播。信封层（`validate_envelope`）允许；邮箱层
> `msg_send` 的 `to` 也接受 `*`（投递给所有已配对节点，收件方 `msg_recv` 会收到
> `to="*"` 的消息）。广播只适合 `hello`/通知这类无需逐个验收的消息；任务类信封
> 建议**逐节点发送**，各自 `thread_id`，便于追踪与超时升级。

---

## 2. 信封 schema：代码强校验的字段

### 2.1 JSON Schema（强校验部分）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MCP Fleet self-dialogue envelope",
  "type": "object",
  "required": ["v", "kind", "thread_id", "from", "to", "payload"],
  "properties": {
    "v":         {"type": "string", "minLength": 1,
                  "description": "协议版本，主版本号必须 >= 5（当前实现门槛）"},
    "kind":      {"type": "string",
                  "enum": ["propose", "accept", "counter", "progress",
                           "result", "fail", "review", "handoff", "hello"]},
    "thread_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"},
    "from":      {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,32}$"},
    "to":        {"type": "string", "pattern": "^(\\*|[A-Za-z0-9_.-]{1,32})$"},
    "payload":   {"type": "object", "description": "结构化正文；字段约定见 §3"},
    "task_id":   {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$",
                  "description": "可选；关联 TaskStore 任务，且不允许包含 .."},
    "deadline_ms": {"type": "number", "exclusiveMinimum": 0,
                    "description": "可选；Unix 毫秒绝对截止时间"},
    "deadline_s":  {"type": "integer", "minimum": 1,
                    "description": "可选；make_envelope 输入相对秒数时回填"},
    "created_ms":  {"type": "number",
                    "description": "make_envelope 自动写入；校验不强制"}
  },
  "additionalProperties": true
}
```

> 说明：`validate_envelope()` 只检查上表字段，**未知顶层字段被忽略**（不报错）；
> 但 `make_envelope()` 只会生成上述字段，手写信封请勿携带密钥类字段。

### 2.2 逐字段校验规则（与 `validate_envelope` 一一对应）

| 字段 | 必填 | 规则 | 失败时的错误信息（会直接给模型看） |
|---|---|---|---|
| `v` | 是 | 字符串；`主版本号 = v.split(".")[0]` 必须是数字且 `>= 5` | `缺少协议版本字段 v（应为 "5.2.4"）` / `协议版本不支持: '4.9.0'（本节点要求主版本 >= 5，当前 5.2.4）` |
| `kind` | 是 | 必须是九种之一 | `缺少 kind 字段（应为 …）` / `非法信封 kind 'bogus'：必须是 … 之一` |
| `thread_id` | 是 | 字符串，`^[A-Za-z0-9_.-]{1,64}$` | `缺少 thread_id（…）` / `非法 thread_id '…'：只允许 1-64 位…` |
| `from` | 是 | 字符串，`^[A-Za-z0-9_.-]{1,32}$`，不允许 `*` | `非法 from '…'：只允许 1-32 位…` |
| `to` | 是 | 同上，但允许 `*` 表示广播 | `非法 to '…'：只允许 1-32 位… 或 *` |
| `payload` | 是 | 必须是 JSON 对象（dict），**不能是数组/字符串** | `payload 必须是 dict（结构化正文）` |
| `task_id` | 否 | 字符串，1-64 位，且**不允许包含 `..`** | `非法 task_id '…'：只允许 1-64 位…` |
| `deadline_ms` | 否 | 数字（`bool` 不算数字）、`> 0` | `deadline_ms 必须是数字（Unix 毫秒时间戳）` / `deadline_ms 必须 > 0` |

实测边界（同一套规则）：

| 输入 | 结果 |
|---|---|
| `v="5"` / `v="5.2.4"` / `v="5.2.4"` / `v="5.9.0"` | 通过（只比较主版本，不设上限） |
| `v="4.9.0"` / `v="v5.2"` | 拒绝（主版本 < 5） |
| `to="*"` | 通过（广播语义，见 §1） |
| `deadline_ms=true` / `deadline_ms=0` | 拒绝 |
| `task_id="a..b"` | 拒绝 |
| 缺 `created_ms` | 通过（校验不强制，只是 `make_envelope` 会写） |

### 2.3 `make_envelope()` 的默认值与推导

```python
make_envelope(kind, thread_id, from_, to, payload,
              task_id=None, deadline_s=None)
```

- `thread_id` 为空 → 自动生成 `th-<8 位 uuid hex>`（例如 `th-9c41d0ab`）。
- `deadline_s` 给定 → `deadline_ms = created_ms + int(deadline_s) * 1000`，
  同时回填 `deadline_s`；`deadline_s <= 0` 或无法转整数 → `ValueError`
  （浮点会被截断：`300.9 → 300`）。
- 构造完会**自校验**一次：任何字段非法直接在发送前抛 `ValueError`，不会带病出门。
- 典型用法：

  ```python
  env = make_envelope("handoff", None, "mac", "nas",
                      {"goal": "…", "acceptance": ["…"]},
                      task_id="t-20261022-nginx", deadline_s=600)
  text = encode_envelope(env)          # 单行 JSON，放进 msg_send.text
  ```

- **版本号取值**：`v` 由 `make_envelope()` 自动填 `fleet_agent.VERSION`（当前 `"5.2.4"`），
  校验门槛是主版本号 `>= 5`（接受 5.x / 6.x / 7.x 等更高版本）。手写信封时直接取
  `VERSION` 即可，不要写死旧版本号；低于 5 的版本会被拒绝并提示
  「本节点要求主版本 >= 5」。

### 2.4 超时判定 `envelope_expired(env, at_ms=None)`

- 有 `deadline_ms` 且 `at_ms > deadline_ms` → `True`（**严格大于**；恰好等于不算过期）。
- 无 `deadline_ms` → 永远 `False`（不会过期）。
- `deadline_ms` 非数字或为 `bool` → 按「无 deadline」处理，返回 `False`。
- **代码不会自动检查超时**：收信方执行变更前应主动调用，或在业务里做同等判断。

---

## 3. 各 kind 的 payload 约定（协议约定，代码只保证是 dict）

`payload` 的结构由业务约定，`validate_envelope()` 不做字段级校验。下表是**推荐字段**，
其中 `handoff` 的 `goal / acceptance / note / context` 由 `fleet_handoff` 工具自动填充，
其余由 Agent 按约定填写。字段名统一用 `snake_case`。

### 3.1 发起类：`handoff` / `propose`

| 字段 | 类型 | 必填（约定） | 说明 |
|---|---|---|---|
| `goal` | string | 是 | 要达成的**可验证**结果，一句话 |
| `acceptance` | string \| string[] \| object[] | 是 | 验收标准；建议数组，逐条可判定（见 §5.4） |
| `plan` | object[] | 否 | 预期步骤 `{goal, tool, assignee, acceptance, args}`，与 `fleet_plan` 同构 |
| `note` | string | 否 | 背景/现象/约束，`fleet_handoff` 会自动填入 |
| `context` | string | 否 | 关联信息（task_id、旧 thread_id、工单号等） |
| `priority` | `low`/`normal`/`high` | 否 | 与 `request_assist` 的优先级枚举保持一致 |
| `deadline_s` | integer | 否 | 仅供人读的相对截止；机器判定用信封的 `deadline_ms` |

- `handoff` = **委派**（对方要做并回结果）；`propose` = **协商**（先谈目标/验收再动手）。
- 两者都可以被 `accept` / `counter` / `fail` 响应；短任务可 `accept` 后直接 `result`。

### 3.2 响应类：`accept` / `counter`

| kind | 字段 | 类型 | 说明 |
|---|---|---|---|
| `accept` | `accepted` | bool | 固定 `true`（拒绝请用 `fail`） |
| | `eta_s` | integer | 预计完成秒数，便于发起方判断是否超时 |
| | `note` | string | 执行前置条件、会影响范围 |
| `counter` | `reason` | string | 为什么不能按原方案做（如磁盘不足） |
| | `proposal` | object | 修订版 `{goal, acceptance, deadline_s}` |
| | `alternatives` | string[] | 备选方案（换节点/改窗口/改增量） |

### 3.3 过程类：`progress`

| 字段 | 类型 | 说明 |
|---|---|---|
| `pct` | integer 0-100 | 粗略进度；不确定就省略，不要编 |
| `stage` | string | 当前阶段：`observe` / `backup` / `apply` / `restart` / `verify` |
| `message` | string | 一句话进展 |
| `evidence` | string[] | 关键证据行（命令输出摘要，≤300 字符/条） |

### 3.4 结论类：`result` / `fail`

| kind | 字段 | 类型 | 说明 |
|---|---|---|---|
| `result` | `status` | `success`/`partial` | 与 `fleet_reflect.outcome` 的取值风格一致；失败请用 `fail` |
| | `summary` | string | 结论一句话（现象/动作/结果） |
| | `evidence` | object[] | **必须**能对上 acceptance：`{criterion, status, detail}` |
| | `outputs` | object[] | 可选：`{step, tool, node, ok, detail}`（编排器轨迹精简版） |
| `fail` | `reason` | string | 人话原因 |
| | `error_class` | `timeout`/`permission`/`network`/`denied`/`protocol`/`other` | 便于分层重试 |
| | `attempts` | integer | 已尝试次数（含首次） |
| | `need` | `human`/`credentials`/`alternate_node`/`nothing` | 需要对方提供什么 |
| | `evidence` | string[] | 失败证据（错误行、exit code） |

> `result.evidence[].status` 只认 `passed` / `failed` / `unknown` 的语义
> （`review_acceptance()` 还接受 `ok`、`passed` 布尔以及 `status/result/verdict/outcome`
> 字符串）。`unknown` 表示「有证据但无法判定」，缺证据请直接不写该条，由 review 判 `unknown`。

### 3.5 验收类：`review`

| 字段 | 类型 | 说明 |
|---|---|---|
| `verdict` | `pass`/`fail`/`inconclusive` | 与 `review_acceptance()["verdict"]` 完全一致 |
| `summary` | string | 形如 `验收 3 条：通过 2 / 失败 1 / 无法判定 0 → fail` |
| `items` | object[] | 每条 `{criterion, status, evidence[], reason}`，status ∈ `passed/failed/unknown` |

### 3.6 介绍类：`hello`

| 字段 | 类型 | 说明 |
|---|---|---|
| `node` | string | 节点名 |
| `version` | string | 节点/fleet 版本 |
| `caps` | string[] | 能力清单（`docker`/`gpu`/`tailscale`…） |
| `note` | string | 可选 |

`hello` 不进入任务状态机、不需要回复，仅用于握手与能力通报。

---

## 4. 状态机

> **重要前提**：`validate_envelope()` 只保证**单条信封合法**，不强制转移顺序；
> 状态机是**协议约定**，供 Agent、人类和后续实现共同遵守。代码里真正给出确定性
> 信号的是这些地方：`fleet_think` 的 `next_action`、`fleet_reflect` 的
> `suggested_tool`/`escalate`、`Orchestrator` 的 `status`、`review_acceptance()` 的 `verdict`。

### 4.1 主流程（happy path）

```text
        [发起方]                                   [接收方]
           │                                          │
   idle ───┤ ① handoff / propose                      │
           │   （thread_id, task_id, deadline_s）      │
           │ ────────────────────────────────────────▶│ proposed
           │                                          │
           │ ② accept {accepted:true, eta_s}           │
           │ ◀────────────────────────────────────────│ accepted
           │                                          │
           │ ③ progress {pct, stage, evidence}         │
           │ ◀────────────────────────────────────────│ in_progress
           │            （可 0..N 条，同 thread）       │
           │                                          │
           │ ④ result {status, summary, evidence[]}    │
           │ ◀────────────────────────────────────────│ resulted
           │                                          │
   reviewed ─┤ ⑤ review {verdict:"pass", items[]}      │
   (done)    │ ────────────────────────────────────────▶│
             │                                          │
             └──────────── 任务关闭，TaskStore 追加 review 事件 ───────┘
```

短任务允许 `accept → result`（跳过 progress）；不允许 `propose → result`（没承诺就交付，
发起方应判 `inconclusive` 并要求补 `accept`）。

### 4.2 协商与拒绝分支

```text
                              ┌──────────────── counter {reason, proposal} ────────────┐
                              │                                                        │
  proposed ──┬── accept ──▶ accepted ──▶ progress* ──▶ result ──▶ review(pass) ──▶ done │
             │                                                                         │
             ├── counter ◀──────────────────────────────────────────────────────────────┘
             │      │
             │      ├── 发起方 propose（修订目标/验收/deadline）──▶ proposed
             │      ├── 发起方 accept（接受对方提案）──────────────▶ accepted
             │      └── 发起方 fail（谈不拢，error_class="other"）──▶ failed
             │
             └── fail ◀── 接收方做不了（超时/权限/依赖缺失）──────── ▶ failed
```

### 4.3 失败、超时与重试

```text
  accepted / in_progress
        │
        ├─ 执行失败 ──▶ fail {error_class:"permission"|"network"|"denied", need:"human"}
        │                     │
        │                     ├─(A) 同 thread 重试：发起方 propose（只改超时/参数/窗口）
        │                     │        └─▶ proposed ──▶ accept ──▶ …
        │                     ├─(B) 换节点：新 thread 新 handoff；
        │                     │        旧 thread 记 fail + review(inconclusive)，note 引用旧 thread
        │                     └─(C) 交人：handoff(to=oncall) 或 review(inconclusive) + 停止
        │
        └─ deadline_ms 到期（envelope_expired(env)==True）
              │
              ├─ 发起方：发 fail {error_class:"timeout"} 收尾（同 thread）
              ├─ 发起方：fleet_reflect(outcome="partial"/"fail") 记录阻塞点
              └─ 升级：换备用节点（caps_index 下一个）或 fleet_handoff 交人
```

**超时不等于失败**：先确认「对方是否还在执行」（`msg_recv(wait=…)` 或 `progress`），
再决定补发 deadline、重试还是升级；不要因为一次没回就并发重发多条 handoff。

### 4.4 各状态的合法来件（约定，供实现方校验）

| 当前状态 | 允许来件 | 处理 |
|---|---|---|
| `proposed` | `accept` / `counter` / `fail` | 分别进入 accepted / 协商 / failed |
| `accepted` | `progress` / `result` / `fail` | 进入 in_progress / resulted / failed |
| `in_progress` | `progress` / `result` / `fail` | 同上（progress 可多条） |
| `resulted` | `review` / `fail`（补交失败说明） | 进入 done 或 failed |
| `reviewed(done)` | `hello` | 线程关闭，新需求开新 thread |
| `failed` | `propose`（同 thread 重试）或新 `handoff`（换节点） | 进入新一轮 |
| `counter` 等待中 | `propose` / `accept` / `fail` | 见 4.2 |

未定义的「取消」语义：协议没有专门的 cancel kind；需要取消时发 `fail`，
在 `reason` 写 `cancelled by <who>: <why>`，并用 `error_class="other"`。

---

## 5. thread_id / task_id / deadline / 验收标准

### 5.1 thread_id：一次协作 = 一个线程

- 生成：`make_envelope(thread_id=None, …)` 自动 `th-` + 8 位 hex；
  也可自定义（如 `th-nginx-20261022`），合法字符 `[A-Za-z0-9_.-]`，1-64 位。
- 规则：**同一轮协作的所有消息共享同一 `thread_id`**；回复必须原样回填。
- 新线程的场景：换节点重试、升级给人、另起一个独立任务。
  旧线程不要复用，把旧 `thread_id` 写进新信封的 `payload.note`/`context`（见示例 B5）。
- 一个 thread 只对应一个 `goal`；目标变了就 `counter` 或新 thread，不要中途偷换。

### 5.2 task_id：挂到任务日志（TaskStore）

- `TaskStore` 落盘结构（目录 0700、文件 0600、原子写）：

  ```text
  <base_dir>/tasks/<task_id>.json          任务快照（status/goal/created_ms/event_count…）
  <base_dir>/tasks/<task_id>.events.jsonl  追加事件（created/think/plan/step/reflect/review/handoff/finish）
  ```

- `task_id` 规则：1-64 位 `[A-Za-z0-9_.-]`，**不允许 `..`**（阻断 `../` 路径穿越）。
- 自动生成格式：`t-<UTC 时间 %Y%m%d-%H%M%S>-<8 位 hex>`，便于排序且不碰撞。
- 推荐：`handoff`/`propose` 就带上 `task_id`；对端执行时用同一个 `task_id` 调
  `fleet_think/plan/reflect/review`，双方证据落在同一任务日志里，便于事后审计。
- 任务状态（TaskStore `status`）：`open` / `running` / `completed` / `partial` /
  `failed` / `awaiting_approval`。

### 5.3 deadline：绝对时间，别用「多久没回」猜

- 信封字段是 `deadline_ms`（Unix 毫秒，绝对值）；`deadline_s` 只是 `make_envelope`
  的输入与回填，便于人读。
- 约定：`handoff/propose` 的 deadline 是**整轮协作的 SLA**；`accept` 用 `eta_s` 表态，
  发起方据此判断是否即将超时。
- 判定函数 `envelope_expired(env, at_ms=None)`：严格 `>`；无 deadline 不过期。
- 代码不自动计时：Agent 在处理消息时应先 `envelope_expired` 再执行变更；
  编排器另有整体预算 `max_seconds`（默认 120s）与 `max_steps`（默认 8）。

### 5.4 acceptance：可判定的验收标准

- 反例：「看看是否正常」「应该没问题」——不可判定，`review` 只会给 `unknown`。
- 正例：「`docker_ps` 中 nginx 状态为 running」「HTTP 返回 200」「exit=0 且输出含 active」
  「`df -h` 可用空间 > 10%」。
- 支持三种写法（`review_acceptance` 的 `_criteria_list`）：字符串、字符串数组、
  对象数组（`{"criterion": "...", "id": "..."}`）；单个对象也接受。
- 判定结果逐条为 `passed` / `failed` / `unknown`，整体 `verdict`：
  - 任一 `failed` → `fail`；
  - 全部 `passed` → `pass`；
  - 其余（含无证据/证据不明确）→ `inconclusive`。
- **无证据不会判 pass**：`fleet_review` 在没有证据时所有条目为 `unknown`，
  整体 `inconclusive`。`unknown` 必须说明缺什么证据（`reason` 字段）。
- 证据匹配顺序（代码行为）：按 `criterion` 精确归一化匹配 → 子串匹配 →
  `goal` 语义回退 → 位置回退（第 i 条标准对第 i 条无归属证据）→ `criterion_id` 匹配。
  因此**保持 `acceptance` 文案与 `result.evidence[].criterion` 一致**最稳。

---

## 6. 错误与重试策略

失败分四层处理，层与层不互相替代：

| 层 | 触发 | 处理 | 上限 |
|---|---|---|---|
| 协议层 | `parse_envelope` 返回 `invalid`、版本不匹配 | 修字段后重发；版本旧则退回纯文本或升级对端 | 不自动重试（同样字节不会变合法） |
| 语义层 | 工具返回 `ok=false` / 超时 / exit≠0 | 同语义重试，只允许改超时/参数/分批大小 | **1 次**（`Orchestrator.max_retries=1` → 共 2 次尝试） |
| 投递层 | 对端离线、HTTP 失败 | 邮箱 outbox 自动 store-and-forward 重投，**不要手工重发** | 250 次后标 `dead`（见下） |
| 人工层 | 策略 `ask` / 需要凭据 / 权限不足 | 停下来请人确认，不换工具绕过 | 由人决定 |

### 6.1 协议层：拒绝要回得清楚

收到 `kind="invalid"` 时，不要猜、不要静默丢弃。约定回复一个 `fail`：

```json
{"v": "5.2.4", "kind": "fail", "thread_id": "th-bad00001",
 "from": "nas", "to": "mac",
 "payload": {"reason": "非法 thread_id '../x'：只允许 1-64 位字母/数字/_.-",
             "error_class": "protocol", "attempts": 1, "need": "nothing"}}
```

常见错误信息与修法：

| 错误信息（原文） | 根因 | 修法 |
|---|---|---|
| `缺少协议版本字段 v（应为 "5.2.4"）` | 漏了 `v` | 补 `"v": "5.2.4"`（即 `fleet_agent.VERSION`） |
| `协议版本不支持: '4.9.0'（本节点要求主版本 >= 5，当前 5.2.4）` | 旧版信封/旧客户端 | 升级，或改发纯文本消息 |
| `非法信封 kind 'bogus'：必须是 … 之一` | kind 拼错/自创 | 只用九种 kind；自定义语义放 payload |
| `payload 必须是 dict（结构化正文）` | payload 写成数组 | 包成对象，如 `{"items":[…]}` |
| `非法 task_id 'a..b'…` | 含 `..` | 换成普通 ID（`t-20261022-nginx`） |
| `deadline_ms 必须是数字…` / `必须 > 0` | 传了 `true`/字符串/0 | 用绝对毫秒，如 `1792602452600` |

### 6.2 投递层：邮箱自动重试（无需人工干预）

`fleet_proto` 的 outbox 按失败次数退避重投：

| 已失败次数 | 下次重试间隔 |
|---|---|
| 0–4 | 每 15s |
| 5–9 | 每 60s |
| 10–14 | 每 240s |
| 15–19 | 每 960s |
| 20+ | 每 3600s（封顶） |

达到 `MAX_RETRY_ATTEMPTS = 250`（约 2 天）标 `status=dead` 停止重试（保留审计）。
所以「对端离线」不是协议失败，**不要**在 outbox 重试期间手工再发同样内容——
会造成重复执行；利用同一 `thread_id` 做幂等判断（见 6.3）。

### 6.3 幂等：同 thread 重发要能被识别

协议没有内置消息序号/去重字段。约定：

- 同 `thread_id` + 同 `kind` + 同 `payload.goal` 视为重发，收方先回 `progress`
  说明「已在执行」，而不是再执行一遍；
- 变更类动作优先设计成幂等命令（`systemctl restart`、`docker restart`、
  「确保配置文件内容为 X」），避免「追加一行」这类不可重复操作；
- 邮箱层已有 `msg_id` 去重与 read-cursor，同一封邮件不会被重复消费，
  但**手工重发的新邮件**不受此保护。

### 6.4 语义层：重试 1 次就换策略

- 只允许改：超时、重试间隔、分批大小、目标节点；**不允许**改验收标准或目标。
- 重试仍失败 → 收集证据 → `fail`（`attempts=2`）→ 发起方 `fleet_reflect`
  记录阻塞点 → 换备用节点或交人。
- 备用节点选择：`caps_index` 中该能力的**其他节点**（按列表顺序逐个尝试，成功即止）；
  编排器会在成功的备用步骤上标 `escalated_from=<原节点>`（`Orchestrator._alternate_nodes`）。

---

## 7. 人机确认点

系统里有**三道**确认闸门，语义一致：`allow` 直接做、`ask` 停下来等人、`deny` 拒绝。
判定异常时：编排层 **fail-closed 按 deny**（`Orchestrator._check_policy`）；
工具层保持 v5 行为（`mcp_agent.call_tool` 策略异常时放行）——这是有意的分层差异。

### 7.1 闸门一：工具层（`ExecPolicy`，`mcp_agent.call_tool`）

- 默认 `deny` 规则（`fleet_core.DEFAULT_DENY_RULES`）：`rm -rf /`、`--no-preserve-root`、
  `mkfs`、`dd of=/dev/*`、重定向写块设备、fork bomb、`chmod 777 /`、`wipefs`、
  `shred /dev/*`。
- 默认 `ask` 规则（`DEFAULT_ASK_RULES`）：关机/重启、`systemctl reboot`、`rm -r`、
  `curl | sh`、`sudo`、包管理器变更、`pip install`、`docker run/rm/stop/prune`、
  `git push/reset --hard/clean -f`、防火墙变更、写 `/etc/`、`crontab`、账号变更等。
- `ask` 的返回：`isError=true`，文本为
  「该操作需要用户确认：<原因>（规则 <id>）。请先向用户说明影响；获得明确同意后，
  带 confirm=true 重新调用。」
- **正确姿势**：先向人展示「做什么 / 影响面 / 回滚方案」，拿到明确同意后再带
  `confirm=true` 重调；**不要**用 `exec` 绕过被 `deny` 的专用工具。

### 7.2 闸门二：编排层（`Orchestrator`）

- 策略命中 `ask` → 该步 `status="awaiting_approval"`、**不执行**，返回：

  ```json
  {"status": "awaiting_approval",
   "pending_approval": {"tool": "exec", "node": "nas",
                        "args": {"command": "docker restart nginx"},
                        "reason": "命中策略 ask: peer_call",
                        "task_id": "t-20261022-nginx"}}
  ```

- 任务快照同步为 `awaiting_approval`；人工确认后调 `Orchestrator.resume(task_id)`：
  已成功的步骤标 `skipped`，从待确认步骤继续（测试 `test_resume_continues_after_approval`）。
- 策略命中 `deny` → 该步 `denied`，任务 `failed`，不执行任何工具。
- 远端步骤会**同时**检查逻辑工具名与传输层 `peer_call`，取更严格的结果
  （防止 `{"ask":["peer_call"]}` 被逻辑工具名绕过）。

### 7.3 闸门三：委派层（`fleet_handoff`）

- 命中 `ask` → 返回 `isError=false`（不是错误），`structuredContent` 示意如下
  （`envelope` 为 §2 的完整信封对象，`envelope_text` 为其单行 JSON 字符串）：

  ```text
  {"policy": "ask", "status": "awaiting_approval", "requires_approval": true,
   "reason": "命中 ask 策略", "envelope": {...}, "envelope_text": "...",
   "sent": false,
   "next_steps": ["展示批准理由并征得确认",
                  "确认后再次调用 fleet_handoff(send=true)",
                  "或用 policy 放行 msg_send"]}
  ```

  此时**没有发送**；人工同意后再次调用并保持 `send=true` 即可投递。
- 命中 `deny` → `isError=true`，信封不发送，也不写 handoff 事件。
- 未注入 `FleetClient` 时只生成信封（`sent=false`），适合人工复制投递。

### 7.4 只读节点

只读模式在 `mcp_agent.call_tool` 的**分发之前**拦截（v5.2 起 `fleet_*` 工具也先过只读
检查），`READ_ONLY_BLOCKED` 覆盖 `exec/write/edit/upload/systemctl/docker_restart/kill/
exec_async`、`msg_send`、`peer_call/peer_add/peer_remove/delegate/group_*`，以及
`fleet_handoff`、`fleet_token_rotate`。

- `fleet_think / fleet_plan / fleet_reflect / fleet_review` 仍可用：它们只写本机任务日志
  （0600），不产生外部副作用。
- 只读节点**无法主动发起或回复跨节点消息**（`msg_send`/`peer_call`/`fleet_handoff` 都被
  禁用）：变更需求由人工转达，或由发起方在超时后按 §6 升级；不要用观察类工具变相执行。
- 被要求变更时的正确输出：在本地任务日志写清「只读节点 / 需要的变更 / 建议的有权节点」，
  交给人类或上游协调者处理。

## 8. 与邮箱、委派、编排器的关系

### 8.1 五层各司其职

| 层 | 实现 | 同步/异步 | 职责 | 证据落点 |
|---|---|---|---|---|
| 邮箱 | `msg_send`/`msg_recv` + `fleet_proto` Mailbox | 异步 store-and-forward | 传输任意文本（含信封） | `inbox/outbox/sent.jsonl` |
| 信封 | `make/validate/parse/encode_envelope` | — | 结构化语义、可校验 | 随邮箱消息保存 |
| 委派 | `fleet_handoff` → `handoff` 信封；`peer_call`/`delegate` | 异步 / 同步 | 把子任务交给别的节点/Agent | `TaskStore` handoff 事件 + 邮箱 |
| 编排 | `Orchestrator` + `TaskStore` | 同步（本进程循环） | 单节点确定性 观察→计划→执行→验证→反思 | `<task_id>.json` + `.events.jsonl` |
| LLM | `LLMAdapter`（可选） | 同步 HTTP | 仅当任务没有显式 steps 时生成计划 | 不落盘，失败降级并在 `reflections` 记录 |

### 8.2 一次跨 Agent 协作的完整时序

```text
人类/调用方        mac（协调者）                 nas（执行者）            邮箱/TaskStore
    │                  │                              │                       │
    │ run(goal)        │                              │                       │
    │─────────────────▶│ Orchestrator：sysinfo 观察    │                       │
    │                  │ fleet_think / fleet_plan ─────┼──────────────────────▶│ events: think/plan
    │                  │ fleet_handoff(to=nas)         │                       │
    │                  │  ── handoff 信封 ─────────────┼── msg_send ──────────▶│ outbox/inbox
    │                  │                              │ msg_recv → parse      │
    │                  │                              │ accept 信封 ─────────▶│
    │                  │                              │ progress* ───────────▶│
    │                  │                              │ result + evidence ───▶│
    │                  │ msg_recv → parse result       │                       │
    │                  │ fleet_review(acceptance) ─────┼──────────────────────▶│ events: review
    │                  │  review 信封 ─────────────────┼──────────────────────▶│
    │◀─────────────────│ 最终汇报（结论/证据/下一步）   │                       │
```

### 8.3 编排器 vs 信封：什么时候用哪个

- **同步**（本机或 `peer_call` 能直接拿到返回）：用 `Orchestrator` / `peer_call`，
  不需要信封；编排器把每步写入 `TaskStore`，返回
  `{"task_id","status","steps","result":{"summary","outputs","review"},
  "reflections","delegated","plan","plan_source","review","limited","pending_approval"}`。
- **异步**（对方可能离线、任务超过一次请求时长、需要多轮协商/验收）：用
  `fleet_handoff` + 信封 + 邮箱；结果到达后用 `fleet_review` 验收。
- **混合**：先用编排器跑同步诊断，再把「需要人/别的节点」的部分 `handoff` 出去；
  两者用**同一个 `task_id`**，事件日志会同时出现 `step`、`handoff`、`review`，叙事完整。

`TaskStore` 事件与协议动作的对应关系：

| 事件 `type` | 写入者 | 对应协议动作 |
|---|---|---|
| `created` | `TaskStore.create_task` | 任务建立（工具自动创建或 `run()`） |
| `think` | `fleet_think` | 观察/判断留痕（Observe） |
| `plan` | `fleet_plan` / `Orchestrator` | 计划（Plan） |
| `step` | `Orchestrator` | 同步执行一步（Act） |
| `reflect` | `fleet_reflect` | 反思/教训（Reflect） |
| `review` | `fleet_review` / 编排器收尾 | 验收结论（对应 review 信封） |
| `handoff` | `fleet_handoff` | 委派发出（对应 handoff 信封） |
| `finish` | `Orchestrator` | 编排循环结束（status/limited） |

---

## 9. 三个完整示例

> 示例均由 `make_envelope()` 生成；`created_ms/deadline_ms` 为示例运行时刻。
> 底层投递是 `msg_send(text=encode_envelope(env))`；若用 `fleet_handoff` 生成，
> 它默认已投递（`send=true`），不要再手工发一次，否则会重复执行。

### 9.1 示例 A：成功（handoff → accept → progress → result → review）

**A1 发起方 mac → 执行方 nas**

```json
{
  "v": "5.2.4", "kind": "handoff", "thread_id": "th-9c41d0ab",
  "from": "mac", "to": "nas",
  "payload": {
    "goal": "恢复 nas 上 nginx 容器（反复 OOM 重启）",
    "acceptance": [
      "docker_ps 中 nginx 状态 running",
      "60s 内无新 OOMKilled 日志",
      "http://127.0.0.1:8080/health 返回 200"
    ],
    "note": "容器 5 分钟内重启 3 次，日志末尾 OOMKilled",
    "context": "t-20261022-nginx"
  },
  "created_ms": 1792601852600,
  "task_id": "t-20261022-nginx",
  "deadline_ms": 1792602452600, "deadline_s": 600
}
```

**A2 执行方受理（给出 ETA）**

```json
{
  "v": "5.2.4", "kind": "accept", "thread_id": "th-9c41d0ab",
  "from": "nas", "to": "mac",
  "payload": {"accepted": true, "eta_s": 180,
              "note": "先核对内存限制与宿主可用内存，再调整上限"},
  "created_ms": 1792601862000
}
```

**A3 阶段进展（可多条）**

```json
{
  "v": "5.2.4", "kind": "progress", "thread_id": "th-9c41d0ab",
  "from": "nas", "to": "mac",
  "payload": {"pct": 60, "stage": "restart",
              "message": "内存上限 256m → 1g，正在重启容器",
              "evidence": ["docker inspect: Memory=1073741824"]},
  "created_ms": 1792601930000
}
```

**A4 交付结果（证据逐条对上 acceptance）**

```json
{
  "v": "5.2.4", "kind": "result", "thread_id": "th-9c41d0ab",
  "from": "nas", "to": "mac",
  "payload": {
    "status": "success",
    "summary": "内存上限调到 1g 后容器稳定运行 10 分钟，健康检查 200",
    "evidence": [
      {"criterion": "docker_ps 中 nginx 状态 running", "status": "passed",
       "detail": "nginx Up 10 minutes (healthy)"},
      {"criterion": "60s 内无新 OOMKilled 日志", "status": "passed",
       "detail": "docker_logs 末尾无 OOMKilled"},
      {"criterion": "http://127.0.0.1:8080/health 返回 200", "status": "passed",
       "detail": "HTTP 200，耗时 41ms"}
    ]
  },
  "created_ms": 1792602120000
}
```

**A5 验收（verdict 与 review_acceptance 一致）**

```json
{
  "v": "5.2.4", "kind": "review", "thread_id": "th-9c41d0ab",
  "from": "mac", "to": "nas",
  "payload": {
    "verdict": "pass",
    "summary": "验收 3 条：通过 3 / 失败 0 / 无法判定 0 → pass",
    "items": [
      {"criterion": "docker_ps 中 nginx 状态 running", "status": "passed",
       "evidence": ["result.evidence[0]: nginx Up 10 minutes (healthy)"]}
    ]
  },
  "created_ms": 1792602140000
}
```

### 9.2 示例 B：失败（权限不足 → inconclusive → 换节点升级）

**B1 发起方 mac → router**

```json
{
  "v": "5.2.4", "kind": "handoff", "thread_id": "th-2b77e1c4",
  "from": "mac", "to": "router",
  "payload": {
    "goal": "检查 router 上防火墙规则是否误拦 8080",
    "acceptance": ["iptables -S 输出中无 8080 DROP",
                   "net_check(port=8080) 返回 open"],
    "note": "同事反馈手机访问 router:8080 超时"
  },
  "created_ms": 1792601874931
}
```

**B2 受理**

```json
{
  "v": "5.2.4", "kind": "accept", "thread_id": "th-2b77e1c4",
  "from": "router", "to": "mac",
  "payload": {"accepted": true, "eta_s": 120},
  "created_ms": 1792601876000
}
```

**B3 失败（带错误分类与需要什么）**

```json
{
  "v": "5.2.4", "kind": "fail", "thread_id": "th-2b77e1c4",
  "from": "router", "to": "mac",
  "payload": {
    "reason": "执行 iptables 需要 root，当前 agent 无权限",
    "error_class": "permission",
    "attempts": 1,
    "need": "human",
    "evidence": ["exec: iptables -S -> Permission denied (exit=4)"]
  },
  "created_ms": 1792601898000
}
```

**B4 发起方验收：没有可用证据 → inconclusive（不得判 pass）**

```json
{
  "v": "5.2.4", "kind": "review", "thread_id": "th-2b77e1c4",
  "from": "mac", "to": "router",
  "payload": {
    "verdict": "inconclusive",
    "summary": "验收 2 条：通过 0 / 失败 0 / 无法判定 2 → inconclusive",
    "items": [
      {"criterion": "iptables -S 输出中无 8080 DROP", "status": "unknown",
       "evidence": ["fail.evidence[0]: Permission denied"],
       "reason": "缺 root 权限下的 iptables 输出"}
    ]
  },
  "created_ms": 1792601910000
}
```

**B5 换备用节点：新 thread，引用旧 thread**

```json
{
  "v": "5.2.4", "kind": "handoff", "thread_id": "th-5d0a88e2",
  "from": "mac", "to": "router-2",
  "payload": {
    "goal": "在备用节点复查 8080 拦截规则（同能力备用节点）",
    "acceptance": ["sudo 可用时输出 iptables -S",
                   "net_check(port=8080) 返回 open"],
    "note": "原节点 router 权限不足，已记录 thread th-2b77e1c4",
    "context": "t-20261022-fw"
  },
  "created_ms": 1792601925000,
  "task_id": "t-20261022-fw",
  "deadline_ms": 1792602225000, "deadline_s": 300
}
```

### 9.3 示例 C：协商（propose → counter → 修订 propose → accept → result）

**C1 初始提案**

```json
{
  "v": "5.2.4", "kind": "propose", "thread_id": "th-c0ffee31",
  "from": "mac", "to": "nas",
  "payload": {
    "goal": "今晚 23:00 在 nas 执行全量备份到 /volume2/backup",
    "acceptance": ["备份任务 exit=0", "目标目录大小与源目录偏差 < 2%"],
    "note": "预计占用 380GB，窗口 4 小时"
  },
  "created_ms": 1792601874931,
  "task_id": "t-20261022-backup"
}
```

**C2 反提案：说明理由 + 修订目标 + 备选**

```json
{
  "v": "5.2.4", "kind": "counter", "thread_id": "th-c0ffee31",
  "from": "nas", "to": "mac",
  "payload": {
    "reason": "/volume2 只剩 12%，全量备份会写满",
    "proposal": {
      "goal": "先清理 7 天前的增量备份，再执行全量备份",
      "acceptance": ["清理后可用空间 > 450GB", "备份任务 exit=0"],
      "deadline_s": 900
    },
    "alternatives": ["改用增量备份", "改备份到 /volume3"]
  },
  "created_ms": 1792601880000
}
```

**C3 接受修订并顺延 deadline（同 thread）**

```json
{
  "v": "5.2.4", "kind": "propose", "thread_id": "th-c0ffee31",
  "from": "mac", "to": "nas",
  "payload": {
    "goal": "先清理 7 天前的增量备份，再执行全量备份",
    "acceptance": ["清理后可用空间 > 450GB", "备份任务 exit=0"],
    "note": "接受 counter 提案，deadline 顺延 900s"
  },
  "created_ms": 1792601889000,
  "task_id": "t-20261022-backup",
  "deadline_ms": 1792602789000, "deadline_s": 900
}
```

**C4 受理**

```json
{
  "v": "5.2.4", "kind": "accept", "thread_id": "th-c0ffee31",
  "from": "nas", "to": "mac",
  "payload": {"accepted": true, "eta_s": 900,
              "note": "先清理再备份，完成后回 result"},
  "created_ms": 1792601892000
}
```

**C5 交付（证据对上修订后的 acceptance）**

```json
{
  "v": "5.2.4", "kind": "result", "thread_id": "th-c0ffee31",
  "from": "nas", "to": "mac",
  "payload": {
    "status": "success",
    "summary": "清理释放 96GB，全量备份 exit=0，占用 361GB",
    "evidence": [
      {"criterion": "清理后可用空间 > 450GB", "status": "passed",
       "detail": "df -h: /volume2 可用 472GB"},
      {"criterion": "备份任务 exit=0", "status": "passed",
       "detail": "restic backup exit=0，snapshot 8f21c0"}
    ]
  },
  "created_ms": 1792602600000
}
```

---

## 10. 反模式

| 反模式 | 为什么错 | 正确做法 |
|---|---|---|
| 每条消息新开 `thread_id` | 无法把请求/进展/结果串起来 | 一轮协作一个 thread，回复原样回填 |
| 用 `to="*"` 发任务信封 | 广播没有逐节点的 thread 与验收，无法追踪/升级 | 任务类逐节点发；广播只用于 `hello`/通知 |
| 把大段日志塞进 `payload` | 邮箱 `text` 上限 64000，且不可读 | 给摘要/路径；原始日志落盘后用 `read` |
| 收到 `invalid` 就静默丢弃 | 对方永远等不到回应 | 回 `fail(error_class="protocol")` 并带 `error` 原文 |
| 没 `accept` 就 `result` | 没有承诺与 ETA，责任不清 | 先 `accept`，长任务先 `progress` |
| 没有证据就 `review: pass` | 验收形同虚设 | 无证据 → `unknown`，整体 `inconclusive` |
| outbox 重试期间手工重发 | 对端可能重复执行 | 依赖 store-and-forward；同 thread 先回 progress 去重 |
| 超时后并发重发多条 handoff | 放大故障、可能重复变更 | 先确认进度，再补 deadline 或换节点 |
| 用 `exec` 绕过被 deny 的专用工具 | 安全边界失效 | 换允许方案，或请人调整策略 |
| 信封里写 token/邀请码 | 泄露密钥 | 只写「已配置」；需要传递时走配对流程 |

---

## 附：代码映射与测试

| 能力 | 代码位置（fleet_agent.py v5.2.4） |
|---|---|
| kind 白名单 | `ENVELOPE_KINDS` |
| 构造 + 默认值 + 自校验 | `make_envelope()` |
| 结构校验（返回 `(ok, error)`） | `validate_envelope()` |
| 超时判定 | `envelope_expired()` |
| 解析（信封/文本/非法） | `parse_envelope()`、`_looks_like_envelope()` |
| 单行序列化 | `encode_envelope()` |
| 验收判定 | `review_acceptance()`、`_criteria_list()` |
| 任务存储 | `TaskStore`（`create_task/append_event/read_events/update_task/list_tasks`） |
| 编排器状态与审批 | `Orchestrator._final_status()`、`_check_policy()`、`_execute_step()`、`resume()` |
| 委派信封 | `_fleet_handoff()`、`_handoff_policy_verdict()` |
| 自对话工具注册 | `SELF_DIALOGUE_TOOLS`、`SELF_DIALOGUE_FUNCS`、`dispatch_tool()` |
| 邮箱投递 | `mcp_agent.py::t_msg_send/t_msg_recv`、`fleet_proto.Mailbox` |
| 策略默认规则 | `fleet_core.DEFAULT_DENY_RULES` / `DEFAULT_ASK_RULES` / `ExecPolicy` |

测试对照（`tests/test_agent_brain.py`，116 项）：`TestEnvelope`（九种 kind 往返、
缺字段/非法姓名/广播/`payload` 非 dict/非法 task_id、过期、纯文本兼容、非信封 JSON、
非法信封、encode 往返）、`TestOrchestrator`（策略 allow/ask/deny、resume、
重试与升级、预算与 LLM 降级）、`TestSelfDialogueTools`（plan 校验、think/reflect
持久化、review 不臆断、handoff 策略）。

相关文件：

- `scripts/fleet_agent.py` — 本协议的唯一权威实现
- `references/agent-playbook.md` — 工具选择、四步法、失败恢复
- `references/prompt-pack.md` — 可直接粘贴给 Agent 的系统提示与任务模板
- `SKILL.md` — 产品级用法（人读）
