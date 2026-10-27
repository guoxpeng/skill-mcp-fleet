#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级备份不能无限堆。

`_backup_code_dir` 每次 `fleet up` 整目录复制一份到 `~/.mcp-fleet/up-backups/`，
从来没人清理。真机上一台设备升了 9 次就占了 9.1MB（每次约 1MB）——在软路由
这种 overlay 只有几百 MB 的机器上，这是会写死设备的那类问题。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class PruneBackupsTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fleet-upb-")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _mk(self, name, age):
        p = os.path.join(self.base, name)
        os.makedirs(p)
        with open(os.path.join(p, "f.txt"), "w") as f:
            f.write("x")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_keeps_newest(self):
        for i in range(9):
            self._mk("up-%d" % i, age=100 - i)     # up-8 最新
        removed = fleet._prune_backups(self.base, keep=5)
        self.assertEqual(removed, 4)
        left = sorted(os.listdir(self.base))
        self.assertEqual(len(left), 5)
        for i in (4, 5, 6, 7, 8):
            self.assertIn("up-%d" % i, left, "应保留最新的 5 份")

    def test_noop_when_few(self):
        for i in range(3):
            self._mk("up-%d" % i, age=i)
        self.assertEqual(fleet._prune_backups(self.base, keep=5), 0)
        self.assertEqual(len(os.listdir(self.base)), 3)

    def test_missing_dir_is_safe(self):
        self.assertEqual(fleet._prune_backups("/nonexistent/xyz", keep=5), 0)

    def test_ignores_files(self):
        """备份区里混进普通文件时不能删（也不该崩）。"""
        self._mk("up-1", 10)
        with open(os.path.join(self.base, "notes.txt"), "w") as f:
            f.write("hi")
        fleet._prune_backups(self.base, keep=0)
        self.assertTrue(os.path.exists(os.path.join(self.base, "notes.txt")))

    def test_default_keep_is_bounded(self):
        self.assertGreaterEqual(fleet.UP_BACKUP_KEEP, 2, "至少要能回滚两次")
        self.assertLessEqual(fleet.UP_BACKUP_KEEP, 10, "留太多等于没上限")


class BackupCallsPruneTest(unittest.TestCase):
    """_backup_code_dir 本身要触发清理，而不是只提供函数没人调。"""

    def test_backup_triggers_prune(self):
        home = tempfile.mkdtemp(prefix="fleet-home-")
        cdir = tempfile.mkdtemp(prefix="fleet-cdir-")
        orig_home = os.environ.get("HOME")
        orig_keep = fleet.UP_BACKUP_KEEP
        try:
            with open(os.path.join(cdir, "fleet.py"), "w") as f:
                f.write("# fake")
            os.environ["HOME"] = home
            fleet.UP_BACKUP_KEEP = 2
            base = os.path.join(home, ".mcp-fleet", "up-backups")
            for i in range(4):
                dst = fleet._backup_code_dir(cdir, "2026010%d-000000" % i, "t")
                self.assertTrue(dst, "备份应成功")
                time.sleep(0.02)
            self.assertLessEqual(len(os.listdir(base)), 2,
                                 "备份份数必须被 UP_BACKUP_KEEP 限制住")
        finally:
            if orig_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = orig_home
            fleet.UP_BACKUP_KEEP = orig_keep
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(cdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
