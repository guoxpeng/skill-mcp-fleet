#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""僵尸 shell 会话的回收。

真机复现：客户端崩溃（我的探测脚本第一版 sid 解析错了，直接抛异常退出），
没来得及 `shell_close`。几分钟后 `shell_list` 里那几个会话仍然「运行中」，
占着 PTY + zsh 进程 + 读线程。

`SHELL_TTL` 只回收**已结束**的会话（`s["end"]` 非空），而断线的会话 `end`
永远是 None——于是永久占用。上限 8 个，断网 8 次之后 `shell_open` 就彻底
用不了了。对「设备要长时间在线」这个目标来说这是致命的。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


class FakeShell(object):
    """不真 fork 的假会话：fd=-1、pid=None，_kill_shell 会安全地跳过。"""

    def __init__(self, sid, idle=0.0, ended=None):
        now = time.time()
        self.sess = {
            "id": sid, "fd": -1, "pid": None, "buf": b"", "base": 0, "total": 0,
            "lock": threading.Lock(),
            "start": now - idle, "last_read": now - idle,
            "end": ended, "exit": None,
            "shell": "/bin/sh", "cols": 80, "rows": 24,
        }

    def __enter__(self):
        agent.SHELLS[self.sess["id"]] = self.sess
        return self.sess

    def __exit__(self, *a):
        agent.SHELLS.pop(self.sess["id"], None)


class ShellSweepTest(unittest.TestCase):
    def setUp(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def tearDown(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def test_active_session_kept(self):
        with FakeShell("sh-active", idle=5):
            agent._shell_sweep()
            self.assertIn("sh-active", agent.SHELLS)

    def test_idle_session_reclaimed(self):
        with FakeShell("sh-idle", idle=agent.SHELL_IDLE + 10):
            agent._shell_sweep()
            self.assertNotIn("sh-idle", agent.SHELLS,
                             "空闲超过 %ds 的会话必须回收，否则断线后永久占槽位"
                             % agent.SHELL_IDLE)

    def test_boundary_not_yet(self):
        """刚好没到阈值时不能杀——客户端只是暂时没来读。"""
        with FakeShell("sh-edge", idle=agent.SHELL_IDLE - 30):
            agent._shell_sweep()
            self.assertIn("sh-edge", agent.SHELLS)

    def test_ended_session_kept_within_ttl(self):
        with FakeShell("sh-done", idle=60, ended=time.time() - 60):
            agent._shell_sweep()
            self.assertIn("sh-done", agent.SHELLS, "已结束但在 TTL 内要留着查输出")

    def test_ended_session_expired(self):
        with FakeShell("sh-old", idle=agent.SHELL_TTL + 100,
                       ended=time.time() - agent.SHELL_TTL - 100):
            agent._shell_sweep()
            self.assertNotIn("sh-old", agent.SHELLS)

    def test_read_refreshes_idle(self):
        """只要还在长轮询，就不该被回收——这是「在用」和「没人管」的分界。"""
        with FakeShell("sh-live", idle=agent.SHELL_IDLE + 60) as sess:
            sess["last_read"] = time.time()
            agent._shell_sweep()
            self.assertIn("sh-live", agent.SHELLS)

    def test_sweep_kills_process(self):
        """回收要真的杀进程，不能只从字典里删掉（否则 zsh 成孤儿）。"""
        killed = []

        class FakeKill(object):
            def __init__(self):
                self.sess = dict(id="sh-kill", fd=-1, pid=999999,
                                 buf=b"", base=0, total=0,
                                 lock=threading.Lock(),
                                 start=time.time() - agent.SHELL_IDLE - 10,
                                 last_read=time.time() - agent.SHELL_IDLE - 10,
                                 end=None, exit=None, shell="/bin/sh",
                                 cols=80, rows=24)

            def __enter__(self):
                agent.SHELLS["sh-kill"] = self.sess
                return self.sess

            def __exit__(self, *a):
                agent.SHELLS.pop("sh-kill", None)

        orig = os.kill
        os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            with FakeKill():
                agent._shell_sweep()
        finally:
            os.kill = orig
        self.assertNotIn("sh-kill", agent.SHELLS)
        self.assertTrue(killed, "回收会话时必须发信号杀掉子进程")


class ShellListShowsIdleTest(unittest.TestCase):
    """`shell_list` 要能一眼看出「这个会话没人管了」。"""

    def setUp(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def tearDown(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def test_idle_shown(self):
        with FakeShell("sh-show", idle=42):
            text, iserr = agent.t_shell_list({})
        self.assertFalse(iserr)
        self.assertIn("空闲", text, text)
        self.assertIn("sh-show", text)


class ShellOpenEvictionTest(unittest.TestCase):
    """槽位满时，挤掉最久没人读的那个，而不是直接拒绝。"""

    def test_pick_stale(self):
        now = time.time()
        entries = [(now - 5, "sh-a"), (now - agent.SHELL_IDLE_MIN - 60, "sh-b")]
        self.assertEqual(agent._pick_evictable(entries, now), "sh-b")

    def test_pick_none_when_all_active(self):
        now = time.time()
        entries = [(now - 5, "sh-a"), (now - 10, "sh-b")]
        self.assertIsNone(agent._pick_evictable(entries, now),
                          "都在正常长轮询时不能误杀")

    def test_pick_none_when_empty(self):
        self.assertIsNone(agent._pick_evictable([], time.time()))

    def test_idle_min_is_well_below_idle(self):
        """挤占阈值必须明显小于空闲回收阈值，否则「满槽位」永远解不开。"""
        self.assertLess(agent.SHELL_IDLE_MIN, agent.SHELL_IDLE)
        self.assertGreaterEqual(agent.SHELL_IDLE_MIN, 60,
                                "太小会误杀刚连上还没开始读的客户端")


class SweepReclaimsFullSlotsTest(unittest.TestCase):
    """8 个断线会话占满槽位后，sweep 之后应该全部释放。"""

    def setUp(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def tearDown(self):
        with agent.SHELLS_LOCK:
            agent.SHELLS.clear()

    def test_all_reclaimed(self):
        for i in range(agent.SHELL_MAX):
            FakeShell("sh-z%d" % i, idle=agent.SHELL_IDLE + 60).__enter__()
        try:
            self.assertEqual(len(agent.SHELLS), agent.SHELL_MAX)
            agent._shell_sweep()
            self.assertEqual(len(agent.SHELLS), 0,
                             "全部超时未读的会话都该被回收")
        finally:
            with agent.SHELLS_LOCK:
                agent.SHELLS.clear()


if __name__ == "__main__":
    unittest.main()
