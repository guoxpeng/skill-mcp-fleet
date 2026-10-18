# -*- coding: utf-8 -*-
"""消息层测试：MailEngine（读写/游标/加密落盘/并发）+ MessageDelivery（三分支投递决策）。"""
import json
import os
import threading

import pytest

from fleet_proto import CRYPTO_PREFIX, MailEngine, MessageDelivery


@pytest.fixture
def tmp_engine(tmp_path):
    return MailEngine(str(tmp_path), "node-a")


@pytest.fixture
def enc_engine(tmp_path):
    return MailEngine(str(tmp_path), "node-a", secret="my-secret")


class TestInbox:
    def test_append_and_read(self, tmp_engine):
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "hi"})
        got = tmp_engine.inbox_read("node-a")
        assert len(got) == 1 and got[0]["text"] == "hi"
        assert got[0]["id"] == 1

    def test_auto_increment_ids(self, tmp_engine):
        for i in range(3):
            tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": str(i)})
        got = tmp_engine.inbox_read("node-a", mark_read=False)
        assert [m["id"] for m in got] == [1, 2, 3]

    def test_cursor_advances(self, tmp_engine):
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "1"})
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "2"})
        tmp_engine.inbox_read("node-a")                 # 读完游标=2
        assert tmp_engine.inbox_cursor("node-a") == 2
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "3"})
        assert tmp_engine.inbox_count("node-a") == 1    # 只剩未读

    def test_topic_filter(self, tmp_engine):
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "x", "topic": "ops"})
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "y", "topic": "dev"})
        got = tmp_engine.inbox_read("node-a", topic="ops", mark_read=False)
        assert len(got) == 1 and got[0]["text"] == "x"

    def test_broadcast_and_self_filter(self, tmp_engine):
        """to=* 广播可读；自己的消息不回读给自己。"""
        tmp_engine.inbox_append({"from": "b", "to": "*", "text": "all"})
        tmp_engine.inbox_append({"from": "node-a", "to": "node-a", "text": "self"})
        got = tmp_engine.inbox_read("node-a", mark_read=False)
        assert len(got) == 1 and got[0]["text"] == "all"

    def test_register_agent(self, tmp_engine):
        tmp_engine.register_agent("agent-x")
        tmp_engine.inbox_append({"from": "b", "to": "agent-x", "text": "go"})
        assert tmp_engine.is_local_agent("agent-x")
        assert tmp_engine.inbox_read("agent-x", mark_read=False)[0]["text"] == "go"


class TestOutbox:
    def test_pending_and_delivered(self, tmp_engine):
        tmp_engine.outbox_append({"from": "a", "to": "offline-node", "text": "later"})
        pend = tmp_engine.outbox_pending()
        assert len(pend) == 1 and pend[0]["status"] == "pending"
        mid = pend[0]["msg_id"]
        tmp_engine.outbox_mark_delivered(mid)
        assert tmp_engine.outbox_pending() == []
        assert os.path.exists(tmp_engine.sent_path)

    def test_increment_attempt(self, tmp_engine):
        tmp_engine.outbox_append({"from": "a", "to": "x", "text": "t"})
        mid = tmp_engine.outbox_pending()[0]["msg_id"]
        tmp_engine.outbox_increment_attempt(mid)
        tmp_engine.outbox_increment_attempt(mid)
        assert tmp_engine.outbox_pending()[0]["attempts"] == 2

    def test_poll_moves_to_deliver(self, tmp_engine):
        """poll_for_agent：拉走给该 agent 的消息并从 inbox 移除。"""
        tmp_engine.inbox_append({"from": "a", "to": "agent-y", "text": "pick"})
        tmp_engine.inbox_append({"from": "a", "to": "other", "text": "stay"})
        got = tmp_engine.poll_for_agent("agent-y")
        assert len(got) == 1 and got[0]["text"] == "pick"
        rest = tmp_engine.inbox_read("other", mark_read=False)
        assert rest[0]["text"] == "stay"


