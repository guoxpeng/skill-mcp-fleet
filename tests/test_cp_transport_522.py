#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fleet cp` 不该依赖副端有没有 `base64` / `dd` 这两个命令。

原来推文件走 `base64 -d > f`、拉文件走 `dd if=... skip=N | base64`。busybox
常常没编译 base64 applet，于是 OpenWrt / iStoreOS 上 cp 直接废——而且报错是
「对端返回被截断」，完全指不到真正原因。

现在推走副端 Python 直接写，拉走 `download` 的 offset/length 分块。
这两个测试文件分别钉住「副端实现」和「客户端探测 + 兼容」。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet
import mcp_agent as agent


class DownloadOffsetTest(unittest.TestCase):
    """副端 download 的 offset / length 语义。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-dl-")
        self.path = os.path.join(self.dir, "a.bin")
        self.data = bytes(range(256)) * 8        # 2048 字节，含 0x00/0xff
        with open(self.path, "wb") as f:
            f.write(self.data)
        self._orig_roots = list(agent.CFG.get("allowed_roots") or [])
        agent.CFG["allowed_roots"] = ["/"]

    def tearDown(self):
        agent.CFG["allowed_roots"] = self._orig_roots
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, **kw):
        text, iserr = agent.t_download(dict(filePath=self.path, **kw))
        self.assertFalse(iserr, text)
        return base64.b64decode(text) if text else b""

    def test_full_file(self):
        self.assertEqual(self._get(), self.data)

    def test_offset_and_length(self):
        self.assertEqual(self._get(offset=100, length=16), self.data[100:116])

    def test_offset_zero_length_means_rest(self):
        self.assertEqual(self._get(offset=100, length=0), self.data[100:])

    def test_last_partial_chunk(self):
        self.assertEqual(self._get(offset=2040, length=128), self.data[2040:])

    def test_offset_at_eof_returns_empty(self):
        """越界读必须返回空，而不是报错——分块循环靠这个收尾。"""
        self.assertEqual(self._get(offset=len(self.data), length=16), b"")

    def test_negative_offset_clamped(self):
        self.assertEqual(self._get(offset=-100, length=16), self.data[:16])

    def test_missing_file_is_error(self):
        text, iserr = agent.t_download({"filePath": self.path + ".nope"})
        self.assertTrue(iserr, text)


class UploadWithoutBase64Test(unittest.TestCase):
    """推文件不该再走 `base64 -d`（busybox 可能没有这个命令）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-ul-")
        self._orig_roots = list(agent.CFG.get("allowed_roots") or [])
        agent.CFG["allowed_roots"] = ["/"]

    def tearDown(self):
        agent.CFG["allowed_roots"] = self._orig_roots
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_bytes(self):
        p = os.path.join(self.dir, "out.bin")
        data = bytes(range(256)) * 4
        text, iserr = agent.t_upload({"filePath": p,
                                      "content": base64.b64encode(data).decode()})
        self.assertFalse(iserr, text)
        self.assertEqual(open(p, "rb").read(), data)

    def test_creates_missing_dirs(self):
        p = os.path.join(self.dir, "a", "b", "c.bin")
        text, iserr = agent.t_upload({"filePath": p,
                                      "content": base64.b64encode(b"hi").decode()})
        self.assertFalse(iserr, text)
        self.assertEqual(open(p, "rb").read(), b"hi")

    def test_source_has_no_base64_command(self):
        """源码里不该再出现 `base64 -d > ` 这种依赖外部命令的写法。"""
        path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "mcp_agent.py")
        src = open(path, encoding="utf-8").read()
        start = src.index("def t_upload")
        end = src.index("def t_download")
        body = src[start:end]
        self.assertIn("with open(p, \"wb\")", body,
                      "非 sudo 路径必须走 Python 直接写")
        # sudo 分支保留命令是允许的（Python 进程不是 root），但要有注释说明
        self.assertIn("sudo", body)


class PullModeTest(unittest.TestCase):
    """客户端要能识别「副端支不支持 download 分块」。"""

    def setUp(self):
        self.data = os.urandom(4096)

    class _Client(object):
        def __init__(self, data, support_offset):
            self.data = data
            self.support = support_offset
            self.calls = []

        def call(self, tool, args=None, timeout=None):
            self.calls.append((tool, dict(args or {})))
            if tool != "download":
                raise AssertionError("未预期工具 %s" % tool)
            a = args or {}
            if self.support:
                off = int(a.get("offset") or 0)
                ln = int(a.get("length") or 0)
                raw = self.data[off:off + ln] if ln else self.data[off:]
            else:
                raw = self.data        # 老版本忽略 offset/length
            return base64.b64encode(raw).decode(), False

    def test_detects_new_agent(self):
        self.assertEqual(fleet._cp_pull_mode(self._Client(self.data, True),
                                             "/x", len(self.data), 10), "download")

    def test_detects_old_agent(self):
        self.assertEqual(fleet._cp_pull_mode(self._Client(self.data, False),
                                             "/x", len(self.data), 10), "dd")

    def test_empty_file_uses_download(self):
        self.assertEqual(fleet._cp_pull_mode(self._Client(b"", True),
                                             "/x", 0, 10), "download")

    def test_error_falls_back_to_dd(self):
        class Bad(object):
            def call(self, *a, **k):
                return "炸了", True
        self.assertEqual(fleet._cp_pull_mode(Bad(), "/x", 100, 10), "dd")

    def test_exception_falls_back_to_dd(self):
        class Boom(object):
            def call(self, *a, **k):
                raise RuntimeError("network down")
        self.assertEqual(fleet._cp_pull_mode(Boom(), "/x", 100, 10), "dd")


