#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端口转发的 listener 也会变僵尸。

`_tun_sweep` 原来对 `kind == "listen"` 直接 continue，理由是「监听器没有数据流」。
可主端崩溃/断网时来不及 `tunnel_close`，listener 就永久留着：占一个 TUN_MAX
槽位，还一直占着那个端口。跑几次 `-R` 之后再监听同一端口就是
"Address already in use"，而现场完全看不出是上一轮残留的 socket 没放。

反过来也要防：不能因为「listener 没数据」就无脑回收——正常跑着的 `-R`
会持续 `tunnel_accept` 长轮询，必须靠它刷新活跃时间。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


def make_listener(tid="tl-test", idle=0.0, with_sock=True):
    """造一个真的 listener（绑 127.0.0.1:0），便于验证端口是否真的释放。"""
    ls = None
    port = 0
    if with_sock:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", 0))
        ls.listen(4)
        port = ls.getsockname()[1]
    now = time.time()
    sess = {"id": tid, "kind": "listen", "lsock": ls, "pending": [],
            "lock": threading.Lock(), "start": now - idle, "last": now - idle,
            "end": None, "bound": "127.0.0.1:%d" % port}
    with agent.TUNNELS_LOCK:
        agent.TUNNELS[tid] = sess
    return sess, port


def port_open(port, timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


class ListenerSweepTest(unittest.TestCase):
    def setUp(self):
        with agent.TUNNELS_LOCK:
            agent.TUNNELS.clear()

    def tearDown(self):
        with agent.TUNNELS_LOCK:
            leftovers = list(agent.TUNNELS)
        for tid in leftovers:
            agent._tun_kill(tid)
        with agent.TUNNELS_LOCK:
            agent.TUNNELS.clear()

    def test_idle_listener_reclaimed(self):
        sess, port = make_listener(idle=agent.TUN_IDLE + 10)
        self.assertTrue(port_open(port), "前置条件：listener 应当真在监听")
        agent._tun_sweep()
        self.assertNotIn("tl-test", agent.TUNNELS,
                         "空闲超过 TUN_IDLE 的 listener 必须回收")
        self.assertFalse(port_open(port), "回收后端口必须真的释放")

    def test_active_listener_kept(self):
        sess, port = make_listener(idle=5)
        agent._tun_sweep()
        self.assertIn("tl-test", agent.TUNNELS, "刚用过的 listener 不能被误杀")
        self.assertTrue(port_open(port))

    def test_accept_refreshes_last(self):
        """accept 是 listener 唯一的活跃信号，必须刷新 last。"""
        sess, port = make_listener(idle=agent.TUN_IDLE - 60)
        old = sess["last"]
        # 直接模拟一次 accept 调用（不真连，pending 为空即可）
        text, iserr = agent.t_tunnel_accept({"tunnel_id": "tl-test", "wait": 0})
        self.assertFalse(iserr, text)
        self.assertGreater(sess["last"], old, "accept 之后 last 必须更新")

    def test_accept_keeps_it_alive_across_sweeps(self):
        """模拟「主端一直在轮询」：连续 sweep 都不该回收它。"""
        sess, port = make_listener(idle=agent.TUN_IDLE - 60)
        for _ in range(3):
            agent.t_tunnel_accept({"tunnel_id": "tl-test", "wait": 0})
            agent._tun_sweep()
            self.assertIn("tl-test", agent.TUNNELS)

    def test_port_reusable_after_reclaim(self):
        """真正要证明的：回收之后能立刻在同一个端口重新监听。"""
        sess, port = make_listener(idle=agent.TUN_IDLE + 10)
        agent._tun_sweep()
        self.assertNotIn("tl-test", agent.TUNNELS)
        ls2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            ls2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls2.bind(("127.0.0.1", port))
            ls2.listen(1)
        finally:
            ls2.close()

    def test_ended_listener_expires_by_ttl(self):
        sess, port = make_listener(idle=60)
        sess["end"] = time.time() - agent.TUN_TTL - 10
        agent._tun_sweep()
        self.assertNotIn("tl-test", agent.TUNNELS)


class ConnLastRefreshTest(unittest.TestCase):
    """conn 的 last 要由读写刷新，否则活跃连接也会被当成孤儿。"""

    def test_source_refreshes_on_read_and_write(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "mcp_agent.py")
        src = open(path, encoding="utf-8").read()
        for fn in ("def t_tunnel_read", "def t_tunnel_write",
                   "def t_tunnel_accept"):
            start = src.index(fn)
            end = src.index("\ndef ", start + 10)
            body = src[start:end]
            self.assertIn('["last"] = time.time()', body,
                          "%s 必须刷新 last，否则会被空闲回收误杀" % fn)


class TunnelCountCapTest(unittest.TestCase):
    """槽位上限还是 64，别把回收改成「永不回收」的另一个极端。"""

    def test_cap_defined(self):
        self.assertGreater(agent.TUN_MAX, 0)
        self.assertLessEqual(agent.TUN_MAX, 256)

    def test_idle_less_than_ttl(self):
        self.assertLess(agent.TUN_IDLE, agent.TUN_TTL,
                        "空闲回收阈值必须小于 TTL，否则先撞 TTL 逻辑")


if __name__ == "__main__":
    unittest.main()