class TestSync:
    def test_sync_summary(self, tmp_engine):
        tmp_engine.outbox_append({"from": "a", "to": "n1", "text": "1"})
        tmp_engine.outbox_append({"from": "a", "to": "n2", "text": "2"})
        s = tmp_engine.sync_summary()
        assert s["pending_count"] == 2 and s["pending_for"] == {"n1": 1, "n2": 1}

    def test_sync_offer(self, tmp_engine):
        tmp_engine.outbox_append({"from": "a", "to": "n1", "text": "for n1"})
        tmp_engine.outbox_append({"from": "a", "to": "n9", "text": "for n9"})
        offer = tmp_engine.sync_offer(["n1"])
        assert len(offer) == 1 and offer[0]["to"] == "n1"


class TestEncryption:
    def test_disk_is_ciphertext(self, enc_engine, tmp_path):
        enc_engine.inbox_append({"from": "b", "to": "node-a", "text": "机密内容123"})
        disk = open(enc_engine.inbox_path, encoding="utf-8").read()
        assert "机密内容123" not in disk
        assert CRYPTO_PREFIX in disk

    def test_read_returns_plaintext(self, enc_engine):
        enc_engine.inbox_append({"from": "b", "to": "node-a", "text": "hello 加密"})
        assert enc_engine.inbox_read("node-a", mark_read=False)[0]["text"] == "hello 加密"

    def test_structure_fields_stay_plain(self, enc_engine):
        """路由字段（to/from/topic）必须保持明文供过滤。"""
        enc_engine.inbox_append({"from": "b", "to": "node-a", "text": "x", "topic": "ops"})
        line = json.loads(open(enc_engine.inbox_path, encoding="utf-8").readline())
        assert line["to"] == "node-a" and line["topic"] == "ops"

    def test_legacy_plaintext_compatible(self, tmp_path):
        """secret=None 时旧明文数据原样可读。"""
        me = MailEngine(str(tmp_path), "n", secret=None)
        me.inbox_append({"from": "b", "to": "n", "text": "旧明文"})
        assert me.inbox_read("n", mark_read=False)[0]["text"] == "旧明文"

    def test_outbox_and_sent_encrypted(self, enc_engine):
        enc_engine.outbox_append({"from": "a", "to": "x", "text": "待投递"})
        disk = open(enc_engine.outbox_path, encoding="utf-8").read()
        assert "待投递" not in disk
        assert enc_engine.outbox_pending()[0]["text"] == "待投递"


class TestConcurrency:
    def test_threaded_append(self, tmp_engine):
        """50 线程并发 append，落盘行数完整无损坏。"""
        def worker(i):
            tmp_engine.inbox_append({"from": "w%d" % i, "to": "node-a", "text": "m%d" % i})
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = tmp_engine.inbox_read("node-a", mark_read=False)
        assert len(got) == 50
        assert len({m["id"] for m in got}) == 50   # id 不重复


class TestStats:
    def test_stats(self, tmp_engine):
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "1"})
        tmp_engine.outbox_append({"from": "a", "to": "x", "text": "2"})
        s = tmp_engine.stats()
        assert s["inbox_total"] == 1 and s["outbox_pending"] == 1


