# Changelog · MCP Fleet

所有版本的更新说明。版本号与 `scripts/mcp_agent.py` 内 `DEFAULT_CONFIG["version"]` 对齐，
每个里程碑版本对应一个 git tag 与一次华为云技能市场提交（submit_id）。
参赛文档中的 "v4.6" / "v5.0" 等简称为对应语义化版本的里程碑标记。

---

---

---

---

---

---

---

---

## [5.2.6] — 2026-10-22 · 远端更新按 commit SHA 取文件（彻底绕开 raw CDN 缓存）

- `raw.githubusercontent.com/.../main/` 与 `/refs/heads/main/` 都可能返回旧缓存
  （实测一个 5.2.4、一个 5.2.5）。现在先查 GitHub API `commits/main` 拿不可变
  commit SHA，再按 `/<sha>/scripts/install.sh` 取文件；API 失败才回退两个分支 URL。
- 手机实测：`fleet update` 下载 5.2.5、SHA256 校验通过、停旧服务、健康 OK，
  版本 5.2.4→5.2.5；本版继续自更新到 5.2.6 验证。
- 全量 **615 passed + 1 skipped**。

---

## [5.2.5] — 2026-10-22 · 远端更新最后两处修复（缓存 / 临时目录）

- **远端 install.sh 缓存**：`raw.githubusercontent.com/.../main/` 有 CDN 旧缓存，
  手机端拉到旧版本（5.2.3）。改用 `/refs/heads/main/` 路径，确保拿到最新发布号。
- **`tempfile` 未导入**：`_fetch_remote_installer()` 缺 `import tempfile`，异常被
  吞掉后永远回退本地安装器；补齐导入。
- **Termux 无 /tmp**：远端安装器优先在 `$HOME` 建临时文件，并给子进程补 `TMPDIR`。
- 手机实测：`fleet update` → 下载 5.2.4 → SHA256 校验通过 → 停旧服务 → 健康 OK，
  版本从 5.2.3 正确升到 5.2.4；随后 5.2.5 发布再自更新验证。
- 全量 **615 passed + 1 skipped**。

---

## [5.2.4] — 2026-10-22 · 升级真正重启 / 远端热更新 / HTTPS 校验加固

**手机 `fleet update` 实测暴露：升级时端口占用误判、旧进程未停（“升级了但没重启”）。**

### 修复
- **升级先停旧服务**：`--upgrade` 在预检前调用 `adapter.stop()`，升级后启动的
  确实是新代码；升级/回滚跳过端口占用检查（旧服务马上停）。
- **`fleet update` 默认拉最新发布包**：从 raw main 取最新 `install.sh`，由它下载
  Release 资产并校验 SHA256；`--offline` 才用节点目录本地安装器；Windows 无 sh
  自动回退本地安装器。
- **HTTPS 证书校验**：自签豁免仅限私网/回环；GitHub 等公网请求恢复系统默认校验，
  防中间人。
- **Termux 唤醒锁失败告警**：`termux-wake-lock` 返回非 0 时明确提示可能被冻结。
- **Docker/邮箱/Termux 修复**沿用 5.2.3。

### 测试
- 全量 **614 passed + 1 skipped**；新增升级停止顺序/远端更新/端口跳过回归。

---

## [5.2.3] — 2026-10-22 · 真机复测修复：Termux 启动 / 邮箱密钥迁移 / Docker

**手机（Android 14 / Termux）与跨端复测暴露的问题，全部修复并补测试。**

### 修复
- **Termux 启动崩溃（P0）**：`TermuxAdapter.start()` 在 `warnings=[]` 赋值前使用，
  真机升级时报 `UnboundLocalError`，服务无法启动。已修正并补单元测试。
- **邮箱密钥迁移破坏旧邮件（P0）**：v5.2.2 用 auth_token 派生邮箱密钥，
  升级/轮换后旧邮件被当密文占位符写回，造成不可恢复。现在：
  - 邮箱固定使用 `data/mailbox.key`，与 token 解耦；
  - 旧 token 派生数据作为 fallback 自动读取；
  - 首次启动一次性迁移，遇到任何解不开的邮件则**整体跳过迁移**；
  - 写回时保留无法解密的原始密文，绝不覆盖成占位符。
