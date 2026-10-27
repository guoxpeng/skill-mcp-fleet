#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计日志不能无限涨。

「设备要长时间在线」不只是连接别断，还包括**别把自己写死**。`_audit_log` 原来
只 append，没有轮转也没有上限：软路由的 overlay 常常只有几百 MB，写满之后
设备直接起不来，而且现场看不出是日志撑爆的——只会表现为「改不动配置了」。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


class AuditRotateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-audit-")
        self.path = os.path.join(self.dir, "audit.log")
        self._orig_path = agent.AUDIT_LOG_PATH
        self._orig_max = agent.AUDIT_MAX_BYTES
        agent.AUDIT_LOG_PATH = self.path
        agent.AUDIT_MAX_BYTES = 1024

    def tearDown(self):
        agent.AUDIT_LOG_PATH = self._orig_path
        agent.AUDIT_MAX_BYTES = self._orig_max
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, n):
        with open(self.path, "a") as f:
            f.write("x" * n)

    def test_small_file_not_rotated(self):
        self._write(100)
        agent._audit_rotate()
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_oversized_file_rotated(self):
        self._write(4096)
        agent._audit_rotate()
        self.assertFalse(os.path.exists(self.path), "轮转后当前日志应为空（不存在）")
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertEqual(os.path.getsize(self.path + ".1"), 4096,
                         "旧内容必须完整保留在 .1 里")

    def test_rotate_keeps_only_one_generation(self):
        """只留一份 .1：留多份等于没上限。"""
        self._write(4096)
        agent._audit_rotate()
        self._write(4096)
        agent._audit_rotate()
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertFalse(os.path.exists(self.path + ".2"))
        self.assertEqual(os.path.getsize(self.path + ".1"), 4096)

    def test_missing_file_is_noop(self):
        agent._audit_rotate()          # 文件不存在不该抛
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_audit_log_calls_rotate(self):
        """_audit_log 本身要触发轮转，而不是只提供函数没人调。"""
        self._write(4096)
        agent._AUDIT_WRITES[0] = 0
        agent._audit_log("exec", {"command": "ls"})
        self.assertTrue(os.path.exists(self.path + ".1"),
                        "写日志时应当先检查并轮转")


class AuditLogStillWorksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-audit2-")
        self.path = os.path.join(self.dir, "audit.log")
        self._orig_path = agent.AUDIT_LOG_PATH
        agent.AUDIT_LOG_PATH = self.path
        self._orig_cfg = dict(agent.CFG)

    def tearDown(self):
        agent.AUDIT_LOG_PATH = self._orig_path
        agent.CFG.clear()
        agent.CFG.update(self._orig_cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_line(self):
        agent.CFG["audit_log"] = True
        agent._audit_log("exec", {"command": "echo hi"}, "1.2.3.4")
        text = open(self.path, encoding="utf-8").read()
        self.assertIn("tool=exec", text)
        self.assertIn("ip=1.2.3.4", text)

    def test_long_arg_truncated(self):
        agent.CFG["audit_log"] = True
        agent._audit_log("upload", {"content": "A" * 5000})
        line = open(self.path, encoding="utf-8").read()
        self.assertLess(len(line), 1000, "单行必须截断，否则一条就能撑爆日志")

    def test_disabled_by_config(self):
        agent.CFG["audit_log"] = False
        agent._audit_log("exec", {"command": "echo hi"})
        self.assertFalse(os.path.exists(self.path))


class SingleAuditImplementationTest(unittest.TestCase):
    """两套审计实现合并成一套。

    历史包袱：`audit_log`（peer_call / delegate）和 `_audit_log`（危险工具调用）
    各写各的锁、各用各的阈值，写的是同一个 audit.log。两个锁互不可见，
    一边 `os.replace` 把文件挪成 `.1`、另一边还握着旧 fd 往里写，轮转之后的
    行就落进了 `.1`；调阈值的人也会困惑「改成 5MB 了怎么还涨到 10MB 才转」。
    """

    def test_same_path(self):
        self.assertEqual(agent.AUDIT_PATH, agent.AUDIT_LOG_PATH,
                         "两个入口必须写同一个文件")

    def test_same_lock(self):
        self.assertIs(agent.AUDIT_LOCK, agent._audit_lock,
                      "两个入口必须共用一把锁，否则轮转与写入会互相踩")

    def test_same_threshold(self):
        self.assertEqual(agent.AUDIT_MAX_BYTES, agent.AUDIT_ROTATE_BYTES,
                         "阈值只能有一个，否则调了不生效")

    def test_both_entries_write_same_file(self):
        """并发混写：两个入口的行必须都落在同一个文件里，一条不少。"""
        import threading
        d = tempfile.mkdtemp(prefix="fleet-audit3-")
        path = os.path.join(d, "audit.log")
        orig = (agent.AUDIT_PATH, agent.AUDIT_LOG_PATH,
                agent.AUDIT_MAX_BYTES, agent.AUDIT_ROTATE_BYTES)
        orig_cfg = dict(agent.CFG)
        try:
            agent.AUDIT_PATH = agent.AUDIT_LOG_PATH = path
            agent.AUDIT_MAX_BYTES = agent.AUDIT_ROTATE_BYTES = 10 * 1024 * 1024
            agent.CFG["audit_log"] = True
            n = 60

            def peer():
                agent.audit_log("peer_call", "a", "b", "exec", "ok")

            def tool():
                agent._audit_log("exec", {"command": "ls"})

            ts = [threading.Thread(target=peer if i % 2 else tool)
                  for i in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), n, "并发混写不能丢行（丢行=两套锁在打架）")
            self.assertIn("peer_call", "".join(lines))
            self.assertIn("tool=exec", "".join(lines))
        finally:
            (agent.AUDIT_PATH, agent.AUDIT_LOG_PATH,
             agent.AUDIT_MAX_BYTES, agent.AUDIT_ROTATE_BYTES) = orig
            agent.CFG.clear()
            agent.CFG.update(orig_cfg)
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
