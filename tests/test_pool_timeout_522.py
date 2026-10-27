#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接池的 per-call 超时必须真的生效。

`_PooledHTTP.post(..., timeout=N)` 原来只写了 `conn.timeout = N`——这是空操作。
`http.client` 只在 `connect()` 时用 `self.timeout` 建 socket，之后 `send()` /
`getresponse()` 都不会再应用它。结果**所有请求实际都用建连时那个超时**：
`fleet cp --timeout 600`、长轮询的长超时全部静默失效，慢链路上表现为
「莫名其妙超时」，而报错完全指不到这里。

顺带钉住另一个同类坑：`socket.timeout` 是 `OSError` 的子类，
`except (ConnectionError, OSError)` 若写在 `except socket.timeout` 前面，
超时分支就是死代码——所有超时都会被报成「连不上」，把人引去查网络/DNS。
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class _SlowServer(object):
    """一个故意慢的服务端，并记录被连了几次（用来判断有没有重试）。"""

    def __init__(self, delay=1.5, body=b'{"ok":1}'):
        self.delay = delay
        self.body = body
        self.accepted = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except Exception:
                return
            self.accepted += 1
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        try:
            c.recv(65536)
            time.sleep(self.delay)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(self.body)
                      + self.body)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass

    @property
    def url(self):
        return "http://127.0.0.1:%d/mcp" % self.port

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass


class PoolTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.srv = _SlowServer()

    def tearDown(self):
        self.srv.close()

    def test_per_call_timeout_actually_applies(self):
        """池的超时是 30s，单次调用要 0.4s——必须按 0.4s 断。"""
        pool = fleet._PooledHTTP(self.srv.url, timeout=30)
        t0 = time.time()
        with self.assertRaises(socket.timeout):
            pool.post(b"{}", {}, timeout=0.4)
        spent = time.time() - t0
        self.assertLess(spent, 1.2,
                        "per-call 超时没生效，等了 %.2fs（池超时是 30s）" % spent)
        pool.close()

    def test_timeout_is_not_retried(self):
        """超时不重试：重试只会把等待时间翻倍。"""
        pool = fleet._PooledHTTP(self.srv.url, timeout=30)
        with self.assertRaises(socket.timeout):
            pool.post(b"{}", {}, timeout=0.4)
        time.sleep(0.3)
        self.assertEqual(self.srv.accepted, 1,
                         "超时被重试了（服务端收到 %d 次连接）" % self.srv.accepted)
        pool.close()

    def test_pool_timeout_used_when_call_omits_it(self):
        """不传 timeout 时用池的默认值。"""
        pool = fleet._PooledHTTP(self.srv.url, timeout=0.4)
        t0 = time.time()
        with self.assertRaises(socket.timeout):
            pool.post(b"{}", {})
        self.assertLess(time.time() - t0, 1.2)
        pool.close()

    def test_slow_but_within_timeout_succeeds(self):
        """超时给够了就必须成功——别把「慢」误判成「连不上」。"""
        pool = fleet._PooledHTTP(self.srv.url, timeout=30)
        status, _hdrs, data = pool.post(b"{}", {}, timeout=10)
        self.assertEqual(status, 200)
        self.assertIn(b"ok", data)
        pool.close()


class TimeoutMessageTest(unittest.TestCase):
    """超时要报「超时」，不能报「连不上」。"""

    def setUp(self):
        self.srv = _SlowServer(delay=1.5)

    def tearDown(self):
        self.srv.close()

    def test_reported_as_timeout_not_unreachable(self):
        c = fleet.MCPClient(self.srv.url, timeout=30)
        try:
            with self.assertRaises(fleet.MCPError) as ctx:
                c.call("tools/list", {}, timeout=0.4)
        finally:
            c.close()
        msg = str(ctx.exception)
        self.assertIn("超时", msg, "超时被误报成：%s" % msg)
        self.assertNotIn("连不上", msg, "超时被误报成「连不上」，会把排查引向网络/DNS")


class ExceptionOrderTest(unittest.TestCase):
    """结构断言：`except socket.timeout` 必须排在 OSError 之前。

    `socket.timeout` 就是 `OSError` 的子类，顺序反了分支永远进不来。
    这种 bug 不会报错、不会崩，只会让错误信息一直错下去。
    """

    def test_timeout_before_oserror(self):
        import inspect
        for fn in (fleet._PooledHTTP.post, fleet.MCPClient._post_locked):
            src = inspect.getsource(fn)
            lines = [ln.strip() for ln in src.splitlines()
                     if ln.strip().startswith("except ")]
            i_timeout = next((i for i, ln in enumerate(lines) if "socket.timeout" in ln), None)
            i_os = next((i for i, ln in enumerate(lines)
                         if "OSError" in ln and "socket.timeout" not in ln), None)
            if i_timeout is None or i_os is None:
                continue
            self.assertLess(i_timeout, i_os,
                            "%s 里 `except socket.timeout` 排在 OSError 之后，是死代码"
                            % fn.__qualname__)


if __name__ == "__main__":
    unittest.main()
