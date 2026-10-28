#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.2.6 连接管理：会话表 / 挂起接管 / 录制回放。

这一版的出发点是把 ssh 散装的三件事（`who` 看谁在、`ss -tp` 看连接、
`pkill` 断连接）合成一条线，并且补上 ssh 做不到的：**断线回来还在**。

测试重点不是"功能有没有"，而是几个容易假对的点：
  * cast 文件是不是真的 asciinema v2（时间戳单调、每行合法 JSON）；
  * PTY 输出在多字节字符中间被切开时，录制里**不能出现替换字符**；
  * `recording_fetch` 的 id 直接拼路径，必须挡住目录穿越；
  * `session_kill` 靠 id 前缀分发，前缀判错就会去杀错东西；
  * 挂起时**不能**调 shell_close —— 调了就等于 ssh 断开，整个功能白做。
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent
import fleet


def _src(name):
    with io.open(os.path.join(os.path.dirname(__file__), "..", "scripts", name),
                 encoding="utf-8") as f:
        return f.read()


class RecordingFormatTest(unittest.TestCase):
    """asciinema v2 cast：格式错了 asciinema play 就放不了，等于白录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-rec-")
        self._orig = mcp_agent.REC_DIR
        mcp_agent.REC_DIR = self.tmp

    def tearDown(self):
        mcp_agent.REC_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sess(self, sid="sh-test0001"):
        return {"id": sid, "cols": 100, "rows": 30, "start": time.time(),
                "shell": "/bin/sh"}

    def _lines(self, sid="sh-test0001"):
        path = os.path.join(self.tmp, sid + ".cast")
        with io.open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_header_is_v2_and_has_geometry(self):
        sess = self._sess()
        path = mcp_agent._rec_start(sess, "xterm-256color")
        self.assertTrue(path.endswith(".cast"))
        mcp_agent._rec_stop(sess)
        header = json.loads(self._lines()[0])
        self.assertEqual(header["version"], 2)
        self.assertEqual(header["width"], 100)
        self.assertEqual(header["height"], 30)
        self.assertEqual(header["env"]["TERM"], "xterm-256color")

    def test_events_are_valid_jsonl_and_monotonic(self):
        sess = self._sess()
        mcp_agent._rec_start(sess, "xterm")
        for i in range(5):
            mcp_agent._rec_write(sess, ("line %d\n" % i).encode())
            time.sleep(0.01)
        mcp_agent._rec_stop(sess)
        lines = self._lines()
        events = [json.loads(l) for l in lines[1:]]
        self.assertEqual(len(events), 5)
        for ev in events:
            self.assertEqual(len(ev), 3)
            self.assertEqual(ev[1], "o")            # 只记输出，不记键盘
            self.assertIsInstance(ev[2], str)
        stamps = [e[0] for e in events]
        self.assertEqual(stamps, sorted(stamps))
        self.assertGreaterEqual(stamps[0], 0.0)

    def test_multibyte_split_across_chunks_is_not_mangled(self):
        """PTY 一次 read 可能正好切在「你」的中间——这是真实会发生的。

        直接 decode(errors="replace") 会得到 \ufffd，回放里就是乱码；
        增量解码器应该把半截留着，等下一块拼齐。
        """
        sess = self._sess()
        mcp_agent._rec_start(sess, "xterm")
        raw = "你好".encode("utf-8")            # e4bd a0e5 a5bd
        mcp_agent._rec_write(sess, raw[:2])     # 切在第一个字中间
        mcp_agent._rec_write(sess, raw[2:4])    # 又切在第二个字中间
        mcp_agent._rec_write(sess, raw[4:])
        mcp_agent._rec_stop(sess)
        text = "".join(json.loads(l)[2] for l in self._lines()[1:])
        self.assertEqual(text, "你好")
        self.assertNotIn("\ufffd", text)

    def test_oversize_stops_writing_instead_of_filling_disk(self):
        sess = self._sess()
        orig = mcp_agent.REC_MAX
        mcp_agent.REC_MAX = 200
        try:
            mcp_agent._rec_start(sess, "xterm")
            for _ in range(20):
                mcp_agent._rec_write(sess, b"x" * 60)
            mcp_agent._rec_stop(sess)
        finally:
            mcp_agent.REC_MAX = orig
        size = os.path.getsize(os.path.join(self.tmp, "sh-test0001.cast"))
        self.assertLess(size, 1000, "超过上限还在写：磁盘会被写满")

    def test_stop_is_idempotent(self):
        sess = self._sess()
        mcp_agent._rec_start(sess, "xterm")
        mcp_agent._rec_write(sess, b"abc")
        mcp_agent._rec_stop(sess)
        mcp_agent._rec_stop(sess)          # 第二次不能抛
        self.assertIsNone(sess.get("rec"))

    def test_start_failure_does_not_break_session(self):
        """录制目录建不出来（只读/权限）时，会话本身必须照常能开。"""
        mcp_agent.REC_DIR = os.path.join(self.tmp, "no", "such", "\0bad")
        sess = self._sess()
        self.assertEqual(mcp_agent._rec_start(sess, "xterm"), "")
        mcp_agent._rec_write(sess, b"still fine")   # 无 rec 时应当静默跳过
        self.assertIsNone(sess.get("rec"))


class RecordingApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-recap-")
        self._orig = mcp_agent.REC_DIR
        mcp_agent.REC_DIR = self.tmp

    def tearDown(self):
        mcp_agent.REC_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, sid, body):
        path = os.path.join(self.tmp, sid + ".cast")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write('{"version": 2, "width": 80, "height": 24}\n')
            f.write(json.dumps([0.1, "o", body]) + "\n")

    def test_list_sorted_by_mtime_desc(self):
        self._make("sh-old", "a")
        time.sleep(0.01)
        self._make("sh-new", "b")
        text, err = mcp_agent.t_recording_list({})
        self.assertFalse(err)
        ids = [r["id"] for r in json.loads(text)["recordings"]]
        self.assertEqual(ids, ["sh-new", "sh-old"])

    def test_fetch_chunks_by_byte_offset(self):
        self._make("sh-chunk", "x" * 300)
        got = b""
        off = 0
        for _ in range(20):
            text, err = mcp_agent.t_recording_fetch(
                {"id": "sh-chunk", "offset": off, "limit": 4096})
            self.assertFalse(err, text)
            d = json.loads(text)
            got += base64.b64decode(d["data_b64"])
            off = d["next"]
            if d["done"]:
                break
        self.assertEqual(got.decode("utf-8").count("x"), 300)

    def test_fetch_rejects_path_traversal(self):
        """id 直接拼进路径，不校验就能读任意文件。"""
        for bad in ("../config", "..", "/etc/passwd", ".hidden", "a/b"):
            text, err = mcp_agent.t_recording_fetch({"id": bad})
            self.assertTrue(err, "居然放过了 %r：%s" % (bad, text))

    def test_fetch_missing_says_where_to_look(self):
        text, err = mcp_agent.t_recording_fetch({"id": "sh-nope"})
        self.assertTrue(err)
        self.assertIn("recordings", text)


class SessionsTableTest(unittest.TestCase):
    """一张表要真的把三类连接都收进来，否则还是得敲三次。"""

    def setUp(self):
        self._shells = dict(mcp_agent.SHELLS)
        self._tuns = dict(mcp_agent.TUNNELS)
        self._jobs = dict(mcp_agent.JOBS)
        mcp_agent.SHELLS.clear()
        mcp_agent.TUNNELS.clear()
        mcp_agent.JOBS.clear()
        now = time.time()
        mcp_agent.SHELLS["sh-aaaa1111"] = {
            "id": "sh-aaaa1111", "fd": -1, "pid": None, "buf": b"", "base": 0,
            "total": 1234, "lock": threading.Lock(), "start": now - 300,
            "last_read": now - 5, "end": None, "exit": None, "shell": "/bin/zsh",
            "cols": 120, "rows": 40, "bytes_in": 42, "label": "deploy",
            "owner": "me@host", "rec": {"path": "/x/sh-aaaa1111.cast", "bytes": 99}}
        mcp_agent.TUNNELS["tn-bbbb2222"] = {
            "id": "tn-bbbb2222", "kind": "conn", "peer": "10.0.0.5:5432",
            "total": 2048, "start": now - 60, "last": now - 1,
            "last_data": now - 1, "end": None, "lock": threading.Lock()}
        mcp_agent.TUNNELS["tl-cccc3333"] = {
            "id": "tl-cccc3333", "kind": "listen", "bound": "127.0.0.1:9000",
            "pending": [], "start": now - 30, "last": now, "end": None,
            "lock": threading.Lock()}
        mcp_agent.JOBS["dddd4444"] = {
            "id": "dddd4444", "proc": None, "buf": bytearray(b"out"),
            "lock": threading.Lock(), "start": now - 10, "end": None,
            "exit": None, "cmd": "sleep 100"}

    def tearDown(self):
        mcp_agent.SHELLS.clear(); mcp_agent.SHELLS.update(self._shells)
        mcp_agent.TUNNELS.clear(); mcp_agent.TUNNELS.update(self._tuns)
        mcp_agent.JOBS.clear(); mcp_agent.JOBS.update(self._jobs)

    def test_all_kinds_present(self):
        text, err = mcp_agent.t_sessions({})
        self.assertFalse(err)
        d = json.loads(text)
        kinds = sorted(s["kind"] for s in d["sessions"])
        self.assertEqual(kinds, ["job", "shell", "tunnel-conn", "tunnel-listen"])
        self.assertEqual(d["count"], 4)

    def test_shell_row_carries_what_you_need_to_decide(self):
        d = json.loads(mcp_agent.t_sessions({})[0])
        row = [s for s in d["sessions"] if s["kind"] == "shell"][0]
        self.assertEqual(row["label"], "deploy")
        self.assertEqual(row["owner"], "me@host")
        self.assertEqual(row["geometry"], "120x40")
        self.assertEqual(row["bytes_out"], 1234)
        self.assertEqual(row["bytes_in"], 42)
        self.assertTrue(row["recording"].endswith(".cast"))
        self.assertTrue(row["alive"])
        self.assertGreaterEqual(row["age"], 290)
        self.assertLessEqual(row["idle"], 10)

    def test_empty_table_still_parses(self):
        mcp_agent.SHELLS.clear(); mcp_agent.TUNNELS.clear(); mcp_agent.JOBS.clear()
        d = json.loads(mcp_agent.t_sessions({})[0])
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["sessions"], [])


class SessionKillDispatchTest(unittest.TestCase):
    """前缀判错就会杀错东西——这条必须逐前缀钉住。"""

    def setUp(self):
        self.calls = []
        self._orig = (mcp_agent.t_shell_close, mcp_agent.t_tunnel_close,
                      mcp_agent.t_exec_kill)

        def mk(name):
            def f(a):
                self.calls.append((name, a))
                return "ok", False
            return f
        mcp_agent.t_shell_close = mk("shell")
        mcp_agent.t_tunnel_close = mk("tunnel")
        mcp_agent.t_exec_kill = mk("job")

    def tearDown(self):
        (mcp_agent.t_shell_close, mcp_agent.t_tunnel_close,
         mcp_agent.t_exec_kill) = self._orig

    def _run(self, **kw):
        text, err = mcp_agent.t_session_kill(kw)
        self.assertFalse(err, text)
        return self.calls[-1]

    def test_shell_prefix(self):
        self.assertEqual(self._run(id="sh-1234abcd")[0], "shell")

    def test_tunnel_conn_prefix(self):
        self.assertEqual(self._run(id="tn-1234abcd")[0], "tunnel")

    def test_tunnel_listen_prefix(self):
        self.assertEqual(self._run(id="tl-1234abcd")[0], "tunnel")

    def test_job_fallback(self):
        self.assertEqual(self._run(id="abcdef123456")[0], "job")

    def test_explicit_kind_wins(self):
        self.assertEqual(self._run(id="sh-1234abcd", kind="job")[0], "job")

    def test_empty_id_is_an_error_not_a_noop(self):
        text, err = mcp_agent.t_session_kill({"id": ""})
        self.assertTrue(err)
        self.assertIn("id", text)

    def test_unknown_kind_rejected(self):
        text, err = mcp_agent.t_session_kill({"id": "sh-x", "kind": "nope"})
        self.assertTrue(err)
        self.assertIn("shell", text)


class AgeFormatTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(mcp_agent._age_str(5), "5s")
        self.assertEqual(mcp_agent._age_str(65), "1m05s")
        self.assertEqual(mcp_agent._age_str(3661), "1h01m")
        self.assertEqual(mcp_agent._age_str(90000), "1d01h")
        self.assertEqual(mcp_agent._age_str(-3), "0s")


class ShellRecordPlumbingTest(unittest.TestCase):
    """真开一个 PTY，确认 record 开关真的落到磁盘上。"""

    def setUp(self):
        if os.name == "nt":
            self.skipTest("Windows 没有 PTY")
        self.tmp = tempfile.mkdtemp(prefix="fleet-recpty-")
        self._orig = mcp_agent.REC_DIR
        mcp_agent.REC_DIR = self.tmp

    def tearDown(self):
        mcp_agent.REC_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_true_creates_cast(self):
        text, err = mcp_agent.t_shell_open({"record": True, "label": "t"})
        self.assertFalse(err, text)
        info = json.loads(text)
        sid = info["session_id"]
        try:
            self.assertTrue(info["recording"].endswith(sid + ".cast"))
            mcp_agent.t_shell_write(
                {"session_id": sid,
                 "data_b64": base64.b64encode(b"echo REC_MARK\n").decode()})
            deadline = time.time() + 5
            while time.time() < deadline:
                chunk, _off, _alive = self._read(sid)
                if b"REC_MARK" in chunk:
                    break
                time.sleep(0.05)
        finally:
            mcp_agent.t_shell_close({"session_id": sid})
        path = os.path.join(self.tmp, sid + ".cast")
        self.assertTrue(os.path.isfile(path))
        with io.open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("REC_MARK", body)
        # 关闭之后录制必须收尾（文件可被完整解析）
        for line in body.splitlines():
            json.loads(line)

    def test_record_false_creates_nothing(self):
        text, err = mcp_agent.t_shell_open({"record": False})
        self.assertFalse(err, text)
        info = json.loads(text)
        sid = info["session_id"]
        try:
            self.assertEqual(info["recording"], "")
            self.assertFalse(os.path.exists(os.path.join(self.tmp, sid + ".cast")))
        finally:
            mcp_agent.t_shell_close({"session_id": sid})

    def _read(self, sid):
        text, err = mcp_agent.t_shell_read({"session_id": sid, "offset": 0})
        self.assertFalse(err, text)
        d = json.loads(text)
        return base64.b64decode(d["data_b64"]), d["offset"], d["alive"]


class CliWiringTest(unittest.TestCase):
    """命令行这一层最容易「参数加了但没接上」。"""

    def test_subcommands_bound(self):
        p = fleet.build_parser()
        for name in ("sessions", "attach", "kill", "recordings", "replay"):
            a = p.parse_args([name] + (["sh-1"] if name in
                                       ("attach", "kill", "replay") else []))
            self.assertTrue(callable(getattr(a, "func", None)), name)

    def test_shell_target_is_optional(self):
        """`fleet shell` 省略副端应当落到本机，而不是 usage。"""
        a = fleet.build_parser().parse_args(["shell"])
        self.assertEqual(a.target, "")

    def test_attach_requires_session_id(self):
        with self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["attach"])

    def test_kill_kind_choices(self):
        a = fleet.build_parser().parse_args(["kill", "sh-1", "--kind", "tunnel"])
        self.assertEqual(a.kind, "tunnel")
        with self.assertRaises(SystemExit):
            fleet.build_parser().parse_args(["kill", "sh-1", "--kind", "nope"])

    def test_replay_flags(self):
        a = fleet.build_parser().parse_args(
            ["replay", "sh-1", "--speed", "2", "--dump"])
        self.assertEqual(a.speed, 2.0)
        self.assertTrue(a.dump)

    def test_detach_byte_is_ctrl_bracket(self):
        self.assertEqual(fleet.DETACH_BYTE, b"\x1d")

    def test_detach_must_not_close_session(self):
        """挂起路径里出现 shell_close 就等于 ssh 断线，功能白做。

        只看 cmd_shell 的源码不够——要确认 `shell_close` 只在 else 分支里。
        """
        src = _src("fleet.py")
        i = src.index("if state.get(\"detached\"):")
        seg = src[i:i + 900]
        self.assertIn("已挂起", seg)
        # 注释里会提到 shell_close（解释为什么不能调），先剥掉注释再断言
        code = "\n".join(l for l in seg.split("else:")[0].splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("shell_close", code)

    def test_sessions_defaults_to_local(self):
        a = fleet.build_parser().parse_args(["sessions"])
        self.assertEqual(a.target, "")


class ForwardCompatibilityTest(unittest.TestCase):
    """连接表里隧道必须能显示出来，否则 -R 开了什么全靠记忆。"""

    def test_tunnel_kinds_are_named_in_sessions(self):
        src = _src("mcp_agent.py")
        self.assertIn('"tunnel-listen" if kind == "listen" else "tunnel-conn"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
