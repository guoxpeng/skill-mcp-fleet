# -*- coding: utf-8 -*-
"""mcp_agent 工具表完整性测试（import 安全：load_config 只读无副作用）。"""
import os
import pytest

mp = pytest.importorskip("mcp_agent", reason="mcp_agent 导入失败")


class TestToolRegistry:
    def test_tool_count(self):
        assert len(mp.TOOLS) == 53

    def test_tool_schema_structure(self):
        """每个工具必须有 name/description/inputSchema(dict)。"""
        for t in mp.TOOLS:
            assert isinstance(t["name"], str) and t["name"], t
            assert isinstance(t["description"], str) and t["description"]
            assert isinstance(t["inputSchema"], dict)
            assert t["inputSchema"].get("type") == "object"

    def test_no_duplicate_names(self):
        names = [t["name"] for t in mp.TOOLS]
        assert len(names) == len(set(names))

    def test_every_tool_has_impl(self):
        """每个注册工具都有可调用实现（TOOL_FUNCS 全覆盖）。"""
        missing = [t["name"] for t in mp.TOOLS
                   if t["name"] not in mp.TOOL_FUNCS]
        assert missing == []

    def test_version_is_51(self):
        assert mp.SERVER_VERSION.startswith("5.2")

    def test_core_tools_present(self):
        must = {"exec", "read", "write", "edit", "list_dir",
                "msg_send", "msg_recv", "peers", "peer_call", "mesh",
                "delegate", "caps", "mesh_caps", "sysinfo", "search"}
        have = set(mp.TOOL_FUNCS.keys())
        assert must <= have, "缺少: %s" % (must - have)

    def test_backcompat_nas_prefix(self):
        """旧版 nas_ 前缀名仍可解析（向后兼容承诺）。"""
        assert "nas_exec" in mp.TOOL_FUNCS
        assert mp.TOOL_FUNCS["nas_exec"] is mp.TOOL_FUNCS["exec"]


