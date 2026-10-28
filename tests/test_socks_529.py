#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.9 `-D`：本地 SOCKS5，从副端出网（ssh -D 的等价物）。

这是「替代 ssh」的最后一截传输能力。它和 -L 的区别只有一句话：
**目标谁定**。`-L` 在命令行上把 `host:port` 写死；`-D` 让客户端每次连的时候说。
所以底层不需要任何新协议——复用 tunnel_open/read/write，副端不用开新端口。

测试重点（SOCKS5 的坑都在协议边界上）：
  * 三种地址类型（IPv4 / 域名 / IPv6）都要认，域名必须**原样交给副端解析**
    （这才叫「借用那台设备的网络环境」，本机解析等于没借）；
  * 客户端不支持「无认证」时要回 `0xFF` 并断开，不能沉默挂住；
  * 非 CONNECT 命令回 `0x07`、未知地址类型回 `0x08`，让客户端立刻报错；
  * 握手阶段给 socket 设过超时，**泵起来必须恢复阻塞**，否则一条安静的
    长连接（SSE / 长轮询）30 秒后会被自己掐断；
  * 端到端：真起副端 + 真 SOCKS 客户端 + 真 HTTP 往返。
"""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
PY = sys.executable


# ------------------------------------------------------------------ 监听规格

class ParseSocksBindTest(unittest.TestCase):

    def test_port_only(self):
        self.assertEqual(fleet._parse_socks_bind("1080", "127.0.0.1"),
                         {"bind": "127.0.0.1", "port": 1080})

    def test_bind_and_port(self):
        self.assertEqual(fleet._parse_socks_bind("0.0.0.0:1080", "127.0.0.1"),
                         {"bind": "0.0.0.0", "port": 1080})

    def test_empty_refused(self):
        self.assertRaises(ValueError, fleet._parse_socks_bind, "", "127.0.0.1")

    def test_not_a_number(self):
        with self.assertRaises(ValueError) as cm:
            fleet._parse_socks_bind("http", "127.0.0.1")
        self.assertIn("不是数字", str(cm.exception))

    def test_out_of_range(self):
        self.assertRaises(ValueError, fleet._parse_socks_bind, "70000", "127.0.0.1")
        self.assertRaises(ValueError, fleet._parse_socks_bind, "0", "127.0.0.1")

    def test_too_many_parts(self):
        self.assertRaises(ValueError, fleet._parse_socks_bind,
                          "127.0.0.1:1080:extra", "127.0.0.1")


# ------------------------------------------------------------------ 协议

def _pair():
    """一对已连通的 socket：a 当客户端，b 当服务端。"""
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    return a, b


class Socks5GreetTest(unittest.TestCase):
    """`_socks5_greet` 的协议边界。"""

    def _greet(self, payload, expect_reply=None):
        """把 payload 从客户端发过去，返回 (结果, 原因, 服务端回了什么)。"""
        cli, srv = _pair()
        got = {}

        def run():
            got["r"] = fleet._socks5_greet(srv, timeout=3)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        cli.sendall(payload)
        # 客户端这侧按需读回显：先读方法选择，再读最终应答
        replies = b""
        cli.settimeout(2)
        try:
            while len(replies) < (expect_reply or 0):
                d = cli.recv(64)
                if not d:
                    break
                replies += d
        except Exception:
            pass
        t.join(timeout=5)
        cli.close()
        try:
            srv.close()
        except Exception:
            pass
        return got.get("r"), replies

    def test_ipv4_connect(self):
        payload = (b"\x05\x01\x00"                       # 版本, 1 个方法, 无认证
                   + b"\x05\x01\x00\x01"                 # CONNECT, RSV, ATYP=IPv4
                   + socket.inet_aton("10.1.2.3")
                   + struct.pack("!H", 8080))
        (res, why), replies = self._greet(payload, expect_reply=12)
        self.assertEqual(res, ("10.1.2.3", 8080), why)
        self.assertEqual(replies[:2], b"\x05\x00")       # 选了无认证

    def test_domain_connect(self):
        host = b"db.internal"
        payload = (b"\x05\x01\x00"
                   + b"\x05\x01\x00\x03" + bytes(bytearray([len(host)])) + host
                   + struct.pack("!H", 3306))
        (res, why), _ = self._greet(payload, expect_reply=12)
        self.assertEqual(res, ("db.internal", 3306), why)

    def test_ipv6_connect(self):
        payload = (b"\x05\x01\x00"
                   + b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, "::1")
                   + struct.pack("!H", 443))
        (res, why), _ = self._greet(payload, expect_reply=12)
        self.assertEqual(res, ("::1", 443), why)

    def test_not_socks5(self):
        (res, why), replies = self._greet(b"GET / HTTP/1.0\r\n\r\n")
        self.assertIsNone(res)
        self.assertIn("不是 SOCKS5", why)
        self.assertEqual(replies, b"")        # 别对着 HTTP 客户端乱回字节

    def test_no_acceptable_method(self):
        # 客户端只支持「用户名密码」，我们只做无认证
        (res, why), replies = self._greet(b"\x05\x01\x02", expect_reply=2)
        self.assertIsNone(res)
        self.assertIn("无认证", why)
        self.assertEqual(replies[:2], b"\x05\xff")

    def test_bind_command_refused(self):
        payload = (b"\x05\x01\x00"
                   + b"\x05\x02\x00\x01" + b"\x00\x00\x00\x00" + b"\x00\x50")
        (res, why), replies = self._greet(payload, expect_reply=12)
        self.assertIsNone(res)
        self.assertIn("CONNECT", why)
        # replies = 方法选择(2 字节) + 失败应答(10 字节)
        self.assertEqual(replies[2:4], b"\x05\x07")   # Command not supported

    def test_unknown_atyp(self):
        payload = (b"\x05\x01\x00"
                   + b"\x05\x01\x00\x09" + b"\x00" * 4 + b"\x00\x50")
        (res, why), replies = self._greet(payload, expect_reply=12)
        self.assertIsNone(res)
        self.assertIn("地址类型", why)
        self.assertEqual(replies[2:4], b"\x05\x08")   # Address type not supported

    def test_truncated_request(self):
        # 只发到一半就断开：必须干净失败，不能卡到超时
        cli, srv = _pair()
        got = {}

        def run():
            got["r"] = fleet._socks5_greet(srv, timeout=3)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        cli.sendall(b"\x05\x01\x00\x05\x01\x00\x01\x0a\x01")
        time.sleep(0.1)
        cli.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "半截请求把服务端挂住了")
        res, why = got["r"]
        self.assertIsNone(res)
        self.assertTrue(why)
        try:
            srv.close()
        except Exception:
            pass

    def test_empty_target_rejected(self):
        payload = (b"\x05\x01\x00"
                   + b"\x05\x01\x00\x01" + b"\x00\x00\x00\x00"
                   + struct.pack("!H", 0))
        (res, why), _ = self._greet(payload, expect_reply=12)
        self.assertIsNone(res)
        self.assertIn("为空", why)


class Socks5ReplyTest(unittest.TestCase):

    def test_reply_format(self):
        cli, srv = _pair()
        fleet._socks5_reply(srv, 0)
        data = cli.recv(64)
        self.assertEqual(data[:2], b"\x05\x00")
        self.assertEqual(data[3], 1)          # ATYP=IPv4
        self.assertEqual(len(data), 10)       # 4 头 + 4 地址 + 2 端口
        cli.close()
        srv.close()

    def test_reply_on_closed_socket_is_silent(self):
        cli, srv = _pair()
        cli.close()
        srv.close()
        fleet._socks5_reply(srv, 5)           # 不能抛


class RecvNTest(unittest.TestCase):

    def test_exact(self):
        cli, srv = _pair()
        cli.sendall(b"abcdef")
        self.assertEqual(fleet._recv_n(srv, 3), b"abc")
        self.assertEqual(fleet._recv_n(srv, 3), b"def")
        cli.close()
        srv.close()

    def test_short_read_returns_partial(self):
        cli, srv = _pair()
        cli.sendall(b"xy")
        cli.close()
        self.assertEqual(fleet._recv_n(srv, 10), b"xy")
        srv.close()


# ------------------------------------------------------------------ 端到端

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


def _socks5_http_get(socks_port, host, port, path="/", timeout=20.0,
                     atyp_domain=True):
    """手写一个 SOCKS5 客户端 + HTTP GET（不依赖 curl 是否带 socks 支持）。"""
    s = socket.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00", "服务端没选无认证"
        if atyp_domain:
            hb = host.encode("ascii")
            req = (b"\x05\x01\x00\x03" + bytes(bytearray([len(hb)])) + hb)
        else:
            req = b"\x05\x01\x00\x01" + socket.inet_aton(host)
        req += struct.pack("!H", port)
        s.sendall(req)
        rep = b""
        while len(rep) < 4:
            d = s.recv(4 - len(rep))
            if not d:
                raise AssertionError("SOCKS 应答被截断")
            rep += d
        assert rep[0] == 5
        if rep[1] != 0:
            raise AssertionError("SOCKS 拒绝，code=%d" % rep[1])
        # 吃掉 BND.ADDR
        if rep[3] == 1:
            need = 4 + 2
        elif rep[3] == 4:
            need = 16 + 2
        else:
            n = s.recv(1)
            need = bytearray(n)[0] + 2
        got = b""
        while len(got) < need:
            d = s.recv(need - len(got))
            if not d:
                break
            got += d
        s.sendall(("GET %s HTTP/1.0\r\nHost: %s\r\n\r\n"
                   % (path, host)).encode("ascii"))
        buf = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
            if len(buf) > 8192:
                break
        return buf.decode("utf-8", "ignore")
    finally:
        try:
            s.close()
        except Exception:
            pass


class SocksE2ETest(unittest.TestCase):
    """真副端 + 真 SOCKS5 客户端 + 真 HTTP 往返。"""

    @classmethod
    def setUpClass(cls):
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
        self.procs = []
        self.agent_port = _free_port()
        self.socks_port = _free_port()

    def tearDown(self):
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _start_agent(self):
        p = subprocess.Popen(
            [PY, "-u", "mcp_agent.py", "--host", "127.0.0.1",
             "--port", str(self.agent_port), "--no-auto-secure",
             "--allow-insecure"],
            cwd=SCRIPTS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        if not _wait_port(self.agent_port, 30):
            self.skipTest("副端没起来（端口被占或环境不允许）")

    def _start_forward(self, *args):
        p = subprocess.Popen(
            [PY, "-u", "fleet.py", "forward"] + list(args)
            + ["http://127.0.0.1:%d/mcp" % self.agent_port],
            cwd=SCRIPTS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def test_socks_proxies_http(self):
        """经 SOCKS5 CONNECT 到目标 HTTP 服务，拿回 200。"""
        self._start_agent()
        self._start_forward("-D", str(self.socks_port))
        self.assertTrue(_wait_port(self.socks_port, 25), "SOCKS 端口没起来")

        body = ""
        for _ in range(10):              # 第一次可能撞上启动窗口
            try:
                body = _socks5_http_get(self.socks_port, "127.0.0.1",
                                        self.web_port, "/", atyp_domain=False)
                if "200" in body.split("\r\n")[0]:
                    break
            except Exception:
                pass
            time.sleep(0.6)
        self.assertIn("200", body.split("\r\n")[0], "经 SOCKS5 没取到目标内容")

    def test_socks_accepts_domain_atyp(self):
        """域名由副端解析：ATYP=3 这条路径必须通（-L 没有这条）。"""
        self._start_agent()
        self._start_forward("-D", str(self.socks_port))
        self.assertTrue(_wait_port(self.socks_port, 25), "SOCKS 端口没起来")

        body = ""
        for _ in range(10):
            try:
                body = _socks5_http_get(self.socks_port, "localhost",
                                        self.web_port, "/", atyp_domain=True)
                if "200" in body.split("\r\n")[0]:
                    break
            except Exception:
                pass
            time.sleep(0.6)
        self.assertIn("200", body.split("\r\n")[0])

    def test_socks_refuses_unreachable_target(self):
        """目标连不上时，客户端要收到明确的失败码，而不是干等。"""
        self._start_agent()
        self._start_forward("-D", str(self.socks_port))
        self.assertTrue(_wait_port(self.socks_port, 25), "SOCKS 端口没起来")
        time.sleep(0.5)

        dead = _free_port()
        with self.assertRaises(AssertionError) as cm:
            _socks5_http_get(self.socks_port, "127.0.0.1", dead, "/",
                             atyp_domain=False)
        self.assertIn("code=", str(cm.exception))


class WiringTest(unittest.TestCase):

    def test_dynamic_flag_registered(self):
        ns = fleet.build_parser().parse_args(
            ["forward", "-D", "1080", "nas"])
        self.assertEqual(ns.dynamic, ["1080"])
        self.assertEqual(ns.target, "nas")

    def test_multiple_dynamic(self):
        ns = fleet.build_parser().parse_args(
            ["forward", "-D", "1080", "-D", "127.0.0.1:1081", "nas"])
        self.assertEqual(len(ns.dynamic), 2)

    def test_source_mentions_both_paths(self):
        src = open(os.path.join(SCRIPTS, "fleet.py"), encoding="utf-8").read()
        self.assertIn("serve_dynamic", src)
        self.assertIn("_socks5_greet", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
