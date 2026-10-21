#!/bin/sh
# MCP Fleet 离线包依赖检查（POSIX sh；只检查，不自动安装）
# 用法：sh check-deps.sh
# 退出码：0 = 依赖齐全；1 = 缺少必需依赖（会打印对应安装命令）
set -u
MISSING=0

have() { command -v "$1" >/dev/null 2>&1; }

say() { printf '%s\n' "$*"; }

os_hint() {
    if [ -n "${PREFIX:-}" ] && printf '%s' "$PREFIX" | grep -q com.termux; then
        say "  pkg update && pkg install python"
    elif [ -f /etc/openwrt_release ]; then
        say "  opkg update && opkg install python3-light python3-urllib"
    elif [ -f /etc/alpine-release ]; then
        say "  apk add python3"
    elif [ -f /etc/debian_version ]; then
        say "  apt-get update && apt-get install -y python3"
    elif [ -f /etc/redhat-release ]; then
        say "  dnf install -y python3"
    elif [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        say "  xcode-select --install   # 或 brew install python3"
    else
        say "  请用系统包管理器安装 Python 3.6+"
    fi
}

say "MCP Fleet 依赖检查"
say "------------------"

if have python3; then
    if python3 -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,6) else 1)' 2>/dev/null; then
        say "[ok] python3: $(python3 -V 2>&1)"
    else
        say "[!!] python3 版本过低（需要 3.6+）：$(python3 -V 2>&1)"
        os_hint
        MISSING=1
    fi
else
    say "[!!] 缺少 python3"
    os_hint
    MISSING=1
fi

if have tar; then say "[ok] tar"; else say "[!!] 缺少 tar（解包离线包需要）"; MISSING=1; fi

# 下载工具仅在在线安装时需要
if have curl || have wget; then
    say "[ok] curl/wget"
else
    say "[!!] 缺少 curl 或 wget（联网安装需要）"
    MISSING=1
fi

# 校验工具：sha256sum / shasum / openssl / python3 任一即可
if have sha256sum || have shasum || have openssl; then
    say "[ok] SHA256 校验工具"
elif have python3; then
    say "[ok] SHA256 校验工具（python3 兜底）"
else
    say "[!!] 缺少 SHA256 校验工具"
    MISSING=1
fi

if [ "$MISSING" -eq 0 ]; then
    say "------------------"
    say "依赖齐全，可以安装：sh install.sh --offline <本目录> --name <名字> --port <端口>"
    exit 0
fi
say "------------------"
say "请先补齐上面的依赖再安装。"
exit 1