- **Docker 部署**：去掉只读根（mailbox 需落盘）、容器内强制 HTTP（TLS 交反代）、
  `MCP_FLEET_DATA=/data` 环境变量覆盖数据目录、镜像补齐 `fleet.py/fleet_core/fleet_agent`。
- **Termux 电池优化**：授权页命令改为可诊断输出，失败不阻塞启动。

### 验证
- 手机真机：升级成功、默认 token、`$PREFIX/bin/fleet`、唤醒锁 + 电池授权页、
  `fleet status/doctor` 正常；Mac ↔ 手机 `peer_call` 双向执行、消息投递正常。
- 全量 **611 passed + 1 skipped**；新增邮箱迁移/不丢数据/Termux 启动回归。
- 8 种服务单元（systemd/openrc/procd/launchd/schtasks/termux/docker/nohup）
  静态渲染全部通过。

---

## [5.2.2] — 2026-10-22 · 修复回滚 TLS / Dashboard 401 / 卸载残留

**v5.2.1 端到端实测继续暴露的 4 个问题，全部修复。**

### 修复
- **回滚后服务起成 HTTP、客户端按 HTTPS 连（P0）**：`rollback()` 创建适配器时没有
  从恢复的配置读 `tls_cert/tls_key`，导致进程不带 `--tls-cert` 启动，
  `fleet status` 报 `SSL: WRONG_VERSION_NUMBER`。现在回滚前把配置里的
  TLS/只读/鉴权参数带回 opts。
- **Dashboard 页面 401（P0）**：`/fleet/ui` 静态页被鉴权闸门拦截，但浏览器
  token 走 URL fragment（不会发送到服务器），页面根本打不开。现在页面本身免鉴权，
  `/fleet/ui/data` 仍需 token/签名；新增回归测试。
- **Dashboard 任务步数显示 0**：任务存储字段是 `event_count` 而非 `steps`，
  已兼容读取。
- **卸载残留启动器/安装目录**：`--uninstall` 现在删除全局 `fleet` 启动器
  （校验内容归属，防误删同名文件）；`--purge` 连安装目录一起清，默认卸载保留
  `data/` 与 `mailbox/` 并明确提示。

### 测试
- 全量 **606 passed + 1 skipped**；新增 Dashboard 页面免鉴权/数据鉴权、升级
  目标保护、卸载清理回归。

---

## [5.2.1] — 2026-10-22 · 修复升级目标解析 / HTTPS 健康检查 / 发布包模板兜底

**v5.2.0 实测暴露的 3 个真实缺陷，已修复并补回归测试。**

### 修复
- **`fleet update/--rollback` 升级错节点（P0）**：CLI 调用安装器时未传
  `--name/--dir/--data-dir/--port/--service`，安装器按默认主机名新建了另一个节点，
  导致健康检查失败、回滚找不到备份。现在 `_local_node_install_info()` 从本机
  配置/manifest 读取真实目标并显式传入；安装器新增“升级目标不匹配/不存在”拒绝。
- **自签 TLS 节点健康检查失败（P0）**：`health_check` 只请求 http，而 `--tls auto`
  实际监听 https，导致安装成功但健康检查 401/重置。现在按 TLS 状态使用 https +
  自签证书校验豁免（仅本机回环）；同时把 `tls_cert/tls_key` 写回配置，
  `fleet status/ask/doctor` 也能识别 https 与自签证书。
- **发布包缺 service-templates 导致跨平台安装失败（P0）**：Release 包只带 3 个
  模板，OpenWrt/Termux/Windows/nohup 等适配器报“缺少模板”。现在
  `fleet_install.py` 内置 10 个模板兜底，发布包不带 templates 也能全平台安装。

### 测试
- 全量 **605 passed + 1 skipped**；新增升级目标不匹配/不存在、CLI 更新参数、
  HTTPS 健康检查覆盖。

