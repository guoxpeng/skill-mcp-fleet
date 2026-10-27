#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`fleet cp` 推送路径：这是「替代 ssh」里最容易悄悄出错的一环。

scp 传坏了会报错；fleet 之前不会——分块拼完就当成功，文件被截断也照样打印
「已推送」。这里用一个假的副端（真文件系统 + 迷你 shell）把三条性质钉住：

  1. 大文件按块流式传，不整个读进内存
  2. 分块拼装分批执行，不拼成一条超长命令（>128 块）
  3. 拼完核对远端大小；中途失败要把 .fleetpart.* 清干净
"""
import base64
import os
import shlex
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class FakeRemote(object):
    """把「远端」实现成本地目录 + 够用的迷你 shell。"""

    def __init__(self, root):
        self.root = root
        self.uploads = []
        self.execs = []
        self.fail_upload_at = None
        self.truncate_final = False

    def _p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def call(self, tool, args=None, timeout=None):
        args = args or {}
        if tool == "upload":
            n = len(self.uploads)
            if self.fail_upload_at is not None and n == self.fail_upload_at:
                return "磁盘满了", True
            p = self._p(args["filePath"])
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = base64.b64decode(args["content"])
            with open(p, "wb") as f:
                f.write(data)
            self.uploads.append(args["filePath"])
            return "ok", False
        if tool == "exec":
            return self._exec(args["command"])
        raise AssertionError("未预期的工具 %s" % tool)

    def _exec(self, cmd):
        self.execs.append(cmd)
        try:
            code, out = self._run(cmd)
        except Exception as e:
            return "[exit 1]\n%s" % e, False
        return "[exit %d]\n%s" % (code, out), False

    def _run(self, cmd):
        if cmd.startswith("mkdir -p "):
            # mkdir -p <dir> && : > <file>
            rest = cmd[len("mkdir -p "):]
            d, _, tail = rest.partition(" && ")
            os.makedirs(self._p(shlex.split(d)[0]), exist_ok=True)
            if tail.startswith(": > "):
                open(self._p(shlex.split(tail[4:])[0]), "wb").close()
            return 0, ""
        if cmd.startswith(": > "):
            p = self._p(shlex.split(cmd[4:])[0])
            open(p, "wb").close()
            return 0, ""
        if cmd.startswith("cat "):
            parts = shlex.split(cmd)
            assert parts[0] == "cat"
            # cat p1 p2 ... > target   或   >> target
            if ">" in parts:
                i = parts.index(">")
                append = False
            elif ">>" in parts:
                i = parts.index(">>")
                append = True
            else:
                raise AssertionError("cat 没有重定向：%s" % cmd[:80])
            srcs, target = parts[1:i], self._p(parts[i + 1])
            assert i - 1 <= 128, "单条 cat 拼了 %d 个分块，会撞 ARG_MAX" % (i - 1)
            blob = b"".join(open(self._p(s), "rb").read() for s in srcs)
            with open(target, "ab" if append else "wb") as f:
                f.write(blob)
            if self.truncate_final and not append:
                # 模拟远端只写了一半就返回成功
                data = open(target, "rb").read()
                open(target, "wb").write(data[:len(data) // 2])
            return 0, ""
        if cmd.startswith("rm -f "):
            pattern = shlex.split(cmd)[2]
            d, base = os.path.split(self._p(pattern))
            prefix = base.rstrip("*")
            for fn in os.listdir(d):
                if fn.startswith(prefix):
                    os.unlink(os.path.join(d, fn))
            return 0, ""
        if cmd.startswith("stat "):
            target = self._p(shlex.split(cmd)[3])
            return 0, str(os.path.getsize(target))
        if cmd.startswith("chmod "):
            return 0, ""
        raise AssertionError("未支持的远端命令：%s" % cmd[:80])

    def leftovers(self, remote):
        d, base = os.path.split(self._p(remote))
        if not os.path.isdir(d):
            return []
        return [f for f in os.listdir(d) if f.startswith(base + ".fleetpart")]


class PushFileTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fleet-cp-remote-")
        self.local_dir = tempfile.mkdtemp(prefix="fleet-cp-local-")
        self.remote = FakeRemote(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.local_dir, ignore_errors=True)

    def _make(self, size):
        p = os.path.join(self.local_dir, "payload.bin")
        data = os.urandom(size)
        with open(p, "wb") as f:
            f.write(data)
        return p, data

    def test_small_file_roundtrip(self):
        p, data = self._make(1000)
        n = fleet._cp_push_file(self.remote, p, "/tmp/x.bin", 30, quiet=True)
        self.assertEqual(n, len(data))
        self.assertEqual(open(os.path.join(self.root, "tmp/x.bin"), "rb").read(), data)

    def test_empty_file(self):
        p = os.path.join(self.local_dir, "empty.bin")
        open(p, "wb").close()
        n = fleet._cp_push_file(self.remote, p, "/tmp/e.bin", 30, quiet=True)
        self.assertEqual(n, 0)
        self.assertEqual(os.path.getsize(os.path.join(self.root, "tmp/e.bin")), 0)

    def test_large_file_batches_cat_commands(self):
        """>128 块时必须分批拼装，否则远端会 "argument list too long"。"""
        size = fleet.CP_PUSH_CHUNK * 300          # 300 块
        p, data = self._make(size)
        n = fleet._cp_push_file(self.remote, p, "/tmp/big.bin", 60, quiet=True)
        self.assertEqual(n, size)
        got = open(os.path.join(self.root, "tmp/big.bin"), "rb").read()
        self.assertEqual(len(got), size)
        self.assertEqual(got, data)
        cats = [c for c in self.remote.execs if c.startswith("cat ")]
        self.assertEqual(len(cats), 3, "300 块按 128 一批应拆成 3 条 cat")

    def test_size_mismatch_raises(self):
        """远端只写了一半却返回成功——必须报错，不能打印「已推送」。"""
        p, _ = self._make(4096)
        self.remote.truncate_final = True
        with self.assertRaises(fleet.MCPError) as cm:
            fleet._cp_push_file(self.remote, p, "/tmp/t.bin", 30, quiet=True)
        self.assertIn("大小不符", str(cm.exception))

    def test_upload_failure_cleans_up_parts(self):
        p, _ = self._make(fleet.CP_PUSH_CHUNK * 5)
        self.remote.fail_upload_at = 2
        with self.assertRaises(fleet.MCPError):
            fleet._cp_push_file(self.remote, p, "/tmp/f.bin", 30, quiet=True)
        self.assertEqual(self.remote.leftovers("/tmp/f.bin"), [],
                         "失败后不该在远端留下 .fleetpart 残块")

    def test_success_leaves_no_parts(self):
        p, _ = self._make(fleet.CP_PUSH_CHUNK * 3)
        fleet._cp_push_file(self.remote, p, "/tmp/ok.bin", 30, quiet=True)
        self.assertEqual(self.remote.leftovers("/tmp/ok.bin"), [])

    def test_does_not_read_whole_file_at_once(self):
        """流式读：确认实现里没有把整个文件读进内存。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                                "fleet.py"), encoding="utf-8").read()
        start = src.index("def _cp_push_file")
        end = src.index("def _cp_clean_parts")
        body = src[start:end]
        # 老实现是 `data = f.read()`，几个 G 的文件直接把客户端撑爆
        self.assertNotIn("data = f.read()", body)
        self.assertIn("f.read(CP_PUSH_CHUNK)", body)


if __name__ == "__main__":
    unittest.main()
