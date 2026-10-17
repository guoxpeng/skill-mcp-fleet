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

import json, os, re, socket, threading, time, uuid, io
import urllib.request, urllib.parse, urllib.error

PROTO_NAME = "fleet"
PROTO_VERSION = 1
DEFAULT_DISCOVER_PORT = 3199
DEFAULT_HTTP_PORT = 3100
DISCOVER_INTERVAL = 30
DELIVERY_INTERVAL = 15
SYNC_INTERVAL = 60
MAX_HOPS = 5


# ---------------------------------------------------------------------------
# 邮箱引擎 MailEngine -- store-and-forward 核心
# ---------------------------------------------------------------------------
class MailEngine:
    """
    每个节点一个 MailEngine，管理本地邮箱。
    mailbox/inbox.jsonl + outbox.jsonl + sent.jsonl + cursor.json
    """
    def __init__(self, base_dir, node_name):
        self.dir = os.path.join(base_dir, "mailbox")
        os.makedirs(self.dir, exist_ok=True)
        self.inbox_path = os.path.join(self.dir, "inbox.jsonl")
        self.outbox_path = os.path.join(self.dir, "outbox.jsonl")
        self.sent_path = os.path.join(self.dir, "sent.jsonl")
        self.cursor_path = os.path.join(self.dir, "cursor.json")
        self.node_name = node_name
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._agents = set([node_name])
        self._next_id = self._calc_next_id()

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
            if "id" not in msg:
                msg["id"] = self._next_id
                self._next_id += 1
            with io.open(self.inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._cond.notify_all()

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
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def outbox_pending(self):
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            return [m for m in msgs if m.get("status") == "pending"]

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
                    f.write(json.dumps(delivered, ensure_ascii=False) + "\n")

    def outbox_increment_attempt(self, msg_id):
        with self._lock:
            msgs = self._load_jsonl(self.outbox_path)
            for m in msgs:
                if m.get("msg_id") == msg_id and m.get("status") == "pending":
                    m["attempts"] = m.get("attempts", 0) + 1
                    m["last_attempt_ts"] = time.time()
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
            return {
                "inbox_total": len(inbox),
                "outbox_pending": len(pending),
                "local_agents": sorted(self._agents),
            }

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
        return msgs

    def _save_jsonl(self, path, msgs):
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
                 discover_port=DEFAULT_DISCOVER_PORT, on_discover=None):
        self.node_name = node_name
        self.http_port = http_port
        self.caps = caps or {}
        self.discover_port = discover_port
        self.on_discover = on_discover
        self._stop = threading.Event()
        self._sock = None
        self._known = {}

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
        return json.dumps({
            "proto": PROTO_NAME, "ver": PROTO_VERSION,
            "name": self.node_name, "port": self.http_port,
            "caps": self.caps, "ts": time.time(),
        }, ensure_ascii=False).encode("utf-8")

    def _broadcast_loop(self):
        while not self._stop.is_set():
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
                sock.close()
            except Exception:
                pass
            self._stop.wait(DISCOVER_INTERVAL)

    def _listen_loop(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception:
                pass
            self._sock.bind(("", self.discover_port))
            self._sock.settimeout(5)
        except Exception:
            return
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

    def known_nodes(self):
        return dict(self._known)


# ---------------------------------------------------------------------------
# 消息投递 MessageDelivery
# ---------------------------------------------------------------------------
class MessageDelivery:
    """
    消息投递引擎：outbox -> HTTP POST /fleet/mail -> 目标节点
    1. 已知目标 URL -> 直接投递
    2. 广播(to=*) -> 发给所有 peer
    3. 未知目标 -> 留 outbox，等发现后重试
    """
    def __init__(self, mail_engine, get_peers_func, self_url=""):
        self.mail = mail_engine
        self.get_peers = get_peers_func
        self.self_url = self_url
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def deliver_now(self, msg):
        """尝试立即投递。返回 True=成功 False=失败。"""
        to = msg.get("to", "")
        url = msg.get("_target_url", "")
        token = msg.get("_target_token", "")
        if not url:
            peers = self.get_peers() if self.get_peers else {}
            if to in peers:
                url = peers[to].get("url", "")
                token = peers[to].get("token", "")
            elif to == "*":
                return self._broadcast(msg)
        if not url:
            return False
        return self._http_deliver(url, token, msg)

    def _http_deliver(self, url, token, msg):
        try:
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
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
        except Exception:
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
        """后台 daemon：定期重试 outbox 投递。"""
        while not self._stop.is_set():
            try:
                pending = self.mail.outbox_pending()
                for msg in pending:
                    msg_id = msg.get("msg_id", "")
                    if self.deliver_now(msg):
                        self.mail.outbox_mark_delivered(msg_id)
                    else:
                        self.mail.outbox_increment_attempt(msg_id)
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
    def __init__(self, mail_engine, get_peers_func, self_url=""):
        self.mail = mail_engine
        self.get_peers = get_peers_func
        self.self_url = self_url
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def sync_with_peer(self, url, token):
        """和一个 peer 同步：告诉它我的本地 agent，拉取给我的消息。"""
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
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read().decode("utf-8"))

            # 2. 如果对方有给我的 agent 的消息，逐个 poll
            for agent in self.mail.local_agents():
                if agent == self.mail.node_name:
                    continue
                msgs = self._poll_peer(base, token, agent)
                for m in msgs:
                    self.mail.inbox_append(m)
            return resp
        except Exception:
            return None

    def _poll_peer(self, base, token, agent_name):
        """从 peer 拉取给 agent_name 的消息。"""
        try:
            poll_url = base + "/fleet/poll?name=" + urllib.parse.quote(agent_name)
            req = urllib.request.Request(poll_url)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8")).get("messages", [])
        except Exception:
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
