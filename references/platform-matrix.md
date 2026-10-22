# MCP Fleet v5.1.1 平台安装矩阵

> **事实来源**：本文所有命令、默认路径、探测规则与限制，均以仓库内
> `scripts/fleet_install.py`（v5.1.1）的实际实现为准。安装器只依赖 **Python 标准库
> （3.6+）**，不 `pip install` 任何东西，也不会擅自安装系统依赖——缺依赖时只打印
> 对应平台的安装命令。凡本安装器**尚未实现**的能力，本文在「已知限制 / 仍缺平台」
> 中如实列出，不做承诺。

## 0. 读前必读

### 0.1 唯一安装入口

```bash
python3 scripts/fleet_install.py --name <节点名> --port 3100
```

- 命令在**解压后的源码包根目录**执行（`scripts/fleet_install.py` 会自动在
  `--source-dir`、当前目录、脚本所在 `scripts/` 中寻找 `mcp_agent.py` 等节点文件）。
- 如果只有 `fleet_install.py` 一个文件，请补 `--source-dir /path/to/scripts`。
- 仓库根 README/SKILL 里的 `sh fleet-install.sh ...` 引导脚本与安装器参数同名
  （`--name/--port/--dir/--mode/--uninstall`）；**v5.1.1 全部高级参数以本表为准**，
  例如 `--service`、`--print-unit`、`--status`、`--json`、`--dry-run`。

### 0.2 常用参数速查（均来自 `--help` 实现）

| 参数 | 作用 |
|------|------|
| `--name <名>` | 节点显示名；文件/服务名自动 slug 化（中文名加哈希，如 `node-1a2b3c`） |
| `--port <端口>` | HTTP 端口，默认 `3100`（1–65535） |
| `--dir <目录>` / `--install-dir <目录>` | 安装目录（默认见 0.4） |
| `--data-dir <目录>` | 数据目录（默认见 0.4，权限 0700） |
| `--source-dir <目录>` | 节点源码目录（含 `mcp_agent.py`） |
| `--service <模式>` / `--mode <模式>` | `auto/systemd/openrc/procd/launchd/schtasks/termux/docker/nohup` |
| `--print-unit` | 只打印将要写入的服务单元（不落盘） |
| `--dry-run` | 零副作用预演：只打印计划，不建目录、不写文件、不启服务 |
| `--json` | stdout 输出**单行 JSON**（进度/诊断走 stderr），便于脚本解析 |
| `--status [--json]` | 查询安装/服务/健康状态（只读） |
| `--uninstall [--purge]` | 卸载；默认**保留数据目录**，`--purge` 才删 |
| `--no-service` | 只落文件与配置，不写服务单元、不启动（容器/自定义托管用） |
| `--host <地址>` | 监听地址，默认 `0.0.0.0` |
| `--user <用户>` | systemd `User=` / openrc `command_user` / procd user（Windows 计划任务不适用） |
| `--tls auto\|off` | `auto` 尝试用 `fleet_core`+`openssl` 自签证书，失败降级 HTTP；`off` 仅 HTTP |
| `--read-only` / `--require-auth` / `--generate-token` | 写入配置：只读节点 / 强制鉴权 / 自动生成 32 字节 token |
| `--auth-token <串>` / `--allow-ips <a,b>` / `--prefix <串>` / `--relay` / `--sudo-pass <串>` | 写入配置的鉴权、白名单、工具前缀、中继、sudo 口令 |
| `--join 'fleet://join/...'` | 解析主端邀请并写入配置；缺少 `fleet_core` 时安装继续并给出警告 |
| `--unit-dir <目录>` | 自定义服务单元目录（测试/自定义 init 用） |
| `--version` | 打印版本号 |

**旧参数兼容**：`--invite <邀请码>`（`fleet://` 开头会映射成 `--join`）、
`--auto-start`、`--tunnel*`、`--frp-*`、`--ssh-*`、`--directory*`、`--tailnet*`
会被接受；v5.1.1 不再内建隧道/frp/tailnet，仅在结果 `warnings` 中提示改用中继工具或系统级隧道。

### 0.3 退出码与 JSON

- 退出码：`0` 成功；`1` 安装/卸载/状态查询失败（可预期错误进 `errors`）；`2` 参数错误。
- `--json` 保证 **stdout 只有一行**、UTF-8 原样中文、无多余空格；日志一律写 stderr。
- `--dry-run` 的 actions 固定为：
  `precheck → dirs → copy → config → identity → tls → service → start → health → report`；
  `--no-service` 会把 `service/start/health` 标记为 `skipped`。

