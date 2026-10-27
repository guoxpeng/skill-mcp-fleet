#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.1 自升级（fleet up）与 macOS 适配的单元测试。

零网络、零真实 git 远端：git 相关只测纯逻辑（版本比较、路径识别、备份/替换/自检）。
真实升级流程已在临时仓库里做过端到端验证（5.2.0 → 5.2.2、坏版本自动回滚）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet
import fleet_install


# ---------------------------------------------------------------- 版本比较
class VersionCompareTest(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(fleet.parse_version("5.2.1"), (5, 2, 1))
        self.assertEqual(fleet.parse_version("v5.2.1"), (5, 2, 1))
        self.assertEqual(fleet.parse_version(" 5.0.10 "), (5, 0, 10))

    def test_parse_invalid(self):
        for bad in ("5.2", "5", "", None, "abc", "5.2.1.0"):
            self.assertIsNone(fleet.parse_version(bad), bad)

    def test_numeric_not_lexicographic(self):
        """5.10.0 必须大于 5.9.0 —— 字符串比较会判反，升级功能直接失效。"""
        self.assertEqual(fleet.ver_cmp("5.10.0", "5.9.0"), 1)
        self.assertEqual(fleet.ver_cmp("5.9.0", "5.10.0"), -1)
        self.assertEqual(fleet.ver_cmp("5.2.1", "5.2.1"), 0)

    def test_cmp_invalid_returns_none(self):
        self.assertIsNone(fleet.ver_cmp("5.2", "5.2.1"))
        self.assertIsNone(fleet.ver_cmp("x", "5.2.1"))


# ---------------------------------------------------------------- 目录识别
class LayoutTest(unittest.TestCase):
    def test_repo_layout(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "scripts"))
            open(os.path.join(d, "scripts", "mcp_agent.py"), "w").close()
            self.assertEqual(fleet._code_dir(d), os.path.join(d, "scripts"))

    def test_installed_layout(self):
        """安装后的节点目录里 mcp_agent.py 在顶层，没有 scripts/。"""
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "mcp_agent.py"), "w").close()
            self.assertEqual(fleet._code_dir(d), d)

    def test_no_layout(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(fleet._code_dir(d), "")


# ---------------------------------------------------------------- 备份/替换
class BackupAndInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.cdir = os.path.join(self.root, "scripts")
        os.makedirs(self.cdir)
        for name in ("fleet.py", "mcp_agent.py", "mcp_agent_config.json"):
            with open(os.path.join(self.cdir, name), "w") as f:
                f.write("old:" + name)
        os.makedirs(os.path.join(self.cdir, "__pycache__"))
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.root          # 备份落到临时 HOME

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_backup_lands_outside_repo(self):
        """备份必须放在仓库外：放在 scripts/ 旁边会立刻变成未提交改动，
        下一次 fleet up 就会被「工作区有改动」永久挡住。"""
        dst = fleet._backup_code_dir(self.cdir, "20260101-000000")
        self.assertTrue(dst)
        self.assertTrue(os.path.isdir(dst))
        self.assertFalse(dst.startswith(self.cdir))
        self.assertTrue(os.path.isfile(os.path.join(dst, "fleet.py")))

    def test_backup_skips_pycache(self):
        dst = fleet._backup_code_dir(self.cdir, "20260101-000001")
        self.assertFalse(os.path.isdir(os.path.join(dst, "__pycache__")))

    def test_install_replaces_code_only(self):
        src = os.path.join(self.root, "src")
        os.makedirs(src)
        for name in ("fleet.py", "mcp_agent.py", "mcp_agent_config.json"):
            with open(os.path.join(src, name), "w") as f:
                f.write("new:" + name)
        done, skipped, err = fleet._install_from_dir(src, self.cdir)
        self.assertEqual(err, "")
        self.assertGreaterEqual(done, 2)
        # 代码换了
        self.assertEqual(open(os.path.join(self.cdir, "fleet.py")).read(), "new:fleet.py")
        # 运行时配置绝不能被覆盖
        self.assertEqual(
            open(os.path.join(self.cdir, "mcp_agent_config.json")).read(),
            "old:mcp_agent_config.json")

    def test_install_skips_missing_source(self):
        src = os.path.join(self.root, "src2")
        os.makedirs(src)
        with open(os.path.join(src, "fleet.py"), "w") as f:
            f.write("new")
        done, skipped, err = fleet._install_from_dir(src, self.cdir)
        self.assertEqual(err, "")
        self.assertEqual(done, 1)
        self.assertGreater(skipped, 0)

    def test_no_tmp_left_behind(self):
        src = os.path.join(self.root, "src3")
        os.makedirs(src)
        with open(os.path.join(src, "fleet.py"), "w") as f:
            f.write("new")
        fleet._install_from_dir(src, self.cdir)
        leftovers = [f for f in os.listdir(self.cdir) if f.endswith(".new")]
        self.assertEqual(leftovers, [])


class VerifyCodeTest(unittest.TestCase):
    def test_ok(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fleet.py"), "w") as f:
                f.write('print("mcp-fleet 5.2.1")\n')
            okv, out = fleet._verify_code(d, "5.2.1")
            self.assertTrue(okv, out)

    def test_version_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fleet.py"), "w") as f:
                f.write('print("mcp-fleet 5.2.0")\n')
            okv, out = fleet._verify_code(d, "5.2.1")
            self.assertFalse(okv)

    def test_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fleet.py"), "w") as f:
                f.write("def broken(((\n")
            okv, _out = fleet._verify_code(d, "5.2.1")
            self.assertFalse(okv)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            okv, _out = fleet._verify_code(d, "5.2.1")
            self.assertFalse(okv)


class UpStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_roundtrip(self):
        fleet._save_up_state({"bad_version": "5.2.3", "reason": "boom"})
        st = fleet._load_up_state()
        self.assertEqual(st["bad_version"], "5.2.3")

    def test_missing_is_empty(self):
        self.assertEqual(fleet._load_up_state(), {})

    def test_corrupt_is_empty(self):
        path = fleet._up_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(fleet._load_up_state(), {})


# ---------------------------------------------------------------- launchd
class LaunchdHelperTest(unittest.TestCase):
    def test_label_parse(self):
        with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as f:
            f.write("<key>Label</key>\n<string>com.mcpfleet.node.nas</string>\n")
            path = f.name
        try:
            self.assertEqual(fleet._plist_label(path), "com.mcpfleet.node.nas")
        finally:
            os.unlink(path)

    def test_label_missing(self):
        with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as f:
            f.write("<plist></plist>")
            path = f.name
        try:
            self.assertEqual(fleet._plist_label(path), "")
        finally:
            os.unlink(path)

    def test_domain(self):
        self.assertEqual(fleet._launchd_domain("/Library/LaunchDaemons/x.plist"), "system")
        self.assertTrue(fleet._launchd_domain("/Users/a/Library/LaunchAgents/x.plist")
                        .startswith("gui/"))

    def test_find_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("com.mcpfleet.node.nas.plist", "com.mcpfleet.node.pi.plist",
                      "com.other.thing.plist"):
                open(os.path.join(d, n), "w").close()
            old = fleet._launchd_dirs
            fleet._launchd_dirs = lambda: [d]
            old_plat = sys.platform
            sys.platform = "darwin"
            try:
                self.assertTrue(fleet._launchd_plist("pi").endswith("node.pi.plist"))
                self.assertTrue(fleet._launchd_plist("nas").endswith("node.nas.plist"))
                # 名字对不上时退回第一个，而不是空手而归
                self.assertIn("mcpfleet", fleet._launchd_plist("unknown"))
            finally:
                fleet._launchd_dirs = old
                sys.platform = old_plat

    def test_non_darwin_returns_empty(self):
        old_plat = sys.platform
        sys.platform = "linux"
        try:
            self.assertEqual(fleet._launchd_plist("x"), "")
        finally:
            sys.platform = old_plat


# ---------------------------------------------------------------- macOS 安装器
class MacInstallTest(unittest.TestCase):
    def test_launchd_path_has_homebrew(self):
        p = fleet_install._launchd_path()
        self.assertIn("/opt/homebrew/bin", p)
        self.assertIn("/usr/local/bin", p)
        self.assertIn("/usr/bin", p)

    def test_launchd_path_no_sandbox_leak(self):
        """不能把安装者当时的临时 PATH 写进别人的服务配置。"""
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/tmp/definitely-not-a-real-bin:/usr/bin"
        try:
            p = fleet_install._launchd_path()
            self.assertNotIn("/tmp/definitely-not-a-real-bin", p)
        finally:
            if old is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old

    def test_plist_template_has_env(self):
        text = fleet_install.load_template("com.mcpfleet.node.plist")
        self.assertIn("EnvironmentVariables", text)
        self.assertIn("{{LAUNCHD_PATH}}", text)
        self.assertIn("{{LAUNCHD_HOME}}", text)

    def test_builtin_matches_file(self):
        """内置兜底必须和模板文件逐字一致，否则从包里装和从仓库装行为不同。"""
        path = os.path.join(fleet_install.TEMPLATE_DIR, "com.mcpfleet.node.plist")
        if not os.path.isfile(path):
            self.skipTest("模板文件不在（精简包）")
        with open(path, encoding="utf-8") as f:
            disk = f.read()
        self.assertEqual(fleet_install._BUILTIN_TEMPLATES["com.mcpfleet.node.plist"], disk)

    def test_plist_renders(self):
        text = fleet_install.load_template("com.mcpfleet.node.plist")
        out = fleet_install.render_template(text, {
            "LAUNCHD_LABEL": "com.mcpfleet.node.t",
            "DISPLAY_NAME": "t",
            "PYTHON": "/usr/bin/python3",
            "AGENT_PATH": "/opt/t/mcp_agent.py",
            "HOST": "127.0.0.1",
            "PORT": "3100",
            "EXTRA_PLIST_ARGS": "",
            "INSTALL_DIR": "/opt/t",
            "LOG_DIR": "/opt/t/logs",
            "USERNAME_BLOCK": "",
            "LAUNCHD_PATH": "/opt/homebrew/bin:/usr/bin",
            "LAUNCHD_HOME": "/Users/t",
        })
        self.assertIn("/opt/homebrew/bin", out)
        self.assertNotIn("{{", out)


if __name__ == "__main__":
    unittest.main()
