#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Fleet v5.1 统一跨平台安装器（纯标准库，Python 3.6+）。

一条命令把 MCP Fleet 节点装到任意设备并注册开机自启：

    python3 scripts/fleet_install.py --name nas --port 3100
    python3 scripts/fleet_install.py --name nas --port 3100 --json      # 单行 JSON
    python3 scripts/fleet_install.py --name nas --dry-run               # 零副作用预演
    python3 scripts/fleet_install.py --name nas --print-unit            # 只看服务单元
    python3 scripts/fleet_install.py --name nas --join 'fleet://join/...'
    python3 scripts/fleet_install.py --name nas --uninstall [--purge]
    python3 scripts/fleet_install.py --status --json

支持的服务管理器（内部适配器，均可单测）：
    systemd / openrc / procd(OpenWrt) / launchd(macOS) / schtasks(Windows) /
    termux(sv + wake-lock) / docker(compose) / nohup(兜底)
群晖 DSM、飞牛 fnOS、威联通 QNAP、绿联 UGREEN 等无 systemd 的 NAS 自动走
nohup + 任务计划提示。

设计约束（见 docs/V5.1-CONTRACT.md 第 0/2 节）：
* 只 import 标准库；不 import mcp_agent / fleet_proto / fleet。
* 依赖缺失只给安装命令，绝不擅自安装；fleet_core 缺失时降级而不是崩溃。
* 敏感文件 0600、数据目录 0700、原子写（tmp + os.replace + fsync）。
* 卸载只删 manifest 里自己生成的路径；默认保留数据目录，--purge 才删；
  拒绝删除 /、$HOME、/usr 等危险路径。

库 API（集成方从外部 import，均为纯函数/可测试）：
    VERSION
    detect_platform() -> dict
    plan(opts)        -> dict
    install(opts)     -> dict
    uninstall(opts)   -> dict
    status(opts)      -> dict
    main(argv=None)   -> int

可直接被 mcp_agent.py / fleet.py 调用，例如：

    import fleet_install
    if fleet_install.detect_platform()["service_manager"] == "nohup":
        ...
"""

import argparse
import base64
import errno
import json
import os
import platform as _platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

VERSION = "5.1.0"

# 节点程序（安装时必须拷到安装目录，mcp_agent.py 从自己所在目录读配置）
NODE_FILES = ["mcp_agent.py", "fleet_proto.py", "dashboard_html.py"]
OPTIONAL_NODE_FILES = ["fleet_core.py", "fleet_agent.py"]
CONFIG_NAME = "mcp_agent_config.json"
MANIFEST_NAME = "manifest.json"
MANIFEST_POINTER = ".fleet-manifest.json"
IDENTITY_NAME = "identity.json"
CTL_NAME = "fleet-ctl.sh"
PID_NAME = "fleet.pid"

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "service-templates")

DEFAULT_PORT = 3100
DEFAULT_DISCOVER_PORT = 3199
DEFAULT_INVITE_TTL = 3600

SERVICE_KEYS = ["auto", "systemd", "openrc", "procd", "launchd",
                "schtasks", "termux", "docker", "nohup"]

# 测试可替换（避免真的 sleep 拖慢用例）
_sleep = time.sleep


class InstallerError(Exception):
    """安装器可预期的失败（调用方应转成结构化错误，而不是崩溃）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _log(msg):
    """进度/诊断一律写 stderr，保证 --json 的 stdout 只有一行 JSON。"""
    sys.stderr.write("  %s\n" % msg)
    try:
        sys.stderr.flush()
    except Exception:
        pass


