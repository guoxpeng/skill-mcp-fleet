#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门：flock、原子写、恢复复测、ping 腿超时定性、全轮模拟。零真实节点。"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

# 看门脚本就在仓库里：用相对路径，别指向某台机器上的私有目录。
GOAL_WATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "scripts", "fleet_watch.py")

FAKE_FLEET = """#!/usr/bin/env python3
import json, os, sys
ctl_path = os.environ.get("FAKE_FLEET_CTL", "")
ctl = {}
if ctl_path and os.path.exists(ctl_path):
    with open(ctl_path, encoding="utf-8") as f:
        ctl = json.load(f)
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "list":
    print(json.dumps(ctl.get("nodes", {})))
    sys.exit(0)
if cmd == "ping":
    name = sys.argv[2]
    sys.exit(0 if ctl.get("ping", {}).get(name, 1) == 0 else 1)
if cmd == "call":
    name = sys.argv[2]
    spec = ctl.get("exec", {}).get(name, {})
    if spec.get("ok"):
        print("FLEET_WATCH_OK")
        sys.exit(0)
    print("exec 失败：HTTP 401" if spec.get("code") == 401 else "exec 失败：连不上")
    sys.exit(1)
print("unknown cmd", file=sys.stderr)
sys.exit(2)
"""


def load_watch():
    spec = importlib.util.spec_from_file_location("fleet_watch_goal", GOAL_WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WatchUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load_watch()
        self.mod.STATE_FILE = os.path.join(self.tmp.name, "watch_state.json")
        self.mod.LOCK_FILE = os.path.join(self.tmp.name, "watch.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_save(self):
        self.mod.save_state({"a": {"ok": True}})
        self.assertFalse(os.path.exists(self.mod.STATE_FILE + ".tmp"))
        with open(self.mod.STATE_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["a"]["ok"], True)

    def test_recovery_needs_two_usable(self):
        calls = {"n": 0}

        def fake_check(name, url, agent):
            calls["n"] += 1
            if calls["n"] == 1:
                return True, True, "可用"
            return False, False, "复测失败"

        self.mod.check_node = fake_check
        usable, _, _ = self.mod.check_node_confirmed("x", "http://u/mcp", "w", was=False)
        self.assertFalse(usable, "恢复必须两次可用，一次复测失败就不能报恢复")
        self.assertEqual(calls["n"], 2)

    def test_recovery_two_usable_ok(self):
        self.mod.check_node = lambda n, u, a: (True, True, "可用")
        usable, _, _ = self.mod.check_node_confirmed("x", "http://u/mcp", "w", was=False)
        self.assertTrue(usable)

    def test_ping_timeout_characterized_by_exec(self):
        # ping 子进程超时 → exec 腿定性：exec 报 401 = 仅可达；exec 成功 = 可用。
        import subprocess as _sp

        class Completed:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def run_ping_timeout(args, **kw):
            if "ping" in args:
                raise _sp.TimeoutExpired(args, 8)
            if "call" in args:
                return Completed(1, "exec 失败：HTTP 401")
            raise AssertionError("unexpected call %r" % (args,))

        orig_sub = self.mod.subprocess
        stub = type("Stub", (), {"run": staticmethod(run_ping_timeout),
                                 "TimeoutExpired": _sp.TimeoutExpired})()
        self.mod.subprocess = stub
        try:
            usable, reachable, detail = self.mod.check_node("x", "http://u/mcp", "w")
            self.assertFalse(usable)
            self.assertTrue(reachable, detail)
            self.assertIn("仅可达", detail)
        finally:
            self.mod.subprocess = orig_sub

        def run_ping_timeout_exec_ok(args, **kw):
            if "ping" in args:
                raise _sp.TimeoutExpired(args, 8)
            if "call" in args:
                return Completed(0, "FLEET_WATCH_OK")
            raise AssertionError("unexpected call %r" % (args,))

        stub2 = type("Stub", (), {"run": staticmethod(run_ping_timeout_exec_ok),
                                  "TimeoutExpired": _sp.TimeoutExpired})()
        self.mod.subprocess = stub2
        try:
            usable, reachable, _ = self.mod.check_node("x", "http://u/mcp", "w")
            self.assertTrue(usable)
            self.assertTrue(reachable)
        finally:
            self.mod.subprocess = orig_sub


class WatchSubprocessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake_fleet.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE_FLEET)
        os.chmod(self.fake, 0o755)
        self.ctl = os.path.join(self.tmp.name, "ctl.json")
        self.state = os.path.join(self.tmp.name, "watch_state.json")
        self.lock = os.path.join(self.tmp.name, "watch.lock")
        self.env = dict(os.environ)
        self.env.update({"FAKE_FLEET_CTL": self.ctl,
                         "FLEET_WATCH_STATE": self.state,
                         "FLEET_WATCH_LOCK": self.lock,
                         "FLEET_WATCH_FLEET": self.fake})

    def tearDown(self):
        self.tmp.cleanup()

    def _write_ctl(self, obj):
        with open(self.ctl, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def _run_watch(self):
        return subprocess.run([sys.executable, GOAL_WATCH],
                              capture_output=True, text=True, timeout=120, env=self.env)

    def test_full_run_and_silence(self):
        self._write_ctl({"nodes": {"n1": {"url": "http://127.0.0.1:3100/mcp"}},
                         "ping": {"n1": 1}, "exec": {}})
        r = self._run_watch()
        self.assertEqual(r.returncode, 1)  # 首轮新节点变化
        self.assertIn("新节点", r.stdout)
        self.assertTrue(os.path.isfile(self.state))
        r = self._run_watch()
        self.assertEqual(r.returncode, 0)  # 无变化静默
        self.assertEqual(r.stdout.strip(), "")

    def test_recovery_reported(self):
        self._write_ctl({"nodes": {"n1": {"url": "http://127.0.0.1:3100/mcp"}},
                         "ping": {"n1": 1}, "exec": {}})
        self._run_watch()
        self._write_ctl({"nodes": {"n1": {"url": "http://127.0.0.1:3100/mcp"}},
                         "ping": {"n1": 0}, "exec": {"n1": {"ok": True}}})
        r = self._run_watch()
        self.assertIn("恢复 n1", r.stdout)

    def test_flock_second_run_silent(self):
        self._write_ctl({"nodes": {"n1": {"url": "http://127.0.0.1:3100/mcp"}},
                         "ping": {"n1": 1}, "exec": {}})
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self._run_watch()
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")
            self.assertFalse(os.path.exists(self.state), "持锁时本轮不应写状态")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
