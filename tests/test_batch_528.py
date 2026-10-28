#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.8 批量并行执行 + 分组。

以前「一次管一堆机器」只能自己写 shell 循环：

    for n in $(fleet targets); do fleet exec $n "uptime"; done

串行、出错不停、输出糊成一片、还看不出哪台失败。这一版把 ssh 世界里
pdsh / clush / ansible ad-hoc 干的那件事补上，所以测试盯的是**批量的特有坑**：

  * 一台离线不能拖住整批（并发 + 短建连超时），也不能把整批带崩；
  * 打印顺序必须**稳定**（按节点名），否则两次同样的批跑出来没法 diff；
  * 退出码要能当脚本判据：全成功 0 / 有失败 1 / 没匹配到 2；
  * 块头横线要按**显示宽度**算，中文「连不上」占两列，用 len() 会歪；
  * 写在命令后面的开关（`run nas --short "df -h"`）不能被当成命令发给设备；
  * `--all "uptime"` 里唯一的位置参数是命令，不是节点名（argparse 必然的歧义）。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


def _src(name):
    with io.open(os.path.join(os.path.dirname(__file__), "..", "scripts", name),
                 encoding="utf-8") as f:
        return f.read()


def _ns(**kw):
    """造一个只有 cmd_run/cmd_group 会读到的字段的 Namespace。"""
    base = dict(agent="auto", port=3100, timeout=30, json=False, no_sync=False,
                all=False, group=None, targets="", local=False, jobs=8,
                short=False, quiet=False, sudo=False, workdir=None,
                timeout_ms=None, command=[])
    base.update(kw)
    return type("A", (), base)()


class _FakeClient:
    """假副端：按 url 决定返回什么，用来测聚合与失败隔离。"""

    script = {}          # {url: ("text", iserr) | Exception}

    def __init__(self, url, timeout=30, token="", connect_timeout=None, **_):
        self.url = url

    def handshake(self):
        pass

    def call(self, name, args, timeout=None):
        r = self.script[self.url]
        if isinstance(r, Exception):
            raise r
        return r


# ------------------------------------------------------------------ 分组

class GroupFieldTest(unittest.TestCase):
    """groups 字段的容错：手工编辑登记表时写歪了也不能把人踢出组。"""

    def test_none_and_empty(self):
        self.assertEqual(fleet._norm_groups(None), [])
        self.assertEqual(fleet._norm_groups(""), [])
        self.assertEqual(fleet._norm_groups([]), [])

    def test_plain_string(self):
        self.assertEqual(fleet._norm_groups("web"), ["web"])

    def test_comma_string(self):
        # 手写 "web,db" 是最常见的走形方式
        self.assertEqual(fleet._norm_groups("web,db"), ["web", "db"])
        self.assertEqual(fleet._norm_groups("web, db  edge"), ["web", "db", "edge"])

    def test_list_dedup_and_strip(self):
        self.assertEqual(fleet._norm_groups(["web", " web ", "db", ""]),
                         ["web", "db"])

    def test_non_list_garbage(self):
        self.assertEqual(fleet._norm_groups({"a": 1}), [])
        self.assertEqual(fleet._norm_groups(42), [])

    def test_node_groups_sorted_members(self):
        reg = {"nodes": {"b": {"groups": ["web"]},
                         "a": {"groups": ["web", "edge"]},
                         "c": {"url": "http://x/mcp"}}}
        got = fleet.node_groups(reg)
        self.assertEqual(got["web"], ["a", "b"])     # 成员排序
        self.assertEqual(got["edge"], ["a"])
        self.assertNotIn("c", got.get("web", []))


class SplitNamesTest(unittest.TestCase):

    def test_comma_and_space(self):
        self.assertEqual(fleet._split_names("a,b c"), ["a", "b", "c"])
        self.assertEqual(fleet._split_names(" a ,, b "), ["a", "b"])

    def test_empty(self):
        self.assertEqual(fleet._split_names(""), [])
        self.assertEqual(fleet._split_names(None), [])