class TestDeliverNow:
    def _make(self, tmp_path, peers):
        me = MailEngine(str(tmp_path), "node-a")
        return MessageDelivery(me, lambda: peers)

    def test_known_peer_direct(self, tmp_path, monkeypatch):
        """分支 1：已知 peer -> 用其 url+token 直投。"""
        d = self._make(tmp_path, {"node-b": {"url": "http://1.2.3.4:3100/mcp", "token": "tk"}})
        calls = []
        monkeypatch.setattr(d, "_http_deliver",
                            lambda url, token, msg: calls.append((url, token)) or True)
        ok = d.deliver_now({"to": "node-b", "text": "hi"})
        assert ok and calls == [("http://1.2.3.4:3100/mcp", "tk")]

    def test_unknown_target_stays_outbox(self, tmp_path, monkeypatch):
        """分支 3：未知目标 -> 返回 False（消息留 outbox 等重试）。"""
        d = self._make(tmp_path, {})
        monkeypatch.setattr(d, "_http_deliver", lambda *a: True)  # 不应被调用
        assert d.deliver_now({"to": "ghost", "text": "x"}) is False

    def test_broadcast_to_all_peers(self, tmp_path, monkeypatch):
        """分支 2：to=* -> 发给所有 peer，任一成功即 True。"""
        peers = {"b": {"url": "http://b:1/mcp", "token": ""},
                 "c": {"url": "http://c:1/mcp", "token": ""}}
        d = self._make(tmp_path, peers)
        seen = []
        monkeypatch.setattr(d, "_http_deliver",
                            lambda url, token, msg: seen.append(url) or True)
        assert d.deliver_now({"to": "*", "text": "bc"}) is True
        assert sorted(seen) == ["http://b:1/mcp", "http://c:1/mcp"]

    def test_broadcast_all_fail(self, tmp_path, monkeypatch):
        d = self._make(tmp_path, {"b": {"url": "http://b:1/mcp", "token": ""}})
        monkeypatch.setattr(d, "_http_deliver", lambda *a: False)
        assert d.deliver_now({"to": "*", "text": "x"}) is False

    def test_explicit_target_url_wins(self, tmp_path, monkeypatch):
        """消息自带 _target_url/_target_token 时优先。"""
        d = self._make(tmp_path, {})
        calls = []
        monkeypatch.setattr(d, "_http_deliver",
                            lambda url, token, msg: calls.append((url, token)) or True)
        d.deliver_now({"to": "z", "text": "t",
                       "_target_url": "http://explicit:9/mcp", "_target_token": "tt"})
        assert calls == [("http://explicit:9/mcp", "tt")]

    def test_delivery_loop_marks_status(self, tmp_path, monkeypatch):
        """重试循环一轮：成功 -> outbox 移入 sent；失败 -> attempts +1。"""
        d = self._make(tmp_path, {"b": {"url": "http://b:1/mcp", "token": ""}})
        d.mail.outbox_append({"from": "a", "to": "b", "text": "first"})
        d.mail.outbox_append({"from": "a", "to": "ghost", "text": "second"})
        monkeypatch.setattr(d, "_http_deliver",
                            lambda url, token, msg: url == "http://b:1/mcp")
        # 让循环体跑一轮后经 wait 钩子置 stop 退出（单轮验证）
        def _wait_then_stop(*args, **kwargs):
            d._stop.set()
        monkeypatch.setattr(d._stop, "wait", _wait_then_stop)
        d.delivery_loop()

        pend = d.mail.outbox_pending()
        assert len(pend) == 1 and pend[0]["to"] == "ghost"
        assert pend[0]["attempts"] == 1          # 失败重试计数增加
        sent = d.mail._load_jsonl(d.mail.sent_path)
        assert len(sent) == 1 and sent[0]["to"] == "b"  # 成功进 sent


