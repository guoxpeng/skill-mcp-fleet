#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主控 HTTP 层：401 不重试、2MB 封顶、ping、probe/mesh 退出码、list 掩码、
cmd_sync 冒名拒绝且令牌零发出。全部用本地 mock 服务器，零真实节点。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
FAKE_TOKEN = "FAKE-TOKEN-1234567890abcdef"


def make_server(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, srv.server_address[1]


class Always401(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()


class BigBody(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _big(self):
        body = b"x" * (3 * 1024 * 1024)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._big()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._big()


class MockMCP(BaseHTTPRequestHandler):
    """可配行为的 MCP mock：类属性控制 exec 成功/失败、mesh 成功/失败、
    GET / 的 server 名；记录收到的 Authorization 头次数。"""
    exec_fail = False
    mesh_fail = False
    server_name = "mock"
    auth_seen = []

    def log_message(self, *a):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        MockMCP.auth_seen.append(self.headers.get("Authorization") or "")
        self._send_json({"status": "ok", "server": MockMCP.server_name,
                         "version": "9.9", "tools": 2})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            msg = {}
        method = msg.get("method", "")
        if method == "initialize":
            self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                             "result": {"serverInfo": {"name": "mock", "version": "9.9"}}})
        elif method == "notifications/initialized":
            self._send_json({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
        elif method == "tools/list":
            self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                             "result": {"tools": [{"name": "exec"}, {"name": "mesh"}]}})
        elif method == "tools/call":
            tool = (msg.get("params") or {}).get("name", "")
            if tool == "exec":
                if MockMCP.exec_fail:
                    self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"content": [{"text": "[exit 1]\nboom"}],
                                                "isError": True}})
                else:
                    self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"content": [{"text": "[exit 0]\nmockhost"}],
                                                "isError": False}})
            elif tool == "mesh":
                if MockMCP.mesh_fail:
                    self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"content": [{"text": "mesh broken"}],
                                                "isError": True}})
                else:
                    self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"content": [{"text": "{}"}],
                                                "isError": False}})
            else:
                self._send_json({"jsonrpc": "2.0", "id": msg.get("id"),
                                 "result": {"content": [{"text": ""}], "isError": False}})
        else:
            self._send_json({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})


class DirectoryMock(BaseHTTPRequestHandler):
    """地址目录 mock：/nodes 返回 impostor 条目；记录请求头。"""
    agent_port = 0
    auth_seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        DirectoryMock.auth_seen.append(self.headers.get("Authorization") or "")
        nodes = {"mock": {"url": "http://127.0.0.1:%d/mcp" % DirectoryMock.agent_port,
                          "age": 5}}
        body = json.dumps({"nodes": nodes}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_cli(args, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "fleet.py")] + args,
                       capture_output=True, text=True, timeout=60, env=env)
    return r


class HttpProbeTest(unittest.TestCase):
    def test_401_no_retry_fast(self):
        srv, port = make_server(Always401)
        try:
            t0 = time.time()
            with self.assertRaises(Exception):
                fleet.http_probe("http://127.0.0.1:%d/" % port, timeout=5, retries=3)
            self.assertLess(time.time() - t0, 2.0, "401 不应退避重试 3 次")
        finally:
            srv.shutdown()

    def test_2mb_cap(self):
        srv, port = make_server(BigBody)
        try:
            with self.assertRaises(fleet.MCPError):
                fleet.http_probe("http://127.0.0.1:%d/" % port, timeout=10, retries=0)
        finally:
            srv.shutdown()

    def test_post_cap(self):
        srv, port = make_server(BigBody)
        try:
            c = fleet.MCPClient("http://127.0.0.1:%d/mcp" % port, timeout=10)
            with self.assertRaises(fleet.MCPError):
                c._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        finally:
            srv.shutdown()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.reg = os.path.join(self.tmp.name, "nodes.json")
        self.env = {"HOME": self.home, "FLEET_REGISTRY": self.reg, "FLEET_NO_SYNC": "1"}
        MockMCP.exec_fail = False
        MockMCP.mesh_fail = False
        MockMCP.server_name = "mock"
        MockMCP.auth_seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def _write_reg(self, nodes):
        with open(self.reg, "w", encoding="utf-8") as f:
            json.dump({"nodes": nodes}, f)

    def _mock_node(self, port, token=FAKE_TOKEN):
        return {"mock": {"url": "http://127.0.0.1:%d/mcp" % port, "token": token}}

    def test_ping_ok_and_dead(self):
        srv, port = make_server(MockMCP)
        try:
            self._write_reg(self._mock_node(port))
            r = run_cli(["ping", "mock"], self.env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("可达", r.stdout)
        finally:
            srv.shutdown()
        self._write_reg(self._mock_node(1))  # 必然不通的端口
        r = run_cli(["ping", "mock"], self.env)
        self.assertEqual(r.returncode, 1)

    def test_probe_exec_fail_nonzero(self):
        srv, port = make_server(MockMCP)
        try:
            MockMCP.exec_fail = True
            self._write_reg(self._mock_node(port))
            r = run_cli(["probe", "mock"], self.env)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        finally:
            srv.shutdown()
        srv, port = make_server(MockMCP)
        try:
            MockMCP.exec_fail = False
            self._write_reg(self._mock_node(port))
            r = run_cli(["probe", "mock"], self.env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            srv.shutdown()

    def test_mesh_fail_nonzero(self):
        srv, port = make_server(MockMCP)
        try:
            MockMCP.mesh_fail = True
            self._write_reg(self._mock_node(port))
            r = run_cli(["mesh"], self.env)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("异常", r.stdout + r.stderr)
        finally:
            srv.shutdown()

    def test_list_json_masked_by_default(self):
        srv, port = make_server(MockMCP)
        try:
            self._write_reg(self._mock_node(port))
            r = run_cli(["list", "--json"], self.env)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            tok = out["mock"]["token"]
            self.assertNotIn(FAKE_TOKEN, tok)
            self.assertTrue(tok.startswith("FAKE") and tok.endswith("cdef"))
            r = run_cli(["list", "--json", "--show-tokens"], self.env)
            out = json.loads(r.stdout)
            self.assertEqual(out["mock"]["token"], FAKE_TOKEN)
        finally:
            srv.shutdown()

    def test_sync_impostor_refused_no_token_sent(self):
        agent_srv, agent_port = make_server(MockMCP)
        dir_srv, dir_port = make_server(DirectoryMock)
        try:
            MockMCP.server_name = "impostor"
            DirectoryMock.agent_port = agent_port
            DirectoryMock.auth_seen = []
            MockMCP.auth_seen = []
            reg = {"nodes": {"mock": {"url": "http://127.0.0.1:39999/mcp",
                                      "token": FAKE_TOKEN}},
                   "directory": {"url": "http://127.0.0.1:%d" % dir_port, "token": ""}}
            with open(self.reg, "w", encoding="utf-8") as f:
                json.dump(reg, f)
            r = run_cli(["sync", "mock"], self.env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("身份不符", r.stdout)
            with open(self.reg, encoding="utf-8") as f:
                after = json.load(f)
            self.assertEqual(after["nodes"]["mock"]["url"], "http://127.0.0.1:39999/mcp")
            # 冒名地址绝不能收到真令牌：agent 侧收到的 Authorization 必须全为空
            self.assertTrue(all(not h for h in MockMCP.auth_seen),
                            "冒名 agent 收到了 Authorization 头：%r" % MockMCP.auth_seen)
        finally:
            agent_srv.shutdown()
            dir_srv.shutdown()


if __name__ == "__main__":
    unittest.main()
