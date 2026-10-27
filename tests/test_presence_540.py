#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.4 长在线可见性：`fleet health` 要能回答「这台是不是一直在线」。

只看当次 UP 是看不出稳定性的——一台每 5 分钟重启一次的服务，每次探活都是 UP。
所以这里有两组东西：
  * 副端自己报的进程在线时长（uptime_s，来自 /fleet/health）
  * 主端本地记账（first_seen / last_ok / up_count），掉线不清零
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class FmtAgeTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(fleet._fmt_age(0), "0s")
        self.assertEqual(fleet._fmt_age(59), "59s")

    def test_minutes(self):
        self.assertEqual(fleet._fmt_age(60), "1m")
        self.assertEqual(fleet._fmt_age(3599), "59m")

    def test_hours_and_days(self):
        self.assertEqual(fleet._fmt_age(3600), "1h00m")
        self.assertEqual(fleet._fmt_age(3700), "1h01m")
        self.assertEqual(fleet._fmt_age(86400), "1d00h")
        self.assertEqual(fleet._fmt_age(90000), "1d01h")

    def test_garbage_does_not_crash(self):
        self.assertEqual(fleet._fmt_age("x"), "?")


class SeenBookTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="fleet-seen-")
        self.path = os.path.join(self.dir, "seen.json")
        self._old = fleet._seen_path
        fleet._seen_path = lambda: self.path

    def tearDown(self):
        fleet._seen_path = self._old

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(fleet._load_seen(), {})

    def test_roundtrip(self):
        d = {"nas": {"first_seen": 100, "last_ok": 200, "up_count": 3}}
        fleet._save_seen(d)
        self.assertEqual(fleet._load_seen(), d)

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(fleet._load_seen(), {})

    def test_non_dict_json_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")
        self.assertEqual(fleet._load_seen(), {})


class _HealthHandler(BaseHTTPRequestHandler):
    payload = {}

    def do_GET(self):
        if self.path != "/fleet/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class NodeUptimeTest(unittest.TestCase):
    def setUp(self):
        _HealthHandler.payload = {"status": "ok", "uptime_s": 7200, "peers": 2}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_reads_uptime_and_strips_mcp_suffix(self):
        d = fleet._node_uptime("http://127.0.0.1:%d/mcp" % self.port)
        self.assertIsNotNone(d)
        self.assertEqual(d["uptime_s"], 7200)
        self.assertEqual(d["peers"], 2)

    def test_unreachable_returns_none_not_raise(self):
        self.assertIsNone(fleet._node_uptime("http://127.0.0.1:1/mcp", timeout=1))

    def test_non_http_url_returns_none(self):
        self.assertIsNone(fleet._node_uptime("nas"))

    def test_wrong_path_returns_none(self):
        """/fleet/health 不存在时不能当成 uptime=0，只能当作「不知道」。"""
        self.assertIsNone(fleet._node_uptime("http://127.0.0.1:%d/other" % self.port))


if __name__ == "__main__":
    unittest.main()
