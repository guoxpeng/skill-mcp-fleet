# MCP 副端 Agent · 安装包

给你的设备集群（NAS / 飞牛 OS / 树莓派 / 云主机 / 容器…）装一个「副端」，即可被 WorkBuddy 主端远程操控。

## 一、这是什么

```
主端（你的 Windows 电脑，装 WorkBuddy）
  └── ~/.workbuddy/mcp.json
        ├── nas      → http://192.168.1.10:3100/mcp   副端·远程
        └── fnos     → http://192.168.1.11:3100/mcp   副端·远程（待装）
              │
副端（各台 Linux 设备，各跑一个 mcp_agent.py，开机自启）
  └── 命令在设备本机执行，不经 SSH —— 更快、更稳
```

**副端 12 个工具**（主端看到的工具名 = `<服务名>__<工具名>`，如 `nas__exec`）：

| 工具 | 作用 |
|---|---|
| `exec` | 执行 shell 命令，`sudo:true` 可提权 |
| `read` / `write` / `edit` | 读文件（可按行）、写/追加、文本块替换 |
| `list_dir` | 列目录 |
| `docker_ps` / `docker_logs` / `docker_restart` | 容器列表 / 日志 / 重启 |
| `systemctl` / `service_logs` | 服务启停状态 / 服务日志（journalctl） |
| `sysinfo` | 主机/负载/内存/磁盘/CPU TOP5 |
| `http_get` | 在设备本机发 HTTP（测本机服务） |

## 二、下载并安装（在**副设备**上）

### 方式 A：两条命令（推荐，最稳）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/mcp-fleet/main/install.sh -o install.sh
sudo bash install.sh --name fnos --port 3100
```

### 方式 B：一条命令（下载即装，需当前为 root）

```bash
curl -fsSL https://raw.githubusercontent.com/guoxpeng/mcp-fleet/main/install.sh | sudo bash -s -- --name fnos --port 3100
```

> 管道方式下脚本自己会用 `sudo bash -s --` 承接参数；若当前用户非 root 且未加 `sudo`，脚本会给出明确提示而不是卡住。

### 下载不到 github 时换镜像（任选一条，内容完全一致）

```bash
# jsDelivr CDN
curl -fsSL https://cdn.jsdelivr.net/gh/guoxpeng/mcp-fleet@main/install.sh -o install.sh

# ghfast.top 加速
curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/guoxpeng/mcp-fleet/main/install.sh -o install.sh

# ghproxy.net 加速
curl -fsSL https://ghproxy.net/https://raw.githubusercontent.com/guoxpeng/mcp-fleet/main/install.sh -o install.sh

# gh-proxy.com 加速
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/guoxpeng/mcp-fleet/main/install.sh -o install.sh

# GitHub API（返回 base64，大陆通常可达；公开仓库可省略 Token）
curl -fsSL https://api.github.com/repos/guoxpeng/mcp-fleet/contents/install.sh \
  | python3 -c "import sys,json,base64;open('install.sh','wb').write(base64.b64decode(json.load(sys.stdin)['content']))"

# 下载后照方式 A 安装
sudo bash install.sh --name fnos --port 3100
```

各通道内容一致，MD5 `482310d6e15d84ad9f78591bba0b7fb7`（77092 B，v3.0）。
下载后建议核对一遍：`md5sum install.sh`。

看到 `服务状态: active`（systemd 机型）或 `已启动`（容器机型）加 `本机自检通过` 就成了。

### 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--name` | 副端标识（决定服务名、主端里的服务名） | 主机名 |
| `--port` | 监听端口 | `3100` |
| `--dir` | 安装目录 | `/opt/<name>_mcp` |
| `--prefix` | 工具名前缀（一般不用填） | 空 |
| `--sudo-pass` | 需要 sudo 提权时写入配置的密码 | 空（自动用免密 sudo） |
| `--mode` | 托管方式：`auto` / `systemd` / `nohup` | `auto` |
| `--tunnel` | 装完顺带起一条公网隧道（云容器/无内网入口时用） | 关 |
| `--auth-token` | 访问令牌（Bearer）。留空=不鉴权；开隧道时脚本会自动补一个 | 空 |
| `--allow-ips` | IP/CIDR 白名单，逗号分隔，如 `'10.0.0.0/8,203.0.113.7'` | 空（不限来源） |
| `--directory` | 地址目录地址，如 `http://192.168.1.10:8790`；填了保活守护会上报隧道地址 | 空 |
| `--directory-token` | 地址目录的 `X-Fleet-Token` | 空 |
| `--no-keepalive` | 不装保活守护（默认装：systemd timer 或常驻进程） | 关 |
| `--uninstall` | 卸载（停服务+删单元+删目录+清隧道进程） | — |

