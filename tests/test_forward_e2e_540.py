#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.4 端到端：`fleet forward` 起真进程、真 socket，不打桩。

为什么值得一个慢测试：单元测试只能证明「函数被调用了」，证明不了
「副端重启后转发会自己恢复」。而「长时间在线」这条需求，用户感知到的恰恰是
后者——网络抖一下、服务重启一次，转发就静默失效，比一开始就不工作更糟。

这里覆盖两件事：
  1. -L 能把字节原样送过去（真 HTTP 往返）
  2. -R 在副端重启后能重建 listener 并继续工作（自愈）
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
PY = sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_port(port, timeout=25.0):
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


def _http_get(port, timeout=15.0):
    """极简 HTTP GET，返回响应体（不用 curl：环境里可能有代理变量干扰）。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        buf = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
            if len(buf) > 4096:
                break
        return buf.decode("utf-8", "ignore")
    finally:
        s.close()


class _Procs(object):
    def __init__(self):
        self.items = []

    def spawn(self, args, cwd=None):
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.items.append(p)
        return p

    def kill_all(self):
        for p in self.items:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self.items:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.items = []


class ForwardE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 一个目标 HTTP 服务：用 Python 自带的 http.server
        cls.web_port = _free_port()
        cls.web = subprocess.Popen(
            [PY, "-u", "-m", "http.server", str(cls.web_port),
             "--bind", "127.0.0.1", "--directory", HERE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_port(cls.web_port, 15):
            cls.web.kill()
            raise unittest.SkipTest("目标 HTTP 服务没起来")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.web.terminate()
            cls.web.wait(timeout=5)
        except Exception:
            try:
                cls.web.kill()
            except Exception:
                pass

    def setUp(self):
        self.procs = _Procs()
        self.agent_port = _free_port()

    def tearDown(self):
        self.procs.kill_all()

    # -- helpers -------------------------------------------------
    def _start_agent(self):
        p = self.procs.spawn(
            [PY, "-u", "mcp_agent.py", "--host", "127.0.0.1",
             "--port", str(self.agent_port), "--no-auto-secure", "--allow-insecure"],
            cwd=SCRIPTS)
        if not _wait_port(self.agent_port, 30):
            self.skipTest("副端没起来（端口被占或环境不允许）")
        return p

    def _start_forward(self, *fwd_args):
        p = self.procs.spawn(
            [PY, "-u", "fleet.py", "forward"] + list(fwd_args) +
            ["http://127.0.0.1:%d/mcp" % self.agent_port],
            cwd=SCRIPTS)
        return p

    def _body_ok(self, port):
        try:
            body = _http_get(port)
        except Exception:
            return False
        return "200" in body.split("\r\n")[0] or "test_forward_e2e" in body

    # -- cases ---------------------------------------------------
    def test_local_forward_serves_target(self):
        self._start_agent()
        lport = _free_port()
        self._start_forward("-L", "%d:127.0.0.1:%d" % (lport, self.web_port))
        self.assertTrue(_wait_port(lport, 20), "本地监听没起来")
        # 第一次请求可能撞上启动窗口，允许重试几次
        ok = False
        for _ in range(8):
            if self._body_ok(lport):
                ok = True
                break
            time.sleep(0.5)
        self.assertTrue(ok, "经 -L 没取到目标内容")

    def test_remote_forward_recovers_after_agent_restart(self):
        """副端重启 → listener 内存态没了 → 转发器必须自己重建，而不是静默失效。"""
        agent = self._start_agent()
        rport = _free_port()
        self._start_forward("-R", "%d:127.0.0.1:%d" % (rport, self.web_port))
        self.assertTrue(_wait_port(rport, 25), "副端监听没起来")

        ok = False
        for _ in range(8):
            if self._body_ok(rport):
                ok = True
                break
            time.sleep(0.5)
        self.assertTrue(ok, "重启前就取不到内容，前置条件不成立")

        # 杀掉副端，等它真的退出
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except Exception:
            agent.kill()

        # 重启副端（同一个端口）
        p = self.procs.spawn(
            [PY, "-u", "mcp_agent.py", "--host", "127.0.0.1",
             "--port", str(self.agent_port), "--no-auto-secure", "--allow-insecure"],
            cwd=SCRIPTS)
        self.assertTrue(_wait_port(self.agent_port, 30), "副端重启失败")

        # 转发器应当在一段时间内自己重建 listener
        recovered = False
        deadline = time.time() + 45
        while time.time() < deadline:
            if self._body_ok(rport):
                recovered = True
                break
            time.sleep(1.0)
        self.assertTrue(recovered, "副端重启后 -R 转发没有自动恢复")
        self.assertIsNone(p.poll(), "副端进程意外退出")


if __name__ == "__main__":
    unittest.main()
