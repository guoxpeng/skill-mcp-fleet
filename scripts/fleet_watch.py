#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fleet 节点健康看门：probe 所有已注册节点，只在状态变化时输出。
供 cron 定时调用，无变化则静默。

用法：python3 fleet_watch.py [--agent workbuddy]

【部署说明】本文件是仓库内独立运行版，与 goal 版看门
（~/workspace/goals/fleet-remote-access/hidden_files/fleet_watch.py）
逻辑同步（8/20 秒超时、三处异常捕获、原子写、flock、ping 腿、恢复复测），
但状态文件独立，避免两个看门互相覆盖状态：
- 本版默认状态文件：~/.mcp-fleet/watch_state_standalone.json
  （环境变量 FLEET_WATCH_STATE 可覆盖）
- 生产 cron 跑的是 goal 版，不是本文件；不要两个同时跑同一个状态文件。
状态文件：~/.mcp-fleet/watch_state_standalone.json

两级口径（P2-10，2026-10-27）：
- “可达”：probe（GET /）通 —— 只说明 HTTP 层有响应。注意有些 agent 的
  GET / 不验权，令牌不对也照样返回 server/version，所以可达 ≠ 可用。
- “可用”：可达 + 一次带鉴权 exec 成功（fleet.py call <name> --tool exec）。
  只有“可用”才算恢复；仅可达（鉴权 exec 失败）不算恢复。
- 状态文件里的 ok 现在就是“可用”。旧状态文件里的 ok 是 probe 级，
  升级后首轮会对“仅可达”的节点报一次掉线（口径校准，不是新故障）。

通知口径不变：对外只报“管理通道连不上/恢复”，绝不说设备掉线/复活；
本脚本输出里的“掉线/恢复”只是内部变化行，由调用方按口径转述。

为避免 cloudflared 隧道短暂抖动造成"掉线"误报（2026-10-07 曾两次误报 vps 掉线，
随后复测均正常），疑似掉线（此前可用、本轮检查失败）时会在等待后复测一次，
只有连续两次失败才记为掉线。

已知 caveat：看门跑在后台任务里时，到 tailnet 节点的 POST 曾被运行时网络
策略拦截（脚本里原有注释）；若该拦截仍在，tailnet 节点会恒报“仅可达”，
此时应先确认 POST 通路，而不是直接认定鉴权坏了。

第三轮改动（2026-10-27）：
- L-05：入口加 flock（已有实例在跑则本轮静默退出）；save_state 原子写。
- L-18：probe 腿改调主控 ping（GET-only，不握手不 exec）；tailnet POST
  拦截不再影响 probe 腿（旧的 tailnet 特殊分支随之删除）。
- L-06：ping 子进程超时不直接判不可达，仍跑 exec 腿定性（401/403
  证明 HTTP 通 → 仅可达；连接类失败 → 不可达）。
- 恢复侧确认：此前不可用、本轮可用时立即再查一次，两次可用才报恢复，
  避免 POST 间歇通断时恢复/掉线来回翻。
