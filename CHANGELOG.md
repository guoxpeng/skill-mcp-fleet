# Changelog · MCP Fleet

所有版本的更新说明。版本号与 `scripts/mcp_agent.py` 内 `DEFAULT_CONFIG["version"]` 对齐，
每个里程碑版本对应一个 git tag 与一次技能市场提交。

> **编号规则**：版本号三段式 `X.Y.Z`，日常迭代**只动末位 Z**，范围 0–99。
> 末位满 100 才进位：`5.2.99 → 5.3.0`（`5.2.100` 非法）。首位 X 仅用于破坏性变更。
> 一个发布只对应一个条目，功能再多也压在同一个末位号里——版本涨得比内容快，
> 评审和回滚都没法对账。

---

## [5.2.8] — 2026-10-28 · 一次管一堆机器：批量执行与分组

竞品调研（`references/competitive-analysis.md`）里剩下的 P0 第一项。ssh 本身没有
「在多台机器上跑同一条命令」的能力，这件事历来由 pdsh / clush / ansible ad-hoc 补。
在此之前，这个项目的答案是让用户自己写循环：

```bash
for n in $(fleet targets); do fleet exec $n "uptime"; done
```

串行、出错不停、输出糊成一片、也看不出哪台失败。**这等于没做。**

### 1. `fleet run`：批量并行执行

```bash
fleet run --all "uptime"
fleet run --group web "systemctl restart nginx"
fleet run nas,pi "df -h"
fleet run --all --short "apt-get -s upgrade"   # 每台一行，看哪台不对
fleet run --all --quiet "reboot"               # 只要结论，判退出码
```

`ThreadPoolExecutor` + **每台一个独立客户端**——`http.client` 的连接不是线程安全的，
共用一个会抛 `CannotSendRequest: Request-sent`（`MCPClient` 的注释里早就写了这条，
批量执行是第一个真的会撞上的场景）。

### 2. 一台离线不能拖住整批

建连用短超时、不重试（和 `health` 同一条理由：体检不是诊断）。离线那台单独记一行
错误，其余照常出结果，退出码 1。原来串行时，五台里三台没开机，一次批量要等
三个建连超时。

### 3. 输出顺序稳定，进度不掺进结果

结果按节点名排序打印，同一批跑两次排版一致，能直接 `diff`。进度（`3/5 完成`）
只写 stderr、且只在终端里显示——重定向到文件时那些 `\r` 只会变成垃圾字符。

### 4. 两个必然踩到的 argparse 坑

- **`fleet run --all "uptime"`**：`[targets?] [command...]` 的歧义让唯一的位置参数
  被 `targets` 吃掉，`command` 变成空。范围已由 `--all/--group/--local` 说清，
  这个孤立的位置参数只能是命令。
- **`fleet run nas --short "df -h"`**：`REMAINDER` 把 `--short` 一起塞进命令，
  设备上直接报 `bash: --short: invalid option`。自己捞一遍开关（`_RUN_BOOL` /
  `_RUN_VAL` 两张表），前后两种写法都成立。

### 5. 分组：`groups` 存在节点上，不另起一份文件

```bash
fleet group                       # 有哪些组、各几个成员
fleet group web                   # 这个组里是谁
fleet group add web istoreos nas
fleet group rm web nas
fleet group del web
fleet add --name pi --ip 192.168.1.9 --group web
```

分组信息写进登记表每个节点的 `groups` 字段，**跟着节点走**：`fleet remove`
删节点时不会留下幽灵成员。重新 `add` 同一台（换地址/换令牌）不会把它踢出原来的组；
`groups` 手写成 `"web,db"` 这种走形格式也认（`_norm_groups`）。

### 6. 一个自己写出来的 bug，值得记一笔

`cmd_run` 里写 `err = _run_take_flags(...)`，把模块级的 `err()` 颜色函数**遮蔽**了，
后面 `err("[x] ...")` 就变成 `'NoneType' object is not callable`。这类错误只在
「走到那条分支」时才炸——`--quiet` 路径不打印错误所以一直正常，直到跑
`fleet run --all -j 2` 才暴露。变量名别跟函数名撞。

### 其它

- 菜单加 [26] 批量执行 / [27] 分组管理，`请选择 (0-25)` → `(0-27)`；
- `fleet list` 每行补 `[组: web]`，`fleet add` 打印归组结果；
- 块头横线按**显示宽度**算（`_dw`），中文「连不上」占两列，用 `len()` 会歪 3 格；
- 工具总数不变（73）：`run` / `group` 是 CLI 侧编排，不新增副端工具。

测试：1039 → **1101 passed, 1 skipped**（新增 `tests/test_batch_528.py` 62 条）。

---


拿到 iStoreOS（24.10.7 / Celeron J4125 / 998MB 内存 / Tailscale）的登录权限后，
把它从 5.2.0 原地升到 5.2.7，逐条敲命令。**前三条命令就翻车了。**

三个 bug 是同一个根因的三种表现，读代码看不出来，因为桌面 macOS/Linux 的
`/etc/profile` 不打横幅——只有软路由会。

### 1. 每条 exec 输出都被开机横幅污染
```
$ fleet exec istoreos "uname -s"
  ▀     ▄▄▄▄    ▄        ▄▄▄▄   ▄▄▄▄      ← 9 行 ASCII 艺术
  ...
iStoreOS 24.10.7, 2026070313
-------------------------------------------------------
Linux
```
根因：`_wrap` 默认 `-lc`（login shell），OpenWrt 的 `/etc/profile` 会 `cat` 横幅。
后果不是「多几行」——是 `| jq`、`$(...)`、`grep` 全部拿到脏数据。

修复：`login_shell` 默认改 **False**；交互式 `shell_open` 仍用 `-l`（那里横幅是
**应该**看到的）。老安装的配置里这个键被安装器写死成 `true`，所以加**一次性迁移**
（写 `_login_shell_migrated` 标记，用户之后手动改回 true 不会被反复打回）。

### 2. `fleet cp` 在设备上完全不可用
```
[x] 搬运失败：远端 stat 输出异常：'\x1b[0;1;34;94m▀\x1b[0m     \x1b[0;34m▄▄▄▄\x1b[0m ...'
```
cp 靠解析远端 `stat` 判断文件，横幅把那行挤掉了。同一个根因，第 1 条修好即消失。

### 3. `net_check listen` 返回 683KB
设备上没有 `ss`，退回的 busybox `netstat -tlnp` **不认 `-l`**，把所有 socket
（含 ESTABLISHED）全倒出来：实测 **6943 行 / 683212 字节**，客户端只能截断，
等于没给信息。修复：优先 `ss`；否则 netstat 自己 `grep -i listen`；两头封 200 行；
空输出明确说「没有监听端口」而不是返回空串。

### 4. 顺带：`exec-async` 的提示在副端上是错的
`fleet exec-async istoreos ...` 打出的 `fleet exec-poll <jid>` 会去问**本机**
（提示里没有 target），得到「未知 job_id」；照提示补上 target 之后，argparse 又
把顺序读反（`target` 排在 `job_id` 前面），报「找不到副端 b5db0c8c4625」。
修复：CLI 层按形状补 target（副端不知道主端用什么名字），`_pick_job_args`
按 **job_id 是纯十六进制**识别，两种顺序都认。

### 真机验证记录
完整数据见 [`references/test-results.md`](references/test-results.md) 第 5 节。

