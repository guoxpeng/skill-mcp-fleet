# 一页排障 · MCP Fleet v5.2

> 先跑 `fleet doctor`，它会告诉你 80% 的原因。下面是按症状查。

## 1. 安装失败

| 症状 | 原因 | 处理 |
|------|------|------|
| `未找到 python3` | 系统没有 Python | Termux `pkg install python`；OpenWrt `opkg install python3-light python3-ssl`；Debian `apt install python3` |
| `SHA256 校验失败` | 下载损坏/被篡改 | 删除临时包重试；或 `--offline <本地源码>` |
| `健康检查失败` | 端口被占/服务没起来 | `fleet logs --tail 100`；换 `--port 3199`；确认 3100/tcp 未被占用 |
| 权限错误 | 非 root 装到系统目录 | 用 `--dir ~/.mcp-fleet/<name>`；或 `sudo` 安装 |
| Termux 装完很快失联 | Android 14 后台冻结 | 安装器会拉起“忽略电池优化”页，点允许；装 Termux:Boot；`termux-wake-lock` |

## 2. 连不上 / 找不到节点

| 症状 | 检查 | 处理 |
|------|------|------|
| `fleet nodes` 为空 | 是否同网段 | 同网段等 30s UDP 发现；跨网段用邀请直连或 Tailscale |
| 能 ping 不能连 | 防火墙/监听地址 | 放行 3100/tcp；确认进程 `--host 0.0.0.0`；`fleet doctor` |
| 401/403 | token/公钥不匹配 | `fleet pair` 重新配对；确认 `allow_ips`；不要手工改 config |
| 超时 | NAT/对端离线 | 对端 `fleet status`；用带公网地址的邀请；Tailscale |
| 配对后仍 401 | 对端未保存本机公钥 | 重新 `fleet pair`（v5.2 会交换公钥）；看 Dashboard 指纹是否变化 |

## 3. 消息/任务不达

| 症状 | 检查 | 处理 |
|------|------|------|
| 消息一直“待投递” | 对端离线 | 正常：outbox 每 15s 退避重试；对端上线自动补投 |
| Dashboard 死信 > 0 | 超过重试上限 | 对端长期不可达；检查地址/端口；死信保留供审计 |
| Agent 任务卡住 | 任务时间线/Dashboard | `fleet logs`；`fleet_plan` 步骤是否有验收标准；循环上限 8 步 |
| 时间相关失败 | 时钟偏差 | 开启 NTP；签名窗口 ±300s |

## 4. 安全相关

| 症状 | 原因 | 处理 |
|------|------|------|
| 启动提示“自动生成 token” | 对外监听且原来没 token | 正常（v5.2 安全默认）；从配置文件取 token 填入客户端 |
| 命令被策略拒绝 | `rm -rf /`/`mkfs` 等 | 看返回规则 ID；不要绕过；确需执行先人工确认并显式 `confirm=true` |
| `审计链异常` | 日志被改/损坏 | 停止节点，保留现场；用备份审计或重新初始化 |
| 指纹变化告警 | 对端重装/被冒充 | 停止连接，核对 SAS/指纹，重新配对 |
| 公网明文告警 | 未开 TLS | `--tls auto`，或 Tailscale，或仅内网使用 |

## 5. 升级/回滚

```bash
fleet update              # 自动备份 -> 覆盖 -> 重启 -> 健康检查
fleet update --rollback   # 回滚最近一次
ls <install_dir>.bak-*    # 手工查看备份
```

失败时保留 `.failed-*` 目录与备份，先别删。

## 6. 日志与端口位置

| 项 | Linux/macOS | Termux | Windows |
|----|-------------|--------|---------|
| 安装目录 | `/opt/mcp-fleet/<name>` 或 `~/.mcp-fleet/<name>` | `~/.mcp-fleet/<name>` | `%LOCALAPPDATA%\\mcp-fleet\\<name>` |
| 数据 | `<install>/data` | 同左 | 同左 |
| 服务日志 | `<data>/logs/agent.log` | 同左 | 同左 |
| 审计 | `<data>/audit.log` | 同左 | 同左 |
| 端口 | 3100/tcp + 3199/udp | 同左 | 同左 |

## 7. 30 秒诊断顺序

```bash
fleet doctor
fleet status
fleet logs --tail 100
fleet nodes
curl -s http://127.0.0.1:3100/fleet/health
python3 -m pytest tests/ -q     # 开发环境
```

仍无法解决：保留 `fleet doctor` 输出、日志尾部、`fleet_identity` 结果，
在 GitHub 提 Issue（不要贴 token/私钥）。