# ===========================================================================
# v5.0.2 连接稳定性：退避 / 重试上限 / per-peer 熔断
# ===========================================================================
class TestConnectionStability:
    """B5/B6 修复护栏：死节点不再拖垮投递与同步循环。"""

    def test_retry_backoff_curve(self):
        """退避曲线：0-4 次 15s；5-9 次 60s；10-14 次 240s；20+ 封顶 3600s。"""
        from fleet_proto import _retry_backoff
        assert _retry_backoff(0) == 15
        assert _retry_backoff(4) == 15
        assert _retry_backoff(5) == 60
        assert _retry_backoff(9) == 60
        assert _retry_backoff(10) == 240
        assert _retry_backoff(20) == 3600
        assert _retry_backoff(999) == 3600     # 封顶

    def test_outbox_retryable_filters_backoff(self, tmp_engine):
        """刚失败的消息按退避间隔排除；间隔过后重新可试。"""
        import time as _t
        me = tmp_engine
        me.outbox_append({"from": "a", "to": "ghost", "text": "x"})
        mid = me.outbox_pending()[0]["msg_id"]
        me.outbox_increment_attempt(mid)          # attempts=1, last=now

        # 距上次失败 5s < 15s 退避 -> 本轮不可试
        assert me.outbox_retryable(now=_t.time() + 5) == []
        # 距上次失败 16s -> 可试
        r = me.outbox_retryable(now=_t.time() + 16)
        assert len(r) == 1 and r[0]["msg_id"] == mid

    def test_outbox_retryable_new_msg_immediate(self, tmp_engine):
        """新消息（无 last_attempt_ts）立即可试，行为与旧版一致。"""
        tmp_engine.outbox_append({"from": "a", "to": "ghost", "text": "n"})
        assert len(tmp_engine.outbox_retryable()) == 1
        assert len(tmp_engine.outbox_pending()) == 1   # 全量语义不变

    def test_max_attempts_marks_dead(self, tmp_engine):
        """超过 MAX_RETRY_ATTEMPTS -> status=dead，pending/retryable 均排除。"""
        from fleet_proto import MAX_RETRY_ATTEMPTS
        me = tmp_engine
        me.outbox_append({"from": "a", "to": "ghost", "text": "x"})
        mid = me.outbox_pending()[0]["msg_id"]
        # 直接把 attempts 拉到上限-1，再一次 increment 触发 dead
        with me._lock:
            msgs = me._load_jsonl(me.outbox_path)
            for m in msgs:
                if m.get("msg_id") == mid:
                    m["attempts"] = MAX_RETRY_ATTEMPTS - 1
            me._save_jsonl(me.outbox_path, msgs)
        me.outbox_increment_attempt(mid)
        assert me.outbox_pending() == []            # 不再 pending
        assert me.outbox_retryable() == []
        dead = me._load_jsonl(me.outbox_path)
        assert len(dead) == 1 and dead[0]["status"] == "dead"   # 记录保留供审计

    def test_peer_health_breaker_state_machine(self):
        """熔断状态机：2 次失败 -> 冷却 90s；失败翻倍；成功清零。"""
        from fleet_proto import PeerHealth
        h = PeerHealth()
        t0 = 1000.0
        assert h.is_available("p1", now=t0)          # 无记录 -> 可用
        h.record_failure("p1", now=t0)
        assert h.is_available("p1", now=t0)          # 1 次未达阈值
        h.record_failure("p1", now=t0)               # 2 次 -> 进入冷却
        assert not h.is_available("p1", now=t0)
        assert not h.is_available("p1", now=t0 + 89)
        assert h.is_available("p1", now=t0 + 90)     # 冷却到期，放行探测
        h.record_failure("p1", now=t0 + 90)          # 探测失败 -> 冷却翻倍 180s
        h.record_failure("p1", now=t0 + 90)
        assert not h.is_available("p1", now=t0 + 90 + 179)
        assert h.is_available("p1", now=t0 + 90 + 180)
        h.record_success("p1")                       # 成功 -> 完全恢复
        assert h.is_available("p1", now=t0)
        assert h.cooldown_remaining("p1", now=t0) == 0

    def test_peer_health_independent_keys(self):
        """不同 peer 的熔断状态互不影响。"""
        from fleet_proto import PeerHealth
        h = PeerHealth()
        h.record_failure("a")
        h.record_failure("a")
        assert not h.is_available("a")
        assert h.is_available("b")                   # b 不受 a 连累

    def test_deliver_skips_cooldown_peer(self, tmp_path):
        """冷却中的 peer：投递直接 False，不发 HTTP（不打 urlopen）。"""
        me = MailEngine(str(tmp_path), "node-a")
        d = MessageDelivery(me, lambda: {"b": {"url": "http://b:1/mcp", "token": ""}})
        d.health.record_failure("http://b:1/mcp")
        d.health.record_failure("http://b:1/mcp")   # 进入冷却
        called = []
        import urllib.request as _u
        orig = _u.urlopen
        monkey_urlopen = lambda *a, **k: called.append(1) or orig(*a, **k)
        _u.urlopen = monkey_urlopen
        try:
            assert d.deliver_now({"to": "b", "text": "x"}) is False
        finally:
            _u.urlopen = orig
        assert called == []                          # 零 HTTP 尝试

    def test_sync_skips_cooldown_peer(self, tmp_path, monkeypatch):
        """冷却中的 peer：gossip 同步跳过（sync_with_peer 返回 None 且不发请求）。"""
        me = MailEngine(str(tmp_path), "node-a")
        from fleet_proto import SyncEngine
        s = SyncEngine(me, lambda: {})
        s.health.record_failure("http://dead:1/mcp")
        s.health.record_failure("http://dead:1/mcp")
        hits = []
        monkeypatch.setattr(s, "_poll_peer",
                            lambda *a: hits.append(1) or [])
        # sync_with_peer 应在 health 检查处直接短路（后续代码不执行）
        assert s.sync_with_peer("http://dead:1/mcp", "") is None
        assert hits == []

    def test_failure_recording_on_dead_peer(self, tmp_path):
        """对真实不可达端口的投递失败 -> 熔断器计数（连通性异常路径）。"""
        me = MailEngine(str(tmp_path), "node-a")
        d = MessageDelivery(me, lambda: {})
        url = "http://127.0.0.1:1/mcp"               # 端口 1 必然拒绝
        assert d._http_deliver(url, "", {"to": "x", "text": "t"}) is False
        assert d._http_deliver(url, "", {"to": "x", "text": "t"}) is False
        assert not d.health.is_available(url)        # 2 次失败 -> 冷却