### 0.4 默认路径（可用 `--dir/--data-dir` 覆盖）

| 场景 | 安装目录 | 数据目录 |
|------|----------|----------|
| Linux **root** | `/opt/mcp-fleet/<slug>` | `/var/lib/mcp-fleet/<slug>` |
| Linux **非 root**（含 Termux） | `~/.mcp-fleet/<slug>` | `<安装目录>/data` |
| macOS **root** | `/usr/local/var/mcp-fleet/<slug>` | `<安装目录>/data` |
| macOS **非 root** | `~/.mcp-fleet/<slug>` | `<安装目录>/data` |
| Windows | `%LOCALAPPDATA%\mcp-fleet\<slug>` | `<安装目录>\data` |
| 容器内（默认降级 nohup） | 按当前用户规则 | `<安装目录>/data` |

服务/单元名：systemd/openrc/procd/termux/docker 用 `mcp-fleet-<slug>`；
launchd 用 `com.mcpfleet.node.<slug>`；Windows 计划任务用 `MCPFleet-<slug>`。

### 0.5 安装器写了什么、权限如何

- 安装目录：`mcp_agent.py`、`fleet_proto.py`、`dashboard_html.py`
  （若源码包存在则带 `fleet_core.py`、`fleet_agent.py`、示例配置）→ 0644；
- `mcp_agent_config.json` → **0600**（合并写入，保留已有 `auth_token`；旧文件损坏会先
  备份成 `*.broken.<时间戳>` 再重建）；
- 数据目录 0700：`manifest.json`（0600 安装记录）、`identity.json`（0600，`fleet_core`
  可用时）、`logs/agent.log`；
- 安装目录里的 `.fleet-manifest.json` 指针（0600），供卸载定位；
- 服务单元（systemd 0644；openrc/procd/termux/nohup 0755；launchd/schtasks 0644）。

### 0.6 通用验证与卸载

```bash
# 验证（任何平台）
curl -s http://127.0.0.1:3100/health                  # 期望 {"status":"ok",...}
python3 scripts/fleet_install.py --name <名> --status --json
# 卸载（默认保留数据目录，--purge 才删）
python3 scripts/fleet_install.py --name <名> --uninstall
python3 scripts/fleet_install.py --name <名> --uninstall --purge
```

> **卸载安全**：只删除 manifest 里记录的、且属于本安装器的路径；默认保留数据目录；
> 显式拒绝 `/`、`$HOME`、`/usr`、`/etc`、`/var`、`/opt`、`/tmp` 等危险路径；
> 拒绝删除非本安装器生成的服务单元；`--purge` 时若 manifest 记录的数据目录与当前
> 解析不一致会拒绝删除。自定义过 `--dir/--data-dir` 的节点，卸载时要带同样的参数。

### 0.7 依赖自检

```bash
sh scripts/service-templates/check-deps.sh   # 只检查不安装，缺什么给对应安装命令
```

---

## 1. 平台支持总表

| 平台 | 探测依据 | 默认服务管理器 | 开机自启方式 | 状态 |
|------|----------|----------------|--------------|------|
| Android / Termux | `$PREFIX` 含 `com.termux` 或 Termux 的 sh 路径 | `termux`（`sv` + 唤醒锁） | `sv-enable` + Termux:Boot 插件 | 支持（有前提） |
| 飞牛 fnOS | `/etc/fnos-release`、`/etc/fnos_version`、`/usr/trim` 或 os-release 含 fnos | `nohup` 降级 | fnOS 任务计划（开机任务） | 支持（手工自启） |
| 群晖 DSM | `/etc/synoinfo.conf` | `nohup` 降级 | DSM 任务计划（触发的任务→开机时） | 支持（手工自启） |
| 威联通 QNAP | `/etc/config/uLinux.conf`、`/sbin/getcfg` | `nohup` 降级 | 控制台开机自动启动 / autorun.sh | 支持（手工自启） |
| 绿联 UGREEN | `/etc/ugreen`、`/usr/sbin/ugreen` | `nohup` 降级 | UGREEN 任务计划（开机任务） | 支持（手工自启） |
| OpenWrt / 软路由 | `/etc/openwrt_release`、`/sbin/procd` | `procd` | `/etc/init.d/<label> enable` + respawn | 支持 |
| Debian / Ubuntu | os-release + systemd | `systemd` | `systemctl enable`（用户级需 linger） | 支持 |
| Alpine | `/etc/alpine-release` + openrc | `openrc` | `rc-update add ... default` + supervise-daemon | 支持 |
| macOS | `sys.platform=darwin` | `launchd` | LaunchAgent/Daemon：RunAtLoad + KeepAlive | 支持 |
| Windows | `os.name=nt` | `schtasks` | 计划任务（BootTrigger/LogonTrigger + 失败重拉） | 支持（需管理员） |
| Docker | `/.dockerenv` 或 cgroup 标记 | `nohup`（建议显式 `--mode docker`） | compose `restart: unless-stopped` | 支持 |
| 华为云/阿里云/腾讯云 ECS | 同 Linux | 同 Linux（通常 `systemd`） | 同 systemd | 支持 |

