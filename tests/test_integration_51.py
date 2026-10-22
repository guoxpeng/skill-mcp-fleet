# -*- coding: utf-8 -*-
"""v5.2 端到端集成测试：真实起节点进程，验证协议/提示词/策略/签名/审计/只读。

与 tests/test_agent_integration.py（v4 老协议）互补：
本文件覆盖 v5.2 新增的 Ed25519 签名请求、防重放、执行策略、哈希链审计、
MCP instructions/prompts 与运维工具。全部用临时目录，不碰用户数据。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from fleet_core import Identity, sign_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
NODE_FILES = ["mcp_agent.py", "fleet_proto.py", "dashboard_html.py",
              "fleet_core.py", "fleet_agent.py",
              "mcp_agent_config.example.json"]


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_health(base, timeout=20):
    """等到 HTTP 服务起来；401/403 也算起来了（可能已开鉴权）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class Node(object):
    def __init__(self, tmpdir, name="it51", read_only=False, token="",
                 host="127.0.0.1", auto_token=False):
        self.dir = tmpdir
        self.host = host
        self.token = token
        self.auto_token = auto_token
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        for fn in NODE_FILES:
            shutil.copy2(os.path.join(SCRIPTS, fn), os.path.join(tmpdir, fn))
        cfg = {
            "name": name, "port": self.port, "data_dir": os.path.join(tmpdir, "data"),
            "auth_token": token, "audit_log": True, "read_only": read_only,
            "agent_autonomy": "local",
        }
        with open(os.path.join(tmpdir, "mcp_agent_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cfg, f)
        env = dict(os.environ)
        env["PYTHONPATH"] = SCRIPTS
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(tmpdir, "mcp_agent.py"),
             "--host", self.host, "--port", str(self.port)],
            cwd=tmpdir, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        assert wait_health(self.base), "节点未在 20s 内启动"
        if auto_token:
            cfg = json.load(open(os.path.join(tmpdir, "mcp_agent_config.json")))
            self.token = str(cfg.get("auth_token") or "")
            assert self.token, "自动鉴权未生成 token"
        self.sid = None

    def rpc(self, method, params=None, headers=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}}).encode("utf-8")
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.token:
            hdrs["Authorization"] = "Bearer " + self.token
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + "/mcp", data=body, method="POST",
                                     headers=hdrs)
        if self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
            return resp.status, json.loads(raw)

    def init(self):
        return self.rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "pytest-v51", "version": "1"}})[1]["result"]

    def tool(self, name, args=None):
        status, data = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        result = data.get("result", {})
        text = (result.get("content") or [{}])[0].get("text", "")
        return result.get("isError"), text

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        tok = self.token if token is None else token
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


@pytest.fixture()
def node(tmp_path):
    n = Node(str(tmp_path))
    yield n
    n.stop()


@pytest.fixture()
def ro_node(tmp_path):
    n = Node(str(tmp_path), name="ro", read_only=True)
    yield n
    n.stop()


