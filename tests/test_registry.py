#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1/S9/SEC-07：登记原子写、损坏拒绝与回退、IPv6、令牌掩码。零真实节点。"""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = os.path.join(self.tmp.name, "nodes.json")
        self.orig = fleet.REGISTRY
        fleet.REGISTRY = self.reg

    def tearDown(self):
        fleet.REGISTRY = self.orig
        self.tmp.cleanup()

    def test_atomic_save_and_bak_rotation(self):
        fleet.save_registry({"nodes": {"a": {"url": "http://127.0.0.1:3100/mcp"}}})
        self.assertTrue(os.path.isfile(self.reg))
        self.assertFalse(os.path.exists(self.reg + ".tmp"))
        mode = stat.S_IMODE(os.stat(self.reg).st_mode)
        self.assertEqual(mode, 0o600)
        fleet.save_registry({"nodes": {"a": {"url": "http://127.0.0.1:3101/mcp"},
                                       "b": {"url": "http://127.0.0.1:3102/mcp"}}})
        with open(self.reg + ".bak", encoding="utf-8") as f:
            bak = json.load(f)
        self.assertEqual(bak["nodes"]["a"]["url"], "http://127.0.0.1:3100/mcp")
        with open(self.reg, encoding="utf-8") as f:
            cur = json.load(f)
        self.assertIn("b", cur["nodes"])

    def test_corrupt_write_refused(self):
        with open(self.reg, "w", encoding="utf-8") as f:
            f.write("{not json!!!")
        with self.assertRaises(fleet.RegistryCorruptError):
            fleet.save_registry({"nodes": {}})
        with open(self.reg, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json!!!")

    def test_corrupt_read_falls_back_to_bak(self):
        with open(self.reg, "w", encoding="utf-8") as f:
            f.write("{broken")
        with open(self.reg + ".bak", "w", encoding="utf-8") as f:
            json.dump({"nodes": {"a": {"url": "http://127.0.0.1:3100/mcp"}}}, f)
        reg = fleet.load_registry()
        self.assertIn("a", reg["nodes"])

    def test_corrupt_read_no_bak_empty(self):
        with open(self.reg, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(fleet.load_registry(), {"nodes": {}})

    def test_normalize_ipv6(self):
        self.assertEqual(fleet.normalize_url("fd00::1"), "http://[fd00::1]:3100/mcp")
        self.assertEqual(fleet.normalize_url("::1"), "http://[::1]:3100/mcp")
        self.assertEqual(fleet.normalize_url("192.168.1.9"), "http://192.168.1.9:3100/mcp")
        self.assertEqual(fleet.normalize_url("[fd00::1]"), "http://[fd00::1]:3100/mcp")
        self.assertEqual(fleet.normalize_url("http://example.com/mcp"),
                         "http://example.com/mcp")

    def test_mask_token(self):
        self.assertEqual(fleet.mask_token("abcdefghij"), "abcd…ghij")
        self.assertEqual(fleet.mask_token("short"), "***")
        self.assertEqual(fleet.mask_token(""), "")


if __name__ == "__main__":
    unittest.main()