```text
ping      → node v5.2.7（升级前 5.2.0）
exec      → [exit 0] / Linux          （横幅已消失）
cp 推/拉   → 17 字节 5.1s / 5.7s，内容一致
forward   → -L → LuCI HTTP 200 三次全通（2.3s / 2.3s / 7.3s）
sessions  → 1 条连接，label/owner/字节/录制齐全
recordings→ /root/fleet/recordings/sh-368b0153.cast 3.6KB，replay 可回放
audit     → ok:true（升级前 ok:false / bad_index 0，5.2.4 那个 bug 在真机复现并消失）
```

设备工具链实测：`bash base64 dd python3 tar gzip curl wget nc netstat` 全有，
**`ss` 没有**——所以上面第 3 条不是理论风险。

### 测试
- 新增 `tests/test_device_527.py`（21 条）：默认非登录 shell（含反向验证）、
  `shell_open` 仍登录、迁移只做一次且不丢令牌、无该键时不该改写文件、
  listen 命令必须自己 grep + 封顶、job 提示补 target、job_id 按形状识别（两种顺序）。
- 全量 **1039 passed, 1 skipped**（5.2.6 是 1018，本版净增 21 条）。

---

## [5.2.6] — 2026-10-28 · 连接管理：看得见、接得回、断得掉、查得到

5.2.5 把「拿它当 ssh 用」的常用命令串了一遍，剩下的最后一块是**连接本身**。
ssh 的连接管理是三件散装工具：`who` 看谁在线、`ss -tp` 看连接、`pkill` 断连接，
而且**断线就全丢**——换个终端、网络闪一下、笔记本合盖，正在跑的东西就没了。

这一版把这三件事收成一条线，并补上 ssh 做不到的那一条。

### 看得见：一张表
- `fleet sessions [副端]`：shell 会话 / 隧道（连接与监听）/ 后台任务，一张表里
  列全，带时长、空闲、双向字节数、是否在录。以前要敲 `shell_list` +
  `tunnel_list` + `exec_jobs` 三次再自己拼。
- 每条会话带**归属**（`谁@哪台机器`）和**标签**，多端连同一台机器时不用靠时间猜。
- 本机也能查：省略副端即本机，和 `ping` / `health` / `exec` / `tools` 一致。

### 接得回：挂起与接管（ssh 做不到的那条）
- `fleet shell` 里按 **Ctrl-]** 是**挂起**而不是断开：远端 shell 继续跑、
  录制继续写，本地干净退出并打印回来/关掉的两条命令。
- `fleet attach <会话id> [副端]` 接管回来，**顺带重放最近 512KB 屏幕内容**——
  离开时屏幕上是什么，回来还是什么。
- 会话空闲回收从 30 分钟放宽到 **2 小时**（`shell_idle_sec` 可配）：
  原来午饭回来会话就没了，「接管」等于没有意义。

### 断得掉：统一 kill
- `fleet kill <id> [副端]`：按 id 前缀（`sh-` / `tn-` / `tl-`）自动判断类型，
  不用记该用 `shell_close` 还是 `tunnel_close`。
- 断开不存在的连接**不再假报成功**：`shell_close` 本身是幂等的，但敲 `kill`
  的人想知道「到底断没断」，所以这里显式报「没有这条连接」并返回 1。

### 查得到：会话录制（对标 Teleport / Tailscale SSH）
- 默认开录，格式是 **asciinema v2 cast**（`{"version":2,...}` + `[time,"o",text]`）——
  `asciinema play` / `agg` / asciinema-player 都能直接放，不用我们写播放器。
- **只录输出，不录键盘**：录制的目的是「屏幕上出现了什么」，而键盘里可能有密码。
- `fleet recordings` 列录制，`fleet replay <id>` 回放（有 `asciinema` 就用它，
  没有就用内置播放器）；`--dump` 不按时间轴直接吐内容，`--speed` 调倍速。
- 三个容易被忽略但会翻车的细节：
  - **多字节字符跨 read 边界**：PTY 一次 `os.read` 可能正好切在「你」的中间，
    直接 `decode(errors="replace")` 会在回放里留 `�`。用增量解码器把半截留住。
  - **行缓冲**：默认 8KB 缓冲会让「刚敲完就崩」的录制丢掉最后几十秒，
    而那一刻恰恰最需要录像。改成每条事件落盘。
  - **上限**：单会话 64MB 封顶，超了就停写而不是把磁盘写满。

### 顺带
- 新增 4 个副端工具（`sessions` / `session_kill` / `recording_list` /
  `recording_fetch`），工具总数 69 → **73**；`shell_open` 增加
  `record` / `label` / `owner` 参数。
- `recording_fetch` 的 id 直接拼路径，**挡掉目录穿越**（`../`、绝对路径、点开头）。
- 菜单加「连接管理」区：看连接 / 接管 / 断开 / 录制与回放（[22]–[25]）。
- `fleet call shell_open` 这类带下划线的工具名不再打「不是已注册副端」的提示——
  那是正常写法，不是误用。

### 测试
- `tests/test_conn_mgmt_526.py`（32 条）：cast 头部与事件格式、时间戳单调、
  **跨块多字节字符不被拆坏**、超限停写、录制失败不影响会话、分块按字节偏移取、
  **目录穿越拦截**、会话表四类齐全且字段完整、`session_kill` 逐前缀分发
  （含显式 kind 优先）、时长格式化、真 PTY 下 `record` 开关落盘、
  子命令绑定与 `shell` 目标可选、**挂起分支不得出现 `shell_close`**。
- 全量 **1018 passed, 1 skipped**（5.2.5 是 986，本版净增 32 条）。
- 端到端实测（真 PTY）：开 shell → 敲命令 → Ctrl-] 挂起 → `sessions` 里还在 →
  `attach` 接管（屏幕重放出上一条命令）→ 再敲命令生效 → `exit` 后表里清空。

---

## [5.2.5] — 2026-10-27 · 单看每行都对，串起来就矛盾

5.2.4 修的是「一条命令自己不对劲」。这一版修的是**命令与命令之间**：
`nodes` 报的地址 `status` 说连不上、`exec-async` 提示的补救命令照敲就报
usage、`cp` 推进去再拉回来目录多一层、自检建议你去 `chmod` 代码目录。
每一条单独看都有道理，用户按顺序走一遍就卡住——而这正是「拿它当 ssh 用」
时的真实路径。

### 修复：两条命令说的不是同一件事
- **`fleet nodes` 报的回连地址，自己都连不上**。`self_url()` 拼的是
  `http://172.16.1.21:3100/mcp`（网卡地址），而没配令牌时 agent 只绑
  `127.0.0.1`——同一个 `fleet status` 里写的是回环。两条命令并排看就是
  自相矛盾，而用户会拿 `nodes` 里那串去配对端。现在记下真实
  `LISTEN_HOST`：绑回环时如实报 `127.0.0.1` 并附一句「只监听回环：
  别的设备连不进来。要让对端回连，先配 auth_token 再重启」；
  `peer_info` 同步给出 `reachable` / `unreachable_reason`，让**对端**
  也能看出这不是网络问题。
- **`fleet exec-async` 的提示照抄就报 usage**。它说「取输出：
  `fleet exec-poll <job_id>`」，但 `exec-poll` 的 `target` 是必填位置参数，
  只给 job_id 会被当成 target。现在 `exec-poll` / `exec-result` /
  `exec-kill` / `exec-jobs` 的 target 均可省略（由 `_pick_job_args` 兜底），
  提示也改成可直接复制的完整命令，并注明 MCP 客户端对应的工具名。