# --------------------------------------------------------------------------
# MCP 能力：instructions / prompts / tools
# --------------------------------------------------------------------------
class TestMcpV51:
    def test_initialize_instructions(self, node):
        res = node.init()
        assert res["serverInfo"]["version"].startswith("5.2")
        instructions = res.get("instructions", "")
        assert len(instructions) > 500
        for key in ("观察", "计划", "执行", "fleet_plan", "安全"):
            assert key in instructions, "instructions 缺少关键指引: %s" % key
        assert "prompts" in res["capabilities"]

    def test_prompts_list_and_get(self, node):
        node.init()
        data = node.rpc("prompts/list", {})[1]["result"]["prompts"]
        names = {p["name"] for p in data}
        assert {"fleet-playbook", "fleet-diagnose", "fleet-plan",
                "fleet-self-review", "fleet-delegate", "fleet-incident",
                "fleet-onboard"} <= names
        got = node.rpc("prompts/get", {"name": "fleet-plan",
                                       "arguments": {"goal": "检查磁盘"}})[1]["result"]
        assert got["messages"] and got["messages"][0]["content"]["text"]

    def test_tools_include_v51(self, node):
        node.init()
        tools = node.rpc("tools/list", {})[1]["result"]["tools"]
        assert len(tools) == 56
        names = {t["name"] for t in tools}
        assert {"fleet_help", "fleet_plan", "fleet_reflect", "fleet_review",
                "fleet_handoff", "fleet_doctor", "fleet_identity",
                "fleet_metrics", "fleet_audit_verify"} <= names

    def test_identity_tool_matches_disk(self, node):
        node.init()
        err, text = node.tool("fleet_identity")
        assert not err
        data = json.loads(text)
        ident = json.load(open(os.path.join(node.dir, "data", "identity.json")))
        assert data["node_id"] == ident["node_id"]
        assert data["fingerprint"] == ident["fingerprint"]
        assert data["trust"] == "S2/S3"

    def test_doctor_and_metrics(self, node):
        node.init()
        err, text = node.tool("fleet_doctor")
        assert not err, text
        report = json.loads(text)
        assert report["checks"] and "summary" in report
        node.tool("exec", {"command": "echo metrics-seed"})
        status, body = node.get("/fleet/metrics")
        assert status == 200 and "fleet_tool_calls_total" in body

    def test_health_and_identity_endpoints(self, node):
        status, body = node.get("/fleet/health")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok" and data["node_id"]
        status, body = node.get("/fleet/identity")
        assert status == 200 and json.loads(body)["public_key"]


# --------------------------------------------------------------------------
# 执行策略
# --------------------------------------------------------------------------
class TestPolicy:
    def test_deny_rm_root(self, node):
        node.init()
        err, text = node.tool("exec", {"command": "rm -rf /", "confirm": True})
        assert err and "执行策略拒绝" in text and "rm_root" in text

    def test_deny_mkfs(self, node):
        node.init()
        err, text = node.tool("exec", {"command": "mkfs.ext4 /dev/sda1"})
        assert err and "执行策略拒绝" in text

    def test_ask_without_confirm(self, node):
        node.init()
        err, text = node.tool("exec", {"command": "reboot"})
        assert err and "需要用户确认" in text
        assert "confirm=true" in text

    def test_normal_command_allowed(self, node):
        node.init()
        err, text = node.tool("exec", {"command": "echo fleet-ok"})
        assert not err and "fleet-ok" in text


# --------------------------------------------------------------------------
# 签名请求 / 防重放
# --------------------------------------------------------------------------
class TestSignedRequests:
    def _peer(self, node, tmp_path):
        ident = Identity.load_or_create(str(tmp_path / "peer-identity.json"))
        peers = {"nodes": {"peer-x": {
            "url": "http://127.0.0.1:%d/mcp" % node.port,
            "token": "", "public_key": ident.public_key_b64,
            "node_id": ident.node_id, "fingerprint": ident.fingerprint}}}
        with open(os.path.join(node.dir, "peers.json"), "w", encoding="utf-8") as f:
            json.dump(peers, f)
        return ident

    def test_unknown_node_rejected(self, node, tmp_path):
        ident = Identity.load_or_create(str(tmp_path / "stranger.json"))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {}}).encode()
        headers = sign_request(ident, "POST", "/mcp", body)
        with pytest.raises(urllib.error.HTTPError) as exc:
            node.rpc("initialize", {}, headers=headers)
        assert exc.value.code == 401

    def test_signature_accepted_replay_rejected(self, node, tmp_path):
        ident = self._peer(node, tmp_path)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05"}}).encode()
        headers = sign_request(ident, "POST", "/mcp", body)
        req = urllib.request.Request(
            node.base + "/mcp", data=body, method="POST",
            headers=dict(headers, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
        # 重放：同一 nonce 第二次必须 401
        req2 = urllib.request.Request(
            node.base + "/mcp", data=body, method="POST",
            headers=dict(headers, **{"Content-Type": "application/json"}))
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req2, timeout=10)
        assert exc.value.code == 401

    def test_tampered_signature_rejected(self, node, tmp_path):
        ident = self._peer(node, tmp_path)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {}}).encode()
        headers = sign_request(ident, "POST", "/mcp", body)
        sig = headers["X-Fleet-Sig"]
        headers["X-Fleet-Sig"] = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
        with pytest.raises(urllib.error.HTTPError) as exc:
            node.rpc("initialize", {}, headers=headers)
        assert exc.value.code == 401