---

## [5.2.0] — 2026-10-22 · 体验增强：安全默认 / 全局命令 / 升级回滚 / Dashboard 2.0 / 自然语言入口

**主题：让普通用户第一次就能装对、连上、看懂、升级。**

### 安装体验（P0）
- **默认鉴权**：安装时自动生成强随机 token（0600），人读输出只打印一次；
  `--no-auth` 显式关闭并警告；`--dry-run` 零副作用。
- **全局 `fleet` 命令**：安装器创建 `/usr/local/bin/fleet`、`$PREFIX/bin/fleet`
  或 `~/.local/bin/fleet`（Windows 为 `fleet.cmd`），写入 manifest，卸载时清理。
- **升级/回滚**：`--upgrade` 自动备份安装目录（含 mailbox/config）→ 覆盖 → 重启 →
  健康检查；`--rollback` 恢复最近备份；`fleet update` 直接调用。
- **Android 14 电池优化**：Termux 安装时自动拉起「忽略电池优化」授权页并提示，
  降低息屏后台冻结概率。
- **发布脚本**：`scripts/release.py` 一键创建 GitHub Release 并上传 zip+SHA256SUMS
  （支持 GITHUB_TOKEN / git 凭据，`--dry-run`，不打印 token）。

### 安全默认（P0）
- 非 loopback 监听且无 token 时，节点启动自动生成 token（`auto_secure`，可关）；
  `--allow-insecure` 显式降级。
- JSON 安装结果中的配置已脱敏，不再回显 token/sudo 密码。
- 错误信息带下一步：401/403 提示重新配对，连接失败提示查防火墙/Tailscale/doctor。

### CLI 体验（P1）
- `fleet ask "<自然语言>" [--run]`：规则意图识别（磁盘/网络/Docker/服务/备份/体检），
  生成计划 + 可粘贴给 AI 的提示词；`--run` 只执行只读白名单命令并汇总证据。
- `fleet invite --qr`：有 `qrencode` 时终端显示二维码，缺失时给安装提示。
- `fleet join --auto-install`：本机未装节点时自动安装再加入。
- `fleet identity export/import`：口令加密备份/恢复 Ed25519 身份，换机不重配全网。
- `fleet update [--rollback]`：封装安装器升级/回滚。

### Dashboard 2.0（P1）
- 新增：节点指纹/安全状态（审计/签名/TLS/只读）、投递队列（待投递/死信）、
  Agent 任务时间线、对端指纹与「移除设备」（`POST /fleet/peer_remove`）、
  `/fleet/tasks` 端点。
- 数据接口扩展 `identity/security/delivery/tasks`，页面 5s 刷新。

### 文档
- 新增 `references/quickstart.md`（5 分钟上手）与 `references/troubleshooting.md`
  （一页排障）；SKILL.md 参考索引同步。
- 版本统一为 **v5.2.0**。

### 测试
- 全量 **603 passed + 1 skipped**；新增 CLI 14 例、Dashboard/默认鉴权 5 例、
  安装器 6 例（默认 token/无鉴权/launcher/升级/回滚）。

### 升级注意
- 从 v5.1 升级会保留数据目录与配置；`fleet update` 自动备份。
- 首次启动 v5.2 的对外监听节点会自动生成 token；旧 MCP 客户端需补 `Authorization`，
  或重新配对后在 `peers.json` 使用签名。

---

## [5.1.0] — 2026-10-22 · 参赛版重构：标准 Skill + 安全身份 + 全平台一键接入 + Agent 自对话

**主题：从"能连"升级为"敢连、好连、会自己想"。**
相对 v5.0.9 是一次大改：新增 3 个核心模块（`fleet_core.py` / `fleet_install.py` /
`fleet_agent.py`），重写安装引导与 Skill 文档，保留 v5 全部协议与工具兼容。

### Skill 规范
- `SKILL.md` 重写为 Agent Skills 规范：frontmatter 仅保留 `name` / `description` /
  `license` / `compatibility`；正文 < 300 行，按渐进披露拆到 `references/`；
  description 覆盖触发场景（NAS/云主机/软路由/手机/PC/多 Agent 协作）。
