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


if __name__ == "__main__":
    unittest.main()