# ===========================================================================
# v5.0.3 邮箱维护（compaction）：长期运行防膨胀
# ===========================================================================
class TestMaintenance:
    def test_compacts_read_inbox_over_window(self, tmp_engine):
        """超窗口（500）的已读消息被清理到窗口大小；未超窗口不动。"""
        from fleet_proto import INBOX_KEEP_READ
        me = tmp_engine
        for i in range(INBOX_KEEP_READ + 300):
            me.inbox_append({"from": "remote", "to": "agent-1", "text": "m%d" % i})
        me.inbox_read("agent-1", after_id=0)          # cursor 到顶 -> 全员已读
        cleaned = me.maintenance()
        assert cleaned["inbox"] == 300                 # 清 300 回到窗口
        left = me._load_jsonl(me.inbox_path)
        assert len(left) == INBOX_KEEP_READ
        assert left[-1]["text"] == "m%d" % (INBOX_KEEP_READ + 299)   # 保留最新

    def test_small_inbox_untouched(self, tmp_engine):
        """不超窗口的小邮箱不清理（避免无谓写入）。"""
        me = tmp_engine
        for i in range(50):
            me.inbox_append({"from": "r", "to": "a1", "text": "m%d" % i})
        me.inbox_read("a1", after_id=0)
        cleaned = me.maintenance()
        assert cleaned["inbox"] == 0
        assert len(me._load_jsonl(me.inbox_path)) == 50

    def test_keeps_unread_when_agent_behind(self, tmp_engine):
        """未读消息永不被清：agent-2 落后时，其未读全保留，只清 agent-1 已读。"""
        from fleet_proto import INBOX_KEEP_READ
        me = tmp_engine
        for i in range(INBOX_KEEP_READ + 100):        # agent-1 的消息
            me.inbox_append({"from": "r", "to": "agent-1", "text": "m%d" % i})
        me.inbox_read("agent-1", after_id=0)          # agent-1 cursor=600
        for i in range(5):                            # agent-2 未读（无 cursor）
            me.inbox_append({"from": "r", "to": "agent-2", "text": "late%d" % i})
        cleaned = me.maintenance()
        # 总量 605，窗口 500，excess=105：从最旧清「id<=600 且已读」-> 105 条
        assert cleaned["inbox"] == 105
        left = me._load_jsonl(me.inbox_path)
        assert len([m for m in left if m["to"] == "agent-2"]) == 5   # 未读全保
        assert len(left) == 500

    def test_compaction_keeps_encryption(self, enc_engine):
        """清理路径复用 _save_jsonl 加密回写：maintenance 后落盘仍密文（B1 交互）。"""
        from fleet_proto import CRYPTO_PREFIX, INBOX_KEEP_READ
        me = enc_engine                                 # secret="my-secret"
        for i in range(INBOX_KEEP_READ + 10):
            me.inbox_append({"from": "r", "to": "a1", "text": "m%d" % i})
        me.inbox_read("a1", after_id=0)
        cleaned = me.maintenance()
        assert cleaned["inbox"] == 10
        raw = open(me.inbox_path, encoding="utf-8").read()
        assert CRYPTO_PREFIX in raw                     # 密文保持
        # 且解密读取正常
        assert len(me.inbox_read("a1", after_id=0)) == INBOX_KEEP_READ

    def test_truncates_sent(self, tmp_engine):
        """sent.jsonl 超过 SENT_KEEP 截断，保留最近。"""
        from fleet_proto import SENT_KEEP
        me = tmp_engine
        with open(me.sent_path, "a", encoding="utf-8") as f:
            for i in range(SENT_KEEP + 100):
                f.write(json.dumps({"to": "b", "text": "s%d" % i, "id": i}) + "\n")
        cleaned = me.maintenance()
        assert cleaned["sent"] == 100
        sent = me._load_jsonl(me.sent_path)
        assert len(sent) == SENT_KEEP and sent[-1]["text"] == "s%d" % (SENT_KEEP + 99)

    def test_truncates_dead_outbox(self, tmp_engine):
        """outbox 的 dead 消息超过 DEAD_KEEP 截断；pending/delivered 不受影响。"""
        from fleet_proto import DEAD_KEEP
        me = tmp_engine
        rows = [{"msg_id": "dead-%03d" % i, "status": "dead", "text": "d%d" % i,
                 "id": i, "to": "x"} for i in range(DEAD_KEEP + 50)]
        rows.append({"msg_id": "pend-1", "status": "pending", "text": "p", "id": 999, "to": "x"})
        rows.append({"msg_id": "ok-1", "status": "delivered", "text": "o", "id": 998, "to": "x"})
        me._save_jsonl(me.outbox_path, rows)
        cleaned = me.maintenance()
        assert cleaned["dead"] == 50
        out = me._load_jsonl(me.outbox_path)
        assert len([m for m in out if m["status"] == "dead"]) == DEAD_KEEP
        assert len([m for m in out if m["status"] == "pending"]) == 1
        assert len([m for m in out if m["status"] == "delivered"]) == 1

    def test_stats_new_fields(self, tmp_engine):
        """stats() 暴露 outbox_dead / inbox_bytes（Dashboard 与 doctor 用）。"""
        me = tmp_engine
        me.outbox_append({"from": "a", "to": "ghost", "text": "x"})
        st = me.stats()
        assert st["inbox_total"] == 0 and st["outbox_pending"] == 1
        assert st["outbox_dead"] == 0
        assert st["inbox_bytes"] == 0          # 空文件或不存在
        me.inbox_append({"from": "r", "to": "a", "text": "hello"})
        st2 = me.stats()
        assert st2["inbox_total"] == 1 and st2["inbox_bytes"] > 0