- 新增 `LICENSE`（MIT）、`AGENTS.md`（仓库内 AI 协作约定）、`SECURITY.md` 六层安全模型。
- 新增 `docs/V5.1-CONTRACT.md`（模块契约）、`references/architecture.md`、`references/faq.md`。
- `scripts/build_package.py`：一键生成 ≤30 文件、含 `SHA256SUMS` 的参赛提交包。

### 安全（v5.1 六层）
- **身份**：每节点 Ed25519 身份 + 指纹；邀请链接签名 + 公钥交换 + 6 位 SAS 人工核对，
  防中间人；公钥 pinning，指纹变化告警（X25519 原语已实现，CLI 暂走公钥交换）。
- **请求**：跨节点请求 Ed25519 签名 + 时间窗 + nonce 防重放；可选 TLS 自签自动生成与指纹展示；
  旧 token 鉴权兼容。
- **数据**：邮箱认证加密 `enc2$`（HKDF + SHA-256 CTR + HMAC，encrypt-then-MAC，AAD）；
  旧 `enc1$` 可读。
- **策略**：`ExecPolicy` 默认拒绝 `rm -rf /`、`mkfs`、`dd of=/dev/*`、关机等；路径 jail、
  超时/输出上限、`--read-only`、`assisted` 审批。
- **审计**：哈希链审计（`prev_hash`），`fleet audit verify` 定位篡改行；关键安全事件计数。
- **供应链**：安装引导 SHA256 校验、固定版本、离线包、`--dry-run`；systemd 服务加固。

### 稳定连接
- 对端状态机（discovered → pairing → trusted → degraded → offline）、退避+抖动、
  熔断与恢复；消息 `msg_id` 去重、退避重试、死信截断；上线自动补投。
- 多路径：局域网 UDP 发现 / 邀请直连 / Tailscale（`--relay` 配置预留）；
  `/fleet/health` + `/fleet/metrics`（Prometheus）+ `fleet doctor` 12 项自检。
- 优雅关闭、配置校验、版本协商、连接/请求限流与超时。

### 全平台一键接入
- 统一 `scripts/fleet_install.py`：识别 systemd / openrc / procd(OpenWrt) / launchd /
  schtasks(Windows) / Termux(sv) / Docker / nohup，生成服务、开机自启、健康检查。
- `scripts/install.sh` 重写为 POSIX 薄引导：SHA256 校验、`--offline`、`--version`、
  `--dry-run`，不再内嵌 5000 行源码。
- Android/Termux、飞牛 fnOS、群晖 DSM、威联通、绿联、OpenWrt、Linux、macOS、Windows、
  Docker、云主机安装矩阵与限制见 `references/platform-matrix.md`。
- `scripts/make-offline.py` 生成带校验和的离线包。

### Agent 自对话
- MCP `initialize.instructions` 内置操作手册；`prompts/list|get` 提供 7 个提示词
  （playbook/diagnose/plan/self-review/delegate/incident/onboard）。
- 新增自对话工具：`fleet_help` / `fleet_think` / `fleet_plan` / `fleet_reflect` /
  `fleet_review` / `fleet_handoff`；结构化消息信封（propose/accept/counter/progress/
  result/fail/review/handoff）与线程/验收/超时；确定性 Orchestrator + 可选 LLM 适配。
- `references/agent-playbook.md`、`self-dialogue-protocol.md`、`prompt-pack.md`。

### 工具与运维
- 新增 `fleet_doctor` / `fleet_metrics` / `fleet_identity` / `fleet_audit_verify` /
  `fleet_token_rotate` 等运维工具；MCP 工具带 annotations（readOnly/destructive）。
- `fleet` CLI 统一入口：`install / up / down / join / invite / pair / nodes / status /
  doctor / send / exec / logs / audit / token / relay / update / uninstall / demo / menu`。