- **`fleet cp ./a.txt node:/tmp/a.txt` 报「格式不对」**。本机节点是 agent，
  不在任何「副端」登记表里，于是解析时认不出 `node:` 是谁。现在
  `resolve_target` 与 `cmd_cp` 都补入本机节点——`exec` / `cp` 都能用本机
  名字当 target，和副端一致。
- **`fleet call sysinfo` 直接被 argparse 挡在门外**。`--tool` 是
  `required=True`，于是「在本机调一个工具」这个最自然的写法被吃成
  `target=sysinfo`、然后报一句 `--tool is required` 的 usage。现在
  `--tool` 与 target 都可省略：只有一个位置参数且它不是任何已知副端时，
  它就是工具名（在 stderr 说明一句），`fleet call node --tool sysinfo` 照旧。
  和 `fleet exec "df -h"` 用的是同一套判定思路。
- **`fleet tools` 必须给副端**，而它恰恰是「这东西能干什么」的第一反应。
  现在省略即列本机工具，与 `ping` / `health` / `exec` 一致。
- **`fleet logs` 永远说「未找到日志文件」**。`fleet start` 把输出重定向到
  `<节点目录>/run/node.log`，而 `logs` 只在 `data/logs/agent.log`、
  `data/node.log`、`agent.log` 三处找——日志明明有 40 KB 却报不存在，
  还把三个不存在的路径列给你看。现在两边共用 `_node_log_paths()`，
  `run/node.log` 排第一；找不到时说明「节点没启动过时不会有日志，
  先 `fleet start`」。
- **`fleet doctor` 直接甩一坨 JSON**。13 项检查、每项四个键——用户出问题时
  正着急，最不该让他先读 JSON。现在按「通过 / 建议 / 真问题」重排成清单，
  关键项失败时最后补一句「先解决：port（其余项多半是被它带出来的）」；
  `--json` 仍输出原始 JSON 给脚本用。
- **`fleet update --check` 报 `unrecognized arguments`**。`up` 和 `update`
  名字太像：查新版在 `up --check`，升级本机节点在 `update`。用户按字面意思
  敲 `update --check` 得到一句 usage，还得自己猜该换成哪个命令。
  现在 `update --check` 直接转发给 `up --check`（同一份实现，不复制逻辑）。
- **UDP 发现端口被占时刷屏，而且 macOS 上根本共享不了**。同一台机器上跑
  两个节点（测试、容器、开发机都很常见）时，`_listen_loop` 只设了
  `SO_REUSEADDR`——Linux 够用，macOS/BSD 必须 `SO_REUSEPORT` 才能共享，
  于是第二个节点永远绑不上，每 2/4/8 秒打一条 `warn`（实测日志里 36 条），
  看起来像出了大事，其实只是发现功能静默失效、其余功能一切正常。
  现在补 `SO_REUSEPORT`；真绑不上时前 3 次说清「发现功能暂不可用，
  其余功能正常」，第 4 次起不再重复。

### 修复：传输往返一次就变形
- **`cp -r ./src nas:/opt/app/src` 得到 `/opt/app/src/src/...`**。远端打包用的是
  `tar czf - -C <父目录> <目录名>`，解到目标目录后自然多套一层；拉回来再套
  一层，往返几次目录就烂了。现在打 `-C <目录> .`，推/拉同一套逻辑，
  目标目录就是源目录的副本。实测往返结构完全一致。
- **`fleet invite` 把 700 多字符的邀请码打了三遍**。终端里折成几十行，
  用户既看不清也容易复制断行（断一个字符就解不开）。现在只完整打印
  一次，其余两处用「上面的邀请码」引用。

### 修复：报出来的问题其实不该报
- **`fleet sysinfo` 在 macOS 上内存、CPU 两段是空的**。命令只按 Linux 写
  （`free -h`、`ps --sort`），失败信息全堆在末尾，中间只剩两个空标题。
  现在每段都「首选 || 退回」（`sysctl -n hw.memsize` 经 `awk` 转 GB、
  `ps -Ao ... -r`），输出也不再冒充命令结果去带 `[exit 0]` 头。
- **自检把代码目录的 0755 当成「数据目录配错」**，而 `fleet repair --auto`
  会照着建议去 `chmod 700` **代码目录**——默认安装下 `DATA_DIR == HERE`，
  修完连自己都读不了。现在检查与修复用同一判断（只有数据目录与代码目录
  **不是同一个**时才收紧），并另加一条 info 级 `data_dir_shared` 说明
  现状；回环监听时的 `auth_disabled` 也降为 info（那是有意的默认）。

### 测试
- `tests/test_usage_525.py`（35 条）：本机节点作 target（`exec` / `cp` 两条
  路径）、`_pick_job_args` 的兜底、`cp` 的 tar 布局（含一次真打包
  roundtrip，断言目录结构不多层）、`t_peers` / `t_peer_info` 在回环与
  非回环下的措辞与 `reachable` 字段、自检权限判断不误伤代码目录、
  `sysinfo` 命令必须带 macOS 退回分支、邀请码只完整打印一次；
  `fleet call` 的四种手势（裸工具名 / 显式 target / 只给 `--tool` /
  什么都不给），断言 `--tool` 不能是 required；`logs` 与 `start` 共用
  同一张日志路径表；`doctor` 的 JSON → 清单转换（含解析失败要原样打印、
  `--json` 必须保留 JSON）；UDP 发现必须设 `SO_REUSEPORT` 且提示有界；
  `update --check` 必须转发给 `up --check` 而不是自己再写一份判断。
- 顺带修掉一条**写法过严**的老用例：`test_discovery_layer_global_in_main`
  断言整行 `global LISTEN_PORT, _discovery_layer`，于是「往 global 里加
  一个 `LISTEN_HOST`」也会红。改成按名字逐个断言——它想守的是「别漏声明」，
  不是「这行不许改」。
- 全量 **986 passed, 1 skipped**（5.2.4 是 951，本版净增 35 条）。

---

## [5.2.4] — 2026-10-27 · 换个位置用：真实使用者视角下的 18 处体验坑

这一版是把「自己当用户」重跑一遍：不看代码，只敲命令，看哪里别扭、哪里慢、
哪里会让人误判。查出来的东西和 5.2.3 那批不同——5.2.3 是「保护没生效」，
这批是「生效了但用户看不见 / 看见了也看不懂 / 看懂了也来不及」。

### 修复：误判与误导
- **`fleet audit` 在默认安装上永远失败**（最严重）。哈希链审计与人类可读的
  文本审计都叫 `audit.log`，没配 `data_dir` 时 `DATA_DIR == HERE`，两者
  **是同一个文件**。于是纯文本行让链的 `_read_tail_state()` 解析失败 →
  每次 `append()` 都抛「链尾损坏」（外面套着 try，**静默**）→ 链里
  **一条记录都没有**；`verify()` 读到第一行不是 JSON → 恒返回 `(False, 0)`。
  用户看到的是「审计链损坏」，实际上它从第一天起就没工作过。
  现在链单独用 `audit.chain.jsonl`；只有老文件确实是合法链时才继续用
  （不丢历史）。修后 `fleet audit` → `ok:true`，链按 idx/prev 正常增长。