### 常见场景

```bash
# 飞牛 OS（fnOS）—— 本身就是 Debian，直接跑
sudo bash install.sh --name fnos --port 3100

# 设备上 sudo 需要密码（配置进去，docker/systemctl 才能用）
sudo bash install.sh --name fnos --port 3100 --sudo-pass '你的密码'

# 装第二个副端，端口错开
sudo bash install.sh --name pi --port 3101

# 卸载
sudo bash install.sh --name fnos --port 3100 --uninstall
```

**重复执行同一命令是安全的**（幂等）：会更新代码并重启服务，但**不会覆盖你已改过的配置文件**。

### 没有 systemd 的环境（Docker / PAI-DSW / Colab / 云容器）

脚本会自动降级为**守护进程模式**：功能完全一样（12 个工具照用），只是不走服务托管。安装目录里会多出一套自包含的控制脚本：

```bash
bash /opt/<name>_mcp/mcp-ctl.sh status     # 状态 + 健康检查
bash /opt/<name>_mcp/mcp-ctl.sh start      # 启动
bash /opt/<name>_mcp/mcp-ctl.sh stop       # 停止
bash /opt/<name>_mcp/mcp-ctl.sh restart    # 重启
bash /opt/<name>_mcp/mcp-ctl.sh log 50     # 最近 50 行日志
bash /opt/<name>_mcp/mcp-ctl.sh tunnel     # 起公网隧道
```

内部带一个 `_supervisor.sh` 守护壳，进程崩了 3 秒自动拉起（等价于 systemd 的 `Restart=always`）。**区别只有一个：容器重启后需要手动 `mcp-ctl.sh start`**（或把这条命令写进平台的启动钩子）。

> 手动指定也行：`--mode nohup` 强制守护进程模式；`--mode systemd` 在真 systemd 机型上强制走服务。

## 二之二、云容器怎么连上？（公网隧道）

容器/云主机通常**没有**能被你家内网直接访问的 IP，此时用内置的 cloudflared 快速隧道：

```bash
bash /opt/<name>_mcp/mcp-tunnel.sh
```

它会自动下载 `cloudflared` 并打印一个 `https://xxx.trycloudflare.com` 地址，主端 `mcp.json` 就填 `<该地址>/mcp`。装的时候一步到位：`--tunnel`。

注意事项：

- 地址是**临时的**，隧道重启就变，换了要同步改主端 `mcp.json` 再重启 WorkBuddy。脚本默认**复用已在运行的隧道**（地址不变，也避免撞限流）；要强制重建用 `CF_FORCE=1 bash mcp-tunnel.sh`。
- 隧道脚本末尾的「公网访问：xxx」是**从副端本机发起**的 curl，只作参考。**容器里常出现 `000`，但隧道其实是通的**——云容器出网多有限制，本机连不上 Cloudflare 边缘，而 cloudflared 走的 UDP 7844 不受影响。判定以主端为准：

  ```bash
  # 在主端电脑上跑，返回 200 就是好的
  curl -s -o /dev/null -w '%{http_code}\n' https://xxx.trycloudflare.com/
  ```
- **别反复重开隧道**：trycloudflare 对新建快隧有频率限制，短时间建太多会出现「隧道连上了但域名一直不解析」，等 10~30 分钟再试。脚本默认**复用已有隧道**，保活守护的铁律也是「能复用绝不重建」，正常不会触发。
- QUIC 被网络设备干扰时（日志刷 `no recent network activity`、访问间歇 502）换协议：`CF_PROTO=http2 bash mcp-tunnel.sh`。**这一步脚本会自动做**：起隧道后 36 秒内轮询日志，命中 QUIC 报错就杀掉、记下 `.tunnel_proto=http2` 并自动重启为 http2，下次直接沿用。
- **开隧道前必须设 token**：脚本检测到 `auth_token` 为空会自动生成 32 字节随机 token、写进配置、重启副端，并打印主端该加的 `headers`。见「六、安全说明」。
- 公网隧道**用完就关**：`kill $(cat /opt/<name>_mcp/tunnel.pid)`。要长期用就上固定隧道（自有域名 + `cloudflared tunnel create`，地址恒定，配 `CF_TOKEN` 走 named tunnel）或端口映射/frp。
- 地址变了不用手工改 mcp.json —— 配「地址目录」让主端自动跟随，见「八之二」。

