#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-fleet 跨平台安装器（v5.0.4，Windows / macOS 主场景，纯标准库）

为什么有它：install.sh 是 bash 脚本，Windows / macOS 电脑（办公室主力机）跑不了。
本安装器用 Python 完成同样的核心路径：

    拷贝节点程序 -> 生成配置与 auth_token -> 生成启动脚本 -> (可选)开机自启+进程保护

用法（Windows / Linux / macOS 通用）：
    python install.py --name office-pc --port 3100
    python install.py --name office-pc --auto-start      # 开机自启 + 进程保护
    python install.py --uninstall

平台守护矩阵（v5.0.4）：
    Linux   : install.sh 的 systemd（Restart=always + watchdog timer）——仍推荐
    Windows : schtasks 双任务（ONLOGON 登录自启[pythonw 无窗口]
              + MINUTE 每分钟探活 /health 异常自动拉起），零提权零依赖
    macOS   : launchd（RunAtLoad 登录自启 + KeepAlive 崩溃自动重启 + 节流）

不做的事（与 install.sh 的边界）：
    - frp 隧道 / Tailscale：跨网段场景请用 Linux 主机的 install.sh
    - systemd：Linux 上仍推荐 install.sh；本脚本在 Linux 只提供前台启动

Python 3.6+，零第三方依赖。
"""

import argparse
import base64
import json
import locale
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from xml.sax.saxutils import escape as _xml_escape

IS_WIN = (os.name == "nt")
IS_MAC = (sys.platform == "darwin")
NODE_FILES = ["mcp_agent.py", "fleet_proto.py", "dashboard_html.py", "install.py"]

# v5.0.4: Windows 计划任务名（自启 + 看门狗），与 Linux 的 systemd 单元对称
TASK_AGENT = "mcp-fleet-agent"
TASK_WATCHDOG = "mcp-fleet-watchdog"
# v5.0.4: macOS launchd 标签前缀与记录文件（卸载时定位 plist，同 install.sh 的 unit.txt）
LAUNCHD_LABEL_PREFIX = "com.mcp-fleet"
LAUNCHD_MARKER = "launchd.txt"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def log(msg):
    print("  " + msg)


def die(msg):
    print("  [错误] " + msg)
    sys.exit(1)


def default_install_dir():
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "mcp-fleet")
    return os.path.expanduser("~/.mcp-fleet")


def gen_token():
    """32 字节 urlsafe 随机令牌（与 mcp_agent.py 同格式）。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _write_bat(path, content):
    """v5.0.1 修复：按 cmd 的 ANSI 代码页（中文系统 = GBK）写 bat。

    原实现固定 utf-8：安装路径含中文（如中文用户名的 LOCALAPPDATA）
    时，cmd 按 GBK 解码 utf-8 字节变乱码 -> cd /d 失败，节点起不来。
    ASCII 路径下 GBK 与 utf-8 字节完全一致，行为不变。
    极端情况（路径含 ANSI 页无法表示的字符）退回 utf-8，至少文件可写。
    """
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        with open(path, "w", encoding=enc, newline="") as fh:
            fh.write(content)
    except (UnicodeEncodeError, LookupError):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)


def gen_device_code(name):
    """设备码 = hostname 前缀 + 4 位随机（v4.3 风格，如 office-a3f2）。"""
    host = (platform.node() or "node").lower()
    prefix = "".join(c for c in host.split("-")[0] if c.isalnum())[:8] or "node"
    rnd = base64.b64encode(os.urandom(6)).decode().replace("+", "").replace("/", "").lower()
    return "%s-%s" % (prefix, rnd[:4])