- **`fleet status` 报的版本是「进程的版本」，不是「代码的版本」**。改了
  `mcp_agent.py`、测试全绿，`fleet status` 还报老版本——因为 agent 是长驻
  进程，磁盘换了代码它不换。开发机上实测：仓库 5.2.3，运行中的进程报
  5.4.0（三小时前启动的旧代码），而配置文件里还写着 5.3.0，三处全对不上。
  现在 `status` / `doctor` / `start` 三方比对（进程 / 磁盘代码 / CLI），
  不一致就直说「进程跑的还是启动时那份代码，重启加载：fleet restart」。
- **健康节点被报成故障**：本机自检用 `_lan_ip()` 拼地址，而 agent 默认只绑
  `127.0.0.1`；多网卡、防火墙拦 en0、Tailscale 出口节点都会让「节点明明在
  跑」被判死，`fleet start` 于是以为没起来、再拉一个实例、端口冲突。本机
  自检现在优先走回环。
- **`fleet start` 说在跑、`fleet stop` 说没在跑**：pidfile 里残留的死 pid 是
  真值，`_read_pid(d) or _find_listener_pid(port)` 被 `or` 短路，端口查找
  永远不执行。改成先验活再回退。
- **`fleet list` 说「没有发现任何副端」，可 `nodes.json` 里明明有**：
  `fleet.py` 同目录若有 `mcp_agent_config.json` 就会改用该目录的
  `peers.json`，全程无提示。现在会打印「当前登记表 / 别的登记表里有节点 /
  切换命令」。
- **`fleet doctor` 的「10/12 通过」把默认值当成故障**：本机回环不配令牌、
  内网不配 TLS 都是合理默认，却被算进同一个通过比例里，于是刚装好的一天
  就看到「10/12」并去追那 2 项不存在的问题。现在每项带 `critical` 标记，
  摘要拆成「N/M 通过（K 项加固建议未启用：auth、tls）」，整体 ok 只看关键项。
- **错误信息不可读**：`<urlopen error timed out>` 之类。现在翻译成
  「超时（设备关机/休眠，或防火墙挡了端口）」「连接被拒（端口没人监听：
  agent 没启动，或端口写错了）」等能指导下一步的话。

### 修复：等太久
- **`fleet exec` 到离线节点卡死 >150 秒**：把「命令超时」当成了「建连超时」
  （`max(a.timeout, 120)` × 2 次重试）。现在建连/握手单独一套超时（10 秒，
  有上下界，不被 `--timeout` 抬高）。顺带修掉 `_connect()` 里
  `except: pass` 吞掉 connect 失败——它会让单次尝试白等两遍、再乘重试。
  **41s → 10.3s**。
- **`fleet ping` 探离线节点要 30 秒**：`--timeout` 默认 30 是给命令执行用的，
  探活不该继承。默认改成 5 秒（显式传 `--timeout` 仍以用户为准）。**30s → 5.3s**。
- **`fleet health` 一台离线要 47 秒，而且是串行**：`http_probe` 默认 30 秒 ×
  3 次重试。改成封顶 8 秒、不重试、**并行探测**。**34s → 8.5s**。
- **`fleet doctor <target>` 离线要 79 秒，还给出 3 条级联误报**：TCP 都不通
  还继续做 HTTP / MCP / E2E，每一步各等一个超时。现在 TCP 不通就跳过
  第 2–7 步并说明原因。**79s → 8.3s，误报 3 条 → 1 条真问题**。

### 修复：手势不顺手
- **`fleet ping` 必须给 target**：它是「我的节点活着吗」的第一反应，却要
  先知道副端名字。现在省略即探本机（与 `status`/`health`/`audit` 一致）。
- **`fleet exec "df -h"` 只回一句「命令为空」**：那句命令被 argparse 当成
  `target` 了，用户在**完全正确的手势**下被劝退，且看不出原因。现在只有
  一个位置参数、且它不是任何已注册副端时，按本机命令执行（并在 stderr
  说明一句）；另加 `--local` 显式指定。`main()` 里那句抢在 `cmd_exec`
  前面的空命令拦截已删除——判定统一放在知道「哪些名字是副端」的地方。
- **`fleet exec` 把退出码压成 1**：`exit 42` 和 `command not found` 一样都是
  1。要替代 ssh，退出码必须原样回传，否则脚本里的判断全错。现在 `exit 42`
  → 42，`test -f` 失败 → 1。
- **`fleet health` 在没有副端时只报「没有发现任何副端」**，读起来像出错，
  而且退出码 1。现在先报本机节点状态，再说明没有副端。
- **`fleet health` 谎报「0 工具」**：工具数只在握手后才有，`GET /` 里没有这个
  字段，而代码写的是 `r.get("tools") or 0`——于是每台在线节点都显示
  「0 工具」，看着像节点没加载任何工具。不知道就写 `?`，别拿 0 冒充事实。

### 修复：交互菜单一选就崩（`fleet` 不带参数的第一入口）
- **菜单里 8 项会抛 `AttributeError`**：exec / probe / doctor / health /
  tools / mesh / msg send / msg history 都用手搓的 `argparse.Namespace(...)`
  传参，漏了 `workdir`、`agent`、`port`、`msg_cmd` 这些属性。只有真的选了
  那一项才暴露，于是「菜单能用」一直是个错觉。现在统一走
  `build_parser().parse_args()`，参数定义只有一处，永远不会再漂。
- **`fleet discover` 一执行就崩**：子命令解析器漏了 `set_defaults(func=...)`，
  `main()` 里 `a.func(a)` 直接 `AttributeError`。help 里写着、菜单第 16 项
  也列着，但从来没跑通过。

### 测试
- `tests/test_version_drift_524.py`（11 条）：磁盘版本读取（锚在
  `DEFAULT_CONFIG`，不误取文件头部的示例串；文件头部超过 8KB 也要读得到）、
  三方比对、**读不到磁盘版本时不许乱报**、`status` 真的把提示打出来，
  外加一条反向对照（版本一致时提示必须消失）。
- `tests/test_cli_ergonomics_524.py`（23 条）：`_is_known_target` 的判定、
  `_pick_exec_target` 的四种兜底、`ping`/`exec` 参数解析、`main()` 里不得
  再出现空命令拦截、`exit 42` 的退出码透传、审计链路径选择（纯文本日志
  不能抢走链文件 / 老链要保留 / 新文件优先）、**每个子命令都必须绑定
  处理函数**、菜单里不得再出现手搓 Namespace、自检摘要要区分
  「真问题」与「加固建议」。
- 全量 **951 passed, 1 skipped**（5.2.3 是 917，本版净增 34 条）。

---

## [5.2.3] — 2026-10-27 · 长时间在线：把「假保护」换成真保护

这一版没有新功能，全是**已经写在注释里、但实际没生效**的保护。它们的共同点是
不报错、不崩溃、平时也看不出来，只在设备内存吃满、链路变慢、或者进程被杀的
那一刻才暴露——而那一刻正是最不该出事的时候。

### 修复
- **异步任务（`exec_async`）读输出只有 1.3 MB/s**：用 `BufferedReader.read(65536)`
  配非阻塞 fd，而 `read(n)` 会**凑满 n 字节才返回**，于是实际节奏是
  「读一块 → `BlockingIOError` → `sleep(50ms)`」——吞吐被钉死在 64KB/50ms。
  实测 20MB 输出要 **13.8 秒**，而日志流、构建输出正是 `exec_async` 的用途。
  改成裸 `os.read(fd, 65536)`：有数据就返回，天然增量又不丢吞吐。
  同一场景 **13.8s → 1.08s（19.9 MB/s）**。顺带把「每来一块就 `buf[-1MB:]`
  整段拷贝」改成攒到 2 倍上限再裁，消掉 16 倍写放大。