## 三、接入主端（在**主端电脑**上）

```bash
python add_fleet_node.py --name fnos --ip 192.168.1.11 --port 3100          # 内网
python add_fleet_node.py --name cloud --url https://xxx.trycloudflare.com/mcp   # 公网隧道
```

脚本会探测连通性、按 MCP 规范握手后真实调一次 `exec`，然后自动写进 `~/.workbuddy/mcp.json`。

其它用法：

```bash
python add_fleet_node.py --list                              # 看已注册的副端
python add_fleet_node.py --name fnos --ip 1.2.3.4 --remove   # 移除
python probe_fleet.py http://192.168.1.11:3100/mcp           # 纯诊断：握手+列工具+调工具
python call_node.py  http://192.168.1.11:3100/mcp 'uname -a' # 免重启直接调一次工具
```

也可以手改 `mcp.json`：

```json
{
  "mcpServers": {
    "fnos": { "type": "http", "url": "http://192.168.1.11:3100/mcp" }
  }
}
```

**最后一步：重启 WorkBuddy**（或重载 MCP 配置）。MCP 配置改完不会自动生效。

## 四、自建内网分发（可选，设备多时省事）

在任一台机器上放一份静态服务：

```bash
sudo python3 -m http.server 8099 --directory /opt/fleet_agent --bind 0.0.0.0
```

之后每台设备只需：

```bash
curl -fsSL http://192.168.1.10:8099/install.sh -o install.sh && sudo bash install.sh --name <名字> --port 3100
```

仓库里附了现成的 `fleet-http.service`（systemd 单元），改好路径后 `systemctl enable --now fleet-http` 即可常驻。

## 五、日常运维（副端上）

**systemd 机型：**

```bash
systemctl status <name>-mcp      # 状态
journalctl -u <name>-mcp -f      # 实时日志
systemctl restart <name>-mcp     # 改完配置重启
```

**容器机型（无 systemd）：**

```bash
bash /opt/<name>_mcp/mcp-ctl.sh {start|stop|restart|status|log|tunnel}
```

查不到机型就统一用 `bash /opt/<name>_mcp/mcp-ctl.sh status`，它会自己判断。

配置文件：`/opt/<name>_mcp/mcp_agent_config.json`

```json
{
  "name": "fnos",
  "tool_prefix": "",
  "sudo_password": "",
  "work_dir": "/",
  "command_timeout": 120,
  "max_output_bytes": 200000,
  "allowed_roots": ["/"],
  "enable_docker": true,
  "login_shell": true
}
```

改完 `systemctl restart <name>-mcp`（容器用 `mcp-ctl.sh restart`）生效。

> 用 `install.sh` 再装一次也会自动重启（配置文件保留）；隧道默认**复用已有地址**，`--tunnel` 重装不会换地址（要换得先 `CF_FORCE=1` 或先卸载）。

- 设备**没有 Docker** → 把 `enable_docker` 设为 `false`，docker 工具会直接返回友好提示。
- 想限制文件读写范围 → 改 `allowed_roots`，例如 `["/opt", "/home"]`。
- **命令输出前面总有一堆欢迎横幅**（PAI-DSW 等容器会在 `/etc/profile.d/` 里打印 ASCII 图）→ 设 `"login_shell": false`。它把执行方式从 `bash -lc`（登录 shell，会 source `/etc/profile`）换成 `bash -c`，输出干净；不影响 PATH（docker / python3 照样能找到）。默认 `true` 是为了兼容老设备。

## 六、安全说明

### 6.1 访问控制（v3.0）

副端以 **root** 运行，`exec` 工具等于**把 root shell 挂出去**。v3.0 起加了两道闸：

| 配置 | 作用 |
|---|---|
| `auth_token` | Bearer 令牌。请求要带 `Authorization: Bearer <token>` 或 `X-Fleet-Token`。**留空 = 完全不鉴权** |
| `allow_ips` | IP/CIDR 白名单。**非空即生效**，不在名单内直接 403（在鉴权之前判） |

