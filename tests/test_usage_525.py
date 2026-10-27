#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5.2.5：把常用命令从头到尾真跑一遍才看得见的问题。

这一批的共同点是「单看某一行都对，串起来就矛盾」：
- `fleet nodes` 报的回连地址自己都连不上，而 `fleet status` 报的是回环；
- `exec-async` 提示 `fleet exec-poll <job_id>`，可 poll 的 target 是必填；
- `cp -r ./src nas:/opt/app/src` 得到 `/opt/app/src/src/...`，往返一次多一层；
- `sysinfo` 只按 Linux 写，macOS 上内存/CPU 两段空白；
- 自检把「代码目录是 0755」当成数据目录配错，照做还会 chmod 代码目录；
- `fleet call sysinfo` / `fleet tools` 这种「省略副端」的写法被 argparse 挡住；
- `fleet logs` 说「未找到日志文件」，而 `fleet start` 明明写了一份；
- `fleet doctor` 把 13 项检查原样甩成 JSON；
- UDP 发现端口被占时每 2/4/8 秒刷一条 warn，macOS 上还根本共享不了。
"""
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import fleet  # noqa: E402


class LocalNodeAsTargetTest(unittest.TestCase):
    """本机节点不在任何登记表里，但它必须是合法 target。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-local-")
        self._orig = (fleet.discover_nodes, fleet.local_node)
        fleet.discover_nodes = lambda agent=None: {}
        fleet.local_node = lambda self_url=None: {
            "name": "node", "url": "http://127.0.0.1:3100/mcp",
            "token": "tok", "dir": self.dir}

    def tearDown(self):
        (fleet.discover_nodes, fleet.local_node) = self._orig

    def test_resolve_local_name(self):
        url, meta = fleet.resolve_target("node", "auto", 3100, sync=False)
        self.assertIn("127.0.0.1", url)
        self.assertEqual(meta.get("token"), "tok")

    def test_cp_split_recognizes_local_name(self):
        """`fleet cp ./a.txt node:/tmp/a.txt` 不能被判成「格式不对」。"""
        me = fleet.local_node()
        known = {"node": {"url": me["url"], "token": me["token"]}}
        self.assertEqual(fleet._split_cp_spec("node:/tmp/a.txt", known),
                         ("node", "/tmp/a.txt"))
        self.assertEqual(fleet._split_cp_spec("./a.txt", known), (None, "./a.txt"))


class JobArgsTest(unittest.TestCase):
    """`fleet exec-poll <job_id>`：只有一个位置参数时它是 job_id。"""

    @staticmethod
    def _ns(target=None, job_id=None):
        return type("A", (), {"target": target, "job_id": job_id})()

    def test_single_arg_is_job_id(self):
        self.assertEqual(fleet._pick_job_args(self._ns("abc123")), ("", "abc123"))

    def test_two_args_keep_both(self):
        self.assertEqual(fleet._pick_job_args(self._ns("nas", "abc123")),
                         ("nas", "abc123"))

    def test_none_is_none(self):
        self.assertEqual(fleet._pick_job_args(self._ns()), ("", ""))

    def test_parser_accepts_single_positional(self):
        a = fleet.build_parser().parse_args(["exec-poll", "abc123"])
        self.assertEqual(a.job_id, None)
        self.assertEqual(fleet._pick_job_args(a), ("", "abc123"))


