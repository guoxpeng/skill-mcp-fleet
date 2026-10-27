#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fleet shell` 必须把用户的终端还回去。

交互式 shell 会把本地 stdin 设成 raw 模式（`tty.setraw`）——不回显、不做行缓冲、
Ctrl-C 不当信号。正常退出走 `finally` 恢复，但**被 SIGTERM / SIGHUP 杀掉时
`finally` 根本不执行**（默认处置是直接终止进程）。

结果就是：kill 一下 fleet 进程，用户的终端永久停在 raw 模式——
回车不换行、Ctrl-C 只打印 ^C、连 `reset` 都得盲敲。ssh 也处理这一档。

这条用例起真 agent + 真 pty，只验证一件事：杀掉进程之后，终端属性回来了。
"""
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import termios
import threading
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


def _echo_on(fd):
    """终端是否开着回显（= 是否已从 raw 模式恢复）。"""
    return bool(termios.tcgetattr(fd)[3] & termios.ECHO)


@unittest.skipIf(os.name == "nt", "Windows 无 PTY")
class ShellTerminalRestoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_port = _free_port()
        cls.agent = subprocess.Popen(
            [PY, "-u", "mcp_agent.py", "--host", "127.0.0.1",
             "--port", str(cls.agent_port), "--no-auto-secure", "--allow-insecure"],
            cwd=SCRIPTS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_port(cls.agent_port, 30):
            cls.agent.kill()
            raise unittest.SkipTest("副端没起来")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.agent.terminate()
            cls.agent.wait(timeout=5)
        except Exception:
            try:
                cls.agent.kill()
            except Exception:
                pass

    def _spawn_shell(self):
        """在真 pty 里起 fleet shell，返回 (进程, 主端 fd, 从端 fd)。

        必须一直有人读主端：pty 缓冲很小，没人读就会满，写端阻塞之后
        连终端恢复都测不出来（表现成「进程不退出」）。
        """
        master, slave = pty.openpty()
        p = subprocess.Popen(
            [PY, "-u", "fleet.py", "shell",
             "http://127.0.0.1:%d/mcp" % self.agent_port],
            cwd=SCRIPTS, stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid)

        stop = threading.Event()
        self._buf = bytearray()

        def drain():
            while not stop.is_set():
                try:
                    r, _, _ = select.select([master], [], [], 0.2)
                except Exception:
                    return
                if not r:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self._buf.extend(chunk)

        self._drain_stop = stop
        self._drain_thread = threading.Thread(target=drain, daemon=True)
        self._drain_thread.start()
        return p, master, slave

    def _wait_connected(self, master, timeout=25.0):
        """等到 shell 真的连上（会打印「已连接」），此时 raw 模式已生效。"""
        marker = "已连接".encode("utf-8")
        end = time.time() + timeout
        while time.time() < end:
            if marker in bytes(self._buf):
                return True
            time.sleep(0.2)
        return False

    def test_termios_restored_after_sigterm(self):
        p, master, slave = self._spawn_shell()
        try:
            if not self._wait_connected(master):
                self.skipTest("shell 没连上（环境不允许）")
            time.sleep(0.5)          # 让 setraw 生效
            self.assertFalse(_echo_on(slave),
                             "前置条件不成立：进入会话后终端应当是 raw（无回显）")

            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
                self.fail("收到 SIGTERM 后进程没有退出")

            self.assertTrue(_echo_on(slave),
                            "被 SIGTERM 杀掉后终端仍是 raw 模式——用户得盲敲 reset 才能恢复")
        finally:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
            for fd in (master, slave):
                try:
                    os.close(fd)
                except Exception:
                    pass

    def test_termios_restored_after_sighup(self):
        """SSH 断线（终端关闭）发的是 SIGHUP，同样要把终端还回去。"""
        p, master, slave = self._spawn_shell()
        try:
            if not self._wait_connected(master):
                self.skipTest("shell 没连上（环境不允许）")
            time.sleep(0.5)
            p.send_signal(signal.SIGHUP)
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
                self.fail("收到 SIGHUP 后进程没有退出")
            self.assertTrue(_echo_on(slave), "被 SIGHUP 杀掉后终端仍是 raw 模式")
        finally:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
            for fd in (master, slave):
                try:
                    os.close(fd)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