- **`fleet shell` 被 SIGTERM / SIGHUP 杀掉后，用户终端卡在 raw 模式**：
  交互式会话把本地 stdin 设成 raw（不回显、不做行缓冲），正常退出走
  `finally` 恢复；但被信号杀掉时 `finally` 根本不执行，终端就永久停在那——
  回车不换行、Ctrl-C 只打印 `^C`，用户得盲敲 `reset` 才能救回来。
  ssh 也处理这一档。现在接管 SIGTERM/SIGHUP：先 `TCSANOW` 恢复终端属性，
  再按默认处置把信号重发一遍（不吞退出码）。用 `TCSANOW` 而不是 `TCSADRAIN`
  是因为临死时对端 pty 很可能已经没人读，等排空就是永久挂死——
  信号处理器里挂死比不恢复更糟。
- **同步 `exec` 的输出上限是假保护（会把设备撑爆）**：原来用
  `p.communicate(timeout=t)` 收输出，收完才截断到 1MB。可 `communicate`
  会把 stdout/stderr **全读进内存**——`yes` 或 `cat /dev/urandom` 在
  timeout（默认 120s）到点之前就已经把设备撑爆了，事后再截断纯属自我安慰。
  实测：400MB 输出，老路径峰值内存涨 **385MB**；软路由只有几百 MB，这一下就没了。
  现在改成边读边丢（超了继续读，否则子进程写满管道会卡住 `wait` 永远不返回，
  但不再往内存里留），stdout 上限 1MB、stderr 单独 64KB，截断时明确告诉
  调用方实际字节数。改后同样 400MB 输出，峰值涨幅 **0 MB**。
- **连接池的 per-call 超时是空操作**：`_PooledHTTP.post(..., timeout=N)` 只写了
  `conn.timeout = N`——`http.client` 只在 `connect()` 时用 `self.timeout` 建
  socket，之后 `send()` / `getresponse()` 都不会再应用它。结果**所有请求实际
  都用建连时那个超时**：`fleet cp --timeout 600`、长轮询的长超时全部静默失效，
  慢链路上表现为「莫名其妙超时」。实测 `timeout=0.5` 的调用跑了 2.02s 才返回。
  现在显式 `sock.settimeout()`，并顺手补上 `TCP_NODELAY`（对端回过
  `Connection: close` 时 http.client 会内部重连，新 socket 上没有它）。
- **超时被报成「连不上」**：`except (ConnectionError, OSError)` 写在
  `except socket.timeout` **前面**，而 `socket.timeout` 本身就是 `OSError`
  的子类——超时分支是死代码，所有超时都报「连不上 %s（timed out）」，
  把人引去查网络/DNS，真实原因只是「这一步花的时间比预期长」。
  调换顺序，并让超时**不重试**（重试只会把等待时间翻倍）。
- **审计日志有两套并行实现，写同一个文件却各持一把锁、各用各的阈值**：
  `audit_log`（peer_call / delegate）用 10MB，`_audit_log`（危险工具调用）用
  5MB，两个锁互相看不见对方——一边 `os.replace` 把文件挪成 `.1`，另一边还
  握着旧 fd 继续写，轮转之后写进去的行全落进了 `.1`，当前日志反而是空的。
  调阈值的人也会困惑「明明改成 5MB 了，怎么还涨到 10MB 才转」。现在统一成
  一个路径、一把锁、一个写入口，两个历史函数名保留为薄封装。
- **`_tailscale_ip` 被定义了两次，后者静默覆盖前者**：第一份用
  `tailscale ip -4` 并带 60 秒缓存，第二份解析 `tailscale status --json`、
  **无缓存**、失败返回 `None`。运行时生效的是第二份，于是 `self_url()` 在
  mesh / info / invite 这些热路径上每次都 fork 一个重量级的 `status --json`
  （超时 10s，手机/NAS 上几百毫秒），而第一份的缓存从来没被执行过，是纯死代码。
  现在合并成一份：先试便宜的 `ip -4`，拿不到再退回解析 `status --json`，
  统一缓存、统一失败返回 `""`（调用方不必再区分 `None` 与空串）。
- **`fleet forward` 的并发连接数没有上限**：每条转发连接占一个线程 + 两个
  MCPClient，而 `--bind 0.0.0.0` 意味着对端可以无限开连接。一次端口扫描就能
  把线程数、内存和副端的 `TUN_MAX` 槽位一次性耗光，之后正常转发全部失败，
  现场表现却是「转发器还活着，只是再也连不上了」——最难查的那种。
  现在上限 128（`FLEET_FWD_MAX_CONNS` 可调），超了立刻关掉并限频提示；
  `-R` 方向被拒时还会显式 `tunnel_close`，否则副端会挂着一个永远没人读的隧道。
### 测试
- **异步吞吐设了下限断言**：20MB 输出必须 > 5 MB/s（老实现是 1.3 MB/s，
  这条线专门防回退）；同时断言缓冲不超过 2 倍上限、小输出一个字节不少。
- **终端恢复用真 pty 验证**：起真 agent + 真 pty 跑 `fleet shell`，SIGTERM /
  SIGHUP 之后断言 `ECHO` 回来了。另用裸进程（只 `setraw` 然后被杀）确认
  「无处理器时 termios 确实残留」——证明这条用例不是恒真。
- **输出上限按峰值内存度量**：独立子进程喂 400MB 输出，断言峰值 RSS 涨幅
  < 120MB。反向对照：老的 `communicate` 路径同样输入涨 385MB，用例必然失败。
- **per-call 超时用真慢服务端验证**：池超时 30s、单次 0.4s，断言按 0.4s 断、
  且服务端只收到 1 次连接（没被重试）。另有一条 AST 断言锁住
  「`except socket.timeout` 必须排在 `OSError` 之前」。
---

## [5.2.2] — 2026-10-27 · 替代 ssh：交互式 shell + 端口转发 + 自升级 + macOS 适配

这一版对准一个具体目标：**普通公网设备上，fleet 能替代 ssh；内网配合 Tailscale
达到同样的手感；连进来的设备能长期在线，agent 可以借它的网络环境干活。**

替代 ssh 需要三件事，缺一不可：一个真终端、能转发端口、以及出事之后能自己恢复。
这一版把三件都做了。

### 新增：`fleet shell`（PTY 交互式会话）
- 远端用 `pty.fork()` 起**真终端**里的登录 shell，本地把 stdin 设 raw 模式，
  按键原样送、输出原样刷。`tty` 返回 `/dev/ttysNNN`，vim / top / less 正常工作。
- 传输仍是请求/响应，拆成 `shell_open / write / read / resize / close / list`
  六个工具；`read` 支持**长轮询**（`wait` 最长 5s），有数据立刻返回。
- 输出走 base64：PTY 吐的是原始字节流，可能在多字节字符中间截断，按文本传会乱码。
- 输出缓冲 512KB 滚动 + **绝对 offset**，客户端落后太多时明确回 `truncated`；
  同时最多 8 个会话，防反复 open 撑爆内存。
- `SIGWINCH` 自动同步窗口大小；远端 `exit` 后客户端立即返回（不卡在阻塞读）。