def _run(argv, timeout=30):
    """执行外部命令，返回 (returncode, 合并输出)。命令不存在不抛异常。"""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError as exc:
        return 127, "命令不可用: %s (%s)" % (argv[0], exc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        out, _ = proc.communicate()
        return 124, (out or b"").decode("utf-8", "replace")
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _which(name):
    """等价 shutil.which；单独抽出来便于测试 monkeypatch。"""
    return shutil.which(name)


def _exists(path):
    return os.path.exists(path)


def _is_dir(path):
    return os.path.isdir(path)


def _read_text(path, limit=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        return data if limit is None else data[:limit]
    except OSError:
        return ""


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_dir(path, mode=0o700):
    """创建目录（递归）并尽力 chmod；Windows 上 chmod 基本无效，忽略失败。"""
    if not path:
        return
    if not os.path.isdir(path):
        os.makedirs(path)
    _chmod(path, mode)


def _atomic_write(path, data, mode=0o600):
    """原子写：tmp 文件 + fsync + os.replace（不落到半截文件）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(),
                            base64.urlsafe_b64encode(os.urandom(6))
                            .decode("ascii").rstrip("="))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, mode)
        # 目录项也 fsync，保证掉电后 rename 可见
        if os.name != "nt":
            try:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _path_key(path):
    """比较用路径键：展开 ~、绝对化、规范化、Windows 忽略大小写。"""
    raw = os.path.expanduser(str(path or "").strip())
    key = os.path.normpath(os.path.abspath(raw))
    if os.name == "nt":
        key = os.path.normcase(key)
    return key


def _is_within(child, parent):
    """child 是否等于 parent 或位于其下（按真实绝对路径比较）。"""
    c = _path_key(child)
    p = _path_key(parent)
    if not c or not p:
        return False
    if c == p:
        return True
    if p.endswith(os.sep):
        return c.startswith(p)
    return c.startswith(p + os.sep)


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _shell_quote(value):
    """命令行动态拼装用：含空白/特殊字符时加双引号并转义。"""
    text = str(value)
    if not re.search(r"[\s\"'$`\\]", text):
        return text
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("$", "\\$").replace("`", "\\`")
    return '"%s"' % out


def _json_line(obj):
    """--json 输出格式：单行、UTF-8 原文、无多余空格。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _gen_token():
    """32 字节 urlsafe 随机令牌（与 mcp_agent.gen_token 同格式）。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def _local_ip():
    """取一个能出网的本地 IP（UDP connect 不发包），失败回环。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _current_user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "root"


def _clean_name(name):
    """节点显示名清洗：允许中英文数字与 . _ - 空格，拒绝路径穿越字符。"""
    text = str(name or "").strip()
    text = text.replace("..", ".").replace("/", "-").replace("\\", "-")
    text = re.sub(r"[^\w\u4e00-\u9fff ._-]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    if not text:
        text = _default_name()
    return text[:48]


def _slug(name):
    """文件/服务名安全的短标识；中文名自动加哈希后缀避免碰撞。"""
    raw = str(name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw)
    slug = slug.replace("..", ".").strip("-._")
    if not slug:
        import hashlib
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        slug = "node-" + digest
    return slug[:40]


def _default_name():
    host = (socket.gethostname() or "node").split(".")[0].lower()
    prefix = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:12] or "node"
    suffix = base64.b32encode(os.urandom(3)).decode("ascii").lower()[:4]
    return "%s-%s" % (prefix, suffix)


def _port_in_use(port, host="127.0.0.1"):
    """探测端口是否已被占用（bind 成功即空闲）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        rc, out = _run(["tasklist", "/FI", "PID eq %d" % pid], timeout=10)
        return rc == 0 and str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


# ---------------------------------------------------------------------------
# 平台探测（每个维度一个可 monkeypatch 的小函数，便于跨平台单测）
# ---------------------------------------------------------------------------
def _detect_os():
    """os 字段：windows / macos / linux / freebsd / 其它 sys.platform。"""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform or "unknown"


def _detect_arch():
    """架构归一化：x86_64 / arm64 / armv7 / x86 / mips / riscv64 ..."""
    machine = (_platform.machine() or "").lower()
    alias = {
        "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
        "aarch64": "arm64", "arm64": "arm64", "armv8l": "arm64",
        "armv7l": "armv7", "armv7": "armv7", "armv6l": "armv6",
        "i386": "x86", "i486": "x86", "i586": "x86", "i686": "x86",
        "x86": "x86", "mips": "mips", "mipsel": "mipsel",
        "riscv64": "riscv64", "ppc64le": "ppc64le", "s390x": "s390x",
    }
    return alias.get(machine, machine or "unknown")


def _is_root():
    return bool(hasattr(os, "geteuid") and os.geteuid() == 0)


def _is_termux():
    prefix = os.environ.get("PREFIX", "") or ""
    if "com.termux" in prefix:
        return True
    return _exists("/data/data/com.termux/files/usr/bin/sh")


def _in_container():
    """容器识别：Docker / Podman / LXC / Kubernetes。WSL 不算容器。"""
    if _exists("/.dockerenv") or _exists("/run/.containerenv"):
        return True
    cgroup = _read_text("/proc/1/cgroup", limit=65536)
    for marker in ("docker", "kubepods", "containerd", "lxc", "podman",
                   "garden", "buildkit"):
        if marker in cgroup:
            return True
    # OpenWrt / 普通机上没有 /proc/1/cgroup 内容时按宿主处理
    return False


def _read_os_release():
    """解析 /etc/os-release（或 /usr/lib/os-release）为 dict；失败返回 {}。"""
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        text = _read_text(path, limit=65536)
        if not text:
            continue
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            result[key.strip()] = value
        if result:
            return result
    return {}


def _detect_distro(os_name, osr, is_termux=False):
    """返回 (distro, distro_version, nas_vendor)。

    nas_vendor 非空表示「无 systemd 的 NAS 生态」，安装器据此走 nohup +
    任务计划提示，而不是硬写 systemd 单元。
    """
    if os_name == "windows":
        return "windows", "", ""
    if os_name == "macos":
        return "macos", _platform.mac_ver()[0] or "", ""
    if is_termux:
        return "termux", "", ""

    osr = osr or {}
    # NAS 厂商标记（DSM / fnOS / QNAP / UGREEN）
    if _exists("/etc/synoinfo.conf"):
        return "synology", osr.get("VERSION_ID", ""), "synology"
    fnos_markers = ("/etc/fnos-release", "/etc/fnos_version", "/usr/trim")
    if any(_exists(p) for p in fnos_markers) or "fnos" in (
            osr.get("NAME", "") + osr.get("ID", "")).lower():
        return "fnos", osr.get("VERSION_ID", ""), "fnos"
    if _exists("/etc/config/uLinux.conf") or _exists("/sbin/getcfg"):
        return "qnap", osr.get("VERSION_ID", ""), "qnap"
    if _exists("/etc/ugreen") or _exists("/usr/sbin/ugreen"):
        return "ugreen", osr.get("VERSION_ID", ""), "ugreen"

    if _exists("/etc/openwrt_release"):
        return "openwrt", osr.get("VERSION_ID", ""), ""
    if _exists("/etc/alpine-release"):
        return "alpine", _read_text("/etc/alpine-release", limit=64).strip(), ""

    distro = (osr.get("ID") or osr.get("DISTRIB_ID") or "").strip().lower()
    version = (osr.get("VERSION_ID") or osr.get("VERSION") or "").strip()
    if not distro and os_name == "linux":
        distro = _read_text("/etc/debian_version", limit=32).strip() and "debian" or "linux"
    return distro or os_name, version, ""


def _detect_init(os_name, is_termux=False):
    """init 系统：systemd / openrc / procd / launchd / schtasks / termux / none。"""
    if os_name == "windows":
        return "schtasks"
    if os_name == "macos":
        return "launchd"
    if is_termux:
        return "termux"
    if _has_systemd():
        return "systemd"
    if _exists("/etc/openwrt_release") or _exists("/sbin/procd"):
        return "procd"
    if (_exists("/sbin/openrc") or _exists("/run/openrc")
            or _exists("/sbin/rc-service")):
        return "openrc"
    return "none"


def _has_systemd():
    """PID 1 是 systemd（或 /run/systemd/system 存在）才算真有 systemd。"""
    if os.name == "nt" or sys.platform == "darwin":
        return False
    if _exists("/run/systemd/system"):
        return True
    if not _which("systemctl"):
        return False
    comm = _read_text("/proc/1/comm", limit=64).strip()
    return comm in ("systemd", "init") and _which("systemctl") is not None


def _detect_service_manager(os_name, init, nas_vendor, is_container):
    """平台默认服务管理器；显式 --mode 会覆盖这里的选择。"""
    if os_name == "windows":
        return "schtasks"
    if os_name == "macos":
        return "launchd"
    if init == "termux":
        return "termux"
    if init == "systemd":
        return "systemd"
    if init == "procd":
        return "procd"
    if init == "openrc":
        return "openrc"
    # NAS / 容器 / 其它：一律 nohup 降级（安装后会给出开机自启提示）
    if nas_vendor or is_container:
        return "nohup"
    return "nohup"


def detect_platform():
    """探测运行平台，返回契约要求的平台字典。

    返回字段：os / arch / distro / distro_version / init / is_root / is_termux /
    is_container / python / python_executable / has_systemd / service_manager /
    nas_vendor / is_openwrt / is_alpine / kernel / docker_available / warnings。
    """
    os_name = _detect_os()
    arch = _detect_arch()
    osr = _read_os_release() or {}
    is_termux = _is_termux()
    is_container = _in_container()
    is_root = _is_root()
    distro, distro_version, nas_vendor = _detect_distro(os_name, osr, is_termux)
    init = _detect_init(os_name, is_termux)
    has_systemd = bool(_has_systemd()) if init == "systemd" else False
    service_manager = _detect_service_manager(os_name, init, nas_vendor,
                                              is_container)
    python_version = "%d.%d.%d" % sys.version_info[:3]
    docker_available = _which("docker") is not None

    warnings = []
    if sys.version_info < (3, 6):
        warnings.append("Python 版本 %s 低于 3.6，部分语法可能无法运行" % python_version)
    if is_termux:
        warnings.append("Termux 环境：建议 pkg install termux-services 获得 sv 托管；"
                        "开机自启需安装 Termux:Boot 插件")
    if is_container and service_manager == "nohup":
        warnings.append("检测到容器环境（默认无 systemd）：已降级为 nohup 托管，"
                        "容器重启后需重新拉起；建议 --mode docker 用 "
                        "restart: unless-stopped 保证自启")
    if nas_vendor and service_manager == "nohup":
        warnings.append("%s NAS 无标准 systemd：使用 nohup 降级托管，"
                        "安装完成后按提示在「任务计划」里添加开机任务" % nas_vendor)
    if not is_root and service_manager in ("systemd", "openrc", "procd"):
        warnings.append("非 root 运行：systemd 将装到用户级实例"
                        "（需 loginctl enable-linger 才能开机自启），"
                        "openrc/procd 建议用 sudo 重跑")
    if is_root and service_manager == "systemd":
        warnings.append("以 root 运行：服务单元写入 /etc/systemd/system，"
                        "已启用 NoNewPrivileges/PrivateTmp/ProtectSystem=strict 加固")
    if os_name == "linux" and arch in ("mips", "mipsel") and service_manager == "systemd":
        warnings.append("MIPS 架构：systemd 加固项（SystemCallArchitectures=native）"
                        "可能需要手工调整")
    if not docker_available and service_manager == "docker":
        warnings.append("未找到 docker 命令：compose 文件已生成，"
                        "装好 Docker 后 docker compose up -d 即可")
    if os_name == "windows":
        warnings.append("Windows：计划任务需在管理员 PowerShell 中注册；"
                        "当前权限不足时会打印手动注册命令")

    platform = {
        "os": os_name,
        "arch": arch,
        "distro": distro,
        "distro_version": distro_version,
        "init": init,
        "is_root": is_root,
        "is_termux": is_termux,
        "is_container": is_container,
        "python": python_version,
        "python_executable": sys.executable or "python3",
        "has_systemd": has_systemd,
        "service_manager": service_manager,
        "nas_vendor": nas_vendor,
        "is_openwrt": distro == "openwrt",
        "is_alpine": distro == "alpine",
        "kernel": _platform.release(),
        "docker_available": docker_available,
        "warnings": warnings,
    }
    return platform


# ---------------------------------------------------------------------------
# 依赖预检：只报告缺什么 + 对应安装命令，绝不擅自安装
# ---------------------------------------------------------------------------
def _install_hint(package, platform=None):
    """按平台给出包安装命令（不执行）。"""
    platform = platform or {}
    if platform.get("is_termux"):
        return "pkg update && pkg install %s" % package
    distro = (platform.get("distro") or "").lower()
    if distro == "openwrt":
        return "opkg update && opkg install %s" % package
    if distro == "alpine":
        return "apk add %s" % package
    if distro in ("debian", "ubuntu", "raspbian", "linuxmint", "pop"):
        return "apt-get update && apt-get install -y %s" % package
    if distro in ("fedora", "rhel", "centos", "rocky", "almalinux", "openeuler"):
        return "dnf install -y %s" % package
    if platform.get("os") == "macos":
        return "brew install %s" % package
    if platform.get("os") == "windows":
        return "winget install %s" % package
    return "请用系统包管理器安装 %s" % package


def _find_source_dir(opts=None):
    """定位节点源文件目录（--source-dir > cwd > 本脚本目录）。"""
    opts = opts or {}
    candidates = []
    if opts.get("source_dir"):
        candidates.append(opts["source_dir"])
    candidates.append(os.getcwd())
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for cand in candidates:
        if cand and _is_dir(cand) and _exists(os.path.join(cand, "mcp_agent.py")):
            return os.path.abspath(cand)
    return ""


def _service_command(service, platform):
    """该服务管理器依赖的外部命令（用于预检提示）。"""
    table = {
        "systemd": "systemctl",
        "openrc": "rc-service",
        "procd": "procd",
        "launchd": "launchctl",
        "schtasks": "schtasks",
        "termux": "sv",
        "docker": "docker",
        "nohup": "sh",
    }
    if service == "systemd" and not platform.get("is_root"):
        return "systemctl"
    return table.get(service, "")


def check_dependencies(opts, platform=None):
    """安装前依赖检查。

    返回 {"ok","errors","warnings","missing":[{"what","hint"}],"source_dir"}。
    缺失项只给安装命令，不执行安装。
    """
    opts = opts or {}
    platform = platform or detect_platform()
    errors = []
    warnings = []
    missing = []

    if sys.version_info < (3, 6):
        errors.append("需要 Python 3.6+，当前 %d.%d.%d" % sys.version_info[:3])
        missing.append({"what": "python3",
                        "hint": _install_hint("python3", platform)})

    source_dir = opts.get("source_dir") or _find_source_dir(opts)
    if not source_dir:
        errors.append("找不到节点源文件（mcp_agent.py 等）："
                      "请用 --source-dir 指向源码包里的 scripts/ 目录")
        missing.append({"what": "源码包", "hint": "重新下载完整离线包后重试"})
    else:
        absent = [f for f in NODE_FILES
                  if not _exists(os.path.join(source_dir, f))]
        if absent:
            errors.append("源码缺少必需文件：%s" % ", ".join(absent))
            missing.append({"what": ", ".join(absent),
                            "hint": "重新下载完整离线包（含 scripts/ 全部文件）"})
        if not _exists(os.path.join(source_dir, "fleet_core.py")):
            warnings.append("源码缺少 fleet_core.py：将降级安装"
                            "（不生成 Ed25519 身份与邀请 URI，节点仍可运行）")

    # 端口占用：真实安装前检查（dry-run 只提示）
    port = int(opts.get("port") or DEFAULT_PORT)
    if opts.get("check_port", True) and _port_in_use(port):
        msg = "端口 %d 已被占用，请换 --port 或先停掉旧节点" % port
        if opts.get("dry_run"):
            warnings.append(msg)
        else:
            errors.append(msg)

    service = (opts.get("service") or opts.get("mode") or "auto").lower()
    if service == "auto":
        service = platform.get("service_manager") or "nohup"
    command = _service_command(service, platform)
    if command and not _which(command):
        msg = "缺少 %s 命令（%s 服务管理器需要）" % (command, service)
        if service == "systemd" and platform.get("os") == "linux":
            msg += "；先安装 systemd，或改用 --mode nohup"
        warnings.append(msg)
        missing.append({"what": command,
                        "hint": _install_hint(command, platform)})

    # 目录可写性（root 场景下 /opt 通常可写；仅提示）
    install_dir = opts.get("dir") or opts.get("install_dir")
    if install_dir:
        parent = os.path.dirname(os.path.abspath(os.path.expanduser(install_dir)))
        if _exists(parent) and not os.access(parent, os.W_OK) and not platform.get("is_root"):
            warnings.append("目录 %s 不可写：请 sudo 运行或改用 --dir ~/..." % parent)

    deps = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "missing": missing,
        "source_dir": source_dir,
        "hints": [item["hint"] for item in missing],
    }
    return deps


# ---------------------------------------------------------------------------
# 路径规划
# ---------------------------------------------------------------------------
def _default_install_dir(platform, slug):
    home = os.path.expanduser("~")
    if platform.get("os") == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "mcp-fleet", slug)
    if platform.get("is_termux"):
        return os.path.join(home, ".mcp-fleet", slug)
    if platform.get("is_root") and platform.get("os") == "linux":
        return os.path.join("/opt", "mcp-fleet", slug)
    if platform.get("is_root") and platform.get("os") == "macos":
        return os.path.join("/usr/local/var", "mcp-fleet", slug)
    return os.path.join(home, ".mcp-fleet", slug)


def _default_data_dir(platform, slug, install_dir):
    if platform.get("os") == "windows":
        base = os.environ.get("PROGRAMDATA") or os.path.dirname(install_dir)
        if platform.get("is_root"):
            return os.path.join(base, "mcp-fleet", slug, "data")
        return os.path.join(install_dir, "data")
    if platform.get("is_root") and platform.get("os") == "linux" \
            and not platform.get("is_termux") and not platform.get("is_container"):
        return os.path.join("/var/lib", "mcp-fleet", slug)
    return os.path.join(install_dir, "data")


def resolve_paths(opts, platform=None):
    """根据 opts/platform 计算所有安装路径（纯计算，无副作用）。"""
    opts = opts or {}
    platform = platform or detect_platform()
    slug = _slug(opts.get("name") or _default_name())
    install_dir = (opts.get("dir") or opts.get("install_dir")
                   or _default_install_dir(platform, slug))
    install_dir = os.path.abspath(os.path.expanduser(install_dir))
    data_dir = (opts.get("data_dir")
                or _default_data_dir(platform, slug, install_dir))
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    log_dir = os.path.abspath(os.path.expanduser(
        opts.get("log_dir") or os.path.join(data_dir, "logs")))
    unit_label = "mcp-fleet-" + slug
    return {
        "slug": slug,
        "unit_label": unit_label,
        "launchd_label": "com.mcpfleet.node." + slug,
        "install_dir": install_dir,
        "data_dir": data_dir,
        "log_dir": log_dir,
        "config_path": os.path.join(install_dir, CONFIG_NAME),
        "manifest_path": os.path.join(data_dir, MANIFEST_NAME),
        "manifest_pointer": os.path.join(install_dir, MANIFEST_POINTER),
        "identity_path": os.path.join(data_dir, IDENTITY_NAME),
        "pid_file": os.path.join(data_dir, PID_NAME),
        "ctl_path": os.path.join(install_dir, CTL_NAME),
        "agent_path": os.path.join(install_dir, "mcp_agent.py"),
    }


def _template_vars(opts, platform, paths):
    """把通用变量映射成模板占位符字典（适配器再追加自己的变量）。"""
    opts = opts or {}
    platform = platform or {}
    is_root = bool(platform.get("is_root"))
    user = opts.get("user") or ("root" if is_root else _current_user())
    python = opts.get("python") or sys.executable or (
        "python.exe" if platform.get("os") == "windows" else "python3")
    agent = paths["agent_path"]
    extra = _extra_agent_args(opts)
    extra_win = " " + " ".join(extra) if extra else ""
    home = os.path.expanduser("~")
    install_dir = paths["install_dir"]

    # 非 root 安装到用户目录时不能 ProtectHome=read-only（会把安装目录一起只读）
    protect_home = ""
    if is_root and user in ("", "root") and not _is_within(install_dir, home):
        protect_home = "ProtectHome=read-only"
    user_line = ""
    if is_root and user and user != "root":
        user_line = "User=%s" % user
    username_block = ""
    if is_root and user and user != "root":
        username_block = ("    <key>UserName</key>\n    <string>%s</string>"
                          % _xml_escape(user))

    copy_prefix = opts.get("_copy_prefix", "scripts/")
    copy_node_line = "COPY %s %s %s ./" % (
        copy_prefix + "mcp_agent.py", copy_prefix + "fleet_proto.py",
        copy_prefix + "dashboard_html.py")
    copy_config_line = copy_prefix + "mcp_agent_config.example.json"

    variables = {
        "VERSION": VERSION,
        "NAME": paths["slug"],
        "DISPLAY_NAME": opts.get("name") or paths["slug"],
        "PORT": str(int(opts.get("port") or DEFAULT_PORT)),
        "HOST": opts.get("host") or "0.0.0.0",
        "PYTHON": python,
        "CMD_PYTHON": _shell_quote(python),
        "CMD_PYTHON_WIN": '\\"%s\\"' % python if " " in python else python,
        "AGENT_PATH": agent,
        "CMD_AGENT": _shell_quote(agent),
        "INSTALL_DIR": install_dir,
        "DATA_DIR": paths["data_dir"],
        "LOG_DIR": paths["log_dir"],
        "CONFIG_PATH": paths["config_path"],
        "PID_FILE": paths["pid_file"],
        "CTL_PATH": paths["ctl_path"],
        "UNIT_LABEL": paths["unit_label"],
        "LAUNCHD_LABEL": paths["launchd_label"],
        "TASK_NAME": "MCPFleet-" + paths["slug"],
        "UNIT_PATH": "",
        "IMAGE_TAG": "mcp-fleet:%s" % VERSION,
        "SERVICE_USER": user or "root",
        "SERVICE_GROUP": opts.get("group") or user or "root",
        "USER_LINE": user_line,
        "HOME_LINE": protect_home,
        "WATCHDOG_LINE": "",
        "USERNAME_BLOCK": username_block,
        "EXTRA_ARGS": (" " + " ".join(_shell_quote(a) for a in extra)) if extra else "",
        "EXTRA_ARGS_WIN": extra_win,
        "EXTRA_PLIST_ARGS": "".join(
            "\n        <string>%s</string>" % _xml_escape(a) for a in extra),
        "COPY_NODE_LINE": copy_node_line,
        "COPY_CONFIG_LINE": copy_config_line,
    }
    return variables


def _extra_agent_args(opts):
    """要透传给 mcp_agent.py 的额外参数（只读 / TLS / 强制鉴权）。"""
    args = []
    if opts.get("read_only"):
        args.append("--read-only")
    cert = opts.get("_tls_cert") or opts.get("tls_cert") or ""
    key = opts.get("_tls_key") or opts.get("tls_key") or ""
    if cert and key:
        args.extend(["--tls-cert", cert, "--tls-key", key])
    if opts.get("require_auth"):
        args.append("--require-auth")
    return args


# ---------------------------------------------------------------------------
# 服务模板
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def load_template(name):
    """读取 service-templates/<name>；缺失时抛可读错误。"""
    path = os.path.join(TEMPLATE_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise InstallerError("缺少服务模板 %s（%s）" % (path, exc))


def render_template(text, variables):
    """做 {{VAR}} 变量替换；有未定义/未替换变量直接失败（防半成品单元）。"""
    missing = []

    def _sub(match):
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return match.group(0)
        return str(variables[key])

    rendered = _TOKEN_RE.sub(_sub, text)
    if missing:
        raise InstallerError("服务模板缺少变量: %s" % ", ".join(sorted(set(missing))))
    if "{{" in rendered or "}}" in rendered:
        raise InstallerError("服务模板存在未替换的占位符")
    return rendered


# ---------------------------------------------------------------------------
# 版本/路径小工具（供 CLI 与集成方使用）
# ---------------------------------------------------------------------------
def _normalize_opts(opts):
    """归一化安装参数：兼容旧参数名、补默认值、清洗名称。"""
    raw = dict(opts or {})
    normalized = {}
    for key, value in raw.items():
        if value is None:
            continue
        normalized[key] = value

    name = raw.get("name") or _default_name()
    normalized["name"] = _clean_name(name)
    try:
        normalized["port"] = int(raw.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        raise InstallerError("--port 必须是整数")
    if not (1 <= normalized["port"] <= 65535):
        raise InstallerError("--port 超出范围 1-65535")

    if raw.get("install_dir") and not raw.get("dir"):
        normalized["dir"] = raw["install_dir"]
    if raw.get("mode") and not raw.get("service"):
        normalized["service"] = raw["mode"]
    service = str(raw.get("service") or raw.get("mode") or "auto").lower()
    if service not in SERVICE_KEYS:
        raise InstallerError("未知 --mode/--service: %s（可选 %s）"
                             % (service, ", ".join(SERVICE_KEYS)))
    normalized["service"] = service
    tls = str(raw.get("tls") or "auto").lower()
    if tls not in ("auto", "off"):
        raise InstallerError("--tls 只支持 auto 或 off")
    normalized["tls"] = tls
    normalized["dry_run"] = bool(raw.get("dry_run"))
    normalized["json"] = bool(raw.get("json"))
    normalized["read_only"] = bool(raw.get("read_only"))
    normalized["no_service"] = bool(raw.get("no_service"))
    normalized["purge"] = bool(raw.get("purge"))
    normalized["relay"] = bool(raw.get("relay"))
    normalized["require_auth"] = bool(raw.get("require_auth"))
    normalized["generate_token"] = bool(raw.get("generate_token"))
    normalized["print_unit"] = bool(raw.get("print_unit"))
    if "user" not in normalized:
        normalized["user"] = ""
    return normalized


# ---------------------------------------------------------------------------
# 服务适配器
# ---------------------------------------------------------------------------
def _spawn_detached(argv, cwd, log_path):
    """脱离父进程拉起常驻进程（nohup / Termux 降级用），返回 pid。"""
    if os.name == "nt":
        flags = 0x00000008  # DETACHED_PROCESS
        kwargs = {"creationflags": flags}
    else:
        kwargs = {"start_new_session": True}
    handle = open(log_path, "ab")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT,
                                **kwargs)
    finally:
        handle.close()
    return proc.pid


class ServiceAdapter(object):
    """服务适配器基类：渲染单元、落盘、启停、状态查询。"""

    key = "nohup"
    display = "nohup 降级"
    default_mode = 0o644

    def __init__(self, opts, platform, paths):
        self.opts = opts or {}
        self.platform = platform or {}
        self.paths = paths or {}

    # ---- 渲染与落盘 ----
    def template_name(self):
        """该适配器主模板文件名；None 表示不渲染主单元。"""
        return None

    def unit_path(self):
        """服务单元/控制文件落盘路径；空串表示无文件。"""
        return ""

    def unit_paths(self):
        """该适配器安装的所有单元类文件路径（卸载用）。"""
        path = self.unit_path()
        return [path] if path else []

    def extra_vars(self):
        """适配器专属模板变量。"""
        return {}

    def extra_files(self):
        """除主单元外的附属文件 [(path, content, mode)]。"""
        return []

    def render(self):
        name = self.template_name()
        if not name:
            raise InstallerError("%s 适配器没有服务单元模板" % self.key)
        variables = _template_vars(self.opts, self.platform, self.paths)
        variables.update(self.extra_vars())
        return render_template(load_template(name), variables)

    def install_unit(self, content=None, mode=None):
        """把服务单元写到目标位置，返回 (written_paths, errors)。"""
        content = self.render() if content is None else content
        path = self.unit_path()
        written = []
        errors = []
        if path:
            try:
                _ensure_dir(os.path.dirname(path), 0o755)
                _atomic_write(path, content, mode or self.default_mode)
                written.append(path)
            except OSError as exc:
                errors.append("写入服务单元失败 %s: %s（可能需要 sudo）"
                              % (path, exc))
        for extra_path, extra_content, extra_mode in self.extra_files():
            try:
                _ensure_dir(os.path.dirname(extra_path), 0o755)
                _atomic_write(extra_path, extra_content, extra_mode)
                written.append(extra_path)
            except OSError as exc:
                errors.append("写入附属文件失败 %s: %s" % (extra_path, exc))
        return written, errors

    # ---- 生命周期 ----
    def start(self):
        return {"ok": False, "method": self.key,
                "detail": "%s 适配器未实现启动" % self.key, "commands": []}

    def stop(self):
        return {"ok": True, "method": self.key, "detail": "无需停止"}

    def status(self):
        return {"running": None, "detail": "unknown"}

    def requires_root(self):
        return False

    def notes(self):
        """安装后给用户的额外提示（开机自启/手工操作）。"""
        return []

    def manual_hint(self):
        """如何手工启停这个服务。"""
        return ""


class SystemdAdapter(ServiceAdapter):
    key = "systemd"
    display = "systemd"

    def template_name(self):
        return "fleet.service"

    def user_mode(self):
        """非 root 用 systemd 用户实例（--user），否则系统级。"""
        return not bool(self.platform.get("is_root"))

    def unit_dir(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        if self.user_mode():
            return os.path.join(os.path.expanduser("~"), ".config",
                                "systemd", "user")
        return "/etc/systemd/system"

    def unit_path(self):
        return os.path.join(self.unit_dir(), self.paths["unit_label"] + ".service")

    def requires_root(self):
        return not self.user_mode()

    def _prefix(self):
        return ["systemctl"] + (["--user"] if self.user_mode() else [])

    def start(self):
        prefix = self._prefix()
        label = self.paths["unit_label"]
        commands = []
        ok = True
        for sub in (["daemon-reload"], ["enable", label], ["restart", label]):
            rc, out = _run(prefix + sub, timeout=90)
            commands.append({"cmd": " ".join(prefix + sub), "rc": rc,
                             "output": out.strip()[:300]})
            if rc != 0:
                ok = False
        warnings = []
        if self.user_mode():
            warnings.append("用户级 systemd 需要 loginctl enable-linger %s "
                            "才能在未登录时自启" % _current_user())
        state = self.status()
        if state.get("running") is False:
            ok = False
            warnings.append("服务未处于 active 状态，请 systemctl%s status %s 查看日志"
                            % (" --user" if self.user_mode() else "", label))
        return {"ok": ok, "method": self.key, "commands": commands,
                "detail": state.get("detail", ""), "warnings": warnings}

    def stop(self):
        prefix = self._prefix()
        label = self.paths["unit_label"]
        commands = []
        for sub in (["stop", label], ["disable", label]):
            rc, out = _run(prefix + sub, timeout=60)
            commands.append({"cmd": " ".join(prefix + sub), "rc": rc,
                             "output": out.strip()[:200]})
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "已停止并禁用 %s" % label}

    def status(self):
        label = self.paths["unit_label"]
        rc, out = _run(self._prefix() + ["is-active", label], timeout=20)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        state = lines[0].strip() if lines else "unknown"
        return {"running": rc == 0 and state == "active", "detail": state}

    def manual_hint(self):
        prefix = "systemctl --user" if self.user_mode() else "systemctl"
        return "%s status|restart %s" % (prefix, self.paths["unit_label"])


class OpenRCAdapter(ServiceAdapter):
    key = "openrc"
    display = "openrc"
    default_mode = 0o755

    def template_name(self):
        return "fleet.openrc"

    def unit_path(self):
        override = self.opts.get("unit_dir")
        base = os.path.abspath(os.path.expanduser(override)) if override else "/etc/init.d"
        return os.path.join(base, self.paths["unit_label"])

    def requires_root(self):
        return True

    def start(self):
        label = self.paths["unit_label"]
        commands = []
        ok = True
        for cmd in (["rc-update", "add", label, "default"],
                    ["rc-service", label, "restart"]):
            rc, out = _run(cmd, timeout=60)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:300]})
            if rc != 0:
                ok = False
        return {"ok": ok, "method": self.key, "commands": commands,
                "detail": "OpenRC 服务 %s" % label, "warnings": []}

    def stop(self):
        label = self.paths["unit_label"]
        commands = []
        for cmd in (["rc-service", label, "stop"],
                    ["rc-update", "del", label, "default"]):
            rc, out = _run(cmd, timeout=60)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:200]})
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "已停止 %s" % label}

    def status(self):
        rc, out = _run(["rc-service", self.paths["unit_label"], "status"], timeout=20)
        return {"running": rc == 0, "detail": out.strip().splitlines()[0]
                if out.strip() else "unknown"}

    def manual_hint(self):
        return "rc-service %s status|restart" % self.paths["unit_label"]


class ProcdAdapter(ServiceAdapter):
    key = "procd"
    display = "procd (OpenWrt)"
    default_mode = 0o755

    def template_name(self):
        return "fleet.procd"

    def unit_path(self):
        override = self.opts.get("unit_dir")
        base = os.path.abspath(os.path.expanduser(override)) if override else "/etc/init.d"
        return os.path.join(base, self.paths["unit_label"])

    def requires_root(self):
        return True

    def start(self):
        path = self.unit_path()
        commands = []
        ok = True
        for cmd in ([path, "enable"], [path, "restart"]):
            rc, out = _run(cmd, timeout=60)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:300]})
            if rc != 0:
                ok = False
        return {"ok": ok, "method": self.key, "commands": commands,
                "detail": "procd 已注册 %s" % self.paths["unit_label"],
                "warnings": []}

    def stop(self):
        path = self.unit_path()
        commands = []
        for cmd in ([path, "stop"], [path, "disable"]):
            rc, out = _run(cmd, timeout=60)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:200]})
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "已停止 %s" % self.paths["unit_label"]}

    def status(self):
        rc, out = _run([self.unit_path(), "status"], timeout=20)
        return {"running": rc == 0, "detail": out.strip()[:200] or "unknown"}

    def manual_hint(self):
        return "/etc/init.d/%s status|restart" % self.paths["unit_label"]


class LaunchdAdapter(ServiceAdapter):
    key = "launchd"
    display = "launchd (macOS)"

    def template_name(self):
        return "com.mcpfleet.node.plist"

    def unit_dir(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        if self.platform.get("is_root"):
            return "/Library/LaunchDaemons"
        return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")

    def unit_path(self):
        return os.path.join(self.unit_dir(), self.paths["launchd_label"] + ".plist")

    def requires_root(self):
        return bool(self.platform.get("is_root"))

    def start(self):
        plist = self.unit_path()
        commands = []
        # 先卸旧实例（忽略失败），再 load -w 注册并启动
        rc_unload, out_unload = _run(["launchctl", "unload", "-w", plist], timeout=30)
        commands.append({"cmd": "launchctl unload -w %s" % plist,
                         "rc": rc_unload, "output": out_unload.strip()[:200]})
        rc, out = _run(["launchctl", "load", "-w", plist], timeout=60)
        commands.append({"cmd": "launchctl load -w %s" % plist, "rc": rc,
                         "output": out.strip()[:300]})
        return {"ok": rc == 0, "method": self.key, "commands": commands,
                "detail": "launchd 标签 %s" % self.paths["launchd_label"],
                "warnings": [] if rc == 0 else ["launchctl load 失败：可尝试 sudo 或 launchctl bootstrap"]}

    def stop(self):
        plist = self.unit_path()
        rc, out = _run(["launchctl", "unload", "-w", plist], timeout=30)
        return {"ok": True, "method": self.key,
                "commands": [{"cmd": "launchctl unload -w %s" % plist,
                              "rc": rc, "output": out.strip()[:200]}],
                "detail": "已卸载 %s" % self.paths["launchd_label"]}

    def status(self):
        rc, out = _run(["launchctl", "list"], timeout=20)
        running = self.paths["launchd_label"] in out
        return {"running": running, "detail": "已注册" if running else "未在 launchctl list 中"}

    def manual_hint(self):
        return "launchctl list | grep %s" % self.paths["launchd_label"]


class SchtasksAdapter(ServiceAdapter):
    key = "schtasks"
    display = "Windows 计划任务 (schtasks)"
    default_mode = 0o644

    def template_name(self):
        return "fleet.schtasks.xml"

    def unit_path(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.join(os.path.abspath(os.path.expanduser(override)),
                                "fleet-%s.xml" % self.paths["slug"])
        return os.path.join(self.paths["install_dir"],
                            "fleet-%s.xml" % self.paths["slug"])

    def extra_vars(self):
        python = self.opts.get("python") or sys.executable or "python.exe"
        quoted = '"%s"' % python if " " in python else python
        return {"UNIT_PATH": self.unit_path(),
                "CMD_PYTHON_WIN": quoted,
                "CMD_PYTHON": quoted}

    def start(self):
        task = "MCPFleet-" + self.paths["slug"]
        path = self.unit_path()
        commands = []
        ok = True
        for cmd in (["schtasks", "/Create", "/F", "/TN", task, "/XML", path],
                    ["schtasks", "/Run", "/TN", task]):
            rc, out = _run(cmd, timeout=60)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:300]})
            if rc != 0:
                ok = False
        warnings = []
        if not ok:
            warnings.append("计划任务注册失败（通常需要管理员权限）："
                            "请在管理员 PowerShell 重跑，或手工执行 %s"
                            % commands[0]["cmd"])
        return {"ok": ok, "method": self.key, "commands": commands,
                "detail": "计划任务 %s" % task, "warnings": warnings}

    def stop(self):
        task = "MCPFleet-" + self.paths["slug"]
        commands = []
        for cmd in (["schtasks", "/End", "/TN", task],
                    ["schtasks", "/Delete", "/F", "/TN", task]):
            rc, out = _run(cmd, timeout=30)
            commands.append({"cmd": " ".join(cmd), "rc": rc,
                             "output": out.strip()[:200]})
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "已结束并删除计划任务 %s" % task}

    def status(self):
        task = "MCPFleet-" + self.paths["slug"]
        rc, out = _run(["schtasks", "/Query", "/TN", task], timeout=20)
        return {"running": rc == 0, "detail": "已注册" if rc == 0 else "未注册"}

    def manual_hint(self):
        return "schtasks /Query /TN MCPFleet-%s" % self.paths["slug"]


class TermuxAdapter(ServiceAdapter):
    key = "termux"
    display = "Termux (sv + wake-lock)"
    default_mode = 0o755

    def template_name(self):
        return "fleet.termux"

    def service_dir(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        prefix = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
        return os.path.join(prefix, "var", "service", self.paths["unit_label"])

    def unit_path(self):
        return os.path.join(self.service_dir(), "run")

    def unit_paths(self):
        # $PREFIX/var/service/<label>/ 整个目录都是安装器创建的
        return [self.unit_path(), self.service_dir()]

    def start(self):
        commands = []
        rc, out = _run(["termux-wake-lock"], timeout=20)
        commands.append({"cmd": "termux-wake-lock", "rc": rc,
                         "output": out.strip()[:120]})
        label = self.paths["unit_label"]
        warnings = []
        if _which("sv"):
            for cmd in (["sv-enable", label], ["sv", "up", label]):
                if not _which(cmd[0]):
                    continue
                rc, out = _run(cmd, timeout=30)
                commands.append({"cmd": " ".join(cmd), "rc": rc,
                                 "output": out.strip()[:200]})
            return {"ok": True, "method": self.key, "commands": commands,
                    "detail": "sv 托管 %s" % label,
                    "warnings": warnings}
        # 没有 termux-services：唤醒锁 + 脱离进程直接拉起
        try:
            pid = _spawn_detached(
                [self.opts.get("python") or sys.executable, "-u",
                 self.paths["agent_path"], "--host",
                 self.opts.get("host") or "0.0.0.0", "--port",
                 str(self.opts.get("port") or DEFAULT_PORT)]
                + _extra_agent_args(self.opts),
                self.paths["install_dir"],
                os.path.join(self.paths["log_dir"], "agent.log"))
            _atomic_write(self.paths["pid_file"], str(pid), 0o600)
        except OSError as exc:
            return {"ok": False, "method": self.key, "commands": commands,
                    "detail": "Termux 拉起失败: %s" % exc, "warnings": warnings}
        warnings.append("未安装 termux-services（pkg install termux-services），"
                        "已用唤醒锁 + 后台进程降级；sv 托管与开机自启不可用")
        warnings.append("Android 14 请在系统设置中关闭 Termux 电池优化，否则后台可能被冻结")
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "唤醒锁 + 后台进程 (pid %d)" % pid,
                "warnings": warnings}

    def stop(self):
        commands = []
        label = self.paths["unit_label"]
        if _which("sv"):
            rc, out = _run(["sv", "down", label], timeout=30)
            commands.append({"cmd": "sv down %s" % label, "rc": rc,
                             "output": out.strip()[:200]})
        pid = _read_text(self.paths["pid_file"], limit=32).strip()
        if pid and _pid_alive(pid):
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
            commands.append({"cmd": "kill %s" % pid, "rc": 0, "output": ""})
        try:
            os.remove(self.paths["pid_file"])
        except OSError:
            pass
        _run(["termux-wake-unlock"], timeout=10)
        return {"ok": True, "method": self.key, "commands": commands,
                "detail": "已停止 %s" % label}

    def status(self):
        label = self.paths["unit_label"]
        if _which("sv"):
            rc, out = _run(["sv", "status", label], timeout=20)
            return {"running": "run:" in out, "detail": out.strip()[:200] or "unknown"}
        pid = _read_text(self.paths["pid_file"], limit=32).strip()
        return {"running": _pid_alive(pid), "detail": "pid %s" % pid if pid else "未运行"}

    def manual_hint(self):
        return "sv status %s（需 termux-services）" % self.paths["unit_label"]


class DockerAdapter(ServiceAdapter):
    key = "docker"
    display = "Docker Compose"

    def template_name(self):
        return "fleet-compose.yml"

    def unit_path(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.join(os.path.abspath(os.path.expanduser(override)),
                                "fleet-compose.yml")
        return os.path.join(self.paths["install_dir"], "fleet-compose.yml")

    def extra_vars(self):
        return {"UNIT_PATH": self.unit_path(),
                "COPY_NODE_LINE": "COPY mcp_agent.py fleet_proto.py "
                                  "dashboard_html.py ./",
                "COPY_CONFIG_LINE": "mcp_agent_config.example.json"}

    def extra_files(self):
        dockerfile = render_template(
            load_template("Dockerfile"),
            dict(_template_vars(self.opts, self.platform, self.paths),
                 **self.extra_vars()))
        return [(os.path.join(os.path.dirname(self.unit_path()) or ".",
                              "Dockerfile"), dockerfile, 0o644)]

    def _compose_cmd(self):
        if not _which("docker"):
            return None
        path = self.unit_path()
        rc, _ = _run(["docker", "compose", "version"], timeout=20)
        if rc == 0:
            return ["docker", "compose", "-f", path]
        if _which("docker-compose"):
            return ["docker-compose", "-f", path]
        return None

    def start(self):
        cmd = self._compose_cmd()
        if not cmd:
            return {"ok": False, "method": self.key, "commands": [],
                    "detail": "未找到 docker/docker compose",
                    "warnings": ["compose 文件已生成：装好 Docker 后执行 "
                                 "docker compose -f %s up -d" % self.unit_path()]}
        rc, out = _run(cmd + ["up", "-d"], timeout=180)
        return {"ok": rc == 0, "method": self.key,
                "commands": [{"cmd": " ".join(cmd + ["up", "-d"]),
                              "rc": rc, "output": out.strip()[:400]}],
                "detail": "compose 已启动" if rc == 0 else out.strip()[:200],
                "warnings": []}

    def stop(self):
        cmd = self._compose_cmd()
        if not cmd:
            return {"ok": True, "method": self.key, "commands": [],
                    "detail": "未找到 docker，跳过停止"}
        rc, out = _run(cmd + ["down"], timeout=120)
        return {"ok": rc == 0, "method": self.key,
                "commands": [{"cmd": " ".join(cmd + ["down"]), "rc": rc,
                              "output": out.strip()[:300]}],
                "detail": "compose 已停止"}

    def status(self):
        name = self.paths["unit_label"]
        if not _which("docker"):
            return {"running": None, "detail": "未找到 docker"}
        rc, out = _run(["docker", "ps", "--filter", "name=%s" % name,
                        "--format", "{{.Names}}"], timeout=30)
        return {"running": rc == 0 and name in out, "detail": out.strip()[:120]}

    def manual_hint(self):
        return "docker compose -f %s ps|logs" % self.unit_path()


class NohupAdapter(ServiceAdapter):
    key = "nohup"
    display = "nohup 降级"
    default_mode = 0o755

    def template_name(self):
        return "fleet.nohup"

    def unit_path(self):
        override = self.opts.get("unit_dir")
        if override:
            return os.path.join(os.path.abspath(os.path.expanduser(override)),
                                CTL_NAME)
        return self.paths["ctl_path"]

    def requires_root(self):
        return False

    def _agent_argv(self):
        argv = [self.opts.get("python") or sys.executable or "python3", "-u",
                self.paths["agent_path"], "--host",
                self.opts.get("host") or "0.0.0.0", "--port",
                str(self.opts.get("port") or DEFAULT_PORT)]
        argv.extend(_extra_agent_args(self.opts))
        return argv

    def start(self):
        pid_text = _read_text(self.paths["pid_file"], limit=32).strip()
        if pid_text and _pid_alive(pid_text):
            return {"ok": True, "method": self.key,
                    "detail": "已在运行 (pid %s)" % pid_text,
                    "commands": [], "warnings": []}
        try:
            pid = _spawn_detached(self._agent_argv(),
                                  self.paths["install_dir"],
                                  os.path.join(self.paths["log_dir"], "agent.log"))
            _atomic_write(self.paths["pid_file"], str(pid), 0o600)
        except OSError as exc:
            return {"ok": False, "method": self.key, "commands": [],
                    "detail": "nohup 拉起失败: %s" % exc,
                    "warnings": ["请手工执行 sh %s start" % self.unit_path()]}
        return {"ok": True, "method": self.key, "commands": [],
                "detail": "后台进程已拉起 (pid %d)" % pid, "warnings": []}

    def stop(self):
        pid_text = _read_text(self.paths["pid_file"], limit=32).strip()
        if not pid_text:
            return {"ok": True, "method": self.key, "commands": [],
                    "detail": "未在运行"}
        try:
            pid = int(pid_text)
        except ValueError:
            pid = 0
        if pid and _pid_alive(pid):
            if os.name == "nt":
                _run(["taskkill", "/F", "/PID", str(pid)], timeout=20)
            else:
                try:
                    os.kill(pid, 15)
                    _sleep(0.5)
                    if _pid_alive(pid):
                        os.kill(pid, 9)
                except OSError:
                    pass
        try:
            os.remove(self.paths["pid_file"])
        except OSError:
            pass
        return {"ok": True, "method": self.key, "commands": [],
                "detail": "已停止 (pid %s)" % pid_text}

    def status(self):
        pid_text = _read_text(self.paths["pid_file"], limit=32).strip()
        running = _pid_alive(pid_text)
        return {"running": running,
                "detail": ("运行中 (pid %s)" % pid_text) if running else "未运行"}

    def notes(self):
        hints = []
        vendor = (self.platform or {}).get("nas_vendor") or ""
        start_cmd = "sh %s start" % self.unit_path()
        if vendor == "synology":
            hints.append("群晖 DSM 开机自启：控制面板 → 任务计划 → 新增 → "
                         "触发的任务 → 开机时 → 运行命令：%s" % start_cmd)
        elif vendor == "fnos":
            hints.append("飞牛 fnOS 开机自启：系统设置 → 任务计划 → 新增开机任务，"
                         "命令：%s" % start_cmd)
        elif vendor == "qnap":
            hints.append("威联通 QNAP 开机自启：控制台 → 系统 → 硬件 → "
                         "开机自动启动，或 autorun.sh 添加：%s" % start_cmd)
        elif vendor == "ugreen":
            hints.append("绿联 UGREEN 开机自启：控制面板 → 任务计划 → "
                         "新增开机任务，命令：%s" % start_cmd)
        elif (self.platform or {}).get("os") == "openwrt":
            hints.append("OpenWrt 兜底自启：把 %s 写进 /etc/rc.local（exit 0 之前）"
                         % start_cmd)
        elif (self.platform or {}).get("is_container"):
            hints.append("容器内无法自启：请用 docker restart 策略或 "
                         "--mode docker 生成 compose（restart: unless-stopped）")
        else:
            hints.append("无服务管理器：把 %s 加进 crontab 的 @reboot 可开机自启"
                         % start_cmd)
        hints.append("手动启停：%s / sh %s stop / sh %s status"
                     % (start_cmd, self.unit_path(), self.unit_path()))
        return hints

    def manual_hint(self):
        return "sh %s start|stop|status" % self.unit_path()


ADAPTERS = {
    "systemd": SystemdAdapter,
    "openrc": OpenRCAdapter,
    "procd": ProcdAdapter,
    "launchd": LaunchdAdapter,
    "schtasks": SchtasksAdapter,
    "termux": TermuxAdapter,
    "docker": DockerAdapter,
    "nohup": NohupAdapter,
}


def _select_service(opts, platform):
    """解析最终服务管理器：显式 --mode 优先，否则用平台默认。"""
    requested = str((opts or {}).get("service")
                    or (opts or {}).get("mode") or "auto").lower()
    if requested == "auto":
        return platform.get("service_manager") or "nohup"
    if requested not in ADAPTERS:
        raise InstallerError("未知服务管理器: %s（可选 %s）"
                             % (requested, ", ".join(sorted(ADAPTERS))))
    return requested


def _make_adapter(service, opts, platform, paths):
    """构造适配器实例；service 支持 'auto'。"""
    key = _select_service(opts, platform) if service in (None, "auto") else service
    if key not in ADAPTERS:
        raise InstallerError("未知服务管理器: %s" % key)
    return ADAPTERS[key](opts, platform, paths)


# ---------------------------------------------------------------------------
# 可选依赖 fleet_core：身份 / 邀请 / 自签 TLS（缺失时降级，不崩溃）
# ---------------------------------------------------------------------------
def _import_fleet_core(opts=None):
    """尝试导入 fleet_core；失败返回 None（安装器继续降级运行）。"""
    cached = sys.modules.get("fleet_core")
    if cached is not None:
        return cached
    candidates = []
    opts = opts or {}
    if opts.get("source_dir"):
        candidates.append(opts["source_dir"])
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.getcwd())
    inserted = []
    try:
        import importlib
        for directory in candidates:
            if directory and directory not in sys.path:
                sys.path.insert(0, directory)
                inserted.append(directory)
        try:
            return importlib.import_module("fleet_core")
        except Exception:
            return None
    finally:
        for directory in inserted:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass


def _core_attr(core, name):
    """安全取 fleet_core 属性；没有则 None。"""
    return getattr(core, name, None) if core is not None else None


def _setup_identity(opts, platform, paths, cfg):
    """生成/加载节点身份与邀请 URI；任何异常都降级为警告。"""
    info = {"node": {}, "join_uri": "", "warnings": []}
    if opts.get("dry_run"):
        info["warnings"].append("dry-run：跳过身份生成（正式安装才会写入 identity.json）")
        return info
    core = _import_fleet_core(opts)
    if core is None:
        info["warnings"].append("未找到 fleet_core 模块：跳过 Ed25519 身份与邀请 URI"
                                "（安装继续，节点仍可运行）")
        return info
    identity_cls = _core_attr(core, "Identity")
    if identity_cls is None:
        info["warnings"].append("fleet_core 版本过旧（无 Identity）：跳过身份生成")
        return info
    try:
        identity = identity_cls.load_or_create(paths["identity_path"])
        info["node"] = {
            "node_id": str(getattr(identity, "node_id", "")),
            "fingerprint": str(getattr(identity, "fingerprint", "")),
            "public_key_b64": str(getattr(identity, "public_key_b64", "")),
        }
    except Exception as exc:
        info["warnings"].append("节点身份生成失败（已降级继续）：%s" % exc)
        return info
    invite_cls = _core_attr(core, "Invite")
    if invite_cls is not None:
        try:
            host = (opts.get("advertise_host") or _local_ip())
            invite = invite_cls.create(
                identity, cfg.get("name") or opts.get("name") or "node",
                host, int(opts.get("port") or DEFAULT_PORT),
                ttl=int(opts.get("invite_ttl") or DEFAULT_INVITE_TTL))
            info["join_uri"] = invite.to_uri()
            info["invite"] = {"host": host,
                              "port": int(opts.get("port") or DEFAULT_PORT),
                              "ttl": int(opts.get("invite_ttl") or DEFAULT_INVITE_TTL)}
        except Exception as exc:
            info["warnings"].append("邀请 URI 生成失败（已降级继续）：%s" % exc)
    return info


def _apply_join(opts):
    """解析 --join fleet://... 邀请。fleet_core 缺失时提示而不中断安装。"""
    uri = (opts.get("join") or "").strip()
    if not uri:
        return {"ok": True, "skipped": True, "warnings": []}
    if not uri.startswith("fleet://"):
        return {"ok": False, "error": "邀请 URI 必须以 fleet:// 开头：%s" % uri,
                "warnings": []}
    core = _import_fleet_core(opts)
    invite_cls = _core_attr(core, "Invite")
    if invite_cls is None or not hasattr(invite_cls, "parse"):
        return {"ok": False, "degraded": True,
                "error": "缺少 fleet_core：无法解析邀请。安装继续；"
                         "装好后在主端执行 fleet_join 或手工配对",
                "warnings": []}
    try:
        invite = invite_cls.parse(uri)
    except Exception as exc:
        return {"ok": False,
                "error": "邀请无效或已过期：%s（安装继续，可稍后重新生成邀请）" % exc,
                "warnings": []}
    peer = {}
    for field in ("name", "node_id", "pubkey", "public_key_b64", "host",
                  "port", "ts", "ttl", "caps"):
        value = getattr(invite, field, None)
        if value is not None:
            peer[field] = value
    return {"ok": True, "peer": peer, "warnings": []}


def _setup_tls(opts, platform, paths):
    """--tls auto 时用 fleet_core 自签证书；不可用则继续 HTTP 并提示。"""
    if opts.get("tls") != "auto":
        return {"enabled": False, "skipped": True, "warnings": []}
    if opts.get("dry_run"):
        return {"enabled": False, "skipped": True, "warnings": []}
    core = _import_fleet_core(opts)
    ensure = _core_attr(core, "ensure_self_signed_cert")
    if ensure is None:
        return {"enabled": False,
                "warnings": ["fleet_core/openssl 不可用：跳过自签 TLS，继续 HTTP"
                             "（可稍后手工传 --tls-cert/--tls-key）"]}
    try:
        cert_dir = os.path.join(paths["data_dir"], "certs")
        cert_name = opts.get("advertise_host") or _local_ip()
        pair = ensure(cert_dir, common_name=cert_name)
        if not pair:
            return {"enabled": False,
                    "warnings": ["自签证书生成失败（可能缺 openssl）：继续 HTTP"]}
        cert, key = pair
        opts["_tls_cert"] = cert
        opts["_tls_key"] = key
        return {"enabled": True, "cert": cert, "key": key, "warnings": []}
    except Exception as exc:
        return {"enabled": False, "warnings": ["自签 TLS 异常（已降级）：%s" % exc]}


# ---------------------------------------------------------------------------
# 配置 / 文件落盘
# ---------------------------------------------------------------------------
def _write_config(opts, paths, source_dir):
    """合并写入 mcp_agent_config.json（0600），保留已有 token/加密相关字段。"""
    cfg = {}
    existing = _read_text(paths["config_path"], limit=1024 * 1024)
    if existing.strip():
        try:
            loaded = json.loads(existing)
            if isinstance(loaded, dict):
                cfg = loaded
        except ValueError:
            # 旧文件损坏：备份后重建，避免用户凭据被静默覆盖
            backup = paths["config_path"] + ".broken." + time.strftime("%Y%m%d%H%M%S")
            try:
                _atomic_write(backup, existing, 0o600)
            except OSError:
                pass
    example = ""
    if source_dir:
        example = _read_text(os.path.join(source_dir, "mcp_agent_config.example.json"),
                             limit=1024 * 1024)
    defaults = {}
    if example.strip():
        try:
            loaded = json.loads(example)
            if isinstance(loaded, dict):
                defaults = loaded
        except ValueError:
            pass
    # 新版本默认值补齐，但已存在配置里的 token/自定义项优先（不覆盖用户凭据）
    merged = dict(defaults)
    merged.update(cfg)
    cfg = merged

    cfg["version"] = VERSION
    cfg["data_dir"] = paths["data_dir"]
    cfg["name"] = opts.get("name") or cfg.get("name") or "node"
    cfg["port"] = int(opts.get("port") or DEFAULT_PORT)
    cfg["audit_log"] = bool(cfg.get("audit_log", True))
    cfg["discover_port"] = int(cfg.get("discover_port") or DEFAULT_DISCOVER_PORT)
    if "allowed_roots" not in cfg:
        cfg["allowed_roots"] = ["/"]
    if opts.get("read_only"):
        cfg["read_only"] = True
    if opts.get("require_auth"):
        cfg["require_auth"] = True
    if opts.get("prefix") is not None:
        cfg["tool_prefix"] = opts.get("prefix") or ""
    if opts.get("sudo_password"):
        cfg["sudo_password"] = opts.get("sudo_password")
    if opts.get("allow_ips"):
        ips = opts["allow_ips"]
        if isinstance(ips, str):
            ips = [item.strip() for item in ips.split(",") if item.strip()]
        cfg["allow_ips"] = list(ips)
    if opts.get("relay"):
        cfg["relay"] = True

    token = str(opts.get("auth_token") or "").strip()
    if token:
        cfg["auth_token"] = token
    need_token = opts.get("require_auth") or opts.get("generate_token")
    if need_token and not (cfg.get("auth_token") or ""):
        cfg["auth_token"] = _gen_token()
    cfg.setdefault("auth_token", "")

    _atomic_write(paths["config_path"],
                  json.dumps(cfg, ensure_ascii=False, indent=2), 0o600)
    return cfg


def _copy_node_files(opts, paths, source_dir):
    """把节点程序拷进安装目录；返回 (copied_files, errors, warnings)。"""
    copied = []
    errors = []
    if not source_dir:
        return copied, ["找不到节点源文件目录"], []
    install_dir = paths["install_dir"]
    if _path_key(source_dir) == _path_key(install_dir):
        return copied, [], ["安装目录就是源码目录，跳过文件拷贝"]
    names = list(NODE_FILES)
    names.extend(f for f in OPTIONAL_NODE_FILES
                 if _exists(os.path.join(source_dir, f)))
    if _exists(os.path.join(source_dir, "mcp_agent_config.example.json")):
        names.append("mcp_agent_config.example.json")
    for name in names:
        src = os.path.join(source_dir, name)
        dst = os.path.join(install_dir, name)
        if not _exists(src):
            if name in NODE_FILES:
                errors.append("缺少必需文件 %s" % src)
            continue
        try:
            shutil.copy2(src, dst)
            _chmod(dst, 0o644)
            copied.append(dst)
        except OSError as exc:
            errors.append("拷贝 %s 失败: %s" % (src, exc))
    return copied, errors, []


def _read_token(config_path):
    text = _read_text(config_path, limit=1024 * 1024)
    if not text:
        return ""
    try:
        cfg = json.loads(text)
    except ValueError:
        return ""
    return str(cfg.get("auth_token") or "")


# ---------------------------------------------------------------------------
# manifest（安装记录）：只删自己生成的东西
# ---------------------------------------------------------------------------
def _manifest_dirs(paths):
    return [paths["install_dir"], paths["data_dir"], paths["log_dir"]]


def _write_manifest(opts, platform, paths, service, adapter, unit_written,
                    copied, extra_files, result):
    """写数据目录 manifest + 安装目录指针，供安全卸载使用。"""
    files = []
    for path in list(copied) + [paths["config_path"]] + list(unit_written):
        if path and path not in files:
            files.append(path)
    manifest = {
        "version": VERSION,
        "installed_at": _now_iso(),
        "name": opts.get("name"),
        "slug": paths["slug"],
        "port": int(opts.get("port") or DEFAULT_PORT),
        "service": service,
        "python": opts.get("python") or sys.executable,
        "install_dir": paths["install_dir"],
        "data_dir": paths["data_dir"],
        "log_dir": paths["log_dir"],
        "config_path": paths["config_path"],
        "unit_paths": list(dict.fromkeys(list(unit_written)
                                         + adapter.unit_paths())),
        "files": files,
        "dirs": _manifest_dirs(paths),
        "node": result.get("node") or {},
        "join_uri": result.get("join_uri") or "",
        "platform": {
            "os": platform.get("os"),
            "arch": platform.get("arch"),
            "distro": platform.get("distro"),
            "service_manager": platform.get("service_manager"),
        },
    }
    errors = []
    try:
        _ensure_dir(paths["data_dir"], 0o700)
        _atomic_write(paths["manifest_path"],
                      json.dumps(manifest, ensure_ascii=False, indent=2), 0o600)
        _atomic_write(paths["manifest_pointer"],
                      json.dumps({"manifest_path": paths["manifest_path"],
                                  "install_dir": paths["install_dir"],
                                  "data_dir": paths["data_dir"],
                                  "name": opts.get("name")},
                                 ensure_ascii=False, indent=2), 0o600)
    except OSError as exc:
        errors.append("写入安装记录失败: %s" % exc)
    return manifest, errors


def _load_manifest(opts, paths):
    """读取安装记录（优先数据目录，其次安装目录指针）。失败返回 None。"""
    candidates = []
    if opts.get("manifest_path"):
        candidates.append(opts["manifest_path"])
    candidates.append(paths["manifest_path"])
    pointer_text = _read_text(paths["manifest_pointer"], limit=65536)
    if pointer_text.strip():
        try:
            pointer = json.loads(pointer_text)
            if isinstance(pointer, dict) and pointer.get("manifest_path"):
                candidates.append(pointer["manifest_path"])
        except ValueError:
            pass
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = _path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        text = _read_text(candidate, limit=4 * 1024 * 1024)
        if not text.strip():
            continue
        try:
            manifest = json.loads(text)
        except ValueError:
            continue
        if isinstance(manifest, dict):
            return manifest
    return None


def _manifest_paths(manifest, opts, platform):
    """用 manifest 纠正 paths（卸载/状态时用户可能没带 --name）。"""
    raw = dict(opts or {})
    name_provided = bool(raw.get("name"))
    normalized = _normalize_opts(raw)
    paths = resolve_paths(normalized, platform)
    manifest = manifest or {}
    changed = False
    if not name_provided and manifest.get("name"):
        normalized["name"] = _clean_name(manifest["name"])
        changed = True
    if not raw.get("dir") and not raw.get("install_dir") and manifest.get("install_dir"):
        normalized["dir"] = manifest["install_dir"]
        changed = True
    if not raw.get("data_dir") and manifest.get("data_dir"):
        normalized["data_dir"] = manifest["data_dir"]
        changed = True
    if changed:
        paths = resolve_paths(normalized, platform)
    return normalized, paths


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
def _health_once(port, token=None, timeout=3.0, host="127.0.0.1"):
    """单次 GET /health，返回结构化结果（不抛异常）。"""
    url = "http://%s:%d/health" % (host, int(port))
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    # 强制禁代理：本机代理软件可能把回环请求转走，误判节点宕机
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=timeout)
        try:
            code = getattr(response, "status", None) or response.getcode()
            body = response.read(64 * 1024).decode("utf-8", "replace")
        finally:
            try:
                response.close()
            except Exception:
                pass
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)[:300]}
    ok = False
    try:
        payload = json.loads(body)
        ok = int(code) == 200 and isinstance(payload, dict) \
            and payload.get("status") == "ok"
    except ValueError:
        ok = False
    return {"ok": ok, "url": url, "status_code": int(code) if code else 0,
            "body": body[:500]}