另外还有**暴力破解节流**：同一 IP 连续失败 10 次 / 60 秒窗口后，每次鉴权失败
额外 `sleep 1s`。令牌比对用 `hmac.compare_digest`（常数时间，防时序侧信道）。

启动时如果 `auth_token` 为空，日志会打印一段醒目警告：

```
鉴权            : [!] 未开启
[!] 未设置 auth_token：任何能访问本端口的人都等于拿到本机 shell。
[!] 仅限内网使用；一旦开公网隧道（mcp-tunnel.sh），请务必先设置：
```

**开隧道时脚本会兜底**：`mcp-tunnel.sh` 检测到 `auth_token` 为空，会
自动生成 32 字节随机 token、写进配置、重启副端，然后打印 token 和主端该加的
`headers` 片段。确实不要这层保护（只在完全可信的内网里用）才用
`FLEET_NO_AUTH=1 bash mcp-tunnel.sh`（会大声警告）。

```bash
# 安装时就指定
sudo bash install.sh --name fnos --port 3100 --auth-token '一串随机'

# 或只允许内网网段（此时不要开隧道）
sudo bash install.sh --name fnos --port 3100 --allow-ips '192.168.1.0/24,10.0.0.0/8'
```

> `allow_ips` 和公网隧道**基本互斥** —— 走隧道时来源是 cloudflared 本机地址。
> 要公网访问就用 token。

### 6.2 其他

- 建议用防火墙限制来源：`ufw allow from 192.168.1.0/24 to any port 3100`。
- `allowed_roots` 是文件读写的安全边界，默认 `/`（不限制）。按需收紧。
- `install.sh` 不含任何硬编码密码，`--sudo-pass` 由你在安装时传入，明文存于配置文件（root 可读）。
- 公网隧道**用完就关**：`kill $(cat /opt/<name>_mcp/tunnel.pid)`。

## 七、常见问题（FAQ）

**Q：`bash: install.sh: 没有那个文件或目录`**
文件没下载到本机。先 `curl -fsSL <URL> -o install.sh && ls -l install.sh` 确认落地再安装。注意从聊天工具里「下载附件」得到的是**本地电脑**上的文件，远端服务器上并不存在。

**Q：装好了，主端连不上**
先在主端确认网络可达：`curl -s http://<副端IP>:3100/`。云服务器/容器通常还要在**安全组或平台侧放行端口**，否则主端无法连接。

**Q：端口被占用**
换 `--port 3101` 等空闲端口，主端配置同步改。

**Q：装的时候报 `System has not been booted with systemd as init system (PID 1). Can't operate.`**
容器里装了 `systemctl` 二进制但 PID 1 不是 systemd（Docker / PAI-DSW / Colab 都这样）。v2.0 已修：脚本改为探测 `/run/systemd/system` + `/proc/1/comm`，这种情况自动走守护进程模式，不再报错。老版本请重新下载 `install.sh`。

**Q：改了 `mcp_agent.py` 后装出去的还是老代码**
内嵌代码需要重新生成：`python build_installer.py`，再重新执行 `install.sh`。

**Q：装完 `systemctl` 里看不到服务**
确认是不是容器环境——容器里本就没有服务单元，用 `mcp-ctl.sh` 管理。

**Q：每次工具返回的内容前面都跟着一大段欢迎横幅 / ASCII 图**
容器（如阿里云 PAI-DSW）在 `/etc/profile.d/` 里写了登录提示。把配置里的 `login_shell` 设为 `false` 再重启服务即可。示例：

```bash
python3 - <<'PY'
import json
p="/opt/fnos_mcp/mcp_agent_config.json"
d=json.load(open(p)); d["login_shell"]=False
json.dump(d, open(p,"w"), ensure_ascii=False)
PY
bash /opt/fnos_mcp/mcp-ctl.sh restart
```

**Q：隧道地址主端已能访问（curl 返回 200），但副端自检显示「公网访问 000」**
正常，忽略即可。容器出网受限导致本机连不上 Cloudflare 边缘，与隧道能否被外部访问无关。

## 八、文件清单