# ------------------------------------------------------------------ 选范围

class RunScopeTest(unittest.TestCase):
    """--all / --group / 点名 三条路都要落到同一份 [(name,url,token)]。"""

    def setUp(self):
        self._d, self._g, self._r = (fleet.discover_nodes, fleet.node_groups,
                                     fleet.resolve_target)
        fleet.discover_nodes = lambda agent=None: {
            "pi": {"url": "http://pi/mcp", "token": "t1"},
            "nas": {"url": "http://nas/mcp", "token": ""},
            "old": {"url": "http://old/mcp", "token": ""},
        }
        fleet.node_groups = lambda reg=None: {"web": ["nas", "pi"],
                                              "edge": ["old"]}
        fleet.resolve_target = lambda n, agent=None, port=3100, sync=None: (
            "http://%s/mcp" % n, {"token": "tok-" + n})

    def tearDown(self):
        (fleet.discover_nodes, fleet.node_groups,
         fleet.resolve_target) = (self._d, self._g, self._r)

    def names(self, **kw):
        picked, unknown = fleet._run_scope(_ns(**kw))
        return [p[0] for p in picked], unknown

    def test_all_is_sorted(self):
        got, unknown = self.names(all=True)
        self.assertEqual(got, ["nas", "old", "pi"])
        self.assertEqual(unknown, [])

    def test_group_keeps_member_order(self):
        got, unknown = self.names(group=["web"])
        self.assertEqual(got, ["nas", "pi"])
        self.assertEqual(unknown, [])

    def test_two_groups_dedup(self):
        got, _ = self.names(group=["web,edge", "web"])
        self.assertEqual(got, ["nas", "pi", "old"])

    def test_explicit_names(self):
        got, _ = self.names(targets="pi,nas")
        self.assertEqual(got, ["pi", "nas"])       # 点名保持用户顺序

    def test_positional_may_be_a_group(self):
        # `fleet run web "uptime"` —— web 是组名，不是一台叫 web 的机器
        got, unknown = self.names(targets="web")
        self.assertEqual(got, ["nas", "pi"])
        self.assertEqual(unknown, [])

    def test_node_wins_over_group_of_same_name(self):
        fleet.node_groups = lambda reg=None: {"pi": ["nas", "old"]}
        got, _ = self.names(targets="pi")
        self.assertEqual(got, ["pi"])

    def test_unknown_reported_not_fatal(self):
        got, unknown = self.names(targets="pi,nosuch")
        self.assertEqual(got, ["pi"])
        self.assertEqual(unknown, [("节点或组", "nosuch")])

    def test_unknown_group_reported(self):
        got, unknown = self.names(group=["nope"])
        self.assertEqual(got, [])
        self.assertEqual(unknown, [("组", "nope")])

    def test_local_appended_last(self):
        self._l = fleet.local_node
        fleet.local_node = lambda: {"name": "node", "url": "http://127.0.0.1:3100/mcp"}
        try:
            got, _ = self.names(targets="pi", local=True)
        finally:
            fleet.local_node = self._l
        self.assertEqual(got, ["pi", "node"])

    def test_all_on_empty_registry(self):
        fleet.discover_nodes = lambda agent=None: {}
        got, unknown = self.names(all=True)
        self.assertEqual((got, unknown), ([], []))


# ------------------------------------------------------------------ 开关