### 测试与质量
- 新增 `tests/test_core_51.py`、`test_installer_51.py`、`test_agent_brain.py`、
  `test_integration_51.py`；修复 7 个 macOS 平台相关失败用例；CI 覆盖 3.9/3.12。
- 全量回归：**578 passed + 1 skipped**（原 130）；含 183 安装器、134 安全核心、
  116 Agent、16 v5.1 端到端集成用例。

### 升级注意
- v5 节点可继续与 v5.1 互通（token/enc1 兼容）；建议全部升级后执行 `fleet pair --reset`
  重建公钥信任，再关闭 `compat_v5`。
- 旧 `install.sh` 参数保留；`scripts/install.py` 保留但推荐 `fleet_install.py`。

---

## [5.0.9] — 2026-10-21 · 深度自检：3 处 CLI/协议层历史缺陷修复

**对全仓库做静态扫描（pyflakes）+ 未测试覆盖脚本审查 + 运行时端点冒烟，
发现并修复 3 处历史缺陷；每处先复现再修复，并补回归守卫测试。**

### 修复
- **fleet msg 命令一执行即崩溃（严重，v4.1 起）**：`cmd_msg` 调用的
  `_resolve_target()` 全仓库无定义，`fleet msg send/recv/history` 100%
  抛 NameError、功能完全不可用。改用与 `cmd_call`/`cmd_probe` 一致的
  `resolve_target()`（返回 `(url, meta)`，token 取自 meta）。
  修复后实测：send 成功入 outbox、history 正常返回。
- **配对确权路径 NameError（v4.1 起）**：fleet.py 使用 `uuid.uuid4()`
  却从未 `import uuid`——invite/verify 在「本机无节点信息且未指定名字」
  时崩溃。补上 import。
- **Dashboard 节点 last_seen 永远为空（v4.0 起）**：`main()` 中
  `_discovery_layer = DiscoveryLayer(...)` 缺少 `global` 声明，只创建了
  局部变量，模块级引用（`_dashboard_data` 读取）永远是 None。
  修复：`main()` 补 `global LISTEN_PORT, _discovery_layer`，并从
  `_init_proto` 的 global 声明中移除该未赋值名字。

### 清理（pyflakes 静态扫描）
- `fleet_proto.py` 重复 `import collections`
- `mcp_agent.py` 3 处未使用局部变量（_auto_discover_peers / _broadcast_join /
  t_mesh_caps）与 `fleet.py` 菜单未使用变量

### 测试
- 新增 5 个 CLI 完整性回归守卫用例（静态断言 + mock 客户端功能验证），
  全量 **130 passed + 1 skipped**（131 收集）。

---

## [5.0.8] — 2026-10-18 · 回归自检：修复 v5.0.7 三项改动引入的 4 处回归

**对 v5.0.7 新增的三项改动（去重/限流/只读模式）做专项回归排查，
每处修复均先写复现实验证明确凿，再落地代码与 pytest 回归用例。
这 4 处问题全部由当日自检发现并在当日修复，未流出版本。**

### 修复
- **去重误用对端 id 导致丢消息（严重）**：v5.0.7 的 `inbox_append` 用
  `msg_id or id` 做去重键，但 gossip poll 拉回的消息携带**对端** inbox 的
  自增 id（`poll_for_agent` 原样返回对端落盘数据），各节点 id 空间独立且
  都是小整数——两个不同 peer 的 id 碰撞时，后到的无 msg_id 消息被误判为
  重复而**静默丢弃**（复现实验：双节点 id=42 碰撞，第二条返回 False）。
  修复：去重键只认 `msg_id`（发件端生成、全局唯一）。
- **去重窗口重启丢失**：v5.0.7 的 `_seen_ids`/`_seen_set` 纯内存，进程
  重启即清空——投递响应丢失后 outbox 重试跨越对端重启时，消息会重复入箱。
  修复：`MailEngine` 启动时从 inbox.jsonl 末尾回填最近 4096 条 msg_id
  重建窗口（msg_id 为结构字段不参与落盘加密，直接读取）。
