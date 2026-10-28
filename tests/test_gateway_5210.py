#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.10 网关模式：一个 MCP endpoint 暴露整个集群。

没有它，接 5 台设备就要在客户端配 5 个 MCP 条目、看到 5×73 个同名工具；
有了它，客户端只认本机一个 URL，对端工具叫 `节点__工具`。

测试盯的是「聚合层特有的假对」：
  * 前缀冲突（`nas.lan` 和 `nas_lan`）不能静默合并——那会变成「调 A 打到 B」；
  * 一台离线不能毁掉整张表（并行拉取 + 跳过 + 留一句提示）；
  * `tools/call` **永不重试**：请求可能已经在对端开始执行了；
  * 网关链成环（A 聚合 B、B 又聚合 A）必须靠跳数拦住，否则 tools/list 无限递归；
  * 本机工具保持裸名：开网关不能把已有客户端配置改坏。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import mcp_agent as agent
import fleet

PY = sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_port(port, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            time.sleep(0.2)
        finally:
            s.close()
    return False


class _Env(object):
    """把 agent 的 peers 指向临时文件，别碰真实的 scripts/peers.json。"""

    def __init__(self, nodes):
        self.nodes = nodes

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-gw-")
        self.path = os.path.join(self.tmp, "peers.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"nodes": self.nodes}, f, ensure_ascii=False)
        self._peers = agent.PEERS_PATH
        self._chain = agent.AUDIT_CHAIN
        self._cfg = dict(agent.CFG)
        agent.PEERS_PATH = self.path
        agent.AUDIT_CHAIN = None          # 别往真实审计链里写测试事件
        with agent.GW_LOCK:
            agent.GW_CACHE.update({"ts": 0.0, "tools": [], "errors": [], "nodes": 0})
            agent.GW_SESS.clear()
        return self

    def __exit__(self, *exc):
        agent.PEERS_PATH = self._peers
        agent.AUDIT_CHAIN = self._chain
        agent.CFG.clear()
        agent.CFG.update(self._cfg)
        with agent.GW_LOCK:
            agent.GW_CACHE.update({"ts": 0.0, "tools": [], "errors": [], "nodes": 0})
            agent.GW_SESS.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


# ------------------------------------------------------------------ 前缀

class SafePrefixTest(unittest.TestCase):
    """节点名 → 工具名前缀。这里错一次就是「调 A 打到 B」。"""

    def test_plain(self):
        self.assertEqual(agent._gw_safe("nas"), "nas")
        self.assertEqual(agent._gw_safe("my-nas_2"), "my-nas_2")

    def test_dots_become_underscore(self):
        self.assertEqual(agent._gw_safe("nas.lan"), "nas_lan")

    def test_cjk_gets_hash_not_shared_name(self):
        a, b = agent._gw_safe("树莓派"), agent._gw_safe("路由器")
        self.assertTrue(a.startswith("node"))
        self.assertNotEqual(a, b, "两个中文名不能折成同一个前缀")

    def test_empty_is_stable(self):
        self.assertEqual(agent._gw_safe(""), agent._gw_safe(None))
        self.assertTrue(agent._gw_safe("").startswith("node"))

    def test_only_allowed_chars(self):
        for name in ("a b/c", "x:y", "设备 01", "q?w=1"):
            got = agent._gw_safe(name)
            self.assertRegex(got, r"^[0-9A-Za-z_-]+$", name)


class PickToolsTest(unittest.TestCase):
    """gateway_tools：core / all / 自定义清单。"""

    def setUp(self):
        self._cfg = dict(agent.CFG)

    def tearDown(self):
        agent.CFG.clear()
        agent.CFG.update(self._cfg)

    def test_core_is_subset(self):
        agent.CFG["gateway_tools"] = "core"
        got = [t["name"] for t in agent._gw_pick(agent.TOOLS)]
        self.assertIn("exec", got)
        self.assertIn("read", got)
        self.assertNotIn("fleet_repair_loop", got)
        self.assertLess(len(got), len(agent.TOOLS))

    def test_all(self):
        agent.CFG["gateway_tools"] = "all"
        self.assertEqual(len(agent._gw_pick(agent.TOOLS)), len(agent.TOOLS))

    def test_custom_csv(self):
        agent.CFG["gateway_tools"] = "exec, read ,sysinfo"
        self.assertEqual([t["name"] for t in agent._gw_pick(agent.TOOLS)],
                         ["exec", "read", "sysinfo"])

    def test_custom_list(self):
        agent.CFG["gateway_tools"] = ["ps"]
        self.assertEqual([t["name"] for t in agent._gw_pick(agent.TOOLS)], ["ps"])

    def test_empty_falls_back_to_core(self):
        agent.CFG["gateway_tools"] = ""
        got = [t["name"] for t in agent._gw_pick(agent.TOOLS)]
        self.assertIn("exec", got)
        self.assertLess(len(got), len(agent.TOOLS))

    def test_unknown_name_yields_nothing(self):
        agent.CFG["gateway_tools"] = "no_such_tool"
        self.assertEqual(agent._gw_pick(agent.TOOLS), [])


# ------------------------------------------------------------------ 选节点

class NodeListTest(unittest.TestCase):

    def test_lists_peers(self):
        nodes = {"b": {"url": "http://b/mcp"}, "a": {"url": "http://a/mcp"}}
        with _Env(nodes):
            self.assertEqual([n for n, _u, _t in agent._gw_node_list()],
                             ["a", "b"])

    def test_gateway_nodes_filter(self):
        nodes = {"a": {"url": "http://a/mcp"}, "b": {"url": "http://b/mcp"}}
        with _Env(nodes) as env:
            agent.CFG["gateway_nodes"] = ["b"]
            self.assertEqual([n for n, _u, _t in agent._gw_node_list()], ["b"])
            agent.CFG["gateway_nodes"] = "a,b"
            self.assertEqual([n for n, _u, _t in agent._gw_node_list()],
                             ["a", "b"])

    def test_skips_entries_without_url(self):
        nodes = {"ok": {"url": "http://x/mcp"}, "bad": {"token": "t"},
                 "weird": {"url": "ftp://x"}}
        with _Env(nodes):
            self.assertEqual([n for n, _u, _t in agent._gw_node_list()], ["ok"])

    def test_route_matches_prefix(self):
        nodes = {"nas.lan": {"url": "http://n/mcp", "token": "t"}}
        with _Env(nodes):
            hit = agent._gw_route("nas_lan__exec")
            self.assertIsNotNone(hit)
            self.assertEqual(hit[0], "nas.lan")
            self.assertEqual(hit[3], "exec")

    def test_route_unknown_prefix(self):
        with _Env({"nas": {"url": "http://n/mcp"}}):
            self.assertIsNone(agent._gw_route("pi__exec"))
            self.assertIsNone(agent._gw_route("exec"))       # 没有 __
            self.assertIsNone(agent._gw_route("__exec"))
            self.assertIsNone(agent._gw_route("nas__"))

    def test_route_ignores_local_prefix_like_tools(self):
        """带下划线但不是 `节点__工具` 的本机工具不能被劫走。"""
        with _Env({"nas": {"url": "http://n/mcp"}}):
            self.assertIsNone(agent._gw_route("docker_ps"))


# ------------------------------------------------------------------ 端到端

class GatewayE2ETest(unittest.TestCase):
    """真起一台副端，经网关聚合 + 路由，验证「一个 endpoint 管全网」。"""

    @classmethod
    def setUpClass(cls):
        # 副端**不能**读用户真实的 mcp_agent_config.json。这台开发机上那份
        # 开着网关、对端还离线，于是副端自己的 tools/list 要等好几秒——
        # 聚合超时看起来就像是被测代码的 bug。整份 scripts 复制到临时目录，
        # 配置 / peers / 审计日志全跟着隔离（*.json 一律不带，连私钥也不带）。
        cls.home = tempfile.mkdtemp(prefix="fleet-gw-e2e-")
        cls.root = os.path.join(cls.home, "scripts")
        shutil.copytree(SCRIPTS, cls.root,
                        ignore=shutil.ignore_patterns("*.json", "*.log", "__pycache__"))
        with open(os.path.join(cls.root, "mcp_agent_config.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"auth_token": "", "gateway": False}, f)
        cls.port = _free_port()
        cls.tok = ""
        cls.proc = subprocess.Popen(
            [PY, "-u", "mcp_agent.py", "--host", "127.0.0.1",
             "--port", str(cls.port), "--no-auto-secure", "--allow-insecure"],
            cwd=cls.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_port(cls.port, 30):
            cls.proc.kill()
            raise unittest.SkipTest("副端没起来（端口被占或环境不允许）")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.env = _Env({"peer1": {"url": "http://127.0.0.1:%d/mcp" % self.port,
                                   "token": self.tok}})
        self.env.__enter__()
        agent.CFG["gateway"] = True
        agent.CFG["gateway_tools"] = "exec,read,sysinfo"
        agent.CFG["gateway_cache_sec"] = 0        # 每次都真拉
        agent.CFG["gateway_call_timeout"] = 30

    def tearDown(self):
        self.env.__exit__()

    def test_tools_list_prefixed(self):
        tools, errors = agent.gw_tools(force=True)
        names = [t["name"] for t in tools]
        self.assertIn("peer1__exec", names)
        self.assertIn("peer1__sysinfo", names)
        self.assertEqual(errors, [])
        # 描述前面挂节点名，AI 才看得出是哪台机器
        d = dict((t["name"], t["description"]) for t in tools)
        self.assertTrue(d["peer1__exec"].startswith("[peer1]"))

    def test_core_filter_applies(self):
        names = [t["name"] for t in agent.gw_tools(force=True)[0]]
        self.assertIn("peer1__exec", names)
        self.assertNotIn("peer1__fleet_repair", names)

    def test_offline_peer_does_not_kill_list(self):
        """一台离线：其余照常出现，离线那台只留一句错误。"""
        with _Env({"peer1": {"url": "http://127.0.0.1:%d/mcp" % self.port,
                             "token": self.tok},
                   "dead": {"url": "http://127.0.0.1:%d/mcp" % _free_port()}}):
            agent.CFG["gateway"] = True
            agent.CFG["gateway_tools"] = "exec"
            agent.CFG["gateway_cache_sec"] = 0
            tools, errors = agent.gw_tools(force=True)
            names = [t["name"] for t in tools]
            self.assertIn("peer1__exec", names)
            self.assertFalse([n for n in names if n.startswith("dead__")])
            self.assertTrue(any(e.startswith("dead：") for e in errors), errors)

    def test_call_routes_to_peer(self):
        res = agent.gw_call("peer1__exec", {"command": "echo gateway-ok"})
        self.assertIsNotNone(res)
        self.assertFalse(res.get("isError"))
        text = "\n".join(c.get("text", "") for c in res.get("content") or [])
        self.assertIn("gateway-ok", text)

    def test_call_unknown_prefix_returns_none(self):
        """前缀不认识 → 返回 None，交回本机处理（可能是带 __ 的自定义工具）。"""
        self.assertIsNone(agent.gw_call("nosuchnode__exec", {"command": "true"}))

    def test_call_failure_reported_not_raised(self):
        with _Env({"dead": {"url": "http://127.0.0.1:%d/mcp" % _free_port()}}):
            agent.CFG["gateway"] = True
            agent.CFG["gateway_call_timeout"] = 3
            res = agent.gw_call("dead__exec", {"command": "true"})
            self.assertIsNotNone(res)
            self.assertTrue(res.get("isError"))
            self.assertIn("连不上", res["content"][0]["text"])

    def test_handle_rpc_tools_list_includes_both(self):
        out = agent.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertIn("exec", names)              # 本机裸名，不破坏已有配置
        self.assertIn("peer1__exec", names)

    def test_handle_rpc_gateway_off_is_unchanged(self):
        agent.CFG["gateway"] = False
        out = agent.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertNotIn("peer1__exec", names)
        self.assertEqual(len(names), len(agent.TOOLS))

    def test_handle_rpc_call_routes(self):
        out = agent.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                "params": {"name": "peer1__exec",
                                           "arguments": {"command": "echo via-rpc"}}})
        text = "\n".join(c.get("text", "") for c in out["result"]["content"])
        self.assertIn("via-rpc", text)

    def test_handle_rpc_local_call_still_works(self):
        out = agent.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "exec",
                                           "arguments": {"command": "echo local-ok"}}})
        text = "\n".join(c.get("text", "") for c in out["result"]["content"])
        self.assertIn("local-ok", text)

    def test_initialize_mentions_naming_rule(self):
        out = agent.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {}})
        ins = out["result"].get("instructions") or ""
        self.assertIn("peer1", ins)
        self.assertIn("__", ins)

    def test_cycle_guard_stops_tools_list_recursion(self):
        """A 聚合 B、B 又聚合 A：跳数到顶就只报自己的工具，不能无限递归。"""
        agent._REQ.hops = int(agent.CFG.get("peer_max_hops", 3) or 3)
        try:
            out = agent.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        finally:
            agent._REQ.hops = 0
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertNotIn("peer1__exec", names)
        self.assertEqual(len(names), len(agent.TOOLS))