class CpTarLayoutTest(unittest.TestCase):
    """目录传输要打 `-C <dir> .`，否则目标目录里多套一层源目录名。"""

    def test_remote_tar_packs_dot(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        m = re.search(r"^def _remote_tar\(.*?\n(?=\ndef )", src, re.S | re.M)
        self.assertTrue(m)
        body = m.group(0)
        self.assertIn('" ."', body, "远端打包必须以 . 结尾（包内是内容，不是目录名）")
        self.assertNotIn("os.path.basename(base)", body)

    def test_push_tar_packs_dot(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        self.assertIn('["tar", "czf", tarpath, "-C", base, "."]', src)

    def test_roundtrip_layout(self):
        """真打一次包，确认解出来没有多一层。"""
        import subprocess
        d = tempfile.mkdtemp(prefix="fleet-tar-")
        src = os.path.join(d, "src")
        os.makedirs(os.path.join(src, "sub"))
        open(os.path.join(src, "a.txt"), "w").write("a")
        open(os.path.join(src, "sub", "b.txt"), "w").write("b")
        tgz = os.path.join(d, "p.tgz")
        subprocess.check_call(["tar", "czf", tgz, "-C", src, "."])
        out = os.path.join(d, "out")
        os.makedirs(out)
        subprocess.check_call(["tar", "xzf", tgz, "-C", out])
        got = sorted(os.path.relpath(os.path.join(r, f), out)
                     for r, _ds, fs in os.walk(out) for f in fs)
        self.assertEqual(got, ["a.txt", os.path.join("sub", "b.txt")])


class PeerUrlHonestyTest(unittest.TestCase):
    """只监听回环时，回连地址不能报内网 IP（自己都连不上）。"""

    def setUp(self):
        import mcp_agent as agent
        self.agent = agent
        self._orig = (agent.LISTEN_HOST, agent.LISTEN_PORT, agent.load_peers)
        agent.LISTEN_PORT = 3100
        agent.load_peers = lambda: {"nodes": {}}

    def tearDown(self):
        (self.agent.LISTEN_HOST, self.agent.LISTEN_PORT,
         self.agent.load_peers) = self._orig

    def test_loopback_reports_loopback_with_hint(self):
        self.agent.LISTEN_HOST = "127.0.0.1"
        text, _err = self.agent.t_peers({})
        first = text.splitlines()[0]
        self.assertIn("127.0.0.1:3100", first)
        self.assertIn("只监听回环", first)

    def test_public_bind_uses_self_url(self):
        self.agent.LISTEN_HOST = "0.0.0.0"
        text, _err = self.agent.t_peers({})
        self.assertNotIn("只监听回环", text.splitlines()[0])

    def test_peer_info_marks_unreachable(self):
        import json
        self.agent.LISTEN_HOST = "127.0.0.1"
        text, _err = self.agent.t_peer_info({})
        info = json.loads(text)
        self.assertFalse(info["reachable"])
        self.assertTrue(info["unreachable_reason"])

    def test_peer_info_reachable_when_public(self):
        import json
        self.agent.LISTEN_HOST = "0.0.0.0"
        info = json.loads(self.agent.t_peer_info({})[0])
        self.assertTrue(info["reachable"])
        self.assertEqual(info["unreachable_reason"], "")


class SelfcheckPermTest(unittest.TestCase):
    """代码目录 0755 不是「数据目录配错」；修复动作也不能去 chmod 它。"""

    def setUp(self):
        import mcp_agent as agent
        self.agent = agent
        self._orig = agent.DATA_DIR
        self.dir = tempfile.mkdtemp(prefix="fleet-sc-")
        os.chmod(self.dir, 0o755)
        agent.DATA_DIR = self.dir

    def tearDown(self):
        self.agent.DATA_DIR = self._orig

    def test_shared_data_dir_is_info_not_perm(self):
        self.agent.DATA_DIR = self.agent.HERE      # 模拟「没配 data_dir」
        codes = [f["code"] for f in self.agent._selfcheck_findings()]
        self.assertIn("data_dir_shared", codes)
        # 代码目录的 0755 不该被当成 dir_perm
        perms = [f for f in self.agent._selfcheck_findings()
                 if f["code"] == "dir_perm" and "data_dir" in f["title"]]
        self.assertEqual(perms, [])

    def test_separate_data_dir_still_checked(self):
        codes = [f["code"] for f in self.agent._selfcheck_findings()]
        self.assertIn("dir_perm", codes, "独立数据目录 0755 仍应提示")

    def test_fix_permissions_skips_code_dir(self):
        src = open(os.path.join(SCRIPTS, "mcp_agent.py"), encoding="utf-8").read()
        m = re.search(r'if action == "fix_permissions":(.*?)if action == "clear_stale_pid"',
                      src, re.S)
        self.assertTrue(m)
        body = m.group(1)
        self.assertIn("os.path.abspath(DATA_DIR) != os.path.abspath(HERE)", body,
                      "修复动作必须和检查用同一套判断，否则会 chmod 代码目录")


class CallGestureTest(unittest.TestCase):
    """`fleet call sysinfo`：工具名被 argparse 吃成 target。

    和 `fleet exec "df -h"` 是同一个坑。`--tool` 曾经是 `required=True`，
    于是「在本机调一个工具」这个最自然的写法直接被 argparse 挡在门外，
    报的还是一句看不懂的 usage。
    """

    class _FakeClient(object):
        last = None

        def __init__(self, url, timeout=None, token="", connect_timeout=None):
            self.url = url
            self.tool = None
            self.args = None
            CallGestureTest._FakeClient.last = self

        def handshake(self):
            pass

        def call(self, tool, args, timeout=None):
            self.tool, self.args = tool, args
            return ("ok:%s" % tool), False

    def setUp(self):
        import io
        self._orig = (fleet.MCPClient, fleet._is_known_target,
                      fleet._local_endpoint, fleet.resolve_target,
                      sys.stdout, sys.stderr)
        fleet.MCPClient = self._FakeClient
        fleet._is_known_target = lambda t, agent=None: t in ("node",)
        fleet._local_endpoint = lambda: ("http://127.0.0.1:3100/mcp",
                                         {"token": "", "name": "node"})
        fleet.resolve_target = lambda t, agent=None, port=3100, sync=None: (
            "http://10.0.0.9:3100/mcp", {"token": "", "name": t})
        self.out = io.StringIO()
        self.err = io.StringIO()
        sys.stdout, sys.stderr = self.out, self.err

    def tearDown(self):
        (fleet.MCPClient, fleet._is_known_target, fleet._local_endpoint,
         fleet.resolve_target, sys.stdout, sys.stderr) = self._orig

    def _run(self, argv):
        a = fleet.build_parser().parse_args(argv)
        return fleet.cmd_call(a)

    def test_bare_tool_name_runs_locally(self):
        self.assertEqual(self._run(["call", "sysinfo"]), 0)
        c = self._FakeClient.last
        self.assertEqual(c.tool, "sysinfo")
        self.assertIn("127.0.0.1", c.url, "只有一个位置参数时它是工具名，应在本机执行")

    def test_explicit_tool_with_node(self):
        self.assertEqual(self._run(["call", "node", "--tool", "sysinfo"]), 0)
        c = self._FakeClient.last
        self.assertEqual(c.tool, "sysinfo")
        self.assertIn("10.0.0.9", c.url)

    def test_tool_only_no_target(self):
        self.assertEqual(self._run(["call", "--tool", "sysinfo"]), 0)
        self.assertEqual(self._FakeClient.last.tool, "sysinfo")
        self.assertIn("127.0.0.1", self._FakeClient.last.url)

    def test_args_still_parse(self):
        self.assertEqual(self._run(["call", "read", "--arg", "path=/etc/hosts"]), 0)
        self.assertEqual(self._FakeClient.last.args, {"path": "/etc/hosts"})

    def test_missing_tool_says_what_to_do(self):
        self.assertEqual(self._run(["call"]), 1)
        msg = self.err.getvalue()
        self.assertIn("工具名", msg)
        self.assertIn("fleet call", msg, "报错里要给出能直接照抄的写法")

    def test_tool_flag_not_required(self):
        """`--tool` 不能是 required：否则最自然的写法连门都进不去。"""
        a = fleet.build_parser().parse_args(["call", "sysinfo"])
        self.assertEqual(a.tool, "")
        self.assertEqual(a.target, "sysinfo")


class LogsPathTest(unittest.TestCase):
    """`fleet start` 把日志写到 run/node.log，`fleet logs` 却去别处找。"""

    def test_run_log_first(self):
        paths = fleet._node_log_paths("/opt/node")
        self.assertEqual(paths[0], os.path.join("/opt/node", "run", "node.log"),
                         "启动时重定向的那份必须排第一，否则刚起来的节点也「找不到日志」")

    def test_start_and_logs_agree(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        self.assertIn("logpath = _node_log_paths(d)[0]", src)
        m = re.search(r"^def cmd_logs\(.*?\n(?=\ndef )", src, re.S | re.M)
        self.assertTrue(m)
        self.assertIn("_node_log_paths(me[\"dir\"])", m.group(0),
                      "logs 必须和 start 用同一张表，不然改一处就漂")

    def test_missing_log_says_how_to_create(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        m = re.search(r"^def cmd_logs\(.*?\n(?=\ndef )", src, re.S | re.M)
        body = m.group(0)
        self.assertIn("fleet start", body, "没日志时要说怎么才有日志")


class DoctorReportTest(unittest.TestCase):
    """`fleet doctor` 是给人敲的，不该甩一坨 JSON。"""

    def _run(self, text):
        import io
        buf = io.StringIO()
        old, sys.stdout = sys.stdout, buf
        try:
            fleet._print_doctor_report(text)
        finally:
            sys.stdout = old
        return buf.getvalue()

    def test_json_becomes_readable(self):
        import json
        payload = json.dumps({"ok": True, "summary": "2/3 通过（1 项加固建议未启用：tls）",
                              "checks": [
                                  {"name": "python", "ok": True, "detail": "3.13", "critical": True},
                                  {"name": "tls", "ok": False, "detail": "未配置", "critical": False},
                                  {"name": "port", "ok": True, "detail": "可连", "critical": True}]})
        out = self._run(payload)
        self.assertNotIn("{", out, "人读模式不该出现 JSON 花括号")
        self.assertIn("python", out)
        self.assertIn("3.13", out)
        self.assertIn("通过", out)
        self.assertIn("tls", out)

    def test_critical_failure_is_called_out(self):
        import json
        payload = json.dumps({"ok": False, "summary": "1/2 通过", "checks": [
            {"name": "port", "ok": False, "detail": "不可连", "critical": True},
            {"name": "disk", "ok": True, "detail": "ok", "critical": True}]})
        out = self._run(payload)
        self.assertIn("先解决", out)
        self.assertIn("port", out)

    def test_unparsable_prints_raw(self):
        out = self._run("不是 JSON，但也不能吞")
        self.assertIn("不能吞", out)

    def test_json_flag_keeps_json(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        self.assertIn('if getattr(a, "json", False):\n            print(text)', src,
                      "--json 必须保留原始 JSON，脚本靠它解析")


class DiscoveryPortTest(unittest.TestCase):
    """同机多节点共用 UDP 发现端口：要能共享，且不能刷屏。"""

    def test_reuseport_set(self):
        src = open(os.path.join(SCRIPTS, "fleet_proto.py"), encoding="utf-8").read()
        m = re.search(r"def _listen_loop\(self\):(.*?)\n    def ", src, re.S)
        self.assertTrue(m)
        body = m.group(1)
        self.assertIn("SO_REUSEPORT", body,
                      "macOS 上只设 SO_REUSEADDR 仍然 EADDRINUSE")
        self.assertIn("SO_REUSEADDR", body)

    def test_warns_bounded(self):
        src = open(os.path.join(SCRIPTS, "fleet_proto.py"), encoding="utf-8").read()
        m = re.search(r"def _listen_loop\(self\):(.*?)\n    def ", src, re.S)
        body = m.group(1)
        self.assertIn("bind_failures <= 3", body)
        self.assertIn("不再重复提示", body,
                      "无限重试就该只说一次，否则日志里全是同一条 warn")
        self.assertIn("其余功能正常", body, "要说明发现失效不影响已登记节点的连接")


class UpdateCheckAliasTest(unittest.TestCase):
    """`fleet update --check` 是「有没有新版」最自然的写法，别让它报 usage。"""

    def test_parser_accepts_check(self):
        a = fleet.build_parser().parse_args(["update", "--check"])
        self.assertTrue(a.check)

    def test_delegates_to_up(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        m = re.search(r"^def cmd_update\(.*?\n(?=\ndef )", src, re.S | re.M)
        self.assertTrue(m)
        body = m.group(0)
        self.assertIn('["up", "--check"]', body,
                      "检查新版只有一处实现，别在这里再写一份")
        self.assertIn("_menu_args", body, "转发也必须走 parser，不能手搓 Namespace")


class SysinfoPortableTest(unittest.TestCase):
    """sysinfo 是新人调的第一个工具，不能在 macOS 上留两个空标题。"""

    def test_commands_have_fallbacks(self):
        import mcp_agent as agent
        import inspect
        src = inspect.getsource(agent.t_sysinfo)
        self.assertIn("free -h 2>/dev/null", src)
        self.assertIn("sysctl -n hw.memsize", src, "macOS 没有 free，要退回 sysctl")
        self.assertIn("ps -Ao pcpu,pmem,comm -r", src, "BSD ps 不认 --sort")
        self.assertNotIn("fmt_result", src, "报告不该带 [exit 0] 头")


class InviteOutputTest(unittest.TestCase):
    def test_uri_printed_once(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        m = re.search(r'if uri:(.*?)return 0', src, re.S)
        self.assertTrue(m)
        body = m.group(1)
        # 只允许出现一次「把 uri 插值进输出」的 print
        hits = [l for l in body.splitlines()
                if "print" in l and "% uri" in l]
        self.assertEqual(len(hits), 1, "邀请码被打了好几遍，终端折行到没法复制")


if __name__ == "__main__":
    unittest.main()
