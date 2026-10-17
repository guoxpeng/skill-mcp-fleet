#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-fleet 跨平台安装器（v5.0，Windows 主场景，纯标准库）

为什么有它：install.sh 是 bash 脚本，Windows 电脑（办公室主力机）跑不了。
本安装器用 Python 完成同样的核心路径：

    拷贝节点程序 -> 生成配置与 auth_token -> 生成启动脚本 -> (可选)开机自启

用法（Windows / Linux / macOS 通用）：
    python install.py --name office-pc --port 3100
    python install.py --name office-pc --auto-start      # 注册开机自启
    python install.py --uninstall

不做的事（与 install.sh 的边界）：
    - frp 隧道 / Tailscale：跨网段场景请用 Linux 主机的 install.sh
    - systemd：Linux 上仍推荐 install.sh；本脚本在 Linux 只提供前台启动

Python 3.6+，零第三方依赖。
"""

import argparse
import base64
import json
import os
import platform
import shutil
import socket
import stat
import sys
import time

IS_WIN = (os.name == "nt")
NODE_FILES = ["mcp_agent.py", "fleet_proto.py", "dashboard_html.py"]


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
    print("║  mcp-fleet 跨平台安装器 v5.0                       ║")
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
        with open(bat, "w", encoding="utf-8") as fh:  # noqa: CRLF 无所谓，cmd 都认
            fh.write('@echo off\r\n'
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
        log("防火墙     : Linux 请自行放行 TCP/%d 与 UDP/3199" % args.port)

    # 5. 开机自启（可选）
    if args.auto_start:
        if IS_WIN:
            startup = os.path.join(os.environ.get("APPDATA", ""),
                                   r"Microsoft\Windows\Start Menu\Programs\Startup")
            if os.path.isdir(startup):
                link = os.path.join(startup, "mcp-fleet.bat")
                with open(link, "w", encoding="utf-8") as fh:
                    fh.write('@echo off\r\n'
                             'cd /d "%s"\r\n'
                             'start "" /min "%s" mcp_agent.py --host 0.0.0.0 --port %d\r\n'
                             % (target, py, args.port))
                log("开机自启   : 已注册（Startup 文件夹）")
            else:
                log("开机自启   : 找不到 Startup 文件夹，跳过")
        else:
            log("开机自启   : Linux 推荐用 install.sh 的 systemd 单元，此处跳过")

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
    # Startup 自启清理
    if IS_WIN:
        link = os.path.join(os.environ.get("APPDATA", ""),
                            r"Microsoft\Windows\Start Menu\Programs\Startup\mcp-fleet.bat")
        if os.path.exists(link):
            os.remove(link)
            log("已移除开机自启")
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
        description="mcp-fleet 跨平台安装器（Windows 主场景，零依赖）")
    ap.add_argument("--name", default=None,
                    help="节点名（默认 自动生成 设备码风格名称）")
    ap.add_argument("--port", type=int, default=3100, help="HTTP 端口（默认 3100）")
    ap.add_argument("--install-dir", default=None, help="安装目录（默认 %s）" % default_install_dir())
    ap.add_argument("--auto-start", action="store_true", help="注册开机自启（Windows Startup）")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    args = ap.parse_args()

    if args.uninstall:
        do_uninstall(args)
        return
    if not args.name:
        args.name = gen_device_code("node")
    do_install(args)


if __name__ == "__main__":
    main()