### 新增：`fleet forward`（端口转发，等价 ssh -L / -R）
```bash
fleet forward -L 8080:db.internal:3306 nas   # 本地 8080 → 副端视角的 db:3306
fleet forward -R 9000:127.0.0.1:3000 nas     # 副端 9000 → 本地 3000
fleet forward --list nas
```
- 副端新增 7 个工具 `tunnel_open/listen/accept/read/write/close/list`。
- 一条命令可叠加多个 `-L` / `-R`；`-L` 默认只绑 `127.0.0.1`，要暴露到局域网
  必须显式 `--bind 0.0.0.0`。
- 命令名用 `forward` 而不是 `tunnel`——后者已被 cloudflared 公网隧道占用。
- **不丢字节**是这一块最关键的实现细节：缓冲区满时读线程挂起等主端来取（背压），
  而不是像终端那样丢弃旧数据。终端丢的是滚屏历史，TCP 丢的是字节流——
  丢一个字节，下载的文件就是坏的。
- 取走即回收：每次 `tunnel_read` 后立刻丢掉已消费前缀。按阈值回收会让
  小缓冲区永远不满足条件，表现为「传到 512KB 就不动了」。
- 每条转发连接配两条 HTTP 通道（读/写各一）：共用一条会让写方向排队等 200ms 长轮询。

### 新增：`fleet up`（本地脚本自升级）
- 检查远端版本，**有新版才动**；`--check` 只看不升，`--ref` 指定分支/标签，
  `--force` 同版本重装，`--no-restart` 不重启节点。
- 两条路线：git 仓库走 `fetch` + `merge --ff-only`；非 git 安装走源码包
  （GitHub + jsdelivr + 三个镜像依次回退）。
- **只升代码，不碰数据**：`CODE_FILES` 白名单，配置/身份/登记表/审计/邮箱一律不覆盖。
- 升级前整目录备份到 `~/.mcp-fleet/up-backups/`（**不在仓库内**——放旁边会立刻
  变成未提交改动，把下一次升级永久挡住）；升级后跑一次 `--version` 自检，
  不过就自动回滚。远端版本坏了记进 `up-state.json`，下次自动跳过。
- 工作区有未提交改动时拒绝升级（不替你 stash/reset），`--force` 才强推。
- 版本比较按**数字三元组**：字符串比较会认为 `5.10.0 < 5.9.0`，升级功能直接失效。

### 新增：长在线可见性（`fleet health`）
- 每台节点多两列：副端自报的**进程在线时长**，和主端**观测到它多久了**。
  只看当次 UP 看不出稳定性——一台每 5 分钟重启一次的服务，每次探活都是 UP。
- 本地记账 `~/.mcp-fleet/seen.json`：`first_seen` / `last_ok` / `up_count` /
  `down_count`，掉线不清零；掉线时显示「最后可达 X 前」。
- 拿不到 `/fleet/health` 时按「不知道」处理，不把老版本副端误判成异常。

### macOS 适配
- **launchd 的 PATH**：launchd 起的进程只有 `/usr/bin:/bin:/usr/sbin:/sbin`，
  Homebrew 的 git/node/ffmpeg 全在 `/opt/homebrew/bin`——不补 PATH，
  `fleet exec` 里跑这些命令一律 command not found。plist 现在显式写
  `EnvironmentVariables`（PATH/HOME/LANG/LC_ALL），PATH 只取固定白名单 +
  存在性检查，**不继承安装者当时的临时环境**。
- **`fleet stop` 真的能停了**：只 kill pid 的话 KeepAlive 会在 10 秒内把进程拉回来。
  现在走 `bootout` + `disable`（并 enable 反向恢复）。
- **`fleet start` 不再绕过 launchd**：发现 plist 就用 `bootstrap` + `kickstart`，
  开机自启与崩溃重拉才真正生效；launchctl 失败才退回直接 spawn。
- **`_lan_ip` 按 en0/en1 取址**：开了 Tailscale 出口节点时默认路由走 utun，
  原实现会把隧道地址当局域网地址广播出去，局域网里谁都连不上。
- 控制台直接提示：没装 git（`xcode-select --install`）、python 来自 Homebrew
  （升级大版本后路径会变）、节点没被 launchd 托管（重启不会自启）。

### 修复
- **OpenWrt / iStoreOS 上每个 `exec` 都会失败（致命）**：`run_local` 和 `_wrap`
  把 shell 写死成 `bash`，而 OpenWrt / iStoreOS / Alpine 默认只有 busybox 的
  `ash`。这些设备上 `exec` 一律返回「找不到 shell」——软路由恰恰是「把家里设备
  接进来」最想接入的节点。现在按 `/bin/bash → /bin/sh → /bin/ash` 探测并缓存，
  探测不到就退到 POSIX 保证存在的 `sh`。
- **`fleet cp` 在 OpenWrt / iStoreOS 上双向都废（致命）**：`upload` 走
  `base64 -d > f`、`download` 走 `base64 f`、拉取分块走 `dd | base64`——三条路
  全都依赖远端命令。busybox 默认不编译 `base64` applet，于是这些设备上
  传什么都失败，报错还只写「对端返回被截断」，完全指不到真正原因。
  现在 `upload` / `download` 改由副端 Python 直接读写文件，`download` 新增
  `offset` / `length` 参数做分块，主端先探测一次再决定走新路还是退回 `dd`
  （老副端会忽略参数返回整文件，用长度比对识别）。零外部命令依赖，
  3MB 拉取 6.28s → 0.94s（顺带省掉每块两次 fork）。
- **`download` 读空内容时主端把返回值包成 JSON**：`_call_once` 里
  `if not text: text = json.dumps(res)` 判的是**文本是否为空**，而不是
  **content 是否存在**。`download` 读空文件、或 offset 越过文件末尾时，
  合法返回就是空字符串，被这句兜底变成 `{"content":[{"text":""}]}`，
  调用方 base64 解码得到垃圾字节写进本地文件——现象是「文件传过去但内容是坏的」，
  从现象根本反推不到这里。
- **僵尸 tunnel listener 永久占槽位 + 占端口**：`_tun_sweep` 对
  `kind == "listen"` 直接 `continue`，理由是「监听器没有数据流」。主端崩溃或
  断网来不及 `tunnel_close`，listener 就一直留着；跑几次 `-R` 之后再监听
  同一个端口就是 `Address already in use`，现场看不出是上一轮的 socket 没放。
  现在 listener 也按 `last` 回收，并在 `tunnel_accept` 的入口和长轮询返回处
  刷新时间戳——不刷新的话，正常跑着的 `-R` 反而会被当成孤儿回收掉。
- **`fleet cp` 大文件基本不可用（致命）**：推 600KB 二进制时被拒，报
  「该操作需要用户确认：防火墙变更」。根因是执行策略把 `upload` 的 `content`
  （base64 文件正文）也拿去跑危险命令正则——`ufw` 只有 3 个字符，几十万字符的
  随机载荷里几乎必然出现，于是**几乎每个大文件上传都被误判**。
  现在把参数分成「命令文本」和「数据载荷」两路：文件正文/二进制分块不参与
  命令匹配，但仍受 `path_jail` 与体积检查；小体量文本载荷（≤4KB）照样扫描。
  实测量化：512KB 块在旧逻辑下 **5.0% 会随机被拦**（`/nft/` 里的 `/` 正好
  构成 `\b` 边界），新逻辑 0%。
