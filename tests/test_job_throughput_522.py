#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步任务的输出读取：既要**有界**，也要**够快**。

原来用 `BufferedReader.read(65536)` 配非阻塞 fd。`read(n)` 会凑满 n 字节才返回，
所以实际节奏是「读一块 → BlockingIOError → sleep(50ms)」——吞吐被钉死在
64KB/50ms ≈ 1.3MB/s，20MB 输出要 13.8 秒。日志流、构建输出这类场景
（正是 exec_async 的用途）会慢到没法用。

改成裸 `os.read(fd, 65536)`：有数据就返回，天然增量，实测 20MB/s 以上。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mcp_agent as agent


def _start(cmd):
    text, is_err = agent.t_exec_async({"command": cmd})
    assert not is_err, text
    jid = [ln for ln in text.splitlines() if ln.startswith("job_id=")][0].split("=")[1]
    return jid


def _wait(jid, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end:
        job = agent._job_get(jid)
        if job is None:
            return None, 0
        if job["exit"] is not None:
            return job, 0
        time.sleep(0.01)
    return agent._job_get(jid), 1


class AsyncThroughputTest(unittest.TestCase):
    def test_throughput_not_pinned_by_polling(self):
        """20MB 输出必须远快于「64KB/50ms」那条线（老实现要 13.8 秒）。"""
        size = 20 * 1000 * 1000
        t0 = time.time()
        jid = _start("head -c %d /dev/zero | tr '\\0' 'z'" % size)
        job, timed_out = _wait(jid, timeout=60)
        self.assertEqual(timed_out, 0, "20MB 输出 60 秒还没读完，吞吐明显不对")
        dt = time.time() - t0
        rate = size / 1e6 / dt
        self.assertGreater(rate, 5.0,
                           "吞吐只有 %.1f MB/s（老的非阻塞轮询实现是 1.3 MB/s，"
                           "这条线是防回退用的）" % rate)

    def test_buffer_stays_bounded(self):
        """海量输出时缓冲不能超过 2 倍上限。"""
        jid = _start("head -c 50000000 /dev/zero | tr '\\0' 'z'")
        peak = 0
        end = time.time() + 90
        while time.time() < end:
            job = agent._job_get(jid)
            if job is None:
                break
            peak = max(peak, len(job["buf"]))
            if job["exit"] is not None:
                break
            time.sleep(0.01)
        self.assertLessEqual(peak, agent.JOB_BUF_MAX * 2 + 65536,
                             "缓冲峰值 %.1f MB，超出 2 倍上限" % (peak / 1e6))
        self.assertTrue(agent._job_get(jid)["dropped"], "海量输出应标记 dropped")

    def test_small_output_is_complete(self):
        """小输出必须一个字节不少——别为了限流把尾巴丢了。"""
        jid = _start("printf 'alpha-beta-gamma'")
        job, timed_out = _wait(jid, timeout=30)
        self.assertEqual(timed_out, 0)
        self.assertEqual(job["exit"], 0)
        text, _ = agent.t_exec_poll({"job_id": jid, "offset": 0})
        self.assertIn("alpha-beta-gamma", text)


if __name__ == "__main__":
    unittest.main()
