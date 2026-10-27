#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.4 端口转发：ssh -L / -R 的等价物。

这是「fleet 替代 ssh」的最后一块拼图。测试分两层：
  * 解析层（_parse_fwd）—— 规格写错时要给出能照着改的提示，而不是栈回溯
  * 数据层（agent 侧 tunnel_*）—— 真实 socket，重点验证**不丢字节**

数据完整性是这里最容易出错的地方：请求/响应传输里，远端一旦发得比主端取得快，
缓冲区就会涨。按「环形缓冲丢旧数据」处理的话，终端滚屏只是少几行，TCP 流却是
文件损坏。所以这里的用例专门用二进制随机数据 + SHA 比对来钉住这一点。
"""
import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent
import fleet as fleet_cli


# ---------------------------------------------------------------------------
# 辅助：一个回显服务器 / 一个数据源服务器
# ---------------------------------------------------------------------------
class _Server(object):
    """极简 TCP 服务：收到什么原样吐回来，或先吐一段固定数据。"""

    def __init__(self, mode="echo", payload=b""):
        self.mode = mode
        self.payload = payload
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except Exception:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            if self.mode == "push":
                conn.sendall(self.payload)
                time.sleep(0.05)
                return
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                conn.sendall(data)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self.stop.set()
        try:
            self.srv.close()
        except Exception:
            pass


def _open(host="127.0.0.1", port=0, **kw):
    args = {"host": host, "port": port}
    args.update(kw)
    text, is_err = mcp_agent.t_tunnel_open(args)
    return text, is_err


def _drain(tid, want, timeout=15.0):
    """一直读到凑够 want 字节或超时，返回累计字节。"""
    off, got = 0, b""
    deadline = time.time() + timeout
    while len(got) < want and time.time() < deadline:
        text, is_err = mcp_agent.t_tunnel_read(
            {"tunnel_id": tid, "offset": off, "wait": 0.2})
        assert not is_err, text
        d = json.loads(text)
        off = d["offset"]
        got += base64.b64decode(d["data_b64"])
        if not d.get("alive", True) and not d["data_b64"]:
            break
    return got


# ---------------------------------------------------------------------------
# 规格解析
# ---------------------------------------------------------------------------
class ParseSpecTest(unittest.TestCase):
    def test_three_parts_uses_default_bind(self):
        f = fleet_cli._parse_fwd("8080:db.internal:3306", "127.0.0.1", True)
        self.assertEqual((f["bind"], f["port"], f["host"], f["hport"]),
                         ("127.0.0.1", 8080, "db.internal", 3306))

    def test_four_parts_overrides_bind(self):
        f = fleet_cli._parse_fwd("0.0.0.0:8080:10.0.0.5:5432", "127.0.0.1", True)
        self.assertEqual((f["bind"], f["port"], f["host"], f["hport"]),
                         ("0.0.0.0", 8080, "10.0.0.5", 5432))

    def test_empty_host_defaults_to_loopback(self):
        f = fleet_cli._parse_fwd("9000::3000", "127.0.0.1", False)
        self.assertEqual(f["host"], "127.0.0.1")
        self.assertEqual(f["hport"], 3000)

    def test_bad_shape_explains_both_forms(self):
        with self.assertRaises(ValueError) as cm:
            fleet_cli._parse_fwd("abc", "127.0.0.1", True)
        msg = str(cm.exception)
        self.assertIn("看不懂的转发规格", msg)
        self.assertIn("端口>:<目标主机>:<目标端口", msg)

    def test_port_out_of_range(self):
        with self.assertRaises(ValueError):
            fleet_cli._parse_fwd("70000:host:80", "127.0.0.1", True)
        with self.assertRaises(ValueError):
            fleet_cli._parse_fwd("0:host:80", "127.0.0.1", True)

    def test_non_numeric_port(self):
        with self.assertRaises(ValueError) as cm:
            fleet_cli._parse_fwd("abc:host:80", "127.0.0.1", True)
        self.assertIn("端口不是数字", str(cm.exception))

    def test_ipv6_bind_rejected_with_hint(self):
        with self.assertRaises(ValueError) as cm:
            fleet_cli._parse_fwd("::1:8080:host:80", "127.0.0.1", True)
        self.assertIn("IPv6", str(cm.exception))


# ---------------------------------------------------------------------------
# 连接语义
# ---------------------------------------------------------------------------
class TunnelConnTest(unittest.TestCase):
    def setUp(self):
        self.srv = _Server("echo")

    def tearDown(self):
        self.srv.close()
        for tid in list(mcp_agent.TUNNELS.keys()):
            mcp_agent._tun_kill(tid)

    def test_open_echo_roundtrip(self):
        text, is_err = _open(port=self.srv.port)
        self.assertFalse(is_err, text)
        d = json.loads(text)
        self.assertEqual(d["kind"], "conn")
        tid = d["tunnel_id"]

        ok, is_err = mcp_agent.t_tunnel_write(
            {"tunnel_id": tid,
             "data_b64": base64.b64encode(b"ping-42").decode()})
        self.assertFalse(is_err, ok)
        self.assertEqual(_drain(tid, 7), b"ping-42")
        mcp_agent.t_tunnel_close({"tunnel_id": tid})

    def test_open_unreachable_reports_target_from_agent_view(self):
        text, is_err = _open(port=1, timeoutMs=1500)
        self.assertTrue(is_err)
        self.assertIn("副端连不上", text)
        # 提示必须点明 host 是「副端视角」——这是最常见的误配
        self.assertIn("副端", text)

    def test_write_after_close_is_error_not_crash(self):
        text, _ = _open(port=self.srv.port)
        tid = json.loads(text)["tunnel_id"]
        mcp_agent.t_tunnel_close({"tunnel_id": tid})
        msg, is_err = mcp_agent.t_tunnel_write(
            {"tunnel_id": tid, "data_b64": base64.b64encode(b"x").decode()})
        self.assertTrue(is_err)
        self.assertIn("未知", msg)

    def test_close_is_idempotent(self):
        text, _ = _open(port=self.srv.port)
        tid = json.loads(text)["tunnel_id"]
        self.assertFalse(mcp_agent.t_tunnel_close({"tunnel_id": tid})[1])
        # 第二次关闭：不存在也算成功，脚本不用先查后关
        self.assertFalse(mcp_agent.t_tunnel_close({"tunnel_id": tid})[1])

    def test_unknown_id_returns_actionable_message(self):
        text, is_err = mcp_agent.t_tunnel_read({"tunnel_id": "tn-nope"})
        self.assertTrue(is_err)
        self.assertIn("未知", text)

    def test_empty_write_rejected(self):
        text, _ = _open(port=self.srv.port)
        tid = json.loads(text)["tunnel_id"]
        msg, is_err = mcp_agent.t_tunnel_write({"tunnel_id": tid, "data_b64": ""})
        self.assertTrue(is_err)
        self.assertIn("没有数据", msg)

    def test_remote_eof_shows_as_not_alive(self):
        srv = _Server("push", b"BYE")
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            got = _drain(tid, 3)
            self.assertEqual(got, b"BYE")
            # 再读一次应当拿到 alive=False，客户端据此收尾。
            # 必须带 wait：对端 close 和读线程察觉 EOF 之间有极短的窗口。
            text, _ = mcp_agent.t_tunnel_read(
                {"tunnel_id": tid, "offset": 3, "wait": 1.0})
            d = json.loads(text)
            self.assertFalse(d["alive"])
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# 数据完整性 / 背压
# ---------------------------------------------------------------------------
class IntegrityTest(unittest.TestCase):
    """二进制不丢字节是隧道和终端的分水岭，必须钉死。"""

    def tearDown(self):
        for tid in list(mcp_agent.TUNNELS.keys()):
            mcp_agent._tun_kill(tid)

    def test_one_megabyte_binary_intact(self):
        payload = os.urandom(1024 * 1024)
        srv = _Server("push", payload)
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            got = _drain(tid, len(payload), timeout=40)
            self.assertEqual(len(got), len(payload))
            self.assertEqual(hashlib.sha256(got).hexdigest(),
                             hashlib.sha256(payload).hexdigest())
        finally:
            srv.close()

    def test_backpressure_does_not_drop_when_buffer_small(self):
        """把缓冲区压到 8KB，逼出「读得比取得快」的场景。

        按环形缓冲丢旧数据实现的话，这里一定会少字节；正确实现是读线程挂起等待。
        """
        payload = os.urandom(200 * 1024)
        srv = _Server("push", payload)
        old = mcp_agent.TUN_BUF_MAX
        mcp_agent.TUN_BUF_MAX = 8 * 1024
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            got = _drain(tid, len(payload), timeout=40)
            self.assertEqual(hashlib.sha256(got).hexdigest(),
                             hashlib.sha256(payload).hexdigest())
        finally:
            mcp_agent.TUN_BUF_MAX = old
            srv.close()

    def test_consumed_prefix_is_reclaimed(self):
        """取走的数据必须从缓冲区丢掉，否则读线程会永久卡在背压上。"""
        payload = os.urandom(600 * 1024)
        srv = _Server("push", payload)
        old = mcp_agent.TUN_BUF_MAX
        mcp_agent.TUN_BUF_MAX = 64 * 1024
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            _drain(tid, len(payload), timeout=40)
            sess = mcp_agent.TUNNELS[tid]
            with sess["lock"]:
                # 全部取完后缓冲区不该还攒着历史数据
                self.assertLessEqual(len(sess["buf"]), mcp_agent.TUN_BUF_MAX)
                self.assertGreater(sess["base"], 0)
        finally:
            mcp_agent.TUN_BUF_MAX = old
            srv.close()

    def test_large_upload_is_not_corrupted(self):
        """上行方向同样要逐字节一致。"""
        payload = os.urandom(300 * 1024)
        srv = _Server("echo")
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            for i in range(0, len(payload), 60000):
                chunk = payload[i:i + 60000]
                ok, is_err = mcp_agent.t_tunnel_write(
                    {"tunnel_id": tid,
                     "data_b64": base64.b64encode(chunk).decode()})
                self.assertFalse(is_err, ok)
            got = _drain(tid, len(payload), timeout=40)
            self.assertEqual(hashlib.sha256(got).hexdigest(),
                             hashlib.sha256(payload).hexdigest())
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# 监听 / -R 的远端半截
# ---------------------------------------------------------------------------
class ListenTest(unittest.TestCase):
    def tearDown(self):
        for tid in list(mcp_agent.TUNNELS.keys()):
            mcp_agent._tun_kill(tid)

    def test_listen_accept_yields_conn(self):
        text, is_err = mcp_agent.t_tunnel_listen({"host": "127.0.0.1", "port": 0})
        self.assertFalse(is_err, text)
        d = json.loads(text)
        lid = d["tunnel_id"]
        self.assertEqual(d["kind"], "listen")
        port = int(d["bound"].rsplit(":", 1)[1])

        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(b"hi")
            text, is_err = mcp_agent.t_tunnel_accept(
                {"tunnel_id": lid, "wait": 1.0})
            self.assertFalse(is_err, text)
            ids = json.loads(text)["tunnels"]
            self.assertEqual(len(ids), 1)
            got = _drain(ids[0], 2)
            self.assertEqual(got, b"hi")
        finally:
            s.close()
            mcp_agent.t_tunnel_close({"tunnel_id": lid})

    def test_read_on_listener_is_rejected(self):
        text, _ = mcp_agent.t_tunnel_listen({"host": "127.0.0.1", "port": 0})
        lid = json.loads(text)["tunnel_id"]
        msg, is_err = mcp_agent.t_tunnel_read({"tunnel_id": lid})
        self.assertTrue(is_err)
        self.assertIn("listener", msg)

    def test_accept_on_conn_is_rejected(self):
        srv = _Server("echo")
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            msg, is_err = mcp_agent.t_tunnel_accept({"tunnel_id": tid})
            self.assertTrue(is_err)
            self.assertIn("不是 listener", msg)
        finally:
            srv.close()

    def test_bind_failure_reports_port(self):
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            msg, is_err = mcp_agent.t_tunnel_listen(
                {"host": "127.0.0.1", "port": port})
            self.assertTrue(is_err)
            self.assertIn(str(port), msg)
        finally:
            blocker.close()


# ---------------------------------------------------------------------------
# 上限 / 回收 / 清单
# ---------------------------------------------------------------------------
class GuardTest(unittest.TestCase):
    def tearDown(self):
        for tid in list(mcp_agent.TUNNELS.keys()):
            mcp_agent._tun_kill(tid)

    def test_list_empty(self):
        for tid in list(mcp_agent.TUNNELS.keys()):
            mcp_agent._tun_kill(tid)
        text, is_err = mcp_agent.t_tunnel_list({})
        self.assertFalse(is_err)
        self.assertIn("没有隧道", text)

    def test_list_shows_conn_and_listen(self):
        srv = _Server("echo")
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            text, _ = mcp_agent.t_tunnel_list({})
            self.assertIn(tid, text)
            self.assertIn("连接", text)
        finally:
            srv.close()

    def test_max_tunnels_guard(self):
        srv = _Server("echo")
        old = mcp_agent.TUN_MAX
        mcp_agent.TUN_MAX = 1
        try:
            text, _ = _open(port=srv.port)
            self.assertNotIn("上限", text)
            text2, is_err = _open(port=srv.port)
            self.assertTrue(is_err)
            self.assertIn("上限", text2)
        finally:
            mcp_agent.TUN_MAX = old
            srv.close()

    def test_idle_tunnel_is_swept(self):
        srv = _Server("echo")
        old = mcp_agent.TUN_IDLE
        mcp_agent.TUN_IDLE = 0.05
        try:
            text, _ = _open(port=srv.port)
            tid = json.loads(text)["tunnel_id"]
            self.assertIn(tid, mcp_agent.TUNNELS)
            time.sleep(0.2)
            mcp_agent._tun_sweep()
            self.assertNotIn(tid, mcp_agent.TUNNELS)
        finally:
            mcp_agent.TUN_IDLE = old
            srv.close()

    def test_dangerous_tools_include_tunnel(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                                "mcp_agent.py"), encoding="utf-8").read()
        for name in ("tunnel_open", "tunnel_listen", "tunnel_write"):
            self.assertIn('"%s"' % name, src)


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
class RegistrationTest(unittest.TestCase):
    NAMES = ["tunnel_open", "tunnel_listen", "tunnel_accept",
             "tunnel_read", "tunnel_write", "tunnel_close", "tunnel_list"]

    def test_all_tunnel_tools_registered(self):
        names = [t[0] for t in mcp_agent.BASE_TOOLS]
        for n in self.NAMES:
            self.assertIn(n, names)
            self.assertIn(n, mcp_agent.BASE_FUNCS)

    def test_each_tool_has_schema(self):
        for t in mcp_agent.BASE_TOOLS:
            if t[0] in self.NAMES:
                self.assertEqual(t[2].get("type"), "object", t[0])

    def test_forward_subcommand_exists(self):
        """`tunnel` 已被 cloudflared 占用，端口转发必须挂在 `forward` 上。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                                "fleet.py"), encoding="utf-8").read()
        self.assertIn('sub.add_parser(\n        "forward"', src)
        self.assertIn("set_defaults(func=cmd_forward)", src)


if __name__ == "__main__":
    unittest.main()
