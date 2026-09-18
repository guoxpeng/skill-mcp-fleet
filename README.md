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

各通道内容一致，MD5 `822a4bf0cc131f7176886a3c6485ad9e`（29,001 字节，v1.0）。

看到 `服务状态: active` 和 `本机自检通过` 就成了。

### 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--name` | 副端标识（决定 systemd 单元名、主端里的服务名） | 主机名 |
| `--port` | 监听端口 | `3100` |
| `--dir` | 安装目录 | `/opt/<name>_mcp` |
| `--prefix` | 工具名前缀（一般不用填） | 空 |
| `--sudo-pass` | 需要 sudo 提权时写入配置的密码 | 空（自动用免密 sudo） |
| `--uninstall` | 卸载（停服务+删单元+删目录） | — |

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

### 没有 systemd 的环境（容器 / 云主机）

脚本会自动降级为 `setsid nohup` 后台运行，功能不变，但**重启后不会自动拉起**——需要把安装命令写进容器的启动脚本或平台的启动钩子。

## 三、接入主端（在**主端电脑**上）

```bash
python add_fleet_node.py --name fnos --ip 192.168.1.11 --port 3100
```

脚本会探测连通性、真实调一次工具、然后自动写进 `~/.workbuddy/mcp.json`。

其它用法：

```bash
python add_fleet_node.py --list                              # 看已注册的副端
python add_fleet_node.py --name fnos --ip 1.2.3.4 --remove   # 移除
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

```bash
systemctl status <name>-mcp      # 状态
journalctl -u <name>-mcp -f      # 实时日志
systemctl restart <name>-mcp     # 改完配置重启
```

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
  "enable_docker": true
}
```

改完 `systemctl restart <name>-mcp` 生效。

- 设备**没有 Docker** → 把 `enable_docker` 设为 `false`，docker 工具会直接返回友好提示。
- 想限制文件读写范围 → 改 `allowed_roots`，例如 `["/opt", "/home"]`。

## 六、安全说明

- 服务以 **root** 运行（systemd 默认），所以 `sudo` 类工具可直接用。**只在内网部署**，不要把端口暴露到公网。
- 建议用防火墙限制来源：`ufw allow from 192.168.1.0/24 to any port 3100`。
- `allowed_roots` 是文件读写的安全边界，默认 `/`（不限制）。按需收紧。
- `install.sh` 不含任何硬编码密码，`--sudo-pass` 由你在安装时传入，明文存于配置文件（root 可读）。

## 七、常见问题（FAQ）

**Q：`bash: install.sh: 没有那个文件或目录`**
文件没下载到本机。先 `curl -fsSL <URL> -o install.sh && ls -l install.sh` 确认落地再安装。注意从聊天工具里「下载附件」得到的是**本地电脑**上的文件，远端服务器上并不存在。

**Q：装好了，主端连不上**
先在主端确认网络可达：`curl -s http://<副端IP>:3100/`。云服务器/容器通常还要在**安全组或平台侧放行端口**，否则主端无法连接。

**Q：端口被占用**
换 `--port 3101` 等空闲端口，主端配置同步改。

**Q：改了 `mcp_agent.py` 后装出去的还是老代码**
内嵌代码需要重新生成：`python build_installer.py`，再重新执行 `install.sh`。

## 八、文件清单

| 文件 | 用途 |
|---|---|
| `install.sh` | **一键安装脚本**（自包含，已内嵌服务器代码）——拷这一个文件就够了 |
| `mcp_agent.py` | 副端服务器源码（与内嵌版一致，便于阅读/改） |
| `add_fleet_node.py` | 主端注册工具（探测 + 测试 + 写 mcp.json） |
| `build_installer.py` | 改完 `mcp_agent.py` 后重新生成 `install.sh` |
| `fleet-http.service` | 内网静态分发服务的 systemd 单元 |
| `mcp_agent_config.example.json` | 配置样例 |

## 九、已踩过的坑（别重犯）

1. **别用 `pkill -f xxx` 杀自己的服务**——命令行里含关键字会把执行该命令的 shell 一起杀掉。安装脚本已改用 `fuser -k <端口>/tcp`。
2. **Python 拼命令不要用 `%` 格式化**——`docker ps --format '{{.Names}}'` 里的 `{{...}}` 会被 `%` 吃掉。代码里已全部改成字符串拼接。
3. **`sudo -S` 要预喂密码**——`printf '%s\n' <密码> | sudo -S bash -lc <命令>`，否则卡住等输入。
4. **配置改动不重启不生效**——`systemctl restart <name>-mcp`。
5. **主端 mcp.json 改完必须重启 WorkBuddy**。
6. **`curl | bash` 时 `$0` 不是文件**——脚本内的 `exec sudo bash "$0"` 会失效，已在 v1.0 修正为检测并提示正确用法。
