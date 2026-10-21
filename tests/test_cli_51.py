# -*- coding: utf-8 -*-
"""v5.2 CLI 体验测试：ask/身份备份/升级映射/邀请二维码/自动安装。

全部 mock，不联网、不写真实 HOME。
"""
import argparse
import base64
import json
import os

import pytest

import fleet
import fleet_core


def ns(**kw):
    return argparse.Namespace(**kw)


class FakeClient(object):
    def __init__(self, *a, **kw):
        self.calls = []

    def call(self, tool, args=None, timeout=None):
        self.calls.append((tool, args or {}))
        return json.dumps({"ok": True, "tool": tool}), False


@pytest.fixture()
def local(monkeypatch, tmp_path):
    """假节点目录 + local_node 返回。"""
    d = tmp_path / "node"
    (d / "data").mkdir(parents=True)
    ident = fleet_core.Identity.load_or_create(str(d / "data" / "identity.json"))
    cfg = {"name": "test-node", "port": 3100, "auth_token": "tok",
           "data_dir": str(d / "data")}
    (d / "mcp_agent_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    me = {"name": "test-node", "url": "http://127.0.0.1:3100/mcp",
          "token": "tok", "dir": str(d),
          "public_key": ident.public_key_b64, "node_id": ident.node_id,
          "fingerprint": ident.fingerprint}
    monkeypatch.setattr(fleet, "local_node", lambda *a, **k: dict(me))
    return me, ident


class TestAsk:
    def test_classify_disk(self):
        name, goal, steps = fleet._classify_ask("NAS 磁盘为什么满了")
        assert name == "disk" and goal and steps

    def test_classify_network(self):
        assert fleet._classify_ask("家里网怎么这么慢")[0] == "network"

    def test_classify_unknown(self):
        assert fleet._classify_ask("帮我写一首诗")[0] == "general"

    def test_ask_run_only_safe(self, local, monkeypatch, capsys):
        client = FakeClient()
        monkeypatch.setattr(fleet, "MCPClient", lambda *a, **k: client)
        rc = fleet.cmd_ask(ns(text="磁盘满了", run=True, timeout=10,
                              self_url=None))
        assert rc == 0
        tools = [c[0] for c in client.calls]
        assert "sysinfo" in tools and "exec" in tools
        # 只允许白名单命令
        for tool, args in client.calls:
            if tool == "exec":
                assert str(args.get("command", "")).startswith(
                    ("df", "uptime", "free", "ip route", "ip addr"))
        out = capsys.readouterr().out
        assert "计划" in out and "可直接粘给 AI 客户端" in out

    def test_ask_without_node(self, monkeypatch):
        monkeypatch.setattr(fleet, "local_node", lambda *a, **k: None)
        assert fleet.cmd_ask(ns(text="磁盘", run=False, timeout=10,
                                self_url=None)) == 1


class TestIdentity:
    def test_export_import_roundtrip(self, local, tmp_path, monkeypatch):
        me, ident = local
        out = str(tmp_path / "identity-export.json")
        rc = fleet.cmd_identity(ns(action="export", out=out, passphrase="secret123",
                                   from_file="", self_url=None, timeout=10))
        assert rc == 0 and os.path.isfile(out)
        data = json.load(open(out))
        assert data["node_id"] == ident.node_id
        assert "blob" in data and data["blob"].startswith("enc2$")
        # 破坏身份后导入恢复
        id_path = os.path.join(me["dir"], "data", "identity.json")
        os.remove(id_path)
        rc = fleet.cmd_identity(ns(action="import", out="", passphrase="secret123",
                                   from_file=out, self_url=None, timeout=10))
        assert rc == 0
        restored = json.load(open(id_path))
        assert restored["node_id"] == ident.node_id
        # 恢复后身份可用
        assert fleet.node_identity(me["dir"])["node_id"] == ident.node_id

    def test_import_wrong_passphrase(self, local, tmp_path):
        me, _ident = local
        out = str(tmp_path / "exp.json")
        fleet.cmd_identity(ns(action="export", out=out, passphrase="secret123",
                              from_file="", self_url=None, timeout=10))
        id_path = os.path.join(me["dir"], "data", "identity.json")
        os.remove(id_path)
        rc = fleet.cmd_identity(ns(action="import", out="", passphrase="wrong",
                                   from_file=out, self_url=None, timeout=10))
        assert rc == 1
        assert not os.path.exists(id_path)

    def test_show(self, local, capsys):
        assert fleet.cmd_identity(ns(action="show", self_url=None,
                                     timeout=10)) == 0
        out = capsys.readouterr().out
        assert "fingerprint" in out or "指纹" in out


class TestQRAndJoin:
    def test_qr_without_qrencode(self, monkeypatch, capsys):
        monkeypatch.setattr(fleet.shutil, "which", lambda name: None)
        assert fleet._show_qr("fleet://join/abc") is False
        assert "qrencode" in capsys.readouterr().out

    def test_join_auto_install(self, local, tmp_path, monkeypatch, capsys):
        me, ident = local
        inv = fleet_core.Invite.create(ident, "peer", "127.0.0.1", 3101, ttl=600)
        calls = []
        # 第一次 local_node 返回 None（未安装），安装后返回 me
        seq = iter([None, dict(me)])
        monkeypatch.setattr(fleet, "local_node", lambda *a, **k: next(seq, dict(me)))
        monkeypatch.setattr(fleet, "_installer_path", lambda: "/tmp/fleet_install.py")
        monkeypatch.setattr(fleet.subprocess, "call",
                            lambda argv: calls.append(argv) or 0)
        monkeypatch.setattr(fleet, "cmd_pair", lambda ns_: 0)
        rc = fleet.cmd_join(ns(uri=inv.to_uri(), auto_install=True,
                               self_url=None, timeout=10))
        assert rc == 0
        assert calls and "--join" in calls[0] and inv.to_uri() in calls[0]

    def test_join_without_auto_install(self, monkeypatch, capsys):
        monkeypatch.setattr(fleet, "local_node", lambda *a, **k: None)
        ident = fleet_core.Identity.load_or_create(
            os.path.join("/tmp", "join-test-identity.json"))
        inv = fleet_core.Invite.create(ident, "peer", "127.0.0.1", 3101, ttl=600)
        assert fleet.cmd_join(ns(uri=inv.to_uri(), auto_install=False,
                                 self_url=None, timeout=10)) == 1
        assert "--auto-install" in capsys.readouterr().out


class TestUpdateAndParser:
    def test_update_maps_upgrade(self, monkeypatch):
        seen = []
        monkeypatch.setattr(fleet, "_installer_path", lambda: "/tmp/i.py")
        monkeypatch.setattr(fleet.subprocess, "call",
                            lambda argv: seen.append(argv) or 0)
        assert fleet.cmd_update(ns(rollback=False, json=True)) == 0
        assert "--upgrade" in seen[0] and "--json" in seen[0]

    def test_update_maps_rollback(self, monkeypatch):
        seen = []
        monkeypatch.setattr(fleet, "_installer_path", lambda: "/tmp/i.py")
        monkeypatch.setattr(fleet.subprocess, "call",
                            lambda argv: seen.append(argv) or 0)
        assert fleet.cmd_update(ns(rollback=True, json=False)) == 0
        assert "--rollback" in seen[0]

    def test_parser_new_commands(self):
        parser = fleet.build_parser()
        for argv in (["ask", "磁盘满了"], ["update", "--rollback"],
                     ["identity", "show"], ["join", "fleet://join/abc",
                                            "--auto-install"]):
            args = parser.parse_args(argv)
            assert getattr(args, "func", None) is not None
