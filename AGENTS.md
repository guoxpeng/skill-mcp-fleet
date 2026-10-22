# AGENTS.md · 给在这个仓库里工作的 AI Agent

本文件是仓库级操作约定。**任何 AI coding agent 在修改本仓库前先读本文件**，
再读 `SKILL.md`（产品用法）与 `docs/V5.2-CONTRACT.md`（模块契约）。

## 1. 项目一句话

MCP Fleet = 多设备 AI 对等协作网。每台设备一个节点（MCP 服务器），节点间自发现、
签名请求、离线信箱、按能力委派。目标用户：NAS/云主机/软路由/PC/手机用户。

## 2. 硬约束（违反即回滚）

- **零第三方依赖**：只用 Python 标准库。运行时代码必须 Python **3.6+** 兼容
  （CI 跑 3.9/3.12；不用 walrus、dataclasses、`removeprefix`、`asyncio.run`）。
- **不破坏兼容**：v5 的 token 鉴权、`enc1$` 邮件、旧 CLI 参数必须继续可用。
- **安全默认**：新增网络端点默认需要鉴权；新增写操作默认进入策略检查；日志默认脱敏。
- **不提交密钥**：token、私钥、邀请码、真实 IP/域名一律不入库；测试用临时目录与假数据。
- **文件归属**：并行开发时只改自己的文件（见 `docs/V5.2-CONTRACT.md` 第 5 节），
  需要改他人文件先在报告里说明，由 Lead 统一改。

## 3. 常用命令

```bash
# 测试（macOS/Linux）
python3 -m venv .venv && .venv/bin/pip install pytest pytest-cov
.venv/bin/python -m pytest tests/ -q

# 语法检查
python3 -m compileall -q scripts/ tests/

# 单节点冒烟
python3 scripts/fleet_install.py --name dev --port 3100 --dry-run --json
python3 scripts/mcp_agent.py --host 127.0.0.1 --port 3100   # 前台运行
curl -s http://127.0.0.1:3100/health

# 双节点演示
bash scripts/demo.sh

# 组装参赛提交包（≤30 文件 + SHA256SUMS）
python3 scripts/build_package.py
```

## 4. 修改前的自检清单

1. 这个改动属于哪个模块？契约里允许改吗？
2. 失败路径是什么？网络断、权限不足、磁盘满、时钟偏差、对端版本旧。
3. 有没有新增日志泄露敏感信息？有没有新增无鉴权端点？
4. 有没有测试覆盖成功 + 失败 + 边界？`pytest` 全绿吗？
5. 文档（SKILL.md / references / CHANGELOG）需要同步吗？

## 5. 调试顺序（不要瞎猜）

```
fleet doctor          → 12 项环境自检
fleet status          → 身份/服务/端口/证书
fleet logs --tail 100 → 服务日志（已脱敏）
fleet nodes           → 对端在线/RTT/能力
curl -s :3100/fleet/health
python3 -m pytest tests/ -q
```

## 6. 提交信息

`v5.2.4: <一句话>`；正文写「问题 → 根因 → 修复 → 验证」。
安全修复标注 `security:`，文档改动标注 `docs:`，测试标注 `test:`。
