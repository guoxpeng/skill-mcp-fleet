#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络相关的地址/工具探测：这些错误只在真机上才暴露，本机回环测不出来。

三条都是踩过的真坑：
  * 把 GUI 主程序当 CLI 用 → `tailscale status --json` 既无输出也不报错，
    fleet 误判「未登录」，而 Tailscale 其实跑得好好的
  * 开了 Tailscale 出口节点后，路由探测拿到 100.64/10 的隧道地址，
    广播给局域网等于给了个错地址
  * DHCP 失败时 en0 拿到 169.254.x.x 自配地址，同样谁也连不上
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fleet


class CgnatTest(unittest.TestCase):
    def test_inside_range(self):
        self.assertTrue(fleet._is_cgnat("100.64.0.1"))
        self.assertTrue(fleet._is_cgnat("100.85.51.14"))
        self.assertTrue(fleet._is_cgnat("100.127.255.254"))

    def test_outside_range(self):
        self.assertFalse(fleet._is_cgnat("100.63.255.255"))
        self.assertFalse(fleet._is_cgnat("100.128.0.1"))
        self.assertFalse(fleet._is_cgnat("192.168.1.5"))
        self.assertFalse(fleet._is_cgnat("10.0.0.1"))

    def test_garbage(self):
        self.assertFalse(fleet._is_cgnat(""))
        self.assertFalse(fleet._is_cgnat("not-an-ip"))


IFCONFIG_SAMPLE = """
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1%en0 prefixlen 64 secured scopeid 0x4
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
utun9: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.85.51.14 --> 100.85.51.14 netmask 0xffffffff
en5: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
\tinet 169.254.10.9 netmask 0xffff0000 broadcast 169.254.255.255
"""


class IfaceFallbackTest(unittest.TestCase):
    def test_picks_routable_address_not_cgnat(self):
        with mock.patch.object(fleet, "_sh", return_value=(0, IFCONFIG_SAMPLE)):
            self.assertEqual(fleet._iface_ipv4_fallback(), "192.168.1.23")

    def test_skips_link_local_when_only_that_exists(self):
        only_ll = "en5: flags=8863\n\tinet 169.254.10.9 netmask 0xffff0000\n"
        with mock.patch.object(fleet, "_sh", return_value=(0, only_ll)):
            self.assertEqual(fleet._iface_ipv4_fallback(), "")

    def test_no_output_returns_empty(self):
        with mock.patch.object(fleet, "_sh", return_value=(1, "")):
            self.assertEqual(fleet._iface_ipv4_fallback(), "")

    def test_ip_addr_style_parsed(self):
        """Linux 的 `ip -4 -o addr` 是 `inet 10.1.2.3/24`，带掩码。"""
        out = "2: eth0    inet 10.1.2.3/24 brd 10.1.2.255 scope global eth0\n"
        calls = {"n": 0}

        def fake_sh(cmd, timeout=5):
            calls["n"] += 1
            # 第一次（ifconfig）没有 inet 行，第二次（ip）给结果
            return (0, out) if cmd[0] == "ip" else (0, "eth0: no inet here\n")

        with mock.patch.object(fleet, "_sh", side_effect=fake_sh):
            self.assertEqual(fleet._iface_ipv4_fallback(), "10.1.2.3")


class TailscaleBinTest(unittest.TestCase):
    GUI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    CLI = "/Applications/Tailscale.app/Contents/MacOS/tailscale"

    def test_prefers_cli_over_gui_when_both_exist(self):
        """App 包里两个二进制都在，但只有小写的那个能当 CLI 用。"""
        def fake_isfile(p):
            return p in (self.GUI, self.CLI)

        def fake_probe(exe, timeout=4):
            return exe == self.CLI

        with mock.patch.object(fleet.shutil, "which", return_value=None), \
                mock.patch.object(fleet.os.path, "isfile", side_effect=fake_isfile), \
                mock.patch.object(fleet, "_ts_probe", side_effect=fake_probe):
            self.assertEqual(fleet._tailscale_bin(), self.CLI)

    def test_falls_back_to_first_found_when_none_respond(self):
        """都不响应时仍返回一个，让上层报具体错误，而不是「未安装」。"""
        def fake_isfile(p):
            return p in (self.GUI, self.CLI)

        with mock.patch.object(fleet.shutil, "which", return_value=None), \
                mock.patch.object(fleet.os.path, "isfile", side_effect=fake_isfile), \
                mock.patch.object(fleet, "_ts_probe", return_value=False):
            got = fleet._tailscale_bin()
            self.assertIn(got, (self.CLI, self.GUI))

    def test_returns_none_when_nothing_exists(self):
        with mock.patch.object(fleet.shutil, "which", return_value=None), \
                mock.patch.object(fleet.os.path, "isfile", return_value=False):
            self.assertIsNone(fleet._tailscale_bin())


class TailscaleStatusMessageTest(unittest.TestCase):
    def test_empty_output_lists_three_causes(self):
        """空输出的三种原因都长一样，提示里必须分开讲，否则用户只会反复 tailscale up。"""
        class R(object):
            stdout = ""
            stderr = ""

        with mock.patch.object(fleet, "_tailscale_bin", return_value="/x/tailscale"), \
                mock.patch.object(fleet.subprocess, "run", return_value=R()):
            st, msg = fleet._tailscale_status()
        self.assertIsNone(st)
        self.assertIn("未登录", msg)
        self.assertIn("CLI", msg)
        self.assertIn("沙盒", msg)

    def test_not_running_backend_state(self):
        class R(object):
            stdout = '{"BackendState": "Stopped"}'
            stderr = ""

        with mock.patch.object(fleet, "_tailscale_bin", return_value="/x/tailscale"), \
                mock.patch.object(fleet.subprocess, "run", return_value=R()):
            st, msg = fleet._tailscale_status()
        self.assertIsNone(st)
        self.assertIn("Stopped", msg)


if __name__ == "__main__":
    unittest.main()
