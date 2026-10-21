# -*- coding: utf-8 -*-
"""tests/test_installer_51.py — fleet_install.py（v5.2 跨平台安装器）单元测试。

覆盖契约 docs/V5.2-CONTRACT.md 第 2 节要求：
* 平台检测（systemd/openrc/procd/launchd/schtasks/termux/容器/NAS）——monkeypatch；
* dry-run JSON 单行可解析、CLI 行为（--json/--dry-run/--print-unit/--status/--uninstall）；
* plan 的动作序列与 requires_root；
* 安装到临时目录生成配置/身份/服务单元（identity 用假 fleet_core mock，不联网）；
* 服务单元内容包含安全加固项；
* 卸载路径安全（拒绝 /、$HOME、/usr，拒绝恶意 manifest 与外部服务文件）；
* join URI 解析（含 fleet_core 缺失降级）；幂等安装；install.sh 的 sh -n 语法。

约束：全部用例只用 mock 与临时目录，不真的安装服务、不需要 root、不 sleep。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types

import pytest

os.environ.setdefault("FLEET_NO_LAUNCHER", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:  # conftest 也会加，这里保证单独跑本文件也 OK
    sys.path.insert(0, SCRIPTS)

import fleet_install as fi  # noqa: E402

# autouse fixture 会把 _port_in_use 换成常量，这里先留存真实实现供专门用例使用
_REAL_PORT_IN_USE = fi._port_in_use


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------
STUB_NODE_FILES = ["mcp_agent.py", "fleet_proto.py", "dashboard_html.py",
                   "fleet_core.py", "fleet_agent.py", "fleet.py"]


def make_platform(**over):
    """伪造 detect_platform() 的返回字典（安装流程只依赖其中少量字段）。"""
    platform = {
        "os": "linux",
        "arch": "x86_64",
        "distro": "debian",
        "distro_version": "12",
        "init": "systemd",
        "is_root": False,
        "is_termux": False,
        "is_container": False,
        "python": "3.9.0",
        "python_executable": sys.executable,
        "has_systemd": True,
        "service_manager": "systemd",
        "nas_vendor": "",
        "is_openwrt": False,
        "is_alpine": False,
        "kernel": "6.1.0-test",
        "docker_available": False,
        "warnings": [],
    }
    platform.update(over)
    return platform


def make_source(tmp_path):
    """造一个最小源码目录（含必需文件与示例配置），避免拷贝仓库真实大文件。"""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name in STUB_NODE_FILES:
        (src / name).write_text("# stub %s\n" % name, encoding="utf-8")
    example = {
        "name": "example",
        "port": 3999,
        "tool_prefix": "",
        "audit_log": True,
        "max_body_bytes": 1048576,
    }
    (src / "mcp_agent_config.example.json").write_text(
        json.dumps(example), encoding="utf-8")
    return str(src)


def make_opts(tmp_path, service="nohup", name="testnode", **over):
    """安装/卸载用 opts：目录全部落在 tmp_path，默认不注册服务。"""
    opts = {
        "name": name,
        "port": 3100,
        "service": service,
        "dir": str(tmp_path / "inst"),
        "data_dir": str(tmp_path / "data"),
        "platform": make_platform(),
        "tls": "off",
        "source_dir": make_source(tmp_path),
        "no_service": True,
    }
    opts.update(over)
    return opts


def paths_of(tmp_path, **over):
    """仅按 opts 解析路径，不触发安装。"""
    opts = fi._normalize_opts(make_opts(tmp_path, **over))
    return opts, fi.resolve_paths(opts, opts["platform"])


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class FakeCore(object):
    """最小 fleet_core 替身：身份写盘 + 邀请 URI，形状与真模块一致。"""

    def __init__(self, join_ok=True):
        self.join_ok = join_ok
        self.Identity = self._make_identity()
        self.Invite = self._make_invite()

    @staticmethod
    def _make_identity():
        class _Identity(object):
            node_id = "testnodeid123456"
            fingerprint = "ABCD-EFGH-IJKL-MNOP"
            public_key_b64 = "FAKE-PUBLIC-KEY"

            @classmethod
            def load_or_create(cls, path):
                fi._atomic_write(path, json.dumps({"node_id": cls.node_id}), 0o600)
                return cls()

        return _Identity

    def _make_invite(self):
        outer = self

        class _Invite(object):
            def __init__(self, **fields):
                self.__dict__.update(fields)

            @classmethod
            def create(cls, identity, name, host, port, ttl=3600, caps=None):
                return cls(name=name, host=host, port=port, ttl=ttl)

            @classmethod
            def parse(cls, uri):
                if not outer.join_ok:
                    raise ValueError("签名无效")
                return cls(name="peer-node", node_id="peerid",
                           host="10.0.0.9", port=3200)

            def to_uri(self):
                return "fleet://join/fake-payload"

        return _Invite


class FakeResponse(object):
    status = 200

    def __init__(self, body=b'{"status":"ok"}'):
        self._body = body
        self.closed = False

    def read(self, _n=None):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# 自动生效的“零副作用” fixture
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fast_env(monkeypatch):
    """所有用例默认：不 sleep、不探测真实端口、不联网、无 fleet_core。"""
    monkeypatch.setattr(fi, "_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(fi, "_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(fi, "_port_in_use", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(fi, "_which", lambda name: "/usr/bin/%s" % name)
    monkeypatch.setattr(fi, "_import_fleet_core", lambda opts=None: None)
    return monkeypatch


@pytest.fixture
def no_service_start(monkeypatch):
    """把 8 个适配器的 start 与健康检查都 mock 掉：只写文件，不启进程。"""

    def _fake_start(self):
        return {"ok": True, "method": self.key, "detail": "mocked",
                "commands": [], "warnings": []}

    for cls in fi.ADAPTERS.values():
        monkeypatch.setattr(cls, "start", _fake_start)
    monkeypatch.setattr(
        fi, "health_check",
        lambda *a, **k: {"ok": True, "attempts": 1,
                         "url": "http://127.0.0.1:3100/health",
                         "last": {}, "history": []})
    return monkeypatch


# ===========================================================================
# 1. 平台检测
# ===========================================================================
def _fake_detect(monkeypatch, os_name="linux", init="systemd", distro="debian",
                 is_termux=False, is_container=False, is_root=False,
                 arch="x86_64", nas_vendor=""):
    """把 detect_platform 依赖的每个小函数都替换成确定值。"""
    monkeypatch.setattr(fi, "_detect_os", lambda: os_name)
    monkeypatch.setattr(fi, "_detect_arch", lambda: arch)
    monkeypatch.setattr(fi, "_read_os_release",
                        lambda: {"ID": distro, "VERSION_ID": "12"})
    monkeypatch.setattr(fi, "_is_termux", lambda: is_termux)
    monkeypatch.setattr(fi, "_in_container", lambda: is_container)
    monkeypatch.setattr(fi, "_is_root", lambda: is_root)
    monkeypatch.setattr(fi, "_detect_init", lambda *a, **k: init)
    monkeypatch.setattr(fi, "_has_systemd", lambda: init == "systemd")
    monkeypatch.setattr(fi, "_detect_distro",
                        lambda *a, **k: (distro, "12", nas_vendor))
    monkeypatch.setattr(fi._platform, "release", lambda: "6.1.0-test")
    return fi.detect_platform()


def test_detect_platform_systemd(monkeypatch):
    platform = _fake_detect(monkeypatch, init="systemd", distro="debian")
    assert platform["os"] == "linux"
    assert platform["init"] == "systemd"
    assert platform["service_manager"] == "systemd"
    assert platform["has_systemd"] is True
    # 非 root 跑 systemd 必须提示用户级实例/linger
    assert any("非 root" in w for w in platform["warnings"])


def test_detect_platform_root_systemd_warning(monkeypatch):
    platform = _fake_detect(monkeypatch, init="systemd", is_root=True)
    assert platform["is_root"] is True
    assert any("加固" in w for w in platform["warnings"])


def test_detect_platform_openrc_alpine(monkeypatch):
    platform = _fake_detect(monkeypatch, init="openrc", distro="alpine")
    assert platform["service_manager"] == "openrc"
    assert platform["is_alpine"] is True
    assert platform["has_systemd"] is False


def test_detect_platform_procd_openwrt(monkeypatch):
    platform = _fake_detect(monkeypatch, init="procd", distro="openwrt")
    assert platform["service_manager"] == "procd"
    assert platform["is_openwrt"] is True


def test_detect_platform_launchd_macos(monkeypatch):
    platform = _fake_detect(monkeypatch, os_name="macos", init="launchd",
                            distro="macos")
    assert platform["os"] == "macos"
    assert platform["service_manager"] == "launchd"
    assert platform["has_systemd"] is False


def test_detect_platform_schtasks_windows(monkeypatch):
    platform = _fake_detect(monkeypatch, os_name="windows", init="schtasks",
                            distro="windows")
    assert platform["service_manager"] == "schtasks"
    assert any("Windows" in w for w in platform["warnings"])


def test_detect_platform_termux(monkeypatch):
    platform = _fake_detect(monkeypatch, init="termux", is_termux=True)
    assert platform["service_manager"] == "termux"
    assert platform["is_termux"] is True
    assert any("Termux" in w for w in platform["warnings"])


def test_detect_platform_container_degrades_to_nohup(monkeypatch):
    platform = _fake_detect(monkeypatch, init="none", is_container=True)
    assert platform["is_container"] is True
    assert platform["service_manager"] == "nohup"
    assert any("容器" in w for w in platform["warnings"])
    assert platform["docker_available"] is True


@pytest.mark.parametrize("vendor", ["synology", "fnos", "qnap", "ugreen"])
def test_detect_platform_nas_vendor_degrades_to_nohup(monkeypatch, vendor):
    platform = _fake_detect(monkeypatch, init="none", nas_vendor=vendor)
    assert platform["nas_vendor"] == vendor
    assert platform["service_manager"] == "nohup"
    assert any(vendor in w for w in platform["warnings"])


def test_detect_platform_required_keys(monkeypatch):
    platform = _fake_detect(monkeypatch, init="systemd")
    for key in ("os", "arch", "distro", "distro_version", "init", "is_root",
                "is_termux", "is_container", "python", "python_executable",
                "has_systemd", "service_manager", "nas_vendor", "is_openwrt",
                "is_alpine", "kernel", "docker_available", "warnings"):
        assert key in platform, "缺少平台字段 %s" % key
    assert platform["service_manager"] in fi.SERVICE_KEYS
    assert isinstance(platform["warnings"], list)


def test_detect_platform_real_environment():
    """真机 sanity：不 mock 也必须返回合法结构（本机是 macOS）。"""
    platform = fi.detect_platform()
    assert platform["service_manager"] in fi.SERVICE_KEYS
    assert platform["python"].count(".") == 2
    assert isinstance(platform["warnings"], list)
    assert platform["os"] in ("linux", "macos", "windows") or platform["os"]


@pytest.mark.parametrize("os_name,init,expected", [
    ("windows", "schtasks", "schtasks"),
    ("macos", "launchd", "launchd"),
    ("linux", "termux", "termux"),
    ("linux", "systemd", "systemd"),
    ("linux", "procd", "procd"),
    ("linux", "openrc", "openrc"),
    ("linux", "none", "nohup"),
])
def test_detect_service_manager_mapping(os_name, init, expected):
    assert fi._detect_service_manager(os_name, init, "", False) == expected


def test_detect_service_manager_nas_and_container_are_nohup():
    assert fi._detect_service_manager("linux", "none", "synology", False) == "nohup"
    assert fi._detect_service_manager("linux", "none", "", True) == "nohup"


@pytest.mark.parametrize("case,exists,expected", [
    ("linux-systemd", set(), "systemd"),
    ("linux-procd", {"/sbin/procd"}, "procd"),
    ("linux-openrc", {"/sbin/openrc"}, "openrc"),
    ("linux-none", set(), "none"),
])
def test_detect_init_fallbacks(monkeypatch, case, exists, expected):
    os_name, is_termux = "linux", False
    monkeypatch.setattr(fi, "_has_systemd",
                        lambda: expected == "systemd")
    monkeypatch.setattr(fi, "_exists", lambda path: path in exists)
    assert fi._detect_init(os_name, is_termux) == expected


def test_detect_init_shortcuts():
    assert fi._detect_init("windows") == "schtasks"
    assert fi._detect_init("macos") == "launchd"
    assert fi._detect_init("linux", is_termux=True) == "termux"


def test_in_container_markers(monkeypatch):
    monkeypatch.setattr(fi, "_exists", lambda path: path == "/.dockerenv")
    assert fi._in_container() is True
    monkeypatch.setattr(fi, "_exists", lambda path: False)
    monkeypatch.setattr(fi, "_read_text",
                        lambda path, limit=None: "12:cpu:/docker/abc\n")
    assert fi._in_container() is True
    monkeypatch.setattr(fi, "_read_text",
                        lambda path, limit=None: "12:cpu:/user.slice\n")
    assert fi._in_container() is False


def test_is_termux_by_prefix(monkeypatch):
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert fi._is_termux() is True
    monkeypatch.setenv("PREFIX", "/usr")
    monkeypatch.setattr(fi, "_exists", lambda path: False)
    assert fi._is_termux() is False


# ===========================================================================
# 2. plan（零副作用安装计划）
# ===========================================================================
EXPECTED_ACTION_IDS = ["precheck", "dirs", "copy", "config", "identity",
                       "tls", "service", "start", "health", "report"]


def test_plan_action_sequence(tmp_path):
    result = fi.plan(make_opts(tmp_path, service="systemd"))
    assert result["dry_run"] is True
    assert result["service"] == "systemd"
    assert [a["id"] for a in result["actions"]] == EXPECTED_ACTION_IDS
    for action in result["actions"]:
        assert action["dry_run"] is True
        assert isinstance(action["description"], str) and action["description"]
        assert "requires_root" in action
        assert action["kind"]


def test_plan_no_service_marks_skipped(tmp_path):
    result = fi.plan(make_opts(tmp_path, service="systemd", no_service=True))
    skipped = {a["id"] for a in result["actions"] if a.get("skipped")}
    assert skipped == {"service", "start", "health"}
    assert all("--no-service" in a["description"]
               for a in result["actions"] if a.get("skipped"))


def test_plan_requires_root_openrc(tmp_path):
    opts = make_opts(tmp_path, service="openrc",
                     platform=make_platform(is_root=False))
    result = fi.plan(opts)
    root_actions = {a["id"] for a in result["actions"] if a["requires_root"]}
    # 非 root 装 /etc/init.d 需要提权
    assert {"dirs", "service", "start"} <= root_actions


def test_plan_systemd_never_requires_root(tmp_path):
    opts = make_opts(tmp_path, service="systemd",
                     platform=make_platform(is_root=False))
    result = fi.plan(opts)
    assert not any(a["requires_root"] for a in result["actions"])


def test_plan_merges_platform_and_dependency_warnings(tmp_path, monkeypatch):
    platform = make_platform(warnings=["PLATFORM-WARN"])
    monkeypatch.setattr(fi, "_port_in_use", lambda port, host="127.0.0.1": True)
    result = fi.plan(make_opts(tmp_path, service="nohup", platform=platform,
                               dry_run=True))
    assert "PLATFORM-WARN" in result["warnings"]
    assert any("端口" in w for w in result["warnings"])


def test_plan_port_in_use_is_error_when_not_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "_port_in_use", lambda port, host="127.0.0.1": True)
    result = fi.plan(make_opts(tmp_path, service="nohup", dry_run=False))
    assert result["ok"] is False
    assert any("端口" in e for e in result["errors"])


def test_plan_docker_mode(tmp_path):
    platform = make_platform(service_manager="nohup", docker_available=True)
    result = fi.plan(make_opts(tmp_path, service="docker", platform=platform))
    assert result["service"] == "docker"
    assert result["paths"]["install_dir"].endswith(
        os.path.join("inst", "")[:-1]) or os.path.isdir(
        os.path.dirname(result["paths"]["install_dir"]))


def test_plan_paths_are_public_strings(tmp_path):
    result = fi.plan(make_opts(tmp_path, service="nohup"))
    assert all(isinstance(v, str) for v in result["paths"].values())
    for key in ("install_dir", "data_dir", "log_dir", "config_path",
                "manifest_path", "identity_path", "unit_label"):
        assert result["paths"][key]


# ===========================================================================
# 3. 服务单元渲染与安全加固
# ===========================================================================
def render_unit(tmp_path, service, name="demo node", **over):
    opts = {
        "name": name,
        "port": 3100,
        "service": service,
        "platform": make_platform(),
        "dir": str(tmp_path / "inst"),
        "data_dir": str(tmp_path / "data"),
        "unit_dir": str(tmp_path / "units"),
        "tls": "off",
    }
    opts.update(over)
    return fi._print_unit(opts)


def test_systemd_unit_hardening(tmp_path):
    unit = render_unit(tmp_path, "systemd")["unit"]
    for marker in ("NoNewPrivileges=yes", "PrivateTmp=yes",
                   "ProtectSystem=strict", "ProtectKernelTunables=yes",
                   "ProtectKernelModules=yes", "ProtectControlGroups=yes",
                   "RestrictSUIDSGID=yes", "LockPersonality=yes",
                   "RestrictRealtime=yes", "SystemCallArchitectures=native",
                   "Restart=always", "RestartSec=3", "TimeoutStopSec=10",
                   "KillMode=mixed", "UMask=0077", "StartLimitIntervalSec=0",
                   "ReadWritePaths=", "WantedBy=multi-user.target"):
        assert marker in unit, "systemd 单元缺少加固项 %s" % marker
    assert "{{" not in unit and "}}" not in unit
    assert "demo node" in unit


def test_systemd_root_protect_home(tmp_path):
    platform = make_platform(is_root=True)
    unit = render_unit(tmp_path, "systemd", platform=platform,
                       dir="/opt/mcp-fleet/demo",
                       data_dir="/var/lib/mcp-fleet/demo")["unit"]
    assert "ProtectHome=read-only" in unit
    # root 运行时不应再写 User=root
    assert "User=root" not in unit


def test_systemd_custom_user_line(tmp_path):
    platform = make_platform(is_root=True)
    unit = render_unit(tmp_path, "systemd", platform=platform, user="fleet",
                       dir="/opt/mcp-fleet/demo",
                       data_dir="/var/lib/mcp-fleet/demo")["unit"]
    assert "User=fleet" in unit


def test_systemd_user_install_has_no_protect_home(tmp_path):
    unit = render_unit(tmp_path, "systemd")["unit"]
    assert "ProtectHome" not in unit


def test_launchd_unit(tmp_path):
    unit = render_unit(tmp_path, "launchd")["unit"]
    for marker in ("<key>Label</key>", "com.mcpfleet.node.demo-node",
                   "<key>RunAtLoad</key>", "<key>KeepAlive</key>",
                   "<key>ThrottleInterval</key>", "<key>Umask</key>",
                   "<key>StandardOutPath</key>", "ProgramArguments"):
        assert marker in unit
    assert "<true/>" in unit
    assert "{{" not in unit


def test_openrc_unit(tmp_path):
    unit = render_unit(tmp_path, "openrc")["unit"]
    for marker in ("#!/sbin/openrc-run", "supervise-daemon",
                   "respawn_delay=3", "respawn_max=0",
                   'command_background="yes"', "command_user="):
        assert marker in unit
    assert "{{" not in unit


def test_procd_unit(tmp_path):
    unit = render_unit(tmp_path, "procd")["unit"]
    for marker in ("USE_PROCD=1", "procd_open_instance",
                   "procd_set_param respawn", "procd_set_param user",
                   "procd_set_param limits"):
        assert marker in unit
    assert "{{" not in unit


def test_termux_unit(tmp_path):
    unit = render_unit(tmp_path, "termux")["unit"]
    assert "termux-wake-lock" in unit
    assert "exec " in unit
    assert "{{" not in unit


def test_docker_compose_unit(tmp_path):
    unit = render_unit(tmp_path, "docker")["unit"]
    for marker in ("restart: unless-stopped", "no-new-privileges:true",
                   "cap_drop:", "read_only: true", "tmpfs:",
                   "healthcheck:", "logging:"):
        assert marker in unit
    assert "{{" not in unit


def test_schtasks_unit(tmp_path):
    unit = render_unit(tmp_path, "schtasks")["unit"]
    for marker in ("<Task version=", "LogonTrigger", "BootTrigger",
                   "HighestAvailable", "MultipleInstancesPolicy",
                   "RestartOnFailure", "ExecutionTimeLimit"):
        assert marker in unit
    assert "{{" not in unit


def test_nohup_control_script(tmp_path):
    unit = render_unit(tmp_path, "nohup")["unit"]
    for marker in ("#!/bin/sh", "set -eu", "PIDF=", "start()", "stop()",
                   "status()", "case "):
        assert marker in unit
    assert "{{" not in unit


def test_extra_agent_args_flow_into_units(tmp_path):
    over = {"read_only": True, "require_auth": True,
            "tls_cert": "/tmp/cert.pem", "tls_key": "/tmp/key.pem"}
    unit = render_unit(tmp_path, "systemd", **over)["unit"]
    assert "--read-only" in unit
    assert "--require-auth" in unit
    assert "--tls-cert" in unit and "--tls-key" in unit


def test_shell_quote_and_xml_escape():
    assert fi._shell_quote("simple") == "simple"
    assert fi._shell_quote("a b") == '"a b"'
    assert "$" in fi._shell_quote("$HOME") and '"' in fi._shell_quote("$HOME")
    escaped = fi._xml_escape('a<b>&"')
    assert escaped == "a&lt;b&gt;&amp;&quot;"


def test_render_template_rejects_missing_vars():
    with pytest.raises(fi.InstallerError):
        fi.render_template("value={{MISSING}}", {})
    with pytest.raises(fi.InstallerError):
        fi.render_template("value={{lower}}", {"LOWER": "x"})
    assert fi.render_template("v={{A}}", {"A": "1"}) == "v=1"


def test_load_template_missing_raises():
    with pytest.raises(fi.InstallerError):
        fi.load_template("definitely-not-a-template.txt")


def test_all_templates_present():
    names = ["fleet.service", "fleet.openrc", "fleet.procd",
             "com.mcpfleet.node.plist", "fleet-compose.yml", "Dockerfile",
             "fleet.nohup", "fleet.termux", "fleet.schtasks.xml",
             "check-deps.sh"]
    for name in names:
        path = os.path.join(fi.TEMPLATE_DIR, name)
        assert os.path.isfile(path), "缺少模板 %s" % name
        assert os.path.getsize(path) > 0


# ===========================================================================
# 4. 安装：配置 / 身份 / 幂等 / 临时目录
# ===========================================================================
def test_install_writes_config_and_node_files(tmp_path):
    opts = make_opts(tmp_path)
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    install_dir, data_dir = opts["dir"], opts["data_dir"]
    for name in STUB_NODE_FILES:
        assert os.path.isfile(os.path.join(install_dir, name))
    assert os.path.isfile(os.path.join(install_dir, "mcp_agent_config.json"))
    assert os.path.isfile(os.path.join(data_dir, "manifest.json"))
    assert os.path.isfile(os.path.join(install_dir, ".fleet-manifest.json"))
    config = read_json(os.path.join(install_dir, "mcp_agent_config.json"))
    assert config["name"] == "testnode"
    assert config["port"] == 3100
    assert config["audit_log"] is True
    assert config["discover_port"] == fi.DEFAULT_DISCOVER_PORT
    assert config["allowed_roots"] == ["/"]
    assert len(config["auth_token"]) >= 32      # v5.2 默认自动生成
    assert result["auth"]["enabled"] is True
    assert result["auth"]["created"] is True
    manifest = read_json(os.path.join(data_dir, "manifest.json"))
    assert manifest["name"] == "testnode"
    assert manifest["service"] == "nohup"
    assert manifest["version"] == fi.VERSION
    assert config["port"] == manifest["port"]
    # --no-service 时不应注册/启动
    assert result["start"]["skipped"] is True
    assert any("--no-service" in w for w in result["warnings"])


def test_install_sets_safe_permissions(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    config_mode = os.stat(os.path.join(opts["dir"],
                                       "mcp_agent_config.json")).st_mode & 0o777
    data_mode = os.stat(opts["data_dir"]).st_mode & 0o777
    manifest_mode = os.stat(os.path.join(
        opts["data_dir"], "manifest.json")).st_mode & 0o777
    assert config_mode == 0o600
    assert data_mode == 0o700
    assert manifest_mode == 0o600


def test_install_config_options_mapped(tmp_path):
    opts = make_opts(tmp_path, require_auth=True, read_only=True, relay=True,
                     prefix="mcp_", allow_ips="10.0.0.1, 10.0.0.2",
                     sudo_password="s3cret")
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    config = read_json(os.path.join(opts["dir"], "mcp_agent_config.json"))
    assert config["read_only"] is True
    assert config["require_auth"] is True
    assert config["relay"] is True
    assert config["tool_prefix"] == "mcp_"
    assert config["allow_ips"] == ["10.0.0.1", "10.0.0.2"]
    assert config["sudo_password"] == "s3cret"
    assert len(config["auth_token"]) >= 32


def test_install_generates_identity_and_join_uri(tmp_path, fast_env):
    core = FakeCore()
    fast_env.setattr(fi, "_import_fleet_core", lambda opts=None: core)
    opts = make_opts(tmp_path, advertise_host="127.0.0.1")
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    assert result["node"]["node_id"] == "testnodeid123456"
    assert result["device_code"] == "testnodeid123456"
    assert result["join_uri"].startswith("fleet://join/")
    assert result["peer"] if "peer" in result else True  # 无 join 时不存在
    identity_path = os.path.join(opts["data_dir"], "identity.json")
    assert os.path.isfile(identity_path)
    assert os.stat(identity_path).st_mode & 0o777 == 0o600


def test_install_identity_dry_run_skipped(tmp_path, fast_env):
    core = FakeCore()
    fast_env.setattr(fi, "_import_fleet_core", lambda opts=None: core)
    info = fi._setup_identity({"dry_run": True}, make_platform(),
                              paths_of(tmp_path)[1], {"name": "n"})
    assert info["node"] == {}
    assert any("dry-run" in w for w in info["warnings"])


def test_install_without_fleet_core_degrades(tmp_path):
    result = fi.install(make_opts(tmp_path))
    assert result["ok"] is True, result["errors"]
    assert result["node"] == {}
    assert result["join_uri"] == ""
    assert any("fleet_core" in w for w in result["warnings"])


def test_install_dry_run_has_no_side_effects(tmp_path):
    opts = make_opts(tmp_path, dry_run=True)
    result = fi.install(opts)
    assert result["dry_run"] is True
    assert result["ok"] is True
    assert not os.path.exists(opts["dir"])
    assert not os.path.exists(opts["data_dir"])
    assert any("dry-run" in step for step in result["next_steps"])


@pytest.mark.parametrize("service,filename,marker", [
    ("systemd", "mcp-fleet-testnode.service", "NoNewPrivileges=yes"),
    ("openrc", "mcp-fleet-testnode", "supervise-daemon"),
    ("procd", "mcp-fleet-testnode", "procd_set_param respawn"),
    ("launchd", "com.mcpfleet.node.testnode.plist", "<key>KeepAlive</key>"),
    ("schtasks", "fleet-testnode.xml", "RestartOnFailure"),
    ("termux", "run", "termux-wake-lock"),
    ("docker", "fleet-compose.yml", "restart: unless-stopped"),
    ("nohup", "fleet-ctl.sh", "start()"),
])
def test_install_writes_adapter_unit(tmp_path, no_service_start,
                                     service, filename, marker):
    opts = make_opts(tmp_path, service=service, no_service=False,
                     unit_dir=str(tmp_path / "units"))
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    unit_path = os.path.join(str(tmp_path / "units"), filename)
    assert os.path.isfile(unit_path), "未生成 %s 单元" % service
    with open(unit_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    assert marker in content
    assert "{{" not in content
    assert result["unit_path"] == unit_path
    manifest = read_json(os.path.join(opts["data_dir"], "manifest.json"))
    assert unit_path in manifest["unit_paths"]


def test_install_docker_also_writes_dockerfile(tmp_path, no_service_start):
    opts = make_opts(tmp_path, service="docker", no_service=False,
                     unit_dir=str(tmp_path / "units"))
    assert fi.install(opts)["ok"] is True
    dockerfile = os.path.join(str(tmp_path / "units"), "Dockerfile")
    assert os.path.isfile(dockerfile)
    with open(dockerfile, "r", encoding="utf-8") as handle:
        content = handle.read()
    assert "FROM python" in content
    assert "HEALTHCHECK" in content
    assert "tini" in content
    assert "{{" not in content


def test_install_is_idempotent_and_keeps_token(tmp_path):
    opts = make_opts(tmp_path, require_auth=True)
    first = fi.install(opts)
    assert first["ok"] is True, first["errors"]
    token_first = read_json(os.path.join(
        opts["dir"], "mcp_agent_config.json"))["auth_token"]
    second = fi.install(dict(opts))
    assert second["ok"] is True, second["errors"]
    token_second = read_json(os.path.join(
        opts["dir"], "mcp_agent_config.json"))["auth_token"]
    assert token_first and token_first == token_second
    manifest = read_json(os.path.join(opts["data_dir"], "manifest.json"))
    assert len(manifest["files"]) == len(set(manifest["files"]))
    assert manifest["unit_paths"] == list(dict.fromkeys(manifest["unit_paths"]))
    assert not any("already" in w.lower() for w in second["warnings"])


def test_install_backs_up_broken_config(tmp_path):
    opts = make_opts(tmp_path)
    os.makedirs(opts["dir"], exist_ok=True)
    broken = os.path.join(opts["dir"], "mcp_agent_config.json")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write("{not valid json")
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    backups = [n for n in os.listdir(opts["dir"]) if ".broken." in n]
    assert backups, "损坏配置应备份而不是静默覆盖"
    assert read_json(broken)["name"] == "testnode"


def test_install_requires_valid_platform(tmp_path):
    opts = make_opts(tmp_path)
    opts["platform"] = {"arch": "x86_64"}  # 有值但缺 os，必须拒绝
    with pytest.raises(fi.InstallerError):
        fi.install(opts)


def test_install_join_uri_peer_is_recorded(tmp_path, fast_env):
    core = FakeCore()
    fast_env.setattr(fi, "_import_fleet_core", lambda opts=None: core)
    opts = make_opts(tmp_path, join="fleet://join/abc",
                     advertise_host="127.0.0.1")
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    assert result["peer"]["node_id"] == "peerid"
    assert result["peer"]["port"] == 3200
    assert any("已识别邀请对端" in step for step in result["next_steps"])


# ===========================================================================
# 5. join URI 解析（含 fleet_core 缺失降级）
# ===========================================================================
def test_apply_join_absent():
    result = fi._apply_join({})
    assert result["ok"] is True
    assert result["skipped"] is True


def test_apply_join_bad_scheme():
    result = fi._apply_join({"join": "http://example.com/x"})
    assert result["ok"] is False
    assert "fleet://" in result["error"]


def test_apply_join_missing_core_degrades():
    result = fi._apply_join({"join": "fleet://join/abc"})
    assert result["ok"] is False
    assert result.get("degraded") is True
    assert "fleet_core" in result["error"]


def test_apply_join_fake_core(fast_env):
    fast_env.setattr(fi, "_import_fleet_core", lambda opts=None: FakeCore())
    result = fi._apply_join({"join": "fleet://join/abc"})
    assert result["ok"] is True
    assert result["peer"]["name"] == "peer-node"
    assert result["peer"]["host"] == "10.0.0.9"


def test_apply_join_parse_error_keeps_install(fast_env):
    fast_env.setattr(fi, "_import_fleet_core",
                     lambda opts=None: FakeCore(join_ok=False))
    result = fi._apply_join({"join": "fleet://join/abc"})
    assert result["ok"] is False
    assert "邀请无效" in result["error"]


def test_install_continues_when_join_invalid(tmp_path):
    result = fi.install(make_opts(tmp_path, join="fleet://join/abc"))
    assert result["ok"] is True, result["errors"]
    assert any("fleet_core" in w or "邀请" in w for w in result["warnings"])


# ===========================================================================
# 6. 配置与路径工具
# ===========================================================================
def test_write_config_preserves_existing_fields(tmp_path):
    opts, paths = paths_of(tmp_path)
    os.makedirs(paths["install_dir"], exist_ok=True)
    fi._atomic_write(paths["config_path"],
                     json.dumps({"auth_token": "keepme", "custom": 42}),
                     0o600)
    config = fi._write_config(opts, paths, "")
    assert config["auth_token"] == "keepme"
    assert config["custom"] == 42
    assert config["name"] == opts["name"]
    assert config["port"] == opts["port"]
    assert fi._read_token(paths["config_path"]) == "keepme"


def test_read_token_invalid(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{oops", encoding="utf-8")
    assert fi._read_token(str(path)) == ""
    assert fi._read_token(str(tmp_path / "missing.json")) == ""


def test_normalize_opts_defaults_and_aliases():
    opts = fi._normalize_opts({"mode": "nohup", "install_dir": "/tmp/x"})
    assert opts["service"] == "nohup"
    assert opts["dir"] == "/tmp/x"
    assert opts["port"] == fi.DEFAULT_PORT
    assert opts["name"]


@pytest.mark.parametrize("raw", [
    {"port": "abc"},
    {"port": -1},
    {"port": 70000},
    {"service": "launchd2"},
    {"mode": "bogus"},
    {"tls": "maybe"},
])
def test_normalize_opts_rejects(raw):
    with pytest.raises(fi.InstallerError):
        fi._normalize_opts(raw)


@pytest.mark.parametrize("name", ["../../etc/passwd", ". . / tmp", "\\..\\x"])
def test_clean_name_and_slug_are_path_safe(name):
    cleaned = fi._clean_name(name)
    slug = fi._slug(name)
    for value in (cleaned, slug):
        assert ".." not in value
        assert "/" not in value
        assert "\\" not in value
        assert value


def test_clean_name_keeps_chinese_and_defaults():
    assert "节点" in fi._clean_name("我的节点 one")
    default = fi._clean_name("")
    assert default and "/" not in default and ".." not in default


def test_slug_chinese_gets_hash_suffix():
    slug = fi._slug("中文节点")
    assert slug.startswith("node-")
    assert len(slug) == len("node-") + 6


@pytest.mark.parametrize("platform,expected_prefix", [
    (make_platform(is_root=True), "/opt/mcp-fleet"),
    (make_platform(is_root=False), os.path.join(os.path.expanduser("~"),
                                                ".mcp-fleet")),
    (make_platform(is_root=False, is_termux=True),
     os.path.join(os.path.expanduser("~"), ".mcp-fleet")),
])
def test_resolve_paths_defaults(platform, expected_prefix):
    paths = fi.resolve_paths({"name": "My Node"}, platform)
    assert paths["slug"] == "my-node"
    assert paths["install_dir"].startswith(expected_prefix)
    assert paths["config_path"] == os.path.join(paths["install_dir"],
                                                fi.CONFIG_NAME)
    assert paths["unit_label"] == "mcp-fleet-my-node"


def test_resolve_paths_windows_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    platform = make_platform(os="windows", init="schtasks",
                             service_manager="schtasks")
    paths = fi.resolve_paths({"name": "win-node"}, platform)
    assert paths["install_dir"] == os.path.join(
        str(tmp_path / "LocalAppData"), "mcp-fleet", "win-node")
    assert paths["data_dir"] == os.path.join(paths["install_dir"], "data")


def test_resolve_paths_root_linux_data_dir():
    platform = make_platform(is_root=True, is_container=False)
    paths = fi.resolve_paths({"name": "root-node"}, platform)
    assert paths["data_dir"] == "/var/lib/mcp-fleet/root-node"


def test_resolve_paths_overrides_expand_user(tmp_path):
    paths = fi.resolve_paths({
        "name": "x",
        "dir": "~/fleet-test-inst",
        "data_dir": "~/fleet-test-data",
    }, make_platform(is_root=False))
    assert os.path.isabs(paths["install_dir"])
    assert os.path.isabs(paths["data_dir"])
    assert paths["install_dir"].endswith("fleet-test-inst")


@pytest.mark.parametrize("hint_platform,expected", [
    ({"is_termux": True}, "pkg install"),
    ({"distro": "openwrt"}, "opkg install"),
    ({"distro": "alpine"}, "apk add"),
    ({"distro": "debian"}, "apt-get install"),
    ({"distro": "ubuntu"}, "apt-get install"),
    ({"distro": "fedora"}, "dnf install"),
    ({"os": "macos"}, "brew install"),
    ({"os": "windows"}, "winget install"),
])
def test_install_hint_per_platform(hint_platform, expected):
    assert expected in fi._install_hint("python3", hint_platform)


def test_path_helpers():
    assert fi._is_within("/a/b/c", "/a/b") is True
    assert fi._is_within("/a/b", "/a/b") is True
    assert fi._is_within("/a/bc", "/a/b") is False
    assert fi._is_within("/a/c", "/a/b") is False
    assert fi._path_key("~/x").startswith(os.sep)


def test_atomic_write_and_ensure_dir(tmp_path):
    path = str(tmp_path / "nested" / "file.txt")
    fi._atomic_write(path, "hello", 0o600)
    assert open(path, "r", encoding="utf-8").read() == "hello"
    assert os.stat(path).st_mode & 0o777 == 0o600
    fi._atomic_write(path, b"world", 0o640)
    assert open(path, "r", encoding="utf-8").read() == "world"
    fi._ensure_dir(str(tmp_path / "data"), 0o700)
    assert os.stat(str(tmp_path / "data")).st_mode & 0o777 == 0o700


def test_json_line_single_line_and_unicode():
    line = fi._json_line({"中文": "值", "ok": True})
    assert "\n" not in line
    assert "中文" in line
    assert json.loads(line)["ok"] is True


def test_gen_token_shape():
    token = fi._gen_token()
    assert isinstance(token, str) and len(token) == 43
    assert token != fi._gen_token()


def test_run_missing_command_returns_127():
    rc, output = fi._run(["definitely-not-a-real-command-xyz"])
    assert rc == 127
    assert "不可用" in output


def test_true_port_in_use(monkeypatch):
    monkeypatch.setattr(fi, "_port_in_use", _REAL_PORT_IN_USE)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert fi._port_in_use(port) is True
    finally:
        sock.close()
    assert fi._port_in_use(1) is True or fi._port_in_use(1) is False  # 不抛异常


def test_pid_alive():
    assert fi._pid_alive(os.getpid()) is True
    assert fi._pid_alive("not-a-pid") is False
    assert fi._pid_alive(0) is False
    assert fi._pid_alive(-5) is False


# ===========================================================================
# 7. 依赖检查
# ===========================================================================
def test_check_dependencies_ok_with_real_scripts_dir():
    opts = {"source_dir": SCRIPTS, "port": 3100}
    deps = fi.check_dependencies(opts, make_platform())
    assert deps["ok"] is True, deps["errors"]
    assert deps["source_dir"] == SCRIPTS
    assert deps["errors"] == []


def test_check_dependencies_missing_source(tmp_path):
    opts = {"source_dir": str(tmp_path / "does-not-exist"), "port": 3100}
    deps = fi.check_dependencies(opts, make_platform())
    assert deps["ok"] is False
    assert any("缺少必需文件" in e for e in deps["errors"])
    assert deps["hints"]


def test_check_dependencies_missing_required_file(tmp_path):
    src = tmp_path / "partial"
    src.mkdir()
    (src / "mcp_agent.py").write_text("# stub", encoding="utf-8")
    deps = fi.check_dependencies({"source_dir": str(src)}, make_platform())
    assert deps["ok"] is False
    assert any("fleet_proto.py" in e for e in deps["errors"])


def test_check_dependencies_warns_without_fleet_core(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("mcp_agent.py", "fleet_proto.py", "dashboard_html.py", "fleet.py"):
        (src / name).write_text("# stub", encoding="utf-8")
    deps = fi.check_dependencies({"source_dir": str(src)}, make_platform())
    assert deps["ok"] is True, deps["errors"]
    assert any("降级" in w or "fleet_core" in w for w in deps["warnings"])


def test_check_dependencies_port_in_use(monkeypatch):
    monkeypatch.setattr(fi, "_port_in_use", lambda port, host="127.0.0.1": True)
    platform = make_platform()
    dry = fi.check_dependencies({"source_dir": SCRIPTS, "dry_run": True},
                                platform)
    assert dry["ok"] is True
    assert any("端口" in w for w in dry["warnings"])
    real = fi.check_dependencies({"source_dir": SCRIPTS}, platform)
    assert real["ok"] is False
    assert any("端口" in e for e in real["errors"])


def test_check_dependencies_missing_service_command(monkeypatch):
    monkeypatch.setattr(fi, "_which", lambda name: None)
    deps = fi.check_dependencies({"source_dir": SCRIPTS, "service": "systemd"},
                                 make_platform())
    assert any("systemctl" in w for w in deps["warnings"])
    assert any(item["what"] == "systemctl" for item in deps["missing"])
    assert deps["hints"]


def test_service_command_table():
    assert fi._service_command("systemd", {"is_root": False}) == "systemctl"
    assert fi._service_command("openrc", {}) == "rc-service"
    assert fi._service_command("termux", {}) == "sv"
    assert fi._service_command("nohup", {}) == "sh"
    assert fi._service_command("unknown", {}) == ""


# ===========================================================================
# 8. 健康检查
# ===========================================================================
def test_health_check_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def fake(port, token=None, timeout=3.0, host="127.0.0.1"):
        calls["n"] += 1
        ok = calls["n"] >= 2
        return {"ok": ok, "url": "http://x:%d/health" % port}

    monkeypatch.setattr(fi, "_health_once", fake)
    result = fi.health_check(3100, attempts=3, backoff=(0, 0, 0))
    assert result["ok"] is True
    assert result["attempts"] == 2
    assert len(result["history"]) == 2


def test_health_check_all_fail(monkeypatch):
    monkeypatch.setattr(fi, "_health_once",
                        lambda *a, **k: {"ok": False, "url": "u"})
    result = fi.health_check(3100, attempts=3, backoff=(0, 0, 0))
    assert result["ok"] is False
    assert result["attempts"] == 3
    assert len(result["history"]) == 3


def test_health_once_success(monkeypatch):
    monkeypatch.setattr(fi.urllib.request, "build_opener",
                        lambda *a, **k: types.SimpleNamespace(
                            open=lambda req, timeout=None: FakeResponse()))
    result = fi._health_once(3100)
    assert result["ok"] is True
    assert result["status_code"] == 200


def test_health_once_network_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(fi.urllib.request, "build_opener",
                        lambda *a, **k: types.SimpleNamespace(open=boom))
    result = fi._health_once(3100)
    assert result["ok"] is False
    assert "error" in result


# ===========================================================================
# 9. 卸载与路径安全
# ===========================================================================
@pytest.mark.parametrize("path", [
    "", "   ", "/", "//", "~", "$HOME", "/usr", "/usr/local", "/etc",
    "/var", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys", "/root",
    "/home", "/opt", "/tmp", "/Users", "/System", "/Library", "/Applications",
])
def test_is_safe_removal_path_rejects_dangerous(path):
    assert fi.is_safe_removal_path(path) is False


def test_is_safe_removal_path_rejects_home_and_ancestors():
    home = os.path.expanduser("~")
    assert fi.is_safe_removal_path(home) is False
    parent = os.path.dirname(home)
    assert fi.is_safe_removal_path(parent) is False


def test_is_safe_removal_path_allows_normal_dirs(tmp_path):
    assert fi.is_safe_removal_path(str(tmp_path / "inst")) is True
    assert fi.is_safe_removal_path("/opt/mcp-fleet/node") is True
    assert fi.is_safe_removal_path(
        os.path.join(tempfile.gettempdir(), "mcp-fleet-test")) is True


def test_uninstall_keeps_data_by_default(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    result = fi.uninstall(dict(opts))
    assert result["ok"] is True, result["errors"]
    assert not os.path.exists(opts["dir"]), "安装目录应被清理"
    assert os.path.isdir(opts["data_dir"]), "数据目录默认必须保留"
    assert opts["data_dir"] in result["kept"]
    assert any("--purge" in w for w in result["warnings"])
    assert result["purged"] is False


def test_uninstall_purge_removes_data_dir(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    result = fi.uninstall(dict(opts, purge=True))
    assert result["ok"] is True, result["errors"]
    assert result["purged"] is True
    assert not os.path.exists(opts["data_dir"])
    assert not os.path.exists(opts["dir"])


def test_uninstall_when_not_installed(tmp_path):
    opts = make_opts(tmp_path)
    result = fi.uninstall(dict(opts))
    assert result["ok"] is False
    assert result["installed"] is False
    assert any("未找到安装记录" in e for e in result["errors"])


def test_uninstall_refuses_dangerous_manifest_paths(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    manifest_path = os.path.join(opts["data_dir"], "manifest.json")
    manifest = read_json(manifest_path)
    manifest["files"] = ["/", "~", "$HOME", "/usr/bin/python3", str(sentinel)]
    manifest["unit_paths"] = ["/etc/systemd/system/mcp-fleet-evil.service"]
    manifest["dirs"] = ["/", os.path.expanduser("~"), opts["dir"]]
    fi._atomic_write(manifest_path, json.dumps(manifest), 0o600)

    result = fi.uninstall(dict(opts))
    assert os.path.isdir(os.path.expanduser("~"))
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    joined = " ".join(result["warnings"])
    assert "拒绝删除危险路径" in joined
    assert "拒绝删除危险目录" in joined


def test_uninstall_refuses_foreign_unit_outside_install_dir(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = outside / "ssh.service"
    foreign.write_text("foreign unit", encoding="utf-8")
    manifest_path = os.path.join(opts["data_dir"], "manifest.json")
    manifest = read_json(manifest_path)
    manifest["unit_paths"] = [str(foreign)]
    fi._atomic_write(manifest_path, json.dumps(manifest), 0o600)

    result = fi.uninstall(dict(opts))
    assert foreign.is_file(), "不属于本安装器的服务文件不能被删除"
    assert any("非本安装器" in w for w in result["warnings"])


def test_uninstall_removes_installer_owned_ctl(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    ctl_path = os.path.join(opts["dir"], fi.CTL_NAME)
    with open(ctl_path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    result = fi.uninstall(dict(opts))
    assert result["ok"] is True, result["errors"]
    assert ctl_path in result["removed"]
    assert not os.path.exists(ctl_path)


def test_uninstall_purge_mismatch_guard(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    manifest_path = os.path.join(opts["data_dir"], "manifest.json")
    manifest = read_json(manifest_path)
    manifest["data_dir"] = str(tmp_path / "other-data")
    fi._atomic_write(manifest_path, json.dumps(manifest), 0o600)

    result = fi.uninstall(dict(opts, purge=True))
    assert result["ok"] is False
    assert any("不一致" in e for e in result["errors"])
    assert os.path.isdir(opts["data_dir"]), "purge 校验失败时不能删数据"


def test_removal_allowed_direct(tmp_path):
    opts, paths = paths_of(tmp_path)
    inside = os.path.join(paths["install_dir"], "mcp_agent.py")
    ok, reason = fi._removal_allowed(inside, paths, set(), False)
    assert ok is True and reason == ""
    ok, reason = fi._removal_allowed("/", paths, set(), False)
    assert ok is False and "危险" in reason
    ok, reason = fi._removal_allowed(str(tmp_path / "elsewhere.txt"), paths,
                                     set(), False)
    assert ok is False and "管理范围" in reason
    foreign = str(tmp_path / "elsewhere" / "ssh.service")
    ok, reason = fi._removal_allowed(foreign, paths,
                                     set([fi._path_key(foreign)]), False)
    assert ok is False and "非本安装器" in reason


def test_owned_unit_path_rules(tmp_path):
    opts, paths = paths_of(tmp_path)
    assert fi._owned_unit_path(
        os.path.join(paths["install_dir"], "fleet-ctl.sh"), paths) is True
    assert fi._owned_unit_path("/etc/systemd/system/mcp-fleet-x.service",
                               paths) is True
    assert fi._owned_unit_path("/etc/systemd/system/ssh.service",
                               paths) is False


def test_manifest_roundtrip_and_corrupt(tmp_path):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    _, paths = paths_of(tmp_path)
    manifest = fi._load_manifest({}, paths)
    assert manifest and manifest["name"] == "testnode"
    assert manifest["service"] == "nohup"
    fi._atomic_write(paths["manifest_path"], "{broken", 0o600)
    if os.path.exists(paths["manifest_pointer"]):
        os.remove(paths["manifest_pointer"])
    assert fi._load_manifest({}, paths) is None


def test_manifest_paths_correction(tmp_path):
    manifest = {
        "name": "from-manifest",
        "install_dir": str(tmp_path / "mf-inst"),
        "data_dir": str(tmp_path / "mf-data"),
    }
    opts, paths = fi._manifest_paths(manifest, {}, make_platform())
    assert opts["name"] == "from-manifest"
    assert paths["install_dir"] == str(tmp_path / "mf-inst")
    assert paths["data_dir"] == str(tmp_path / "mf-data")


# ===========================================================================
# 10. 状态查询
# ===========================================================================
def test_status_after_install(tmp_path, monkeypatch):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    monkeypatch.setattr(fi, "_health_once", lambda *a, **k: {"ok": False})
    result = fi.status({
        "name": "testnode",
        "dir": opts["dir"],
        "data_dir": opts["data_dir"],
        "platform": make_platform(),
    })
    assert result["ok"] is True
    assert result["installed"] is True
    assert result["service"] == "nohup"
    assert result["port"] == 3100
    assert result["healthy"] is False
    assert result["paths"]["install_dir"] == opts["dir"]


def test_status_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "_health_once", lambda *a, **k: {"ok": False})
    result = fi.status({
        "name": "ghost",
        "dir": str(tmp_path / "none"),
        "data_dir": str(tmp_path / "none-data"),
        "platform": make_platform(),
    })
    assert result["installed"] is False
    assert result["ok"] is True


# ===========================================================================
# 11. CLI 行为（--json / --dry-run / --version / 错误码）
# ===========================================================================
@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setattr(fi, "detect_platform", lambda: make_platform())
    src = make_source(tmp_path)
    return {"src": src, "tmp": tmp_path, "platform": make_platform()}


def test_cli_dry_run_json_single_line(cli_env, capsys):
    inst = str(cli_env["tmp"] / "inst")
    code = fi.main(["--name", "cli-node", "--port", "3100", "--dry-run",
                    "--json", "--dir", inst, "--data-dir",
                    str(cli_env["tmp"] / "data"),
                    "--source-dir", cli_env["src"]])
    captured = capsys.readouterr()
    assert code == 0
    lines = [ln for ln in captured.out.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, " --json 必须只有一行 stdout：%r" % captured.out
    payload = json.loads(lines[0])
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["version"] == fi.VERSION
    assert payload["service"] in fi.SERVICE_KEYS
    assert "platform" in payload and "paths" in payload
    assert not os.path.exists(inst), "dry-run 不能落盘"


def test_cli_json_install_without_service(cli_env, capsys):
    inst = str(cli_env["tmp"] / "inst")
    data = str(cli_env["tmp"] / "data")
    code = fi.main(["--name", "cli-node", "--port", "3100", "--json",
                    "--no-service", "--tls", "off", "--dir", inst,
                    "--data-dir", data, "--source-dir", cli_env["src"]])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True, payload["errors"]
    assert os.path.isfile(os.path.join(inst, "mcp_agent_config.json"))
    assert payload["start"]["skipped"] is True


def test_cli_print_unit_plain(cli_env, capsys):
    code = fi.main(["--name", "unit-node", "--print-unit",
                    "--service", "systemd", "--tls", "off",
                    "--unit-dir", str(cli_env["tmp"] / "units"),
                    "--dir", str(cli_env["tmp"] / "inst")])
    captured = capsys.readouterr()
    assert code == 0
    assert "[Unit]" in captured.out
    assert "NoNewPrivileges=yes" in captured.out


def test_cli_print_unit_json(cli_env, capsys):
    code = fi.main(["--name", "unit-node", "--print-unit", "--json",
                    "--service", "launchd", "--tls", "off",
                    "--unit-dir", str(cli_env["tmp"] / "units"),
                    "--dir", str(cli_env["tmp"] / "inst")])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip().splitlines()[0])
    assert payload["service"] == "launchd"
    assert "<key>KeepAlive</key>" in payload["unit"]


def test_cli_version(cli_env, capsys):
    assert fi.main(["--version"]) == 0
    captured = capsys.readouterr()
    assert fi.VERSION in captured.out


def test_cli_invalid_mode_json(cli_env, capsys):
    code = fi.main(["--name", "x", "--mode", "bogus", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out.strip().splitlines()[0])
    assert payload["ok"] is False
    assert any("未知" in e for e in payload["errors"])


def test_cli_bad_port_is_usage_error(cli_env, capsys):
    assert fi.main(["--name", "x", "--port", "not-a-number"]) == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower() or "error" in captured.err.lower()


def test_cli_port_out_of_range_json(cli_env, capsys):
    code = fi.main(["--name", "x", "--port", "70000", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out.strip().splitlines()[0])
    assert any("范围" in e for e in payload["errors"])


def test_cli_human_dry_run_output(cli_env, capsys):
    code = fi.main(["--name", "human-node", "--dry-run", "--tls", "off",
                    "--dir", str(cli_env["tmp"] / "inst"),
                    "--source-dir", cli_env["src"]])
    captured = capsys.readouterr()
    assert code == 0
    assert "MCP Fleet" in captured.out
    assert "dry-run" in captured.out or "预演" in captured.out


def test_cli_status_json(cli_env, tmp_path, capsys, monkeypatch):
    inst = str(cli_env["tmp"] / "inst")
    data = str(cli_env["tmp"] / "data")
    assert fi.main(["--name", "cli-node", "--json", "--no-service",
                    "--tls", "off", "--dir", inst, "--data-dir", data,
                    "--source-dir", cli_env["src"]]) == 0
    capsys.readouterr()
    monkeypatch.setattr(fi, "_health_once", lambda *a, **k: {"ok": False})
    code = fi.main(["--name", "cli-node", "--status", "--json",
                    "--dir", inst, "--data-dir", data])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip().splitlines()[0])
    assert payload["installed"] is True
    assert payload["port"] == 3100


def test_cli_uninstall_json(cli_env, capsys):
    inst = str(cli_env["tmp"] / "inst")
    data = str(cli_env["tmp"] / "data")
    assert fi.main(["--name", "cli-node", "--json", "--no-service",
                    "--tls", "off", "--dir", inst, "--data-dir", data,
                    "--source-dir", cli_env["src"]]) == 0
    capsys.readouterr()
    code = fi.main(["--name", "cli-node", "--uninstall", "--json",
                    "--dir", inst, "--data-dir", data])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip().splitlines()[0])
    assert payload["ok"] is True, payload["errors"]
    assert payload["installed"] is True
    assert not os.path.exists(inst)


def test_cli_legacy_invite_and_tunnel_mapping():
    parser = fi._build_parser()
    args = parser.parse_args(["--name", "x", "--invite", "fleet://join/abc"])
    opts = fi._namespace_to_opts(args)
    assert opts["join"] == "fleet://join/abc"

    args = parser.parse_args(["--name", "x", "--invite", "PLAINCODE"])
    opts = fi._namespace_to_opts(args)
    assert any("--invite" in w for w in opts["_legacy_warnings"])

    args = parser.parse_args(["--name", "x", "--tunnel", "--frp-server", "h"])
    opts = fi._namespace_to_opts(args)
    assert "tunnel" not in opts
    assert any("隧道" in w or "frp" in w for w in opts["_legacy_warnings"])

    args = parser.parse_args(["--name", "x", "--auto-start"])
    opts = fi._namespace_to_opts(args)
    assert any("--auto-start" in w for w in opts["_legacy_warnings"])


# ===========================================================================
# 12. 安装脚本语法（sh -n / bash -n）
# ===========================================================================
def test_install_sh_syntax():
    path = os.path.join(SCRIPTS, "install.sh")
    assert os.path.isfile(path)
    shell = shutil.which("sh")
    assert shell, "需要 sh 才能做语法检查"
    proc = subprocess.run([shell, "-n", path], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("script", ["install.sh", "check-deps.sh"])
def test_shell_scripts_syntax(script):
    path = os.path.join(SCRIPTS, script)
    if not os.path.isfile(path):
        path = os.path.join(fi.TEMPLATE_DIR, script)
    assert os.path.isfile(path), "找不到 %s" % script
    proc = subprocess.run(["sh", "-n", path], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_install_sh_no_crlf():
    path = os.path.join(SCRIPTS, "install.sh")
    with open(path, "rb") as handle:
        head = handle.read(4096)
    assert b"\r\n" not in head, "install.sh 不能有 CRLF（Linux 下 shebang 会失效）"


# ===========================================================================
# 13. 常量与适配器表
# ===========================================================================
def test_version_and_adapter_registry():
    assert fi.VERSION == "5.2.0"
    assert set(fi.ADAPTERS) == set(fi.SERVICE_KEYS) - {"auto"}
    for key, cls in fi.ADAPTERS.items():
        assert cls.key == key
        assert cls.display


def test_adapter_requires_root_flags(tmp_path):
    opts, paths = paths_of(tmp_path)
    assert fi.OpenRCAdapter(opts, make_platform(),
                            paths).requires_root() is True
    assert fi.ProcdAdapter(opts, make_platform(),
                           paths).requires_root() is True
    assert fi.NohupAdapter(opts, make_platform(),
                           paths).requires_root() is False


def test_select_service_auto_and_explicit():
    platform = make_platform(service_manager="systemd")
    assert fi._select_service({"service": "auto"}, platform) == "systemd"
    assert fi._select_service({"service": "docker"}, platform) == "docker"
    assert fi._select_service({"mode": "nohup"}, platform) == "nohup"
    with pytest.raises(fi.InstallerError):
        fi._select_service({"service": "bogus"}, platform)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# v5.2: 默认鉴权 / 全局命令 / 升级回滚
# ---------------------------------------------------------------------------
def test_default_auth_generated(tmp_path):
    opts = make_opts(tmp_path)
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    cfg = read_json(os.path.join(opts["dir"], "mcp_agent_config.json"))
    assert len(cfg["auth_token"]) >= 32
    assert result["auth"]["enabled"] is True
    assert result["auth"]["created"] is True
    # JSON 结果不泄露 token
    assert cfg["auth_token"] not in json.dumps(result)


def test_no_auth_flag(tmp_path):
    opts = make_opts(tmp_path, no_auth=True)
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    cfg = read_json(os.path.join(opts["dir"], "mcp_agent_config.json"))
    assert cfg["auth_token"] == ""
    assert result["auth"]["disabled"] is True
    assert any("--no-auth" in w for w in result["warnings"])


def test_launcher_created_and_removable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FLEET_NO_LAUNCHER", "")
    opts = make_opts(tmp_path, no_launcher=False)
    # 平台按非 root、非 Termux 处理
    opts["platform"]["is_termux"] = False
    opts["platform"]["os"] = "linux"
    result = fi.install(opts)
    assert result["ok"] is True, result["errors"]
    launcher = result["launcher"]
    assert launcher["ok"] is True
    assert os.path.isfile(launcher["path"])
    content = open(launcher["path"], encoding="utf-8").read()
    assert "fleet.py" in content
    assert os.access(launcher["path"], os.X_OK)


def test_upgrade_creates_backup(tmp_path):
    opts = make_opts(tmp_path)
    first = fi.install(opts)
    assert first["ok"] is True, first["errors"]
    marker = os.path.join(opts["dir"], "old-marker.txt")
    open(marker, "w").write("v1")
    upgraded = fi.install(dict(opts, upgrade=True))
    assert upgraded["ok"] is True, upgraded["errors"]
    backup = (upgraded.get("upgrade") or {}).get("backup")
    assert backup and os.path.isdir(backup)
    assert os.path.isfile(os.path.join(backup, "old-marker.txt"))


def test_rollback_restores_backup(tmp_path, monkeypatch):
    opts = make_opts(tmp_path)
    assert fi.install(opts)["ok"] is True
    marker = os.path.join(opts["dir"], "old-marker.txt")
    open(marker, "w").write("v1")
    up = fi.install(dict(opts, upgrade=True))
    assert up["ok"] is True, up["errors"]
    # 升级后删掉 marker，回滚应恢复
    os.remove(marker)
    monkeypatch.setattr(fi.NohupAdapter, "start", lambda self: {"ok": True})
    monkeypatch.setattr(fi.NohupAdapter, "stop", lambda self: {"ok": True})
    monkeypatch.setattr(fi, "health_check",
                        lambda *a, **k: {"ok": True, "attempts": 1})
    result = fi.rollback(opts)
    assert result["ok"] is True, result["errors"]
    assert os.path.isfile(marker)
    assert open(marker).read() == "v1"


def test_rollback_without_backup(tmp_path):
    opts = make_opts(tmp_path)
    result = fi.rollback(opts)
    assert result["ok"] is False
    assert any("没有可用备份" in e for e in result["errors"])