def health_check(port, token=None, attempts=3, timeout=3.0, host="127.0.0.1",
                 backoff=None):
    """HTTP /health 健康检查：最多 attempts 次，指数退避。

    返回 {"ok","attempts","url","last","history"}；全失败也不抛异常。
    """
    backoff = backoff or (0.5, 1.0, 2.0)
    history = []
    last = {"ok": False, "url": "http://%s:%d/health" % (host, int(port))}
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        last = _health_once(port, token=token, timeout=timeout, host=host)
        last["attempt"] = attempt
        history.append(last)
        if last.get("ok"):
            return {"ok": True, "attempts": attempt, "url": last["url"],
                    "last": last, "history": history}
        if attempt < total:
            delay = backoff[min(attempt - 1, len(backoff) - 1)]
            _sleep(delay)
    return {"ok": False, "attempts": total,
            "url": "http://%s:%d/health" % (host, int(port)),
            "last": last, "history": history}


# ---------------------------------------------------------------------------
# plan：零副作用安装计划
# ---------------------------------------------------------------------------
def _public_paths(paths):
    return {key: value for key, value in (paths or {}).items()
            if isinstance(value, str)}


def _build_actions(opts, platform, paths, adapter, deps_ok):
    root_needed = bool(adapter.requires_root() and not platform.get("is_root"))
    no_service = bool(opts.get("no_service"))
    actions = [
        {"id": "precheck", "kind": "check", "requires_root": False,
         "description": "预检 python3、源码文件、端口占用、服务管理器命令"},
        {"id": "dirs", "kind": "mkdir", "requires_root": root_needed,
         "description": "创建安装目录 0755、数据目录 0700、日志目录 0700"},
        {"id": "copy", "kind": "copy", "requires_root": False,
         "description": "拷贝 %s（可选 %s）到 %s" % (
             ", ".join(NODE_FILES), ", ".join(OPTIONAL_NODE_FILES),
             paths["install_dir"])},
        {"id": "config", "kind": "config", "requires_root": False,
         "path": paths["config_path"],
         "description": "合并写入配置（0600，保留已有 auth_token）"},
        {"id": "identity", "kind": "identity", "requires_root": False,
         "path": paths["identity_path"],
         "description": "生成 Ed25519 节点身份与邀请 URI（fleet_core 可用时）"},
        {"id": "tls", "kind": "tls", "requires_root": False,
         "description": "自签 TLS 证书（--tls auto 且 fleet_core/openssl 可用时）"},
        {"id": "service", "kind": "service", "requires_root": root_needed,
         "path": adapter.unit_path() or "",
         "description": "写入 %s 服务单元 %s" % (adapter.key,
                                                adapter.unit_path() or "（无）")},
        {"id": "start", "kind": "start", "requires_root": root_needed,
         "description": "注册开机自启并启动服务（%s）" % adapter.display},
        {"id": "health", "kind": "probe", "requires_root": False,
         "description": "HTTP /health 健康检查（3 次退避）"},
        {"id": "report", "kind": "report", "requires_root": False,
         "description": "输出设备码 / 邀请 URI / 下一步"},
    ]
    if no_service:
        for action in actions:
            if action["id"] in ("service", "start", "health"):
                action["skipped"] = True
                action["description"] = "--no-service：跳过 " + action["description"]
    return actions