`--service auto`（默认）会选表中「默认服务管理器」；想固定行为请显式传 `--service`。

---

## 2. Android / Termux

**探测**：`PREFIX` 含 `com.termux`，或存在 Termux 的 `/data/data/com.termux/files/usr/bin/sh`
→ `os=linux`、`distro=termux`、`init=termux`、`service_manager=termux`。

```bash
# 一条命令
python3 scripts/fleet_install.py --name phone --port 3100
```

- **前置依赖**
  - Python：`pkg update && pkg install python`（用 `python3 -V` 确认 ≥ 3.6）。
  - sv 托管：`pkg install termux-services`（安装器检测不到时降级为
    `termux-wake-lock` + 后台进程，并打印警告）。
  - 开机自启：从 F-Droid 安装 **Termux:Boot** 插件。
- **开机自启**：安装器把脚本写到
  `$PREFIX/var/service/mcp-fleet-<slug>/run`，执行 `termux-wake-lock`、
  `sv-enable`、`sv up`；Android 重启后由 Termux:Boot 拉起（把启动命令放进
  `~/.termux/boot/`）。唤醒锁用于防止后台进程被系统冻结。
- **验证**：`sv status mcp-fleet-<slug>`；无 sv 时
  `python3 scripts/fleet_install.py --name phone --status`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name phone --uninstall`（停 sv、
  释放唤醒锁、删服务目录，默认保留 `~/.mcp-fleet/<slug>/data`）；
  彻底清：加 `--purge`。
- **已知限制**：非 root、无 systemd；Android 省电策略可能仍杀掉节点；sv 依赖
  termux-services；Termux:Boot 不装就没有真正的开机自启；默认只监听局域网地址，
  公网访问需自建隧道或中继（安装器不再内建隧道）。

## 3. 飞牛 fnOS

**探测**：`/etc/fnos-release`、`/etc/fnos_version`、`/usr/trim` 任一存在，或
os-release 的 `NAME/ID` 含 `fnos` → `nas_vendor=fnos`，默认 `nohup` 降级。

```bash
# 一条命令（root SSH）
python3 scripts/fleet_install.py --name fnos --port 3100
```

- **前置依赖**：Python 3.6+（系统应用中心/自带；`python3 -V` 确认）。安装器对未知
  发行版只提示「请用系统包管理器安装 python3」，不会自动装。
- **开机自启**：安装器无法写 fnOS 的任务计划，会在结果的 `next_steps` 里给出提示：
  系统设置 → 任务计划 → 新增开机任务，命令：
  `sh /opt/mcp-fleet/<slug>/fleet-ctl.sh start`
  （root 安装；非 root 为 `~/.mcp-fleet/<slug>/fleet-ctl.sh`）。
- **验证**：`sh /opt/mcp-fleet/<slug>/fleet-ctl.sh status`；
  `python3 scripts/fleet_install.py --name fnos --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name fnos --uninstall`；再到任务计划里
  删除对应开机任务。
- **已知限制**：无 systemd，服务靠 `fleet-ctl.sh` + nohup 守护（进程崩溃不会自动
  重拉，可用 cron `@reboot` 或任务计划兜底）；系统升级可能改动 `/opt`，建议用
  `--dir /<数据盘>/mcp-fleet/<slug> --data-dir /<数据盘>/mcp-fleet-data/<slug>`
  指到持久盘。

## 4. 群晖 DSM

**探测**：存在 `/etc/synoinfo.conf` → `distro=synology`、`nas_vendor=synology`，
默认 `nohup` 降级。

```bash
# 一条命令（root 或 sudo）
sudo python3 scripts/fleet_install.py --name synology --port 3100
```

- **前置依赖**：套件中心安装 Python 3（DSM 7 的 Python 3.x），`python3 -V` ≥ 3.6；
  先在「控制面板 → 终端机和 SNMP」开启 SSH。
- **开机自启**：控制面板 → 任务计划 → 新增 → **触发的任务** → **开机时** →
  运行命令：`sh /opt/mcp-fleet/<slug>/fleet-ctl.sh start`（root 安装路径）。
- **验证**：`sh /opt/mcp-fleet/<slug>/fleet-ctl.sh status`；
  `python3 scripts/fleet_install.py --name synology --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`sudo python3 scripts/fleet_install.py --name synology --uninstall`；
  再删除任务计划条目（安装器不会改 DSM 的任务计划）。