class PullFileRoundTripTest(unittest.TestCase):
    """整条拉取链路：download 模式与 dd 模式结果必须一致。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-pull-")
        self.remote = os.path.join(self.dir, "remote.bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _client(self, data, support_offset, chunk):
        class C(object):
            def __init__(self):
                self.dd_calls = 0

            def call(self, tool, args=None, timeout=None):
                a = args or {}
                if tool == "download":
                    if support_offset:
                        off = int(a.get("offset") or 0)
                        ln = int(a.get("length") or 0)
                        raw = data[off:off + ln] if ln else data[off:]
                    else:
                        raw = data
                    return base64.b64encode(raw).decode(), False
                if tool == "exec":
                    cmd = a.get("command", "")
                    if cmd.startswith("stat "):
                        return "[exit 0]\n%d" % len(data), False
                    if cmd.startswith("dd "):
                        self.dd_calls += 1
                        parts = cmd.split()
                        # dd if=X bs=N skip=K count=1 ...
                        bs = int(parts[2].split("=")[1])
                        skip = int(parts[3].split("=")[1])
                        chunk_data = data[skip * bs:(skip + 1) * bs]
                        return ("[exit 0]\n"
                                + base64.b64encode(chunk_data).decode()), False
                    raise AssertionError("未预期命令 %s" % cmd[:60])
                raise AssertionError("未预期工具 %s" % tool)
        return C()

    def _roundtrip(self, data, support_offset, chunk):
        with open(self.remote, "wb") as f:
            f.write(data)
        orig = fleet.CP_PULL_CHUNK
        fleet.CP_PULL_CHUNK = chunk
        try:
            local = os.path.join(self.dir, "back.bin")
            client = self._client(data, support_offset, chunk)
            n = fleet._cp_pull_file(client, self.remote, local, 30, quiet=True)
            got = open(local, "rb").read()
        finally:
            fleet.CP_PULL_CHUNK = orig
        self.assertEqual(n, len(data))
        self.assertEqual(got, data)
        return client

    def test_download_mode(self):
        data = os.urandom(1000)
        client = self._roundtrip(data, True, 128)
        self.assertEqual(client.dd_calls, 0, "新副端不该再走 dd")

    def test_dd_mode_still_works(self):
        data = os.urandom(1000)
        client = self._roundtrip(data, False, 128)
        self.assertGreater(client.dd_calls, 0, "老副端必须回退到 dd")

    def test_empty_file(self):
        self._roundtrip(b"", True, 128)


class EmptyResultNotWrappedTest(unittest.TestCase):
    """工具合法返回空字符串时，客户端不能把它包成一坨 JSON。

    `_call_once` 原来是 `if not text: text = json.dumps(res)`，于是
    `download` 读空文件 / 越界 offset 时调用方拿到的是
    `{"content": [{"type": "text", "text": ""}], ...}`，拿去 base64 解码
    就是垃圾数据——这类错误从现象上完全反推不出来。
    """

    def _client_with(self, res):
        c = fleet.MCPClient("http://127.0.0.1:1/mcp", timeout=1)
        c._post = lambda payload, timeout=None, retry=False: {"result": res}
        return c

    def test_empty_text_stays_empty(self):
        c = self._client_with({"content": [{"type": "text", "text": ""}],
                               "isError": False})
        text, iserr = c.call("download", {})
        self.assertEqual(text, "")
        self.assertFalse(iserr)

    def test_missing_content_falls_back_to_json(self):
        c = self._client_with({"resources": []})
        text, _ = c.call("resources/list", {})
        self.assertIn("resources", text, "没有 content 字段时才展示完整结构")

    def test_normal_text_passes_through(self):
        c = self._client_with({"content": [{"type": "text", "text": "hello"}]})
        self.assertEqual(c.call("x", {})[0], "hello")

    def test_multi_content_joined(self):
        c = self._client_with({"content": [{"type": "text", "text": "a"},
                                           {"type": "text", "text": "b"}]})
        self.assertEqual(c.call("x", {})[0], "a\nb")


if __name__ == "__main__":
    unittest.main()