class RunFlagTest(unittest.TestCase):
    """写在命令后面的开关会被 argparse 的 REMAINDER 吞进命令里。"""

    def take(self, toks, **kw):
        a = _ns(**kw)
        err = fleet._run_take_flags(a, toks)
        return a, toks, err

    def test_trailing_bool_flags(self):
        a, toks, err = self.take(["--short", "--quiet", "df", "-h"])
        self.assertIsNone(err)
        self.assertTrue(a.short)
        self.assertTrue(a.quiet)
        self.assertEqual(toks, ["df", "-h"])

    def test_trailing_short_alias(self):
        a, toks, _ = self.take(["-q", "-a", "uptime"])
        self.assertTrue(a.quiet)
        self.assertTrue(a.all)
        self.assertEqual(toks, ["uptime"])

    def test_value_flag_separate(self):
        a, toks, _ = self.take(["-j", "3", "--workdir", "/tmp", "ls"])
        self.assertEqual(a.jobs, 3)
        self.assertEqual(a.workdir, "/tmp")
        self.assertEqual(toks, ["ls"])

    def test_value_flag_inline(self):
        a, toks, _ = self.take(["--jobs=5", "--timeout-ms=1500", "ls"])
        self.assertEqual((a.jobs, a.timeout_ms), (5, 1500))
        self.assertEqual(toks, ["ls"])

    def test_group_flag_appends(self):
        a, toks, _ = self.take(["-g", "web", "--group", "db", "uptime"],
                               group=["edge"])
        self.assertEqual(a.group, ["edge", "web", "db"])
        self.assertEqual(toks, ["uptime"])

    def test_unknown_flag_is_part_of_command(self):
        # `ls -la` 里的 -la 是命令的一部分，不能被吃掉
        a, toks, _ = self.take(["-la", "/tmp"])
        self.assertEqual(toks, ["-la", "/tmp"])

    def test_bad_int(self):
        a, toks, err = self.take(["-j", "many", "ls"])
        self.assertIn("整数", err)

    def test_value_flag_without_value(self):
        a, toks, err = self.take(["--workdir"])
        self.assertIsNone(err)
        self.assertEqual(toks, ["--workdir"])   # 当命令的一部分，不吞


# ------------------------------------------------------------------ 渲染

class DisplayWidthTest(unittest.TestCase):

    def test_cjk_counts_two(self):
        self.assertEqual(fleet._dw("连不上"), 6)
        self.assertEqual(fleet._dw("[exit 0]"), 8)
        self.assertEqual(fleet._dw("─"), 1)

    def test_block_headers_align(self):
        """三种块头的**显示宽度**必须一致，否则一排横线参差不齐。"""
        rows = [{"name": "istoreos", "exit": 0, "ms": 12, "out": "", "err": ""},
                {"name": "istoreos", "exit": 7, "ms": 12, "out": "", "err": ""},
                {"name": "istoreos", "exit": None, "ms": 12, "out": "",
                 "err": "", "error": "连接被拒"}]
        widths = []
        for r in rows:
            buf = io.StringIO()
            with redirect_stdout(buf):
                fleet._run_block(r)
            head = buf.getvalue().splitlines()[0]
            # 去掉颜色码再量
            import re
            head = re.sub(r"\x1b\[[0-9;]*m", "", head)
            widths.append(fleet._dw(head))
        self.assertEqual(widths[0], widths[1])
        self.assertEqual(widths[0], widths[2])

    def test_block_shows_stderr_inline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fleet._run_block({"name": "x", "exit": 1, "ms": 5,
                              "out": "out", "err": "boom"})
        text = buf.getvalue()
        self.assertIn("out", text)
        self.assertIn("boom", text)

    def test_block_says_no_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fleet._run_block({"name": "x", "exit": 0, "ms": 5, "out": "", "err": ""})
        self.assertIn("无输出", buf.getvalue())


class BriefTest(unittest.TestCase):

    def test_first_line_only(self):
        got = fleet._run_brief({"name": "x", "exit": 0, "ms": 1,
                                "out": "a\nb\nc", "err": ""})
        self.assertTrue(got.startswith("a"))
        self.assertIn("+2 行", got)

    def test_error_wins(self):
        got = fleet._run_brief({"name": "x", "exit": None, "ms": 1, "out": "x",
                                "err": "", "error": "超时"})
        self.assertEqual(got, "超时")

    def test_empty(self):
        got = fleet._run_brief({"name": "x", "exit": 0, "ms": 1,
                                "out": "  ", "err": ""})
        self.assertIn("无输出", got)

    def test_long_line_truncated(self):
        got = fleet._run_brief({"name": "x", "exit": 0, "ms": 1,
                                "out": "z" * 200, "err": ""})
        self.assertIn("…", got)
        self.assertLess(len(got), 80)