def local_ip():
    """取一个能出网的本地 IP（不真的发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# v5.0.4: 平台守护 —— 命令构造与文本解析均为纯函数（tests 可直接覆盖），
# 副作用（schtasks/launchctl/taskkill/Popen）单独收敛，Linux 上也能测分支逻辑。
# ---------------------------------------------------------------------------
def sanitize_label(name):
    """节点名 -> 合法 launchd Label 片段（字母数字连字符）。"""
    out = []
    for c in (name or "node"):
        out.append(c if (c.isalnum() or c == "-") else "-")
    s = "".join(out).strip("-") or "node"
    return s[:40]


def mac_plist_path(label):
    base = os.path.expanduser("~/Library/LaunchAgents")
    return os.path.join(base, "%s.%s.plist" % (LAUNCHD_LABEL_PREFIX, label))


def build_launchd_plist(label, python, target, port, throttle=10):
    """生成 launchd plist XML：RunAtLoad 自启 + KeepAlive 崩溃重启 + 节流。

    一个系统机制同时覆盖「开机启动」与「进程保护」，macOS 上的唯一正解；
    ThrottleInterval 防止崩溃风暴（默认 10s 重拉一次）。
    """
    argv = [python, os.path.join(target, "mcp_agent.py"),
            "--host", "0.0.0.0", "--port", str(port)]
    prog = "".join("        <string>%s</string>\n" % _xml_escape(a) for a in argv)
    logfile = _xml_escape(os.path.join(target, "agent.log"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>Label</key>\n'
        '    <string>%s.%s</string>\n'
        '    <key>ProgramArguments</key>\n'
        '    <array>\n'
        '%s'
        '    </array>\n'
        '    <key>RunAtLoad</key>\n'
        '    <true/>\n'
        '    <key>KeepAlive</key>\n'
        '    <true/>\n'
        '    <key>ThrottleInterval</key>\n'
        '    <integer>%d</integer>\n'
        '    <key>WorkingDirectory</key>\n'
        '    <string>%s</string>\n'
        '    <key>StandardOutPath</key>\n'
        '    <string>%s</string>\n'
        '    <key>StandardErrorPath</key>\n'
        '    <string>%s</string>\n'
        '</dict>\n'
        '</plist>\n'
    ) % (_xml_escape(LAUNCHD_LABEL_PREFIX), _xml_escape(label), prog,
         throttle, _xml_escape(target), logfile, logfile)


def build_schtasks_onlogon(python, target, port, task=TASK_AGENT):
    """登录自启任务（pythonw 无窗口）。返回 subprocess 参数列表。"""
    tr = '"%s" "%s" --host 0.0.0.0 --port %d' % (
        python, os.path.join(target, "mcp_agent.py"), port)
    return ["schtasks", "/Create", "/F", "/TN", task, "/SC", "ONLOGON", "/TR", tr]


def build_schtasks_minute(python, target, port, task=TASK_WATCHDOG):
    """每分钟看门狗任务：跑本安装器的 --watchdog 单次探活。"""
    tr = '"%s" "%s" --watchdog --port %d --install-dir "%s"' % (
        python, os.path.join(target, "install.py"), port, target)
    return ["schtasks", "/Create", "/F", "/TN", task,
            "/SC", "MINUTE", "/MO", "1", "/TR", tr]


def win_find_pythonw():
    """python.exe 同目录的 pythonw.exe（无控制台窗口）；找不到回退当前解释器。"""
    py = sys.executable or "python"
    cand = os.path.join(os.path.dirname(py), "pythonw.exe")
    return cand if os.path.isfile(cand) else py


def parse_netstat_pids(text, port):
    """从 netstat -ano 输出解析 LISTENING 指定端口的 PID（纯函数，含 IPv6 行）。"""
    pids = set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" \
                and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pids.add(int(parts[4]))
                except ValueError:
                    pass
    return sorted(pids)


def parse_tasklist_image(text):
    """从 tasklist 输出取第一行数据进程的映像名（纯函数）。"""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().endswith(".exe"):
            try:
                int(parts[1])  # 第二列是 PID，确认是数据行
                return parts[0]
            except ValueError:
                continue
    return None


def is_python_image(name):
    """python.exe / pythonw.exe / python3.11.exe 都算（防误杀其他服务）。"""
    n = (name or "").lower()
    return n.startswith("python") and n.endswith(".exe")


def probe_agent(port, timeout=5.0, token=None):
    """探活：GET /health 且校验 JSON 结构（status=ok 且带 version）。

    只看端口通不通不够 —— 别的服务恰好占了端口也会 200，
    必须确认返回体是 mcp-fleet 的健康结构才动手重启。
    开了 auth_token 的节点匿名 /health 是 401（安全收敛：不向未认证者
    暴露指纹），所以 watchdog 必须带本机 token 探活；裸奔模式自动匿名。
    """
    url = "http://127.0.0.1:%d/health" % port
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read().decode("utf-8", "replace"))
            return data.get("status") == "ok" and "version" in data
    except Exception:
        return False


def win_kill_port_pids(port):
    """杀掉 LISTENING 该端口的 python 进程（卡死但没退净的 agent）。

    双重校验：netstat 定位 PID -> tasklist 确认是 python 系映像才杀，
    避免误杀恰好占用同端口的其他服务。返回被杀 PID 列表。
    """
    killed = []
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return killed
    for pid in parse_netstat_pids(out, port):
        try:
            tl = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                capture_output=True, text=True, timeout=15).stdout
            if not is_python_image(parse_tasklist_image(tl)):
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
            killed.append(pid)
        except Exception:
            continue
    return killed


def spawn_agent_detached(py, target, port):
    """后台无窗口拉起 agent（watchdog 复位路径）。"""
    argv = [py, os.path.join(target, "mcp_agent.py"),
            "--host", "0.0.0.0", "--port", str(port)]
    kw = {"cwd": target, "stdin": subprocess.DEVNULL,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = 0x00000008  # DETACHED_PROCESS：脱离控制台
    else:
        kw["start_new_session"] = True   # POSIX：新会话，免 tty 挂断牵连
    subprocess.Popen(argv, **kw)


def watchdog_once(port, target, timeout=5.0, probe=None, spawn=None, kill=None):
    """单次看门狗（schtasks 每分钟调一次）：

    探活通过 -> 什么都不做；不通过 -> 清端口残留 -> 拉起 agent。
    与 Linux 的 mcp-watchdog.sh 同语义（那边是 PID 文件 + HTTP 双检）。
    依赖注入 probe/spawn/kill 便于测试。返回 None（健康）或动作记录。
    """
    probe = probe or probe_agent
    spawn = spawn or spawn_agent_detached
    kill = kill or (win_kill_port_pids if IS_WIN else (lambda p: []))
    # 带本机 auth_token 探活（开鉴权的节点匿名 /health 是 401，会误判成宕机）
    token = None
    try:
        cfg_path = os.path.join(target, "mcp_agent_config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as fh:
                token = (json.load(fh) or {}).get("auth_token") or None
    except Exception:
        token = None
    if probe(port, timeout=timeout, token=token):
        return None
    killed = kill(port)
    if killed:
        time.sleep(1.0)  # 等端口彻底释放
    spawn(sys.executable, target, port)
    return {"restarted": True, "killed": killed}


def _run_cmd(argv):
    """静默执行外部命令，返回 (rc, 输出拼串)。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def find_source_dir():
    """定位 mcp_agent.py 等源文件：优先同目录，其次安装器所在目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.getcwd(), here):
        if all(os.path.exists(os.path.join(d, f)) for f in NODE_FILES):
            return d
    die("找不到 %s（应与 install.py 放在同一目录）" % ", ".join(NODE_FILES))


def do_install(args):
    src = find_source_dir()
    target = args.install_dir or default_install_dir()
    os.makedirs(target, exist_ok=True)

    print("\n╔════════════════════════════════════════════════════╗")
    print("║  mcp-fleet 跨平台安装器 v5.0.4                    ║")
    print("╚════════════════════════════════════════════════════╝\n")

    # 1. 拷贝节点程序
    log("安装目录   : %s" % target)
    for f in NODE_FILES:
        shutil.copy2(os.path.join(src, f), os.path.join(target, f))
    log("节点程序   : %s（共 %d 个文件）" % (", ".join(NODE_FILES), len(NODE_FILES)))

    # 2. 配置（保留已有 auth_token / 旧配置字段，增量更新）
    cfg_path = os.path.join(target, "mcp_agent_config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}
    cfg["name"] = args.name
    cfg["port"] = args.port
    if not cfg.get("auth_token"):
        cfg["auth_token"] = gen_token()
        log("auth_token : 新生成（节点访问凭据 + 邮箱加密密钥派生源）")
    else:
        log("auth_token : 沿用已有（加密数据可继续解密）")
    cfg.setdefault("audit_log", True)
    cfg.setdefault("discover_port", 3199)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    # 3. 启动脚本
    py = sys.executable or ("python" if IS_WIN else "python3")
    if IS_WIN:
        bat = os.path.join(target, "start-fleet.bat")
        # v5.0.1: _write_bat 按 ANSI 代码页写（原 utf-8 导致中文路径乱码）
        _write_bat(bat, '@echo off\r\n'
                   'cd /d "%s"\r\n'
                   '"%s" mcp_agent.py --host 0.0.0.0 --port %d\r\n'
                   'pause\r\n' % (target, py, args.port))
        log("启动脚本   : %s" % bat)
    else:
        sh = os.path.join(target, "start-fleet.sh")
        with open(sh, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\ncd "%s"\nexec "%s" mcp_agent.py --host 0.0.0.0 --port %d\n'
                     % (target, py, args.port))
        os.chmod(sh, os.stat(sh).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        log("启动脚本   : %s" % sh)

    # 4. 防火墙（仅 Windows，尽力而为）
    if IS_WIN:
        try:
            rule = ("netsh advfirewall firewall add rule name=\"mcp-fleet %d\" "
                    "dir=in action=allow protocol=TCP localport=%d" % (args.port, args.port))
            rc = os.system(rule)
            if rc == 0:
                log("防火墙     : 已放行 TCP/%d" % args.port)
            else:
                log("防火墙     : 自动放行失败（需管理员），请手动放行 TCP/%d" % args.port)
        except Exception:
            log("防火墙     : 跳过（请手动放行 TCP/%d）" % args.port)
    else:
        if IS_MAC:
            log("防火墙     : macOS 应用防火墙默认不拦入站监听；若手动开启过，"
                "请在「安全性与隐私」放行 Python")
        else:
            log("防火墙     : Linux 请自行放行 TCP/%d 与 UDP/3199" % args.port)

    # 5. 开机自启 + 进程保护（可选，v5.0.4 三平台对齐）
    if args.auto_start:
        if IS_WIN:
            # v5.0.4: 先清 v5.0.3 及以前注册的 Startup bat，避免双启动抢端口
            _legacy = os.path.join(os.environ.get("APPDATA", ""),
                                   r"Microsoft\Windows\Start Menu\Programs\Startup\mcp-fleet.bat")
            if os.path.exists(_legacy):
                os.remove(_legacy)
                log("旧自启     : 已移除 Startup 启动项（升级清理）")
            pyw = win_find_pythonw()
            rc1, out1 = _run_cmd(build_schtasks_onlogon(pyw, target, args.port))
            rc2, out2 = _run_cmd(build_schtasks_minute(sys.executable, target, args.port))
            if rc1 == 0:
                log("开机自启   : 已注册（计划任务 %s / 登录自启，pythonw 无窗口）" % TASK_AGENT)
            else:
                log("开机自启   : schtasks 注册失败：%s" % (out1 or "").strip()[:120])
            if rc2 == 0:
                log("进程保护   : 已注册（%s 每分钟探活 /health，异常自动拉起）" % TASK_WATCHDOG)
            else:
                log("进程保护   : schtasks 注册失败：%s" % (out2 or "").strip()[:120])
        elif IS_MAC:
            label = sanitize_label(args.name)
            plist = mac_plist_path(label)
            try:
                os.makedirs(os.path.dirname(plist), exist_ok=True)
                with open(plist, "w", encoding="utf-8") as fh:
                    fh.write(build_launchd_plist(label, py, target, args.port))
                _run_cmd(["launchctl", "unload", "-w", plist])  # 先卸旧实例（忽略失败）
                rc, out = _run_cmd(["launchctl", "load", "-w", plist])
                if rc == 0:
                    with open(os.path.join(target, LAUNCHD_MARKER), "w",
                              encoding="utf-8") as fh:
                        fh.write(plist)  # 卸载时用它定位（同 install.sh 的 unit.txt）
                    log("开机自启   : 已注册（launchd RunAtLoad，%s）" % plist)
                    log("进程保护   : 已启用（launchd KeepAlive，崩溃 10s 自动重启）")
                else:
                    log("开机自启   : launchctl load 失败：%s" % (out or "").strip()[:120])
            except Exception as e:
                log("开机自启   : 写 plist 失败：%s" % e)
        else:
            log("开机自启   : Linux 推荐用 install.sh 的 systemd 单元"
                "（Restart=always + watchdog timer），此处跳过")
    else:
        if IS_WIN or IS_MAC:
            log("提示       : 加 --auto-start 可一并注册开机自启与进程保护")

    # 6. 摘要
    code = gen_device_code(args.name)
    ip = local_ip()
    print("\n╔════════════════════════════════════════════════════╗")
    print("║  安装完成                                           ║")
    print("╠════════════════════════════════════════════════════╣")
    print("║  设备码   : %s" % code.ljust(38) + "║")
    print("║  API      : http://%s:%d/mcp" % (ip, args.port))
    print("║  Dashboard: http://%s:%d/fleet/ui" % (ip, args.port))
    print("║  auth     : %s..." % cfg["auth_token"][:16])
    print("║                                                     ║")
    print("║  启动: %s" % (("start-fleet.bat" if IS_WIN else "./start-fleet.sh").ljust(40)) + "║")
    print("║  同网段节点 UDP 广播 30s 内自动互相发现             ║")
    print("╚════════════════════════════════════════════════════╝\n")
    log("设备码供其他节点配对（peer_add）使用；auth_token 用于远程调用与邮箱加密")
    log("提示：--auto-start 可注册开机自启；--uninstall 一键卸载")


def do_uninstall(args):
    target = args.install_dir or default_install_dir()
    if not os.path.isdir(target):
        die("安装目录不存在: %s" % target)
    print("\n  卸载 %s ..." % target)
    if IS_WIN:
        # v5.0.4: 计划任务（自启 + 看门狗）—— 必须先删看门狗，
        # 否则它下一分钟又把 agent 拉起来（同 install.sh 先停 watchdog.timer 的坑）
        for task in (TASK_WATCHDOG, TASK_AGENT):
            rc, _ = _run_cmd(["schtasks", "/Delete", "/F", "/TN", task])
            if rc == 0:
                log("已删除计划任务 %s" % task)
        # legacy: v5.0.3 及以前的 Startup 启动项
        link = os.path.join(os.environ.get("APPDATA", ""),
                            r"Microsoft\Windows\Start Menu\Programs\Startup\mcp-fleet.bat")
        if os.path.exists(link):
            os.remove(link)
            log("已移除 Startup 启动项（legacy）")
    elif IS_MAC:
        marker = os.path.join(target, LAUNCHD_MARKER)
        if os.path.exists(marker):
            try:
                with open(marker, encoding="utf-8") as fh:
                    plist = fh.read().strip()
                if plist and os.path.exists(plist):
                    _run_cmd(["launchctl", "unload", "-w", plist])
                    os.remove(plist)
                    log("已卸载并移除 launchd 配置：%s" % plist)
            except Exception as e:
                log("launchd 清理失败（可手工处理）：%s" % e)
    keep = ("mailbox", "mcp_agent_config.json")  # 保留数据与凭据
    for name in os.listdir(target):
        if name in keep:
            log("保留 %s（数据/凭据，手工删除可彻底清理）" % name)
            continue
        p = os.path.join(target, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
    log("卸载完成\n")


def main():
    ap = argparse.ArgumentParser(
        description="mcp-fleet 跨平台安装器（Windows / macOS 主场景，零依赖）")
    ap.add_argument("--name", default=None,
                    help="节点名（默认 自动生成 设备码风格名称）")
    ap.add_argument("--port", type=int, default=3100, help="HTTP 端口（默认 3100）")
    ap.add_argument("--install-dir", default=None, help="安装目录（默认 %s）" % default_install_dir())
    ap.add_argument("--auto-start", action="store_true",
                    help="注册开机自启 + 进程保护（Windows schtasks 双任务 / macOS launchd）")
    ap.add_argument("--watchdog", action="store_true",
                    help="单次看门狗：探活 /health，异常自动拉起（供计划任务调用）")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    args = ap.parse_args()

    if args.uninstall:
        do_uninstall(args)
        return
    if args.watchdog:
        # 供 schtasks 每分钟调用：健康时零动作退出，异常时清残留并拉起
        target = args.install_dir or default_install_dir()
        try:
            act = watchdog_once(args.port, target)
        except Exception as e:
            print("[watchdog] error: %s" % e)
            sys.exit(1)
        sys.exit(0 if act is None else 0)
    if not args.name:
        args.name = gen_device_code("node")
    do_install(args)


if __name__ == "__main__":
    main()
