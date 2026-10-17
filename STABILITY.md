# Fleet / Tailscale 稳定性重构（2026-10-13）

> 起因：2026-10-12 Tailscale 一晚掉了 4–5 次；手机 tailnet IP 从
> `100.103.118.7` 漂到 `100.81.253.35`，主端登记表还是旧地址，Fleet 跟着断，
> 每次都要人工发现、人工改。老郭要求彻底重构稳定性。

## 一、根因分析

| # | 现象 | 根因 | 位置 |
|---|------|------|------|
| 1 | 节点 IP 漂移后主端永远连不上，只能人工改 | `sync_from_directory` 依赖额外部署的目录服务器（实际没部署）；且 `domain_ok` 把 IP→IP 变更当成"换域名"直接拒绝，Tailscale 漂移**不可能**自动纠正 | `fleet.py` |
| 2 | 主端 POST 失败直接抛错，无重试 | `MCPClient._post` 无重试；只有 `http_probe`（GET /）有退避重试 | `fleet.py` |
| 3 | 手机 Termux 里 agent 被系统杀掉后无人拉起 | Linux 节点有 `_supervisor.sh` + `mcp-watchdog.sh`，手机没有；且手机上的 `fleet` 启动脚本用僵尸 pidfile 谎报"已在运行" | 运维脚本缺失 |
| 4 | `tailscaled` 掉线/掉登录态无人管 | 各节点无保活 | 运维脚本缺失 |
| 5 | 副端实际 IP vs 登记 IP 不一致时无从诊断 | `/health` 不暴露本机 tailnet IP | `mcp_agent.py` |
| 6 | 健康看门 30 分钟才跑一轮 | cron 间隔太长 | cron 配置 |

## 二、改了什么（v3.4）

### 2.1 主端 `fleet.py`

1. **`domain_ok` 放行 tailnet 内 IP 漂移**：同属 `100.64.0.0/10`（或
   `fd7a:115c:a1e0::/48`）的 IP→IP 变更直接通过。真正的鉴权仍由
   「匿名探活 + Bearer token 探活」两道闸门保证，不降低安全性。

2. **Tailscale API 自动纠正 IP（新增，最核心）**：
   - `tailscale_device_ips()`：用 `TAILSCALE_API_KEY` 查控制面
     `GET /api/v2/tailnet/-/devices`，得 `{hostname: [ips]}`。无 key 时
     返回 `None`，调用方静默跳过 —— 不配 key 的老用法零影响。
   - `sync_from_tailnet(name, cur_url, token)`：登记表取 `tailnet` 主机名 →
     API 查当前 IP → 与现地址比对 → 有漂移则**匿名探活 + token 探活**
     双验证通过后回写登记表（`url` / `url_prev` / `tailnet_ip` /
     `synced_at`）。数据源是用户自己的 tailnet 控制面（可信），且必须
     通过 token 鉴权，不会被投毒。
   - `resolve_target` 里自动触发：directory 纠正之后再试 tailnet 纠正。
     **故意不受 `--no-sync` 影响**（健康看门用 `--no-sync` 跑 probe，
     照样能自愈）；关闭：`FLEET_NO_TAILNET_SYNC=1`。
   - 新子命令：`fleet.py tailscale-sync [name]` 手动刷新；
     `fleet.py add --tailnet <主机名>` 登记 MagicDNS 主机名。

3. **幂等调用加重试**：`MCPClient._post(..., retry=True)`，对"连不上/超时"
   重试一次（1s 退避）。`handshake()` / `list_tools()` 启用；`call(exec)`
   等写操作**永远不重试**（避免命令执行两次）。

### 2.2 副端 `mcp_agent.py`（3.3.0 → 3.4.0）

- `/health` 可信分支新增 `tailscale_ip` 字段（本机 `tailscale ip -4`，
  60 秒缓存，失败返回空串）。主端 `probe` 可直接看到"登记 IP vs 实际 IP"，
  漂移诊断不再靠猜。

### 2.3 新增运维脚本

