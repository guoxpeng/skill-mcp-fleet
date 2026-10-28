#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.7 —— 真机（iStoreOS 24.10.7 / busybox）跑出来的两个 bug。

这两条都不是读代码能看出来的，是拿真设备敲命令敲出来的：

1. `fleet exec istoreos "uname -s"` 返回 9 行 ASCII 横幅 + `Linux`。
   根因是 `_wrap` 默认 `-lc`，而 OpenWrt 的 /etc/profile 会打 MOTD。
   后果不是"多几行"——是 `| jq`、`$(...)`、`grep` 全部拿到脏数据。

2. `net_check listen` 在没装 `ss` 的 busybox 上，退回的 `netstat -tlnp`
   **不认 -l**，把所有 socket 全倒出来：实测 6943 行 / 683KB，
   客户端只能截断，等于没给信息。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent
import fleet


def _src(name):
    with io.open(os.path.join(os.path.dirname(__file__), "..", "scripts", name),
                 encoding="utf-8") as f:
        return f.read()


class NoLoginShellByDefaultTest(unittest.TestCase):
    """默认必须是 `-c`：exec 的输出要能被程序消费。"""

    def setUp(self):
        self._orig = dict(mcp_agent.CFG)

    def tearDown(self):
        mcp_agent.CFG.clear()
        mcp_agent.CFG.update(self._orig)

    def test_default_is_not_login(self):
        mcp_agent.CFG.pop("login_shell", None)
        wrapped = mcp_agent._wrap("uname -s")
        self.assertIn(" -c ", wrapped)
        self.assertNotIn(" -lc ", wrapped)

    def test_opt_in_login_still_works(self):
        """有人确实需要 profile 里的 PATH —— 打开开关要能回到 -lc。"""
        mcp_agent.CFG["login_shell"] = True
        self.assertIn(" -lc ", mcp_agent._wrap("uname -s"))

    def test_explicit_false(self):
        mcp_agent.CFG["login_shell"] = False
        self.assertNotIn(" -lc ", mcp_agent._wrap("uname -s"))

    def test_interactive_shell_still_logs_in(self):
        """`shell_open` 用 `[shell, "-l"]`，横幅在那里是应该看到的。"""
        s = _src("mcp_agent.py")
        self.assertIn('os.execvpe(shell, [shell, "-l"], env)', s)

    def test_wrap_reason_is_documented(self):
        """注释里要留原因，否则下次有人"顺手优化"回 -lc。"""
        s = _src("mcp_agent.py")
        i = s.index("def _wrap")
        seg = s[i:i + 1400]
        self.assertIn("iStoreOS", seg)
        self.assertIn("横幅", seg)


class ListenFallbackTest(unittest.TestCase):
    """busybox 的 netstat 不认 -l，必须自己过滤 + 封顶。"""

    def _cmd(self):
        s = _src("mcp_agent.py")
        i = s.index('elif action == "listen":')
        seg = s[i:i + 1200]
        start = seg.index('cmd = (')
        return seg[start:seg.index("code, out, err", start)]

    def test_prefers_ss(self):
        self.assertIn("command -v ss", self._cmd())

    def test_falls_back_to_netstat(self):
        self.assertIn("netstat -tlnp", self._cmd())

    def test_filters_listen_itself(self):
        """不自己 grep 就会把 ESTABLISHED 一起倒出来（6943 行的来源）。"""
        cmd = self._cmd()
        self.assertIn("grep", cmd)
        self.assertIn("listen", cmd.lower())

    def test_caps_line_count(self):
        self.assertIn("head -200", self._cmd())

    def test_empty_output_says_so(self):
        """既无 ss 又无 netstat 时，别返回一句空字符串让人以为工具挂了。"""
        s = _src("mcp_agent.py")
        i = s.index('elif action == "listen":')
        seg = s[i:i + 1400]
        self.assertIn("没有监听端口", seg)