| 文件 | 用途 |
|---|---|
| `install.sh` | **一键安装脚本**（自包含，已内嵌服务器代码）——拷这一个文件就够了 |
| `install-offline.sh` | **自解压安装脚本**，内嵌了 install.sh，只需传这一个文件 |
| `install-offline.b64` | 离线载荷 = `base64(zlib(install.sh))`，**纯 base64 无注释行**（**不是 tar 包**，只需 python3） |
| `install-offline.partNN.txt` | 同一载荷的分片（默认每片 4000 字符），**纯 base64**，`cat` 拼即可 |
| `install-offline.README.txt` | 离线包的使用说明（三条路径的命令、校验值、注意事项） |
| `install-offline.sha256` | 全部产物的 md5 校验和 |
| `make-offline.py` | 由 `install.sh` 生成上面几个离线文件；`--check` 逐字节校验一致性 |
| `fleet-directory.py` | **地址目录服务**：副端上报隧道地址、主端按名字查最新地址 |
| `mcp_agent.py` | 副端服务器源码（与内嵌版一致，便于阅读/改） |
| `add_fleet_node.py` | 主端注册工具（探测 + 测试 + 写 mcp.json） |
| `build_installer.py` | 改完 `mcp_agent.py` 后重新生成 `install.sh` |
| `fleet-http.service` | 内网静态分发服务的 systemd 单元 |
| `mcp_agent_config.example.json` | 配置样例 |
| `probe_fleet.py` | 纯诊断脚本（握手 + 列工具 + 调工具），连不上时先用它定位 |
| `call_node.py` | 直接调用副端工具（不必重启 WorkBuddy），`--list` 列工具、`--tool` 调任意工具 |
| `.gitattributes` | 强制全仓库 LF 换行，防止 Windows 上 clone 后 shell 脚本被转成 CRLF |

> 改了 `mcp_agent.py` 要按顺序重新生成：`python3 build_installer.py && python3 make-offline.py`。
> 只跑前者的话 `install-offline.*` 还是旧的 —— 跑 `python3 make-offline.py --check` 能发现。

安装完成后，副端目录里还会生成：

| 文件 | 用途 |
|---|---|
| `mcp-ctl.sh` | 控制脚本（无 systemd 环境使用，两种机型都能看状态） |
| `_supervisor.sh` | 守护壳，崩溃 3 秒自动重启 |
| `_portkill.py` | 零依赖按端口清理僵尸进程（替代 `fuser`） |
| `mcp-tunnel.sh` | cloudflared 公网隧道（自动补 token、QUIC 自动回退） |
| `mcp-watchdog.sh` | **保活守护**（`--once` 或常驻循环） |
| `unit.txt` | 记录 systemd 单元名 |
| `.want_tunnel` / `.no_keepalive` / `.tunnel_proto` | 标记：要隧道 / 关保活 / 选定的隧道协议 |
| `current_url.txt` | 当前可用的公网地址（保活守护持续刷新） |
| `port.txt` / `agent.pid` / `watchdog.pid` / `tunnel.pid` / `agent.log` / `tunnel.log` / `watchdog.log` | 运行时文件 |

## 八之二、保活与地址自动跟随（v3.0）

### 保活：让主端「随时可连」

副端可能因为 OOM、容器重启、隧道进程掉线而失联。保活守护每 60 秒做三件事：

1. **agent 挂了 → 拉起来**（systemd 机型走 `systemctl restart`，容器机型走 `mcp-ctl.sh start`）
2. **隧道进程没了 → 重建**（*只在进程真的没了时* —— 能复用就绝不重建，避免撞 trycloudflare 限流）
3. **把当前地址写进 `current_url.txt`，并上报「地址目录」**

systemd 机型用 oneshot service + timer，不占常驻进程：

```bash
systemctl status <name>-mcp-watchdog.timer
systemctl start  <name>-mcp-watchdog.service    # 立刻跑一轮
cat /opt/<name>_mcp/watchdog.log
```

容器机型用常驻循环（`mcp-ctl.sh start` 会自动拉起）：

```bash
bash /opt/<name>_mcp/mcp-ctl.sh watchdog    # 跑一轮 + 看守护状态
bash /opt/<name>_mcp/mcp-ctl.sh wdlog 50    # 看保活日志
bash /opt/<name>_mcp/mcp-ctl.sh keepalive off   # 关掉
```

### 地址目录：隧道换域名也不用改 mcp.json

