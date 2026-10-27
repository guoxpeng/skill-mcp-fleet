#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`_tailscale_ip` 只能有一份实现。

历史上这个模块里有两个同名函数，后者静默覆盖前者：

- 第一份：`tailscale ip -4`，60 秒缓存，失败返回 `""`；
- 第二份：解析 `tailscale status --json`，**无缓存**，失败返回 `None`。

运行时生效的是第二份，于是 `self_url()` 在 mesh / info / invite 这些热路径上
每次都 fork 一个重量级的 `status --json`（超时 10s，手机/NAS 上几百毫秒）。
第一份的缓存从来没被执行过，是纯粹的死代码——而且调用方一半按 `""` 写、
一半按 `None` 写，`health` 里还会漏出 `null`。

这类「重复定义 + 后者覆盖」的 bug 不会报错、不会崩，只会悄悄变慢和变脏，
所以用一条 AST 断言把它钉死。
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


class _FakeProc(object):
    def __init__(self, out=b"", rc=0):
        self._out = out
        self.returncode = rc

    def communicate(self, timeout=None):
        return self._out, b""


class SingleDefinitionTest(unittest.TestCase):
    def test_only_one_definition(self):
        """AST 层面断言：整个模块里 `_tailscale_ip` 只能被 def 一次。"""
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "mcp_agent.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        defs = [n.lineno for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_tailscale_ip"]
        self.assertEqual(len(defs), 1,
                         "重复定义会静默覆盖，运行时只认最后一个（行号：%s）" % defs)


class TailscaleIpTest(unittest.TestCase):
    def setUp(self):
        self._orig_popen = agent.subprocess.Popen
        self._orig_status = agent._tailscale_status
        self._orig_cache = dict(agent._TS_IP_CACHE)
        agent._TS_IP_CACHE["at"] = 0.0
        agent._TS_IP_CACHE["ip"] = ""
        self.calls = []

    def tearDown(self):
        agent.subprocess.Popen = self._orig_popen
        agent._tailscale_status = self._orig_status
        agent._TS_IP_CACHE.clear()
        agent._TS_IP_CACHE.update(self._orig_cache)

    def _popen(self, out, rc=0, record=None):
        calls = self.calls

        def fake(cmd, *a, **kw):
            calls.append(list(cmd))
            if record is not None:
                record.append(list(cmd))
            return _FakeProc(out, rc)
        agent.subprocess.Popen = fake

    def test_uses_cheap_command(self):
        """优先走 `tailscale ip -4`，不要动不动就拉 status --json。"""
        self._popen(b"100.113.145.90\n")
        self.assertEqual(agent._tailscale_ip(), "100.113.145.90")
        self.assertEqual(self.calls, [["tailscale", "ip", "-4"]])

    def test_returns_empty_string_not_none(self):
        """失败一律返回 `""`：调用方不必再区分 None 与空串。"""
        self._popen(b"", rc=1)
        agent._tailscale_status = lambda: None
        got = agent._tailscale_ip()
        self.assertEqual(got, "")
        self.assertIsInstance(got, str)

    def test_cached_within_ttl(self):
        """60 秒内重复调用只能 fork 一次——热路径反复调，不能每次都起进程。"""
        self._popen(b"100.1.2.3\n")
        for _ in range(5):
            self.assertEqual(agent._tailscale_ip(), "100.1.2.3")
        self.assertEqual(len(self.calls), 1, "缓存失效了，5 次调用 fork 了 %d 次" % len(self.calls))

    def test_ignores_non_tailnet_output(self):
        """`ip -4` 吐出非 100. 开头的地址（不是 tailnet）时不采信。"""
        self._popen(b"192.168.1.5\n")
        agent._tailscale_status = lambda: None
        self.assertEqual(agent._tailscale_ip(), "")

    def test_falls_back_to_status_json(self):
        """老版本 tailscale 没有 `ip -4`：退回解析 status --json 的 Self。"""
        self._popen(b"", rc=1)
        agent._tailscale_status = lambda: {
            "Self": {"UserID": 7, "TailscaleIPs": ["100.99.1.1", "fd7a::1"]},
            "Peer": {},
        }
        self.assertEqual(agent._tailscale_ip(), "100.99.1.1")

    def test_fallback_prefers_peer_match(self):
        """Self 里没有地址时，用 Peer 中同 UserID 的那台。"""
        self._popen(b"", rc=1)
        agent._tailscale_status = lambda: {
            "Self": {"UserID": 7, "TailscaleIPs": []},
            "Peer": {"k1": {"UserID": 7, "TailscaleIPs": ["100.50.0.2"]}},
        }
        self.assertEqual(agent._tailscale_ip(), "100.50.0.2")

    def test_self_url_uses_tailnet(self):
        """端到端：self_url 应当拿到 tailnet 地址而不是退回内网 IP。"""
        self._popen(b"100.113.145.90\n")
        orig_cfg = dict(agent.CFG)
        try:
            agent.CFG["self_url"] = ""
            u = agent.self_url()
            self.assertIn("100.113.145.90", u)
            self.assertTrue(u.endswith("/mcp"))
        finally:
            agent.CFG.clear()
            agent.CFG.update(orig_cfg)


if __name__ == "__main__":
    unittest.main()