class LoginShellMigrationTest(unittest.TestCase):
    """老安装的配置里 `login_shell` 是安装器写死的 true，光改默认值救不了它们。"""

    def setUp(self):
        import json
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="fleet-cfg-")
        self.path = os.path.join(self.tmp, "mcp_agent_config.json")
        self._orig = mcp_agent.CONFIG_PATH
        mcp_agent.CONFIG_PATH = self.path
        self.json = json

    def tearDown(self):
        import shutil
        mcp_agent.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, cfg):
        with io.open(self.path, "w", encoding="utf-8") as f:
            self.json.dump(cfg, f)

    def _read(self):
        with io.open(self.path, encoding="utf-8") as f:
            return self.json.load(f)

    def test_old_true_is_flipped_and_persisted(self):
        self._write({"name": "nas", "auth_token": "t0k", "login_shell": True})
        cfg = mcp_agent.load_config()
        self.assertFalse(cfg["login_shell"])
        self.assertTrue(cfg["_login_shell_migrated"])
        disk = self._read()
        self.assertFalse(disk["login_shell"], "没落盘：下次启动又会迁一遍")
        self.assertEqual(disk["auth_token"], "t0k", "迁移不能弄丢令牌")

    def test_user_choice_is_respected_after_migration(self):
        """迁移只做一次：用户手动改回 true，不能被反复打回 false。"""
        self._write({"login_shell": True, "_login_shell_migrated": True})
        cfg = mcp_agent.load_config()
        self.assertTrue(cfg["login_shell"])

    def test_missing_key_uses_new_default_without_writing(self):
        self._write({"name": "nas"})
        before = io.open(self.path, encoding="utf-8").read()
        cfg = mcp_agent.load_config()
        self.assertFalse(cfg["login_shell"])
        self.assertEqual(io.open(self.path, encoding="utf-8").read(), before,
                         "文件里本来没这个键，不该被无谓改写")


class RemoteJobHintTest(unittest.TestCase):
    """`fleet exec-async istoreos ...` 打出的提示必须能直接复制。

    真机上照着提示敲 `fleet exec-poll <jid>`，它去问了**本机**，回一句
    「未知 job_id」；而照提示补上 target 之后，argparse 又把顺序读反了
    （target 排在 job_id 前面），报「找不到副端 b5db0c8c4625」。
    两处一起修：提示补 target + 按形状识别 job_id。
    """

    HINT = ("job_id=b5db0c8c4625\n"
            "取输出：fleet exec-poll b5db0c8c4625 ｜ 等完成：fleet exec-result b5db0c8c4625 "
            "｜ 终止：fleet exec-kill b5db0c8c4625\n")

    def test_hint_gets_target_appended(self):
        out = fleet._job_hint(self.HINT, "istoreos")
        for verb in ("exec-poll", "exec-result", "exec-kill"):
            self.assertIn("fleet %s b5db0c8c4625 istoreos" % verb, out)

    def test_local_hint_untouched(self):
        self.assertEqual(fleet._job_hint(self.HINT, ""), self.HINT)

    def test_garbage_text_untouched(self):
        self.assertEqual(fleet._job_hint("没有 job_id 的文本", "nas"), "没有 job_id 的文本")

    def _args(self, *argv):
        return fleet.build_parser().parse_args(list(argv))

    def test_job_id_first_order_is_swapped(self):
        """`exec-result <jid> <节点>`：argparse 会读成 target=jid。"""
        target, jid = fleet._pick_job_args(self._args("exec-result", "b5db0c8c4625", "istoreos"))
        self.assertEqual((target, jid), ("istoreos", "b5db0c8c4625"))

    def test_canonical_order_still_works(self):
        target, jid = fleet._pick_job_args(self._args("exec-result", "istoreos", "b5db0c8c4625"))
        self.assertEqual((target, jid), ("istoreos", "b5db0c8c4625"))

    def test_single_arg_is_job_id_on_local(self):
        target, jid = fleet._pick_job_args(self._args("exec-poll", "b5db0c8c4625"))
        self.assertEqual((target, jid), ("", "b5db0c8c4625"))

    def test_node_name_is_not_mistaken_for_job_id(self):
        self.assertIsNone(fleet._JOB_ID_RE.match("istoreos"))
        self.assertIsNone(fleet._JOB_ID_RE.match("http://10.0.0.1:3100/mcp"))
        self.assertIsNotNone(fleet._JOB_ID_RE.match("b5db0c8c4625"))


class RealDeviceRegressionTest(unittest.TestCase):
    """把真机观察到的数字钉进注释，防止下次又被"优化"掉。"""

    def test_numbers_recorded(self):
        s = _src("mcp_agent.py")
        self.assertIn("6943", s)
        self.assertIn("683KB", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