副端重启会换 trycloudflare 域名，主端写死的地址立刻失效。两步解决：

```bash
# 1) 在一台 7x24 的机器上跑目录服务（NAS 就行）
python3 fleet-directory.py --port 8790 --token <一串随机>

# 2) 副端安装时告诉它目录在哪
sudo bash install.sh --name cloud --port 3100 --tunnel \
  --directory http://192.168.1.10:8790 --directory-token <同一串>

# 3) 主端登记目录
python3 fleet.py directory --url http://192.168.1.10:8790 --token <同一串>
```

之后主端在地址不通时会自动问一次目录、换成新地址并回写登记表：

```bash
python3 fleet.py sync              # 手动刷一遍
python3 fleet.py exec cloud "uptime"   # 地址失效时自动纠正后重试
```

目录服务接口：`POST /register`（上报）、`GET /nodes`、`GET /resolve?name=<n>`、
`DELETE /nodes?name=<n>`。零依赖，落盘 `~/.mcp-fleet/directory.json`。

## 九、已踩过的坑（别重犯）

1. **别用 `pkill -f xxx` 杀自己的服务**——命令行里含关键字会把执行该命令的 shell 一起杀掉。脚本改用「按端口 scan `/proc/net/tcp` 找 pid」的 `_portkill.py`（不依赖 `fuser`，容器里常没装 psmisc）。
2. **Python 拼命令不要用 `%` 格式化**——`docker ps --format '{{.Names}}'` 里的 `{{...}}` 会被 `%` 吃掉。代码里已全部改成字符串拼接。
3. **`sudo -S` 要预喂密码**——`printf '%s\n' <密码> | sudo -S bash -lc <命令>`，否则卡住等输入。
4. **配置改动不重启不生效**——`systemctl restart <name>-mcp` 或 `mcp-ctl.sh restart`。
5. **主端 mcp.json 改完必须重启 WorkBuddy**。
6. **`curl | bash` 时 `$0` 不是文件**——脚本内的 `exec sudo bash "$0"` 会失效，已在 v1.0 修正为检测并提示正确用法。
7. **别用 `command -v systemctl` 判断有没有 systemd**（v2.0 修复）——容器里这个二进制通常存在，但 PID 1 不是 systemd，`systemctl daemon-reload` 必炸；且 `set -e` 会让整个安装脚本在那里静默中止，前面写的文件全白装。
8. **不要用 `$!` 记 `setsid nohup ... &` 的 pid**——`setsid` 在「调用者已是进程组长」时会 fork，`$!` 拿到的是马上退出的中间进程，pidfile 直接失效（表现为卸载后隧道/agent 进程残留）。正确做法是让被守护的进程自己写 pidfile，或用 `pgrep -f` 事后回查真实 pid。
9. **副端自检里的「公网访问」数值不能当判决**——容器出网受限时本机 curl 返回 `000`，但隧道对外其实完全可用。别据此反复重开隧道（会撞上 trycloudflare 的频率限制，越试越坏）。判定只看主端那一次 curl。
10. **登录 shell 会把容器的欢迎横幅塞进每一次工具返回**——`bash -lc` 会 source `/etc/profile`，PAI-DSW 这类镜像在那里打印 ASCII 图。用配置项 `login_shell: false` 切成 `bash -c` 解决。
11. **Windows 上做语法检查别直接敲 `bash`**——可能解析到 `C:\Windows\System32\bash.exe`（WSL 桩），在有安全策略的机器上会被拦且报错莫名其妙。用系统里真实 Git Bash 的绝对路径。
12. **重装不等于更新（v2.2 修复）**——老版本再装一次会打印「已在运行 (pid xxx)」直接跳过，**跑的还是老代码**，你会以为改动没生效。现在：nohup 分支检测到旧实例自动 `restart`，systemd 分支把 `enable --now` 拆成 `enable` + `restart`（`enable --now` 对已 active 的单元不会重启）。配置文件仍然保留、不覆盖。

### v3.0 新增踩过的坑