class TestInboxDedup:
    """v5.0.7: 收件箱 msg_id 滑动窗口去重 —— gossip 双路径（直投 /fleet/mail
    + 同步 /fleet/poll）在重试/重连场景重复投递同一条消息时只入一次。"""

    def test_duplicate_msg_id_skipped(self, tmp_engine):
        ok1 = tmp_engine.inbox_append({"msg_id": "m1", "from": "b", "to": "node-a", "text": "hi"})
        ok2 = tmp_engine.inbox_append({"msg_id": "m1", "from": "b", "to": "node-a", "text": "hi-dup"})
        assert ok1 is True and ok2 is False
        assert tmp_engine.inbox_count("node-a") == 1

    def test_same_id_twice_keeps_first(self, tmp_engine):
        tmp_engine.inbox_append({"msg_id": "m2", "from": "b", "to": "node-a", "text": "first"})
        tmp_engine.inbox_append({"msg_id": "m2", "from": "b", "to": "node-a", "text": "second"})
        got = tmp_engine.inbox_read("node-a", mark_read=False)
        assert len(got) == 1 and got[0]["text"] == "first"

    def test_no_msg_id_not_deduped(self, tmp_engine):
        """本地直写消息（无 msg_id）不受去重影响，每次都入箱。"""
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "x"})
        tmp_engine.inbox_append({"from": "b", "to": "node-a", "text": "y"})
        assert tmp_engine.inbox_count("node-a") == 2

    def test_encrypted_dedup(self, enc_engine):
        """加密存储下去重同样生效，且保留首条文本。"""
        ok1 = enc_engine.inbox_append({"msg_id": "m3", "from": "b", "to": "node-a", "text": "sec"})
        ok2 = enc_engine.inbox_append({"msg_id": "m3", "from": "b", "to": "node-a", "text": "sec"})
        assert ok1 is True and ok2 is False
        got = enc_engine.inbox_read("node-a")
        assert len(got) == 1 and got[0]["text"] == "sec"

    def test_dedup_window_eviction(self, tmp_engine):
        """窗口（4096）溢出后最旧 msg_id 被淘汰，可再次入箱。"""
        for i in range(4100):
            tmp_engine.inbox_append({"msg_id": "m%d" % i, "from": "b", "to": "node-a", "text": str(i)})
        ok = tmp_engine.inbox_append({"msg_id": "m0", "from": "b", "to": "node-a", "text": "again"})
        assert ok is True
        assert tmp_engine.inbox_count("node-a") == 4101

    def test_peer_id_collision_not_deduped(self, tmp_engine):
        """v5.0.8 回归：无 msg_id 的消息绝不参与去重。

        gossip poll 拉回的消息携带对端 inbox 的自增 id（poll_for_agent
        原样返回对端落盘数据），各节点 id 空间独立且都是小整数。v5.0.7
        误用 "msg_id or id" 做去重键，两个不同 peer 的 id 碰撞时后到的
        消息被静默丢弃。"""
        ok1 = tmp_engine.inbox_append({"id": 42, "from": "peerA", "to": "node-a", "text": "from A"})
        ok2 = tmp_engine.inbox_append({"id": 42, "from": "peerC", "to": "node-a", "text": "from C"})
        assert ok1 is True and ok2 is True
        assert tmp_engine.inbox_count("node-a") == 2

    def test_dedup_window_rebuilt_on_restart(self, tmp_path):
        """v5.0.8 回归：去重窗口启动时从 inbox.jsonl 回填。

        v5.0.7 窗口纯内存，进程重启即清空——重启前已入箱的 msg_id 若被
        对端重投（投递响应丢失后 outbox 重试跨越本端重启）会再次入箱。"""
        me1 = MailEngine(str(tmp_path), "node-a")
        assert me1.inbox_append({"msg_id": "r1", "from": "b", "to": "node-a", "text": "first"}) is True
        # 同一 mailbox 目录新建引擎 = 模拟进程重启
        me2 = MailEngine(str(tmp_path), "node-a")
        assert me2.inbox_append({"msg_id": "r1", "from": "b", "to": "node-a", "text": "dup"}) is False
        assert me2.inbox_count("node-a") == 1
