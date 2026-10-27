#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 侧：配置损坏拒绝、sudo 密码不进命令串、同步 exec 有界、
邀请并发单 200 + 标记失败 500、UDP 发现验真。零真实节点，全用假令牌。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent

FAKE_PW = "FAKE-SUDO-PW-12345"
FAKE_TOKEN = "FAKE-PEER-TOKEN-abcdef"


class ConfigTest(unittest.TestCase):
    def test_corrupt_config_refuses(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.write("{corrupt!!!")
        tmp.close()
        orig_path = mcp_agent.CONFIG_PATH
        orig_flag = mcp_agent._CONFIG_LOAD_ERROR
        try:
            mcp_agent.CONFIG_PATH = tmp.name
            mcp_agent.load_config()
            self.assertTrue(mcp_agent._CONFIG_LOAD_ERROR)
            with self.assertRaises(SystemExit) as cm:
                mcp_agent.die_if_config_corrupt()
            self.assertEqual(cm.exception.code, 2)
        finally:
            mcp_agent.CONFIG_PATH = orig_path
            mcp_agent._CONFIG_LOAD_ERROR = orig_flag
            os.unlink(tmp.name)

    def test_missing_config_ok(self):
        orig_path = mcp_agent.CONFIG_PATH
        orig_flag = mcp_agent._CONFIG_LOAD_ERROR
        try:
            mcp_agent.CONFIG_PATH = "/nonexistent-dir-xyz/mcp_agent_config.json"
            mcp_agent.load_config()
            self.assertEqual(mcp_agent._CONFIG_LOAD_ERROR, "")
            mcp_agent.die_if_config_corrupt()  # 不抛
        finally:
            mcp_agent.CONFIG_PATH = orig_path
            mcp_agent._CONFIG_LOAD_ERROR = orig_flag


class SudoTest(unittest.TestCase):
    def test_password_not_in_command_string(self):
        orig_pw = mcp_agent.CFG.get("sudo_password")
        orig_root = mcp_agent._is_root
        try:
            mcp_agent.CFG["sudo_password"] = FAKE_PW
            mcp_agent._is_root = lambda: False
            cmd = mcp_agent._wrap("id -u", sudo=True)
            self.assertNotIn(FAKE_PW, cmd)
            self.assertIn("sudo -S", cmd)
            prefix = mcp_agent._sudo_stdin_prefix()
            self.assertIn(FAKE_PW.encode(), prefix)
        finally:
            if orig_pw is None:
                mcp_agent.CFG.pop("sudo_password", None)
            else:
                mcp_agent.CFG["sudo_password"] = orig_pw
            mcp_agent._is_root = orig_root

    def test_run_local_bounded(self):
        code, out, err = mcp_agent.run_local(
            "python3 -c \"import sys; sys.stdout.write('y'*1500000)\"", timeout=60)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.encode("utf-8")), mcp_agent.RUN_BUF_MAX + 1024)
        self.assertIn("超 1MB", err)


def _mock_agent_server(good=True, auth=False):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if good:
                self._send({"status": "ok", "server": "mocknode", "version": "9.9"})
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"<html>")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if auth:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class VerifyAgentTest(unittest.TestCase):
    def test_good_agent(self):
        srv, port = _mock_agent_server(good=True)
        try:
            self.assertTrue(mcp_agent._verify_agent("http://127.0.0.1:%d/mcp" % port))
        finally:
            srv.shutdown()

    def test_garbage_not_agent(self):
        srv, port = _mock_agent_server(good=False)
        try:
            self.assertFalse(mcp_agent._verify_agent("http://127.0.0.1:%d/mcp" % port))
        finally:
            srv.shutdown()

    def test_auth_endpoint_counts(self):
        srv, port = _mock_agent_server(good=True, auth=True)
        try:
            self.assertTrue(mcp_agent._verify_agent("http://127.0.0.1:%d/mcp" % port))
        finally:
            srv.shutdown()

    def test_dead_not_agent(self):
        self.assertFalse(mcp_agent._verify_agent("http://127.0.0.1:1/mcp"))


class InviteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_cfg = mcp_agent.CONFIG_PATH
        self.orig_peers = mcp_agent.PEERS_PATH
        mcp_agent.CONFIG_PATH = os.path.join(self.tmp.name, "mcp_agent_config.json")
        mcp_agent.PEERS_PATH = os.path.join(self.tmp.name, "peers.json")
        with open(mcp_agent._invite_file_path(), "w", encoding="utf-8") as f:
            json.dump({"inv1": {"expires": int(time.time()) + 3600, "used": False,
                                "created": int(time.time())}}, f)

    def tearDown(self):
        mcp_agent.CONFIG_PATH = self.orig_cfg
        mcp_agent.PEERS_PATH = self.orig_peers
        self.tmp.cleanup()

    def _payload(self):
        return {"invite_id": "inv1", "name": "peerA",
                "url": "http://127.0.0.1:39999/mcp", "token": FAKE_TOKEN}

    def test_concurrent_verify_single_200(self):
        results = []

        def worker():
            results.append(mcp_agent._verify_invite(self._payload())[0])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1, results)
        self.assertEqual(results.count(403), 7, results)
        with open(mcp_agent._invite_file_path(), encoding="utf-8") as f:
            inv = json.load(f)["inv1"]
        self.assertTrue(inv["used"])
        self.assertEqual(inv["used_by"], "peerA")

    def test_mark_failure_500(self):
        orig = mcp_agent._save_invites_atomic
        try:
            def boom(invites, path):
                raise OSError("FAKE disk full")
            mcp_agent._save_invites_atomic = boom
            code, body = mcp_agent._verify_invite(self._payload())
            self.assertEqual(code, 500)
            self.assertIn("标记", body.get("error", ""))
        finally:
            mcp_agent._save_invites_atomic = orig


if __name__ == "__main__":
    unittest.main()