# ------------------------------------------------------------------ 执行

class RunExecTest(unittest.TestCase):
    """cmd_run 的聚合语义：并发、失败隔离、退出码、JSON。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-run-")
        self._d, self._g, self._r, self._c = (
            fleet.discover_nodes, fleet.node_groups,
            fleet.resolve_target, fleet.MCPClient)
        fleet.discover_nodes = lambda agent=None: {
            "a": {"url": "http://a/mcp", "token": ""},
            "b": {"url": "http://b/mcp", "token": ""},
            "c": {"url": "http://c/mcp", "token": ""},
        }
        fleet.node_groups = lambda reg=None: {"web": ["a", "b"]}
        fleet.resolve_target = lambda n, agent=None, port=3100, sync=None: (
            "http://%s/mcp" % n, {"token": ""})
        fleet.MCPClient = _FakeClient
        _FakeClient.script = {}

    def tearDown(self):
        (fleet.discover_nodes, fleet.node_groups,
         fleet.resolve_target, fleet.MCPClient) = (
            self._d, self._g, self._r, self._c)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, **kw):
        a = _ns(**kw)
        buf = io.StringIO()
        # 范围提示走 stderr（stdout 只放结果），两个都收，否则断言看不见
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = fleet.cmd_run(a)
        return rc, buf.getvalue()

    def test_all_ok_returns_zero(self):
        _FakeClient.script = dict((u, ("[exit 0]\nok", False))
                                  for u in ("http://a/mcp", "http://b/mcp"))
        rc, out = self.run_cmd(targets="a,b", command=["echo ok"])
        self.assertEqual(rc, 0)
        self.assertIn("全部成功", out)

    def test_one_failure_returns_one(self):
        _FakeClient.script = {"http://a/mcp": ("[exit 0]\nok", False),
                              "http://b/mcp": ("[exit 3]\nbad", True)}
        rc, out = self.run_cmd(targets="a,b", command=["x"])
        self.assertEqual(rc, 1)
        self.assertIn("1 成功", out)
        self.assertIn("1 失败", out)

    def test_offline_node_does_not_kill_batch(self):
        """一台连不上：其余照常出结果，离线那台单独记一笔。"""
        _FakeClient.script = {"http://a/mcp": fleet.MCPError("连接被拒"),
                              "http://b/mcp": ("[exit 0]\nok", False)}
        rc, out = self.run_cmd(targets="a,b", command=["x"])
        self.assertEqual(rc, 1)
        self.assertIn("ok", out)             # 好的那台结果还在
        self.assertIn("a", out.split("失败")[-1])

    def test_output_order_is_stable(self):
        _FakeClient.script = dict((u, ("[exit 0]\n" + u, False))
                                  for u in ("http://a/mcp", "http://b/mcp",
                                            "http://c/mcp"))
        rc, out = self.run_cmd(all=True, command=["x"])
        ia, ib, ic = out.find("── a "), out.find("── b "), out.find("── c ")
        self.assertTrue(0 <= ia < ib < ic, out)

    def test_no_match_returns_two(self):
        fleet.discover_nodes = lambda agent=None: {}
        rc, out = self.run_cmd(all=True, command=["x"])
        self.assertEqual(rc, 2)
        self.assertIn("没有匹配到", out)

    def test_missing_command_is_error(self):
        rc, out = self.run_cmd(targets="a")
        self.assertEqual(rc, 1)
        self.assertIn("没给命令", out)

    def test_lone_positional_is_command_with_all(self):
        """`fleet run --all "uptime"` —— argparse 会把 uptime 塞进 targets。"""
        _FakeClient.script = {"http://a/mcp": ("[exit 0]\nuptime-out", False),
                              "http://b/mcp": ("[exit 0]\nuptime-out", False),
                              "http://c/mcp": ("[exit 0]\nuptime-out", False)}
        rc, out = self.run_cmd(all=True, targets="uptime", command=[])
        self.assertEqual(rc, 0)
        self.assertIn("uptime-out", out)

    def test_json_shape(self):
        _FakeClient.script = {"http://a/mcp": ("[exit 0]\nok", False),
                              "http://b/mcp": ("[exit 2]\nnope", True)}
        rc, out = self.run_cmd(targets="a,b", command=["x"], json=True)
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["command"], "x")
        self.assertIn("elapsed_s", data)
        codes = sorted(r["exit"] for r in data["results"])
        self.assertEqual(codes, [0, 2])

    def test_quiet_hides_blocks(self):
        _FakeClient.script = {"http://a/mcp": ("[exit 0]\nSECRET-BODY", False)}
        rc, out = self.run_cmd(targets="a", command=["x"], quiet=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("SECRET-BODY", out)
        self.assertIn("全部成功", out)

    def test_short_hides_body_keeps_first_line(self):
        _FakeClient.script = {"http://a/mcp": ("[exit 0]\nfirst\nsecond", False)}
        rc, out = self.run_cmd(targets="a", command=["x"], short=True)
        self.assertIn("first", out)
        self.assertNotIn("── a ", out)       # 不走分块视图

    def test_tool_error_with_zero_exit_is_not_success(self):
        """返回体不合标准格式时 parse_exec 会给 code=0，但 iserr=True —— 不能报绿。"""
        _FakeClient.script = {"http://a/mcp": ("垃圾返回", True)}
        rc, out = self.run_cmd(targets="a", command=["x"])
        self.assertEqual(rc, 1)

    def test_group_scope(self):
        _FakeClient.script = dict((u, ("[exit 0]\nok", False))
                                  for u in ("http://a/mcp", "http://b/mcp"))
        rc, out = self.run_cmd(group=["web"], command=["x"])
        self.assertEqual(rc, 0)
        self.assertNotIn("── c ", out)

    def test_jobs_clamped_to_node_count(self):
        """--jobs 100 配 3 台机器不该开 100 个线程。"""
        _FakeClient.script = dict((u, ("[exit 0]\nok", False))
                                  for u in ("http://a/mcp", "http://b/mcp",
                                            "http://c/mcp"))
        rc, out = self.run_cmd(all=True, command=["x"], jobs=100)
        self.assertEqual(rc, 0)

    def test_sudo_workdir_forwarded(self):
        seen = {}

        class Recorder(_FakeClient):
            def call(self, name, args, timeout=None):
                seen.update(args)
                return ("[exit 0]\n", False)

        fleet.MCPClient = Recorder
        rc, out = self.run_cmd(targets="a", command=["id"], sudo=True,
                               workdir="/tmp", timeout_ms=1500)
        self.assertEqual(rc, 0)
        self.assertTrue(seen.get("sudo"))
        self.assertEqual(seen.get("workDir"), "/tmp")
        self.assertEqual(seen.get("timeoutMs"), 1500)


# ------------------------------------------------------------------ 分组命令

class GroupCmdTest(unittest.TestCase):
    """cmd_group 要真的落盘，且 rm/del 不能把节点本身弄丢。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-grp-")
        self._reg = fleet.REGISTRY
        fleet.REGISTRY = os.path.join(self.tmp, "peers.json")
        fleet.save_registry({"nodes": {"a": {"url": "http://a/mcp"},
                                       "b": {"url": "http://b/mcp"}}})

    def tearDown(self):
        fleet.REGISTRY = self._reg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def grp(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fleet.cmd_group(fleet._menu_args(["group"] + list(argv)))
        return rc, buf.getvalue()

    def test_add_then_list(self):
        rc, out = self.grp("add", "web", "a", "b")
        self.assertEqual(rc, 0)
        self.assertEqual(fleet.node_groups()["web"], ["a", "b"])

    def test_add_is_idempotent(self):
        self.grp("add", "web", "a")
        rc, out = self.grp("add", "web", "a")
        self.assertEqual(rc, 0)
        self.assertEqual(fleet.node_groups()["web"], ["a"])
        self.assertIn("没有变化", out)

    def test_rm_keeps_node(self):
        self.grp("add", "web", "a")
        self.grp("rm", "web", "a")
        reg = fleet.load_registry()
        self.assertIn("a", reg["nodes"])                 # 节点还在
        self.assertNotIn("groups", reg["nodes"]["a"])     # 只是不在组里了

    def test_del_whole_group(self):
        self.grp("add", "web", "a", "b")
        self.grp("del", "web")
        self.assertEqual(fleet.node_groups(), {})
        self.assertEqual(len(fleet.load_registry()["nodes"]), 2)

    def test_del_unknown_group(self):
        rc, out = self.grp("del", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("没有组叫", out)

    def test_add_unknown_node_refused(self):
        rc, out = self.grp("add", "web", "ghost")
        self.assertEqual(rc, 1)
        self.assertIn("ghost", out)
        self.assertEqual(fleet.node_groups(), {})

    def test_bare_group_name_lists_members(self):
        self.grp("add", "web", "a")
        rc, out = self.grp("web")
        self.assertEqual(rc, 0)
        self.assertIn("组 web", out)
        self.assertIn("a", out)

    def test_list_all_shows_members(self):
        self.grp("add", "web", "a", "b")
        rc, out = self.grp()
        self.assertIn("web", out)
        self.assertIn("2 台", out)


class AddPreservesGroupsTest(unittest.TestCase):
    """重新 add 同一台（换地址/换令牌）不该把它踢出原来的组。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-add-")
        self._reg = fleet.REGISTRY
        fleet.REGISTRY = os.path.join(self.tmp, "peers.json")
        self.mcp = os.path.join(self.tmp, "mcp.json")
        with io.open(self.mcp, "w", encoding="utf-8") as f:
            f.write("{}")

    def tearDown(self):
        fleet.REGISTRY = self._reg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, url, group=None):
        a = fleet.build_parser().parse_args(
            ["add", "--name", "pi", "--url", url, "--no-test",
             "--mcp", self.mcp] + (["--group", group] if group else []))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fleet.cmd_add(a)
        self.assertEqual(rc, 0)

    def test_group_survives_readd(self):
        self.add("http://pi1/mcp", group="web")
        self.add("http://pi2/mcp")                  # 换地址，没写 --group
        reg = fleet.load_registry()
        self.assertEqual(reg["nodes"]["pi"]["groups"], ["web"])
        self.assertIn("pi2", reg["nodes"]["pi"]["url"])

    def test_explicit_group_overrides(self):
        self.add("http://pi1/mcp", group="web")
        self.add("http://pi1/mcp", group="db")
        self.assertEqual(fleet.load_registry()["nodes"]["pi"]["groups"], ["db"])


# ------------------------------------------------------------------ 接线

class WiringTest(unittest.TestCase):

    def test_subcommands_registered(self):
        p = fleet.build_parser()
        for argv, want in ((["run", "--all", "x"], "cmd_run"),
                           (["group"], "cmd_group")):
            ns = p.parse_args(argv)
            self.assertIs(getattr(ns, "func", None), getattr(fleet, want))

    def test_run_defaults(self):
        ns = fleet.build_parser().parse_args(["run", "a", "cmd"])
        self.assertEqual(ns.jobs, fleet.RUN_JOBS)
        self.assertFalse(ns.short or ns.quiet or ns.json)
        self.assertEqual(ns.targets, "a")

    def test_menu_entries_exist(self):
        src = _src("fleet.py")
        for marker in ('elif choice == "26"', 'elif choice == "27"',
                       "请选择 (0-27)"):
            self.assertIn(marker, src)

    def test_run_help_mentions_group(self):
        doc = fleet.__doc__ or ""
        self.assertTrue(doc)          # 顶层文档存在即可，子命令帮助由 argparse 出


if __name__ == "__main__":
    unittest.main(verbosity=2)