- **`exec` 的 stdin 同样 5% 误判，且拖慢每次调用**：原来只要工具名在
  `_EXEC_TOOLS` 里就全量扫 stdin，1MB 载荷要 **150ms**（25 条规则 × 载荷长度）。
  改为看**命令文本**：只有 `bash`/`python3`/`sh`/`perl` 这类解释器才把 stdin
  当命令扫；`base64 -d > f`、`tar xz` 的 stdin 是数据。误判归零，耗时 150ms → 0.1ms。
- **僵尸交互式会话永久占槽位（长时间在线的杀手）**：客户端崩溃/断网来不及
  `shell_close`，会话就一直是「运行中」。`SHELL_TTL` 只回收 `end` 非空的会话，
  而断线的会话 `end` 永远是 None——8 次断网之后 `shell_open` 彻底失败。
  现在按 `last_read` 做空闲回收（30 分钟），槽位满时还能挤掉「2 分钟没人读」
  的那个；`shell_list` 增加「空闲 Ns」列，一眼看出谁没人管了。
- **审计日志无限增长**：`_audit_log` 只 append，没有轮转也没有上限。软路由的
  overlay 常常只有几百 MB，写满之后设备直接起不来，而且现场看不出是日志撑爆的。
  现在超过 5MB 轮转成 `audit.log.1`（只留一份），每 100 行才 stat 一次。
- **升级备份无限堆积**：每次 `fleet up` 整目录复制一份，从不清理；真机升了
  9 次就占了 9.1MB。现在只保留最近 5 份。
- **每个 HTTP 响应都白等一拍延迟 ACK**：服务端与客户端都没关 Nagle，
  MCP 又是「一问一答」，响应头分几次小 write 发出去时后一段要等前一段的 ACK。
  连接建立即设 `TCP_NODELAY`。
- `-R` 转发在网络抖动/副端重启后静默失效：原来 `tunnel_accept` 一抛错就退出，
  用户只看到「转发莫名其妙不工作了」。现在区分两类错误：副端不认识 listener
  （重启后内存态没了）→ 自动重建；网络类错误 → 退避重试，连续失败才提示。
  实测 kill 掉副端再拉起，`-R` 端口 10 秒内自动恢复。
- **`fleet forward` 的连接泄漏**：`tunnel_open` 失败时两个客户端没关，
  每被拒一次漏两条 HTTP 连接；`clients` 清单只增不减。改为集合，关闭即摘除。
- **单条连接关闭会关掉整个转发器**：全局 stop 事件被单条连接的 pump 置位。
  改为每条连接独立开关，全局开关只由 Ctrl-C 触发。
- **`tunnel_list` 的「空闲」永远显示 0s**：客户端即使没数据也在持续长轮询，
  `last` 一直是新的。改用 `last_data`（只在真有字节流动时更新）。
- **`MCPClient` 的连接不是线程安全的**：读线程和写线程共用一个 `http.client`
  连接会抛 `CannotSendRequest: Request-sent`，交互式 shell 直接崩。
  现在 `_post_once` 加锁串行化；`fleet shell` 另开一条写连接。
- **`fleet menu` 从来没绑定处理函数**：缺 `set_defaults(func=...)`，执行直接
  `AttributeError`。
- `fleet shell` 在非终端 stdin（管道 / CI）下会抛 `termios.error` 并**泄漏远端会话**；
  现在给出可读提示并主动关闭会话。
- `show_fleet_status` 里 `st, err = ...` 遮蔽了模块级 `err()`，有对端就崩。
- `_first_run_guide` 的 `%` 占位符与参数个数不匹配，新用户第一次运行就崩。
- 运行期配置 `scripts/mcp_agent_config.json`（含 token）与 `scripts/mailbox.key`
  加入 `.gitignore`。

### 测试
- 端到端用例起真进程 + 真 socket：验证 `-L` 往返，以及 **`-R` 在副端重启后自动恢复**
  （这条只能靠端到端证明，单测证明不了）。
- 数据完整性用 1MB / 5MB 随机二进制 + SHA256 比对，覆盖上行/下行/`-L`/`-R` 四方向。
- 本机回环实测：`-L` 5MB ≈ 0.5s、`-R` 5MB ≈ 3s。
- **真机推拉**：起真 agent 进程（真 socket、真文件系统），跑 `fleet cp` 推/拉
  0 / 1 / 1000 / 4096 / 64K / 600K / 3MB 七档 + 非 ASCII 路径，双向 SHA256 一致、
  无残留分块。600KB 那档正是上面那个策略误判的复现用例。
- 策略分流的回归用例单独锁住：大 base64 载荷放行、小文本危险正文仍拦、
  `exec` 的 stdin 不豁免、非典型字段藏命令仍命中、`path_jail` 仍生效。
- **真机交互式 shell**：Ctrl-C 中断前台进程、退出码 130、heredoc 多行粘贴、
  中文、二进制字节原样、全屏程序、700KB 输出、resize、并发会话互不干扰。
- **真机资源回收**：把回收阈值临时调到 5s/3s 起一个真 agent，验证「只读的那个
  会话被保留、放着的被回收」，以及 8 个槽位占满后第 9 个能挤掉最久未读的。
- 审计日志轮转、升级备份清理各有独立用例（含「函数要真的被调用」这类断言，
  防止只写了清理函数却没人调）。
- `download` 分块、`upload` 不依赖 `base64`、拉取模式探测各有独立用例；
  另有一条**空 content 不得被包成 JSON** 的断言锁住上面那个坑。
- **真机 listener 回收**：把 `TUN_IDLE` 调到 5s 起真 agent，验证空闲 listener
  被回收且**端口真的释放**，而持续 accept 的 listener 8 秒轮询后仍存活。
- **重复定义用 AST 断言钉死**：`_tailscale_ip` 这类「同名 def 两份、后者静默
  覆盖」的 bug 不报错、不崩，只会悄悄变慢变脏，普通用例抓不到。
- **转发并发上限反向验证**：把 `FLEET_FWD_MAX_CONNS` 调到 999 跑同一场景，
  8/8 连接全部存活（断言必然失败）——证明这条用例真的能抓到「没有闸」。
- **审计日志并发混写**：两个入口各起 30 个线程写同一个文件，断言一条不丢。

---

## [5.2.1] — 2026-10-27 · 稳定性回归 + 丝滑度层 + 版本号三段式

这一版把 `fix/fleet-p0-20261027` 分支上做过的稳定性修复重新移植回 v5.2 主线
（该分支从 v4.5 拉出，而 main 已重写到 v5.2，合并时全部修复被静默丢弃），
并在真机上量了延迟，补上了「连接一次要等一秒」的体感问题。

### 安全（移植自 v4.5 分支，共 6 项）
- **sudo 密码不再进 argv**：`run_local()` 改为把密码经 stdin 前缀喂给 `sudo -S`，
  进程列表里看不到明文（原来 `ps aux` 一眼可见）。
- **邀请码并发校验加锁**：`_verify_invite()` 用 `INVITE_LOCK` + 原子写，
  并发下同一个码只能成功兑换一次；落盘失败返回 500 而不是假成功。
- **目录同步身份门**：`sync_from_directory()` / `cmd_sync()` 校验
  `anon["server"] == 节点名`，防止被投毒的目录把假地址写进登记表。
- **登记文件损坏不再静默清空**：`load_registry()` 识别损坏后回退 `.bak`，
  两者都坏则拒绝覆盖，避免「一次断电丢失全部节点」。
- **配置损坏拒绝启动**：`die_if_config_corrupt()` 退出码 2 并给修复指引，
  不再带着半份配置无鉴权监听。