def plan(opts=None):
    """生成安装计划（纯计算，零副作用）。返回契约要求的 actions/warnings/platform。"""
    opts = _normalize_opts(opts or {})
    platform = opts.get("platform") or detect_platform()
    paths = resolve_paths(opts, platform)
    service = _select_service(opts, platform)
    adapter = _make_adapter(service, opts, platform, paths)
    deps = check_dependencies(opts, platform)
    warnings = list(platform.get("warnings") or [])
    warnings.extend(deps.get("warnings") or [])
    actions = _build_actions(opts, platform, paths, adapter, deps.get("ok"))
    for action in actions:
        action["dry_run"] = True
    return {
        "ok": bool(deps.get("ok")),
        "dry_run": True,
        "version": VERSION,
        "service": service,
        "platform": platform,
        "paths": _public_paths(paths),
        "actions": actions,
        "warnings": warnings,
        "errors": list(deps.get("errors") or []),
        "dependencies": deps,
    }


# ---------------------------------------------------------------------------
# install：完整安装流程
# ---------------------------------------------------------------------------
def _install_next_steps(opts, platform, paths, service, adapter, result):
    port = int(opts.get("port") or DEFAULT_PORT)
    steps = []
    steps.append("验证节点：curl -s http://127.0.0.1:%d/health" % port)
    hint = adapter.manual_hint()
    if hint:
        steps.append("服务管理：%s" % hint)
    node = result.get("node") or {}
    if node.get("node_id"):
        steps.append("节点 ID：%s（指纹 %s）"
                     % (node.get("node_id"), node.get("fingerprint", "")))
    if result.get("join_uri"):
        steps.append("配对邀请：把下面这行交给主端（fleet_join 或 mcp 客户端）：")
        steps.append(result["join_uri"])
    elif not _import_fleet_core(opts):
        steps.append("配对：本机缺少 fleet_core，稍后用完整包重跑或手工填 auth_token 配对")
    if opts.get("join") and result.get("peer"):
        peer = result["peer"]
        steps.append("已识别邀请对端：%s（%s:%s）"
                     % (peer.get("name", "?"), peer.get("host", "?"),
                        peer.get("port", "?")))
    if any(a["id"] == "start" and not a.get("skipped")
           for a in (result.get("actions") or [])):
        steps.append("Dashboard：http://%s:%d/fleet/ui" % (_local_ip(), port))
    steps.extend(adapter.notes())
    steps.append("卸载：python3 fleet_install.py --name %s --uninstall"
                 "（默认保留数据目录，--purge 才删）" % opts.get("name"))
    return steps