- **已知限制**：无 systemd；DSM 大版本升级可能重置系统分区里的 `/opt`、`/var`，
  建议 `--dir /volume1/mcp-fleet/<slug> --data-dir /volume1/mcp-fleet-data/<slug>`；
  默认禁用 root SSH 的机型请全程用 `sudo`；进程崩溃需靠任务计划/`cron @reboot` 兜底。

## 5. 威联通 QNAP

**探测**：存在 `/etc/config/uLinux.conf` 或 `/sbin/getcfg` → `nas_vendor=qnap`，
默认 `nohup` 降级。

```bash
# 一条命令（root SSH）
python3 scripts/fleet_install.py --name qnap --port 3100
```

- **前置依赖**：App Center 安装 Python 3（`python3 -V` ≥ 3.6）；开启 SSH。
- **开机自启**：控制台 → 系统 → 硬件 → 开机自动启动（autorun.sh），加入：
  `sh /opt/mcp-fleet/<slug>/fleet-ctl.sh start`；或在任务计划里建开机任务。
- **验证**：`sh /opt/mcp-fleet/<slug>/fleet-ctl.sh status`；
  `python3 scripts/fleet_install.py --name qnap --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name qnap --uninstall`；并从 autorun /
  任务计划里删除启动行。
- **已知限制**：无 systemd；部分型号的 autorun.sh 需在控制台显式启用，固件升级可能
  重置；建议把 `--dir/--data-dir` 指到数据卷（如 `/share/CACHEDEV1_DATA/...`）。

## 6. 绿联 UGREEN

**探测**：存在 `/etc/ugreen` 或 `/usr/sbin/ugreen` → `nas_vendor=ugreen`，
默认 `nohup` 降级。

```bash
# 一条命令（root SSH）
python3 scripts/fleet_install.py --name ugreen --port 3100
```

- **前置依赖**：系统自带/应用中心安装 Python 3（`python3 -V` ≥ 3.6）；在设置里开启 SSH。
- **开机自启**：控制面板 → 任务计划 → 新增开机任务，命令：
  `sh /opt/mcp-fleet/<slug>/fleet-ctl.sh start`。
- **验证**：`sh /opt/mcp-fleet/<slug>/fleet-ctl.sh status`；
  `python3 scripts/fleet_install.py --name ugreen --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name ugreen --uninstall`；删除任务计划条目。
- **已知限制**：无 systemd；固件升级可能重置系统目录，建议 `--dir/--data-dir`
  指到存储卷；不自带进程崩溃重拉（靠 nohup，必要时加 cron 保活）。

## 7. OpenWrt / 软路由

**探测**：存在 `/etc/openwrt_release` 或 `/sbin/procd` → `distro=openwrt`、
`init=procd`、`service_manager=procd`。

```bash
# 一条命令（root，软路由通常是 root）
python3 scripts/fleet_install.py --name router --port 3100
```

- **前置依赖**：`opkg update && opkg install python3`；精简固件若 Python 功能不全，
  可装 `python3-light` + `python3-urllib`（`check-deps.sh` 给出的建议）。
  建议 `python3 -V` 确认 ≥ 3.6。
- **开机自启**：安装器写 `/etc/init.d/mcp-fleet-<slug>`（procd，`respawn` 自动重启），
  并执行 `/etc/init.d/<label> enable` + `restart`。兜底可把
  `sh <安装目录>/fleet-ctl.sh start` 写进 `/etc/rc.local`（`exit 0` 之前）。
- **验证**：`/etc/init.d/mcp-fleet-<slug> status`；
  `python3 scripts/fleet_install.py --name router --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name router --uninstall`
  （先 stop + disable 再删单元，默认保留数据目录）。
