#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Fleet 参赛提交包生成器（纯标准库）。

把仓库裁剪成一个符合 Agent Skills 规范、≤30 文件、≤40 MB 的提交包，并生成
SHA256SUMS 与 MANIFEST.json，保证评审可复现、可校验。

用法：
    python3 scripts/build_package.py
    python3 scripts/build_package.py --check          # 只校验，不构建
    python3 scripts/build_package.py --out dist --version 5.2.1
    python3 scripts/build_package.py --with-tests      # 默认也包含 tests
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_VERSION = "5.2.1"
MAX_FILES = 30
MAX_BYTES = 40 * 1024 * 1024
SKILL_NAME = "mcp-fleet"

# 提交包白名单：按顺序收集，超过 MAX_FILES 时先丢 references 之外的大文件。
CORE_FILES = [
    "SKILL.md",
    "README.md",
    "AGENTS.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "LICENSE",
]

SCRIPT_FILES = [
    "scripts/mcp_agent.py",
    "scripts/fleet_proto.py",
    "scripts/fleet_core.py",
    "scripts/fleet_agent.py",
    "scripts/fleet_install.py",
    "scripts/fleet.py",
    "scripts/dashboard_html.py",
    "scripts/install.sh",
    "scripts/demo.sh",
    "scripts/mcp_agent_config.example.json",
]

TEMPLATE_FILES = [
    "scripts/service-templates/fleet.service",
    "scripts/service-templates/fleet-compose.yml",
    "scripts/service-templates/Dockerfile",
]

TEST_FILES = [
    "tests/conftest.py",
    "tests/test_core_51.py",
    "tests/test_installer_51.py",
    "tests/test_integration_51.py",
]

REFERENCE_FILES = [
    "references/agent-playbook.md",
    "references/self-dialogue-protocol.md",
    "references/prompt-pack.md",
    "references/security-model.md",
    "references/platform-matrix.md",
    "references/quickstart.md",
    "references/troubleshooting.md",
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_frontmatter(path):
    """极简 YAML frontmatter 解析：只取顶层 key: value，够校验标准字段。"""
    out = {}
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 缺少 frontmatter（首行必须是 ---）")
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    raise ValueError("SKILL.md frontmatter 未闭合")


def validate_skill():
    """按 Agent Skills 规范校验：name/description 必填、name 合法、description 长度适中。"""
    path = os.path.join(ROOT, "SKILL.md")
    errors = []
    if not os.path.exists(path):
        return ["SKILL.md 不存在"]
    try:
        fm = parse_frontmatter(path)
    except ValueError as e:
        return [str(e)]
    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    unknown = set(fm) - allowed
    if unknown:
        errors.append("frontmatter 含非标准字段: %s" % ", ".join(sorted(unknown)))
    name = fm.get("name", "")
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        errors.append("name 必须是小写字母/数字/连字符: %r" % name)
    desc = fm.get("description", "")
    if len(desc) < 50:
        errors.append("description 太短（<50 字符），无法触发")
    if len(desc) > 1024:
        errors.append("description 超过 1024 字符（当前 %d）" % len(desc))
    body_lines = 0
    in_body = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            if in_body:
                body_lines += 1
            elif line.strip() == "---":
                in_body = True
    if body_lines > 500:
        errors.append("SKILL.md 正文 %d 行，超过 500 行，应移到 references/" % body_lines)
    return errors


def collect(with_tests=True):
    groups = [("core", CORE_FILES), ("scripts", SCRIPT_FILES),
              ("templates", TEMPLATE_FILES), ("references", REFERENCE_FILES)]
    if with_tests:
        groups.append(("tests", TEST_FILES))
    files = []
    for group, names in groups:
        for rel in names:
            if os.path.exists(os.path.join(ROOT, rel)):
                files.append((group, rel))
    return files


def build(out_dir, version, with_tests=True, max_files=MAX_FILES):
    errors = validate_skill()
    if errors:
        print("[x] SKILL 校验失败：", file=sys.stderr)
        for e in errors:
            print("    - " + e, file=sys.stderr)
        return 1

    files = collect(with_tests)
    if len(files) > max_files:
        # 优先裁剪：先丢测试，再丢大体积 reference；保留 SKILL/README/核心脚本。
        files = [f for f in files if f[0] != "tests"]
    if len(files) > max_files:
        files = files[:max_files]
    total = sum(os.path.getsize(os.path.join(ROOT, rel)) for _, rel in files)
    if total > MAX_BYTES:
        print("[x] 包体 %.1f MB 超过 40 MB 上限" % (total / 1024.0 / 1024.0), file=sys.stderr)
        return 1

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "mcp-fleet-v%s.zip" % version)
    manifest = {
        "schema": "mcp-fleet-package/1",
        "name": SKILL_NAME,
        "version": version,
        "file_count": len(files),
        "total_bytes": total,
        "entry": "SKILL.md",
        "files": {},
        "generated_by": "scripts/build_package.py",
    }
    # 确定性时间戳：便于复现构建（zip 内时间固定为 2026-01-01）。
    fixed_date = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for _group, rel in files:
            src = os.path.join(ROOT, rel)
            digest = sha256_file(src)
            manifest["files"][rel] = {"sha256": digest, "bytes": os.path.getsize(src)}
            arc = SKILL_NAME + "/" + rel
            info = zipfile.ZipInfo(arc, date_time=fixed_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                zf.writestr(info, f.read())
        zf.writestr(SKILL_NAME + "/MANIFEST.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))

    # SHA256SUMS：包内文件摘要（不含自身）
    sums_path = os.path.join(out_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (sha256_file(zip_path), os.path.basename(zip_path)))
        for rel, meta in sorted(manifest["files"].items()):
            f.write("%s  %s/%s\n" % (meta["sha256"], SKILL_NAME, rel))

    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)

    print("[+] 提交包: %s" % zip_path)
    print("    文件数: %d（上限 %d）  大小: %.1f KB（上限 40 MB）"
          % (len(files), max_files, total / 1024.0))
    print("    校验和: %s" % sums_path)
    if len(files) > max_files:
        print("[!] 已自动裁剪到上限，请检查是否缺少必要文件", file=sys.stderr)
        return 1
    return 0


def check():
    errors = validate_skill()
    files = collect()
    total = sum(os.path.getsize(os.path.join(ROOT, rel)) for _, rel in files)
    print("SKILL 校验: %s" % ("通过" if not errors else "失败"))
    for e in errors:
        print("  - " + e)
    print("收集文件: %d / %d，总大小 %.1f KB" % (len(files), MAX_FILES, total / 1024.0))
    missing = [rel for _, rel in files if not os.path.exists(os.path.join(ROOT, rel))]
    if missing:
        print("缺失文件（构建时会跳过）: %s" % ", ".join(missing))
    return 0 if not errors and len(files) <= MAX_FILES else 1


def main():
    ap = argparse.ArgumentParser(description="生成 MCP Fleet 参赛提交包")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--check", action="store_true", help="只校验，不构建")
    ap.add_argument("--no-tests", action="store_true", help="不包含 tests/")
    ap.add_argument("--max-files", type=int, default=MAX_FILES)
    args = ap.parse_args()
    if args.check:
        return check()
    return build(args.out, args.version, with_tests=not args.no_tests,
                 max_files=args.max_files)


if __name__ == "__main__":
    sys.exit(main())