def install(opts=None):
    """执行安装：预检 → 目录/配置 → 身份 → 服务单元 → 启动 → 健康检查。

    返回契约要求的 dict；任何可预期失败都进 errors（不抛异常），
    ok 为 False 表示本次安装未完成但已写入的部分由 manifest 记录、可卸载。
    """
    opts = _normalize_opts(opts or {})
    platform = opts.get("platform") or detect_platform()
    if not isinstance(platform, dict) or not platform.get("os"):
        raise InstallerError("platform 必须是 detect_platform() 返回的字典")
    paths = resolve_paths(opts, platform)
    service = _select_service(opts, platform)
    adapter = _make_adapter(service, opts, platform, paths)
    dry = bool(opts.get("dry_run"))

    result = {
        "ok": False,
        "version": VERSION,
        "dry_run": dry,
        "platform": platform,
        "service": service,
        "paths": _public_paths(paths),
        "node": {},
        "device_code": "",
        "join_uri": "",
        "dependencies": {},
        "actions": [],
        "warnings": list(platform.get("warnings") or [])
                    + list(opts.get("_legacy_warnings") or []),
        "errors": [],
        "next_steps": [],
        "manifest": paths["manifest_path"],
    }

    # 1) 依赖预检（缺失只给安装命令）
    deps = check_dependencies(opts, platform)
    result["dependencies"] = deps
    result["warnings"].extend(deps.get("warnings") or [])
    result["errors"].extend(deps.get("errors") or [])
    result["actions"] = _build_actions(opts, platform, paths, adapter,
                                       deps.get("ok"))

    if dry:
        result["ok"] = bool(deps.get("ok"))
        result["unit_path"] = adapter.unit_path()
        result["next_steps"] = [
            "这是 dry-run：未写入任何文件、未启动服务",
            "正式安装：去掉 --dry-run 重跑本命令",
            "预览服务单元：加 --print-unit",
        ]
        return result

    if not deps.get("ok"):
        return result

    # 2) 目录（数据目录 0700）
    try:
        _ensure_dir(paths["install_dir"], 0o755)
        _ensure_dir(paths["data_dir"], 0o700)
        _ensure_dir(paths["log_dir"], 0o700)
    except OSError as exc:
        result["errors"].append("创建目录失败: %s" % exc)
        return result

    # 3) 拷贝节点程序
    copied, copy_errors, copy_warnings = _copy_node_files(
        opts, paths, deps.get("source_dir"))
    result["errors"].extend(copy_errors)
    result["warnings"].extend(copy_warnings)
    if copy_errors:
        return result

    # 4) 配置（0600，保留已有 token）
    try:
        cfg = _write_config(opts, paths, deps.get("source_dir"))
    except OSError as exc:
        result["errors"].append("写配置失败: %s" % exc)
        return result
    result["config"] = cfg

    # 5) 身份 / 邀请（fleet_core 缺失则降级）
    identity = _setup_identity(opts, platform, paths, cfg)
    result["node"] = identity.get("node") or {}
    result["join_uri"] = identity.get("join_uri") or ""
    result["device_code"] = (result["node"].get("node_id")
                             or result["node"].get("fingerprint") or "")
    result["warnings"].extend(identity.get("warnings") or [])

    # 6) --join 邀请解析（缺失 fleet_core 只提示）
    join = _apply_join(opts)
    if join.get("peer"):
        result["peer"] = join["peer"]
    if not join.get("ok", True):
        result["warnings"].append(join.get("error", "邀请解析失败"))
    elif join.get("skipped"):
        pass

    # 7) TLS（可选自签）
    tls = _setup_tls(opts, platform, paths)
    result["tls"] = tls
    result["warnings"].extend(tls.get("warnings") or [])

    # 8) 服务单元
    unit_written = []
    if not opts.get("no_service"):
        try:
            content = adapter.render()
        except InstallerError as exc:
            result["errors"].append(str(exc))
            content = ""
        if content:
            written, unit_errors = adapter.install_unit(content)
            unit_written.extend(written)
            result["errors"].extend(unit_errors)
            result["unit_path"] = adapter.unit_path()
            result["unit"] = content
    else:
        result["unit_path"] = ""
        result["warnings"].append("--no-service：只落地文件与配置，不注册服务、不启动")

    # 9) manifest（先写，保证失败也能安全卸载）
    manifest, manifest_errors = _write_manifest(
        opts, platform, paths, service, adapter, unit_written, copied,
        adapter.extra_files(), result)
    result["errors"].extend(manifest_errors)
    result["manifest"] = paths["manifest_path"]

    # 10) 启动
    if not opts.get("no_service"):
        start = adapter.start()
        result["start"] = start
        result["warnings"].extend(start.get("warnings") or [])
        if not start.get("ok"):
            result["errors"].append("服务启动失败：%s" % (start.get("detail") or "未知原因"))
        else:
            # 11) 健康检查（3 次退避）
            token = str(cfg.get("auth_token") or "")
            health = health_check(int(opts.get("port") or DEFAULT_PORT),
                                 token=token or None, attempts=3)
            result["health"] = health
            if not health.get("ok"):
                result["errors"].append(
                    "健康检查失败：%s 在 3 次退避后仍未返回 status=ok"
                    % health.get("url"))
    else:
        result["start"] = {"ok": True, "skipped": True,
                           "detail": "--no-service：未启动"}

    result["ok"] = not result["errors"]
    result["next_steps"] = _install_next_steps(opts, platform, paths, service,
                                               adapter, result)
    return result


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def status(opts=None):
    """查询节点安装/运行/健康状态（只读）。"""
    raw = dict(opts or {})
    opts = _normalize_opts(raw)
    platform = opts.get("platform") or detect_platform()
    paths = resolve_paths(opts, platform)
    manifest = _load_manifest(opts, paths)
    opts, paths = _manifest_paths(manifest, raw, platform)
    service = (manifest or {}).get("service") or _select_service(opts, platform)
    try:
        adapter = _make_adapter(service, opts, platform, paths)
    except InstallerError:
        adapter = _make_adapter(_select_service(opts, platform), opts, platform, paths)
        service = adapter.key
    installed = bool(manifest) or _exists(paths["config_path"])
    service_status = adapter.status()
    port = int((manifest or {}).get("port") or opts.get("port") or DEFAULT_PORT)
    health = None
    if installed:
        health = _health_once(port, token=_read_token(paths["config_path"]))
    pid_text = _read_text(paths["pid_file"], limit=32).strip()
    result = {
        "ok": True,
        "version": VERSION,
        "installed": installed,
        "platform": platform,
        "service": service,
        "service_status": service_status,
        "running": bool(service_status.get("running")),
        "healthy": bool(health.get("ok")) if health else None,
        "health": health,
        "port": port,
        "paths": _public_paths(paths),
        "manifest": paths["manifest_path"],
        "node": (manifest or {}).get("node") or {},
        "pid": pid_text or None,
    }
    return result


