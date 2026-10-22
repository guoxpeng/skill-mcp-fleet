#!/bin/sh
# ============================================================================
#  MCP Fleet v5.2.1 · 通用安装引导（POSIX sh，薄引导，不内嵌源码）
# ----------------------------------------------------------------------------
#  作用：找到/下载源码 → 校验（可用时）→ 交给 scripts/fleet_install.py 完成
#        平台识别、服务注册、身份生成、健康检查。
#
#  用法：
#    sh install.sh --name nas --port 3100
#    sh install.sh --join 'fleet://join/...'
#    sh install.sh --offline /path/to/mcp-fleet --name nas
#    sh install.sh --dry-run --json --name test
#
#  引导参数（会被本脚本消费，其余原样传给 fleet_install.py）：
#    --offline <dir>     使用本地源码目录（离线/内网/已 clone）
#    --version <ver>     指定版本（默认 5.2.1），用于下载 release
#    --repo <owner/name> 指定仓库（默认 guoxpeng/skill-mcp-fleet）
#    --no-verify         跳过 SHA256 校验（仅限可信内网，会明确警告）
#    --require-verify    下载包必须校验通过，否则退出
#    -h, --help          显示帮助
#
#  安全：下载 release 包时同时取 SHA256SUMS 校验；校验失败拒绝执行。
#        不做 `curl | sh` 的隐式执行——本脚本只负责下载、校验、调用安装器。
# ============================================================================
set -eu

VERSION="5.2.1"
REPO="guoxpeng/skill-mcp-fleet"
OFFLINE=""
NO_VERIFY=0
REQUIRE_VERIFY=0
PASSTHRU=""

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 解析引导参数，其余原样保留 -------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --offline)        [ $# -ge 2 ] || die "--offline 需要目录参数"; OFFLINE="$2"; shift 2 ;;
        --version)        [ $# -ge 2 ] || die "--version 需要版本号"; VERSION="$2"; shift 2 ;;
        --repo)           [ $# -ge 2 ] || die "--repo 需要 owner/name"; REPO="$2"; shift 2 ;;
        --no-verify)      NO_VERIFY=1; shift ;;
        --require-verify) REQUIRE_VERIFY=1; shift ;;
        -h|--help)        usage ;;
        *)                PASSTHRU="$PASSTHRU
$1"; shift ;;
    esac
done

# 位置参数还原（逐行，兼容含空格参数；参数中不允许换行）
if [ -n "$PASSTHRU" ]; then
    _old_ifs=$IFS
    IFS='
'
    # shellcheck disable=SC2086
    set -- $PASSTHRU
    IFS=$_old_ifs
else
    set --
fi

# ---- 找 python -------------------------------------------------------------
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "未找到 python3。Android/Termux: pkg install python；OpenWrt: opkg install python3-light python3-ssl；Debian/Ubuntu: apt install python3"

# ---- 定位源码 --------------------------------------------------------------
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC=""
TMPDIR_FLEET=""

cleanup() { [ -n "$TMPDIR_FLEET" ] && rm -rf "$TMPDIR_FLEET" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [ -n "$OFFLINE" ]; then
    SRC=$(CDPATH= cd -- "$OFFLINE" 2>/dev/null && pwd) || die "--offline 目录不存在: $OFFLINE"
elif [ -f "$SCRIPT_DIR/fleet_install.py" ]; then
    # 本地仓库 / 已解压的包
    SRC=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
else
    # 远程引导：下载 release 包（带 SHA256SUMS 校验）
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
        || die "需要 curl 或 wget 下载安装包"
    TMPDIR_FLEET=$(mktemp -d 2>/dev/null || echo "/tmp/fleet-install-$$")
    mkdir -p "$TMPDIR_FLEET"
    ZIP="$TMPDIR_FLEET/pkg.zip"
    SUMS="$TMPDIR_FLEET/SHA256SUMS"
    BASE="https://github.com/$REPO/releases/download/v$VERSION"
    fetch() {  # fetch <url> <out>
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL --connect-timeout 15 --max-time 180 "$1" -o "$2"
        else
            wget -q -T 30 -O "$2" "$1"
        fi
    }
    log "下载 MCP Fleet v$VERSION 发布包 ..."
    if ! fetch "$BASE/mcp-fleet-v$VERSION.zip" "$ZIP"; then
        [ "$REQUIRE_VERIFY" = "1" ] && die "下载 release 包失败且要求校验"
        warn "release 包不可用，回退到 main 分支源码（无法提供发布校验和）"
        fetch "https://github.com/$REPO/archive/refs/heads/main.tar.gz" "$TMPDIR_FLEET/pkg.tar.gz" \
            || die "下载源码失败（检查网络/代理）"
        "$PY" - "$TMPDIR_FLEET/pkg.tar.gz" "$TMPDIR_FLEET" <<'PYEOF'
import sys, tarfile
tarfile.open(sys.argv[1]).extractall(sys.argv[2])
PYEOF
        SRC=$(find "$TMPDIR_FLEET" -maxdepth 3 -name fleet_install.py -path '*/scripts/*' | head -1 | sed 's#/scripts/fleet_install.py##')
        [ -n "$SRC" ] || die "源码包结构异常：找不到 scripts/fleet_install.py"
    else
        if fetch "$BASE/SHA256SUMS" "$SUMS"; then
            EXPECT=$(grep -i "mcp-fleet-v$VERSION.zip" "$SUMS" | head -1 | awk '{print $1}')
            if [ -n "$EXPECT" ]; then
                ACTUAL=$("$PY" - "$ZIP" <<'PYEOF'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())
PYEOF
)
                if [ "$EXPECT" != "$ACTUAL" ]; then
                    die "SHA256 校验失败：期望 $EXPECT 实际 $ACTUAL（拒绝安装）"
                fi
                log "SHA256 校验通过"
            elif [ "$REQUIRE_VERIFY" = "1" ]; then
                die "SHA256SUMS 中找不到发布包条目"
            else
                warn "SHA256SUMS 缺少本包条目，继续（建议 --require-verify 场景手工核对）"
            fi
        elif [ "$REQUIRE_VERIFY" = "1" ]; then
            die "无法获取 SHA256SUMS 且要求校验"
        else
            warn "未获取到 SHA256SUMS：跳过发布校验（内网/可信源可接受）"
        fi
        "$PY" - "$ZIP" "$TMPDIR_FLEET" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])
PYEOF
        SRC=$(find "$TMPDIR_FLEET" -maxdepth 3 -name fleet_install.py -path '*/scripts/*' | head -1 | sed 's#/scripts/fleet_install.py##')
        [ -n "$SRC" ] || die "发布包结构异常：找不到 scripts/fleet_install.py"
    fi
fi

[ -f "$SRC/scripts/fleet_install.py" ] || die "源码目录不完整（缺 scripts/fleet_install.py）: $SRC"

log "安装器: $SRC/scripts/fleet_install.py"
exec "$PY" "$SRC/scripts/fleet_install.py" "$@"
