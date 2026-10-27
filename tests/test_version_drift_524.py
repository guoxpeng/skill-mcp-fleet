# -*- coding: utf-8 -*-
"""5.2.4：运行中的 agent 版本 ≠ 磁盘代码版本时必须说出来。

真实场景（就是开发机上发生的）：改了 `mcp_agent.py`、跑完测试全绿，
`fleet status` 还报老版本 —— 因为 agent 是长驻进程，磁盘换了代码它不换。
用户会以为「修好了却没效果」，于是去怀疑代码、怀疑网络、怀疑人生。

这里钉住三件事：
1. 能从节点目录的源码里读到版本（锚在 DEFAULT_CONFIG，不误取别处的 version）；
2. 三方版本（进程 / 磁盘 / CLI）对不上时，提示必须出现且可执行；
3. 读不到磁盘版本时**不能**乱报（宁可不提示，也不要制造噪音）。
"""
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import fleet  # noqa: E402

AGENT_SRC = """# -*- coding: utf-8 -*-
\"\"\"说明文档：这里提到 /health 会返回 {"version": "9.9.9"} 的样例。\"\"\"

DEFAULT_CONFIG = {
    "name": "node",
    "tool_prefix": "",
    "version": "%s",
    "sudo_password": "",
}
SERVER_VERSION = DEFAULT_CONFIG["version"]
"""


def _mk_node_dir(version, body=None):
    d = tempfile.mkdtemp(prefix="fleet-ver-")
    with open(os.path.join(d, "mcp_agent.py"), "w", encoding="utf-8") as f:
        f.write(body if body is not None else AGENT_SRC % version)
    return d


class DiskVersionTest(unittest.TestCase):
    def test_reads_version_from_source(self):
        d = _mk_node_dir("5.2.4")
        self.assertEqual(fleet._disk_agent_version(d), "5.2.4")

    def test_anchors_on_default_config_not_first_occurrence(self):
        """文件头部的样例串 `"version": "9.9.9"` 不能被当成真版本。"""
        d = _mk_node_dir("5.2.4")
        self.assertEqual(fleet._disk_agent_version(d), "5.2.4")
        self.assertNotEqual(fleet._disk_agent_version(d), "9.9.9")

    def test_missing_file_is_empty_not_crash(self):
        self.assertEqual(fleet._disk_agent_version(tempfile.mkdtemp()), "")
        self.assertEqual(fleet._disk_agent_version(""), "")
        self.assertEqual(fleet._disk_agent_version(None), "")

    def test_version_far_from_head_still_found(self):
        """头顶塞一大段说明（> 8KB）也要读得到——否则就是静默不检查。"""
        pad = "\n".join("# 填充注释 %04d" % i for i in range(900))
        body = "# -*- coding: utf-8 -*-\n" + pad + "\n" + (AGENT_SRC % "5.2.4")
        self.assertGreater(len(body), 8192)
        d = _mk_node_dir(None, body=body)
        self.assertEqual(fleet._disk_agent_version(d), "5.2.4")


class DriftLinesTest(unittest.TestCase):
    def test_drift_reports_running_vs_disk(self):
        d = _mk_node_dir("5.2.4")
        lines = fleet._version_drift_lines("5.4.0", d, "5.2.4")
        text = "\n".join(lines)
        self.assertIn("5.4.0", text)
        self.assertIn("5.2.4", text)
        self.assertIn("fleet restart", text, "提示必须给出可执行的下一步")

    def test_no_drift_no_noise(self):
        d = _mk_node_dir("5.2.4")
        self.assertEqual(fleet._version_drift_lines("5.2.4", d, "5.2.4"), [])

    def test_unknown_disk_version_is_silent(self):
        """读不到磁盘版本（单文件部署）时不许猜、不许报。"""
        d = tempfile.mkdtemp()
        self.assertEqual(fleet._version_drift_lines("5.4.0", d, "5.2.4"), [])
        self.assertEqual(fleet._version_drift_lines("5.4.0", None, "5.2.4"), [])

    def test_unknown_running_version_still_checks_cli(self):
        """进程版本拿不到，但 CLI 与磁盘不一致，也要说。"""
        d = _mk_node_dir("5.1.0")
        lines = fleet._version_drift_lines("", d, "5.2.4")
        text = "\n".join(lines)
        self.assertIn("5.1.0", text)
        self.assertIn("5.2.4", text)
        self.assertIn("install.sh", text)


class StatusIntegrationTest(unittest.TestCase):
    """cmd_status 真的会把提示打出来（不是只在函数里存在）。"""

    def setUp(self):
        self.dir = _mk_node_dir("5.2.4")
        self._orig = (fleet._local_mcp, fleet._http_json, fleet._running_agent_version)
        me = {"name": "node", "url": "http://127.0.0.1:3100/mcp",
              "token": "", "dir": self.dir}

        class _C(object):
            def call(self, *a, **k):
                return "", False

        fleet._local_mcp = lambda a: (me, _C())

        def fake_http(url, token="", timeout=10, insecure=False):
            if url.endswith("/fleet/health"):
                return {"name": "node", "version": "5.4.0", "uptime_s": 1,
                        "peers": 0, "inbox": 0, "outbox": 0}
            return {"node_id": "n1", "fingerprint": "FP", "trust": "S2"}
        fleet._http_json = fake_http

    def tearDown(self):
        (fleet._local_mcp, fleet._http_json,
         fleet._running_agent_version) = self._orig

    def _run(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fleet.cmd_status(object())
        return rc, buf.getvalue()

    def test_status_prints_drift(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("5.4.0", out)
        self.assertIn("fleet restart", out)

    def test_reverse_control_no_warning_when_versions_match(self):
        """反向验证：版本一致时提示必须消失，否则上面的断言可能是恒真。"""
        def fake_http(url, token="", timeout=10, insecure=False):
            if url.endswith("/fleet/health"):
                return {"name": "node", "version": "5.2.4", "uptime_s": 1,
                        "peers": 0, "inbox": 0, "outbox": 0}
            return {"node_id": "n1", "fingerprint": "FP", "trust": "S2"}
        fleet._http_json = fake_http
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("fleet restart", out)


class SourceGuardTest(unittest.TestCase):
    """结构性断言：这些入口都得接上漂移检查，别只接了 status 一个。"""

    def test_entry_points_are_wired(self):
        src = open(os.path.join(ROOT, "scripts", "fleet.py"),
                   encoding="utf-8").read()
        for fn in ("cmd_status", "cmd_doctor", "cmd_start"):
            m = re.search(r"^def %s\(a\):(.*?)(?=^def )" % fn, src, re.S | re.M)
            self.assertTrue(m, "找不到 %s" % fn)
            self.assertIn("_version_drift_lines", m.group(1),
                          "%s 没接版本漂移检查" % fn)


if __name__ == "__main__":
    unittest.main()
