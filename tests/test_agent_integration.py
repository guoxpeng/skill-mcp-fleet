# -*- coding: utf-8 -*-
"""HTTP 集成测试：起真实节点进程，覆盖鉴权/邀请码/dashboard/加密落盘/收信。

端口用 39xx 高位段，避免与开发环境冲突。每个用例独立临时目录。
"""
import base64
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
PORT = 3981
BASE = "http://127.0.0.1:%d" % PORT
TOKEN = "it-test-token-123"


def wait_port(port, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def get(path, token=TOKEN, expect_error=False):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code, e.read().decode("utf-8")
        raise


def post(path, obj, token=TOKEN):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(obj).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return e.code, body


@pytest.fixture(scope="module")
def node():
    """起一个带 token 的真实节点（独立临时目录，不污染仓库）。"""
    import tempfile
    workdir = tempfile.mkdtemp(prefix="fleet-it-")
    for f in ("mcp_agent.py", "fleet_proto.py", "dashboard_html.py"):
        os.symlink(os.path.join(SCRIPTS, f), os.path.join(workdir, f))
    with open(os.path.join(workdir, "mcp_agent_config.json"), "w") as fh:
        json.dump({"name": "it-node", "auth_token": TOKEN}, fh)
    p = subprocess.Popen(
        ["python3", "mcp_agent.py", "--port", str(PORT)],
        cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # 端口通了还不够：必须确认是我们起的进程活着（防历史孤儿节点劫持端口）
    if not wait_port(PORT) or p.poll() is not None:
        p.kill()
        out = p.stdout.read().decode()
        pytest.fail("节点未启动: " + out[-500:])
    yield {"proc": p, "dir": workdir}
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


class TestAuth:
    def test_no_token_401(self, node):
        code, _ = get("/fleet/ui/data", token=None, expect_error=True)
        assert code == 401

    def test_wrong_token_401(self, node):
        code, _ = get("/fleet/ui/data", token="evil", expect_error=True)
        assert code == 401

    def test_health_requires_token_when_configured(self, node):
        """配置了 auth_token 的节点：无凭证全路径拦截（含探活）。"""
        code, _ = get("/", token=None, expect_error=True)
        assert code == 401
        code, body = get("/", token=TOKEN)
        assert code == 200 and json.loads(body)["status"] == "ok"


class TestDashboard:
    def test_ui_html(self, node):
        code, html = get("/fleet/ui")
        assert code == 200 and "Fleet Dashboard" in html and "<svg" in html

    def test_ui_data(self, node):
        code, body = get("/fleet/ui/data")
        d = json.loads(body)
        assert d["node"] == "it-node" and d["encryption"] is True
        assert d["version"].startswith("5.0") and d["tools"] == 41
        assert isinstance(d["peers"], list) and "sys" in d


class TestMailboxEncrypted:
    def test_mail_lands_encrypted(self, node):
        post("/fleet/mail", {"from": "agent-a", "to": "it-node",
                             "text": "集成测试机密内容xyz"})
        disk = open(os.path.join(node["dir"], "mailbox", "inbox.jsonl"),
                    encoding="utf-8").read()
        assert "集成测试机密内容xyz" not in disk
        assert disk.startswith("{") and "enc1$" in disk
        # 通过 poll 拉取的是解密后明文
        code, body = get("/fleet/poll?name=it-node")
        msgs = json.loads(body)["messages"]
        assert msgs and msgs[0]["text"] == "集成测试机密内容xyz"


class TestInvite:
    def test_invite_roundtrip(self, node):
        """邀请码生成 -> 解码校验结构 -> verify 端点确权（应成功配对）。"""
        code, resp = post("/fleet/invite", {})
        assert code == 200 and resp.get("invite_code")
        raw = base64.b64decode(resp["invite_code"]).decode("utf-8")
        payload = json.loads(raw)
        assert payload["code"] == "it-node"
        assert payload["invite_id"] and payload["token"] == TOKEN
        assert payload["expires"] > time.time()
        # 模拟 B 节点带邀请码来确权
        code2, resp2 = post("/fleet/invite/verify", {
            "invite_id": payload["invite_id"],
            "name": "b-node",
            "url": "http://127.0.0.1:3999/mcp",
            "token": "b-token",
        })
        assert code2 == 200, resp2
        assert resp2.get("ok") or resp2.get("paired")
        # 已用过的邀请码不可重放（防重放承诺）
        code3, resp3 = post("/fleet/invite/verify", {
            "invite_id": payload["invite_id"],
            "name": "c-node", "url": "http://127.0.0.1:3998/mcp", "token": "",
        })
        assert code3 == 403 and "已使用" in json.dumps(resp3, ensure_ascii=False)


class TestMcpProtocol:
    def test_initialize_and_tools_list(self, node):
        """MCP 握手：initialize -> tools/list 应返回 41 个工具。"""
        code, resp = post("/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "0"}}})
        assert code == 200
        code, resp = post("/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = resp.get("result", {}).get("tools", [])
        assert len(tools) == 41