- **503 限流分支从未真正发出（严重）**：`handle()` 阶段调用 `send_response()`
  依赖 `parse_request()` 才初始化的 `requestline`/`request_version` 属性，
  必然 AttributeError 且被 `except: pass` 吞掉——超限客户端收到的是
  空响应/连接重置而非 503（最小复现实验 + 真实服务器 SSE 占槽实测双重
  确认）。修复：503 分支改为直接写原始响应字节，不依赖任何未初始化状态。
  修复后实测：4 槽占满后第 5 个请求正确收到 503。
- **旧版 nas_ 前缀绕过只读拦截（严重）**：`TOOL_FUNCS` 同时注册旧版
  `nas_` 前缀名，而 v5.0.7 的 read-only 检查只剥 `PREFIX`——用
  `nas_write` 调用在 read_only 模式下**真实写入了文件**（复现实验确认）。
  修复：前缀剥离与 assisted 模式对齐（PREFIX 与 nas_ 都剥）。

### 优化
- **SSE keepalive 周期 20s → 5s**：客户端断开后 SSE 线程要等下一次
  keepalive 写失败才能退出并归还 HTTP_SLOT，20s 周期意味着断开的连接
  最长占槽 20s（实测 22s 才恢复）。缩短后断开连接 5s 内释放槽位。

### 测试
- 新增 3 个回归用例：`test_peer_id_collision_not_deduped`（对端 id 碰撞
  不丢消息）、`test_dedup_window_rebuilt_on_restart`（重启回填）、
  `test_legacy_nas_prefix_blocked`（nas_ 前缀拦截 + 文件不落盘断言）。
  全量 **125 passed + 1 skipped**（126 收集）。

---

## [5.0.7] — 2026-10-18 · 外部审查落地：消息去重 + 连接限流 + 只读模式

**对一份外部系统化审查报告（6 项运行时隐患 + 5 项架构建议）逐项核查后落地。
核查结论：6 项隐患中 5 项已由前四轮审查防御（RLock 全方法覆盖、1MB body
上限 + Content-Length 校验、exec 超时 + killpg 进程组、realpath + allowed_roots
路径白名单、UDP hello 包 <200B 无工具清单），仅「消息去重」为真实缺口；
架构建议中 SQLite / asyncio / 动态工具加载属重写级，记录进 roadmap 不落地
（会推翻现有 122 用例测试套件的存储与网络层假设）。**

### 修复
- **收件箱 msg_id 去重（真实缺口）**：gossip 双路径（直投 /fleet/mail + 同步
  /fleet/poll）在重试/重连场景可能把同一条消息投两次。新增 4096 条滑动窗口
  （deque 淘汰顺序 + set O(1) 查询），重复 msg_id 直接丢弃，本地无 msg_id 的
  直写消息不受影响。
- **修复 example.json 版本号污染**：mcp_agent_config.example.json 的 version
  被早期快照回滚为 4.0.0 并误传至 GitHub，统一为 5.0.7。

### 新增（加固）
- **HTTP 并发连接限流**：`max_http_threads=64`（默认）—— ThreadingHTTPServer
  每连接一线程且无上限，网络扫描/并发洪水可打满线程。信号量在连接入口
  非阻塞抢槽，超限直接回 503；SSE 常驻连接同样占槽（16 < 64 余量充足）。
- **`--read-only` 只读模式**：审计观察节点专用。exec/write/edit/upload/
  docker_restart/systemctl/exec_async/exec_kill/kill/msg_send/request_assist/
  assist_queue/peer_call/peer_add/peer_remove/delegate/group_form/group_disband
  共 19 个变更类工具调用直接拒绝（带前缀调用同样拦截）；查询类
  （read/list_dir/sysinfo/docker_ps/docker_logs/ps/mesh_caps 等）全部放行；
  tools/list 仍返回完整 41 工具（拦截发生在调用层，计入审计日志）。

### 测试
- **122 用例全绿**（+10）：TestInboxDedup 5 项（重复去重/首条保留/无 id 不受
  影响/加密去重/窗口淘汰）、TestReadOnlyMode 5 项（变更类拒绝/查询放行/
  前缀拦截/未知工具/清单完整）。

