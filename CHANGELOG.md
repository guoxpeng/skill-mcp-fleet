# Changelog

## 4.5.1 / 主控 1.2.1（2026-10-27，分支 fix/fleet-p0-20261027）

小版本补丁：在 4.5 线上修安全、稳定性与流畅性问题。协议不变
（PROTO_VERSION 仍为 1）。除下列行为变更外，对外行为不变。

主控版本（scripts/fleet.py）1.2.0 → 1.2.1；agent 版本
（scripts/mcp_agent.py 与 mcp_agent_config.example.json）4.5 → 4.5.1。
install.sh 内嵌副本仍为旧版（见“未动手”），其 VERSION 保持 1.2.0 不动，
避免只改版本号不改代码造成名实不符。

### 安全

- 安装默认拒绝无令牌：对外绑定安装时无 `--auth-token` 且无
  `--generate-token` 时拒绝安装；自设 `--auth-token` 短于 20 字符拒绝；
  单元模板补 `UMask=0077`。
- pair/add 写登记前必须先过带鉴权 exec 试调；pair 双写 agent 配置与
  登记表并回读校验；读路径统一接受 `headers.Authorization` 与裸 token，
  写路径双写。
- `cmd_sync` 与 `sync_from_directory` 补身份门：匿名探活返回的 server 名
  必须等于期望节点名，否则拒绝，令牌不发出。
- 邀请 verify 全程加锁 + 原子写，标记持久化失败返回 500；
  `/fleet/discover` 已有 peer 且无 `force=true` 时返回 409。
- UDP 发现先验真（匿名 GET + 握手）再加 peer；sudo 密码改走 stdin，
  不进命令行；`list --json` 默认掩码令牌（`--show-tokens` 才全量）；
  `--generate-token` 的令牌走 stderr；邀请输出标注“邀请码 base64 解码后
  就是令牌本身，用后即焚”。

### 稳定性

- 登记表原子写（临时文件 + fsync + os.replace + 目录 fsync），损坏时
  写路径拒绝覆盖、读路径回退 .bak 并警告；每次成功保存轮转 .bak。
- agent 配置文件存在但损坏时拒绝启动；`--generate-token` 写配置失败
  拒绝启动；端口占用打印诊断并以 exit 3 退出。
- 同步 exec 流式读 + 1MB 上限丢弃并打标记；主控响应读取 2MB 封顶；
  `http_probe` 对 401/403 不重试。
- 目录服务惰性淘汰 7 天无上报条目；裸 IPv6 地址拼 URL 时加方括号；
  `msg` 不再 NameError（改走 resolve_target）；`mesh` 改走统一发现。

### 流畅性

- 目录存活预检与鉴权探活不再重试叠加（`retries=0`）+ 各阶段进度行；
  exec 帮助点名 `--no-sync` 逃生口。
- `exec-poll` 加 `--limit`，`exec-result` 改自动分页循环流式输出；
  exec 超 10 秒打 stderr 心跳；health 逐节点即时打印 + `--parallel`；
  msg recv 超时按 `max(timeout, wait+30)`；主控加 GET-only `ping` 子命令；
  看门入口 flock、状态原子写、恢复需两次可用。
- 退出码修正：probe exec 失败返回 1、mesh 任一失败返回 1、
  BrokenPipe 静默退出 0；帮助注明 exec/call 客户端超时下限 120 秒、
  🔒 仅表示已配令牌。

### 测试

- 新增 `tests/`（stdlib unittest，零真实节点）：登记原子写与损坏、
  HTTP 封顶与 401 计时、ping、退出码、掩码、冒名拒绝、配置损坏、sudo、
  有界 exec、邀请并发、看门——31 项全过。

### 行为变更（部署前请知晓）

1. `probe`：exec 试调失败时返回 1（以前恒 0）。
2. `mesh`：任一节点失败返回 1，末尾有汇总行（以前恒 0）。
3. `install.sh`：对外绑定安装无令牌拒绝安装；自设 `--auth-token`
   短于 20 字符拒绝安装（重装且配置已有令牌时不受此限）。
4. `list --json`：默认掩码令牌，需 `--show-tokens` 才全量。
5. `exec-result`：不再调服务端 `exec_result` 工具，改 `exec_poll`
   分页循环（服务端工具保留，供其他客户端用）。
6. `/fleet/discover`：已有 peer 无 `force=true` 时返回 409（原来静默覆盖）。
7. agent 启动：配置损坏拒绝启动（exit 2）；`--generate-token`
   写配置失败拒绝启动（exit 2，原来继续无鉴权跑）；端口占用退出码 3
   （原来直接抛 traceback）。
8. agent `peer_call` 默认超时 130 秒 → 120 秒。

### 未动手（需另定方案）

- install.sh 内嵌副本隔代 stale（内嵌 fleet.py 缺 tailnet 代理分支、
  内嵌 agent 缺 `/fleet/invite` 等），本次不做机械同步；新安装仍用旧内嵌，
  上述修复不会自动进新安装。
- 目录逐节点凭证（改协议，会断现有共享令牌部署）；`http_get` SSRF
  白名单（策略问题）；连接数上限（与 SSE/长轮询超时纠缠）；跳数服务端
  累计（需协议改；中继限流已兜底）；默认改 fail-closed。
- 本仓库 agent 为 v4.5 线，手机跑 v5.2：上述 agent 侧改动需移植到 v5.2
  才对手机生效。
- 看门脚本（`~/workspace/goals/fleet-remote-access/hidden_files/fleet_watch.py`）
  不在本仓库内，其改动已实时生效，不随本次推送走 git。