13. **`pkill -f <关键字>` 会杀掉自己**——这条在开发 v3.0 时又踩了两次：`pkill -f fleet-directory.py` 会把执行该命令的 shell 一起杀掉（因为它的命令行里也含这个字符串），表现是**整条命令毫无输出**。要么用 `pkill -f '[f]leet-directory.py'` 这种方括号技巧，要么把 pkill 写进脚本文件里（脚本自己的命令行只有脚本路径）。
14. **重装换模式时老的 `_supervisor.sh` 会和 systemd 抢端口**——先用 nohup 装、再用 systemd 装（或反过来），老 supervisor 还在每 3 秒拉起 `mcp_agent.py`，日志里只见 `OSError: [Errno 98] Address already in use` 反复刷，但 `systemctl status` 看起来又是好的。v3.0 起 `install.sh` 在启动前会 `systemctl stop` + 按安装目录 `pkill` 清干净。
15. **`--auth-token` 在重装时被静默忽略**——配置文件「已存在则不覆盖」是好事（保住你的调参），但副作用是命令行给的 `--auth-token` / `--allow-ips` / `--directory` 全不生效，表现为「我明明传了 token，副端却还在用旧的」。v3.0 起这些参数会**合并进**已有配置（不带则保留原值）。
16. **`mcp-ctl.sh status` 说「未运行」但服务是好的**——老脚本只看 `agent.pid`，systemd 机型根本没这个文件。v3.0 起先问 `systemctl is-active`，输出 `运行中（systemd: xxx，状态 active）`，并带上保活定时器和当前隧道地址。
17. **自解压脚本别用 heredoc 喂给 python**——`python3 -c '...' "$TMP" <<'EOF' ... EOF` 在 Git-Bash + 原生 Windows Python 下 stdin 拿不到数据（解出 0 字节）。`make-offline.py` 生成的脚本改成「让 python 读脚本自己、按 `FLEET_PAYLOAD_BEGIN/END` 标记切出载荷」，任何平台都稳，且载荷行前缀 `#` 所以 `bash -n` 也能过。
18. **探测死地址抛异常会盖住真正的成功**——`urlopen` 对死地址抛 `URLError`。如果不吞掉，用户看到的是「连不上」，而不是「自动纠正到新地址后成功了」。所有探活调用都要 `try/except`。
19. **改了 `install.sh` 忘了重新生成离线包**——`install-offline.*` 是**生成物**，不会自动跟着变。跑 `python3 make-offline.py --check` 会逐字节比对三个产物与 `install.sh`，不一致就报错。发布前务必跑一次。
20. **离线载荷文件里绝不能写注释说明**——`install-offline.b64` 和 `install-offline.partNN.txt` 原本各带一个 `# ... 第 1/9 片` 的说明头。看起来无害，实际是致命的：头里的 `MCP`、`install`、`sh`、`1/9` **全都在 base64 字母表内**，`b64decode` 不会丢弃它们，而是当成数据插进流里 → `zlib.error: incorrect header check`。而文档给用户的命令恰恰是「整文件解码」和「`cat` 拼接」，所以用户照着做必然失败。现在这两个文件只放纯 base64，说明文字统一挪到 `install-offline.README.txt`。
21. **校验脚本不能自己「帮」用户预处理**——上面的 bug 之所以一直没被发现，是因为 `make-offline.py --check` 里**自己先剥掉了 `#` 注释行**再去解码，于是永远绿。校验必须用**用户实际会敲的那条命令**（整文件 `b64decode`、朴素 `cat`）去跑，否则校验通过 ≠ 用户能跑通。现在 `--check` 还会检查载荷里有没有 base64 字母表之外的字符。
22. **`--uninstall` 漏删保活定时器**——早先卸载只删 `${UNIT}.service`，忘了 `${UNIT}-watchdog.timer` / `.service`。而 timer 是 `enabled` + `active` 的，安装目录被 `rm -rf` 之后它仍会**每 60 秒**触发一次 oneshot，单元里的 `WorkingDirectory=$DIR` 已不存在，于是 journal 里永远刷：
    ```
    Failed at step CHDIR spawning /bin/bash: No such file or directory
    nasbeta-mcp-watchdog.service: Failed with result 'exit-code'.
    ```
    现在卸载会**先** `disable --now` 掉 timer、停掉 service、删掉两个单元文件，再删目录。遇到老版本装出来的机器，手工清一下：
    ```bash
    systemctl disable --now <name>-mcp-watchdog.timer
    rm -f /etc/systemd/system/<name>-mcp-watchdog.{timer,service}
    systemctl daemon-reload
    ```