# ---------------------------------------------------------------------------
# uninstall：安全卸载（只删 manifest 记录、默认保留数据）
# ---------------------------------------------------------------------------
# 卸载绝不能碰的绝对路径：根、系统目录、家目录层级（Windows 盘根另判）。
_DANGEROUS_REMOVAL_PATHS = (
    "/", "/usr", "/usr/local", "/etc", "/var", "/bin", "/sbin", "/lib",
    "/lib32", "/lib64", "/boot", "/dev", "/proc", "/sys", "/run", "/root",
    "/home", "/opt", "/srv", "/mnt", "/media", "/tmp", "/private",
    "/System", "/Library", "/Applications", "/Users", "/Volumes", "/cores",
    "C:/Windows", "C:/Windows/System32", "C:/Program Files",
    "C:/Program Files (x86)", "C:/ProgramData", "C:/Users",
)


def _removal_norm(path):
    """卸载路径比较键：展开 ~/$VAR、绝对化、统一分隔符、Windows 小写、去尾分隔符。"""
    expanded = os.path.expanduser(os.path.expandvars(str(path or "").strip()))
    key = _path_key(expanded).replace("\\", "/")
    while len(key) > 1 and key.endswith("/"):
        key = key[:-1]
    if os.name == "nt":
        key = key.lower()
    return key