- **已知限制**：需 root；`/overlay` 空间小，建议 `--data-dir /mnt/<盘>/mcp-fleet-data/<slug>`；
  部分固件 Python 包版本低于 3.6；MIPS 机型若改用 systemd 加固项（本机是 procd，
  不受影响）；无 systemd。

## 8. Debian / Ubuntu

**探测**：os-release `ID=debian/ubuntu` + PID 1 为 systemd（或 `/run/systemd/system`
存在）→ `service_manager=systemd`。

```bash
# 一条命令（root 安装：/opt + /etc/systemd/system）
sudo python3 scripts/fleet_install.py --name app --port 3100
# 或非 root 安装（~/.mcp-fleet + systemd 用户级实例）
python3 scripts/fleet_install.py --name app --port 3100
```

- **前置依赖**：`apt-get update && apt-get install -y python3`（≥3.6；Ubuntu 20.04+
  自带 3.8+）；`sudo`（root 安装时）。
- **开机自启**：root 安装 → `/etc/systemd/system/mcp-fleet-<slug>.service`，安装器执行
  `systemctl daemon-reload`、`enable`、`restart`；非 root 安装 → 用户级
  `~/.config/systemd/user/mcp-fleet-<slug>.service`，还需要
  `loginctl enable-linger $USER` 才能在未登录时自启（安装器会提示）。
- **验证**：`systemctl status mcp-fleet-<slug>`（非 root 加 `--user`）；
  `curl -s http://127.0.0.1:3100/health`；
  `python3 scripts/fleet_install.py --name app --status --json`。
- **卸载**：`sudo python3 scripts/fleet_install.py --name app --uninstall`
  （stop + disable + 删单元；默认保留 `/var/lib/mcp-fleet/<slug>`，`--purge` 才删）。
- **已知限制**：`ProtectSystem=strict` + `ReadWritePaths` 只允许写安装/数据/日志目录，
  若节点要写其他路径需自行改单元；非 root 自启依赖 linger；`--tls auto` 需要
  `openssl`，否则降级 HTTP。

## 9. Alpine

**探测**：存在 `/etc/alpine-release` + openrc → `distro=alpine`、`init=openrc`、
`service_manager=openrc`。

```bash
# 一条命令（root）
python3 scripts/fleet_install.py --name alpine --port 3100
```

- **前置依赖**：`apk add python3`；openrc 为默认 init。
- **开机自启**：安装器写 `/etc/init.d/mcp-fleet-<slug>`，执行
  `rc-update add mcp-fleet-<slug> default` + `rc-service ... restart`；
  模板用 `supervise-daemon`（`respawn_delay=3`、`respawn_max=0`）实现崩溃重拉。
- **验证**：`rc-service mcp-fleet-<slug> status`；
  `python3 scripts/fleet_install.py --name alpine --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name alpine --uninstall`
  （`rc-service stop` + `rc-update del`）。
- **已知限制**：写 `/etc/init.d` 需要 root；musl 环境；`--user` 会映射为
  `command_user`，需该用户对安装/数据目录有权限；无 systemd。

## 10. macOS

**探测**：`sys.platform == darwin` → `init=launchd`、`service_manager=launchd`。

```bash
# 一条命令（普通用户：~/Library/LaunchAgents）
python3 scripts/fleet_install.py --name mac-mini --port 3100
# root：/Library/LaunchDaemons（真正的开机自启）
sudo python3 scripts/fleet_install.py --name mac-mini --port 3100
```

- **前置依赖**：系统自带 `python3`（`xcode-select --install`），或
  `brew install python3`；`python3 -V` ≥ 3.6。
- **开机自启**：生成 `com.mcpfleet.node.<slug>.plist`（`RunAtLoad=true`、
  `KeepAlive=true`、`ThrottleInterval=10`、`Umask=63`），安装器执行
  `launchctl unload -w <plist>` + `launchctl load -w <plist>`。
  root 装到 `/Library/LaunchDaemons` 才是登录前启动；普通用户装到
  `~/Library/LaunchAgents` 只在该用户登录后启动。
- **验证**：`launchctl list | grep com.mcpfleet.node.<slug>`；
  `python3 scripts/fleet_install.py --name mac-mini --status --json`；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`python3 scripts/fleet_install.py --name mac-mini --uninstall`
  （`launchctl unload -w` 并删 plist；数据目录默认保留）。
- **已知限制**：新版 macOS 上 `launchctl load -w` 可能失败，安装器会提示改用
  `sudo` 或 `launchctl bootstrap`；GUI 会话级 LaunchAgent 不随开机（需登录）；
  `exec` 类工具访问受保护目录时仍受 TCC 限制；非 root 安装数据目录随安装目录。

