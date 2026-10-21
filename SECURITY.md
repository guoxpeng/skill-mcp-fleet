# Security Policy / 安全策略

MCP Fleet 让 AI Agent 在你的设备上执行命令、读写文件、管理服务——安全是第一设计约束。
本项目采用**纵深防御**：身份、传输、应用、策略、审计、供应链六层，任一层被突破仍有下一层。
欢迎安全研究，感谢负责任披露。

## 支持版本

| 版本 | 支持状态 |
| ---- | -------- |
| 6.0.x | ✅ 当前版本，接收安全修复 |
| 5.0.x | ⚠️ 仅关键漏洞修复，建议升级 |
| < 5.0 | ❌ 不再维护 |

## 报告漏洞

**请不要开公开 Issue 报告安全漏洞。**

1. GitHub 私有安全通告：仓库页 Security → "Report a vulnerability"
2. 或邮件联系维护者（见 README 作者信息）

请包含：影响版本、复现步骤、影响评估（RCE / 越权 / 消息泄露 / DoS）、
最小复现脚本。我们承诺 **72 小时内响应**；修复后在致谢中列出报告者（除非要求匿名）。
请勿在修复发布前公开细节。

## v5.2 安全设计（六层）

### 第 1 层 · 身份与信任

- 每节点启动生成 **Ed25519 身份**（`identity.json`，0600），节点 ID = `base32(sha256(pubkey))[:16]`，
  指纹 = 人类可读 16 字符分组（`ABCD-EFGH-IJKL-MNOP`）。
- 配对走 **邀请链接 + SAS 校验**：邀请由邀请方私钥签名，含一次性 nonce、地址、TTL；
  接受方验证签名与有效期后，双方交换 Ed25519 公钥并计算同一个 **6 位 SAS**，
  由人核对，防中间人。fleet_core 已提供 X25519 + pair_proof 原语，供后续升级为
  前向保密握手；当前 CLI 采用公钥交换。
- 首次信任为 TOFU（首次使用即信任）+ SAS 人工确认；公钥固定（pinning），
  指纹变化时拒绝连接并告警，需显式 `fleet pair --reset`。
- 支持指纹/密钥轮换：`fleet token rotate --identity`，旧公钥进入吊销表。

### 默认安全（v5.2）

- 安装时默认生成随机 `auth_token`（`--no-auth` 才关闭），并只向终端打印一次。
- 非 loopback 监听且无 token 时，节点启动会自动生成 token 并写配置（`auto_secure`）；
  `--allow-insecure` 可显式降级。
- JSON 安装结果中的配置已脱敏，不回显 token/sudo 密码。

### 第 2 层 · 传输与请求

- 每个跨节点请求带 **Ed25519 签名**：`fleet1|METHOD|PATH|ts|nonce|sha256(body)`；
  服务端校验时间窗（默认 ±300s）、nonce 去重（`ReplayGuard`，容量上限 + 定期清理）、
  签名有效性。重放、篡改、伪造均拒绝并计数。
- 可选 **TLS**：`--tls auto` 自动用 openssl 生成自签证书（无 openssl 则跳过并提示）；
  证书指纹在 `fleet status` / `fleet doctor` 展示；跨网段/公网部署建议 TLS 或 Tailscale；
  `--relay` 目前只写配置，未实现反向中继。
- 旧 v5 的 Bearer token 仍兼容（`compat_v5: true`），但默认在公网/只读/新配对场景要求签名。
- 所有请求体有大小上限，连接有并发上限（信号量）、空闲超时与 TCP keepalive；
  慢速攻击/连接洪水返回 503，不占满线程。

### 第 3 层 · 数据与消息

- 邮箱消息默认 **认证加密**（`enc2$`：HKDF-SHA256 派生 + SHA-256 CTR 流 + HMAC-SHA256，
  encrypt-then-MAC，带 AAD 绑定上下文）；密钥由配对会话密钥派生，落盘为密文。
- 旧 `enc1$` 邮件可读（向后兼容），首次升级后新消息自动使用 `enc2$`。
- 邮件带 `msg_id` 去重、`seq` 排序、`expires_at` 过期、`origin_node` 来源签名；
  跨节点 gossip 不能伪造 `from`（伪造签名直接丢弃并计入指标）。
- 敏感配置（token/私钥/sudo 密码）0600、目录 0700；日志/审计/Dashboard **自动脱敏**。

### 第 4 层 · 执行策略（最小权限）

