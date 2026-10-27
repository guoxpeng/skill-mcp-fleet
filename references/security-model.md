# 安全模型 · MCP Fleet v5.2

> 读者：安全评审、想公网部署的人、想理解「为什么这样设计」的开发者。
> 产品用法见 `SKILL.md`；协议细节见 `references/architecture.md`；披露流程见 `SECURITY.md`。

## 1. 资产与信任边界

| 资产 | 位置 | 泄露/篡改后果 |
|------|------|----------------|
| 节点身份私钥 | `data/identity.json`（0600） | 可冒充该节点发起签名请求 |
| 访问 token（兼容 v5） | `mcp_agent_config.json`（0600） | 可调用该节点全部工具 |
| 对端公钥/地址 | `peers.json`（0600） | 可被替换为攻击者地址（需同时改文件权限） |
| 邮箱消息 | `mailbox/*.jsonl`（加密） | 明文泄露本机/跨机任务内容 |
| 审计日志 | `audit.chain.jsonl`（0600，哈希链） | 掩盖攻击痕迹 |
| 执行能力 | 本机进程权限 | 远程命令执行（最高风险） |

信任边界：

```
[不可信] 网络（局域网/公网/中继）
   │  UDP 发现包、HTTP 请求、邮箱消息
   ▼
[边界 1] 传输层：TLS（可选）+ 请求签名 + 时间窗 + nonce
   ▼
[边界 2] 身份层：Ed25519 公钥 pinning + SAS 人工核对
   ▼
[边界 3] 应用层：工具白名单/只读/审批 + 执行策略 + 路径 jail
   ▼
[边界 4] 数据层：邮箱加密 + 配置 0600 + 审计脱敏
   ▼
[可信] 本机内核/文件系统（本模型不防宿主被攻陷）
```

## 2. 威胁模型（STRIDE）

| 威胁 | 场景 | 缓解 | 残余风险 |
|------|------|------|----------|
| **S**poofing | 局域网冒充已知节点 | 已配对节点请求必须 Ed25519 签名；发现包 HMAC/pinning；指纹变化告警 | 首次配对前的 TOFU 窗口依赖 SAS |
| **T**ampering | 篡改命令/消息/审计 | 请求签名覆盖 method/path/body；邮箱 enc2$ 认证加密；审计哈希链 | 本机 root 可改一切（超出模型） |
| **R**epudiation | 否认执行过操作 | 哈希链审计记录工具/参数摘要/结果/耗时/对端 | 审计文件被删除无法自证 |
| **I**nformation disclosure | 抓包/备份泄露 | TLS（可选）+ 邮箱认证加密 + 日志脱敏 + Dashboard token 走 fragment | 未开 TLS 时可被被动嗅探元数据 |
| **D**enial of service | 连接洪水/消息 flooding/大 body | 并发信号量、body 上限、限流、inbox 上限、超时、死信截断 | 带宽型 DDoS 无法在应用层解决 |
| **E**levation of privilege | 低权限节点调用高权限工具 | 只读模式、assisted 审批、非 root 运行、路径 jail、危险命令策略 | 节点进程被完全控制 = 该进程权限 |

## 3. 信任等级

| 等级 | 条件 | 允许的操作 |
|------|------|-----------|
| **S0** 未配对 | 仅 UDP 发现/健康检查 | 默认只读查询；变更类建议拒绝（公网部署应关闭裸奔） |
| **S1** token | 共享 Bearer token（v5 兼容） | 全部工具（受策略/只读约束） |
| **S2** 公钥 | 配对并 pinning 对方 Ed25519 公钥 | 全部工具 + 签名请求（推荐） |
| **S3** 公钥+TLS | S2 + 自签/受信 TLS | 公网/跨网段推荐 |

## 4. 身份生命周期

