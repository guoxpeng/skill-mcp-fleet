#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fleet_proto -- MCP-Fleet 自研通讯协议层（零依赖，纯 Python 标准库）

设计目标：虚拟空间中的 agent 使用本协议直接相互通信：
  - 在线 agent -> 消息直接 HTTP 投递
  - 离线 agent -> 消息存邮箱(outbox)，上线后自动收信
  - 不依赖 Tailscale / 任何外部服务
  - UDP 广播自动发现同网段节点
  - store-and-forward：消息不会因节点离线而丢失
"""

import json, os, re, socket, sys, threading, time, uuid, io, collections
import urllib.request, urllib.parse, urllib.error
import ipaddress
import base64, hashlib, hmac
import collections

PROTO_NAME = "fleet"
PROTO_VERSION = 1
DEFAULT_DISCOVER_PORT = 3199
DEFAULT_HTTP_PORT = 3100
DISCOVER_INTERVAL = 30
DELIVERY_INTERVAL = 15
SYNC_INTERVAL = 60
MAX_HOPS = 5

# v5.0.2 连接稳定性：
#   HTTP_TIMEOUT      —— 客户端超时（投递/sync/poll）。原 10s：对「挂起不
#                       响应」的主机，每条消息都要等满；配合熔断降为 6s，
#                       局域网/中转场景均足够宽裕。
#   RETRY_BACKOFF     —— 消息级重试退避：attempts 0-4 每 15s；5-9 每 60s；
#                       10-14 每 240s；15-19 每 960s；20+ 每 3600s 封顶。
#                       目标节点短暂离线时快速重试，长期离线时降频省资源。
#   MAX_RETRY_ATTEMPTS —— 重试上限（约 2 天），到顶标 status=dead 停止重试，
#                       防 outbox 无限膨胀；dead 记录保留供审计。
#   DELIVERY_BATCH_MAX —— 投递循环单轮批量上限，防大 outbox 长时间占循环。
HTTP_TIMEOUT = 6
MAX_RETRY_ATTEMPTS = 250
DELIVERY_BATCH_MAX = 50

# v5.0.3 邮箱维护（compaction）：
#   INBOX_KEEP_READ —— inbox 清理后保留的最近消息条数（全员已读的更旧消息移出；
#                     保留窗口供 Dashboard 最近消息流与迟到 agent 补读）
#   SENT_KEEP       —— sent.jsonl 截断保留条数（投递成功的历史）
#   DEAD_KEEP       —— outbox 中 dead 消息截断保留条数（审计用途）
#   MAINTENANCE_INTERVAL —— 维护周期（由投递循环顺带触发，不新增线程）
INBOX_KEEP_READ = 500
SENT_KEEP = 200
DEAD_KEEP = 200
MAINTENANCE_INTERVAL = 3600


def _retry_backoff(attempts):
    """按失败次数计算下次可重试的等待秒数（见上表）。"""
    return min(15 * 4 ** min(attempts // 5, 4), 3600)


class PeerHealth:
    """v5.0.2: per-peer 连接熔断器（circuit breaker）。

    连接稳定性核心：一个下线/挂死的 peer 不再拖垮全局——
    - 连续失败 FAIL_THRESHOLD 次 -> 进入冷却期，期间对该 peer 的一切
      主动连接（投递/gossip/poll）直接跳过，不再每 15s 白等一次超时；
    - 冷却到期后放行一次探测请求：成功即完全恢复，失败则冷却期翻倍
      （90s -> 180s -> ... 封顶 30min），适配「长期离线的设备」；
    - 投递引擎与同步引擎共享同一实例，健康状态全局部一致。
    """

    FAIL_THRESHOLD = 2
    COOLDOWN_BASE = 90.0
    COOLDOWN_MAX = 1800.0

    def __init__(self):
        self._lock = threading.Lock()
        # key -> {"fails": 连续失败数, "until": 冷却截止 ts, "rounds": 冷却轮次}
        self._state = {}

    def is_available(self, key, now=None):
        """该 peer 当前是否可尝试连接（无记录/不在冷却期 -> True）。"""
        now = time.time() if now is None else now
        with self._lock:
            st = self._state.get(key)
            return (not st) or now >= st.get("until", 0)

    def cooldown_remaining(self, key, now=None):
        """剩余冷却秒数（调试/Dashboard 用，不在冷却返回 0）。"""
        now = time.time() if now is None else now
        with self._lock:
            st = self._state.get(key)
            return max(0, int(st.get("until", 0) - now)) if st else 0

    def record_success(self, key):
        """连接成功：健康状态完全清零。"""
        with self._lock:
            self._state.pop(key, None)

    def record_failure(self, key, now=None):
        """连接失败：连续达阈值则开启/加倍冷却期。"""
        now = time.time() if now is None else now
        with self._lock:
            st = self._state.setdefault(key, {"fails": 0, "until": 0, "rounds": 0})
            st["fails"] += 1
            if st["fails"] >= self.FAIL_THRESHOLD:
                cd = min(self.COOLDOWN_BASE * (2 ** st["rounds"]), self.COOLDOWN_MAX)
                st["until"] = now + cd
                st["rounds"] += 1
                st["fails"] = 0   # 冷却结束后从探测重新计数


# ---------------------------------------------------------------------------
# v5.0 落盘加密（标准库实现，零第三方依赖）
#
# 结构: enc1$<base64(salt[16] | nonce[16] | ciphertext | tag[16])>
#   - 密钥   : PBKDF2-HMAC-SHA256(secret, salt, 100k 轮)
#   - 密文   : 明文 XOR keystream；keystream 块 = HMAC-SHA256(key, b"enc"+nonce+counter)
#             （HMAC 作为伪随机函数的 CTR 型结构，等价于对称流加密）
#   - 完整性 : HMAC-SHA256(key, b"tag"+nonce+ct) 截取 16 字节
# 威胁模型：防止 mailbox 落盘文件被直接读取（备份泄露 / 本机其他用户 / 云盘同步）。
# 传输层另有可选 TLS（v4.5）。
# ---------------------------------------------------------------------------
CRYPTO_PREFIX = "enc1$"
PBKDF2_ROUNDS = 100000


def _derive_key(secret, salt):
    """PBKDF2 派生（带 LRU 缓存）。

    v5.0.1 修复：原实现每次加/解密都重跑 100k 轮 PBKDF2（实测单次 ~18ms），
    _load_jsonl 逐条解密导致 N 条消息 ~N*18ms，且投递循环（15s）/Dashboard
    轮询（5s）/gossip 同步（60s）每次都重复支付 —— 消息一多节点近乎卡死。
    现按 (secret, salt) 缓存派生结果，同一 salt 只派生一次。
    """
    cache_key = (secret, bytes(salt))
    with _KEY_CACHE_LOCK:
        got = _KEY_CACHE.get(cache_key)
        if got is not None:
            _KEY_CACHE.move_to_end(cache_key)
            return got
    key = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ROUNDS)
    with _KEY_CACHE_LOCK:
        _KEY_CACHE[cache_key] = key
        if len(_KEY_CACHE) > _KEY_CACHE_MAX:
            _KEY_CACHE.popitem(last=False)   # 淘汰最久未用
    return key


# 派生密钥 LRU 缓存（模块级：多 MailEngine 实例共享同一 secret 时也命中）
_KEY_CACHE = collections.OrderedDict()
_KEY_CACHE_LOCK = threading.Lock()
_KEY_CACHE_MAX = 128


def _keystream(key, nonce, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, b"enc" + nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return out[:length]


def encrypt_text(secret, plaintext, salt=None):
    """加密字符串 -> 'enc1$...' 形态。secret 为节点 auth_token。

    salt 缺省随机（兼容旧调用/测试）；MailEngine 写路径传固定 salt，
    使同一邮箱的所有消息共享派生密钥（nonce 仍每条随机，keystream 不重复，
    安全性等价），配合 _derive_key 缓存实现 O(1) 读写。
    """
    salt = bytes(salt) if salt is not None else os.urandom(16)
    nonce = os.urandom(16)
    key = _derive_key(secret, salt)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(key, nonce, len(pt))))
    tag = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()[:16]
    return CRYPTO_PREFIX + base64.b64encode(salt + nonce + ct + tag).decode("ascii")


def decrypt_text(secret, blob):
    """解密 'enc1$...' -> 明文字符串。验签失败抛 ValueError。"""
    if not isinstance(blob, str) or not blob.startswith(CRYPTO_PREFIX):
        raise ValueError("not an encrypted blob")
    raw = base64.b64decode(blob[len(CRYPTO_PREFIX):].encode("ascii"))
    if len(raw) < 48:
        raise ValueError("blob too short")
    salt, nonce, ct, tag = raw[:16], raw[16:32], raw[32:-16], raw[-16:]
    key = _derive_key(secret, salt)
    expect = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        raise ValueError("integrity check failed")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
    return pt.decode("utf-8")


def hello_signature(token, name, port, ts):
    """v5.0 UDP hello 防伪签名：HMAC-SHA256(节点 token, name|port|ts) 截取 32 hex。"""
    basis = "%s|%s|%s" % (name, port, ts)
    return hmac.new(token.encode("utf-8"), basis.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def verify_hello_signature(token, pkt):
    """校验 hello 包签名。返回 True=通过；False=伪造（已知 peer 签名不符）。"""
    try:
        expect = hello_signature(token, pkt.get("name", ""),
                                 pkt.get("port", DEFAULT_HTTP_PORT),
                                 pkt.get("ts", 0))
        return hmac.compare_digest(pkt.get("sig", ""), expect)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 邮箱引擎 MailEngine -- store-and-forward 核心
# ---------------------------------------------------------------------------
class MailEngine:
    """
    每个节点一个 MailEngine，管理本地邮箱。
    mailbox/inbox.jsonl + outbox.jsonl + sent.jsonl + cursor.json
    """
    def __init__(self, base_dir, node_name, secret=None):
        self.dir = os.path.join(base_dir, "mailbox")
        os.makedirs(self.dir, exist_ok=True)
        self.inbox_path = os.path.join(self.dir, "inbox.jsonl")
        self.outbox_path = os.path.join(self.dir, "outbox.jsonl")
        self.sent_path = os.path.join(self.dir, "sent.jsonl")
        self.cursor_path = os.path.join(self.dir, "cursor.json")
        self.node_name = node_name
        # v5.0: 落盘加密密钥（节点 auth_token 派生）。None=不加密（兼容旧部署）
        self.secret = secret or None
        # v5.0: RLock（可重入）—— sync_summary() 等方法持锁调用 outbox_pending()
        # 等内部加锁方法。原 Lock 不可重入，v4.1 起 gossip 同步一触发即死锁
        # （测试套件首轮捕获，见 tests/test_mail_engine.py::TestSync）。
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._agents = set([node_name])
        # v5.0.1: 邮箱固定 salt（mailbox/.salt）。写路径统一用它 -> 所有消息
        # 共享同一派生密钥（配合 _derive_key 缓存，写读均 O(1) 派生）。
        # blob 内仍自带 salt 字段，旧格式（每条独立 salt）解密自动兼容。
        self._salt = self._load_or_create_salt() if self.secret else None
        self._next_id = self._calc_next_id()
        # v5.0.7: 收件箱 msg_id 去重滑动窗口 —— gossip 双路径（直投 /fleet/mail
        # + 同步 /fleet/poll）在重试/重连场景可能把同一条消息投两次，
        # 窗口内重复 msg_id 直接丢弃。deque 做淘汰顺序，set 做 O(1) 查询。
        self._seen_ids = collections.deque(maxlen=4096)
        self._seen_set = set()

    def _load_or_create_salt(self):
        p = os.path.join(self.dir, ".salt")
        try:
            if os.path.exists(p):
                data = open(p, "rb").read()
                if len(data) >= 16:
                    return data[:16]
            s = os.urandom(16)
            with io.open(p, "wb") as f:
                f.write(s)
            return s
        except OSError:
            return os.urandom(16)   # 只读目录等场景：退化为每条随机 salt

    # -- v5.0: 落盘加密钩子 --
    def _encrypt_field(self, msg):
        """落盘前加密 text 字段（返回浅拷贝，不改调用方 dict）。结构字段
        （to/from/id/topic/status）保持明文以供路由与过滤；仅内容加密。"""
        if not self.secret:
            return msg
        m = dict(msg)
        t = m.get("text")
        if isinstance(t, str) and t and not t.startswith(CRYPTO_PREFIX):
            try:
                m["text"] = encrypt_text(self.secret, t, salt=self._salt)
            except Exception:
                pass
        return m

    def _decrypt_field(self, msg):
        """读取后解密 text 字段。旧明文数据（无 enc1$ 前缀）原样返回。"""
        if not self.secret:
            return msg
        m = dict(msg)
        t = m.get("text")
        if isinstance(t, str) and t.startswith(CRYPTO_PREFIX):
            try:
                m["text"] = decrypt_text(self.secret, t)
            except Exception:
                pass  # 旧密钥数据/损坏行：保留密文，不中断读取
        return m

    def _calc_next_id(self):
        msgs = self._load_jsonl(self.inbox_path)
        return (msgs[-1].get("id", 0) + 1) if msgs else 1

    # -- agent 注册表 --
    def register_agent(self, name):
        with self._lock:
            self._agents.add(name)

    def is_local_agent(self, name):
        with self._lock:
            return name in self._agents

    def local_agents(self):
        with self._lock:
            return sorted(self._agents)

    # -- inbox --
    def inbox_append(self, msg):
        with self._cond:
            mid = msg.get("msg_id") or msg.get("id")
            # v5.0.7: 滑动窗口去重（同一条消息重复投递只入一次）。无 msg_id
            # 的本地消息（如 agent 间直写）不受影响。
            if mid is not None:
                if mid in self._seen_set:
                    return False
                if len(self._seen_ids) >= 4096:
                    old = self._seen_ids.popleft()
                    self._seen_set.discard(old)
                self._seen_ids.append(mid)
                self._seen_set.add(mid)
            if "id" not in msg:
                msg["id"] = self._next_id
                self._next_id += 1
            with io.open(self.inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._encrypt_field(msg), ensure_ascii=False) + "\n")
            self._cond.notify_all()
            return True

    def inbox_read(self, agent_name, after_id=0, topic=None, mark_read=True):
        with self._lock:
            msgs = self._load_jsonl(self.inbox_path)
            got = [m for m in msgs
                   if m.get("id", 0) > after_id
                   and m.get("to") in (agent_name, "*")
                   and m.get("from") != agent_name]
            if topic:
                got = [m for m in got if m.get("topic") == topic]
            if got and mark_read:
                cursors = self._load_cursors()
                cursors[agent_name] = got[-1].get("id", 0)
                self._save_cursors(cursors)
            return got

    def inbox_cursor(self, agent_name):
        with self._lock:
            return self._load_cursors().get(agent_name, 0)

    def inbox_count(self, agent_name):
        with self._lock:
            msgs = self._load_jsonl(self.inbox_path)
            cursor = self._load_cursors().get(agent_name, 0)
            return len([m for m in msgs
                        if m.get("id", 0) > cursor
                        and m.get("to") in (agent_name, "*")
                        and m.get("from") != agent_name])

    # -- outbox --
    def outbox_append(self, msg):
        with self._lock:
            if "msg_id" not in msg:
                msg["msg_id"] = uuid.uuid4().hex[:16]
            msg["status"] = "pending"
            msg["attempts"] = 0
            msg["created_ts"] = time.time()
            with io.open(self.outbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._encrypt_field(msg), ensure_ascii=False) + "\n")

    def outbox_pending(self):
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            return [m for m in msgs if m.get("status") == "pending"]

    def outbox_retryable(self, now=None):
        """v5.0.2: 本轮投递循环可尝试的消息（退避过滤 + 上限过滤）。

        与 outbox_pending()（全量 pending，统计语义）分开：
        - 无 last_attempt_ts（新消息）-> 立即可试
        - 距上次失败不足 _retry_backoff(attempts) -> 跳过本轮
        - status=dead（超过 MAX_RETRY_ATTEMPTS）-> 永久排除
        """
        now = time.time() if now is None else now
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            out = []
            for m in msgs:
                if m.get("status") != "pending":
                    continue
                last = m.get("last_attempt_ts")
                if last is not None and now - last < _retry_backoff(m.get("attempts", 0)):
                    continue
                out.append(m)
            return out

    def outbox_mark_delivered(self, msg_id):
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            remaining = []
            delivered = None
            for m in msgs:
                if m.get("msg_id") == msg_id and m.get("status") == "pending":
                    m["status"] = "delivered"
                    m["delivered_ts"] = time.time()
                    delivered = m
                else:
                    remaining.append(m)
            self._save_jsonl(self.outbox_path, remaining)
            if delivered:
                with io.open(self.sent_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(self._encrypt_field(delivered), ensure_ascii=False) + "\n")

    def outbox_increment_attempt(self, msg_id):
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            for m in msgs:
                if m.get("msg_id") == msg_id and m.get("status") == "pending":
                    m["attempts"] = m.get("attempts", 0) + 1
                    m["last_attempt_ts"] = time.time()
                    # v5.0.2: 超过重试上限 -> 标 dead 停止重试（记录保留供审计）
                    if m["attempts"] >= MAX_RETRY_ATTEMPTS:
                        m["status"] = "dead"
                        m["dead_ts"] = time.time()
            self._save_jsonl(self.outbox_path, msgs)

    # -- poll（上线收信）--
    def poll_for_agent(self, agent_name, max_msgs=100):
        """被远程节点调用：拉取给 agent_name 的待收消息，从 inbox 移出。"""
        with self._lock:
            msgs = self._load_jsonl(self.inbox_path)
            to_deliver = []
            remaining = []
            for m in msgs:
                if (m.get("to") == agent_name or
                    (m.get("to") == "*" and m.get("from") != agent_name)):
                    if len(to_deliver) < max_msgs:
                        to_deliver.append(m)
                    else:
                        remaining.append(m)
                else:
                    remaining.append(m)
            self._save_jsonl(self.inbox_path, remaining)
            return to_deliver

    # -- sync（gossip）--
    def sync_summary(self):
        with self._lock:
            pending = self.outbox_pending()
            agents = {}
            for m in pending:
                to = m.get("to", "")
                agents[to] = agents.get(to, 0) + 1
            return {
                "node": self.node_name,
                "pending_count": len(pending),
                "pending_for": agents,
                "local_agents": list(self._agents),
                "ts": time.time(),
            }

    def sync_offer(self, agent_names):
        """对方问：你有给这些 agent 的消息吗？返回匹配的待投递消息。"""
        with self._lock:
            pending = self.outbox_pending()
            return [m for m in pending if m.get("to") in agent_names]

    # -- 统计 --
    def stats(self):
        with self._lock:
            inbox = self._load_jsonl(self.inbox_path)
            outbox = self._load_jsonl(self.outbox_path)
            pending = [m for m in outbox if m.get("status") == "pending"]
            dead = [m for m in outbox if m.get("status") == "dead"]
            inbox_bytes = 0
            try:
                inbox_bytes = os.path.getsize(self.inbox_path)
            except OSError:
                pass
            return {
                "inbox_total": len(inbox),
                "outbox_pending": len(pending),
                "outbox_dead": len(dead),          # v5.0.3: 死信数（长期离线目标）
                "inbox_bytes": inbox_bytes,        # v5.0.3: 邮箱体积（膨胀观测）
                "local_agents": sorted(self._agents),
            }

    def maintenance(self):
        """v5.0.3: 邮箱 compaction——长期运行防膨胀（NAS/软路由常年挂机场景）。

        三个泄漏源逐一处理：
        - inbox：总量超过 INBOX_KEEP_READ 时，从最旧开始移出「全部 agent
          已读（id <= min(各 cursor)）」的消息，清到回到窗口大小。未读消息
          永远保留（agent 长期未读的数据不是垃圾）；小邮箱（不超窗口）不动。
          设计权衡：邮箱承诺的是「离线期间的消息暂存」而非永久存档；未
          注册/从未读取的 agent 不参与 min 计算（其消息保守保留）。
        - sent：投递成功的历史截断保留最近 SENT_KEEP 条。
        - outbox dead：超过重试上限的死信截断保留最近 DEAD_KEEP 条。
        持锁原子写，复用 _save_jsonl 的加密回写路径（不破坏落盘加密）。
        返回 {"inbox": n, "sent": n, "dead": n} 清理统计。
        """
        cleaned = {"inbox": 0, "sent": 0, "dead": 0}
        with self._lock:
            # 1) inbox：总量超窗口时，从最旧开始清「全员已读」的消息；
            #    未读消息永远保留（有 agent 长期没来读，那是数据不是垃圾）。
            #    总量不超窗口的小邮箱不动（避免无谓的小写入）。
            cursors = self._load_cursors()
            min_cursor = min(cursors.values()) if cursors else 0
            msgs = self._load_jsonl(self.inbox_path)
            if len(msgs) > INBOX_KEEP_READ and min_cursor > 0:
                excess = len(msgs) - INBOX_KEEP_READ
                keep, removed = [], 0
                for m in msgs:                      # 从旧到新
                    if removed < excess and m.get("id", 0) <= min_cursor:
                        removed += 1
                        continue
                    keep.append(m)
                if removed:
                    self._save_jsonl(self.inbox_path, keep)
                    cleaned["inbox"] = removed
            # 2) sent 截断
            sent = self._load_jsonl(self.sent_path)
            if len(sent) > SENT_KEEP:
                self._save_jsonl(self.sent_path, sent[-SENT_KEEP:])
                cleaned["sent"] = len(sent) - SENT_KEEP
            # 3) outbox dead 截断
            out = self._load_jsonl(self.outbox_path)
            dead = [m for m in out if m.get("status") == "dead"]
            if len(dead) > DEAD_KEEP:
                keep_ids = set(m.get("msg_id") for m in dead[-DEAD_KEEP:])
                new_out = [m for m in out
                           if m.get("status") != "dead"
                           or m.get("msg_id") in keep_ids]
                self._save_jsonl(self.outbox_path, new_out)
                cleaned["dead"] = len(dead) - DEAD_KEEP
        return cleaned

    # -- 内部工具 --
    def _load_jsonl(self, path):
        msgs = []
        if os.path.exists(path):
            try:
                with io.open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                msgs.append(json.loads(line))
                            except ValueError:
                                pass
            except (OSError, IOError):
                pass
        # v5.0: 读取时自动解密 text（旧明文数据兼容）
        if self.secret:
            msgs = [self._decrypt_field(m) for m in msgs]
        return msgs

    def _save_jsonl(self, path, msgs):
        # v5.0.1 修复：_load_jsonl 读时解密 -> 修改（投递重试/成功/poll 移出）
        # -> 这里写回。原实现直接 json.dumps 落盘，密文一经任何重写操作
        # 即整体退化为明文（投递循环每 15s 触发一次，加密静默失效）。
        # 现在写盘前对称过 _encrypt_field（幂等：已是 enc1$ 的不会被二次加密）。
        if self.secret:
            msgs = [self._encrypt_field(m) for m in msgs]
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def _load_cursors(self):
        if os.path.exists(self.cursor_path):
            try:
                with io.open(self.cursor_path, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    def _save_cursors(self, cursors):
        tmp = self.cursor_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(cursors, f, ensure_ascii=False)
        os.replace(tmp, self.cursor_path)


# ---------------------------------------------------------------------------
# UDP 发现层 DiscoveryLayer
# ---------------------------------------------------------------------------
class DiscoveryLayer:
    """
    UDP 广播发现：同网段的 fleet 节点自动互相发现。
    不依赖 Tailscale / 任何外部服务。
    """
    def __init__(self, node_name, http_port, caps=None,
                 discover_port=DEFAULT_DISCOVER_PORT, on_discover=None,
                 sign_secret=None, verify_token_func=None):
        self.node_name = node_name
        self.http_port = http_port
        self.caps = caps or {}
        self.discover_port = discover_port
        self.on_discover = on_discover
        # v5.0: hello 防伪
        #   sign_secret       : 本节点 auth_token，用于给自己的 hello 签名
        #   verify_token_func : f(name) -> 该 peer 的 token（配对时保存），用于验签
        self.sign_secret = sign_secret or None
        self.verify_token_func = verify_token_func
        self._stop = threading.Event()
        self._sock = None
        self._known = {}
        self._rejected = 0  # 被签名校验拒绝的伪造包计数

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _make_hello(self):
        pkt = {
            "proto": PROTO_NAME, "ver": PROTO_VERSION,
            "name": self.node_name, "port": self.http_port,
            "caps": self.caps, "ts": time.time(),
        }
        # v5.0: 已配对节点可用存的 token 验证本签名，防伪造已知节点
        if self.sign_secret:
            pkt["sig"] = hello_signature(self.sign_secret,
                                         pkt["name"], pkt["port"], pkt["ts"])
        return json.dumps(pkt, ensure_ascii=False).encode("utf-8")

    def _verify_hello(self, pkt):
        """v5.0 hello 验签策略（保守兼容，不破坏旧网络）：
        - 无 sig（v4.x 旧节点）      -> 放行（兼容）
        - 未知节点（本地无其 token）  -> 放行（首次发现走原有流程）
        - 已知 peer 且带 sig         -> 必须验签通过，否则拒收（防冒充）
        """
        sig = pkt.get("sig", "")
        if not sig or not self.verify_token_func:
            return True
        try:
            token = self.verify_token_func(pkt.get("name", ""))
        except Exception:
            return True
        if not token:
            return True
        return verify_hello_signature(token, pkt)

    def _broadcast_loop(self):
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(1)
                hello = self._make_hello()
                sock.sendto(hello, ("255.255.255.255", self.discover_port))
                for subnet in ["192.168.1.255", "192.168.0.255",
                               "10.0.0.255", "172.16.0.255"]:
                    try:
                        sock.sendto(hello, (subnet, self.discover_port))
                    except Exception:
                        pass
            except Exception:
                # v5.0.6.1: 端口/权限异常等不中断线程，等下一轮重试
                pass
            finally:
                if sock:
                    sock.close()
            self._stop.wait(DISCOVER_INTERVAL)

    def _listen_loop(self):
        bind_failures = 0
        while not self._stop.is_set():
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except Exception:
                    pass
                self._sock.bind(("", self.discover_port))
                self._sock.settimeout(5)
                bind_failures = 0  # 重置失败计数
            except Exception as e:
                bind_failures += 1
                wait = min(2 ** bind_failures, 60)  # 指数退避，最大 60s
                # v5.0.6.1: 端口被占用/权限不足时不退订，等重试
                if bind_failures <= 3:
                    print("[mcp-fleet/warn] UDP 监听端口 %d 失败: %s — %ds 后重试"
                             % (self.discover_port, e, wait), file=sys.stderr)
                self._stop.wait(wait)
                continue
            # 正常接收循环
            while not self._stop.is_set():
                try:
                    data, addr = self._sock.recvfrom(4096)
                    if not data:
                        continue
                    try:
                        pkt = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if pkt.get("proto") != PROTO_NAME:
                        continue
                    if pkt.get("name") == self.node_name:
                        continue
                    # v5.0: 伪造已知节点（签名不符）直接丢弃
                    if not self._verify_hello(pkt):
                        self._rejected += 1
                        continue
                    name = pkt.get("name", "")
                    port = pkt.get("port", DEFAULT_HTTP_PORT)
                    caps = pkt.get("caps", {})
                    ip = addr[0]
                    self._known[name] = time.time()
                    if self.on_discover:
                        self.on_discover(name, ip, port, caps)
                except socket.timeout:
                    continue
                except Exception:
                    continue
            # 退出接收循环（_stop 已设置），清理 socket
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def known_nodes(self):
        return dict(self._known)


# ---------------------------------------------------------------------------
# 消息投递 MessageDelivery
# ---------------------------------------------------------------------------
def _opener_for(url):
    """v5.0.5: 内网/本机地址不走系统代理。

    Windows 装了代理软件（注册表代理未豁免 <local>）时，urllib 的默认
    opener 会把 127.0.0.1 / 192.168.x.x 的请求也发给代理服务器 —— 代理
    够不到用户的局域网，投递/gossip/poll 全部失败进死信，看门狗探活
    假阴性引发重启风暴。与 mcp_agent._peer_opener 同规则：
    回环/私网/链路本地/localhost/.local 直连，公网地址仍走系统代理。
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host == "localhost" or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        direct = direct or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