### Roadmap（大赛前不落地；赛后作为 v5.1 演进项评估）

**处置决定（2026-10-19）**：以下三项经评估在大赛交付前搁置（重写级改动
将推翻现有 126 用例的存储/网络层假设，收益与风险不匹配），赛后随 v5.1
规划重新评估取舍。
- **SQLite 替代 JSONL 存储**：现 RLock 全方法覆盖 + 原子逐行追加，单进程多
  线程写入已安全；SQLite 迁移将推翻存储层测试假设，收益与应用场景不匹配。
- **asyncio 重写网络层**：现 ThreadingHTTPServer + daemon_threads + 连接级
  限流已可控；重写为 asyncio 属架构演进，非缺陷修复。
- **动态工具加载**（两阶段发现）：41 工具 schema 约数千 token，后续可做
  工具分组按需暴露；本期未改以保证大赛工具声明稳定。

---

## [5.0.6] — 2026-10-18 · Termux 一键安装 + 中文交互菜单（git tag: v5.0.6）

**打通安卓端：Termux 用户一条命令装主控 CLI（免 git clone），节点安装自动 nohup
守护；fleet.py 新增中文交互菜单（手机/桌面通用），降低非技术用户门槛。**

### 新增
- **Termux 主控 CLI 一键部署**：`install.sh --cli-only` —— 自动 pkg 装 python，
  从 GitHub 拉 fleet.py 部署到 `$HOME/.mcp-fleet-cli/`，单文件零依赖，免 git clone。
  部署后直接 `python3 ~/.mcp-fleet-cli/fleet.py menu` 进入中文菜单。
- **Termux 节点安装**：`install.sh` 自动检测 Termux（`uname -o=Android`），
  路径 root 用 `$PREFIX/opt`，普通用户用 `$HOME/.fleet/`，强制 nohup 模式，
  supervisor 3s 拉起 + kill -9 自动复活（与 Linux systemd 同语义）。
- **fleet.py 中文交互菜单**：`fleet.py menu`（无参数默认进入）—— 16 项操作，
  覆盖节点管理/诊断体检/远程操控/消息拓扑/安装发现，纯 input 零依赖，
  手机 Termux 与桌面终端都能跑，降低非技术用户门槛。
- **install.sh 参数 `--cli-only` / `--invite` / `--uninstall` 完整闭环。**

### 修复
- **_TERMUX_PREFIX 被覆盖（历史 bug）**：行 51 的 `_TERMUX_PREFIX="${PREFIX:-}"`
  把环境变量清掉，导致 Termux 检测在测试/特殊环境失效。修复为保留已有值。
- **CLI_ONLY 未初始化**：`set -u` 下未设 CLI_ONLY 时节点安装报 unbound variable。
- **fleet.py build_parser 缺 return p**：菜单子命令插入时意外丢失，导致无参数时
  报 `NoneType.parse_args`。
- **连接可靠性加固（v5.0.6.1，C1-C8）**：单播投递 `_http_deliver` 加 2 次重试
  （指数退避 1s/3s），网络抖动时消息不进死信；`_broadcast_loop` 广播 sock 异常
  路径泄漏（finally 兜底关闭）；`_listen_loop` 端口被占用/权限不足时不再静默
  退出（指数退避重试，最大 60s，前 3 次告警）；gossip 同步/拉信失败输出调试
  日志；`t_peers --check` 401/403 显示「在线(需令牌)」不再误报「不通」；
  `cmd_add`/`cmd_doctor` 路径提示改通用（不再硬编码特定用户目录）；
  install-cmd 帮助文本补 Termux 说明；三处 setsid 改为存在性检测 + nohup
  兜底（Termux 未装 coreutils 时安装失败）；curl 下载失败加 python urllib
  兜底（Termux 无 curl 场景）。
- **中文菜单参数 bug（v5.0.6.2，M1-M6）**：菜单选项 5/6 的 `name→target`
  参数名错误；选项 9 缺 `timeout/agent/port`；选项 12 缺 `text/file`；
  选项 14 缺 16 个参数导致调用必报错；选项 15 改为提示信息（无对应子命令）。

