#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make-offline.py —— 把 install.sh 打成「全离线安装包」

为什么需要
----------
有些副设备（内网机器、被墙的云主机、隔离网段）根本下载不了 install.sh。
但只要有 python3，就能靠一段纯文本把它还原出来 —— 纯文本可以走 U 盘、
聊天窗口、内网粘贴板、邮件正文，任何能传字的地方都能传。

产物
----
  install-offline.b64         base64(zlib(install.sh))，**纯 base64，无任何注释行**
  install-offline.sh          自解压脚本：拷过去 `sudo bash install-offline.sh` 就行
  install-offline.partNN.txt  分片（每片约 4000 字符），**纯 base64**，`cat` 拼即可
  install-offline.README.txt  给人看的说明（命令、校验值、注意事项）
  install-offline.sha256      各产物的校验和

用法
----
  python3 make-offline.py                # 用同目录的 install.sh
  python3 make-offline.py --check        # 只校验现有产物与 install.sh 是否一致
  python3 make-offline.py --chunk 3000   # 自定义分片大小（字符）

约定（改代码时别破坏）
--------------------
* 载荷 = zlib.compress(install.sh)，再 base64。**不是 tar、不是 gzip 归档**，
  只有一个文件。历史上有过把它当 tar 解包的误解，所以这里在注释和产物头部
  都写死说明。
* **载荷文件里绝不能出现说明文字**（`.b64` 和 `.partNN.txt` 都是）。踩过的坑：
  注释头 `# MCP 副端 install.sh ... 第 1/9 片` 里的 `MCP`、`install`、`sh`、
  `1/9` 全都在 base64 字母表内，`b64decode` **不会丢弃**它们，而是当数据插进流里
  —— 结果是 `zlib.error: incorrect header check`。而且文档给用户的命令就是
  整文件解码 / `cat` 拼接，所以这两个文件必须是纯载荷。说明文字一律放
  `install-offline.README.txt`。
  自解压脚本 `install-offline.sh` 是例外：它的载荷行前缀 `#`（为了 `bash -n`
  能过），由脚本自己的 python `lstrip("#")` 剥掉，从不依赖朴素解码。
* 生成的 .b64 / .sh 必须保证 LF，且末尾有换行 —— 否则 `base64 -d` 会报错。
* `--check` 必须用**用户实际会敲的命令**去校验（整文件解码、朴素 `cat`），
  不能自己先剥注释 —— 否则会把上面那个坑盖住，校验永远绿。
"""

import argparse
import base64
import hashlib
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "install.sh")
OUT_B64 = os.path.join(HERE, "install-offline.b64")
OUT_SH = os.path.join(HERE, "install-offline.sh")
OUT_SUM = os.path.join(HERE, "install-offline.sha256")
OUT_README = os.path.join(HERE, "install-offline.README.txt")
PART_FMT = os.path.join(HERE, "install-offline.part%02d.txt")

# base64 字母表 —— 载荷文件里出现这之外的字符就说明混进了说明文字。
B64_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "abcdefghijklmnopqrstuvwxyz"
                "0123456789+/=\n\r")

# 自解压脚本模板。{payload} 会被替换成 base64 文本（每行前面加 # 注释符）。
#
# 为什么不把载荷塞进 heredoc？
#   `python3 -c '...' "$TMP" <<'EOF'` 在 Git-Bash + Windows Python 下 stdin 拿不到数据
#   （实测解出 0 字节）。改成让 python 直接读本脚本自己、按标记切出载荷，
#   在任何平台都稳，也方便 `bash -n` 静态检查（载荷行是注释，不会被当命令解析）。
SELF_EXTRACT = r"""#!/usr/bin/env bash
# ============================================================================
# MCP 副端 —— 全离线安装（自解压版）
# ----------------------------------------------------------------------------
# 这个文件里**内嵌**了完整的 install.sh。拷到副设备上直接跑就行：
#
#     sudo bash install-offline.sh --name nas --port 3100
#
# 只需要副设备上有 bash + python3，**不需要任何网络**。
#
# 内嵌格式：base64( zlib( install.sh ) )，一个文件，不是 tar 包。
# 源 install.sh 大小 {size} 字节，md5 {md5}
#
# 注意：请整文件原样拷贝，不要用会把换行改成 CRLF 的编辑器另存，
#       否则解压会失败（脚本里有 md5 校验会拦住）。
# ============================================================================
set -euo pipefail

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[x] 本机没有 python3，无法自解压。"
  echo "    退路：用 install-offline.b64（纯文本）在别的机器上解出 install.sh 再拷过来。"
  exit 1