class MessageDelivery:
    """
    消息投递引擎：outbox -> HTTP POST /fleet/mail -> 目标节点
    1. 已知目标 URL -> 直接投递
    2. 广播(to=*) -> 发给所有 peer
    3. 未知目标 -> 留 outbox，等发现后重试
    """
    def __init__(self, mail_engine, get_peers_func, self_url="", health=None):
        self.mail = mail_engine
        self.get_peers = get_peers_func
        self.self_url = self_url
        # v5.0.2: 连接熔断器（与 SyncEngine 共享同一实例，见 mcp_agent._init_proto）
        self.health = health or PeerHealth()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def deliver_now(self, msg):
        """尝试立即投递。返回 True=成功 False=失败。

        v5.0.1 修复：目标匹配顺序 ——
        1. 消息自带 _target_url/_target_token
        2. to 是 peer 节点名 -> 投给该节点
        3. to 是远程 agent 名 -> 查各 peer 的 agents 归属（gossip 同步时
           对端上报 local_agents，由宿主节点记录），命中则投给宿主节点。
           原实现缺第 3 步：单播给远程 agent 永远 False，消息滞留 outbox
           无法自动投递（仅 to=* 广播可经 poll 到达）。
        4. to == "*" -> 广播
        """
        to = msg.get("to", "")
        url = msg.get("_target_url", "")
        token = msg.get("_target_token", "")
        if not url:
            peers = self.get_peers() if self.get_peers else {}
            if to in peers:
                url = peers[to].get("url", "")
                token = peers[to].get("token", "")
            elif to != "*":
                # v5.0.1: agent 归属匹配 —— 找到宣称收留该 agent 的 peer
                for pname, info in peers.items():
                    if to in (info.get("agents") or []):
                        url = info.get("url", "")
                        token = info.get("token", "")
                        break
            else:
                return self._broadcast(msg)
        if not url:
            return False
        return self._http_deliver(url, token, msg)

    def _http_deliver(self, url, token, msg):
        # v5.0.2: 冷却中的 peer 直接跳过——不让挂死的主机每轮白等一次超时
        if not self.health.is_available(url):
            return False
        base = url.rstrip("/")
        for suffix in ["/mcp", "/sse", "/rpc"]:
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        deliver_url = base + "/fleet/mail"
        payload = {
            "msg_id": msg.get("msg_id", uuid.uuid4().hex[:16]),
            "from": msg.get("from", ""),
            "to": msg.get("to", ""),
            "text": msg.get("text", ""),
            "topic": msg.get("topic", ""),
            "ts": msg.get("ts", time.strftime("%Y-%m-%d %H:%M:%S")),
            "source_node": self.mail.node_name,
            "hops": msg.get("hops", 0) + 1,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(deliver_url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        # v5.0.6.1: 重试 2 次（指数退避 1s/3s），网络抖动时消息不进死信
        attempts = 0
        while attempts < 3:
            try:
                with _opener_for(deliver_url).open(req, timeout=HTTP_TIMEOUT) as r:
                    ok = (r.status == 200)
                if ok:
                    self.health.record_success(url)
                    return True
                # 非 200 状态码（非网络错误）——不重试，直接失败
                self.health.record_failure(url)
                return False
            except Exception:
                attempts += 1
                if attempts < 3:
                    time.sleep(2 ** attempts)  # 1s, 3s 退避
        # 3 次均失败
        self.health.record_failure(url)
        return False

    def _broadcast(self, msg):
        peers = self.get_peers() if self.get_peers else {}
        any_ok = False
        for name, info in peers.items():
            url = info.get("url", "")
            token = info.get("token", "")
            if url and self._http_deliver(url, token, msg):
                any_ok = True
        return any_ok

    def delivery_loop(self):
        """后台 daemon：定期重试 outbox 投递。

        v5.0.2: 用 outbox_retryable()（退避过滤）替代全量 pending——
        刚失败的消息按退避间隔跳过，避免死节点消息每轮阻塞循环；
        单轮最多 DELIVERY_BATCH_MAX 条，防止超大 outbox 挤占循环周期。
        v5.0.3: 每 MAINTENANCE_INTERVAL 顺带触发一次邮箱 compaction
        （不新增线程；维护在投递间隙执行，持锁原子写）。
        """
        last_maint = 0.0
        while not self._stop.is_set():
            try:
                pending = self.mail.outbox_retryable()[:DELIVERY_BATCH_MAX]
                for msg in pending:
                    msg_id = msg.get("msg_id", "")
                    if self.deliver_now(msg):
                        self.mail.outbox_mark_delivered(msg_id)
                    else:
                        self.mail.outbox_increment_attempt(msg_id)
                now = time.time()
                if now - last_maint >= MAINTENANCE_INTERVAL:
                    try:
                        self.mail.maintenance()
                    except Exception:
                        pass
                    last_maint = now
            except Exception:
                pass
            self._stop.wait(DELIVERY_INTERVAL)

    def start_delivery(self):
        threading.Thread(target=self.delivery_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# 邮箱同步 SyncEngine -- gossip 协议
# ---------------------------------------------------------------------------
class SyncEngine:
    """
    节点间邮箱同步：定期向已知 peer 发 sync_summary，
    如果对方有给我的本地 agent 的消息，就 poll 过来。
    """
    def __init__(self, mail_engine, get_peers_func, self_url="",
                 on_peer_agents=None, health=None):
        self.mail = mail_engine
        self.get_peers = get_peers_func
        self.self_url = self_url
        # v5.0.1: 对端 gossip 响应携带 local_agents 时回调
        # f(peer_node_name, [agent, ...])，宿主侧记入 peers 用于投递归属
        self.on_peer_agents = on_peer_agents
        # v5.0.2: 与 MessageDelivery 共享的连接熔断器
        self.health = health or PeerHealth()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def sync_with_peer(self, url, token):
        """和一个 peer 同步：告诉它我的本地 agent，拉取给我的消息。"""
        # v5.0.2: 冷却中的 peer 跳过本轮 gossip（死节点不再拖慢同步循环）
        if not self.health.is_available(url):
            return None
        try:
            base = url.rstrip("/")
            for suffix in ["/mcp", "/sse", "/rpc"]:
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            # 1. 发 sync 请求：告诉对方我的本地 agent
            sync_url = base + "/fleet/sync"
            payload = {
                "node": self.mail.node_name,
                "local_agents": self.mail.local_agents(),
                "ts": time.time(),
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(sync_url, data=data, method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with _opener_for(sync_url).open(req, timeout=HTTP_TIMEOUT) as r:
                resp = json.loads(r.read().decode("utf-8"))
            self.health.record_success(url)   # v5.0.2: gossip 通了即恢复健康

            # v5.0.1: 记录对端 agent 归属（用于 deliver_now 的远程 agent 匹配）
            if self.on_peer_agents and isinstance(resp, dict):
                try:
                    self.on_peer_agents(resp.get("node", ""),
                                        resp.get("local_agents") or [])
                except Exception:
                    pass

            # 2. 如果对方有给我的 agent 的消息，逐个 poll
            for agent in self.mail.local_agents():
                if agent == self.mail.node_name:
                    continue
                msgs = self._poll_peer(base, token, agent)
                for m in msgs:
                    self.mail.inbox_append(m)
            return resp
        except Exception as e:
            # v5.0.2: gossip 失败计入熔断（与投递共享冷却状态）
            self.health.record_failure(url)
            # v5.0.6.1: 失败日志便于调试连接问题
            print("[mcp-fleet/debug] sync_with_peer %s failed: %s"
                  % (url, e), file=sys.stderr)
            return None

    def _poll_peer(self, base, token, agent_name):
        """从 peer 拉取给 agent_name 的消息。"""
        try:
            poll_url = base + "/fleet/poll?name=" + urllib.parse.quote(agent_name)
            req = urllib.request.Request(poll_url)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with _opener_for(poll_url).open(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8")).get("messages", [])
        except Exception as e:
            print("[mcp-fleet/debug] _poll_peer %s failed: %s"
                  % (base, e), file=sys.stderr)
            return []

    def sync_loop(self):
        """后台 daemon：定期和所有 peer 同步邮箱。"""
        while not self._stop.is_set():
            try:
                peers = self.get_peers() if self.get_peers else {}
                for name, info in peers.items():
                    url = info.get("url", "")
                    token = info.get("token", "")
                    if url:
                        self.sync_with_peer(url, token)
            except Exception:
                pass
            self._stop.wait(SYNC_INTERVAL)

    def start_sync(self):
        threading.Thread(target=self.sync_loop, daemon=True).start()
