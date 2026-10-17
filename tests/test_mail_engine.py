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