fi

# 定位自己：优先 BASH_SOURCE，其次 $0；相对路径补成绝对路径
SELF="${{BASH_SOURCE[0]:-$0}}"
case "$SELF" in
  /*) ;;
  *) SELF="$(pwd)/$SELF" ;;
esac
# Git-Bash on Windows：POSIX 路径（/c/...）原生 python 打不开，转成 Windows 路径。
# Linux 上没有 cygpath，这行是空操作。
if command -v cygpath >/dev/null 2>&1; then
  SELF="$(cygpath -w "$SELF" 2>/dev/null || printf '%s' "$SELF")"
fi
if [ ! -f "$SELF" ]; then
  echo "[x] 找不到自身文件（$SELF）。请用 `bash install-offline.sh ...` 运行，"
  echo "    不要用 `bash < install-offline.sh` 这种把内容灌进 stdin 的方式。"
  exit 1
fi

TMP="$(mktemp /tmp/mcp-install-XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT
# Git-Bash on Windows 下 /tmp 与原生 python 眼中的 C:\tmp 不是同一个地方，
# 所以要给 python 一个它能打开的路径；Linux 上没有 cygpath，这里就等于 $TMP。
TMP_PY="$TMP"
if command -v cygpath >/dev/null 2>&1; then
  TMP_PY="$(cygpath -w "$TMP" 2>/dev/null || printf '%s' "$TMP")"
fi

# 从自身文件里切出 FLEET_PAYLOAD_BEGIN..END 之间的 base64（行首 # 是注释符，剥掉）
"$PY" -c '
import base64, sys, zlib
src, dst = sys.argv[1], sys.argv[2]
buf, on = [], False
for line in open(src, encoding="utf-8", errors="ignore"):
    s = line.strip()
    if s == "FLEET_PAYLOAD_BEGIN":
        on = True; continue
    if s == "FLEET_PAYLOAD_END":
        break
    if on:
        buf.append(s.lstrip("#").strip())
data = "".join(buf)
if not data:
    sys.stderr.write("[x] 没找到内嵌载荷（文件可能被截断了）。\n")
    sys.exit(4)
try:
    raw = zlib.decompress(base64.b64decode(data))
except Exception as e:
    sys.stderr.write("[x] 解压失败：%r\n    文件可能在传输中被改动（比如换行变成 CRLF）。\n" % (e,))
    sys.exit(3)
open(dst, "wb").write(raw)
' "$SELF" "$TMP_PY" || exit 3

GOT="$(md5sum "$TMP" 2>/dev/null | awk '{{print $1}}' || true)"
if [ -n "$GOT" ] && [ "$GOT" != "{md5}" ]; then
  echo "[!] 校验和不符：期望 {md5}，实得 $GOT"
  echo "    仍要继续请加 FLEET_SKIP_CHECK=1"
  [ "${{FLEET_SKIP_CHECK:-0}}" = "1" ] || exit 1
fi

echo "[+] 已解出安装脚本（{size} 字节），开始安装 ..."
exec bash "$TMP" "$@"
exit 0

# ============================================================================
# 以下是内嵌载荷（base64），仅供上面的 python 读取；行首 # 只是注释符。
# 请勿手工编辑。
# ============================================================================
FLEET_PAYLOAD_BEGIN
{payload}
FLEET_PAYLOAD_END
"""

# 给人看的说明文件。**不要**把这些文字塞进 .b64 / .partNN.txt —— 见文件头「约定」。
OFFLINE_README = """MCP 副端 · 全离线安装包说明
============================================================
内嵌的是 install.sh：{size} 字节，md5 {md5}
载荷格式 = base64( zlib( install.sh ) ) —— **单个压缩文件，不是 tar 包**，
不要用 `tar xf` 去解，那样只会报错。

下面三个文件内容等价，按「你能传什么」挑一个用。

------------------------------------------------------------
【1】install-offline.sh —— 推荐，只需传一个文件
------------------------------------------------------------
拷到副设备上（U 盘 / scp / 内网共享都行），然后：

    sudo bash install-offline.sh --name <名字> --port 3100

脚本会自己切出内嵌载荷、校验 md5、然后执行安装。需要副设备有 bash + python3。
请整文件原样拷贝，别用会把换行改成 CRLF 的编辑器另存（脚本里有 md5 校验会拦住）。

------------------------------------------------------------
【2】install-offline.b64 —— 纯文本，适合粘贴
------------------------------------------------------------
只有 base64，没有任何注释行 —— 所以下面这条命令是**整文件直接解码**，不用预处理：

    python3 -c "import base64,zlib;open('/tmp/mcp-install.sh','wb').write(zlib.decompress(base64.b64decode(open('install-offline.b64','rb').read())))"
    md5sum /tmp/mcp-install.sh      # 应为 {md5}
    sudo bash /tmp/mcp-install.sh --name <名字> --port 3100

------------------------------------------------------------
【3】install-offline.partNN.txt —— 单条消息有长度限制时用
------------------------------------------------------------
共 {total} 片，每片 {chunk} 字符，全部是纯 base64。**按序号顺序拼起来**即可：

    cat install-offline.part*.txt > install-offline.b64
    md5sum install-offline.b64      # 与上面的 install-offline.b64 一致

然后照【2】的命令还原安装。
（别把片名里的数字当顺序乱拼；`cat` 是按文件名排序的，part01…part{total:02d} 正好对。）

------------------------------------------------------------
校验
------------------------------------------------------------
    md5sum install.sh                        {md5}   ← 解出来的应是这个
    python3 make-offline.py --check          # 逐字节比对全部产物

全部产物的校验和见 install-offline.sha256。
"""


def md5_of(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def load_payload():
    if not os.path.isfile(SRC):
        print("[x] 找不到 %s —— 先跑 python3 build_installer.py 生成它" % SRC)
        sys.exit(2)
    with open(SRC, "rb") as f:
        raw = f.read()
    if b"\r\n" in raw:
        print("[!] 警告：%s 含 CRLF（%d 处）。在 Linux 上执行会出错，"
              "请先转成 LF。" % (SRC, raw.count(b"\r\n")))
    return raw, base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def write_lf(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def wrap(s, width=76):
    return "\n".join(s[i:i + width] for i in range(0, len(s), width))


def do_build(chunk):
    raw, b64 = load_payload()
    digest = hashlib.md5(raw).hexdigest()

    # 1) install-offline.b64 —— **纯 base64，一行说明都不加**
    #    见文件头「约定」：注释里的英文/数字都在 base64 字母表内，会被当数据解进去。
    write_lf(OUT_B64, wrap(b64) + "\n")

    # 2) install-offline.sh（自解压）
    #    每行前面加 "#"：这样即使有人 `bash -n` 静态检查，载荷也只是注释，
    #    不会被当成命令解析；python 侧再 lstrip("#") 还原。
    body = "\n".join("#" + l for l in wrap(b64, 100).splitlines())
    write_lf(OUT_SH, SELF_EXTRACT.format(payload=body, size=len(raw), md5=digest))
    os.chmod(OUT_SH, 0o755)

    # 3) 分片 —— 同样是纯 base64，这样 `cat install-offline.part*.txt > x.b64` 直接可用
    for old in os.listdir(HERE):
        if old.startswith("install-offline.part") and old.endswith(".txt"):
            os.remove(os.path.join(HERE, old))
    parts = [b64[i:i + chunk] for i in range(0, len(b64), chunk)] or [""]
    total = len(parts)
    for i, p in enumerate(parts, 1):
        write_lf(PART_FMT % i, p + "\n")

    # 3.5) 给人看的说明（命令、校验值）—— 单独一个文件，永远不会被拼进载荷
    write_lf(OUT_README, OFFLINE_README.format(
        size=len(raw), md5=digest, total=total, chunk=chunk))

    # 4) 校验和
    lines = []
    for p in [SRC, OUT_B64, OUT_SH, OUT_README] + [PART_FMT % i for i in range(1, total + 1)]:
        if os.path.isfile(p):
            lines.append("%s  %s" % (md5_of(p), os.path.basename(p)))
    write_lf(OUT_SUM, "\n".join(lines) + "\n")

    print("[+] 源 install.sh         %7d 字节  md5 %s" % (len(raw), digest))
    print("[+] install-offline.b64   %6d 字节（纯 base64，可直接粘贴）" % os.path.getsize(OUT_B64))
    print("[+] install-offline.sh    %6d 字节（自解压，只需这一个文件）" % os.path.getsize(OUT_SH))
    print("[+] 分片 %d 片，每片 %d 字符 → %s" % (total, chunk, os.path.basename(PART_FMT % 1)))
    print("[+] 说明 %s" % os.path.basename(OUT_README))
    print("[+] 校验和 %s" % os.path.basename(OUT_SUM))
    print()
    print("    副端上执行（任选其一）：")
    print("      sudo bash install-offline.sh --name <名字> --port 3100")
    print("      python3 -c \"...\"  && sudo bash /tmp/mcp-install.sh --name <名字> --port 3100")
    return 0


def do_check():
    if not os.path.isfile(SRC):
        print("[x] 没有 %s" % SRC)
        return 2
    raw = open(SRC, "rb").read()
    digest = hashlib.md5(raw).hexdigest()
    ok = True
    print("源 install.sh: %d 字节  md5 %s" % (len(raw), digest))

    def bad_chars(txt):
        """载荷里混进说明文字的话，返回那些非法字符（base64 字母表之外的除外，
        字母表之内的混入是**看不见**的，所以另有一道「解出来对不对」的校验兜底）。"""
        return sorted(set(txt) - B64_CHARS)

    # 用「用户实际会敲的命令」校验：整文件直接 b64decode，绝不自己剥注释。
    # 自己剥注释会把「注释混进载荷」这类 bug 盖住，校验就永远是绿的了。
    if not os.path.isfile(OUT_B64):
        print("[x] 缺 %s" % os.path.basename(OUT_B64))
        ok = False
    else:
        txt = open(OUT_B64, encoding="utf-8").read()
        bad = bad_chars(txt)
        if bad:
            print("[x] install-offline.b64  含非 base64 字符 %r —— 直接解码会失败" % (bad[:12],))
            ok = False
        try:
            got = zlib.decompress(base64.b64decode(txt))
        except Exception as e:
            print("[x] install-offline.b64  整文件解码失败：%r" % (e,))
            print("    多半是文件里混进了说明文字（注释里的英文/数字也在 base64 字母表内，")
            print("    不会被 b64decode 丢弃，而是当成数据插进流里）。")
            ok = False
        else:
            if got == raw:
                print("[+] install-offline.b64  一致（整文件解码）")
            else:
                print("[x] install-offline.b64  与 install.sh 不一致"
                      "（%d 字节 vs %d）—— 跑 make-offline.py 重新生成"
                      % (len(got), len(raw)))
                ok = False

    if os.path.isfile(OUT_SH):
        txt = open(OUT_SH, encoding="utf-8").read()
        if digest not in txt:
            print("[x] install-offline.sh   内嵌 md5 过期 —— 重新生成")
            ok = False
        else:
            # 真解一遍，确认能还原出 install.sh
            buf, on = [], False
            for line in txt.splitlines():
                s = line.strip()
                if s == "FLEET_PAYLOAD_BEGIN":
                    on = True
                    continue
                if s == "FLEET_PAYLOAD_END":
                    break
                if on:
                    buf.append(s.lstrip("#").strip())
            try:
                got = zlib.decompress(base64.b64decode("".join(buf)))
            except Exception as e:
                print("[x] install-offline.sh   自解压失败：%r" % e)
                ok = False
            else:
                if got == raw:
                    print("[+] install-offline.sh   自解压还原一致")
                else:
                    print("[x] install-offline.sh   自解压结果与 install.sh 不一致")
                    ok = False
    else:
        print("[x] 缺 %s" % os.path.basename(OUT_SH))
        ok = False

    parts = sorted(f for f in os.listdir(HERE)
                   if f.startswith("install-offline.part") and f.endswith(".txt"))
    if parts:
        # 模拟用户的 `cat install-offline.part*.txt > x.b64`：原样拼接，不剥任何东西
        joined = "".join(open(os.path.join(HERE, f), encoding="utf-8").read()
                         for f in parts)
        bad = bad_chars(joined)
        if bad:
            print("[x] 分片含非 base64 字符 %r —— `cat` 拼接后会解码失败" % (bad[:12],))
            ok = False
        try:
            if zlib.decompress(base64.b64decode(joined)) == raw:
                print("[+] %d 个分片 `cat` 拼接后一致" % len(parts))
            else:
                print("[x] 分片拼接后与 install.sh 不一致")
                ok = False
        except Exception as e:
            print("[x] 分片拼接解压失败：%r" % e)
            ok = False
    else:
        print("[x] 没有分片文件")
        ok = False

    # 自解压脚本必须是 LF（CRLF 会让 bash 报错）
    for p in (OUT_SH, SRC):
        if os.path.isfile(p):
            n = open(p, "rb").read().count(b"\r\n")
            if n:
                print("[x] %s 含 %d 处 CRLF —— Linux 上执行会出错" % (os.path.basename(p), n))
                ok = False

    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="把 install.sh 打成全离线安装包")
    ap.add_argument("--chunk", type=int, default=4000, help="分片大小（字符，默认 4000）")
    ap.add_argument("--check", action="store_true", help="只校验现有产物")
    a = ap.parse_args()
    return do_check() if a.check else do_build(a.chunk)


if __name__ == "__main__":
    sys.exit(main())
