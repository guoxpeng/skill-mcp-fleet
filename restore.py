#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp-fleet 沙箱重置一键恢复脚本。

背景：开发沙箱会周期性全量清空 /workspace，本地一切文件（源码、zip、
辅助脚本）都会丢失；GitHub 仓库是唯一可信源。本脚本把重置后的恢复
流程固化为一条命令，避免临场重踩历史坑：

    cd /workspace && python3 restore.py        # 或 git clone 后在仓库内执行

依次完成：
  1. hosts 直连：探测 GitHub 可用 IP 写入 /etc/hosts（沙箱 DNS 间歇故障）
  2. 确保仓库存在且为最新（公开仓库，clone 无需凭证）
  3. 安装 pytest（幂等）
  4. 全量测试回归（预期 125 passed + 1 skipped）
  5. 重打包 30 文件大赛 zip（/workspace/mcp-fleet.zip）+ 标志校验

已知坑（本脚本已固化处理）：
  - /etc/hosts 是 bind mount，sed -i 会报 "Device or resource busy"，
    需用 Python 直接读写（truncate 不 rename）
  - zip 必须根目录平铺（不带 mcp-fleet/ 顶层目录）且恰好 30 文件
    （华为云技能市场 ≤30 文件 ≤40MB 硬约束）
  - 治理文件（.github/、CONTRIBUTING.md、SECURITY.md、restore.py 自身）
    不进 zip，打包时必须排除

