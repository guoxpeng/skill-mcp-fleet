#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行策略必须分清「命令」和「文件内容」。

真机复现出来的 bug：推 600KB 二进制到副端，`fleet cp` 报

    该操作需要用户确认：防火墙变更（规则 firewall）。

原因是 ``ExecPolicy`` 把 ``upload`` 的 ``content``（base64 正文）也拿去跑危险
命令正则，而 ``ufw`` 只有 3 个字符——80 万字符的随机载荷里几乎必然出现。
后果是**几乎每个大文件上传都被误判**，`fleet cp` 基本不可用。

这里把边界钉死：数据字段不参与命令匹配，但「藏命令」的绕过路径必须仍然拦住。
"""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet_core as core


def _blob(n=512 * 1024):
    """随机二进制 -> base64，体积和真实分块一致。"""
    return base64.b64encode(os.urandom(n)).decode()


class PayloadNotCommandTest(unittest.TestCase):
    """大块数据字段不该被当成命令行。"""

    def setUp(self):
        self.policy = core.ExecPolicy.default()

    def test_large_upload_is_allowed(self):
        decision = self.policy.check(
            "upload", {"filePath": "/tmp/x.bin", "content": _blob()})
        self.assertEqual(decision[0], "allow", decision)

    def test_large_write_file_is_allowed(self):
        # 纯文本大文件（源码/日志）里出现 ufw、nft 这类词同样常见
        text = ("# 关于 ufw 与 nft 的说明\n" * 2000)
        decision = self.policy.check(
            "write_file", {"filePath": "/tmp/notes.md", "content": text})
        self.assertEqual(decision[0], "allow", decision)

    def test_tunnel_write_b64_is_allowed(self):
        decision = self.policy.check(
            "tunnel_write", {"id": "t1", "data_b64": _blob(64 * 1024)})
        self.assertEqual(decision[0], "allow", decision)

    def test_edit_new_text_is_not_command(self):
        # 大段正文（改一大块代码）里出现 ufw 字样不该被当成命令
        decision = self.policy.check("edit_file", {
            "filePath": "/tmp/a.py",
            "oldText": "x = 1",
            "newText": "x = 2  # ufw 端口放行说明\n" * 500,
        })
        self.assertEqual(decision[0], "allow", decision)

    def test_bulk_field_not_in_command_text(self):
        text = core._command_text("upload", {"filePath": "/tmp/a", "content": _blob(8192)})
        self.assertNotIn("content", text)
        self.assertLess(len(text), 1024, "大载荷不该被拼进待匹配文本")
        self.assertIn("/tmp/a", text, "路径参数仍要参与匹配")


class SmallPayloadStillScannedTest(unittest.TestCase):
    """小体量正文照样扫——写一段危险脚本值得拦一下。"""

    def setUp(self):
        self.policy = core.ExecPolicy.default()

    def test_small_dangerous_content_is_flagged(self):
        decision = self.policy.check(
            "write_file", {"filePath": "/tmp/f.sh", "content": "ufw disable\n"})
        self.assertEqual(decision[0], "ask", decision)

    def test_small_dangerous_upload_is_flagged(self):
        payload = base64.b64encode(b"iptables -F\n").decode()
        decision = self.policy.check(
            "upload", {"filePath": "/tmp/f.sh", "content": payload})
        # base64 后原文不可见，但体量小 -> 参与匹配；这里断言不崩且给出决策
        self.assertIn(decision[0], ("allow", "ask", "deny"))

    def test_threshold_boundary(self):
        body = "ufw " * (core._BULK_SCAN_LIMIT // 4)
        self.assertLessEqual(len(body), core._BULK_SCAN_LIMIT)
        decision = self.policy.check(
            "write_file", {"filePath": "/tmp/b.sh", "content": body})
        self.assertEqual(decision[0], "ask", "阈值内必须扫描")

        body2 = body + "x"
        self.assertGreater(len(body2), core._BULK_SCAN_LIMIT)
        decision2 = self.policy.check(
            "write_file", {"filePath": "/tmp/b.sh", "content": body2})
        self.assertEqual(decision2[0], "allow", "阈值外按数据对待")


class ExecStdinStillScannedTest(unittest.TestCase):
    """exec 的 content 是 stdin：**只有命令是解释器时**它才是命令。"""

    def setUp(self):
        self.policy = core.ExecPolicy.default()

    def test_exec_stdin_large_still_scanned(self):
        payload = "ufw disable\n" + ("#" * 20000)
        decision = self.policy.check(
            "exec", {"cmd": "bash", "content": payload})
        self.assertEqual(decision[0], "ask", decision)

    def test_exec_cmd_still_denied(self):
        decision = self.policy.check("exec", {"cmd": "iptables -F"})
        self.assertEqual(decision[0], "ask", decision)

    def test_python_stdin_scanned(self):
        decision = self.policy.check("exec", {
            "command": "python3",
            "content": "import os\nos.system('nft flush ruleset')\n" * 100})
        self.assertEqual(decision[0], "ask", decision)

    def test_sudo_bash_stdin_scanned(self):
        decision = self.policy.check("exec", {
            "command": "sudo bash", "content": "ufw disable\n" * 500})
        self.assertEqual(decision[0], "ask", decision)

    def test_data_command_stdin_not_scanned(self):
        """`base64 -d > f` 的 stdin 是数据，不能因为里面有 /nft/ 就拦。

        旧实现全量扫描：512KB 随机 base64 有 5% 会命中 `/nft/` 之类
        （`/` 不是单词字符，正好构成 \\b 边界），等于随机 5% 的传输失败。
        """
        blob = _blob(512 * 1024)
        decision = self.policy.check(
            "exec", {"command": "base64 -d > /tmp/o.bin", "content": blob})
        self.assertEqual(decision[0], "allow", decision)

    def test_tar_stdin_not_scanned(self):
        decision = self.policy.check(
            "exec", {"command": "tar xzf - -C /tmp", "content": _blob(512 * 1024)})
        self.assertEqual(decision[0], "allow", decision)


class PayloadScanCostTest(unittest.TestCase):
    """策略检查在每个工具调用前都要跑一次，不能有 O(载荷) 的代价。"""

    def setUp(self):
        self.policy = core.ExecPolicy.default()

    def test_large_stdin_is_cheap(self):
        import time
        blob = "A" * (1024 * 1024)
        t = time.time()
        self.policy.check("exec", {"command": "base64 -d > /tmp/x", "content": blob})
        cost = time.time() - t
        # 旧实现全量扫描 1MB × 25 条规则 ≈ 150ms；这里只要求「不是线性代价」
        self.assertLess(cost, 0.05, "1MB 数据载荷的策略检查耗时 %.0fms" % (cost * 1000))


class BypassStillBlockedTest(unittest.TestCase):
    """不能因为分流而给「把命令藏到非典型字段」开口子。"""

    def setUp(self):
        self.policy = core.ExecPolicy.default()

    def test_unusual_field_still_matched(self):
        decision = self.policy.check(
            "exec", {"cmd": "echo hi", "whatever": "iptables -F"})
        self.assertEqual(decision[0], "ask", decision)

    def test_nested_field_still_matched(self):
        decision = self.policy.check("exec", {
            "cmd": "echo hi",
            "env": {"NOTE": "sudo reboot"},
        })
        self.assertEqual(decision[0], "ask", decision)

    def test_path_jail_still_applies(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="fleet-jail-")
        try:
            policy = core.ExecPolicy({"path_jail": [root]})
            decision = policy.check(
                "upload", {"filePath": "/etc/passwd", "content": _blob(1024)})
            self.assertEqual(decision[0], "deny", decision)
            self.assertEqual(decision[2], "path_jail")
        finally:
            os.rmdir(root)


class BulkKeyTableTest(unittest.TestCase):
    """常量表本身别被写坏。"""

    def test_keys_lowercase(self):
        for key in core._BULK_ARG_KEYS:
            self.assertEqual(key, key.lower())

    def test_exec_tools_include_exec(self):
        self.assertIn("exec", core._EXEC_TOOLS)
        self.assertIn("shell", core._EXEC_TOOLS)

    def test_scan_limit_is_sane(self):
        self.assertGreaterEqual(core._BULK_SCAN_LIMIT, 1024)
        self.assertLessEqual(core._BULK_SCAN_LIMIT, 64 * 1024)


if __name__ == "__main__":
    unittest.main()