- **`fleet list --json` 默认掩码令牌**：`mask_token()` 保留前 4 后 4，
  需要原文显式 `--show-tokens`。

### 稳定连接（7 项）
- 响应体 **2MB 封顶**（`_read_capped`），防止失控节点/中间人灌爆内存。
- **401/403 不重试**，`MCPError` 不重试——令牌错了重试只是白等。
- 只有**幂等**方法（`initialize` / `tools/list` / 探活）才重试一次；
  `tools/call` 永不重试，避免把 `rm -rf` 跑两遍。
- exec 输出 **1MB 封顶**并附中文提示，不再让一次 `cat 大文件` 撑死连接。
- 裸 IPv6 地址自动加方括号（`fd00::1` → `http://[fd00::1]:3100/mcp`），
  家庭 ULA 与 tailnet `fd7a:115c:a1e0::/48` 都能直接用。
- `self_url()` 优先级改为 配置 > tailnet 地址 > 隧道 URL > 局域网 IP，
  多网卡机器不再对外广播一个对端连不上的地址。
- 配了代理时 **tailnet 地址不再被绕过代理**（`_opener_for`），
  否则受限网络里 tailnet 节点会全部变成「连不上」。

### 丝滑度（本版新增）
- **延迟握手**：本节点是无状态 HTTP 服务，`tools/call` 不依赖 `initialize`。
  先直接发，只有对端明确要求时才补握手重试。900ms 的链路上省掉一个往返。
- **直连 HTTP 长连接复用**（`_PooledHTTP`）：`fleet cp` 一个 1MB 文件要 9 个
  来回，复用 TCP 后不再每次三次握手。代理模式默认关闭（各家代理对同一连接上
  的第二条请求处理不一致），需要时 `FLEET_POOL_PROXY=1` 显式打开。
- **新命令**：
  - `fleet ping <节点>` — 只 `GET /`，不握手不 exec，退出码 0/1，给看门脚本用
  - `fleet cp <src> <dst>` — 双向搬运文件，分块避开三处硬上限，支持目录
  - `fleet scan [网段]` — 主动扫同网段节点，`--add` 直接登记
  - `fleet tailnet [status|sync|up]` — Tailscale 状态 / 地址漂移自愈 / 起隧道
  - `fleet start|stop|restart` — 本机节点启停，pid 与 HTTP 双判定
- **无参数 `fleet` = 控制台**：本机节点没跑就自动拉起 → 报「已连接 N/M 台」
  → 一屏命令菜单。敲一个词就知道现在什么状态、下一步能做什么。
- `_peer_summary()` 并发探活（16 线程、2 秒超时），5 台设备不再排队等超时。

### 版本号
- 版本号统一为**三段式 `X.Y.Z`**，并写进 `AGENTS.md` 4.1 节：
  默认只动补丁位，新增能力才动次版本位，破坏性变更才动主版本位。
- `build_package.py` 新增 `check_version_sync()`：5 个脚本 + `SKILL.md`
  frontmatter 全部同源对账，任何一处不是三段式或数值不一致都判构建失败。

### 测试
- 新增 `tests/test_smoothness_521.py`（31 项）：延迟握手三种路径、
  连接池选路 8 种组合、`cp` 路径拆分、`ping`/`pid` 判定、版本号规范。
- 修复 `show_fleet_status()` 里 `err` 被 `st, err = _tailscale_status()` 遮蔽
  导致的 `UnboundLocalError`（菜单在有对端时必崩）。
- 修复 `_first_run_guide()` 的 `%` 格式化实参多传一个导致的 `TypeError`
  （空环境敲 `fleet` 第一屏就崩，是最伤人的那个 bug）。
- `test_version` 不再写死 `== "5.2.0"`，改为校验三段式——写死等于给每次补丁
  加一道无意义的人工门槛。
- 全量 **690 passed, 1 skipped**。

---

## [5.2.0] — 2026-10-22 · 新功能：自修复 + 菜单提示词 + 真机测试文档

### 新增：Agent 自修复
- **`fleet_selfcheck`**（只读）：返回结构化 findings（code/severity/action/repairable）
  与 repair_plan；覆盖配置权限、身份、鉴权、审计链、磁盘、端口、data/mailbox
  权限、pid 文件与实际进程不一致。
- **`fleet_repair`**（需确认）：白名单动作 `restart_service` / `fix_permissions` /
  `clear_stale_pid` / `rollback`；必须 `confirm=<节点名>`，只读节点拒绝。
- **`fleet_repair_loop`**（需确认）：自检 → 修可修项 → 复检，默认 2 次、上限 3 次；
  失败立即停止并返回 remaining；不删数据、不轮换 token、不执行任意命令。
- 重启类修复先校准 pidfile，再调度 1 秒后的外部重启，避免在工具进程内杀自己/起双进程。
- 每次修复写入哈希链审计，可用 `fleet_audit_verify` 复核。

### 新增：菜单与内置提示词
- `fleet` 无参数重做为一屏式菜单：本机状态、常用命令、可直接粘给 AI 的提示词。
- `fleet prompt [self_repair|diagnose|collaborate|pair|upgrade]`。
- 交互菜单新增 `[17] 自检/自修复`、`[18] 查看 AI 提示词`；
  `initialize.instructions` 加入自修复章节。
- 工具总数 41 设备 + 15 智能 = **56**。

### 打包与发布
- 提交包改为**根目录平铺**（SKILL.md 在根、30 文件、包内无 MANIFEST.json），
  满足技能市场格式；MANIFEST/SHA256SUMS 留在 zip 外部。
- SKILL frontmatter 增加 `version: 5.2.0`，构建时校验与代码版本一致。
- CI 增加 workflow artifact 上传；`v*` tag 自动上传 Release 并标记 latest。

### 文档
- 新增 `references/self-repair.md`（工具/白名单/安全边界/验收标准）。
- 新增 `references/test-results.md`（macOS / Ubuntu 24.04 / Android 14 真机结果、
  修复过的真实缺陷、未覆盖项）。
- SKILL/README/SECURITY 同步到 v5.2。

### 测试
- 全量 **626 passed + 1 skipped**；新增自修复工具注册/确认边界/只读保护/有界循环/
  失败停止、菜单与提示词、CLI 映射测试。

---

## [5.1.1] — 2026-10-22 · 修复合集（默认鉴权 / 升级回滚 / TLS / 邮箱 / 跨平台）

> 按版本规则：5.1.0 之后的小问题统一收敛到 5.1.1，不再逐个占用版本号。

### 安全与默认
- 安装默认生成随机 auth_token（`--no-auth` 关闭）；对外监听无 token 时自动生成。
- JSON 安装结果脱敏；自签 TLS 健康检查/回滚/CLI 全链路修复。

### 安装与升级
- 全局 `fleet` 命令；`--upgrade` 先停旧服务、自动备份；`--rollback` 恢复备份。
- 发布包缺模板时内置兜底；Docker 只读根/TLS/data_dir 修复。
- `fleet update` 远端拉最新 release（commit SHA 固定、SHA256 校验），失败回退本地。

### 体验
- `fleet ask`、`fleet invite --qr`、`fleet join --auto-install`、
  `fleet identity export/import`、Dashboard 2.0。
- `fleet update --rollback`、`fleet selfcheck`（自修复工具见 5.2.0）。

### 真机验证
- macOS(launchd)、Ubuntu 24.04(systemd)、Android 14/Termux 安装/升级/回滚/卸载通过。

### 测试
- 全量 **615 passed + 1 skipped**。

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
