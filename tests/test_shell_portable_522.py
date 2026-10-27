#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""别把 shell 写死成 bash。

OpenWrt / iStoreOS / Alpine 默认只有 busybox 的 `ash`，没有 bash。原来
`run_local` 与 `_wrap` 都硬编码 `bash`，这些设备上**每一个 exec 都会失败**，
报「找不到 shell（Windows 需 cmd，POSIX 需 bash）」——而软路由恰恰是
「把家里设备接进来」最想接入的那一类节点。
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


class ShellBinTest(unittest.TestCase):
    def setUp(self):
        self._orig = list(agent._SHELL_BIN)
        self._orig_cfg = dict(agent.CFG)
        agent._SHELL_BIN.clear()

    def tearDown(self):
        agent._SHELL_BIN.clear()
        agent._SHELL_BIN.extend(self._orig)
        agent.CFG.clear()
        agent.CFG.update(self._orig_cfg)

    def test_returns_existing_executable(self):
        sh = agent._shell_bin()
        self.assertTrue(os.path.isabs(sh), sh)
        self.assertTrue(os.path.isfile(sh), "%s 不存在" % sh)
        self.assertTrue(os.access(sh, os.X_OK), "%s 不可执行" % sh)

    def test_cached(self):
        a = agent._shell_bin()
        b = agent._shell_bin()
        self.assertEqual(a, b)
        self.assertEqual(len(agent._SHELL_BIN), 1, "探测结果应当缓存")

    def test_falls_back_to_ash_without_bash(self):
        """模拟 OpenWrt：/bin/bash 不存在，只有 /bin/ash。"""
        real_isfile = os.path.isfile
        real_access = os.access
        real_which = shutil.which

        def fake_isfile(p):
            if p.endswith("bash"):
                return False
            if p == "/bin/ash":
                return True
            return real_isfile(p)

        os.path.isfile = fake_isfile
        os.access = lambda p, m: True if p == "/bin/ash" else real_access(p, m)
        shutil.which = lambda n: None
        agent._SHELL_BIN.clear()
        try:
            sh = agent._shell_bin()
        finally:
            os.path.isfile = real_isfile
            os.access = real_access
            shutil.which = real_which
        # OpenWrt 上 /bin/sh 本身就是指向 busybox ash 的软链，选它同样正确
        self.assertIn(sh, ("/bin/sh", "/usr/bin/sh", "/bin/ash", "/usr/bin/ash"),
                      "没有 bash 时必须退到 sh/ash 系，实际 %s" % sh)

    def test_never_empty(self):
        real_isfile = os.path.isfile
        real_which = shutil.which
        os.path.isfile = lambda p: False
        shutil.which = lambda n: None
        agent._SHELL_BIN.clear()
        try:
            sh = agent._shell_bin()
        finally:
            os.path.isfile = real_isfile
            shutil.which = real_which
        self.assertTrue(sh, "探测失败也要给出兜底值，不能返回空串")


class WrapUsesDetectedShellTest(unittest.TestCase):
    def setUp(self):
        self._orig = list(agent._SHELL_BIN)
        self._orig_cfg = dict(agent.CFG)
        agent._SHELL_BIN.clear()
        agent._SHELL_BIN.append("/bin/ash")     # 假装在 OpenWrt 上

    def tearDown(self):
        agent._SHELL_BIN.clear()
        agent._SHELL_BIN.extend(self._orig)
        agent.CFG.clear()
        agent.CFG.update(self._orig_cfg)

    def test_wrap_uses_detected(self):
        cmd = agent._wrap("echo hi")
        self.assertTrue(cmd.startswith("/bin/ash "), cmd)
        self.assertNotIn("bash", cmd)

    def test_wrap_quotes_command(self):
        cmd = agent._wrap("echo 'a b' && rm -rf /tmp/x")
        self.assertIn("echo", cmd)
        self.assertTrue("'" in cmd or '"' in cmd, "命令必须被引起来，否则会被拆词")

    def test_wrap_sudo_uses_detected(self):
        agent.CFG["sudo_password"] = "pw"
        cmd = agent._wrap("id", sudo=True)
        self.assertIn("/bin/ash", cmd)
        self.assertIn("sudo -S", cmd)
        self.assertNotIn("bash", cmd)

    def test_no_hardcoded_bash_in_source(self):
        """源码里不该再有硬编码 bash 的调用点。"""
        path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "mcp_agent.py")
        src = open(path, encoding="utf-8").read()
        self.assertNotIn('["bash", "-c"', src)
        self.assertNotIn('"bash " + flag', src)


class RunLocalSmokeTest(unittest.TestCase):
    def test_exec_works(self):
        code, out, err = agent.run_local("echo hello-from-run-local")
        self.assertEqual(code, 0, err)
        self.assertIn("hello-from-run-local", out)


if __name__ == "__main__":
    unittest.main()
