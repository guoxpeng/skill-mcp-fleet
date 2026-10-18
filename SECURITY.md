# Security Policy / 安全策略

mcp-fleet 让 AI Agent 在你的设备上执行命令（exec/write 等 41 个工具），
安全是本项目的第一设计约束。欢迎安全研究，感谢负责任披露。

## 支持版本

| 版本 | 支持状态 |
| ---- | -------- |
| 5.0.x | ✅ 支持 |
| < 5.0 | ❌ 不再维护 |

## 报告漏洞

**请不要开公开 Issue 报告安全漏洞。**

请通过以下方式私密报告：

1. GitHub 私有安全通告：仓库页 Security 标签 → "Report a vulnerability"
2. 或邮件联系维护者（见 README 作者信息）

请在报告中包含：影响版本、复现步骤、影响评估（如远程代码执行 / 越权 /
消息泄露）。我们承诺 **72 小时内响应**，修复后会在致谢中列出报告者
（除非要求匿名）。

## 安全设计概要

- **鉴权**：节点间通信 Bearer token；无 token 模式仅限内网并强警告
- **传输**：可选 TLS（--tls-cert/--tls-key）
- **落盘**：邮箱消息 PBKDF2 派生密钥加密（enc1$ 前缀）
- **发现**：UDP hello 包 HMAC 防伪签名（冒充已知节点拒收）
- **路径**：realpath + allowed_roots 白名单防路径遍历
- **进程**：exec 超时 + 进程组回收（start_new_session + killpg）
- **限流**：工具层令牌桶 + /fleet/mail 独立限流 + 连接级信号量（超限 503）
- **只读**：`--read-only` 模式 19 个变更类工具全部拒绝
- **审计**：危险工具调用全量留痕（audit.log）

详见仓库内 `references/安全审计报告.md` 与 `references/四轮审查-安全稳定易用.md`。
