# 自修复（Self-Repair）· MCP Fleet v5.2

> 目标：连接上的 AI Agent 发现节点异常后，**能自己诊断并修复常见故障**，
> 同时有明确边界，不会把“自愈”变成“失控重试/删数据”。

## 1. 三个 MCP 工具

| 工具 | 读/写 | 作用 |
|------|-------|------|
| `fleet_selfcheck` | 只读 | 返回结构化 findings（code/severity/title/detail/action/repairable）与 repair_plan |
| `fleet_repair` | 写（需确认） | 执行一个白名单动作：重启服务 / 修权限 / 清 stale pid / 回滚 |
| `fleet_repair_loop` | 写（需确认） | 有界自愈：自检 → 修可修项 → 复检，默认 2 次、最多 3 次 |

`fleet_selfcheck` 是安全入口：任何 Agent 都可以先调用它；只有确认后才进入写操作。

## 2. 会检测什么

- 配置缺失 / 权限过宽（0600）
- 节点身份缺失（fleet_core/identity.json）
- 未启用 token/签名
- 审计哈希链损坏或无法校验
- 数据目录磁盘不足（<100MB）
- 监听端口不可连
- data/mailbox 权限或不可写
- 残留 pid 文件（进程已退出）
- （可扩展）版本/服务状态

每条 finding 带 `severity`（critical/high/medium）与 `repairable`。

## 3. 允许的修复动作

| action | 行为 | 风险 |
|--------|------|------|
| `restart_service` | 通过安装器生成的 `fleet-ctl.sh restart` 重启（systemd/launchd/Termux/nohup 统一入口） | 短暂断连 |
| `fix_permissions` | config 0600、data/mailbox 0700 | 低 |
| `clear_stale_pid` | 仅当 pid 对应进程已不存在时删除 pid 文件 | 低 |
| `rollback` | 调用 `fleet_install.py --rollback` 恢复最近备份 | 高，必须显式 `allow_rollback=true` |

**禁止**：删除数据/邮箱、轮换 token、修改代码、执行任意 shell、递归重试。

## 4. Agent 使用方式（内置提示词）

`fleet prompt self_repair` 或 `fleet menu` → [18] 可直接复制：

```text
你是 MCP Fleet 节点上的运维 agent。先调用 fleet_selfcheck 获取结构化发现；
如果存在可修复项，调用 fleet_repair_loop(confirm=<节点名>, max_attempts=2)，
只允许它做重启/权限/pid 修复；失败立即停止，把 remaining findings 原样报告，
禁止循环重试、禁止执行未列出的命令、禁止删除数据或轮换 token。
```

工具调用示例：

```json
{"name":"fleet_selfcheck","arguments":{}}
{"name":"fleet_repair_loop","arguments":{"confirm":"my-nas","max_attempts":2}}
{"name":"fleet_repair","arguments":{"action":"restart_service","confirm":"my-nas"}}
```

## 5. CLI

```bash
fleet selfcheck                 # 只读自检 + 修复计划
fleet repair                    # 默认只展示计划，不执行
fleet repair --auto             # 有界自愈（最多 2 次）
fleet repair --auto --attempts 3
fleet repair --restart          # 单动作
fleet repair --fix-permissions
fleet repair --clear-stale-pid
fleet repair --rollback         # 高风险，需显式
```

菜单：`fleet`（无参数）或 `fleet menu` → `[17] 本机自检 / 自修复`。

## 6. 安全设计

1. **确认门槛**：写操作必须 `confirm=<节点名>`，防止误调用。
2. **只读节点拒绝**：`read_only=true` 时 `fleet_repair*` 直接拒绝。
3. **有界**：最多 3 次；失败即停，不循环。
4. **白名单**：只允许上述 4 个动作；`rollback` 额外开关。
5. **审计**：每次修复写 `fleet_repair` / `fleet_repair_loop` 审计记录，可用
   `fleet_audit_verify` 复核哈希链。
6. **不碰密钥/数据**：修复不读取或输出 token、不删除邮箱、不改代码。
7. **失败上报**：返回 `remaining`（critical/high）与 `next`，让 Agent 交给人工。

## 7. 局限

- 自愈只覆盖“常见可恢复故障”；代码 bug、磁盘写满、内核/网络故障仍需人工。
- `restart_service` 依赖安装器生成的 `fleet-ctl.sh`；手工部署需自行提供。
- rollback 依赖升级备份（`<install>.bak-*`）；首次安装没有备份。
- 修复动作是“重启级”自治，不提供任意命令执行；这是刻意限制。
- 自检是本地视角，不探测对端网络质量（用 `fleet doctor`/`mesh`）。

## 8. 验收标准

- 自检返回 JSON，含 findings/repair_plan；
- 重启类故障修复后 `/health` 恢复 200；
- 权限/stale pid 修复后复检无对应 finding；
- 任何失败不得超过 `max_attempts`，且返回 remaining；
- 审计链可通过 `fleet_audit_verify` 校验；
- 只读节点调用被拒绝。