- `ExecPolicy` 默认拒绝：`rm -rf /`、`mkfs*`、`dd of=/dev/*`、fork bomb、写 `/etc/shadow`、
  `shutdown/reboot`（默认 ask）、管道到 shell 的可疑组合等；返回明确 rule_id 与原因。
- 路径 jail（`allowed_roots`，realpath 防软链逃逸）、命令超时、输出上限、进程组回收。
- `--read-only`：所有变更类工具拒绝（查询类放行），用于审计观察节点。
- `assisted` 模式：远程 agent 的变更请求进入本机审批队列，人工确认后执行。
- 支持全局/每节点 `ask` 规则，Agent 在执行前必须获得用户确认。

### 第 5 层 · 审计与可观测

- **哈希链审计**（`audit.log`）：每条记录含 `prev_hash`，`fleet audit verify` 可定位被篡改行；
  记录时间、来源节点/指纹、工具、参数摘要（脱敏）、结果、耗时。
- 审计轮转（大小上限 + 保留份数），日志权限 0600；关键安全事件（配对、指纹变化、
  鉴权失败、策略拒绝、重放）单独计数并可在 `fleet metrics` 查看。
- `/fleet/health`（存活/版本/摘要）、`/fleet/metrics`（Prometheus）只读且需鉴权。

### 第 6 层 · 供应链

- 安装引导脚本下载发布包后 **SHA256 校验**，失败拒绝执行；支持 `--version` 固定版本、
  `--offline <dir>` 本地包、`--dry-run` 预览。
- 发布包由 `scripts/build_package.py` 生成，附 `SHA256SUMS` 与文件清单；
  仓库 CI 做语法检查、测试、CodeQL。
- 不静默安装第三方依赖；安装器只检查并给出平台对应的安装命令，由用户执行。
- 服务单元加固：systemd `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=strict` /
  `Restart=always`；默认非 root 用户运行，需要提权时单独配置。

## 威胁模型摘要

| 威胁 | 缓解 |
|------|------|
| 局域网内冒充节点 | 配对公钥 pinning + 请求签名 + 发现包签名 |
| 中间人替换邀请 | 邀请签名 + SAS 人工核对 |
| 抓包读取命令/文件 | TLS + 应用层消息加密（纵深） |
| 消息重放/篡改 | 时间窗 + nonce + 签名 + 认证加密 |
| 恶意节点注入任务 | 仅信任已配对公钥；策略拒绝危险命令；`from` 由签名确定 |
| 本机横向提权 | 非 root 运行、路径 jail、无 sudo（默认）、systemd 沙箱 |
| 磁盘被拷走 | 邮箱认证加密；配置 0600；审计脱敏 |
| 供应链投毒 | SHA256 校验 + 固定版本 + 离线包 + CI |
| DoS（连接/消息/磁盘） | 并发信号量、体上限、限流、inbox 上限、退避 |
| 节点被完全攻陷 | 明确边界：攻击者获得该进程权限；用低权限用户/网络隔离/只读降低影响 |

## 安全边界（诚实声明）

- 纯 Python 实现的 Ed25519/X25519 未做严格常数时间优化，**不适合高价值长期密钥**；
  它用于局域网/家庭/小团队场景的身份与防篡改，不替代经过审计的密码学库。
- 不提供完美前向保密（长期身份密钥 + 会话密钥）；密钥泄露后的历史消息保护依赖会话密钥轮换。
- 节点进程被完全控制时，任何本机工具都可能被滥用；本项目的目标是**限制被远程利用的面**，
  而不是在宿主被攻陷后提供隔离。
- 默认不开公网监听；公网部署请叠加 TLS/Tailscale 或带公网地址的邀请直连，并启用
  `--require-auth` 与只读/审批。

## 安全测试

- `tests/test_core_51.py`：RFC 7748/8032 向量、篡改、重放、邀请伪造/过期、策略矩阵、审计链。
- `tests/test_hello.py` / `tests/test_crypto.py`：旧协议兼容与加密往返。
- `tests/test_agent_tools.py`：鉴权、只读、路径 jail、连接限流。
- `tests/test_integration_51.py`：端到端签名请求、重放拒绝、审计校验。
- 建议每月运行 `python3 -m pytest tests/ -q` 与 `fleet doctor`。

完整威胁模型与密钥生命周期见 `references/security-model.md`。
历史审计记录见 `references/安全审计报告.md` 与 `references/四轮审查-安全稳定易用.md`。