本脚本不做 push / release / 华为云提交（需凭证，属人工或 agent 会话内操作）。
"""
import json
import os
import re
import socket
import subprocess
import sys
import zipfile

REPO_URL = "https://github.com/guoxpeng/skill-mcp-fleet.git"
WORKSPACE = "/workspace"
REPO_DIR = os.path.join(WORKSPACE, "mcp-fleet")
ZIP_OUT = os.path.join(WORKSPACE, "mcp-fleet.zip")

# GitHub 已知 IP 池（沙箱 DNS 对 github.com/api.github.com 间歇失效，
# TCP 探测 443 后取第一个可用 IP 写 hosts）
GITHUB_IPS = [
    "140.82.112.3", "140.82.113.3", "140.82.114.3", "140.82.116.3",
    "140.82.121.3", "20.205.243.160", "20.205.243.161", "20.205.243.166",
    "20.27.177.113", "20.200.245.247",
]

# 打包排除（华为云 ≤30 文件约束）：
# - 原有 5 个无引用/历史文件；.gitignore 无用
# - 治理文件不进 zip：.github/ 全部、CONTRIBUTING.md、SECURITY.md、restore.py
EXCLUDE_FILES = {
    ".gitignore", "restore.py", "CONTRIBUTING.md", "SECURITY.md",
    "scripts/make-offline.py", "scripts/add_fleet_node.py",
    "scripts/fleet-http.service", "scripts/install-fleet-node.sh",
}
EXCLUDE_PREFIXES = (".github/",)


def sh(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("[FAIL] %s\n%s" % (" ".join(cmd), r.stderr[:400]))
        sys.exit(1)
    return r.stdout


def ensure_github_hosts():
    """探测可用 IP 并写入 /etc/hosts（幂等，处理 bind-mount 不可 rename）。"""
    usable = []
    for ip in GITHUB_IPS:
        s = socket.socket()
        s.settimeout(4)
        try:
            s.connect((ip, 443))
            usable.append(ip)
        except OSError:
            pass
        finally:
            s.close()
    if not usable:
        print("[SKIP] hosts: 所有 GitHub IP 均不可达（可能暂不需要直连）")
        return
    ip = usable[0]
    try:
        with open("/etc/hosts", "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        github_lines = [ln for ln in lines if re.search(r"\bgithub\.com\b", ln)]
        if any(ln.split()[0] == ip for ln in github_lines):
            print("[OK]   hosts: github.com -> %s 已就绪" % ip)
            return
        # 移除旧 github.com 行，追加新行（truncate 写回，不走 rename）
        kept = [ln for ln in lines if not re.search(r"\bgithub\.com\b", ln)]
        kept.append("%s github.com" % ip)
        with open("/etc/hosts", "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        print("[OK]   hosts: github.com -> %s（其余可用: %d 个）" % (ip, len(usable) - 1))
    except OSError as e:
        print("[WARN] hosts 写入失败（%s），若 clone 失败请手动处理" % e)


def ensure_repo():
    """仓库存在则同步到 origin/main 最新，否则 clone。"""
    if os.path.isdir(os.path.join(REPO_DIR, ".git")):
        sh(["git", "fetch", "origin"], cwd=REPO_DIR)
        local = sh(["git", "rev-parse", "HEAD"], cwd=REPO_DIR).strip()
        remote = sh(["git", "rev-parse", "origin/main"], cwd=REPO_DIR).strip()
        if local == remote:
            print("[OK]   repo: 已是最新 (%s)" % local[:7])
        else:
            sh(["git", "reset", "--hard", "origin/main"], cwd=REPO_DIR)
            print("[OK]   repo: 已同步到 origin/main (%s)" % remote[:7])
        return
    if os.path.isdir(REPO_DIR):
        print("[FAIL] %s 存在但非 git 仓库，请手动清理后重跑" % REPO_DIR)
        sys.exit(1)
    sh(["git", "clone", REPO_URL, REPO_DIR], cwd=WORKSPACE)
    head = sh(["git", "rev-parse", "HEAD"], cwd=REPO_DIR).strip()
    print("[OK]   repo: clone 完成 (%s)" % head[:7])


def ensure_pytest():
    r = subprocess.run([sys.executable, "-m", "pytest", "--version"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("[OK]   pytest: %s" % r.stdout.split()[-1].strip())
        return
    sh([sys.executable, "-m", "pip", "install", "-q", "pytest"])
    print("[OK]   pytest: 已安装")


def run_tests():
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                       cwd=REPO_DIR, capture_output=True, text=True)
    tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1]
    print("[OK]   tests: %s" % tail)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        sys.exit(1)
    return tail


def pack_zip():
    out = sh(["git", "-c", "core.quotepath=false", "ls-files"], cwd=REPO_DIR)
    files = [f for f in out.split("\n") if f.strip()
             and f not in EXCLUDE_FILES
             and not f.startswith(EXCLUDE_PREFIXES)]
    assert len(files) == 30, "文件数 %d != 30（检查 EXCLUDE 是否需同步治理文件）" % len(files)

    if os.path.exists(ZIP_OUT):
        os.remove(ZIP_OUT)
    with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(REPO_DIR, f), f)

    # 校验：根目录平铺 + 版本号三处一致 + 核心标志存在
    z = zipfile.ZipFile(ZIP_OUT)
    assert all(not n.startswith("mcp-fleet/") for n in z.namelist()), "zip 不能带顶层目录"
    skill = z.read("SKILL.md").decode("utf-8", "replace")
    agent = z.read("scripts/mcp_agent.py").decode("utf-8", "replace")
    cfgex = z.read("scripts/mcp_agent_config.example.json").decode("utf-8", "replace")
    v_skill = re.search(r"^version:\s*(\S+)", skill, re.M).group(1)
    v_agent = re.search(r'"version":\s*"([^"]+)"', agent).group(1)
    v_cfg = json.loads(cfgex)["version"]
    assert v_skill == v_agent == v_cfg, "版本号不一致: %s/%s/%s" % (v_skill, v_agent, v_cfg)
    for flag in ("READ_ONLY_BLOCKED", "HTTP_SLOT"):
        assert flag in agent, "核心标志缺失: " + flag
    proto = z.read("scripts/fleet_proto.py").decode("utf-8", "replace")
    for flag in ("_seen_ids", "_seen_set"):
        assert flag in proto, "协议层标志缺失: " + flag
    size = os.path.getsize(ZIP_OUT)
    assert size <= 40 * 1048576, "zip 超 40MB"
    print("[OK]   zip: %d 文件, %.2f MB, 版本 %s 三处一致, 标志校验通过"
          % (len(files), size / 1048576.0, v_skill))


def main():
    print("== mcp-fleet 沙箱恢复 ==")
    ensure_github_hosts()
    ensure_repo()
    ensure_pytest()
    run_tests()
    pack_zip()
    print("\n恢复完成：源码 %s\n大赛 zip：%s\n（push/release/华为云提交需另行操作）"
          % (REPO_DIR, ZIP_OUT))


if __name__ == "__main__":
    main()