## 11. Windows

**探测**：`os.name == nt` → `init=schtasks`、`service_manager=schtasks`。Windows 上
`is_root` 恒为 False（无 `geteuid`），因此默认装到用户目录、数据目录在安装目录内。

```powershell
# 一条命令（管理员 PowerShell）
python scripts\fleet_install.py --name office-pc --port 3100
```

- **前置依赖**：Python 3.6+（`winget install python3` 或 python.org 安装包，勾选
  “Add python.exe to PATH”）；注册计划任务需要**管理员权限**。
- **开机自启**：生成 `fleet-<slug>.xml`（`BootTrigger` + `LogonTrigger`、
  `MultipleInstancesPolicy=IgnoreNew`、`RestartOnFailure`、`RunLevel=HighestAvailable`），
  安装器执行 `schtasks /Create /F /TN MCPFleet-<slug> /XML <xml>` + `/Run`。
- **验证**：`schtasks /Query /TN MCPFleet-<slug>`；
  `python scripts\fleet_install.py --name office-pc --status --json`；
  `Invoke-RestMethod http://127.0.0.1:3100/health`。
- **卸载**：`python scripts\fleet_install.py --name office-pc --uninstall`
  （`schtasks /End` + `/Delete`；数据目录默认保留，`--purge` 才删）。
- **已知限制**：非管理员注册会失败并打印手工命令；计划任务以**交互用户**运行，
  该用户未登录时可能不启动；没有 Windows 服务（NSSM）集成；`--user` 对 schtasks
  不生效；没有 `chmod`（权限由 NTFS ACL 决定）；`--tls auto` 依赖 openssl 可用性。

## 12. Docker

**探测**：`/.dockerenv`、`/run/.containerenv`，或 `/proc/1/cgroup` 含
`docker/kubepods/containerd/lxc/podman/garden/buildkit` → `is_container=true`，
默认降级 `nohup` 并提示「建议 --mode docker 用 restart 策略保证自启」。

```bash
# 一条命令（显式 docker 适配器）
python3 scripts/fleet_install.py --name docker-node --port 3100 --mode docker
```

- **前置依赖**：Docker Engine + Compose（`docker compose` 或兼容的
  `docker-compose`）；构建镜像需要网络（`python:3.12-alpine` + `apk add tini curl`）。
- **开机自启**：生成 `fleet-compose.yml` + `Dockerfile`，compose 里
  `restart: unless-stopped`，宿主机 Docker 服务自启即可；安装器在 Docker 可用时
  自动 `docker compose -f ... up -d`，不可用时打印手工命令。
- **验证**：`docker compose -f <安装目录>/fleet-compose.yml ps`；
  `docker ps --filter name=mcp-fleet-<slug>`；compose 内置 `/health` healthcheck；
  `curl -s http://127.0.0.1:3100/health`。
- **卸载**：`docker compose -f <安装目录>/fleet-compose.yml down`
  （`--uninstall` 会先尝试 compose down），再
  `python3 scripts/fleet_install.py --name docker-node --uninstall`。
- **已知限制**：compose 固定 `read_only: true` + `cap_drop: ALL` +
  `no-new-privileges`，节点内 `exec` 类工具能力受限；容器内默认没有 systemd；
  端口映射与 `--port` 一一对应；compose 把 `DATA_DIR`/`INSTALL_DIR` 挂进容器
  （`/data`、`/app`），`--purge` 才会删数据目录；没有被编排平台（Swarm/K8s）清单。

## 13. 华为云 ECS / 阿里云 ECS / 腾讯云 ECS

三家云主机的安装方式与 Linux 完全一致，差异主要在**镜像自带 Python 版本**和
**安全组放行**。以下命令以 Ubuntu/Debian 镜像、root 安装为例。

### 13.1 华为云 ECS

```bash
# 一条命令（root）
sudo python3 scripts/fleet_install.py --name huawei-ecs --port 3100
```

- **前置依赖**：`apt-get update && apt-get install -y python3`；`sudo`；
  安全组入方向放行 **TCP 3100**。
- **开机自启**：systemd（`/etc/systemd/system/mcp-fleet-huawei-ecs.service`，
  `systemctl enable` 由安装器完成）。
- **验证**：`systemctl status mcp-fleet-huawei-ecs`；
  `curl -s http://127.0.0.1:3100/health`；
  `python3 scripts/fleet_install.py --name huawei-ecs --status --json`。
