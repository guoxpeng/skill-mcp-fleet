# 真机测试结果 · MCP Fleet v5.2.0

> 本文件记录可复现的真实设备验证。测试时间：2026-10-22。
> 自动化用例：**626 passed + 1 skipped**（`python3 -m pytest tests/ -q`）。
> 下列结果来自实际安装/升级/运行，不是单元测试推断。

## 1. 覆盖矩阵

| 平台 | 服务托管 | 版本 | 结果 |
|------|----------|------|------|
| macOS（Apple Silicon） | launchd | 5.1.0 → 5.1.1 → 5.2.0 | ✅ 安装 / 升级 / 回滚 / Dashboard / CLI / 卸载 |
| Ubuntu 24.04.5 LTS（云主机） | systemd 255 | 5.1.1（修复合集） | ✅ 安装 / 自签 TLS / token / 自愈 / 远程更新 / 回滚 / 卸载 |
| Android 14（realme RMX2202） | Termux（wake-lock + 后台进程） | 5.2.0 → 5.2.0 | ✅ 安装 / 默认 token / 电池引导 / CLI / 跨端执行 / 消息 / 自更新 |
| Linux 其他发行版 | systemd/openrc/nohup | — | 🟡 单元测试 + 模板渲染，未真机 |
| OpenWrt / 软路由 | procd | — | 🟡 单元测试 + 模板渲染，未真机 |
| Windows | schtasks | — | 🟡 单元测试 + 模板渲染，未真机 |
| Docker | compose | — | 🟡 静态审查 + 配置修复，未做运行时验证 |

## 2. Ubuntu 24.04 云主机（systemd）

测试方式：SSH 到独立测试机，使用隔离节点名 `sshtest`、端口 3120，测完卸载。

```text
安装: 下载 release v5.1.1 → SHA256 校验通过 → systemd 单元 → 启动 → 健康 OK
单元: /etc/systemd/system/mcp-fleet-sshtest.service
加固: NoNewPrivileges / PrivateTmp / ProtectSystem=strict / ReadWritePaths 白名单
鉴权: 无 token /health=401；带 token=200
TLS : 自签证书，健康检查走 https://127.0.0.1:3120/health
CLI : fleet status / doctor / ask --run 正常（修复了公网 IP 自签证书校验问题）
自愈: kill -9 主进程 → 5s 内 systemd 重启 → 健康 200
更新: fleet update → 下载 v5.1.1 → SHA256 → 停旧服务 → 新进程 → 健康 OK
回滚: fleet update --rollback → 成功
卸载: --uninstall --purge → 启动器/systemd 单元/安装目录/数据目录全部删除，端口释放
```

> 该机器原本有一个 `/opt/vps_mcp` 节点（端口 3100），测试全程未触碰。

## 3. Android 14 / Termux

```text
安装/升级: 5.1.0 → 5.1.1 → 5.2.0，健康 OK
权限: 配置 0600、data 0700、mailbox.key 0600
鉴权: 默认自动生成 token；无 token 请求被拒
全局命令: $PREFIX/bin/fleet（status/doctor/ask/search）
电池: 安装时拉起“忽略电池优化”授权页；termux-wake-lock 失败会明确告警
Dashboard: /fleet/ui=200；无 token /fleet/ui/data=401；带 token=200
跨端: Mac ↔ 手机 peer_call 双向 exit 0；消息在线投递
热更新: fleet update 拉最新 release + SHA256 校验 + 重启成功
```

Termux 限制（已在 `platform-matrix.md` 记录）：
- 未安装 `termux-services` 时用 wake-lock + 后台进程降级，开机自启需 Termux:Boot。
- Android 14 可能冻结后台；安装器会引导关闭电池优化，但系统仍可能杀进程。

## 4. macOS（launchd）

```text
安装: launchd plist + 自签 TLS + 默认 token + 全局 fleet
升级/回滚: 备份 → 覆盖 → 重启 → 健康检查；rollback 恢复备份
Dashboard: 页面/数据接口鉴权正确
卸载: 启动器、plist、安装目录、数据目录按 --purge 清理
```

## 5. 修复过的真实缺陷（真机发现，已合并进 5.1.1）

| 版本 | 缺陷 | 修复 |
|------|------|------|
| 5.1.1 | `fleet update` 升级成默认主机名节点 | 从 config+manifest 解析真实 name/dir/port/service；安装器拒绝目标不匹配 |
| 5.1.1 | 自签 TLS 健康检查失败 | 按 TLS 状态用 https + 自签豁免；TLS 路径写回配置 |
| 5.1.1 | 发布包缺 service templates | 安装器内置 10 个模板兜底 |
| 5.1.1 | 回滚后服务起成 HTTP | 回滚前从配置恢复 TLS/只读/鉴权参数 |
| 5.1.1 | Dashboard 页面 401 | 静态页免鉴权；数据接口仍鉴权 |
| 5.1.1 | 卸载残留启动器/目录 | 删除归属校验的 launcher；`--purge` 清安装目录 |
| 5.1.1 | Termux 启动 `warnings` 未初始化 | 初始化顺序修复 + 回归测试 |
| 5.1.1 | 邮箱密钥迁移覆盖旧密文 | 稳定 mailbox.key + 旧 token 回退 + 失败整体跳过 + 原密文保留 |
| 5.1.1 | Docker 只读根/TLS/data_dir | 去掉只读根、容器 HTTP、`MCP_FLEET_DATA` 覆盖 |
| 5.1.1 | 升级不停旧服务 / 端口误判 | upgrade 先 stop、跳过端口检查 |
| 5.1.1 | 远端 install.sh CDN 缓存 / 无 tempfile | refs 路径 + 补 import + HOME 临时目录 |
| 5.1.1 | raw CDN 两个路径都可能旧缓存 | 按 GitHub API commit SHA 取不可变 URL |
| 5.1.1 | 公网 IP 下本机自签 TLS CLI 失败 | 本机命令显式 insecure；公网对端仍校验证书 |

## 6. 可复现命令

```bash
# 自动化
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q

# 单机
python3 scripts/fleet_install.py --name dev --port 3100 --json
fleet status && fleet doctor && fleet selfcheck

# 跨端
fleet invite --ttl 600
# 对方：sh scripts/install.sh --join 'fleet://join/...'
fleet nodes
fleet exec <对端> 'hostname; df -h'
```

## 7. 未覆盖 / 已知限制

- 中继（relay）仍只是配置预留，反向中继未实现。
- 纯 Python 加密不是常数时间；高对抗场景需审计。
- Windows/OpenWrt/NAS 未真机验证；Docker 未运行时验证。
- 旧手机测试邮箱数据在早期缺陷中损坏（仅测试数据），新版本已防止再次发生。
- iPhone 原生客户端未实现（Termux/Android 可用）。