def is_safe_removal_path(path):
    """卸载删除白名单：拒绝空值、根目录、盘根、系统目录、家目录及其祖先。

    返回 True 表示「看起来是一个可以安全删除的具体路径」；最终是否真的删还要
    再过 manifest / 安装目录归属校验（见 _removal_allowed）。
    """
    if not str(path or "").strip():
        return False
    target = _removal_norm(path)
    if not target or target == ".":
        return False
    # "/"、"//"、Windows 盘根（C: / C:/）一律拒绝
    if target == "/":
        return False
    if re.match(r"^[a-z]:$", target):
        return False
    for dangerous in _DANGEROUS_REMOVAL_PATHS:
        if target == _removal_norm(dangerous):
            return False
    # 家目录本身，以及「删掉会连家目录一起带走」的祖先（如 /Users、/home/x）
    home = _removal_norm("~")
    if home and target != "/" and (target == home
                                   or home.startswith(target + "/")):
        return False
    return True


_OWNED_UNIT_PREFIXES = ("mcp-fleet-", "com.mcpfleet.node.", "MCPFleet-",
                        "fleet-", "run")


def _owned_unit_path(path, paths):
    """判断是否为安装器生成的服务单元路径（防恶意 manifest 借刀删文件）。"""
    base = os.path.basename(str(path).rstrip(os.sep))
    if base.startswith(_OWNED_UNIT_PREFIXES):
        return True
    return _is_within(path, paths["install_dir"])


def _removal_allowed(path, paths, unit_keys, purge):
    """卸载删除前的白名单校验；返回 (ok, reason)。"""
    if not str(path or "").strip():
        return False, "空路径"
    if not is_safe_removal_path(path):
        return False, "拒绝删除危险路径：%s" % path
    key = _path_key(path)
    if key in unit_keys:
        if not _owned_unit_path(path, paths):
            return False, "拒绝删除非本安装器的服务文件：%s" % path
        return True, ""
    if _is_within(path, paths["install_dir"]):
        return True, ""
    if purge and _is_within(path, paths["data_dir"]):
        return True, ""
    return False, "不在本安装器管理范围内，跳过：%s" % path


