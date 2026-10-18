# Contributing / 贡献指南

感谢关注 mcp-fleet！这是一个「设备上的 AI Agent 互联协作网」——
零依赖（纯 Python 标准库）、自研 UDP 发现 + HTTP 消息 + store-and-forward
邮箱协议。贡献前请先读 README 了解架构。

## 开发原则（重要）

1. **零外部依赖是硬约束** — scripts/ 下不允许 import 任何第三方库
   （pytest 仅限 tests/ 开发态使用）
2. **Python 3.6+ 兼容** — 目标设备包括老 NAS/软路由（常驻 3.6/3.7）。
   3.9+ 语法特性（如 `str.removesuffix`）必须提供兼容写法
   （参考 `mcp_agent.py` 中 `_strip_suffix` 与 `ThreadingHTTPServer` 降级）
3. **行为变更必须带测试** — 每个修复/新特性都要有对应 pytest 用例，
   且能复现修复前的问题（回归测试先行）
4. **中文注释说明"为什么"** — 代码即文档：安全/兼容性相关的决策
   在注释中说明动机与权衡

## 开发流程

```bash
git clone https://github.com/guoxpeng/skill-mcp-fleet.git
cd skill-mcp-fleet

# 跑测试套件（125 用例，需先 pip install pytest）
python3 -m pytest tests/ -q

# 本地起一个节点试试
python3 scripts/mcp_agent.py --port 3100
```

## 提交规范

- 提交信息格式：`<类型>: <一句话摘要>`，类型用 fix / feat / docs /
  test / refactor / chore（参考 `git log --oneline`）
- 一个 PR 聚焦一件事；危险工具（exec/write/edit 等）的行为变更
  必须在 PR 描述中说明安全影响
- CI 必须绿（push 自动触发 pytest 全量回归 + CodeQL 扫描）

## 打包约束（华为云技能市场提交）

大赛/市场提交的 zip 有 **≤30 文件、≤40MB** 上限，只含运行必需文件。
`.github/` 治理目录与治理文档（本文件、SECURITY.md）**不进 zip** —
打包脚本（如 `pack_508.py` 模式）用 `git ls-files` 枚举后需排除。

## 安全相关问题

不要在 Issue/PR 中暴露漏洞细节，走 [SECURITY.md](SECURITY.md)
的私密披露流程。