# ------------------------------------------------------------------ CLI 侧

class _HangServer(object):
    """收下连接但**不回话**的 TCP 服务：模拟「机器在，但处理不动」。

    比「端口不通」更贴近真实现场——端口不通是立刻 ECONNREFUSED，
    真正拖垮客户端的是对端 accept 了却迟迟不返回。
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self.closed = False
        self.th = threading.Thread(target=self._run)
        self.th.daemon = True
        self.th.start()

    def _run(self):
        while not self.closed:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)          # 收下就不管：不读、不回、不关

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass


class LatencyBoundTest(unittest.TestCase):
    """tools/list 的等待必须有上限。

    真机上抓到的现场：开发机自己开着网关、对端离线，于是它替别人处理
    tools/list 时要去等一个超时——客户端看到的是「节点连不上」，而节点
    其实是好的。两道闸都得钉住：单台预算 + 整轮总等待。
    """

    def setUp(self):
        with agent.GW_LOCK:
            agent.GW_CACHE.update({"ts": 0.0, "tools": [], "errors": [],
                                   "nodes": 0, "ttl": 0.0})
        agent.CFG["gateway"] = True
        agent.CFG["gateway_tools"] = "all"

    def test_single_fetch_shares_one_deadline(self):
        """三次往返共用一份预算：挂住的机器只能拖 timeout，不能拖 3×timeout。"""
        srv = _HangServer()
        try:
            t0 = time.time()
            tools, why = agent._gw_fetch(
                "hang", "http://127.0.0.1:%d/mcp" % srv.port, "", 1.0, 1)
            cost = time.time() - t0
        finally:
            srv.close()
        self.assertIsNone(tools)
        self.assertTrue(why)
        self.assertLess(cost, 2.0, "单台预算 1s，实际拖了 %.1fs" % cost)

    def test_total_wait_is_capped(self):
        """整轮也有上限：8 台挂住的机器串行排队会拖成分钟级。"""
        srvs = [_HangServer() for _ in range(3)]
        try:
            nodes = dict(("h%d" % i,
                          {"url": "http://127.0.0.1:%d/mcp" % s.port})
                         for i, s in enumerate(srvs))
            with _Env(nodes):
                agent.CFG["gateway_list_timeout"] = 2
                agent.CFG["gateway_list_wait"] = 1
                agent.CFG["gateway_cache_sec"] = 0
                t0 = time.time()
                tools, errors = agent.gw_tools(force=True)
                cost = time.time() - t0
        finally:
            for s in srvs:
                s.close()
        self.assertEqual(tools, [])
        self.assertEqual(len(errors), 3)
        self.assertLess(cost, 2.5, "总等待上限 1s，实际拖了 %.1fs" % cost)

    def test_partial_result_gets_short_ttl(self):
        """没拿全就别按 300 秒缓存：设备重启后要等五分钟才重新出现。"""
        with _Env({"dead": {"url": "http://127.0.0.1:%d/mcp" % _free_port()}}):
            agent.CFG["gateway_cache_sec"] = 300
            agent.CFG["gateway_list_timeout"] = 1
            agent.CFG["gateway_list_wait"] = 2
            agent.gw_tools(force=True)
            self.assertLessEqual(agent.GW_CACHE["ttl"], 30)

    def test_empty_result_is_still_cached(self):
        """一台对端都拿不到时也要缓存。

        这是最该被缓存的场景：全部离线 → 聚合结果恰好是空列表。如果拿
        「tools 非空」当命中条件，每一次 tools/list 都要重新等一轮超时，
        网关就变成了「每连一次卡两秒」。
        """
        calls = []
        real = agent._gw_fetch

        def _spy(*a, **kw):
            calls.append(a[0])
            return real(*a, **kw)

        with _Env({"dead": {"url": "http://127.0.0.1:%d/mcp" % _free_port()}}):
            agent.CFG["gateway_cache_sec"] = 60
            agent.CFG["gateway_list_timeout"] = 1
            agent.CFG["gateway_list_wait"] = 2
            agent._gw_fetch = _spy
            try:
                agent.gw_tools(force=True)
                self.assertEqual(len(calls), 1)
                tools, errors = agent.gw_tools()          # 非 force：应命中缓存
                self.assertEqual(len(calls), 1, "空结果没被缓存，又去拉了")
                self.assertEqual(tools, [])
                self.assertTrue(errors)
            finally:
                agent._gw_fetch = real

    def test_defaults_present(self):
        self.assertIn("gateway_list_timeout", agent.DEFAULT_CONFIG)
        self.assertIn("gateway_list_wait", agent.DEFAULT_CONFIG)
        # 总等待不能比单台预算还宽松，否则那道闸等于没装
        self.assertLess(agent.DEFAULT_CONFIG["gateway_list_wait"],
                        agent.DEFAULT_CONFIG["gateway_call_timeout"])


class FleetSideTest(unittest.TestCase):

    def test_split_groups_by_prefix(self):
        local, remote = fleet._gw_split([
            {"name": "exec"}, {"name": "read"},
            {"name": "nas__exec"}, {"name": "nas__read"}, {"name": "pi__exec"}])
        self.assertEqual(sorted(local), ["exec", "read"])
        self.assertEqual(sorted(remote), ["nas", "pi"])
        self.assertEqual(sorted(remote["nas"]), ["exec", "read"])

    def test_split_empty(self):
        self.assertEqual(fleet._gw_split([]), ([], {}))

    def test_parser_wiring(self):
        ns = fleet.build_parser().parse_args(["gateway", "on"])
        self.assertEqual(ns.action, "on")
        self.assertFalse(ns.no_restart)
        self.assertIs(ns.func, fleet.cmd_gateway)

    def test_parser_defaults(self):
        ns = fleet.build_parser().parse_args(["gateway"])
        self.assertEqual(ns.action, "")

    def test_status_json_shape(self):
        """cmd_gateway 的 --json 是给脚本消费的，键名不能悄悄变。"""
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        for key in ('"config_gateway"', '"running"', '"local_tools"', '"remote"'):
            self.assertIn(key, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