# --------------------------------------------------------------------------
# 审计 / 只读
# --------------------------------------------------------------------------
class TestAuditAndReadOnly:
    def test_audit_chain_valid_after_calls(self, node):
        node.init()
        node.tool("exec", {"command": "echo audit-1"})
        node.tool("exec", {"command": "echo audit-2"})
        err, text = node.tool("fleet_audit_verify", {"tail": 5})
        assert not err
        report = json.loads(text)
        assert report["ok"] is True and report["tail"]

    def test_read_only_blocks_mutations(self, ro_node):
        ro_node.init()
        err, text = ro_node.tool("write", {"filePath": "/tmp/v51.txt", "content": "x"})
        assert err and "只读" in text
        err, text = ro_node.tool("fleet_token_rotate", {"confirm": ro_node.dir})
        assert err and "只读" in text
        err, text = ro_node.tool("fleet_handoff", {"to": "b", "goal": "x"})
        assert err and "只读" in text

    def test_read_only_allows_queries(self, ro_node):
        ro_node.init()
        err, text = ro_node.tool("fleet_identity")
        assert not err and "node_id" in text
        err, text = ro_node.tool("fleet_doctor")
        assert not err


# --------------------------------------------------------------------------
# v5.2：Dashboard 数据/设备管理/任务时间线/默认鉴权
# --------------------------------------------------------------------------
class TestDashboardV52:
    def test_dashboard_payload_fields(self, node):
        node.init()
        status, body = node.get("/fleet/ui/data")
        assert status == 200
        data = json.loads(body)
        assert data["identity"]["fingerprint"]
        assert data["security"]["audit_ok"] is True
        assert "pending" in data["delivery"] and "dead" in data["delivery"]
        assert isinstance(data["tasks"], list)
        assert data["peers"] == []

    def test_tasks_endpoint_after_plan(self, node):
        node.init()
        err, _ = node.tool("fleet_plan", {
            "goal": "检查磁盘",
            "steps": [{"goal": "df", "tool": "exec", "assignee": "self",
                       "acceptance": "输出含 /"}]})
        assert not err
        status, body = node.get("/fleet/tasks")
        assert status == 200
        tasks = json.loads(body)["tasks"]
        assert tasks and tasks[-1]["goal"] == "检查磁盘"

    def test_peer_remove_loopback(self, node):
        node.init()
        peers = {"nodes": {"ghost": {"url": "http://127.0.0.1:1/mcp",
                                     "token": "", "public_key": "x"}}}
        with open(os.path.join(node.dir, "peers.json"), "w", encoding="utf-8") as f:
            json.dump(peers, f)
        body = json.dumps({"name": "ghost"}).encode()
        req = urllib.request.Request(node.base + "/fleet/peer_remove", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["ok"] is True
        data = json.loads(node.get("/fleet/ui/data")[1])
        assert data["peers"] == []

    def test_auto_secure_generates_token(self, tmp_path):
        n = Node(str(tmp_path), name="public", host="0.0.0.0", auto_token=True)
        try:
            # 无 token 401，带 token 200
            try:
                n.get("/fleet/health", token="")
                raise AssertionError("未鉴权请求应被拒绝")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            status, body = n.get("/fleet/health")
            assert status == 200 and json.loads(body)["status"] == "ok"
        finally:
            n.stop()

    def test_dashboard_ui_public_data_guarded(self, tmp_path):
        # 页面本身免鉴权（token 走 fragment），数据接口必须鉴权
        n = Node(str(tmp_path), name="dash-auth", token="tok-123456")
        try:
            with urllib.request.urlopen(n.base + "/fleet/ui", timeout=10) as resp:
                assert resp.status == 200
                assert b"Fleet Dashboard" in resp.read()
            try:
                urllib.request.urlopen(n.base + "/fleet/ui/data", timeout=10)
                raise AssertionError("无 token 读取数据应被拒绝")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            status, _body = n.get("/fleet/ui/data")
            assert status == 200
        finally:
            n.stop()

    def test_unknown_tool_guidance(self, node):
        node.init()
        err, text = node.tool("no_such_tool", {})
        assert err and "下一步" in text and "tools/list" in text