1. **生成**：首次启动生成 Ed25519 密钥对（`fleet_core.Identity`），私钥 0600，目录 0700；
   `node_id = base32(sha256(pub))[:16]`，`fingerprint` 为 16 字符分组（如 `A0B5-C21D-26B4-D0B2`）。
2. **配对**（当前 CLI 实现）：
   - A 生成邀请：`Invite{name,node_id,pubkey,host,port,ts,ttl,nonce,sig}`，
     `sig = Ed25519(A, "fleet1|invite|...")`；
   - B 校验签名 + 有效期，通过 `POST /fleet/pair` 带上自己的公钥；
   - A 保存 B 公钥并回传自己的公钥；
   - 双方计算 **SAS = 6 位数字(sha256(排序后的两个公钥))**，由人核对；
   - 双方 pinning 对方公钥（`peers.json` 0600）。
   - `fleet_core` 同时提供 X25519 临时密钥 + `pair_proof` 原语，供后续版本升级为
     前向保密握手；当前 CLI 走「公钥交换 + SAS」，不传共享密钥。
3. **轮换**：`fleet token rotate` 轮换 token（旧 token 立即失效）；身份轮换需重新配对。
4. **吊销**：从 `peers.json` 删除对端 + 对端删除本机；指纹变化时节点拒绝签名请求。

## 5. 请求签名与防重放

```
X-Fleet-Node:  <node_id>
X-Fleet-Ts:    1792602140
X-Fleet-Nonce: 32 hex（每次随机）
X-Fleet-Sig:   base64(Ed25519("fleet1|METHOD|PATH|ts|nonce|sha256(body) hex"))
```

服务端顺序：IP 白名单 → 节点已配对 → 时间窗（默认 ±300s）→ nonce 未见过 →
签名有效 → 放行。失败返回 401，并计入鉴权失败统计。

- **重放**：同一 nonce 第二次请求在 `ReplayGuard` 窗口内拒绝（实测 401）。
- **篡改**：body/path/method 任一变化都会使签名失效（实测 401）。
- **时钟偏差**：超过窗口一律拒绝；`fleet doctor` 会提示系统时间异常。
- 旧 v5 客户端无签名时回落 token（`compat_v5=true`）；关闭后仅接受签名。

## 6. 数据加密

| 数据 | 格式 | 算法 | 说明 |
|------|------|------|------|
| 邮箱内容（v5.2） | `enc2$<b64(salt+seal)>` | PBKDF2-SHA256 派生 + SHA-256 CTR + HMAC-SHA256（encrypt-then-MAC，AAD=`fleet-mail`） | 默认；`fleet_core` 可用时启用 |
| 邮箱内容（v5.0 兼容） | `enc1$<b64(salt+nonce+ct+tag)>` | 同上结构 | 旧数据可读，新数据优先 enc2$ |
| 敏感配置 | JSON 0600 | 文件权限 | token/私钥/sudo 密码 |
| 日志/审计 | 文本 0600 | `redact()` | token/私钥/邀请码/enc2$/enc1$ 自动打码 |

局限：PBKDF2 100k 轮用于口令派生，抗离线爆破能力有限；邮箱密钥来自节点 token
（未配 token 时使用持久化随机密钥），token/密钥泄露等于邮箱可解密。建议每节点
独立 token、定期轮换。

**迁移注意**：v5.0 及更早的明文消息不会自动重写；升级后新消息使用 enc2$。
若旧消息敏感，停节点后清理 `mailbox/` 再从备份恢复，或接受其保持明文并按磁盘
加密/权限保护。

## 7. 执行策略矩阵（默认）

