# -*- coding: utf-8 -*-
"""MessageDelivery 路由测试：三分支投递决策（mock HTTP，不发真请求）。"""
from fleet_proto import MailEngine, MessageDelivery


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
