#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5.2.4：真实使用者会怎么敲命令。

这一批全是「敲下去才发现」的问题，不是读代码能读出来的：

1. `fleet ping` 不带参数 → argparse 直接报 usage。它是「我的节点活着吗」的
   第一反应，要求先知道副端名字不合理（status/health/audit 都默认本机）。
2. `fleet exec "df -h"` → 那句命令被 argparse 当成 target，于是回一句
   「命令为空」。用户在**完全正确的手势**下被劝退，而且看不出原因。
3. `fleet exec` 的退出码被压成 1，`exit 42` 和 `command not found` 一样。
   要替代 ssh，退出码必须原样回传，否则脚本里的判断全错。
4. `fleet audit` 在默认安装上**永远失败**：哈希链和人类可读日志用了同一个
   文件名，纯文本行让链 verify 恒为 (False, 0)。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import fleet  # noqa: E402


class KnownTargetTest(unittest.TestCase):
    def setUp(self):
        self._orig = fleet.discover_nodes
        fleet.discover_nodes = lambda agent=None: {
            "nas": {"url": "http://192.168.1.9:3100/mcp", "token": ""},
        }

    def tearDown(self):
        fleet.discover_nodes = self._orig

    def test_registered_name_is_target(self):
        self.assertTrue(fleet._is_known_target("nas", "auto"))

    def test_url_and_ip_are_targets(self):
        self.assertTrue(fleet._is_known_target("http://1.2.3.4:3100/mcp"))
        self.assertTrue(fleet._is_known_target("192.168.1.9"))
        self.assertTrue(fleet._is_known_target("nas.lan:3100"))

    def test_bare_command_word_is_not_target(self):
        self.assertFalse(fleet._is_known_target("ls"))
        self.assertFalse(fleet._is_known_target("df -h"))


class PickExecTargetTest(unittest.TestCase):
    def setUp(self):
        self._orig = fleet.discover_nodes
        fleet.discover_nodes = lambda agent=None: {
            "nas": {"url": "http://192.168.1.9:3100/mcp", "token": ""},
        }

    def tearDown(self):
        fleet.discover_nodes = self._orig

    @staticmethod
    def _ns(target, command, local=False):
        return type("A", (), {"target": target, "command": list(command),
                              "agent": "auto", "local": local})()

    def test_single_unknown_token_becomes_local_command(self):
        """`fleet exec "df -h"` → 本机执行，而不是「命令为空」。"""
        target, toks = fleet._pick_exec_target(
            self._ns("df -h", []), [])
        self.assertEqual(target, "")
        self.assertEqual(toks, ["df -h"])

    def test_registered_target_untouched(self):
        target, toks = fleet._pick_exec_target(
            self._ns("nas", ["df -h"]), ["df -h"])
        self.assertEqual(target, "nas")
        self.assertEqual(toks, ["df -h"])

    def test_registered_target_without_command_stays(self):
        """`fleet exec nas` 没给命令：保持 target，让上层报用法，别去本机跑 nas。"""
        target, toks = fleet._pick_exec_target(self._ns("nas", []), [])
        self.assertEqual(target, "nas")
        self.assertEqual(toks, [])

    def test_local_flag_forces_local(self):
        target, toks = fleet._pick_exec_target(
            self._ns("whoami", [], local=True), [])
        self.assertEqual(target, "")
        self.assertEqual(toks, ["whoami"])

    def test_local_flag_with_target_and_command(self):
        target, toks = fleet._pick_exec_target(
            self._ns("nas", ["ls"], local=True), ["ls"])
        self.assertEqual(target, "")
        self.assertEqual(toks, ["nas", "ls"])


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.p = fleet.build_parser()

    def test_ping_without_target(self):
        a = self.p.parse_args(["ping"])
        self.assertIsNone(a.target)
        self.assertEqual(a.timeout, 5)

    def test_ping_with_target(self):
        a = self.p.parse_args(["ping", "nas"])
        self.assertEqual(a.target, "nas")

    def test_exec_local_flag(self):
        a = self.p.parse_args(["exec", "--local", "whoami"])
        self.assertTrue(a.local)
        self.assertEqual(a.target, "whoami")

    def test_exec_with_target_and_command(self):
        a = self.p.parse_args(["exec", "nas", "ls -l"])
        self.assertEqual(a.target, "nas")
        self.assertEqual(a.command, ["ls -l"])

    def test_exec_async_without_target(self):
        a = self.p.parse_args(["exec-async", "--local", "sleep 1"])
        self.assertTrue(a.local)


class MainDoesNotShortCircuitTest(unittest.TestCase):
    """main() 里不能再有 `exec and not command → die`：它会抢在 cmd_exec 之前。"""

    def test_main_has_no_exec_empty_guard(self):
        import re
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        m = re.search(r"^def main\(\):(.*?)(?=^def |\Z)", src, re.S | re.M)
        self.assertTrue(m)
        body = m.group(1)
        self.assertNotIn('die("命令为空")', body,
                         "main() 又抢在 cmd_exec 前面拦空命令了")
        self.assertIn("_pick_exec_target", src)