- **卸载**：`sudo python3 scripts/fleet_install.py --name huawei-ecs --uninstall`。
- **已知限制**：公网暴露必须配合 `--require-auth --generate-token`（或
  `--auth-token`/TLS），否则等于裸奔；`--tls auto` 需要 openssl；多网卡/弹性 IP
  场景下邀请 URI 里的主机取本机出口 IP，需要时在主端手工核对。

### 13.2 阿里云 ECS

```bash
# 一条命令（root）
sudo python3 scripts/fleet_install.py --name aliyun-ecs --port 3100
```

- **前置依赖**：`apt-get update && apt-get install -y python3`（Alibaba Cloud Linux/
  CentOS 系用 `dnf install -y python3`）；安全组放行 **TCP 3100**。
- **开机自启**：systemd，同上。
- **验证**：`systemctl status mcp-fleet-aliyun-ecs`；
  `curl -s http://127.0.0.1:3100/health`；`--status --json`。
- **卸载**：`sudo python3 scripts/fleet_install.py --name aliyun-ecs --uninstall`。
- **已知限制**：同上；Alibaba Cloud Linux 请用 `dnf` 装 Python；实例重启后 systemd
  自动拉起，但安全组变更不会自动同步。

### 13.3 腾讯云 ECS

```bash
# 一条命令（root）
sudo python3 scripts/fleet_install.py --name tencent-ecs --port 3100
```

- **前置依赖**：`apt-get update && apt-get install -y python3`（TencentOS/CentOS 系
  用 `dnf install -y python3`）；安全组/防火墙放行 **TCP 3100**。
- **开机自启**：systemd，同上。
- **验证**：`systemctl status mcp-fleet-tencent-ecs`；
  `curl -s http://127.0.0.1:3100/health`；`--status --json`。
- **卸载**：`sudo python3 scripts/fleet_install.py --name tencent-ecs --uninstall`。
- **已知限制**：同上；部分轻量应用服务器镜像默认无 `ufw`/防火墙差异，按控制台为准；
  公网暴露务必开鉴权或 TLS。

---

## 14. 常见问题与排查顺序

1. **先看安装器输出**：`--dry-run --json` 看计划与 warnings；
   正式安装后 `--status --json` 看 `installed/running/healthy`。
2. **依赖缺失**：`sh scripts/service-templates/check-deps.sh`；安装器只给命令不代装。
3. **端口被占**：`--port <新端口>`；`--dry-run` 时只提示，正式安装会报错。
4. **服务起不来**：看数据目录 `logs/agent.log`；`--print-unit` 对比单元内容；
   systemd 用 `systemctl status`/`journalctl -u`，openrc 用 `rc-service status`，
   launchd 用 `launchctl list`，Windows 用 `schtasks /Query`，容器用
   `docker compose logs`。
5. **没有 fleet_core**：安装继续、节点可运行，但没有 Ed25519 身份与邀请 URI，
   `warnings` 里会明确写；补全源码包后重跑即可。
6. **卸载找不到记录**：用与安装时相同的 `--name/--dir/--data-dir` 再执行；
   安装记录在数据目录 `manifest.json`，指针在安装目录 `.fleet-manifest.json`。

## 15. 仍缺的平台 / 尚未实现的能力

以下内容**当前安装器未支持**，不要按「已支持」宣传：

- **FreeBSD / TrueNAS / 其它 BSD 的 rc.d**：无对应适配器，只会降级到 `nohup`；
  `_detect_init` 不识别 BSD rc.d。
- **Kubernetes / Helm / Docker Swarm**：只有单机 `docker compose`，没有编排清单。
- **Windows 服务（NSSM）**：契约里提过可选 NSSM，代码未实现；只有计划任务。
- **systemd `WatchdogSec`**：模板变量预留为空，未真正启用看门狗。
- **未识别的 NAS 品牌**（如华芸 Asustor、铁威马 TerraMaster、极空间 Zspace 等）：
  探测不到厂商标记 → 走通用 `nohup` + `crontab @reboot` 提示，需要用户手工加自启。
- **群晖 DSM / 飞牛 / 威联通 / 绿联的原生服务框架**：安装器不写各厂商的服务注册，
  只给任务计划命令；开机自启必须人工在 UI 里加一次。