| 命令/操作 | 决策 | 规则 ID |
|-----------|------|---------|
| `rm -rf /`、`rm -rf /*` | deny | `rm_root` |
| `mkfs.*` | deny | `mkfs` |
| `dd ... of=/dev/*` | deny | `dd_blockdev` |
| fork bomb | deny | `fork_bomb` |
| 写 `/etc/shadow`、`/etc/sudoers` | deny | `shadow_write` |
| `shutdown` / `reboot` / `poweroff` | ask | `power_state` |
| 网络脚本直接执行（`curl ... | sh`） | ask | `pipe_to_shell` |
| 超时 > max_timeout（默认 60s） | ask | `timeout` |
| 路径越界（配置 path_jail 时） | deny | `path_jail` |
| 其他 | allow | `default_allow` |

- 用户配置**只能追加**规则（`ExecPolicy.with_defaults`），不能因为漏写
  `deny_patterns` 而关闭默认拦截（真实设备上发现并修复的缺陷）。
- `ask` 需要调用方先向用户确认，再带 `confirm=true` 重试；`--read-only` 节点直接拒绝
  所有变更类工具（包括 `fleet_token_rotate` / `fleet_handoff`）。
- `assisted` 模式：远程请求进入本机审批队列，本机 agent 批准后才执行。

## 8. 审计与取证

- 每条记录：`{ts, idx, prev, hash, event{tool,args(redacted),status,peer,ms}}`。
- `hash = sha256(idx|ts|prev|canonical(event))`，`prev` 指向上一条 hash。
- `fleet audit` / `fleet_audit_verify` 返回 `ok` 与被篡改的 `bad_index`。
- 审计轮转按大小（默认 5 MB）；轮转不破坏链（新文件重新起链，旧文件保留）。
- 安全事件（配对、指纹变化、鉴权失败、策略拒绝、重放）进入审计与指标。

## 9. 供应链与部署

- `scripts/install.sh` 下载 release 后校验 `SHA256SUMS`；失败拒绝执行；
  `--offline` 用本地源码；`--version` 固定版本；`--require-verify` 强制校验。
- 发布包由 `scripts/build_package.py` 生成，含 `MANIFEST.json` + `SHA256SUMS`，
  CI 校验文件数与哈希。
- 服务单元加固：systemd `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=strict` /
  `ReadWritePaths` / `Restart=always`；Windows 计划任务双守护；launchd `KeepAlive`。
- 公网部署清单：`--host 0.0.0.0` + `--require-auth` + TLS 或 Tailscale/中继 +
  `allow_ips` + 非 root 用户 + `fleet doctor` 全绿 + 审计巡检。

## 10. 已知限制（诚实声明）

- 纯 Python 大整数运算**非常数时间**，Ed25519/X25519 实现适合低价值/短生命周期场景；
  高价值部署应叠加 TLS/Tailscale 或替换为原生密码库。
- 当前 CLI 配对为「签名邀请 + 公钥交换 + SAS」，不提供前向保密；X25519 原语已实现，
  但尚未接入 CLI 握手（后续版本）。
- 中继/转发（`--relay`）目前只写配置与状态展示，未实现真正的反向中继；
  跨 NAT 优先用 Tailscale 或带公网地址的邀请直连。
- 消息的 `seq`/`expires_at` 语义为规划项；已实现的是 msg_id 去重、退避重试、
  死信截断、上线补投。
- 节点进程被完全控制时，本模型不提供隔离；请用独立低权限用户/容器/网络隔离。
- 审计只保证「链内篡改可发现」，不防删除整个日志或 root 级伪造。

## 11. 验证方法（可复现）

```bash
# 单元/集成测试：签名、重放、篡改、策略、审计链
.venv/bin/python -m pytest tests/test_core_51.py tests/test_integration_51.py -q

# 实机：启动节点后
fleet identity                 # 查看指纹/SAS 所需公钥
fleet doctor                   # 12 项自检
fleet audit --tail 20          # 哈希链校验
curl -s http://127.0.0.1:3100/fleet/identity
```

2026-10-22 真机（Android 14/Termux + macOS）实测：签名请求 200、同 nonce 重放 401、
篡改签名 401、`rm -rf /` 被 `rm_root` 拒绝、审计链 `ok=true`。
