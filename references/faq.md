# FAQ · 常见问题与取舍

## 1. 这和 SSH / 远程桌面有什么区别？

SSH 是**人**连机器；MCP Fleet 是**Agent**连机器。你不需要记命令、IP、端口，直接用自然语言
对 AI 说"看看 NAS 剩余空间"，AI 通过 MCP 工具在远端执行。此外它提供 SSH 没有的东西：
自动发现、离线信箱、能力委派、多 Agent 协作、审计与策略、Dashboard。

## 2. 和 Tailscale / ZeroTier 是什么关系？

互补。它们解决**网络层**互通（NAT 穿透、加密隧道）；MCP Fleet 解决**Agent 层**协作
（谁有什么能力、任务怎么委派、离线怎么办、结果怎么验收）。检测到 Tailscale 会自动利用；
没有也能在局域网/公网直连（中继配置预留）。

## 3. 安全吗？让 AI 在我设备上执行命令不怕吗？

默认有六层防护（身份、签名请求、加密消息、执行策略、审计、供应链），**不是裸奔**。
但必须诚实：任何远程执行工具，一旦节点进程被完全控制，攻击者就拥有该进程权限。
降低风险的做法：独立低权限用户运行、`--read-only` 观察节点、`assisted` 审批模式、
只在内网/Tailscale 暴露、开启 TLS、定期看 `fleet audit verify`。

## 4. 为什么还要自己实现加密/身份？不用现成库？

项目定位是**零依赖、一条命令、离线可用**：NAS/软路由/老设备常常没有 pip、没有编译环境，
装一个密码学库可能失败。因此用标准库实现了 RFC 7748/8032 的最小可用版本，
并在文档里明确边界（未做严格常数时间优化，不用于高价值长期密钥）。
需要更高保证时可以叠加 TLS/Tailscale——它们是可选层，不是替代品。

## 5. 手机（Termux）能干什么？会不会很耗电？

手机可当**主控**（只装 CLI，遥控其他设备）或**副端**（被控/提供算力）。
Termux 用 `sv` 守护 + `termux-wake-lock`，空闲心跳 15s；`--lite` 降低频率。
长期后台受 Android 省电策略影响，建议插电 + 关闭电池优化。

## 6. 软路由（OpenWrt）能装吗？

能。安装器识别 `procd`，生成 `/etc/init.d/mcp-fleet`；需要先 `opkg install python3-light
python3-ssl`。资源有限，建议 `--lite`，只开查询类工具或只读模式。

## 7. 没有公网 IP，怎么从外面连家里？

三种方式：① 家里任一设备跑 `--relay` + 公网入口（云主机/端口映射）做中继；
②  Tailscale 组网；③ 云主机节点反向拉取（store-and-forward，离线也能收）。
不依赖任何中心服务是设计目标，中继是**你自己的**节点。

## 8. 离线消息会丢吗？

不会（在保留期内）。发送方写 outbox，按指数退避重试；目标上线后 `/fleet/poll` 拉取，
gossip 跨节点同步。消息有 `msg_id` 去重、`expires_at` 过期、死信记录。
磁盘满/权限错会在 `fleet doctor` 报出。

## 9. 多个 Agent 怎么分工？会互相打架吗？

用能力声明（`caps`/`mesh_caps`）和结构化信封（propose/accept/result/review）明确
谁做什么、验收标准、超时。编排器有循环/时间/调用预算上限，失败升级给人，不静默重试。
详见 `references/self-dialogue-protocol.md`。

## 10. Agent 真的会"自己想"吗？

本项目提供三样东西让 Agent 自对话：
1. `initialize.instructions` 内置操作手册；
2. 7 个 prompts 与 6 个自对话工具（plan/think/reflect/review/handoff）；
3. 确定性 Orchestrator 做规划-委派-验证循环，可选接 LLM。
质量取决于宿主 Agent 的模型能力；本项目保证**流程与证据**，不夸大"自主意识"。

## 11. 为什么默认不监听公网？怎么开？

安全默认：只监听内网，公网需显式 `--host 0.0.0.0` + `--require-auth` + TLS/Tailscale。
开启后 `fleet doctor` 会给出公网暴露风险提示与检查项。

## 12. 参赛版本和开源版本有什么区别？

同一份代码。参赛提交包由 `scripts/build_package.py` 从仓库生成（≤30 文件 + SHA256SUMS），
不含开发脚本与 CI 配置，但功能一致、可复现。版本号与 git tag 对齐。

## 13. 数据在哪？怎么备份/迁移？

默认 `~/.fleet/`（Linux root 为 `/var/lib/mcp-fleet/`）。备份 `mailbox/` 与
`identity.json`（后者决定节点身份，丢失需重新配对）。迁移：停服务 → 拷贝数据目录 →
新机安装时 `--data-dir` 指向它 → 启动。

## 14. 出问题怎么快速定位？

按顺序：`fleet doctor` → `fleet status` → `fleet logs --tail 100` → `fleet nodes` →
`curl :3100/fleet/health`。仍不行再跑 `python3 -m pytest tests/ -q` 与查看
`references/agent-playbook.md` 的故障恢复手册。
