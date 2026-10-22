# 架构 · MCP Fleet v5.2

> 读者：想理解协议细节、做二次开发或安全评审的人。
> 产品用法看 `SKILL.md`；模块 API 看 `docs/V5.2-CONTRACT.md`；威胁模型看 `references/security-model.md`。

## 1. 一句话

每台设备跑一个**标准 MCP 服务器**（节点），节点之间用自研轻量协议互联：
UDP 发现 + HTTP 消息/同步端点 + 离线信箱 + 多路径中继。设备上的 Agent 通过 MCP 工具
互相调用；跨节点调用由本节点的 `peer_call` 转发，最多 3 跳，带环检测。

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│  MCP 客户端（Claude Code / Cursor / CodeArts / 自研 Agent）     │
│    initialize.instructions · prompts/list · tools/call        │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP + JSON-RPC (可选 TLS)
┌───────────────────────────▼──────────────────────────────────┐
│  节点服务层  scripts/mcp_agent.py                             │
│  ┌─────────────┬──────────────┬──────────────┬──────────────┐ │
│  │ 鉴权/签名    │ 工具路由/策略 │ 审计/指标     │ Dashboard    │ │
│  └─────────────┴──────────────┴──────────────┴──────────────┘ │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ 协议层 scripts/fleet_proto.py + 安全核心 scripts/fleet_core.py │
│  │ 发现 · 信箱 · 投递 · gossip · 身份 · 签名 · 加密 · 防重放   │ │
│  └──────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ Agent 智能层 scripts/fleet_agent.py                       │ │
│  │ instructions · prompts · 自对话工具 · 信封 · 编排器        │ │
│  └──────────────────────────────────────────────────────────┘ │
└───────────────────────────┬──────────────────────────────────┘
                            │
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
     局域网 UDP 发现     直连 HTTP         中继/转发
     (3199/udp)        (3100/tcp)        (可选 --relay)
```

## 3. 节点身份与信任

- 启动时 `identity.json`（Ed25519 私钥，0600）不存在则生成；
  `node_id = base32(sha256(pub))[:16]`，`fingerprint` 为 16 字符分组。
- 节点记录 `peers.json`：`name → {url, pubkey, node_id, trust, last_seen, caps, ...}`。
- 信任等级：
  - `S0` 未配对（仅同网段发现；默认不接受变更类调用）
  - `S1` token 信任（v5 兼容）
  - `S2` 公钥签名（v5.2 配对）
  - `S3` 签名 + TLS（公网/跨网段推荐）
- 配对时序（当前 CLI：签名邀请 + 公钥交换 + SAS；下方 X25519 前向保密流程为
  `fleet_core` 已提供原语的升级路径，尚未接入 CLI）：

```
A: fleet invite
   └─ Invite{v,name,node_id,pubkey,host,port,ts,ttl,nonce,sig=Ed25519(A)}
        │  （邀请链接，带外传输；可自行转成二维码）
        ▼
B: 验证 sig + ttl → 生成临时 X25519 (b_priv,b_pub)
   POST /fleet/pair  {invite, b_pub, name_B, pubkey_B, proof=Ed25519(B, transcript)}
        ▼
A: 验证 proof → 计算 shared = X25519(a_eph, b_pub)
   返回 {a_pub_eph, proof_A}
        ▼
B: 验证 proof_A → shared = X25519(b_eph, a_pub_eph)
   双方: key = HKDF(shared, transcript); SAS = digits(sha256(transcript))
        ▼
   人工核对 6 位 SAS → 双方保存 pubkey + key（0600）