- **Termux 开机自启自动化**：依赖用户安装 Termux:Boot 并自行放置启动命令。
- **TLS 证书自动续期 / ACME**：仅支持 `fleet_core` + `openssl` 自签，不接 Let's Encrypt。
- **IPv6-only / 纯代理环境**：健康检查强制禁代理，邀请 URI 的地址探测走 IPv4
  （UDP connect 8.8.8.8），IPv6-only 环境需手工核对地址。
- **`--advertise-host` 等库级参数没有 CLI**：`advertise_host` 只能通过库 API 传入，
  CLI 不可用。

---

## 16. 真机验证记录（2026-10-22）

在真实 Android 设备上完成安装、启动、配对与跨端调用，不是模拟器。

| 项 | 值 |
|----|----|
| 设备 | Realme RMX2202（arm64-v8a） |
| 系统 | Android 14（SDK 34） |
| 环境 | Termux 0.119.1 + Python 3.14.6（`PREFIX=/data/data/com.termux/files/usr`） |
| 安装命令 | `python3 scripts/fleet_install.py --name phone-v6 --port 3101 --json` |
| 平台识别 | `os=android, distro=termux, init=termux, service_manager=termux` |
| 守护方式 | `termux-wake-lock` + 后台进程（未装 termux-services 时自动降级） |
| 节点身份 | `uc24ehjgwtilftqp` / 指纹 `A0B5-C21D-26B4-D0B2`（Ed25519，S2 信任） |
| 安装耗时 | 约 3 秒（不含下载） |

验证结果：

1. `GET /health`、`/fleet/identity`、`/fleet/metrics` 均正常；MCP `initialize`
   返回 2660 字符 instructions 与 7 个 prompts；`tools/list` 53 个工具。
2. Mac 节点（macOS，`fleet_install.py --no-service`）与手机节点
   `fleet pair` 成功，双方交换公钥并显示一致 SAS `497534`。
3. 双向执行：Mac→手机 `echo hello-from-phone; uname -m` → `hello-from-phone/aarch64`；
   手机→Mac `echo hello-from-mac; sw_vers -productVersion` → `hello-from-mac/27.0.1`。
4. 邮箱：Mac `msg_send` → 手机 `msg_recv` 拉到 `hello via mailbox`。
5. 安全：手机节点对 Mac 的 Ed25519 签名请求放行；同一 nonce 重放返回 401；
   篡改签名返回 401；`fleet_audit_verify` 返回 `ok=true`。
6. 策略：`rm -rf /`、`mkfs.ext4 /dev/sda1` 被拒绝并给出规则 ID；`reboot` 需
   `confirm=true`；普通 `echo` 正常。

### Android 14 注意事项（实测踩坑）

- **后台冻结**：Termux 不在前台时，Android 14 可能冻结进程，表现为端口能连但
  超时。解决：安装后执行 `termux-wake-lock`（安装器已自动调用），并在系统设置中
  关闭 Termux 电池优化；长期运行建议装 Termux:Boot。
- **没有 `sv`**：未装 `termux-services` 时安装器自动降级为唤醒锁 + 后台进程，
  开机自启不可用；`pkg install termux-services` 后重跑安装即可用 `sv` 托管。
- **`openssl` 缺失**：Termux 默认可能没有 openssl，`--tls auto` 会降级为 HTTP
  并提示；公网场景请先 `pkg install openssl` 或叠加 Tailscale/中继。
- **存储权限**：Termux 访问 `/sdcard` 需 `termux-setup-storage`；节点自身数据
  放在应用私有目录，不需要外部存储权限。

---

## 17. v5.1.1 安装体验变化（所有平台通用）

- **默认鉴权**：安装时自动生成 `auth_token` 并写入配置（0600），人读输出只显示一次；
  `--no-auth` 才关闭。首次启动时若对外监听且无 token，节点也会自动生成（`auto_secure`）。
- **全局命令**：安装器创建 `fleet`（Linux/macOS：`/usr/local/bin` 或 `~/.local/bin`；
  Termux：`$PREFIX/bin`；Windows：安装目录 `fleet.cmd`），之后直接 `fleet status`。
- **升级/回滚**：`fleet update` / `fleet update --rollback`，安装器 `--upgrade/--rollback`，
  自动备份安装目录（含 mailbox/config）并在失败时保留现场。
- **Android 14**：安装时尝试拉起「忽略电池优化」授权页；请点允许，并安装 Termux:Boot。
- **发布校验**：`scripts/release.py` 创建 Release 并上传 zip + SHA256SUMS；
  `install.sh` 远程安装会校验 SHA256。