class MenuArgsTest(unittest.TestCase):
    """交互菜单是「不带参数运行 fleet」的第一入口，不能一选就崩。

    原来每项都手搓 `argparse.Namespace(...)`，漏一个属性就是运行时
    `AttributeError: 'Namespace' object has no attribute 'workdir'`：
    exec / msg send / msg history / probe / doctor / health / tools / mesh
    全部中招，而且只有真的选了那一项才暴露。现在统一走 build_parser()。
    """

    CASES = [
        ["exec", "node", "echo hi"],
        ["health"],
        ["tools", "node"],
        ["mesh"],
        ["msg", "node", "send", "--text", "hi"],
        ["msg", "node", "history", "--lines", "20"],
        ["call", "node", "--tool", "sysinfo", "--args", "{}"],
        ["probe", "node", "--timeout", "5"],
        ["doctor", "node", "--timeout", "10"],
        ["remove", "node"],
        ["add", "--name", "x", "--url", "http://1.2.3.4:3100/mcp"],
        ["pair", "node"],
        ["discover"],
        ["discover", "--deep"],
    ]

    def test_menu_argv_parse(self):
        for argv in self.CASES:
            a = fleet._menu_args(argv)
            self.assertTrue(callable(getattr(a, "func", None)),
                            "%s 没解析出处理函数" % " ".join(argv))

    def test_every_subcommand_has_func(self):
        """`fleet discover` 曾因漏 set_defaults 而 `a.func(a)` 直接崩。"""
        p = fleet.build_parser()
        sub = [a for a in p._actions
               if a.__class__.__name__ == "_SubParsersAction"][0]
        missing = [n for n, sp in sub.choices.items() if "func" not in sp._defaults]
        self.assertEqual(missing, [], "这些子命令没有绑定处理函数：%s" % missing)

    def test_menu_uses_parser_not_handmade_namespace(self):
        """结构性断言：菜单里不该再出现手搓 Namespace。"""
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        start = src.index("def cmd_menu(a):")
        end = src.index("\ndef main()")
        body = src[start:end]
        self.assertNotIn("argparse.Namespace(", body,
                         "菜单里又出现了手搓 Namespace（漏属性 = 运行时崩溃）")
        self.assertIn("_menu_args(", body)


class DoctorSummaryTest(unittest.TestCase):
    """「10/12 通过」会让人去追那 2 项，而它们往往是故意的默认值。

    本机回环不配令牌、内网不配 TLS，都是合理默认；把它们和真问题混在
    同一个比例里，等于让用户去修不存在的故障。摘要要区分
    「真问题」与「加固建议」，每项也要标 critical。
    """

    def test_summary_separates_advice(self):
        import json
        import mcp_agent as agent
        text, _is_err = agent.t_fleet_doctor({})
        rep = json.loads(text)
        self.assertTrue(all("critical" in c for c in rep["checks"]),
                        "每项检查都要标 critical，前端才能区分")
        if any(not c["ok"] and not c["critical"] for c in rep["checks"]):
            self.assertIn("加固建议", rep["summary"])
        # 关键项全过时，整体必须判为 ok（不能因为没配 TLS 就报故障）
        crit_ok = all(c["ok"] for c in rep["checks"] if c["critical"])
        self.assertEqual(rep["ok"], crit_ok)


class ExecExitCodeTest(unittest.TestCase):
    """`exit 42` 必须回 42，而不是笼统的 1。"""

    def _run(self, script):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "fleet.py")]
                              + script, capture_output=True, text=True)

    def test_exit_code_propagates(self):
        """没有运行中的节点时跳过（这条是端到端，依赖本机节点）。"""
        me = fleet.local_node()
        if not me or not fleet._local_health(me, timeout=1.5):
            self.skipTest("本机没有运行中的节点")
        r = self._run(["exec", "--local", "exit 42"])
        self.assertEqual(r.returncode, 42, r.stdout + r.stderr)
        r = self._run(["exec", "--local", "true"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class AuditChainPathTest(unittest.TestCase):
    """哈希链不能再和人类可读的 audit.log 抢同一个文件名。"""

    def setUp(self):
        import mcp_agent as agent
        self.agent = agent
        self.dir = tempfile.mkdtemp(prefix="fleet-chain-")
        self._orig = agent.DATA_DIR
        agent.DATA_DIR = self.dir

    def tearDown(self):
        self.agent.DATA_DIR = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fresh_dir_uses_jsonl(self):
        got = self.agent._audit_chain_path()
        self.assertTrue(got.endswith("audit.chain.jsonl"), got)

    def test_plain_text_log_does_not_steal_chain(self):
        with open(os.path.join(self.dir, "audit.log"), "w", encoding="utf-8") as f:
            f.write("2026-10-27 14:44:49 | ip=- | tool=exec | args={}\n")
        got = self.agent._audit_chain_path()
        self.assertTrue(got.endswith("audit.chain.jsonl"), got)
        self.assertFalse(self.agent._looks_like_chain(
            os.path.join(self.dir, "audit.log")))

    def test_existing_real_chain_is_kept(self):
        """老节点：链本来就在 audit.log 里，不能换文件把历史丢了。"""
        import json
        rec = {"idx": 0, "ts": 1, "prev": "0" * 64,
               "event": {"tool": "exec"}, "hash": "a" * 64}
        with open(os.path.join(self.dir, "audit.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        got = self.agent._audit_chain_path()
        self.assertTrue(got.endswith(os.path.join("", "audit.log")), got)
        self.assertNotIn("audit.chain.jsonl", got)

    def test_chain_file_wins_when_both_exist(self):
        open(os.path.join(self.dir, "audit.chain.jsonl"), "a").close()
        open(os.path.join(self.dir, "audit.log"), "w").write("plain\n")
        got = self.agent._audit_chain_path()
        self.assertTrue(got.endswith("audit.chain.jsonl"), got)


if __name__ == "__main__":
    unittest.main()
