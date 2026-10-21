#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布 MCP Fleet 到 GitHub Release（纯标准库，零依赖）。

做的事情：
  1. 读取 dist/mcp-fleet-v<ver>.zip 与 SHA256SUMS；
  2. 用 GitHub API 创建（或复用）tag 对应的 release；
  3. 上传 zip + SHA256SUMS 作为资产。

凭据顺序：
  - 环境变量 GITHUB_TOKEN / GH_TOKEN；
  - 否则调用 `git credential fill`（HTTPS 凭据，常见为 PAT）。
凭据不会打印到输出。

示例：
  python3 scripts/release.py --dry-run
  python3 scripts/release.py
  python3 scripts/release.py --repo guoxpeng/skill-mcp-fleet --tag v5.2.0
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://api.github.com"


def local_version():
    """从 fleet_install.py 读取 VERSION，避免重复维护。"""
    path = os.path.join(HERE, "fleet_install.py")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("VERSION") and "=" in line:
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return "0.0.0"


def find_assets(dist_dir, version):
    zip_name = "mcp-fleet-v%s.zip" % version
    zip_path = os.path.join(dist_dir, zip_name)
    sums_path = os.path.join(dist_dir, "SHA256SUMS")
    missing = [p for p in (zip_path, sums_path) if not os.path.isfile(p)]
    return zip_path, sums_path, missing


def credential():
    """返回 (用户名, token)；找不到返回 (None, None)。不打印 token。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return "x-access-token", token.strip()
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=15)
        data = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
        if data.get("password"):
            return data.get("username") or "x-access-token", data["password"]
    except Exception:
        pass
    return None, None


def api(method, url, token, payload=None, raw=None, content_type=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mcp-fleet-release",
    }
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:600]
        return exc.code, {"error": body}


def create_or_get_release(repo, tag, token, name, notes, draft, prerelease):
    status, data = api("POST", "%s/repos/%s/releases" % (API, repo), token, {
        "tag_name": tag,
        "name": name,
        "body": notes,
        "draft": draft,
        "prerelease": prerelease,
    })
    if status in (200, 201):
        return data
    if status == 422:
        # 已存在：按 tag 查询
        status2, data2 = api("GET", "%s/repos/%s/releases/tags/%s" % (API, repo, tag),
                             token)
        if status2 == 200:
            print("[i] release 已存在，复用: %s" % data2.get("html_url"))
            return data2
    print("[x] 创建 release 失败 HTTP %s: %s" % (status, data.get("error", "")))
    return None


def upload_asset(release, path, token):
    upload_url = (release.get("upload_url") or "").split("{")[0]
    if not upload_url:
        print("[x] release 响应缺少 upload_url")
        return False
    name = os.path.basename(path)
    # 已存在同名资产时先删除（幂等重跑）
    status, assets = api("GET", "%s/repos/%s/releases/%s/assets"
                         % (API, release["_repo"], release["id"]), token)
    if status == 200:
        for asset in assets if isinstance(assets, list) else []:
            if asset.get("name") == name:
                api("DELETE", "%s/repos/%s/releases/assets/%s"
                    % (API, release["_repo"], asset["id"]), token)
    with open(path, "rb") as f:
        payload = f.read()
    status, data = api("POST", upload_url + "?name=" + urllib.parse.quote(name),
                       token, raw=payload,
                       content_type="application/octet-stream")
    if status in (200, 201):
        print("[+] 已上传: %s (%d bytes)" % (name, len(payload)))
        return True
    print("[x] 上传 %s 失败 HTTP %s: %s" % (name, status, data.get("error", "")))
    return False


def main():
    ap = argparse.ArgumentParser(description="发布 MCP Fleet Release")
    ap.add_argument("--repo", default="guoxpeng/skill-mcp-fleet")
    ap.add_argument("--tag", default="")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--notes", default="")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    version = local_version()
    tag = args.tag or ("v" + version)
    zip_path, sums_path, missing = find_assets(args.dist, version)
    if missing:
        print("[x] 缺少发布资产：%s" % ", ".join(missing))
        print("    先执行: python3 scripts/build_package.py")
        return 2

    notes = args.notes or ("MCP Fleet %s · 标准 Skill + 六层安全 + 全平台安装 + Agent 自对话\n\n"
                           "详见 CHANGELOG.md" % version)
    print("[i] 仓库: %s" % args.repo)
    print("[i] tag : %s" % tag)
    print("[i] 资产: %s, %s" % (os.path.basename(zip_path),
                                os.path.basename(sums_path)))
    if args.dry_run:
        print("[i] --dry-run：不创建 release、不上传。")
        print("[i] 将创建的 release: %s (draft=%s prerelease=%s)"
              % (tag, args.draft, args.prerelease))
        return 0

    user, token = credential()
    if not token:
        print("[x] 未找到 GitHub 凭据：请设置 GITHUB_TOKEN，或先 git push 保存 HTTPS 凭据")
        return 2
    print("[i] 凭据用户: %s（token 不显示）" % user)

    release = create_or_get_release(args.repo, tag, token, "MCP Fleet %s" % version,
                                    notes, args.draft, args.prerelease)
    if not release:
        return 1
    release["_repo"] = args.repo
    ok = True
    for path in (zip_path, sums_path):
        ok = upload_asset(release, path, token) and ok
    if ok:
        print("[+] Release 完成: %s" % release.get("html_url", ""))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