def uninstall(opts=None):
    """卸载：先停服务，再按 manifest 删除自己生成的单元/文件。

    默认保留数据目录（邮箱/身份/日志），--purge 才删除；拒绝危险路径。
    """
    raw = dict(opts or {})
    opts = _normalize_opts(raw)
    platform = opts.get("platform") or detect_platform()
    paths = resolve_paths(opts, platform)
    manifest = _load_manifest(opts, paths)
    opts, paths = _manifest_paths(manifest, raw, platform)
    service = (manifest or {}).get("service") or _select_service(opts, platform)
    adapter = _make_adapter(service, opts, platform, paths)

    result = {
        "ok": False,
        "version": VERSION,
        "installed": bool(manifest) or _exists(paths["config_path"]),
        "purged": False,
        "platform": platform,
        "service": service,
        "paths": _public_paths(paths),
        "removed": [],
        "kept": [],
        "errors": [],
        "warnings": [],
    }
    if not result["installed"]:
        result["errors"].append("未找到安装记录（%s 或 %s 均不存在）"
                                % (paths["manifest_path"], paths["config_path"]))
        return result

    # 1) 先停服务（避免删完单元进程还在）
    try:
        result["stop"] = adapter.stop()
    except Exception as exc:
        result["warnings"].append("停止服务时异常（继续卸载）：%s" % exc)

    unit_paths = []
    for path in list((manifest or {}).get("unit_paths") or []) + adapter.unit_paths():
        if path and path not in unit_paths:
            unit_paths.append(path)
    unit_keys = set(_path_key(p) for p in unit_paths)
    files = list((manifest or {}).get("files") or [])
    ordered = list(dict.fromkeys(unit_paths + files))
    if not manifest:
        ordered = list(dict.fromkeys(unit_paths + [paths["config_path"]]))

    for path in ordered:
        allowed, reason = _removal_allowed(path, paths, unit_keys,
                                           bool(opts.get("purge")))
        if not allowed:
            if reason:
                result["warnings"].append(reason)
            continue
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)
                result["removed"].append(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
                result["removed"].append(path)
        except OSError as exc:
            result["errors"].append("删除 %s 失败: %s" % (path, exc))

    # 2) 指针文件（在安装目录里，属于安装器生成）——必须先删，
    #    否则它会占着安装目录导致下一步「空目录清理」失败、残留空目录。
    pointer = paths["manifest_pointer"]
    if _exists(pointer) and _path_key(pointer) != _path_key(paths["config_path"]):
        try:
            os.remove(pointer)
            result["removed"].append(pointer)
        except OSError:
            pass

    # 3) 尝试删除空目录（install/log 默认清理；data 默认保留）
    for directory in list((manifest or {}).get("dirs") or []):
        if not directory:
            continue
        if _path_key(directory) == _path_key(paths["data_dir"]) \
                and not opts.get("purge"):
            continue
        if not is_safe_removal_path(directory):
            result["warnings"].append("拒绝删除危险目录：%s" % directory)
            continue
        if not (_is_within(directory, paths["install_dir"])
                or (opts.get("purge") and _is_within(directory, paths["data_dir"]))):
            continue
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
                result["removed"].append(directory)
        except OSError:
            pass

    # 4) --purge 才删除数据目录
    data_dir = paths["data_dir"]
    if opts.get("purge"):
        expected = (manifest or {}).get("data_dir")
        if expected and _path_key(expected) != _path_key(data_dir):
            result["errors"].append("manifest 记录的数据目录（%s）与当前解析（%s）"
                                    "不一致，拒绝 --purge" % (expected, data_dir))
        elif not is_safe_removal_path(data_dir):
            result["errors"].append("拒绝删除危险路径的数据目录：%s" % data_dir)
        elif _exists(data_dir):
            try:
                shutil.rmtree(data_dir)
                result["removed"].append(data_dir)
                result["purged"] = True
            except OSError as exc:
                result["errors"].append("删除数据目录失败: %s" % exc)
    else:
        result["kept"].append(data_dir)
        result["warnings"].append("数据目录已保留（邮箱/身份/日志）：%s；"
                                  "彻底清理请加 --purge" % data_dir)

    result["ok"] = not result["errors"]
    return result


# ---------------------------------------------------------------------------
# --print-unit
# ---------------------------------------------------------------------------
def _print_unit(opts=None):
    opts = _normalize_opts(opts or {})
    platform = opts.get("platform") or detect_platform()
    paths = resolve_paths(opts, platform)
    service = _select_service(opts, platform)
    adapter = _make_adapter(service, opts, platform, paths)
    content = adapter.render()
    return {
        "ok": True,
        "version": VERSION,
        "dry_run": bool(opts.get("dry_run")),
        "service": service,
        "unit_path": adapter.unit_path(),
        "unit": content,
        "platform": platform,
        "paths": _public_paths(paths),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser():
    parser = argparse.ArgumentParser(
        prog="fleet_install.py",
        description="MCP Fleet v%s 统一跨平台安装器（纯标准库）" % VERSION,
        epilog="示例: python3 fleet_install.py --name nas --port 3100 --json")
    parser.add_argument("--name", help="节点名（默认按主机名生成）")
    parser.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 3100）")
    parser.add_argument("--dir", help="安装目录（默认 /opt/mcp-fleet/<name> 或 ~/.mcp-fleet/<name>）")
    parser.add_argument("--install-dir", dest="install_dir", help="--dir 的旧别名")
    parser.add_argument("--data-dir", dest="data_dir", help="数据目录（0700）")
    parser.add_argument("--source-dir", dest="source_dir",
                        help="源码目录（含 mcp_agent.py；默认自动定位）")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="只打印计划，不落盘不启服务")
    parser.add_argument("--print-unit", action="store_true", dest="print_unit",
                        help="打印将要写入的服务单元")
    parser.add_argument("--join", help="加入邀请：fleet://join/<...>")
    parser.add_argument("--relay", action="store_true", help="启用中继模式（写配置）")
    parser.add_argument("--tls", default="auto", choices=["auto", "off"],
                        help="TLS：auto=尝试自签证书，off=仅 HTTP")
    parser.add_argument("--read-only", action="store_true", dest="read_only",
                        help="只读节点（禁用变更类工具）")
    parser.add_argument("--user", help="服务运行用户（默认 root/当前用户）")
    parser.add_argument("--host", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--no-service", action="store_true", dest="no_service",
                        help="只落地文件与配置，不注册服务、不启动")
    parser.add_argument("--uninstall", action="store_true", help="卸载节点")
    parser.add_argument("--purge", action="store_true",
                        help="卸载时一并删除数据目录（危险，需明确指定）")
    parser.add_argument("--status", action="store_true", help="查询节点状态")
    parser.add_argument("--service", help="服务管理器（auto/systemd/openrc/procd/"
                                         "launchd/schtasks/termux/docker/nohup）")
    parser.add_argument("--mode", help="--service 的旧别名（auto|systemd|nohup）")
    parser.add_argument("--unit-dir", dest="unit_dir",
                        help="自定义服务单元目录（测试/自定义 init 用）")
    parser.add_argument("--version", action="store_true", dest="version_flag",
                        help="打印版本号")
    # ---- v5 旧参数兼容（不报错，尽量映射到 v5.1 行为）----
    parser.add_argument("--generate-token", action="store_true", dest="generate_token")
    parser.add_argument("--require-auth", action="store_true", dest="require_auth")
    parser.add_argument("--auth-token", dest="auth_token")
    parser.add_argument("--allow-ips", dest="allow_ips")
    parser.add_argument("--prefix")
    parser.add_argument("--sudo-pass", dest="sudo_password")
    parser.add_argument("--auto-start", action="store_true", dest="auto_start")
    parser.add_argument("--invite")
    parser.add_argument("--tunnel", action="store_true")
    parser.add_argument("--tunnel-backend", dest="tunnel_backend")
    parser.add_argument("--frp-server", dest="frp_server")
    parser.add_argument("--frp-port", dest="frp_port")
    parser.add_argument("--frp-token", dest="frp_token")
    parser.add_argument("--frp-remote-port", dest="frp_remote_port")
    parser.add_argument("--ssh-dest", dest="ssh_dest")
    parser.add_argument("--ssh-port", dest="ssh_port")
    parser.add_argument("--ssh-remote-port", dest="ssh_remote_port")
    parser.add_argument("--print-frps", action="store_true", dest="print_frps")
    parser.add_argument("--directory", dest="directory")
    parser.add_argument("--directory-token", dest="directory_token")
    parser.add_argument("--no-keepalive", action="store_true", dest="no_keepalive")
    parser.add_argument("--tailnet", dest="tailnet")
    parser.add_argument("--tailnet-key", dest="tailnet_key")
    parser.add_argument("--discover-port", dest="discover_port")
    parser.add_argument("--control", dest="control")
    parser.add_argument("--ttl", dest="ttl")
    parser.add_argument("--cli-only", action="store_true", dest="cli_only")
    return parser


_LEGACY_TUNNEL_KEYS = (
    "tunnel", "tunnel_backend", "frp_server", "frp_port", "frp_token",
    "frp_remote_port", "ssh_dest", "ssh_port", "ssh_remote_port", "print_frps",
    "directory", "directory_token", "tailnet", "tailnet_key", "control", "ttl",
)


def _namespace_to_opts(args):
    """argparse 命名空间 -> install(opts) 字典，并做旧参数映射。"""
    opts = {}
    for key, value in vars(args).items():
        if key == "version_flag":
            continue
        if value is None:
            continue
        if isinstance(value, bool) and value is False:
            continue
        opts[key] = value
    legacy = []
    if opts.get("invite") and not opts.get("join"):
        invite = str(opts.pop("invite"))
        if invite.startswith("fleet://"):
            opts["join"] = invite
        else:
            legacy.append("--invite <code> 旧邀请码格式：请改用 --join fleet://join/...")
    if opts.get("auto_start"):
        opts.pop("auto_start", None)
        legacy.append("--auto-start 已默认开启（v5.1 统一注册开机自启），参数保留兼容")
    if opts.get("discover_port"):
        opts["discover_port"] = int(opts["discover_port"])
    used_tunnel = [key for key in _LEGACY_TUNNEL_KEYS if opts.pop(key, None)]
    if used_tunnel:
        legacy.append("v5.1 安装器不再内建隧道/frp/tailnet（%s）："
                      "请用 fleet 中继工具或系统级隧道" % ", ".join(used_tunnel))
    if legacy:
        opts["_legacy_warnings"] = legacy
    return opts


def _print_human(result, mode):
    """非 JSON 模式的中文人读摘要（写 stdout）。"""
    errors = result.get("errors") or []
    if mode == "print_unit" and result.get("unit"):
        sys.stdout.write(result["unit"])
        if not result["unit"].endswith("\n"):
            sys.stdout.write("\n")
        return
    lines = []
    if mode == "uninstall":
        lines.append("MCP Fleet 卸载结果：%s" % ("成功" if result.get("ok") else "有错误"))
        for path in result.get("removed") or []:
            lines.append("  已删除: %s" % path)
        for path in result.get("kept") or []:
            lines.append("  已保留: %s" % path)
        if result.get("purged"):
            lines.append("  数据目录已彻底删除（--purge）")
    elif mode == "status":
        platform = result.get("platform") or {}
        lines.append("MCP Fleet 节点状态")
        lines.append("  平台: %s/%s（服务管理器 %s）"
                     % (platform.get("os"), platform.get("arch"),
                        result.get("service")))
        lines.append("  已安装: %s" % ("是" if result.get("installed") else "否"))
        status_info = result.get("service_status") or {}
        lines.append("  服务: %s（%s）" % (status_info.get("detail", "unknown"),
                                         "运行中" if result.get("running") else "未运行"))
        if result.get("healthy") is not None:
            lines.append("  健康: %s" % ("正常" if result.get("healthy") else "异常"))
        if result.get("pid"):
            lines.append("  PID: %s" % result["pid"])
        lines.append("  安装目录: %s" % (result.get("paths") or {}).get("install_dir", ""))
    else:
        platform = result.get("platform") or {}
        paths = result.get("paths") or {}
        lines.append("MCP Fleet v%s 安装结果：%s"
                     % (result.get("version", VERSION),
                        "成功" if result.get("ok") else "未完成"))
        lines.append("  平台: %s/%s  发行版: %s  init: %s"
                     % (platform.get("os"), platform.get("arch"),
                        platform.get("distro"), platform.get("init")))
        lines.append("  服务: %s（%s）" % (result.get("service"),
                                         platform.get("service_manager")))
        lines.append("  安装目录: %s" % paths.get("install_dir", ""))
        lines.append("  数据目录: %s" % paths.get("data_dir", ""))
        node = result.get("node") or {}
        if node.get("node_id"):
            lines.append("  设备码/节点 ID: %s" % node.get("node_id"))
            lines.append("  指纹: %s" % node.get("fingerprint", ""))
        if result.get("join_uri"):
            lines.append("  邀请 URI: %s" % result["join_uri"])
        health = result.get("health")
        if health:
            lines.append("  健康检查: %s（尝试 %s 次）"
                         % ("OK" if health.get("ok") else "失败",
                            health.get("attempts")))
        if result.get("next_steps"):
            lines.append("  下一步：")
            for index, step in enumerate(result["next_steps"], 1):
                lines.append("    %d. %s" % (index, step))
    if result.get("warnings"):
        lines.append("  提示：")
        for warning in result["warnings"]:
            lines.append("    - %s" % warning)
    if errors:
        lines.append("  错误：")
        for error in errors:
            lines.append("    - %s" % error)
    sys.stdout.write("\n".join(lines) + "\n")


def main(argv=None):
    """CLI 入口；返回进程退出码（0 成功 / 1 失败 / 2 参数错误）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.version_flag:
        sys.stdout.write("fleet_install.py %s\n" % VERSION)
        return 0
    opts = _namespace_to_opts(args)
    mode = "install"
    if opts.get("uninstall"):
        mode = "uninstall"
    elif opts.get("status"):
        mode = "status"
    elif opts.get("print_unit"):
        mode = "print_unit"
    try:
        if mode == "uninstall":
            result = uninstall(opts)
        elif mode == "status":
            result = status(opts)
        elif mode == "print_unit":
            result = _print_unit(opts)
        else:
            result = install(opts)
    except InstallerError as exc:
        result = {"ok": False, "version": VERSION, "errors": [str(exc)],
                  "platform": {}, "paths": {}, "service": ""}
    except Exception as exc:  # 最后一道防线：绝不让安装器崩在半路
        result = {"ok": False, "version": VERSION,
                  "errors": ["安装器内部错误: %r（可加 --dry-run 复现步骤）" % (exc,)],
                  "platform": {}, "paths": {}, "service": ""}
    if opts.get("json"):
        sys.stdout.write(_json_line(result) + "\n")
    else:
        _print_human(result, mode)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
