# 5 分钟上手 · MCP Fleet v5.2

> 面向第一次使用的普通用户。不需要懂命令行，也不需要读完全部文档。
> 遇到问题看 `troubleshooting.md` 一页排查。

## 1. 它会给你什么

让手机 / NAS / 云主机 / 软路由 / 电脑上的 AI 互相协作：
你在聊天里说一句“看看 NAS 空间”，家里的设备自己执行并回报。
一条命令安装，邀请链接配对，之后全部用自然语言。

## 2. 安装第一台设备

在要当“主控/节点”的设备上执行（Linux / macOS / NAS / Termux / OpenWrt）：

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/scripts/install.sh -o fleet-install.sh
sh fleet-install.sh --name my-nas --port 3100
```

安装器会：

1. 识别系统（systemd / OpenWrt / 群晖 / Termux / macOS / Windows / Docker）；
2. 生成节点身份（Ed25519）与访问 token；
3. 注册开机自启、启动、做健康检查；
4. 打印 **设备码、指纹、token（仅一次）、邀请 URI**。

> 把 token 存进密码管理器。忘了可用 `fleet identity show` 看身份，
> token 在配置文件里（权限 0600）：`grep auth_token <安装目录>/mcp_agent_config.json`。

## 3. 加入第二台设备

在 A 上生成邀请：

```bash
fleet invite --ttl 3600 --qr     # --qr 需要 qrencode；没有就复制 URI
```

把邀请 URI 发到 B，在 B 上执行：

```bash
sh fleet-install.sh --join 'fleet://join/...'
# 已装节点：fleet join 'fleet://join/...'
```

两端会显示 **6 位 SAS 校验码**，一致再确认。配对后即可互相调用。

## 4. 看状态 / 排障

```bash
fleet status          # 节点、指纹、对端、收件箱、运行时长
fleet nodes           # 对端列表、在线、指纹、信任状态
fleet doctor          # 12 项自检（端口/磁盘/时钟/审计/策略/TLS…）
fleet logs --tail 100 # 日志
fleet audit --tail 20 # 审计哈希链
```

Dashboard：浏览器打开 `http://<节点IP>:3100/fleet/ui#<token>`，
可看拓扑、设备、投递队列、Agent 任务时间线，并移除设备。

## 5. 用自然语言使唤它

```bash
fleet ask "帮我看下 NAS 磁盘为什么满了"        # 生成计划 + 可粘给 AI 的提示词
fleet ask "家里网怎么这么慢" --run              # 立即跑只读诊断并汇总证据
```

`--run` 只执行只读命令（df/uptime/ip route 等），不会删改任何东西。
需要变更时，Agent 会先向你确认。

## 6. 升级 / 回滚 / 卸载

```bash
fleet update                 # 备份当前安装目录后升级，健康检查失败可回滚
fleet update --rollback      # 回滚到最近一次备份
fleet identity export --out ~/fleet-identity.json   # 加密备份身份
fleet identity import --from ~/fleet-identity.json  # 换机恢复
```

卸载：

```bash
python3 scripts/fleet_install.py --name my-nas --uninstall          # 保留数据
python3 scripts/fleet_install.py --name my-nas --uninstall --purge  # 连数据一起删
```

## 7. 安全默认（请知悉）

- 默认自动生成访问 token；对外监听时未鉴权会拒绝/告警。
- 危险命令（`rm -rf /`、`mkfs`、`dd of=/dev/*`、关机）默认拒绝或需确认。
- 邮箱默认加密落盘；审计是哈希链，可校验是否被改。
- 公网/跨网段建议开 TLS 或走 Tailscale；`--relay` 配置已预留，反向中继尚未实现。
- 详细威胁模型：`security-model.md`。

## 8. 平台速查

| 设备 | 安装方式 |
|------|---------|
| Linux / 云主机 | `sh fleet-install.sh --name cloud` |
| 飞牛/群晖/威联通/绿联 | `sh fleet-install.sh --name nas`（无 systemd 自动降级） |
| Android / Termux | `sh fleet-install.sh --name phone`；装 Termux:Boot、关电池优化 |
| OpenWrt / 软路由 | `sh fleet-install.sh --name router`（procd） |
| macOS | `python3 scripts/fleet_install.py --name mac` |
| Windows | `python scripts\fleet_install.py --name pc` |
| Docker | `docker compose -f scripts/service-templates/fleet-compose.yml up -d` |

细节与限制见 `platform-matrix.md`。
