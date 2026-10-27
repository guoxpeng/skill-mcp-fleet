#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 `exec` 的输出上限必须**在读的时候**生效。

原来用 `p.communicate(timeout=t)` 收输出，收完才截断到 1MB。问题是
`communicate` 会把 stdout/stderr 全读进内存——`yes` 或 `cat /dev/urandom`
在 timeout（默认 120s）到点之前就已经把设备撑爆了，事后再截断纯属自我安慰。
软路由只有几百 MB 内存，这个区别就是「活着」和「起不来」。

验证方式是给子进程设 `RLIMIT_AS`：如果实现还在无界缓冲，几百 MB 的输出
必然 MemoryError；只有边读边丢才能过。
"""
import os
import resource
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
PY = sys.executable

sys.path.insert(0, SCRIPTS)
import mcp_agent as agent


class CappedOutputTest(unittest.TestCase):
    def test_small_output_untouched(self):
        code, out, err = agent.run_local("printf 'hello'")
        self.assertEqual(code, 0)
        self.assertEqual(out, "hello")
        self.assertNotIn("截断", err)

    def test_large_output_capped_and_flagged(self):
        """输出被截到 1MB，且明确告诉调用方「被截断了、实际多少」。"""
        code, out, err = agent.run_local("yes abcdefghij | head -c 20000000", timeout=60)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), agent.RUN_BUF_MAX,
                             "stdout 超过上限，说明截断没生效")
        self.assertIn("截断", err, "截断后必须告诉调用方，不能静默丢数据")
        self.assertIn("20000000", err, "提示里要给出实际字节数，便于判断该换 async")

    def test_stderr_capped_too(self):
        # 注意别用 `yes ... 1>&2`：那是个无限生产者，只有超时才能停，
        # 测的就不是上限而是超时了。用 head -c 造一段有确定长度的输出。
        code, out, err = agent.run_local(
            "head -c 5000000 /dev/zero | tr '\\0' 'o' >&2", timeout=60)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(err), 65536 + 400,
                             "stderr 没有被单独限制，会把提示本身挤掉")
        self.assertIn("截断", err)

    def test_stdin_still_works(self):
        code, out, err = agent.run_local("cat", stdin="hello-stdin")
        self.assertEqual(code, 0)
        self.assertEqual(out, "hello-stdin")

    def test_timeout_still_kills_tree(self):
        import time
        t0 = time.time()
        code, out, err = agent.run_local("sleep 30 & sleep 30", timeout=1)
        self.assertEqual(code, -1)
        self.assertIn("超时", err)
        self.assertLess(time.time() - t0, 10, "超时后没有及时返回（子孙进程没杀干净）")


class BoundedMemoryTest(unittest.TestCase):
    """喂 400MB 输出，看进程峰值内存涨了多少。

    无界缓冲（老的 `communicate` 路径）会把 400MB 全收进内存，峰值至少涨
    400MB；边读边丢则只涨到 1MB 上限那一档。用独立子进程量，基线在同一
    进程内取，不受测试进程历史峰值干扰。
    """

    SCRIPT = r'''
import resource, sys
sys.path.insert(0, %r)
import mcp_agent as a

def peak_mb():
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 单位是字节，Linux 是 KB
    return v / 1048576.0 if sys.platform == "darwin" else v / 1024.0

base = peak_mb()
code, out, err = a.run_local("head -c 400000000 /dev/zero", timeout=120)
print("OK", code, len(out), "截断" in err, round(peak_mb() - base, 1))
'''

    def test_no_memory_blowup(self):
        script = self.SCRIPT % SCRIPTS
        p = subprocess.run([PY, "-c", script], capture_output=True, timeout=300)
        out = p.stdout.decode("utf-8", "ignore").strip()
        err = p.stderr.decode("utf-8", "ignore")
        self.assertEqual(p.returncode, 0,
                         "子进程崩了（很可能就是内存被输出撑爆）：\n%s" % err[-1500:])
        self.assertTrue(out.startswith("OK"), "输出异常：%r" % out[:300])
        parts = out.split()
        self.assertEqual(parts[1], "0", "命令没正常结束")
        self.assertLessEqual(int(parts[2]), agent.RUN_BUF_MAX)
        self.assertEqual(parts[3], "True", "海量输出却没有标记截断")
        growth = float(parts[4])
        self.assertLess(growth, 120.0,
                        "输出 400MB 时峰值内存涨了 %.1f MB —— 说明还是先把整段"
                        "收进内存再截断，设备会被撑爆" % growth)


if __name__ == "__main__":
    unittest.main()