```

## 4. 请求与消息

### 4.1 签名请求（v5.2）

```
X-Fleet-Node:  <node_id>
X-Fleet-Ts:    1699999999
X-Fleet-Nonce: <32 hex>
X-Fleet-Sig:   base64(Ed25519("fleet1|METHOD|PATH|ts|nonce|sha256(body)"))
```

服务端：时间窗 ±300s → nonce 未见过 → 公钥查 peers → 验签 → 放行；失败计数并审计。
旧客户端无签名时回落 token 校验（`compat_v5`）。

### 4.2 邮箱消息

```json
{
  "msg_id": "uuid", "seq": 42, "from": "a-agent", "to": "b-agent",
  "text": "...", "topic": "task", "ts": "...", "expires_at": 0,
  "origin_node": "a3f2", "sig": "base64(Ed25519(from+msg_id+sha256(text)))"
}
```

- 落盘：`mailbox/inbox.jsonl`，`text` 字段为 `enc2$...` 密文；`from` 由来源节点签名确认。
- 投递：在线 → `POST /fleet/mail`；离线 → outbox，指数退避 + 抖动重试；
  上线 → `/fleet/poll` 拉取 + gossip `/fleet/sync` 摘要交换。
- 去重：`msg_id` 窗口（重启回填）；至少一次投递；过期消息进死信并从待投递移除。

### 4.3 端点

| 端点 | 方法 | 鉴权 | 作用 |
|------|------|------|------|
| `/mcp` | POST | 签名/token | MCP JSON-RPC |
| `/sse` | GET | 签名/token | SSE 传输 |
| `/fleet/health` | GET | 可选 | 存活/版本/摘要 |
| `/fleet/metrics` | GET | 签名/token | Prometheus 指标 |
| `/fleet/identity` | GET | 签名/token | 本机身份/指纹（公钥安全） |
| `/fleet/mail` | POST | 签名/token | 收消息 |
| `/fleet/poll` | GET | 签名/token | 上线收信 |
| `/fleet/sync` | POST | 签名/token | gossip 摘要 |
| `/fleet/discover` | POST | 可选 | 握手/交换 URL |
| `/fleet/pair` | POST | 邀请 proof | 配对 |
| `/fleet/relay` | POST | 签名/token | 中继转发（**预留**，当前只写配置） |
| `/fleet/ui` | GET | fragment token | Dashboard |
| `/fleet/status` | GET | 签名/token | 邮箱/节点状态 |

## 5. 发现与多路径

1. **局域网 UDP 广播**（3199/udp）：每 10s 发 hello；已配对节点 hello 带签名，
   伪造被拒收。仅同网段。
2. **邀请直连**：跨网段/公网，地址由邀请携带；失败时提示检查 NAT/防火墙。
3. **Tailscale**（可选）：检测到 tailnet 时自动纳管，MagicDNS + 端到端加密。
4. **中继（预留）**：`--relay` 目前只写配置并在状态中展示；真正的反向中继、
   跨 NAT 自动穿透尚未实现，跨网段请用 Tailscale 或带公网地址的邀请直连。
5. **重连**：每 peer 状态机 + 指数退避（1s→2s→…→300s，±20% 抖动）+ 熔断；
   `fleet doctor` 显示每条路径的最近成功/失败与 RTT。

## 6. 数据目录

```
~/.fleet/ 或 /var/lib/mcp-fleet/        （0700）
├── identity.json       Ed25519 私钥/公钥（0600）
├── peers.json          信任节点与公钥（0600）
├── mcp_agent_config.json  配置含 token（0600）
├── mailbox/
│   ├── inbox.jsonl    接收（加密 text）
│   ├── outbox.jsonl   待投递（重试计数/下次时间）
│   ├── sent.jsonl     已投递
│   ├── cursor.json    各 agent 已读位置
│   └── salt.bin       邮箱密钥盐
├── audit.log          哈希链审计（0600）
├── tasks/             自对话任务与计划（0600）
├── relay/             中继队列（启用时）
└── install-manifest.json  安装器清单（卸载依据）
```

## 7. 性能与资源

- 单节点空闲约 15–25 MB 内存；1000 条消息信箱约 1–2 MB。
- 局域网发现 < 10s；投递重试间隔 15s；SSE 推送延迟 < 100ms。
- 连接上限 64（可配）、SSE 16、请求体 1 MB（可配）、输出 200 KB（可配）。
- 软路由/手机可 `--lite`：关闭 Docker 工具、降低并发、缩小消息与输出上限。

## 8. 扩展点

- **新工具**：`BASE_TOOLS` + `BASE_FUNCS` 注册；自动获得前缀/只读/策略支持。
- **新平台**：`fleet_install.py` 增加 ServiceAdapter。
- **新传输**：实现 `FleetClient` 接口即可复用 Agent 层。
- **新提示词**：`fleet_agent.PROMPTS` + `references/` 文档。

## 9. 版本与兼容

- MCP `protocolVersion` 2024-11-05；节点 `SERVER_VERSION` 5.2.3。
- 协议字段新增保持向后兼容（未知字段忽略）；v5 节点可与 v5.2 混跑。
- 关闭 `compat_v5` 后仅接受签名请求；升级路径见 CHANGELOG。