- **`termux-watchdog.sh`**：手机专用保活。零特殊依赖（不用 pgrep/ss/which，
  2026-10-12 实测 Termux 默认没有这些）：pidfile + `kill -0` 判活，
  curl/python 双探活，挂掉自动拉起（`--host 0.0.0.0`），连续失败指数退避
  （60s→300s 封顶）。支持 `--once`（配 Tasker/cron）与常驻两种模式。
  修复了今晚"pidfile 僵尸谎报运行"的坑：启动前先清残留进程。
- **`tailscale-keepalive.sh`**：`tailscaled` 进程 + 登录态双检查，异常时
  `systemctl start tailscaled` + `tailscale up` 自动恢复。掉登录态且是已授
  权机器时 `tailscale up` 无需 key；全新登录才需要 key（`TS_AUTHKEY`
  环境变量传入，不写死在脚本里）。支持 `--once`（配 systemd timer /
  cron）与常驻模式。

### 2.4 健康看门

- cron `fleet-health-watch` 间隔：30 分钟 → **5 分钟**。
- 看门跑 `probe --no-sync`，但 tailnet 纠正不受 `--no-sync` 影响，
  所以 IP 漂移现在**5 分钟内自动自愈**，不再需要人工发现。

## 三、一次性部署（老郭操作）

```bash
# 1. 建 Tailscale API token（devices:read 即可）：
#    https://login.tailscale.com/admin/settings/keys → Generate API access token
export TAILSCALE_API_KEY='tskey-api-...'

# 2. 给 tailnet 节点登记 MagicDNS 主机名（主机名去 Tailscale Admin 面板抄）：
python fleet.py add --name phone    --ip 100.81.253.35  --tailnet <手机主机名> --force
python fleet.py add --name minipc   --ip 100.73.184.5   --tailnet mini-pc      --force
python fleet.py add --name istoreos --ip 100.113.145.90 --tailnet <主机名>     --force
python fleet.py add --name nat      --ip 100.91.169.103 --tailnet <主机名>     --force

# 3. 验证自动纠正：
python fleet.py tailscale-sync

# 4. 手机 Termux 里启动保活（二选一）：
bash termux-watchdog.sh            # 常驻（配合 Termux:Boot 自启）
# 或：定期跑  bash termux-watchdog.sh --once

# 5. 各 Linux 节点加 tailscale 保活（systemd）：
#    bash tailscale-keepalive.sh --once  配 timer 每 5 分钟跑一次，
#    或常驻跑 bash tailscale-keepalive.sh
```

> token 只放环境变量或 `~/.mcp-fleet/nodes.json` 顶层 `tailscale_api_key`
> （文件权限 0600），**不要**进 git、不要贴聊天。

## 四、验证方法

```bash
# 语法/回归
python3 _selftest_fleet.py && python3 _selftest_v31.py && python3 _selftest_directory.py

# tailnet 纠正（ dry-run 逻辑，不写表也可用 --help 看）
python fleet.py tailscale-sync --help

# 模拟 IP 漂移：把登记表里 phone 的 url 改成一个错的 100.x，
# 跑  python fleet.py probe phone  → 应自动纠正回 API 查到的 IP
# （需要 TAILSCALE_API_KEY；无 key 时行为与旧版完全一致）

# 副端 /health（需 token）：
#   应看到 "tailscale_ip": "100.x.x.x", "version": "3.4.0"

# 看门：
#   5 分钟一轮；节点掉线/恢复/被纠正时收到中文通知
```

## 五、没做的（有意）

- **副端断线重连 TCP 会话**：MCP 是无状态 HTTP，每次调用独立建连，
  不存在"长连接断线重连"问题；进程级保活已由 supervisor/watchdog 覆盖。
- **DERP/TCP 强制兜底**：Tailscale 在 UDP 受限时自动降级走 DERP 中继，
  无需配置；keepalive 脚本只保证 daemon 活着、登录态有效。
- **主端沙盒的 tailscale CLI 增强**：沙盒侧是运行时托管的 wrapper，
  不支持 `status --json`，所以主端走**控制面 API** 而不是本地 daemon 查 IP。
