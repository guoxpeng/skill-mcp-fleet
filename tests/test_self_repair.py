# -*- coding: utf-8 -*-
"""v5.2 自修复：工具注册 / 确认边界 / 有界循环 / 只读保护。"""
import json

import mcp_agent as mp


def test_self_repair_tools_registered():
    names = {t["name"] for t in mp.TOOLS}
    assert {"fleet_selfcheck", "fleet_repair", "fleet_repair_loop"} <= names
    read_only = {t["name"] for t in mp.TOOLS
                 if t.get("annotations", {}).get("readOnlyHint")}
    assert "fleet_selfcheck" in read_only
    assert "fleet_repair" not in read_only


def test_repair_requires_confirm(monkeypatch):
    monkeypatch.setattr(mp, "_repair_do", lambda action: (True, "ok"))
    text, err = mp.t_fleet_repair({"action": "restart_service"})
    assert err and "需要确认" in text
    text, err = mp.t_fleet_repair({"action": "restart_service", "confirm": "x"})
    assert err and "需要确认" in text


def test_repair_unknown_action_refused(monkeypatch):
    text, err = mp.t_fleet_repair({"action": "rm_rf", "confirm": mp.SERVER_NAME})
    assert err and "未知动作" in text


def test_repair_readonly_refuses(monkeypatch):
    monkeypatch.setitem(mp.CFG, "read_only", True)
    text, err = mp.t_fleet_repair({"action": "restart_service",
                                   "confirm": mp.SERVER_NAME})
    assert err and "只读" in text
    text, err = mp.t_fleet_repair_loop({"confirm": mp.SERVER_NAME})
    assert err and "只读" in text


def test_selfcheck_shape(monkeypatch):
    monkeypatch.setattr(mp, "_selfcheck_findings",
                        lambda: [{"code": "stale_pid", "severity": "medium",
                                  "title": "stale", "detail": "", "action":
                                  "clear_stale_pid", "repairable": True}])
    text, err = mp.t_fleet_selfcheck({})
    data = json.loads(text)
    assert err is False
    assert data["repair_plan"] == ["clear_stale_pid"]
    assert data["findings"][0]["code"] == "stale_pid"


def test_repair_loop_bounded_and_stops(monkeypatch):
    calls = []
    seq = [
        [{"code": "port_unreachable", "severity": "critical", "title": "p",
          "detail": "", "action": "restart_service", "repairable": True}],
        [],
    ]

    def fake_findings():
        return seq.pop(0) if seq else []

    monkeypatch.setattr(mp, "_selfcheck_findings", fake_findings)
    monkeypatch.setattr(mp, "_repair_do", lambda action: calls.append(action)
                        or (True, "ok"))
    monkeypatch.setattr(mp, "_audit_call", lambda *a, **k: None)
    text, err = mp.t_fleet_repair_loop({"confirm": mp.SERVER_NAME,
                                        "max_attempts": 3})
    data = json.loads(text)
    assert err is False
    assert calls == ["restart_service"]
    assert len(data["attempts"]) == 1
    assert data["ok"] is True


def test_repair_loop_stops_on_failure(monkeypatch):
    finding = {"code": "port_unreachable", "severity": "critical", "title": "p",
               "detail": "", "action": "restart_service", "repairable": True}
    monkeypatch.setattr(mp, "_selfcheck_findings", lambda: [finding])
    monkeypatch.setattr(mp, "_repair_do", lambda action: (False, "boom"))
    monkeypatch.setattr(mp, "_audit_call", lambda *a, **k: None)
    text, err = mp.t_fleet_repair_loop({"confirm": mp.SERVER_NAME})
    data = json.loads(text)
    assert err is True
    assert data["attempts"][0]["ok"] is False
    assert data["remaining"][0]["code"] == "port_unreachable"


def test_package_flat_format(tmp_path):
    """CI/华为云要求：根目录平铺、SKILL.md 在根、包内无 MANIFEST、30 文件。"""
    import subprocess, sys, zipfile, json, glob, os
    out = tmp_path / "dist"
    r = subprocess.run([sys.executable, os.path.join("scripts", "build_package.py"),
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    zpath = glob.glob(str(out / "*.zip"))[0]
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
    assert "SKILL.md" in names
    assert "MANIFEST.json" not in names
    assert not any(n.startswith("mcp-fleet/") for n in names)
    assert len(names) == 30
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert manifest["file_count"] == 30