### 新增（v5.0.6.3）
- **`fleet` 无参数显示本机状态摘要**：已登记副端数 + 在线/离线 + 令牌配置
  状态 + 中文命令说明 + 快速开始指南，新用户零门槛上手。

### 测试
- **112 用例全绿**：新增 TestTermuxInstall 6 项——CLI 一键安装（真 HTTP 源）、
  Termux 节点安装（nohup 守护 + /health）、supervisor kill-9 复活、
  _TERMUX_PREFIX 保留、CLI_ONLY 初始化、菜单子命令存在；新增
  TestWinHardening 6 项（代理豁免/探活禁代理/pythonw 回退链）与
  TestPlatformGuard 11 项（三平台守护等价性）。
- Termux 冒烟全流程：mock Termux 环境验证 CLI 一键安装 + 节点安装 + 守护。

---

## [5.0.5] — 2026-10-17 · Windows 代理穿透修复版本（git tag: v5.0.5）

**第五轮审查聚焦 Windows 端。实锤并修复一个贯穿协议层与安装器的同源根因：
Windows 代理软件（科学上网/抓包工具）未豁免 `<local>` 时，urllib 默认 opener
会把发往 127.0.0.1 / 局域网 IP 的请求全部劫持到代理服务器——代理够不到
用户局域网，节点间消息全进死信、看门狗探活假阴性引发每分钟重启风暴。**
mcp_agent 工具层早有代理豁免（`_peer_opener`），协议层与安装器从未对齐。

### 修复
- **[高危] fleet_proto 三处裸 urlopen 被系统代理劫持（W1）**：单播投递
  （deliver）、gossip 同步（sync_with_peer）、拉信（_poll_peer）直连内网 URL
  时若系统代理未豁免回环，消息必然失败进死信。修复：新增 `_opener_for(url)`；
  回环/私网/链路本地/localhost/.local 直连（空 proxies），公网仍走系统代理
  （隧道/中转场景不受影响）——与 mcp_agent._peer_opener 同规则。
- **[高危] 看门狗探活被系统代理劫持（W2）**：probe_agent 匿名裸 urlopen，
  代理环境下健康节点被误判宕机 → 每分钟重启风暴。修复：探活目标恒为
  127.0.0.1，强制 `ProxyHandler({})` 禁代理（空 proxies 语义：直连）。
  端到端实测：假系统代理环境下 watchdog 对健康节点零动作。
- **[中危] 看门狗计划任务每分钟闪黑窗（W3）**：MINUTE 任务用 python.exe
  （控制台程序）→ 任务计划程序每分钟弹一次黑色窗口。修复：改用 pythonw。
- **[中危] venv 下找不到 pythonw 闪窗（W4）**：win_find_pythonw 只查
  sys.executable 同目录，venv 的 Scripts 下无 pythonw.exe → 回退 python.exe
  → 登录自启闪黑窗。修复：回退链补 sys.base_prefix/base_exec_prefix
  （venv 内指向真实 Python 安装目录）。
- **[低危] 安装流程 UnicodeDecodeError（W5）**：_run_cmd 的 text=True 遇
  schtasks 本地化输出（非 UTF-8 字节）抛解码异常中断安装。修复：errors=replace。
- **[低危] goodbye 上报裸 urlopen（W6）**：agent 退出广播 goodbye 同受代理
  劫持（低频），顺手走 _peer_opener。

### 测试
- **94 → 100 用例**：TestWinHardening 6 项——假代理环境下探活仍准确（真 HTTP
  服务 + watchdog 全链路零动作）、_opener_for 私网直连/公网保代理（功能断言：
  真实服务 + 假代理正反对照）、pythonw venv 回退链与最终回退、_run_cmd 容错。
- **端到端**：冷启动 0.5s；健康零动作；kill -9 复活；**假系统代理下不再误判**。
- 探活防误判保持：只认 `{status:ok, version:...}` 结构（与 5.0.4 一致）。

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
