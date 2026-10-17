#!/usr/bin/env bash
# =============================================================================
# mcp-fleet 副端一键安装（对齐 v3.1）
#
#   bash install-fleet-node.sh check                    # 只下载 + 校验，不安装
#   bash install-fleet-node.sh install <名字> [端口]     # 下载 + 校验 + 安装（带公网隧道 + 强制鉴权）
#   bash install-fleet-node.sh uninstall <名字>          # 卸载
#
#   端口默认 3100；install 之后多出来的参数会原样透传给 install.sh，
#   例如： bash install-fleet-node.sh install mysrv 3100 --allow-ips 1.2.3.4
#
# 为什么不能直接 `curl ... | bash`（官方文档那种写法）：
#   1. md5 是**比对**，不是打印。原文档把 md5 打出来让人肉眼看，
#      通道被截断/替换时根本看不出来 —— 这里体积 + md5 双校验，不符就换通道。
#   2. 先落盘再执行，能先 `bash -n` 语法校验，管道流式执行做不到。
#   3. 通道回退：raw.githubusercontent.com 在国内经常被掐，先走镜像。
#
# 默认带上 --tunnel --generate-token --require-auth：
#   公网隧道场景下**必须**鉴权，否则等于把 root shell 挂到公网上。
# =============================================================================
set -euo pipefail

SIZE=88743
MD5=ba8d4c106adc6185a22c7586f696b46e
RAW="https://raw.githubusercontent.com/guoxpeng/skill-mcp-fleet/main/install.sh"
F="${TMPDIR:-/tmp}/mcp-install.sh"

have() { command -v "$1" >/dev/null 2>&1; }

fetch_url() {   # $1=url  $2=outfile
  if have curl; then
    curl -fsSL --connect-timeout 15 --max-time 180 "$1" -o "$2"
  elif have wget; then
    wget -q -T 180 -O "$2" "$1"
  else
    echo "[x] 既没有 curl 也没有 wget，装一个再跑" >&2
    return 127
  fi
}

file_md5() {    # $1=file -> stdout md5（取不到就输出空）
  if have md5sum; then md5sum "$1" | cut -d' ' -f1
  elif have md5;    then md5 -q "$1"
  elif have openssl; then openssl md5 "$1" 2>/dev/null | awk '{print $NF}'
  else echo ""
  fi
}

fetch() {
  echo "[*] 目标版本: ${SIZE} 字节  md5 ${MD5}"
  echo "[*] 仓库    : guoxpeng/skill-mcp-fleet"
  for U in \
    "https://ghfast.top/${RAW}" \
    "https://ghproxy.net/${RAW}" \
    "https://gh-proxy.com/${RAW}" \
    "https://ghproxy.cc/${RAW}" \
    "${RAW}"; do
    echo "[*] 尝试通道: ${U}"
    if fetch_url "$U" "$F"; then
      SZ="$(wc -c < "$F" 2>/dev/null || echo 0)"
      if [ "$SZ" -eq "$SIZE" ]; then
        echo "[+] 下载成功 ${SZ} 字节"
        GOT="$(file_md5 "$F")"
        if [ -z "$GOT" ]; then
          echo "[!] 本机没有 md5sum/md5/openssl，跳过 md5 校验（体积已对上）"
          bash -n "$F" && echo "[+] 语法校验通过"
          return 0
        fi
        if [ "$GOT" = "$MD5" ]; then
          echo "[+] md5 校验通过: ${GOT}"
          bash -n "$F" && echo "[+] 语法校验通过"
          return 0
        fi
        echo "[!] md5 不符（期望 ${MD5}，实际 ${GOT}），丢弃该通道"
      else
        echo "[!] 体积异常（${SZ} 字节，期望 ${SIZE}），丢弃该通道"
      fi
    else
      echo "[!] 通道不可达"
    fi
  done
  echo "[x] 所有通道都没拿到正确的 install.sh"
  echo "    改用离线包（仓库根目录）："
  echo "      install-offline.sh              # 自解压，一条命令"
  echo "      install-offline.b64             # base64，需整文件解码"
  echo "      install-offline.part01..10.txt  # 分片，按 README 拼接"
  return 1
}

need_root() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""
  elif have sudo; then SUDO="sudo"
  else echo "[x] 需要 root 权限：请用 root 登录，或先装 sudo" >&2; exit 1
  fi
}

case "${1:-install}" in
  check)
    fetch
    echo
    echo "[+] 校验完成，未安装。要装就跑： bash $0 install <名字> [端口]"
    ;;

  install)
    NAME="${2:-cloud}"
    PORT="${3:-3100}"
    [ $# -ge 3 ] && shift 3 || shift $#
    need_root
    fetch
    echo
    echo "[*] 开始安装：name=${NAME} port=${PORT}（公网隧道 + 强制鉴权）"
    echo
    $SUDO bash "$F" --name "$NAME" --port "$PORT" \
        --tunnel --generate-token --require-auth "$@"
    echo
    echo "============================================================"
    echo " 装完了。上面输出里的「公网隧道地址」和「鉴权 token」抄下来，"
    echo " 在主端（跑 agent 的那台机器）注册："
    echo
    echo "   python fleet.py add --name ${NAME} --url https://<隧道域名>/mcp --token <鉴权 token>"
    echo "   python fleet.py doctor ${NAME}      # 六项自检"
    echo "   python fleet.py exec ${NAME} 'uname -a; whoami; docker ps'"
    echo "============================================================"
    ;;

  uninstall)
    NAME="${2:-cloud}"
    need_root
    fetch
    echo
    echo "[*] 卸载：name=${NAME}"
    $SUDO bash "$F" --name "$NAME" --uninstall
    ;;

  *)
    echo "用法: $0 check | install <名字> [端口] [额外参数...] | uninstall <名字>"
    exit 2
    ;;
esac