"""
import json
import os
import subprocess
import sys
import time

STATE_FILE = os.environ.get("FLEET_WATCH_STATE") or os.path.expanduser("~/.mcp-fleet/watch_state_standalone.json")
LOCK_FILE = os.environ.get("FLEET_WATCH_LOCK") or os.path.expanduser("~/.mcp-fleet/watch.lock")
# 仓库版：fleet.py 就在同目录（goal 版用的是沙盒绝对路径，不要照搬）。
FLEET = os.environ.get("FLEET_WATCH_FLEET") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.py")

# 疑似掉线后等待多久再复测（秒）；复测仍失败才上报
CONFIRM_WAIT_SECS = 20


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    # L-05（与 S5 同一改动）：临时文件 + fsync + os.replace，避免半截状态文件。
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def list_nodes(agent):
    # 崩溃加固（与 probe/exec 同类问题）：list 子进程超时或异常也不应掐断
    # 整轮；失败返回空表，main 会打印“没有已注册节点”并正常退出（状态文件
    # 保持上一轮，不会被损坏）。
    try:
        r = subprocess.run([sys.executable, FLEET, "list", "--agent", agent, "--json"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return {}
    try:
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def probe_node(name, url, agent):
    """L-18：probe 腿改调主控 ping（GET-only，不握手不 exec）。

    返回 (ok: bool, timed_out: bool, detail: str)：ping 退出 0 = 可达；
    ping 子进程超时 → timed_out=True（L-06：不直接判不可达，由 check_node
    跑 exec 腿定性）。旧的 tailnet 特殊分支随之删除——ping 本来就是 GET，
    tailnet POST 拦截不再影响 probe 腿。超时/异常只算该节点本次失败，
    绝不掐断整轮（2026-10-27 11:53/11:54 的生产崩溃教训）。
    """
    try:
        r = subprocess.run([sys.executable, FLEET, "ping", name,
                            "--agent", agent, "--no-sync", "--timeout", "8"],
                           capture_output=True, text=True, timeout=8)
        out = (r.stdout or "") + (r.stderr or "")
        detail = ""
        for line in out.splitlines():
            if "可达" in line or "不可达" in line or "失败" in line:
                detail = line.strip()[:100]
                break
        return (r.returncode == 0), False, detail
    except subprocess.TimeoutExpired:
        return False, True, "ping 超时（8s）"
    except Exception as e:
        return False, False, "ping 异常：%s" % e


def exec_trial(name, agent):
    """带鉴权 exec 试调：返回 (ok: bool, detail: str)。

    用 fleet.py call 调 exec 工具（会走完整握手 + tools/call，令牌不对就
    401）。只认“返回码 0 且输出里有标记”才算成功；超时、401、其他报错
    一律算失败。--no-sync：看门只做判定，不顺手改登记。
    """
    # 与 probe_node 同理：超时/异常只算本次试调失败，绝不掐断整轮。
    # 可用性未被证明时按失败计（fail closed），不认残留输出。
    try:
        r = subprocess.run([sys.executable, FLEET, "call", name,
                            "--agent", agent, "--no-sync",
                            "--tool", "exec",
                            "--args", '{"command": "echo FLEET_WATCH_OK"}'],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "鉴权 exec 超时（20s）"
    except Exception as e:
        return False, "鉴权 exec 异常：%s" % e
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "FLEET_WATCH_OK" in (r.stdout or ""):
        return True, "鉴权 exec 成功"
    detail = ""
    for line in out.splitlines():
        if "失败" in line or "401" in line or "403" in line or "HTTP" in line:
            detail = line.strip()[:100]
            break
    return False, detail or "鉴权 exec 失败"


def check_node(name, url, agent):
    """两级检查。返回 (usable: bool, reachable: bool, detail: str)。

    ping 通 → 再做 exec 试调，试调成功才算可用；
    ping 明确失败 → 不可达，不跑 exec；
    ping 超时 → L-06：跑 exec 腿定性——exec 成功 = 可用；exec 报 401/403
    = 仅可达（HTTP 通、鉴权未过）；其他 exec 失败 = 不可达。
    """
    ok, timed_out, detail = probe_node(name, url, agent)
    if ok:
        tok, tdetail = exec_trial(name, agent)
        if tok:
            return True, True, detail or "可用"
        return False, True, "仅可达，" + (tdetail or "鉴权 exec 失败")
    if not timed_out:
        return False, False, detail or "ping 失败"
    tok, tdetail = exec_trial(name, agent)
    if tok:
        return True, True, "ping 超时但鉴权 exec 成功"
    if "401" in (tdetail or "") or "403" in (tdetail or ""):
        return False, True, "仅可达，ping 超时（exec 401/403 证明 HTTP 通）"
    return False, False, "ping 超时且 " + (tdetail or "鉴权 exec 失败")


def check_node_confirmed(name, url, agent, was):
    """检查一个节点。was：True（此前可用）/ False（此前不可用）/ None（新节点）。

    返回 (usable, reachable, detail)。
    可用→疑似掉线：等 20 秒复测一次，两次失败才记掉线（原有逻辑）。
    不可用→疑似恢复：立即再查一次，两次可用才报恢复（L-05 恢复侧确认，
    避免 POST 间歇通断时恢复/掉线来回翻；新节点首见不复测）。
    """
    usable, reachable, detail = check_node(name, url, agent)
    if was is True and not usable:
        time.sleep(CONFIRM_WAIT_SECS)
        usable2, reachable2, detail2 = check_node(name, url, agent)
        if usable2:
            return True, True, (detail2 or "复测正常") + "（首次检查疑似隧道抖动）"
        return False, reachable2, (detail2 or detail or "检查失败") + "（连续两次检查失败）"
    if was is False and usable:
        usable2, reachable2, detail2 = check_node(name, url, agent)
        if usable2:
            return True, True, detail2 or detail
        return False, reachable2, detail2 or detail
    return usable, reachable, detail


def main():
    # L-05：看门超周期无锁——已有实例在跑则本轮静默退出，不重叠扫、不写坏状态。
    try:
        import fcntl
        _lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
    except Exception:
        pass  # 拿不到锁文件也不阻塞看门（锁只是防重叠，不是必选项）
    agent = "workbuddy"
    if "--agent" in sys.argv:
        agent = sys.argv[sys.argv.index("--agent") + 1]
    nodes = list_nodes(agent)
    if not nodes:
        print("WATCH: 没有已注册节点")
        return 0
    prev = load_state()
    cur, changes = {}, []
    for name in sorted(nodes):
        was = prev.get(name, {}).get("ok")
        info = nodes[name] or {}
        url = info.get("url") if isinstance(info, dict) else info
        usable, reachable, detail = check_node_confirmed(name, url, agent, was)
        cur[name] = {"ok": usable, "reachable": reachable, "detail": detail}
        if was is None:
            changes.append("新节点 %s：%s" % (
                name, "可用" if usable else ("仅可达" if reachable else "不在线")))
        elif was and not usable:
            changes.append("掉线 %s：%s" % (name, detail or "检查失败"))
        elif not was and usable:
            changes.append("恢复 %s：可用" % name)
    # 注册表中消失的节点
    for name in sorted(set(prev) - set(cur)):
        changes.append("注销 %s：已从注册表移除" % name)
    save_state(cur)
    if changes:
        print("WATCH 变化：")
        for c in changes:
            print("  - " + c)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
