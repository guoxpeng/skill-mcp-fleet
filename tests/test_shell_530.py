#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.3 交互式 shell（PTY）：这是「fleet 替代 ssh」的核心能力。

这些用例直接调 agent 侧的工具函数，不经过 HTTP —— 传输层的行为由
tests/test_master_http.py / test_agent.py 覆盖，这里只关心 PTY 本身：
是不是真终端、增量读取的 offset 语义、并发上限、关闭是否幂等。
"""
import base64
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent


def _open(**kw):
    text, is_err = mcp_agent.t_shell_open(kw)
    assert not is_err, text
    return json.loads(text)["session_id"]


def _read(sid, offset=0, wait=0.0):
    text, is_err = mcp_agent.t_shell_read(
        {"session_id": sid, "offset": offset, "wait": wait})
    assert not is_err, text
    d = json.loads(text)
    return base64.b64decode(d["data_b64"]), d["offset"], d["alive"]


def _write(sid, data):
    if isinstance(data, str):
        data = data.encode()
    return mcp_agent.t_shell_write(
        {"session_id": sid, "data_b64": base64.b64encode(data).decode("ascii")})


def _drain(sid, offset, seconds=2.0):
    """把这段时间的输出全收下来。"""
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        chunk, offset, alive = _read(sid, offset, wait=0.2)
        buf += chunk
        if not alive:
            break
    return buf, offset


@unittest.skipIf(os.name == "nt", "Windows 无 PTY")
class ShellSessionTest(unittest.TestCase):
    def setUp(self):
        self.sid = _open(cols=100, rows=30)

    def tearDown(self):
        mcp_agent.t_shell_close({"session_id": self.sid})

    def test_is_real_tty(self):
        """必须是真终端：否则 vim/top 会直接报 not a tty，替代 ssh 就是空话。"""
        _drain(self.sid, 0, 1.5)
        _write(self.sid, "tty\n")
        out, _ = _drain(self.sid, 0, 1.5)
        self.assertIn(b"/dev/", out)

    def test_term_is_set(self):
        _write(self.sid, "echo T=$TERM\n")
        out, _ = _drain(self.sid, 0, 1.5)
        self.assertIn(b"T=xterm-256color", out)

    def test_command_runs_and_echoes(self):
        _write(self.sid, "echo MARK-$((6*7))\n")
        out, _ = _drain(self.sid, 0, 1.5)
        self.assertIn(b"MARK-42", out)

    def test_incremental_offset(self):
        """offset 是绝对偏移：读过的内容不会再返回一遍。"""
        _write(self.sid, "echo FIRST\n")
        out1, off1 = _drain(self.sid, 0, 1.5)
        self.assertIn(b"FIRST", out1)
        out2, off2 = _drain(self.sid, off1, 0.5)
        self.assertNotIn(b"FIRST", out2)
        self.assertGreaterEqual(off2, off1)

    def test_resize(self):
        ok, err = mcp_agent.t_shell_resize({"session_id": self.sid,
                                            "cols": 132, "rows": 43})
        self.assertFalse(err, ok)
        sess = mcp_agent.SHELLS[self.sid]
        self.assertEqual((sess["cols"], sess["rows"]), (132, 43))

    def test_list_contains_session(self):
        text, err = mcp_agent.t_shell_list({})
        self.assertFalse(err)
        self.assertIn(self.sid, text)

    def test_close_is_idempotent(self):
        mcp_agent.t_shell_close({"session_id": self.sid})
        _text, err = mcp_agent.t_shell_close({"session_id": self.sid})
        self.assertFalse(err, "重复关闭不该报错")

    def test_exit_marks_dead(self):
        _write(self.sid, "exit\n")
        off, alive = 0, True
        # offset 必须跟着走，否则每次都立刻返回同一段缓冲，等不到 EOF
        for _ in range(25):
            _chunk, off, alive = _read(self.sid, off, wait=0.2)
            if not alive:
                break
        self.assertFalse(alive, "远端 exit 之后会话应标记为已结束")


@unittest.skipIf(os.name == "nt", "Windows 无 PTY")
class ShellGuardTest(unittest.TestCase):
    def test_unknown_session_write(self):
        text, err = mcp_agent.t_shell_write(
            {"session_id": "sh-nope", "data_b64": base64.b64encode(b"x").decode()})
        self.assertTrue(err)
        self.assertIn("未知 session_id", text)

    def test_unknown_session_read(self):
        _text, err = mcp_agent.t_shell_read({"session_id": "sh-nope", "offset": 0})
        self.assertTrue(err)

    def test_close_unknown_is_ok(self):
        _text, err = mcp_agent.t_shell_close({"session_id": "sh-nope"})
        self.assertFalse(err, "关闭不存在的会话应当幂等成功")

    def test_empty_write_rejected(self):
        sid = _open()
        try:
            text, err = mcp_agent.t_shell_write({"session_id": sid, "data_b64": ""})
            self.assertTrue(err)
        finally:
            mcp_agent.t_shell_close({"session_id": sid})

    def test_session_cap(self):
        """会话数有上限，防止被反复 open 撑爆内存。"""
        opened = []
        try:
            for _ in range(mcp_agent.SHELL_MAX):
                opened.append(_open())
            text, err = mcp_agent.t_shell_open({})
            self.assertTrue(err)
            self.assertIn("上限", text)
        finally:
            for sid in opened:
                mcp_agent.t_shell_close({"session_id": sid})


if __name__ == "__main__":
    unittest.main()
