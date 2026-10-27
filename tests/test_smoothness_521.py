#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.1 丝滑度层：延迟握手、连接池选路、cp 路径拆分、探活、pid 判定、版本号规范。

零真实节点、零网络：所有用例都在本地用假 client / 纯函数完成。
"""
import ipaddress
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


PROXY_KEYS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
              "ALL_PROXY", "all_proxy", "FLEET_POOL_PROXY")


def _clear_proxy_env():
    """清掉代理环境变量，保证用例不受开发机环境影响。"""
    return dict((k, os.environ.pop(k, None)) for k in PROXY_KEYS)


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------- 延迟握手
class _FakeClient(fleet.MCPClient):
    """记录发出的 JSON-RPC 方法名；可选「必须先 initialize」的严格模式。"""

    def __init__(self, strict=False):
        fleet.MCPClient.__init__(self, "http://127.0.0.1:9/mcp", timeout=1)
        self.strict = strict
        self.methods = []
        self.server_ready = False

    def _post_once(self, payload, timeout=None):
        m = payload.get("method")
        self.methods.append(m)
        if m == "initialize":
            self.server_ready = True
            return {"result": {"serverInfo": {"name": "fake", "version": "9.9"}}}
        if m == "notifications/initialized":
            return {}
        if m == "tools/call":
            if self.strict and not self.server_ready:
                raise fleet.MCPError("not initialized: send initialize first")
            return {"result": {"content": [{"type": "text", "text": "pong"}],
                               "isError": False}}
        raise AssertionError("未预期的方法：%s" % m)


class LazyHandshakeTest(unittest.TestCase):
    def test_ready_defaults_false(self):
        """__init__ 必须初始化 _ready，否则 call() 会 AttributeError。"""
        c = fleet.MCPClient("http://127.0.0.1:9/mcp")
        self.assertFalse(c._ready)

    def test_call_skips_handshake_when_server_allows(self):
        """宽容服务端：一次调用只有一个往返，不白发 initialize。"""
        c = _FakeClient(strict=False)
        text, is_err = c.call("fleet_ping", {})
        self.assertEqual(text, "pong")
        self.assertFalse(is_err)
        self.assertEqual(c.methods, ["tools/call"])

    def test_call_falls_back_to_handshake_when_required(self):
        """严格服务端：首次被拒后自动补握手再重试，用户无感。"""
        c = _FakeClient(strict=True)
        text, is_err = c.call("fleet_ping", {})
        self.assertEqual(text, "pong")
        self.assertFalse(is_err)
        self.assertEqual(c.methods, ["tools/call", "initialize",
                                     "notifications/initialized", "tools/call"])
        self.assertTrue(c._ready)

    def test_handshake_happens_at_most_once(self):
        """握手成功后不再重复；即便再次失败也直接抛出，避免死循环。"""
        c = _FakeClient(strict=True)
        c.call("fleet_ping", {})
        before = len(c.methods)
        c.server_ready = False          # 服务端「失忆」，但客户端已 ready
        c.strict = True
        with self.assertRaises(fleet.MCPError):
            c.call("fleet_ping", {})
        self.assertEqual(len(c.methods) - before, 1)   # 只多了一次 tools/call

    def test_unrelated_error_is_not_retried_as_handshake(self):
        """业务错误不能被误判成「需要握手」而重发——那等于把命令跑两遍。"""
        c = _FakeClient(strict=False)

        def boom(payload, timeout=None):
            c.methods.append(payload.get("method"))
            raise fleet.MCPError("执行失败：permission denied")

        c._post_once = boom
        with self.assertRaises(fleet.MCPError):
            c.call("fleet_exec", {"cmd": "rm -rf /tmp/x"})
        self.assertEqual(c.methods, ["tools/call"])


# ---------------------------------------------------------------- 连接池选路
class PoolPlanTest(unittest.TestCase):
    def setUp(self):
        self.env = _clear_proxy_env()

    def tearDown(self):
        _restore_env(self.env)

    def test_https_always_urllib(self):
        self.assertEqual(fleet._pool_plan("https://example.com/mcp"), (None, None))

    def test_loopback_direct(self):
        self.assertEqual(fleet._pool_plan("http://127.0.0.1:3100/mcp"),
                         ("direct", None))

    def test_lan_direct_even_with_proxy(self):
        """局域网节点必须绕开代理，否则会被代理成 404。"""
        os.environ["HTTP_PROXY"] = "http://10.0.0.1:8888"
        self.assertEqual(fleet._pool_plan("http://192.168.5.2:3100/mcp"),
                         ("direct", None))

    def test_tailnet_without_proxy_direct(self):
        self.assertEqual(fleet._pool_plan("http://100.113.145.90:3100/mcp"),
                         ("direct", None))

    def test_tailnet_with_proxy_falls_back_to_urllib(self):
        """代理下的长连接默认关闭：各家代理对第二条请求处理不一致。"""
        os.environ["HTTP_PROXY"] = "http://10.0.0.1:8888"
        self.assertEqual(fleet._pool_plan("http://100.113.145.90:3100/mcp"),
                         (None, None))

    def test_tailnet_proxy_pool_opt_in(self):
        os.environ["HTTP_PROXY"] = "http://10.0.0.1:8888"
        os.environ["FLEET_POOL_PROXY"] = "1"
        mode, proxy = fleet._pool_plan("http://100.113.145.90:3100/mcp")
        self.assertEqual(mode, "proxy")
        self.assertEqual(proxy[0], "10.0.0.1")
        self.assertEqual(proxy[1], 8888)

    def test_public_ip_no_proxy_direct(self):
        self.assertEqual(fleet._pool_plan("http://38.64.56.15:3100/mcp"),
                         ("direct", None))

    def test_proxy_addr_reads_auth(self):
        os.environ["HTTP_PROXY"] = "http://u:p@10.0.0.1:3128"
        host, port, auth = fleet._proxy_addr("http")
        self.assertEqual((host, port), ("10.0.0.1", 3128))
        self.assertTrue(auth.startswith("Basic "))


# ---------------------------------------------------------------- cp 路径拆分
class SplitCpSpecTest(unittest.TestCase):
    def test_known_node_wins(self):
        self.assertEqual(fleet._split_cp_spec("nas:/opt/a.py", {"nas"}),
                         ("nas", "/opt/a.py"))

    def test_single_letter_known_node(self):
        """单字母节点名不能被 Windows 盘符启发式吃掉。"""
        self.assertEqual(fleet._split_cp_spec("t:/tmp/a", {"t"}), ("t", "/tmp/a"))

    def test_windows_drive_is_local(self):
        self.assertEqual(fleet._split_cp_spec(r"C:\x\a.txt", set()),
                         (None, r"C:\x\a.txt"))

    def test_unknown_name_with_colon_is_local(self):
        self.assertEqual(fleet._split_cp_spec("a:b.txt", set()),
                         (None, "a:b.txt"))

    def test_plain_path_is_local(self):
        self.assertEqual(fleet._split_cp_spec("/tmp/a.py", {"nas"}),
                         (None, "/tmp/a.py"))

    def test_empty_spec(self):
        self.assertEqual(fleet._split_cp_spec("", {"nas"}), (None, ""))


# ---------------------------------------------------------------- 小工具
class HelperTest(unittest.TestCase):
    def test_mask_token(self):
        self.assertEqual(fleet.mask_token(""), "")
        self.assertEqual(fleet.mask_token("short"), "***")
        self.assertEqual(fleet.mask_token("12345678"), "***")
        self.assertEqual(fleet.mask_token("abcdefghijkl"), "abcd…ijkl")

    def test_normalize_url_ipv6(self):
        self.assertEqual(fleet.normalize_url("fd00::1"),
                         "http://[fd00::1]:3100/mcp")

    def test_normalize_url_ipv6_with_port(self):
        self.assertEqual(fleet.normalize_url("[fd00::1]:4000"),
                         "http://[fd00::1]:4000/mcp")

    def test_normalize_url_ipv4(self):
        self.assertEqual(fleet.normalize_url("192.168.1.5"),
                         "http://192.168.1.5:3100/mcp")

    def test_normalize_url_keeps_full_url(self):
        self.assertEqual(fleet.normalize_url("https://x.ts.net/mcp"),
                         "https://x.ts.net/mcp")

    def test_pid_alive_self(self):
        self.assertTrue(fleet._pid_alive(os.getpid()))

    def test_pid_alive_garbage(self):
        self.assertFalse(fleet._pid_alive(0))
        self.assertFalse(fleet._pid_alive(None))
        self.assertFalse(fleet._pid_alive("not-a-pid"))

    def test_default_scan_cidr_is_valid(self):
        cidr = fleet._default_scan_cidr()
        net = ipaddress.ip_network(cidr, strict=False)
        self.assertEqual(net.prefixlen, 24)

    def test_node_pid_path(self):
        self.assertEqual(fleet._node_pid_path("/opt/x"),
                         os.path.join("/opt/x", "run", "node.pid"))


# ---------------------------------------------------------------- 首屏不崩
class FirstRunSmokeTest(unittest.TestCase):
    """空环境敲 `fleet` 是新用户看到的第一屏，绝不能崩。

    这类 `%` 格式化参数对不上的 bug，只有真正走一遍空状态才暴露——
    之前的菜单测试带了对端，绕过了 `_first_run_guide()`。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        self.old_reg = fleet.REGISTRY
        os.environ["HOME"] = self.tmp.name
        fleet.REGISTRY = os.path.join(self.tmp.name, ".mcp-fleet", "nodes.json")

    def tearDown(self):
        fleet.REGISTRY = self.old_reg
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def test_first_run_guide_renders(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fleet._first_run_guide()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(fleet.VERSION, out)
        self.assertIn("install.sh", out)

    def test_no_arg_console_renders(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fleet.show_fleet_status()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(fleet.VERSION, out)


# ---------------------------------------------------------------- 版本号规范
class VersionPolicyTest(unittest.TestCase):
    """版本号必须是三段式，且所有落点同源。

    两段式（5.2 / 4.6）会让「每次改动都要进位」成为习惯，版本涨得比内容快，
    回滚和评审都没法对账。这里把规范钉在测试里，改坏了 CI 直接红。
    """

    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def test_three_part_format(self):
        self.assertRegex(fleet.VERSION, r"^\d+\.\d+\.\d+$")

    def test_all_version_sources_agree(self):
        sys.path.insert(0, os.path.join(self.ROOT, "scripts"))
        import build_package
        self.assertEqual(build_package.check_version_sync(), [])

    def test_skill_frontmatter_matches_code(self):
        path = os.path.join(self.ROOT, "SKILL.md")
        with open(path, encoding="utf-8") as f:
            head = f.read(600)
        m = re.search(r"^version:\s*(\S+)", head, re.M)
        self.assertIsNotNone(m, "SKILL.md frontmatter 缺少 version")
        self.assertEqual(m.group(1), fleet.VERSION)


if __name__ == "__main__":
    unittest.main()
