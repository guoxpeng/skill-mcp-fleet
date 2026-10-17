# -*- coding: utf-8 -*-
"""DiscoveryLayer 验签策略测试（不起真实 UDP socket，只测决策逻辑）。"""
import json
import time

from fleet_proto import (DiscoveryLayer, hello_signature,
                         PROTO_NAME, PROTO_VERSION)


def make_layer(peers):
    return DiscoveryLayer(
        "self-node", 3100,
        sign_secret="my-token",
        verify_token_func=lambda n: peers.get(n),
    )


class TestMakeHello:
    def test_hello_has_signature(self):
        d = make_layer({})
        pkt = json.loads(d._make_hello().decode("utf-8"))
        assert pkt["proto"] == "fleet" and pkt["name"] == "self-node"
        assert "sig" in pkt and len(pkt["sig"]) == 32

    def test_unsigned_when_no_secret(self):
        d = DiscoveryLayer("n", 3100)  # 未配 sign_secret
        pkt = json.loads(d._make_hello().decode("utf-8"))
        assert "sig" not in pkt

    def test_wire_roundtrip_verified(self):
        """自己签的 hello，持有我 token 的对方能验过（json 往返）。"""
        d = make_layer({})
        wire = d._make_hello().decode("utf-8")
        recv = json.loads(wire)
        assert hello_signature("my-token", "self-node", 3100, recv["ts"]) == recv["sig"]


class TestVerifyPolicy:
    def test_known_peer_forged_rejected(self):
        """已配对节点 + 签名不符 -> 拒收（核心防伪能力）。"""
        d = make_layer({"peer1": "real-token"})
        pkt = {"name": "peer1", "port": 3100, "ts": 1.5, "sig": "deadbeef"}
        assert d._verify_hello(pkt) is False

    def test_known_peer_valid_accepted(self):
        d = make_layer({"peer1": "real-token"})
        pkt = {"name": "peer1", "port": 3100, "ts": 1.5,
               "sig": hello_signature("real-token", "peer1", 3100, 1.5)}
        assert d._verify_hello(pkt) is True

    def test_legacy_unsigned_accepted(self):
        """v4.x 旧节点无 sig -> 兼容放行（不破坏混合版本网络）。"""
        d = make_layer({"peer1": "real-token"})
        assert d._verify_hello({"name": "peer1", "port": 3100, "ts": 1.5}) is True

    def test_unknown_node_accepted(self):
        """未知节点首次发现 -> 走原有流程（不做拦截）。"""
        d = make_layer({"peer1": "real-token"})
        pkt = {"name": "stranger", "port": 3100, "ts": 1.5, "sig": "whatever"}
        assert d._verify_hello(pkt) is True

    def test_no_verifier_accepted(self):
        """未配置 verify_token_func（无验签能力）-> 不拦截。"""
        d = DiscoveryLayer("n", 3100, sign_secret="s")
        pkt = {"name": "x", "port": 1, "ts": 1.0, "sig": "zzz"}
        assert d._verify_hello(pkt) is True

    def test_tampered_port_rejected(self):
        """真实签名但字段被改 -> 拒收。"""
        d = make_layer({"peer1": "tok"})
        sig = hello_signature("tok", "peer1", 3100, 2.5)
        pkt = {"name": "peer1", "port": 8080, "ts": 2.5, "sig": sig}
        assert d._verify_hello(pkt) is False


class TestUDPSocketDelivery:
    """真实 UDP socket 投递链路（127.0.0.1 单播，覆盖监听/解析/验签全路径）。"""

    def _free_port(self):
        import socket as _s
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def test_real_socket_accept_forged_reject(self):
        """单播投递：合法签名包触发发现回调，伪造签名包被拒收。"""
        import socket as _s
        port = self._free_port()
        found = []
        d = DiscoveryLayer(
            "listener", 4101, discover_port=port,
            on_discover=lambda n, ip, p, c: found.append((n, ip, p)),
            sign_secret="tok-l",
            verify_token_func=lambda n: {"known-peer": "tok-real"}.get(n),
        )
        d.start()
        time.sleep(0.3)   # 等 listen 线程完成 bind（全量测试负载下尤为必要）
        try:
            sender = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            # 合法签名 -> 接受并触发发现回调
            pkt = {"proto": PROTO_NAME, "ver": PROTO_VERSION, "name": "known-peer",
                   "port": 5001, "caps": {}, "ts": time.time()}
            pkt["sig"] = hello_signature("tok-real", pkt["name"], pkt["port"], pkt["ts"])
            sender.sendto(json.dumps(pkt).encode(), ("127.0.0.1", port))
            time.sleep(0.4)
            assert ("known-peer", "127.0.0.1", 5001) in found
            # 伪造签名（已知节点）-> 拒收
            before = d._rejected
            bad = dict(pkt, port=5002)
            bad["sig"] = hello_signature("WRONG", bad["name"], bad["port"], bad["ts"])
            sender.sendto(json.dumps(bad).encode(), ("127.0.0.1", port))
            time.sleep(0.4)
            assert d._rejected == before + 1
            assert ("known-peer", "127.0.0.1", 5002) not in found
            sender.close()
        finally:
            d.stop()