# ===========================================================================
# v5.0.3 连接/长期运行稳定性：mail 限流 + audit 轮转
# ===========================================================================
class TestMailRateLimit:
    def test_bucket_allows_then_blocks(self, monkeypatch):
        """独立令牌桶：cap 内放行，超 cap 拒绝；cap<=0 不限。"""
        monkeypatch.setattr(mp, "CFG", {"mail_rate_per_min": 3})
        monkeypatch.setattr(mp, "_mail_rate_bucket", {"tokens": 0, "last": 0})
        results = [mp.rate_limit_mail() for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_disabled_when_zero(self, monkeypatch):
        monkeypatch.setattr(mp, "CFG", {"mail_rate_per_min": 0})
        assert all(mp.rate_limit_mail() for _ in range(100))

    def test_independent_from_tool_bucket(self, monkeypatch):
        """mail 桶与工具层桶独立：工具桶打满不影响 mail 桶。"""
        monkeypatch.setattr(mp, "CFG", {"rate_limit_per_min": 1, "mail_rate_per_min": 5})
        monkeypatch.setattr(mp, "_rate_bucket", {"tokens": 0, "last": 0})
        monkeypatch.setattr(mp, "_mail_rate_bucket", {"tokens": 0, "last": 0})
        assert mp.rate_limit_allow() is True      # 工具桶第 1 个
        assert mp.rate_limit_allow() is False     # 工具桶空
        assert mp.rate_limit_mail() is True       # mail 桶不受影响


class TestAuditRotation:
    def test_rotates_when_over_limit(self, tmp_path, monkeypatch):
        """超过阈值轮转为 .1；小文件不动。"""
        audit = tmp_path / "audit.log"
        audit.write_text("x" * 300)
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        mp._audit_rotate_if_needed(max_bytes=100)
        assert not audit.exists()                       # 原文件被移走
        assert (tmp_path / "audit.log.1").read_text() == "x" * 300

    def test_no_rotate_when_small(self, tmp_path, monkeypatch):
        audit = tmp_path / "audit.log"
        audit.write_text("x" * 50)
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        mp._audit_rotate_if_needed(max_bytes=100)
        assert audit.exists() and audit.read_text() == "x" * 50
        assert not (tmp_path / "audit.log.1").exists()

    def test_audit_log_writes_and_rotates(self, tmp_path, monkeypatch):
        """完整路径：写日志 -> 超限自动轮转 -> 继续写新文件。"""
        audit = tmp_path / "audit.log"
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        monkeypatch.setattr(mp, "CFG", {"audit_log": True})
        monkeypatch.setattr(mp, "AUDIT_ROTATE_BYTES", 100)   # 缩小阈值便于测试
        # 预置一个超限文件
        audit.write_text("old" * 100)
        mp.audit_log("peer_call", "a", "b", "exec", "ok")   # 应触发轮转并写入新文件
        assert (tmp_path / "audit.log.1").read_text() == "old" * 100
        assert "peer_call" in audit.read_text()
# -*- coding: utf-8 *


class TestPlatformGuard:
    """v5.0.4 平台守护：launchd plist / schtasks 构造 / 看门狗（纯逻辑，跨平台可测）。"""

    inst = pytest.importorskip("install", reason="install 导入失败")

    def test_sanitize_label(self):
        s = self.inst
        assert s.sanitize_label("office pc.1") == "office-pc-1"
        assert s.sanitize_label("") == "node"
        assert s.sanitize_label("a" * 100) == "a" * 40  # 截断防超长 Label

    def test_launchd_plist_structure(self):
        s = self.inst
        p = s.build_launchd_plist("office-pc", "/usr/bin/python3",
                                  "/Users/x/.mcp-fleet", 3100)
        for key in ("<key>RunAtLoad</key>", "<key>KeepAlive</key>",
                    "<key>ThrottleInterval</key>", "<key>WorkingDirectory</key>"):
            assert key in p, key
        assert "<string>com.mcp-fleet.office-pc</string>" in p
        assert "--port" in p and "<string>3100</string>" in p  # 端口进了 ProgramArguments
        assert "<true/>" in p  # RunAtLoad/KeepAlive 布尔真

    def test_launchd_plist_escapes_xml(self):
        """安装路径含 & < 时 plist 仍是合法 XML。"""
        s = self.inst
        p = s.build_launchd_plist("n", "/usr/bin/python3", '/Users/a&b/c<d', 3100)
        assert '<string>/Users/a&amp;b/c&lt;d</string>' in p

    def test_schtasks_onlogon_cmd(self):
        s = self.inst
        c = s.build_schtasks_onlogon("C:\\py\\pythonw.exe", "C:\\fleet", 3100)
        assert c[0] == "schtasks" and "/SC" in c and "ONLOGON" in c
        tr = c[-1]
        assert "pythonw" in tr and "mcp_agent.py" in tr and "3100" in tr

    def test_schtasks_watchdog_cmd(self):
        s = self.inst
        c = s.build_schtasks_minute("C:\\py\\python.exe", "C:\\fleet", 3100)
        assert "MINUTE" in c and "/MO" in c and c[c.index("/MO") + 1] == "1"
        tr = c[-1]
        assert "--watchdog" in tr and "install.py" in tr and "--port 3100" in tr

    def test_netstat_pid_parsing(self):
        s = self.inst
        sample = "\n".join([
            "  TCP    0.0.0.0:3100           0.0.0.0:0              LISTENING       12345",
            "  TCP    [::]:3100              [::]:0                 LISTENING       12345",
            "  TCP    192.168.1.5:3100       10.0.0.2:8080          ESTABLISHED     12345",
            "  TCP    0.0.0.0:3101           0.0.0.0:0              LISTENING       99999",
        ])
        assert s.parse_netstat_pids(sample, 3100) == [12345]  # v4+v5.2 去重，排除 ESTABLISHED
        assert s.parse_netstat_pids(sample, 3101) == [99999]
        assert s.parse_netstat_pids(sample, 3102) == []

    def test_tasklist_python_filter(self):
        s = self.inst
        tl = "\n".join([
            "Image Name                     PID Session Name",
            "========================= ======== ================",
            "svchost.exe                  12345 Services",
        ])
        assert s.parse_tasklist_image(tl) == "svchost.exe"
        assert s.is_python_image("python.exe") and s.is_python_image("pythonw.exe")
        assert s.is_python_image("python3.11.exe")  # 版本号后缀也算
        assert not s.is_python_image("svchost.exe")  # 非 python 映像不杀

    def test_probe_agent_verifies_json(self):
        """探活必须校验 /health 的 JSON 结构：200 但非 agent 结构 -> 不算健康（防误判）。"""
        import threading, http.server, json
        s = self.inst

        class AgentLike(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = json.dumps({"status": "ok", "server": "x",
                                "version": "5.0.4"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        class Stranger(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = b"<html>welcome</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        srvs = []
        for handler, expect in ((AgentLike, True), (Stranger, False)):
            srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            assert s.probe_agent(srv.server_address[1], timeout=3) is expect
            srvs.append(srv)
        assert s.probe_agent(1, timeout=0.5) is False  # 连接失败 -> 不健康
        for srv in srvs:
            srv.shutdown()

    def test_watchdog_healthy_noop(self, tmp_path):
        s = self.inst
        calls = []
        # 裸奔模式（无 auth_token 配置文件）-> 匿名探活
        act = s.watchdog_once(3100, str(tmp_path),
                              probe=lambda p, timeout=5.0, token=None: True,
                              spawn=lambda *a: calls.append(a),
                              kill=lambda p: calls.append(("kill", p)))
        assert act is None and calls == []  # 健康时零动作

    def test_watchdog_reads_token_for_probe(self, tmp_path):
        """开鉴权节点：watchdog 自动读 auth_token 带凭据探活（匿名 /health 是 401）。"""
        s = self.inst
        import json as _json
        (tmp_path / "mcp_agent_config.json").write_text(
            _json.dumps({"auth_token": "TOK123"}))
        seen = {}

        def fake_probe(p, timeout=5.0, token=None):
            seen["token"] = token
            return True

        s.watchdog_once(3100, str(tmp_path), probe=fake_probe,
                        spawn=lambda *a: None, kill=lambda p: [])
        assert seen["token"] == "TOK123"  # 从安装目录配置读出并传给探活

    def test_watchdog_unhealthy_restarts(self, tmp_path):
        s = self.inst
        calls = []
        act = s.watchdog_once(3100, str(tmp_path),
                              probe=lambda p, timeout=5.0, token=None: False,
                              spawn=lambda *a: calls.append(a),
                              kill=lambda p: [777])
        assert act == {"restarted": True, "killed": [777]}
        assert len(calls) == 1  # 杀残留 -> 拉起，各一次
# -*- coding: utf-8 *


class TestWinHardening:
    """v5.0.5 Windows 专项加固：系统代理豁免 / pythonw 回退链 / 命令容错。"""

    inst = pytest.importorskip("install", reason="install 导入失败")

    def test_probe_agent_ignores_system_proxy(self):
        """代理软件环境（系统代理未豁免回环）下探活不再被劫持——防看门狗重启风暴。"""
        import threading, http.server, json, urllib.request
        s = self.inst

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = json.dumps({"status": "ok", "server": "x",
                                "version": "5.0.5"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        # 安装指向死端口的假系统代理（模拟 Windows 代理软件未设 <local> 豁免）
        urllib.request.install_opener(urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": "http://127.0.0.1:1"})))
        try:
            assert s.probe_agent(port, timeout=3) is True  # 修复后：禁代理直连
            # 全链路：健康时 watchdog 零动作（不再误判宕机重启）
            import tempfile, os
            with tempfile.TemporaryDirectory() as td:
                act = s.watchdog_once(port, td)
            assert act is None
        finally:
            srv.shutdown()
            urllib.request.install_opener(urllib.request.build_opener())  # 还原全局

    def test_opener_for_private_direct(self, monkeypatch):
        """私网/回环目标禁代理：真实服务 + 假系统代理下直连成功，默认 opener 被劫持（对照）。"""
        import threading, http.server, urllib.request
        fp = pytest.importorskip("fleet_proto", reason="fleet_proto 导入失败")

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/x" % srv.server_address[1]
        # 模拟 Windows 代理软件：系统代理指向死端口（未豁免回环）
        monkeypatch.setattr(urllib.request, "getproxies",
                            lambda: {"http": "http://127.0.0.1:1"})
        try:
            # 正向：_opener_for 对回环直连（ProxyHandler({}) 空代理语义）
            with fp._opener_for(url).open(url, timeout=3) as r:
                assert r.read() == b"ok"
            # 反向对照：默认 opener（读 getproxies）同请求被假代理劫持
            default = urllib.request.build_opener()
            with pytest.raises(Exception):
                default.open(url, timeout=3)
        finally:
            srv.shutdown()

    def test_opener_for_public_keeps_proxy(self, monkeypatch):
        """公网地址仍走系统代理（隧道/中转场景需要）。"""
        import urllib.request
        fp = pytest.importorskip("fleet_proto", reason="fleet_proto 导入失败")
        monkeypatch.setattr(urllib.request, "getproxies",
                            lambda: {"http": "http://1.2.3.4:8080"})
        opener = fp._opener_for("http://example.com/mcp")
        # 功能断言：公网 opener 的请求会尝试经 1.2.3.4:8080（不可达 -> 连接错误）
        try:
            opener.open("http://example.com/mcp", timeout=3)
            raised = None
        except Exception:
            raised = True
        assert raised is not None  # 走了假代理（而非直连成功）

    def test_win_find_pythonw_venv_fallback(self, monkeypatch):
        """venv（Scripts 下无 pythonw.exe）回退到 base_prefix 找，防登录闪黑窗。"""
        s = self.inst
        import sys as _sys
        monkeypatch.setattr(s.sys, "executable", "/venv/Scripts/python.exe")
        monkeypatch.setattr(_sys, "executable", "/venv/Scripts/python.exe")
        monkeypatch.setattr(_sys, "base_prefix", "/real/py")
        monkeypatch.setattr(_sys, "base_exec_prefix", "/real/py")

        def fake_isfile(p):
            return p == "/real/py/pythonw.exe"  # venv 无，真实目录有

        monkeypatch.setattr(s.os.path, "isfile", fake_isfile)
        assert s.win_find_pythonw() == "/real/py/pythonw.exe"

    def test_win_find_pythonw_final_fallback(self, monkeypatch):
        """都找不到时回退当前解释器（行为不崩）。"""
        s = self.inst
        import sys as _sys
        monkeypatch.setattr(_sys, "executable", "/venv/Scripts/python.exe")
        monkeypatch.setattr(_sys, "base_prefix", "/real/py")
        monkeypatch.setattr(s.os.path, "isfile", lambda p: False)
        assert s.win_find_pythonw() == "/venv/Scripts/python.exe"

    def test_run_cmd_decodes_leniently(self):
        """_run_cmd 正常命令返回 rc=0 且输出可读（errors=replace 不抛解码异常）。"""
        s = self.inst
        rc, out = s._run_cmd(["echo", "hello-fleet"])
        assert rc == 0 and "hello-fleet" in out



class TestTermuxInstall:
    """v5.2 安装链路：POSIX 薄引导 + 统一安装器识别 Termux。"""

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_install_sh_is_posix_bootstrap(self):
        src = open(os.path.join(self.root, "scripts", "install.sh"),
                   encoding="utf-8").read()
        assert src.startswith("#!/bin/sh") or src.startswith("#!/usr/bin/env sh")
        assert "fleet_install.py" in src
        assert "SHA256SUMS" in src or "sha256" in src.lower()
        assert "set -e" in src

    def test_install_sh_has_offline_mode(self):
        src = open(os.path.join(self.root, "scripts", "install.sh"),
                   encoding="utf-8").read()
        assert "--offline" in src
        assert "--dry-run" in src

    def test_install_sh_syntax(self):
        import subprocess
        r = subprocess.run(["sh", "-n", os.path.join(self.root, "scripts", "install.sh")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_fleet_install_termux_adapter(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fleet_install", os.path.join(self.root, "scripts", "fleet_install.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "termux" in mod.ADAPTERS
        assert "systemd" in mod.ADAPTERS and "procd" in mod.ADAPTERS

    def test_installer_has_join_flag(self):
        src = open(os.path.join(self.root, "scripts", "fleet_install.py"),
                   encoding="utf-8").read()
        assert '"--join"' in src or "'--join'" in src
        assert "fleet://join/" in src


class TestReadOnlyMode:
    """v5.0.7: read_only 只读模式 —— 变更类工具拒绝、查询类工具放行。"""

    def _set(self, v):
        mp.CFG["read_only"] = v

    def test_exec_blocked_when_read_only(self):
        self._set(True)
        try:
            r = mp.call_tool("exec", {"command": "true"})
            assert r["isError"] and "只读" in r["content"][0]["text"]
        finally:
            self._set(False)
        # 关闭后恢复正常
        r = mp.call_tool("exec", {"command": "true"})
        assert not r["isError"]

    def test_write_edit_and_ops_blocked(self):
        self._set(True)
        try:
            assert mp.call_tool("write", {"filePath": "/tmp/x", "content": "1"})["isError"]
            assert mp.call_tool("edit", {"filePath": "/tmp/x", "oldText": "a", "newText": "b"})["isError"]
            assert mp.call_tool("upload", {"filePath": "/tmp/x", "content": "YQ=="})["isError"]
            assert mp.call_tool("systemctl", {"action": "restart", "unit": "x"})["isError"]
            assert mp.call_tool("docker_restart", {"name": "x"})["isError"]
            assert mp.call_tool("exec_async", {"command": "true"})["isError"]
            assert mp.call_tool("exec_kill", {"job_id": "1"})["isError"]
            assert mp.call_tool("kill", {"pid": 1})["isError"]
            assert mp.call_tool("msg_send", {"to": "y", "text": "z"})["isError"]
            assert mp.call_tool("peer_call", {})["isError"]
            assert mp.call_tool("delegate", {})["isError"]
            assert mp.call_tool("group_form", {})["isError"]
        finally:
            self._set(False)

    def test_query_tools_allowed(self):
        self._set(True)
        try:
            assert not mp.call_tool("read", {"filePath": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")})["isError"]
            assert not mp.call_tool("list_dir", {"filePath": "/"})["isError"]
            assert not mp.call_tool("sysinfo", {})["isError"]
            assert not mp.call_tool("mesh_caps", {})["isError"]
        finally:
            self._set(False)

    def test_unknown_tool_still_unknown(self):
        self._set(True)
        try:
            r = mp.call_tool("no_such_tool", {})
            assert r["isError"] and "未知工具" in r["content"][0]["text"]
        finally:
            self._set(False)

    def test_prefixed_call_blocked(self):
        """带 PREFIX 的调用同样被拦截（如 nas_exec）。"""
        prefix = mp.PREFIX or ""
        if not prefix:
            pytest.skip("PREFIX 为空，无前缀调用场景")
        self._set(True)
        try:
            r = mp.call_tool(prefix + "exec", {"command": "true"})
            assert r["isError"] and "只读" in r["content"][0]["text"]
        finally:
            self._set(False)

    def test_lists_full_toolchain_even_read_only(self):
        """tools/list 仍返回完整 41 工具（read-only 靠调用层拦截，工具数声明不变）。"""
        self._set(True)
        try:
            assert len(mp.TOOLS) == 53
        finally:
            self._set(False)

    def test_legacy_nas_prefix_blocked(self, tmp_path):
        """v5.0.8 回归：旧版 nas_ 前缀调用同样被拦截。

        v5.0.7 前缀剥离只认 PREFIX，而 TOOL_FUNCS 同时注册了旧版
        nas_ 前缀名（nas_write 等仍为有效工具）——用 nas_write 可
        完全绕过只读拦截，实测 read_only 下真实写入了文件。"""
        target = str(tmp_path / "bypass.txt")
        self._set(True)
        try:
            r = mp.call_tool("nas_write", {"filePath": target, "content": "x"})
            assert r["isError"] and "只读" in r["content"][0]["text"]
            assert not os.path.exists(target)   # 文件绝不能被写入
            r2 = mp.call_tool("nas_exec", {"command": "true"})
            assert r2["isError"] and "只读" in r2["content"][0]["text"]
        finally:
            self._set(False)


# ---------------------------------------------------------------------------
# v5.0.9: CLI 完整性回归守卫（与 test_agent_tools.py 合并以保持 zip 30 文件上限）
# 守卫本轮排查发现的 CLI 级历史缺陷，防止复发。
# ---------------------------------------------------------------------------
import argparse as _argparse
import sys as _sys

_INTEGRITY_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _INTEGRITY_SCRIPTS not in _sys.path:
    _sys.path.insert(0, _INTEGRITY_SCRIPTS)


def _src(name):
    with open(os.path.join(_INTEGRITY_SCRIPTS, name), encoding="utf-8") as f:
        return f.read()


class TestCliIntegrity:
    def test_no_undefined_resolve_target(self):
        """fleet.py 不得调用从未定义的 _resolve_target（v5.0.9 修复）。

        该函数全仓库无 def，fleet msg 一执行即 NameError，功能自 v4.1 起
        完全不可用；cmd_msg 已改用 resolve_target（注释中提及名称无妨，
        但不得存在调用形式 `_resolve_target(target` 或定义）。"""
        s = _src("fleet.py")
        assert "_resolve_target(target" not in s
        assert "def _resolve_target" not in s
        assert "def resolve_target(" in s

    def test_uuid_imported(self):
        """fleet.py 必须导入 uuid（invite/verify 确权路径使用 uuid4）。"""
        s = _src("fleet.py")
        assert "\nimport uuid" in s
        assert "uuid.uuid4()" in s

    def test_discovery_layer_global_in_main(self):
        """main() 必须把 _discovery_layer 声明为 global（v4.0 起的历史 bug）。

        缺少声明时 `_discovery_layer = DiscoveryLayer(...)` 只创建局部变量，
        模块级引用（_dashboard_data 读取）永远是 None。"""
        s = _src("mcp_agent.py")
        assert "global LISTEN_PORT, _discovery_layer" in s

    def test_no_duplicate_collections_import(self):
        """fleet_proto.py 不得重复 import collections（独立重复行已删除）。"""
        s = _src("fleet_proto.py")
        assert "\nimport collections\n" not in s
        assert ", collections" in s   # 仍以组合 import 形式使用

    def test_cmd_msg_runs_with_mocked_client(self, monkeypatch):
        """cmd_msg 在 mock 客户端下完整走通 msg send 分支（回归：NameError）。"""
        fleet = pytest.importorskip("fleet")
        calls = []

        class FakeClient:
            def __init__(self, url, timeout, token=""):
                calls.append(("init", url, token))

            def handshake(self):
                calls.append(("handshake",))

            def call(self, name, args):
                calls.append(("call", name, args))
                return ("OK", False)

        monkeypatch.setattr(fleet, "MCPClient", FakeClient, raising=False)
        monkeypatch.setattr(
            fleet, "resolve_target",
            lambda t, agent=None, port=3100: ("http://127.0.0.1:3100/mcp", {"token": "tok"}),
            raising=False)

        a = _argparse.Namespace(target="node1", agent="auto", port=3100, timeout=30,
                               msg_cmd="send", frm="me", to="you", text="hi", topic="")
        rc = fleet.cmd_msg(a)
        assert rc == 0
        assert ("handshake",) in calls
        assert any(c[0] == "call" and c[1] == "msg_send" for c in calls)
        assert ("init", "http://127.0.0.1:3100/mcp", "tok") in calls