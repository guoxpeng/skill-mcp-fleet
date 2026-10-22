# -*- coding: utf-8 -*-
"""MCP Fleet v5.2 Agent 智能层（内置提示词 + 自对话协议 + 确定性编排器）。

设计目标
--------
让接入任意 MCP 客户端（Claude Desktop / Cursor / VSCode / 自研 agent）的模型，
无需额外系统提示，就能按「观察 → 计划 → 执行 → 验证 → 反思」的固定套路安全地
操作 fleet mesh：

* :func:`build_instructions` 生成 MCP ``initialize`` 的 instructions（Markdown，
  上限 6000 字符）：节点定位、工具选择矩阵、安全边界、四步自对话法、跨设备委派、
  失败升级、常见任务模板。
* :data:`PROMPTS` / :func:`list_prompts` / :func:`get_prompt` 提供 MCP prompts
  规范结构，覆盖 playbook / 诊断 / 规划 / 自审 / 委派 / 事故 / 入网 七个场景。
* :data:`SELF_DIALOGUE_TOOLS` / :data:`SELF_DIALOGUE_FUNCS` 提供六个自对话工具：
  ``fleet_help`` / ``fleet_think`` / ``fleet_plan`` / ``fleet_reflect`` /
  ``fleet_review`` / ``fleet_handoff``。``think`` / ``reflect`` 把思考写进
  TaskStore 的 JSONL 任务日志（0600），``plan`` 强制每步具备
  ``goal/tool/assignee/acceptance``，``review`` 对验收标准逐条判定。
* :class:`TaskStore` 是 0600/0700、原子写、线程安全的极简任务存储。
* :func:`make_envelope` / :func:`parse_envelope` / :func:`validate_envelope`
  定义 agent 间结构化消息信封（与邮箱 ``text`` 字段配合，普通文本仍兼容）。
* :class:`FleetClient` 是协议无关的薄封装，集成方注入 ``call_tool(name, args)``。
* :class:`Orchestrator` 是确定性的「观察→计划→执行→验证→反思」循环：能力路由、
  步数/时间预算、失败重试与升级、策略（allow/ask/deny）前置检查、结构化轨迹。
* :class:`LLMAdapter` 可选（OpenAI 兼容 ``/chat/completions``，纯 urllib，超时/
  重试/预算上限）；LLM 不可用时编排器自动降级为确定性规划。

约束
----
* 纯标准库，Python 3.6+ 语法兼容（不使用 dataclasses / walrus / removeprefix /
  ``asyncio.run``）。
* **不 import** ``mcp_agent`` / ``fleet_proto`` / ``fleet``，避免循环依赖；集成方
  从外部注入 ``call_tool`` / ``policy``。
* 所有敏感落盘文件 0600、目录 0700，写文件原子（tmp + fsync + ``os.replace``）。
* 不在日志/异常文本中泄露 token、私钥、邀请码（见 :func:`redact`）。

与 MCP 的接线方式（由集成方在 ``mcp_agent.py`` 中完成）::

    from fleet_agent import (build_instructions, list_prompts, get_prompt,
                             SELF_DIALOGUE_TOOLS, dispatch_tool, TaskStore)

    # initialize 结果
    {"instructions": build_instructions({"name": SERVER_NAME, "version": ...})}
    # prompts/list -> {"prompts": list_prompts()}
    # prompts/get  -> get_prompt(name, arguments)
    # 工具表追加 SELF_DIALOGUE_TOOLS；分发时调用
    #   dispatch_tool(name, args, {"store": _store, "client": _client, "policy": _policy})
"""

import io
import json
import os
import re
import threading
import time
import uuid

try:  # Python 3.6+ 均有 urllib.request；显式兜底便于极简环境
    import urllib.error
    import urllib.request
except ImportError:  # pragma: no cover - 正常 CPython 不会走到
    urllib = None

VERSION = "5.2.5"

# MCP initialize instructions 硬上限（契约要求 ≤ 6000 字符）
MAX_INSTRUCTIONS = 6000

#: 任务日志 / 思考记录等敏感文件权限
FILE_MODE = 0o600
#: 任务目录权限
DIR_MODE = 0o700

#: 结构化信封允许的类型
ENVELOPE_KINDS = ("propose", "accept", "counter", "progress", "result", "fail",
                  "review", "handoff", "hello")

#: 节点/agent 名允许的字符（与 mcp_agent 的 _NAME_RE 保持一致）
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
#: task_id / thread_id 允许的字符
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# 脱敏规则：token / password / api_key / Bearer / sk- 密钥 / 邀请码
_SECRET_PATTERNS = [
    (re.compile(r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|passwd|"
                r"secret|api[_-]?key|authorization)\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\s,;\"'}]+)"), r"\1***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{4,}"), "Bearer ***"),
    (re.compile(r"sk-[A-Za-z0-9._\-]{8,}"), "sk-***"),
    (re.compile(r"fleet://join/[A-Za-z0-9_\-+/=]+"), "fleet://join/***"),
    (re.compile(r"enc2\$[A-Za-z0-9+/=]+"), "enc2$***"),
]

_tmp_lock = threading.Lock()
_tmp_counter = [0]


# ===========================================================================
# 一、基础工具：时间、脱敏、原子写、JSONL
# ===========================================================================

def now_ms():
    """返回当前 Unix 毫秒时间戳（int）。"""
    return int(time.time() * 1000)


def redact(text):
    """对文本中的 token/密码/私钥/邀请码做脱敏，避免写进日志或异常。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _truncate(text, limit):
    """截断文本到 ``limit`` 字符，超出部分加省略号。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def _ensure_dir(path, mode=DIR_MODE):
    """确保目录存在；新建目录强制 0700（已存在则不改权限，避免破坏用户设置）。"""
    try:
        os.makedirs(path, mode)
    except OSError:
        if not os.path.isdir(path):
            raise
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _fsync_dir(path):
    """尽力 fsync 目录项，保证 rename 落盘（部分平台不支持则忽略）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path, data, mode=FILE_MODE):
    """原子写文本文件：同目录 tmp + fsync + ``os.replace`` + chmod 0600。

    任何异常都会清理临时文件；写入成功后目录项也尽力 fsync。
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    _ensure_dir(directory)
    if isinstance(data, bytes):
        payload = data
    else:
        payload = str(data).encode("utf-8")
    with _tmp_lock:
        _tmp_counter[0] += 1
        seq = _tmp_counter[0]
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), seq)
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_dir(directory)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def append_jsonl(path, obj, mode=FILE_MODE):
    """向 JSONL 追加一行（带 fsync）。单行一次 O_APPEND 写入，多进程并发可接受。

    行内容经过 ``json.dumps(sort_keys=True)``，保证同结构事件可稳定比较。
    """
    path = os.path.abspath(path)
    _ensure_dir(os.path.dirname(path) or ".")
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    data = line.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise IOError("写入 JSONL 失败: %s" % path)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def read_jsonl(path, limit=0):
    """读取 JSONL 文件；跳过损坏行。``limit>0`` 时只返回最后 N 条。"""
    items = []
    if not path or not os.path.exists(path):
        return items
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except (OSError, IOError):
        return items
    if limit and limit > 0:
        return items[-int(limit):]
    return items


def _json_safe(obj):
    """把对象转成可 JSON 序列化的结构（失败则退化为字符串）。"""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return redact(str(obj))


# ===========================================================================
# 二、TaskStore：任务 + 事件日志（原子写、并发安全、0600/0700）
# ===========================================================================

class TaskStore(object):
    """极简任务存储。

    落盘结构::

        <base_dir>/                      # 0700
          tasks/                         # 0700
            <task_id>.json               # 0600，任务快照（原子替换）
            <task_id>.events.jsonl       # 0600，追加式事件日志（think/plan/step/...）

    使用线程锁保证同一进程内 create/append 的读-改-写不互相踩；JSONL 追加使用
    单次 ``O_APPEND`` 写入 + fsync，跨进程并发追加不会互相覆盖行。
    """

    def __init__(self, base_dir):
        if not base_dir or not isinstance(base_dir, str):
            raise ValueError("TaskStore 需要非空 base_dir")
        self.base_dir = os.path.abspath(base_dir)
        self.tasks_dir = os.path.join(self.base_dir, "tasks")
        self._lock = threading.RLock()
        _ensure_dir(self.base_dir, DIR_MODE)
        _ensure_dir(self.tasks_dir, DIR_MODE)

    # -- 路径/校验 --
    @staticmethod
    def new_task_id():
        """生成任务 ID：``t-<UTC时间>-<8位随机>``，便于排序且避免碰撞。"""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return "t-%s-%s" % (stamp, uuid.uuid4().hex[:8])

    @staticmethod
    def check_id(task_id):
        """校验 task_id 合法性，阻断 ``../`` 等路径穿越。"""
        if not isinstance(task_id, str) or not _ID_RE.match(task_id):
            raise ValueError(
                "非法 task_id %r：只允许 1-64 位字母/数字/_.-（不能含路径分隔符）"
                % (task_id,))
        if ".." in task_id:
            raise ValueError("非法 task_id %r：不允许包含 .." % (task_id,))
        return task_id

    def _task_path(self, task_id):
        self.check_id(task_id)
        return os.path.join(self.tasks_dir, task_id + ".json")

    def _events_path(self, task_id):
        self.check_id(task_id)
        return os.path.join(self.tasks_dir, task_id + ".events.jsonl")

    def _write_task(self, task):
        atomic_write(self._task_path(task["task_id"]),
                     json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True),
                     FILE_MODE)

    # -- CRUD --
    def create_task(self, task):
        """创建任务；``task_id`` 缺省自动生成。重复 ID 抛 ValueError。

        返回落盘后的任务副本（含 task_id/status/created_ms/updated_ms/event_count）。
        """
        if not isinstance(task, dict):
            raise ValueError("create_task 需要 dict 参数")
        record = dict(task)
        task_id = record.get("task_id") or self.new_task_id()
        self.check_id(task_id)
        with self._lock:
            path = self._task_path(task_id)
            if os.path.exists(path):
                raise ValueError("任务已存在: %s" % task_id)
            ts = now_ms()
            record["task_id"] = task_id
            record.setdefault("status", "open")
            record.setdefault("goal", record.get("objective") or "")
            record.setdefault("created_ms", ts)
            record["updated_ms"] = ts
            record.setdefault("event_count", 0)
            record.setdefault("origin", "api")
            self._write_task(record)
            self._append_event_locked(task_id, {
                "type": "created", "goal": record.get("goal", ""),
                "status": record.get("status", "open"), "at_ms": ts})
            return _json_safe(record)

    def get_task(self, task_id):
        """读取任务快照；不存在抛 KeyError（错误信息含 task_id）。"""
        self.check_id(task_id)
        path = self._task_path(task_id)
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, IOError):
            raise KeyError("任务不存在: %s" % task_id)
        except ValueError:
            raise ValueError("任务文件损坏: %s" % path)
        if not isinstance(data, dict):
            raise ValueError("任务文件格式错误: %s" % path)
        return data

    def has_task(self, task_id):
        """判断任务是否存在（不抛异常）。"""
        try:
            self.check_id(task_id)
        except ValueError:
            return False
        return os.path.exists(self._task_path(task_id))

    def update_task(self, task_id, patch):
        """合并更新任务字段并原子落盘；返回更新后的任务副本。"""
        if not isinstance(patch, dict):
            raise ValueError("update_task 需要 dict patch")
        with self._lock:
            task = self.get_task(task_id)
            task.update(patch)
            task["task_id"] = task_id
            task["updated_ms"] = now_ms()
            self._write_task(task)
            return _json_safe(task)

    def _append_event_locked(self, task_id, event):
        """（持锁调用）追加事件并同步事件的计数/更新时间，失败不阻塞主流程。"""
        ev = dict(event or {})
        ev.setdefault("at_ms", now_ms())
        ev["task_id"] = task_id
        append_jsonl(self._events_path(task_id), ev, FILE_MODE)
        # 事件计数是观测元数据，尽力更新；并发下允许多算/少算 1
        try:
            task = self.get_task(task_id)
            task["event_count"] = int(task.get("event_count") or 0) + 1
            task["updated_ms"] = ev["at_ms"]
            self._write_task(task)
        except (KeyError, ValueError, OSError):
            pass

    def append_event(self, task_id, event):
        """向任务事件日志追加一条事件（任务必须存在）。"""
        if not isinstance(event, dict):
            raise ValueError("append_event 需要 dict event")
        with self._lock:
            if not self.has_task(task_id):
                raise KeyError("任务不存在: %s" % task_id)
            self._append_event_locked(task_id, event)

    def read_events(self, task_id, limit=0):
        """读取任务事件日志（按写入顺序；``limit>0`` 取最后 N 条）。"""
        self.check_id(task_id)
        return read_jsonl(self._events_path(task_id), limit=limit)

    def list_tasks(self, status=None):
        """列出任务（可按 status 过滤），按 created_ms、task_id 升序。"""
        tasks = []
        try:
            names = os.listdir(self.tasks_dir)
        except OSError:
            return tasks
        for name in names:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            path = os.path.join(self.tasks_dir, name)
            try:
                with io.open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, IOError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if status is not None and data.get("status") != status:
                continue
            tasks.append(data)
        tasks.sort(key=lambda t: (t.get("created_ms") or 0, t.get("task_id") or ""))
        return tasks


# ===========================================================================
# 三、结构化消息信封（与邮箱 text 字段配合）
# ===========================================================================

def _valid_agent_name(value, allow_broadcast=False):
    if not isinstance(value, str) or not value:
        return False
    if allow_broadcast and value == "*":
        return True
    return bool(_NAME_RE.match(value))


def make_envelope(kind, thread_id, from_, to, payload, task_id=None,
                  deadline_s=None):
    """构造结构化信封。

    :param kind: 必须属于 :data:`ENVELOPE_KINDS`
    :param thread_id: 会话线程 ID；为空时自动生成 ``th-<8位hex>``
    :param from_: 发送方 agent/节点名
    :param to: 接收方 agent/节点名（``*`` 表示广播）
    :param payload: dict，消息正文（结构化字段）
    :param task_id: 可选关联任务
    :param deadline_s: 可选相对超时（秒），落盘为绝对 ``deadline_ms``
    :raises ValueError: 参数非法（错误信息可直接展示给模型）

    生成的 dict 可直接 ``json.dumps`` 后放进邮件的 ``text`` 字段。
    """
    if not isinstance(kind, str) or kind not in ENVELOPE_KINDS:
        raise ValueError("非法信封 kind %r：必须是 %s 之一"
                         % (kind, "/".join(ENVELOPE_KINDS)))
    if not thread_id:
        thread_id = "th-" + uuid.uuid4().hex[:8]
    if not isinstance(thread_id, str) or not _ID_RE.match(thread_id):
        raise ValueError("非法 thread_id %r：只允许 1-64 位字母/数字/_.-"
                         % (thread_id,))
    if not _valid_agent_name(from_):
        raise ValueError("非法发送方 from %r：只允许 1-32 位字母/数字/_.-"
                         % (from_,))
    if not _valid_agent_name(to, allow_broadcast=True):
        raise ValueError("非法接收方 to %r：只允许 1-32 位字母/数字/_.- 或 *"
                         % (to,))
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict（结构化正文），收到 %s"
                         % type(payload).__name__)
    env = {
        "v": VERSION,
        "kind": kind,
        "thread_id": thread_id,
        "from": from_,
        "to": to,
        "payload": payload,
        "created_ms": now_ms(),
    }
    if task_id:
        if not isinstance(task_id, str) or not _ID_RE.match(task_id) or ".." in task_id:
            raise ValueError("非法 task_id %r：只允许 1-64 位字母/数字/_.-"
                             % (task_id,))
        env["task_id"] = task_id
    if deadline_s is not None:
        try:
            seconds = int(deadline_s)
        except (TypeError, ValueError):
            raise ValueError("deadline_s 必须是整数秒，收到 %r" % (deadline_s,))
        if seconds <= 0:
            raise ValueError("deadline_s 必须 > 0，收到 %d" % seconds)
        env["deadline_ms"] = env["created_ms"] + seconds * 1000
        env["deadline_s"] = seconds
    ok, err = validate_envelope(env)
    if not ok:
        raise ValueError(err)
    return env


def validate_envelope(env):
    """校验信封结构，返回 ``(ok, error)``；error 为可直接展示的中文说明。"""
    if not isinstance(env, dict):
        return False, "信封必须是 JSON 对象（dict），收到 %s" % type(env).__name__
    version = env.get("v")
    if not isinstance(version, str) or not version:
        return False, "缺少协议版本字段 v（应为 \"%s\"）" % VERSION
    head = version.split(".")[0]
    if not head.isdigit() or int(head) < 5:
        return False, "协议版本不支持: %r（本节点要求主版本 >= 5，当前 %s）" % (
            version, VERSION)
    kind = env.get("kind")
    if not isinstance(kind, str) or not kind:
        return False, "缺少 kind 字段（应为 %s 之一）" % "/".join(ENVELOPE_KINDS)
    if kind not in ENVELOPE_KINDS:
        return False, "非法信封 kind %r：必须是 %s 之一" % (
            kind, "/".join(ENVELOPE_KINDS))
    thread_id = env.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return False, "缺少 thread_id（同一会话线程的消息必须共享同一 thread_id）"
    if not _ID_RE.match(thread_id):
        return False, "非法 thread_id %r：只允许 1-64 位字母/数字/_.-" % (thread_id,)
    from_ = env.get("from")
    if not _valid_agent_name(from_):
        return False, "非法 from %r：只允许 1-32 位字母/数字/_.-" % (from_,)
    to = env.get("to")
    if not _valid_agent_name(to, allow_broadcast=True):
        return False, "非法 to %r：只允许 1-32 位字母/数字/_.- 或 *" % (to,)
    if not isinstance(env.get("payload"), dict):
        return False, "payload 必须是 dict（结构化正文）"
    task_id = env.get("task_id")
    if task_id is not None:
        if (not isinstance(task_id, str) or not _ID_RE.match(task_id)
                or ".." in task_id):
            return False, "非法 task_id %r：只允许 1-64 位字母/数字/_.-" % (task_id,)
    deadline = env.get("deadline_ms")
    if deadline is not None:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return False, "deadline_ms 必须是数字（Unix 毫秒时间戳）"
        if deadline <= 0:
            return False, "deadline_ms 必须 > 0"
    return True, None


def envelope_expired(env, at_ms=None):
    """判断信封是否已过 deadline（无 deadline 视为不过期）。"""
    if not isinstance(env, dict):
        return False
    deadline = env.get("deadline_ms")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return False
    current = now_ms() if at_ms is None else at_ms
    return current > deadline


def _looks_like_envelope(obj):
    if not isinstance(obj, dict):
        return False
    if obj.get("kind") in ENVELOPE_KINDS:
        return True
    for key in ("thread_id", "payload", "deadline_ms", "task_id", "v"):
        if key in obj:
            return True
    return False


def parse_envelope(text):
    """解析邮箱 ``text`` 为信封或普通文本。

    返回：

    * 合法信封 → 原信封 dict（``kind`` 为 propose/...）；
    * 普通文本（含非信封 JSON）→ ``{"kind": "text", "text": 原文, "raw": 原文}``；
    * 看起来是信封但非法 → ``{"kind": "invalid", "error": 原因, "raw": 原文}``。

    这样旧版纯文本消息、JSON 工具输出都不会被误判成协议错误。
    """
    raw = text
    if isinstance(text, dict):
        ok, err = validate_envelope(text)
        if ok:
            out = dict(text)
            return out
        return {"kind": "invalid", "error": err, "raw": _json_safe(text)}
    if text is None:
        return {"kind": "text", "text": "", "raw": ""}
    if not isinstance(text, str):
        text = str(text)
    stripped = text.strip()
    if not stripped:
        return {"kind": "text", "text": text, "raw": text}
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if _looks_like_envelope(obj):
            ok, err = validate_envelope(obj)
            if ok:
                return obj
            return {"kind": "invalid", "error": err, "raw": text}
    return {"kind": "text", "text": text, "raw": text}


def encode_envelope(env):
    """把信封序列化为可放进邮件 text 的单行 JSON（UTF-8、字段排序）。"""
    ok, err = validate_envelope(env)
    if not ok:
        raise ValueError(err)
    return json.dumps(env, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


# ===========================================================================
# 四、工具结果归一化 + FleetClient
# ===========================================================================

def _content_text(value):
    """从 MCP content 列表里抽取可读文本。"""
    parts = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
    return "\n".join(parts)


_ERROR_PREFIXES = ("错误", "失败", "失败：", "error", "Error", "ERROR",
                   "[error]", "denied", "拒绝")


def normalize_tool_result(value, tool=None, node=None):
    """把各种工具返回值归一化为 ``{"ok","text","raw","tool","node"}``。

    兼容四类返回：MCP ``content`` 结构 / ``(text, is_error)`` 元组 /
    fleet 工具常见的 ``(text, is_error)`` / 普通字符串。
    """
    raw = value
    ok = True
    text = ""
    if isinstance(value, dict):
        if "ok" in value and "text" in value and "raw" in value:
            out = dict(value)
            out.setdefault("tool", tool)
            out.setdefault("node", node)
            return out
        if "isError" in value:
            ok = not bool(value["isError"])
            text = _content_text(value.get("content")) or value.get("text") or ""
            if not text:
                text = json.dumps(value, ensure_ascii=False)
        elif "ok" in value:
            ok = bool(value["ok"])
            text = value.get("text") or value.get("detail") or ""
            if not text:
                text = json.dumps({k: v for k, v in value.items()
                                   if k not in ("raw", "ok")}, ensure_ascii=False)
        elif "error" in value and value.get("error"):
            ok = False
            text = str(value.get("error"))
        else:
            text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, (tuple, list)) and len(value) >= 2 \
            and isinstance(value[1], bool):
        text = value[0] if isinstance(value[0], str) else str(value[0])
        ok = not value[1]
    elif isinstance(value, str):
        text = value
        stripped = value.strip()
        for prefix in _ERROR_PREFIXES:
            if stripped.startswith(prefix):
                ok = False
                break
    else:
        text = "" if value is None else str(value)
    text = redact(_truncate(text, 4000))
    return {"ok": bool(ok), "text": text, "raw": raw, "tool": tool, "node": node}


def _is_local_node(node, self_name="agent"):
    """判断 assignee 是否表示本机。"""
    if node is None:
        return True
    if not isinstance(node, str):
        return False
    name = node.strip().lower()
    return name in ("", "local", "self", "localhost", "本机", "me",
                    (self_name or "").lower())


class FleetClient(object):
    """协议无关的 fleet 薄封装。

    集成方注入 ``call_tool(name, args)`` 可调用对象（例如 mcp_agent 的工具分发表），
    本类只负责参数拼装、本地/远端路由、结果归一化与脱敏。

    所有公开方法返回 :func:`normalize_tool_result` 结构：
    ``{"ok": bool, "text": str, "raw": ..., "tool": str, "node": str}``。
    """

    def __init__(self, call_tool, name="agent", timeout=60, policy=None):
        if not callable(call_tool):
            raise TypeError("FleetClient 需要可调用的 call_tool(name, args)")
        self.call_tool = call_tool
        self.name = name or "agent"
        self.timeout = int(timeout or 60)
        self.policy = policy

    # -- 内部 --
    def _invoke(self, tool, args):
        try:
            return self.call_tool(tool, args or {})
        except Exception as exc:  # 统一转成脱敏后的 RuntimeError
            raise RuntimeError("调用工具 %s 失败: %s"
                               % (tool, redact(str(exc))))

    def raw(self, tool, args=None):
        """直接调用工具并返回原始值（不做归一化，供集成方使用）。"""
        return self._invoke(tool, args)

    # -- 只读查询 --
    def caps(self):
        """查询本节点能力清单（``caps`` 工具）。"""
        return normalize_tool_result(self._invoke("caps", {}), "caps", "local")

    def peers(self, check=False):
        """列出对等节点；``check=True`` 时顺带探活。"""
        return normalize_tool_result(self._invoke("peers", {"check": bool(check)}),
                                     "peers", "local")

    def mesh_caps(self):
        """查询整个 mesh 的能力清单。"""
        return normalize_tool_result(self._invoke("mesh_caps", {}),
                                     "mesh_caps", "local")

    # -- 执行 --
    def exec_on(self, node, cmd, timeout=None):
        """在指定节点执行 shell 命令；``node`` 为本机时直接走 ``exec``。"""
        timeout = int(timeout or self.timeout)
        if _is_local_node(node, self.name):
            value = self._invoke("exec", {"command": cmd,
                                          "timeoutMs": timeout * 1000})
            return normalize_tool_result(value, "exec", "local")
        args = {"peer": node, "tool": "exec",
                "arguments": {"command": cmd, "timeoutMs": timeout * 1000},
                "timeoutMs": (timeout + 5) * 1000}
        return normalize_tool_result(self._invoke("peer_call", args),
                                     "exec", node)

    def call(self, node, tool, args=None):
        """在任意节点调用任意工具（本机直调，远端经 ``peer_call`` 一跳）。"""
        args = dict(args or {})
        if _is_local_node(node, self.name):
            value = self._invoke(tool, args)
            return normalize_tool_result(value, tool, "local")
        value = self._invoke("peer_call", {"peer": node, "tool": tool,
                                           "arguments": args})
        return normalize_tool_result(value, tool, node)

    # -- 邮箱 --
    def send_msg(self, to, text, topic=None):
        """发送邮箱消息（``msg_send``），默认 from 为本 client 名字。"""
        args = {"from": self.name, "to": to, "text": text}
        if topic:
            args["topic"] = topic
        return normalize_tool_result(self._invoke("msg_send", args),
                                     "msg_send", "local")

    def recv_msg(self, me=None, wait=0):
        """收取发给 ``me`` 的新消息（``msg_recv``）。"""
        args = {"me": me or self.name, "wait": int(wait or 0)}
        return normalize_tool_result(self._invoke("msg_recv", args),
                                     "msg_recv", "local")


# ===========================================================================
# 五、LLMAdapter（可选，OpenAI 兼容；失败可降级）
# ===========================================================================

class LLMError(RuntimeError):
    """LLM 调用失败（网络/协议/预算）。消息已脱敏，可直接展示。"""


def _extract_completion(payload):
    """从 OpenAI 兼容响应中抽取 ``choices[0].message.content``。"""
    if not isinstance(payload, dict):
        raise LLMError("LLM 响应不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM 响应缺少 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("LLM 响应 choices[0] 格式错误")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMError("LLM 响应缺少 message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM 返回空内容")
    return content


def _http_post_json(url, payload, api_key="", timeout=30):
    """POST JSON 并返回解析后的响应（纯 urllib；错误信息不含密钥）。"""
    if urllib is None:  # pragma: no cover
        raise LLMError("当前环境缺少 urllib，无法调用 LLM")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", "Bearer %s" % api_key)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        try:
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        raise LLMError("LLM HTTP %s: %s" % (exc.code, redact(str(exc.reason))))
    except urllib.error.URLError as exc:
        raise LLMError("LLM 网络错误: %s" % redact(str(exc.reason)))
    except Exception as exc:  # socket timeout 等
        raise LLMError("LLM 请求失败: %s" % redact(str(exc)))
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise LLMError("LLM 返回非 JSON 文本")


class LLMAdapter(object):
    """OpenAI 兼容 ``/chat/completions`` 适配器（可选组件）。

    * 纯 ``urllib``，超时 ``timeout`` 秒；
    * 失败自动重试 ``retries`` 次（指数退避、上限 2s）；
    * ``max_calls`` 是**总尝试次数预算**，用尽后直接抛 :class:`LLMError`；
    * ``api_key`` 只用于请求头，绝不写进异常/日志（错误信息统一 :func:`redact`）。
    """

    def __init__(self, base_url, model, api_key="", timeout=30, max_calls=8,
                 retries=1, temperature=0.0):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self._api_key = api_key or ""
        self.timeout = float(timeout or 30)
        self.max_calls = max(1, int(max_calls or 1))
        self.retries = max(0, int(retries or 0))
        self.temperature = float(temperature or 0.0)
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def calls(self):
        """已消耗的尝试次数。"""
        return self._calls

    @property
    def remaining(self):
        """剩余尝试次数预算。"""
        return max(0, self.max_calls - self._calls)

    def endpoint(self):
        """返回完整请求地址。"""
        return self.base_url + "/chat/completions"

    def complete(self, messages):
        """调用聊天补全并返回纯文本；失败抛 :class:`LLMError`。"""
        if not self.base_url:
            raise LLMError("LLM 未配置 base_url，无法调用")
        if not self.model:
            raise LLMError("LLM 未配置 model，无法调用")
        if not isinstance(messages, list) or not messages:
            raise LLMError("messages 不能为空")
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "stream": False}
        last_error = "未知错误"
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            with self._lock:
                if self._calls >= self.max_calls:
                    raise LLMError("LLM 调用预算已用尽（max_calls=%d）"
                                   % self.max_calls)
                self._calls += 1
            try:
                data = _http_post_json(self.endpoint(), payload,
                                       self._api_key, self.timeout)
                return _extract_completion(data)
            except Exception as exc:  # 网络/协议/第三方 mock 的任意异常都要降级
                last_error = redact(str(exc))
                if attempt >= attempts:
                    break
                time.sleep(min(0.5 * attempt, 2.0))
        raise LLMError("LLM 调用失败（已尝试 %d 次）: %s"
                       % (attempts, last_error))


# ===========================================================================
# 六、验收判定 + 能力索引
# ===========================================================================

_PASS_WORDS = ("pass", "passed", "ok", "success", "succeeded", "done", "true",
               "通过", "成功", "已完成")
_FAIL_WORDS = ("fail", "failed", "error", "denied", "timeout", "false",
               "失败", "错误", "拒绝", "超时")
_UNKNOWN_WORDS = ("unknown", "pending", "ask", "skipped", "待确认", "未知")


def _norm_criterion(text):
    if not isinstance(text, str):
        text = str(text or "")
    return re.sub(r"[\s，。；;,.：:、（）()\[\]\"'`]+", "", text.lower())


def _criteria_list(acceptance):
    """把 acceptance 归一化为 ``[{"text": str, "id": str|None}]``。"""
    if acceptance is None:
        return []
    if isinstance(acceptance, str):
        items = [acceptance]
    elif isinstance(acceptance, dict):
        items = [acceptance]
    elif isinstance(acceptance, (list, tuple)):
        items = list(acceptance)
    else:
        items = [str(acceptance)]
    result = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("criterion") or item.get("goal") or item.get("text") \
                or item.get("id") or ""
            result.append({"text": str(text), "id": item.get("id")})
        else:
            result.append({"text": str(item), "id": None})
    return result


def _evidence_status(item):
    """从一条证据中提取 passed/failed/unknown 判定。"""
    if not isinstance(item, dict):
        return "unknown", ""
    for key in ("status", "result", "verdict", "outcome"):
        value = item.get(key)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _PASS_WORDS:
                return "passed", "证据声明 %s=%s" % (key, value)
            if low in _FAIL_WORDS:
                return "failed", "证据声明 %s=%s" % (key, value)
            if low in _UNKNOWN_WORDS:
                return "unknown", "证据声明 %s=%s" % (key, value)
    ok = item.get("ok")
    if isinstance(ok, bool):
        return ("passed", "证据 ok=true") if ok else ("failed", "证据 ok=false")
    if isinstance(item.get("passed"), bool):
        return ("passed", "证据 passed=true") if item["passed"] else \
            ("failed", "证据 passed=false")
    return "unknown", "证据存在但未给出明确判定"


def _evidence_has_verdict(item):
    if not isinstance(item, dict):
        return False
    for key in ("status", "result", "verdict", "outcome", "ok", "passed"):
        if key in item:
            return True
    return False


def _evidence_label(item, index):
    """给证据生成可读标签（用于 review 输出）。"""
    if not isinstance(item, dict):
        return "evidence[%d]=%s" % (index, _truncate(redact(str(item)), 120))
    bits = []
    for key in ("step", "tool", "node", "criterion", "detail", "text", "output"):
        value = item.get(key)
        if value not in (None, ""):
            bits.append("%s=%s" % (key, _truncate(redact(str(value)), 160)))
    if not bits:
        bits.append(_truncate(redact(json.dumps(item, ensure_ascii=False)), 160))
    return "evidence[%d]: %s" % (index, "; ".join(bits))


def review_acceptance(acceptance, evidence):
    """按验收标准逐条判定，返回 ``{"verdict","items","summary"}``。

    * 每条标准输出 ``status ∈ passed|failed|unknown`` 与证据引用；
    * 显式证据（``status/result/verdict/outcome/ok/passed``）优先；
    * 无证据 → ``unknown``（不臆断通过）；
    * 整体 ``verdict``：任一 failed → ``fail``；全部 passed → ``pass``；
      否则 ``inconclusive``。
    """
    criteria = _criteria_list(acceptance)
    evidence = list(evidence or [])
    if not criteria:
        judged = []
        if evidence:
            statuses = [_evidence_status(e)[0] for e in evidence]
            if "failed" in statuses:
                verdict = "fail"
            elif statuses and all(s == "passed" for s in statuses):
                verdict = "pass"
            else:
                verdict = "inconclusive"
        else:
            verdict = "inconclusive"
        return {"verdict": verdict, "items": [],
                "summary": "未提供验收标准，按执行结果判定为 %s" % verdict}

    # 证据先按 criterion 归类；无 criterion 的证据留作位置回退
    buckets = {}
    generic = []
    for index, item in enumerate(evidence):
        key = None
        if isinstance(item, dict):
            key = item.get("criterion") or item.get("acceptance") or item.get("goal")
        if key:
            buckets.setdefault(_norm_criterion(key), []).append((index, item))
        else:
            generic.append((index, item))

    items = []
    used_generic = set()
    for position, criterion in enumerate(criteria):
        norm = _norm_criterion(criterion["text"])
        matches = list(buckets.get(norm, []))
        if not matches:
            for key, bucket in buckets.items():
                if key and (key in norm or norm in key):
                    matches.extend(bucket)
        if not matches:
            # 语义回退：验收标准与证据的 goal/步骤目标一致（任务级验收常用）
            for ev_index, item in enumerate(evidence):
                if not isinstance(item, dict):
                    continue
                goal_text = item.get("goal")
                if not goal_text:
                    continue
                goal_norm = _norm_criterion(goal_text)
                if goal_norm and (goal_norm in norm or norm in goal_norm):
                    matches.append((ev_index, item))
        if not matches and position < len(generic):
            # 位置回退：第 i 条标准对应第 i 条无归属证据（如编排器步骤轨迹）
            candidate = generic[position]
            if candidate[0] not in used_generic:
                matches = [candidate]
                used_generic.add(candidate[0])
        if not matches and criterion["id"]:
            for item in evidence:
                if isinstance(item, dict) and item.get("criterion_id") == criterion["id"]:
                    matches = [(0, item)]
                    break

        refs = [_evidence_label(item, index) for index, item in matches]
        status = "unknown"
        reason = "没有对应证据，无法判定"
        if matches:
            statuses = []
            reasons = []
            for _index, item in matches:
                item_status, item_reason = _evidence_status(item)
                statuses.append(item_status)
                reasons.append(item_reason)
            if "failed" in statuses:
                status = "failed"
            elif "passed" in statuses and all(s == "passed" for s in statuses):
                status = "passed"
            else:
                status = "unknown"
            reason = "; ".join(sorted(set(reasons))) or "证据无法判定"
        items.append({"criterion": criterion["text"], "status": status,
                      "evidence": refs, "reason": reason})

    statuses = [it["status"] for it in items]
    if "failed" in statuses:
        verdict = "fail"
    elif statuses and all(s == "passed" for s in statuses):
        verdict = "pass"
    else:
        verdict = "inconclusive"
    passed = sum(1 for s in statuses if s == "passed")
    failed = sum(1 for s in statuses if s == "failed")
    unknown = len(statuses) - passed - failed
    summary = "验收 %d 条：通过 %d / 失败 %d / 无法判定 %d → %s" % (
        len(statuses), passed, failed, unknown, verdict)
    return {"verdict": verdict, "items": items, "summary": summary}


def _verdict_rank(verdict):
    """策略严格度排序：deny > ask > allow，用于合并多重检查结果。"""
    return {"allow": 0, "ask": 1, "deny": 2}.get(str(verdict).lower(), 2)


def build_caps_index(raw):
    """把多种能力清单格式归一化为 ``{capability: [node, ...]}``。

    支持：

    * ``{"docker": ["nas"], "gpu": "workstation"}``（推荐格式）
    * ``[{"node": "nas", "caps": ["docker"]}, ...]``
    * ``[{"name": "nas", "capabilities": ["docker"]}, ...]``
    * ``{"nas": {"caps": ["docker"]}}``
    """
    result = {}

    def add(cap, node):
        cap = str(cap or "").strip().lower()
        node = str(node or "").strip()
        if not cap or not node:
            return
        bucket = result.setdefault(cap, [])
        if node not in bucket:
            bucket.append(node)

    if not raw:
        return result
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                nodes = value.get("nodes") or []
                caps = value.get("caps") or value.get("capabilities")
                if caps:
                    for cap in caps:
                        add(cap, key)
                for node in nodes:
                    if isinstance(node, dict):
                        for cap in (node.get("caps") or []):
                            add(cap, node.get("node") or node.get("name"))
                    elif isinstance(node, str):
                        add(key, node)
                continue
            if isinstance(value, str):
                add(key, value)
            elif isinstance(value, (list, tuple, set)):
                for node in value:
                    if isinstance(node, dict):
                        add(key, node.get("node") or node.get("name"))
                    else:
                        add(key, node)
    elif isinstance(raw, (list, tuple, set)):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            node = entry.get("node") or entry.get("name")
            for cap in (entry.get("caps") or entry.get("capabilities") or []):
                add(cap, node)
    return result


# ===========================================================================
# 七、确定性编排器
# ===========================================================================

PLAN_SYSTEM_PROMPT = (
    "你是 MCP Fleet 的确定性任务规划器。只输出 JSON，不要解释。结构："
    '{"steps":[{"goal":"目标","tool":"工具名","assignee":"local或节点名",'
    '"acceptance":"可判定的验收标准","args":{}}]}。'
    "tool 只能从 sysinfo/exec/read/list_dir/docker_ps/docker_logs/ps/"
    "net_check/caps/peer_call/mesh_caps 中选；assignee 为 local 表示本机。"
)


class Orchestrator(object):
    """确定性「观察→计划→执行→验证→反思」循环。

    不依赖 LLM 即可完成常见任务；注入 :class:`LLMAdapter` 后，仅当任务没有显式
    ``steps`` 时用 LLM 生成计划，且 LLM 任何失败都会**降级为确定性计划**（不会
    让任务失败）。

    安全：每个步骤执行前检查 policy（dict/callable/带 check() 的对象），
    ``ask`` 返回 ``awaiting_approval`` 且**不执行**，``deny`` 直接终止该步并
    整体失败；策略检查自身异常时按 deny 处理（fail-closed）。

    策略 dict 示例::

        {"deny": ["rm -rf", "mkfs"], "ask": ["restart", "reboot"]}
        {"exec": "ask"}          # 按工具名
        "allow"                  # 全部放行

    返回的轨迹结构::

        {"task_id","status","steps":[{"step","tool","node","ok","status",
          "detail","acceptance","attempts","goal"}],
         "result":{"summary","outputs","review"},
         "reflections":[{"at_step","note","next"}],
         "delegated":[node...], "plan":[...], "plan_source","review",
         "limited":bool,"pending_approval":dict|None}
    """

    def __init__(self, store, client, llm=None, caps_index=None, max_steps=8,
                 max_seconds=120, policy=None, max_retries=1,
                 agent_name=None, clock=None):
        if store is None:
            raise ValueError("Orchestrator 需要 TaskStore")
        if client is None:
            raise ValueError("Orchestrator 需要 FleetClient（或兼容对象）")
        self.store = store
        self.client = client
        self.llm = llm
        self.caps_index = build_caps_index(caps_index or {})
        self.max_steps = max(1, int(max_steps or 1))
        self.max_seconds = max(0, int(max_seconds or 0))
        self.policy = policy if policy is not None else getattr(client, "policy", None)
        self.max_retries = max(0, int(max_retries if max_retries is not None else 1))
        self.agent_name = agent_name or getattr(client, "name", "agent")
        self._clock = clock or time.time

    # -- 公共入口 --
    def run(self, task, dry_run=False):
        """执行任务；``dry_run=True`` 只规划不调用任何工具。"""
        if not isinstance(task, dict):
            raise ValueError("run(task) 需要 dict")
        goal = str(task.get("goal") or task.get("objective")
                   or task.get("prompt") or "未命名任务")
        task_id = task.get("task_id")
        if task_id:
            TaskStore.check_id(task_id)
            if self.store.has_task(task_id):
                record = self.store.update_task(task_id, {"goal": goal,
                                                          "status": "running"})
            else:
                record = self.store.create_task(dict(task, task_id=task_id, goal=goal))
                record["status"] = "running"
                self.store.update_task(task_id, {"status": "running"})
        else:
            record = self.store.create_task(dict(task, goal=goal, status="running"))
            task_id = record["task_id"]
        return self._run(dict(task, goal=goal), task_id, dry_run=dry_run,
                         resume_done=set())

    def resume(self, task_id):
        """从事件日志恢复任务：跳过已成功的步骤，继续执行剩余步骤。"""
        TaskStore.check_id(task_id)
        task = self.store.get_task(task_id)
        events = self.store.read_events(task_id)
        done = set()
        for event in events:
            if event.get("type") == "step" and event.get("ok"):
                try:
                    done.add(int(event.get("step")))
                except (TypeError, ValueError):
                    continue
        self.store.update_task(task_id, {"status": "running"})
        return self._run(task, task_id, dry_run=False, resume_done=done)

    # -- 计划 --
    def _plan(self, task, reflections):
        explicit = task.get("steps") or task.get("plan")
        if explicit:
            plan = []
            if isinstance(explicit, dict):
                explicit = [explicit]
            for index, item in enumerate(explicit or []):
                plan.append(self._normalize_step(item, index))
            plan = [step for step in plan if step]
            if plan:
                return plan, "explicit"
        if self.llm is not None:
            llm_plan = self._llm_plan(task, reflections)
            if llm_plan:
                return llm_plan, "llm"
        return self._deterministic_plan(task), "deterministic"

    @staticmethod
    def _normalize_step(item, index):
        if not isinstance(item, dict):
            return None
        tool = item.get("tool") or item.get("action")
        if not tool:
            return None
        acceptance = item.get("acceptance") or item.get("acceptance_criteria") or ""
        if isinstance(acceptance, (list, tuple)):
            acceptance = "；".join(str(a) for a in acceptance if a)
        return {
            "goal": str(item.get("goal") or item.get("description") or tool),
            "tool": str(tool),
            "assignee": str(item.get("assignee") or item.get("node") or "local"),
            "acceptance": str(acceptance) or "步骤成功返回结果",
            "args": dict(item.get("args") or item.get("arguments") or {}),
            "capability": item.get("capability"),
            "timeout": item.get("timeout"),
        }

    def _llm_plan(self, task, reflections):
        if self.llm is None:
            return None
        messages = [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"goal": task.get("goal"), "acceptance": task.get("acceptance"),
                 "capability": task.get("capability"), "node": task.get("node"),
                 "command": task.get("command"),
                 "notes": task.get("notes")}, ensure_ascii=False)},
        ]
        try:
            raw = self.llm.complete(messages)
            parsed = self._parse_plan_text(raw)
        except Exception as exc:  # LLM 任何失败都降级，不影响主流程
            reflections.append({
                "at_step": 0,
                "note": "LLM 规划失败，已降级为确定性规划",
                "next": "检查 LLM 配置/预算，或手工提供 steps",
                "error": _truncate(redact(str(exc)), 200)})
            return None
        if not parsed:
            reflections.append({
                "at_step": 0,
                "note": "LLM 返回的计划无法解析/不含有效步骤，已降级为确定性规划",
                "next": "用 fleet_plan 显式给出步骤"})
            return None
        return parsed

    @staticmethod
    def _parse_plan_text(raw):
        """从 LLM 文本中尽量提取合法步骤；失败返回 None。"""
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        if text.startswith("```"):
            lines = [line for line in text.splitlines()
                     if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start:end + 1])
        payload = None
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
                break
            except ValueError:
                continue
        if not isinstance(payload, dict):
            return None
        steps = payload.get("steps") or payload.get("plan")
        if not isinstance(steps, list):
            return None
        plan = []
        for index, item in enumerate(steps):
            step = Orchestrator._normalize_step(item, index)
            if step:
                plan.append(step)
        if not plan:
            return None
        return plan[:32]

    def _deterministic_plan(self, task):
        plan = [{
            "goal": "观察本机现状", "tool": "sysinfo", "assignee": "local",
            "acceptance": "sysinfo 返回主机/负载/内存/磁盘信息", "args": {},
            "capability": None, "timeout": None}]
        capability = task.get("capability")
        node = task.get("node") or task.get("target")
        command = task.get("command")
        if capability or node or command:
            target = node or self._route(capability) or "local"
            plan.append({
                "goal": "在目标节点执行命令", "tool": "exec",
                "assignee": target,
                "acceptance": "命令返回 exit=0 且输出非空",
                "args": {"command": command or "uptime"},
                "capability": capability, "timeout": task.get("timeout")})
            if target == "local" and capability:
                plan.append({
                    "goal": "记录能力路由降级", "tool": "note", "assignee": "local",
                    "acceptance": "已在反思中说明降级原因",
                    "args": {"note": "未找到具备能力 %s 的节点，已降级本机执行"
                                     % capability},
                    "capability": None, "timeout": None})
        plan.append({
            "goal": "汇总结果", "tool": "summary", "assignee": "local",
            "acceptance": "输出结构化结论（诊断/委派/结果）", "args": {},
            "capability": None, "timeout": None})
        return plan

    def _route(self, capability):
        if not capability:
            return None
        candidates = self.caps_index.get(str(capability).strip().lower()) or []
        return candidates[0] if candidates else None

    def _alternate_nodes(self, step):
        capability = step.get("capability")
        if not capability:
            return []
        return [node for node in self.caps_index.get(
            str(capability).strip().lower(), [])
            if node and node != step.get("assignee")]

    # -- 策略 --
    def _check_policy(self, tool, args):
        """返回 ``(verdict, reason)``，verdict ∈ allow/ask/deny。异常 fail-closed。"""
        policy = self.policy
        if policy is None:
            return "allow", "未配置策略"
        try:
            if hasattr(policy, "check") and callable(policy.check):
                result = policy.check(tool, args or {})
                if isinstance(result, tuple) and len(result) >= 2:
                    return self._norm_verdict(result[0]), str(result[1] or "")
                return self._norm_verdict(result), ""
            if callable(policy):
                result = policy(tool, args or {})
                if isinstance(result, tuple) and len(result) >= 2:
                    return self._norm_verdict(result[0]), str(result[1] or "")
                return self._norm_verdict(result), ""
            if isinstance(policy, str):
                return self._norm_verdict(policy), "全局策略"
            if isinstance(policy, dict):
                return self._dict_policy(policy, tool, args)
        except Exception as exc:
            return "deny", "策略检查异常，按拒绝处理: %s" % redact(str(exc))
        return "deny", "无法识别的策略类型，按拒绝处理"

    @staticmethod
    def _norm_verdict(value):
        text = str(value or "").strip().lower()
        if text in ("allow", "ok", "permit", "允许"):
            return "allow"
        if text in ("ask", "confirm", "approve", "确认"):
            return "ask"
        return "deny"

    def _dict_policy(self, policy, tool, args):
        try:
            haystack = (str(tool) + " " + json.dumps(args or {}, ensure_ascii=False,
                                                    sort_keys=True)).lower()
        except (TypeError, ValueError):
            haystack = str(tool).lower()
        tool_key = str(tool).strip().lower()
        if isinstance(policy.get("tools"), dict):
            verdict = policy["tools"].get(tool_key)
            if isinstance(verdict, str):
                return self._norm_verdict(verdict), "工具级策略 %s" % tool
        for key, verdict in (("deny", "deny"), ("ask", "ask")):
            patterns = policy.get(key) or policy.get(key + "_patterns") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            for pattern in patterns:
                if not pattern:
                    continue
                needle = str(pattern).strip().lower()
                if needle and needle in haystack:
                    return verdict, "命中策略 %s: %s" % (key, needle)
        if isinstance(policy.get("default"), str):
            return self._norm_verdict(policy["default"]), "策略默认值"
        if isinstance(policy.get("mode"), str):
            return self._norm_verdict(policy["mode"]), "策略模式"
        return "allow", "未命中策略规则"

    # -- 执行 --
    def _run(self, task, task_id, dry_run=False, resume_done=None):
        resume_done = resume_done or set()
        start = self._clock()
        reflections = []
        delegated = []
        pending_approval = None
        limited = False
        plan, plan_source = self._plan(task, reflections)
        self.store.append_event(task_id, {
            "type": "plan", "source": plan_source,
            "steps": [{"goal": s["goal"], "tool": s["tool"],
                       "assignee": s["assignee"]} for s in plan]})

        if dry_run:
            preview = [{"step": index + 1, "tool": step["tool"],
                        "node": step["assignee"], "ok": True,
                        "status": "planned", "goal": step["goal"],
                        "detail": "dry-run：仅规划未执行",
                        "acceptance": step["acceptance"], "attempts": 0}
                       for index, step in enumerate(plan)]
            summary = "dry-run 计划 %d 步（来源 %s），未执行任何工具" % (
                len(plan), plan_source)
            return {
                "task_id": task_id, "status": "planned", "steps": preview,
                "result": {"summary": summary, "outputs": [], "review": None},
                "reflections": reflections, "delegated": [],
                "plan": plan, "plan_source": plan_source, "review": None,
                "limited": False, "pending_approval": None,
            }

        trace = []
        for index, step in enumerate(plan):
            number = index + 1
            if number in resume_done:
                trace.append({
                    "step": number, "tool": step["tool"],
                    "node": step["assignee"], "ok": True, "status": "skipped",
                    "detail": "已完成，恢复时跳过", "goal": step["goal"],
                    "acceptance": step["acceptance"], "attempts": 0})
                continue
            if len(trace) >= self.max_steps:
                limited = True
                reflections.append({
                    "at_step": number,
                    "note": "达到步数上限 max_steps=%d，停止执行" % self.max_steps,
                    "next": "拆分任务或提高 max_steps"})
                break
            if self._clock() - start > self.max_seconds:
                limited = True
                reflections.append({
                    "at_step": number,
                    "note": "达到时间上限 max_seconds=%ds，停止执行" % self.max_seconds,
                    "next": "缩小任务范围或提高 max_seconds"})
                break
            outcome, delegated_nodes = self._execute_step(
                step, number, task, reflections)
            delegated.extend(delegated_nodes)
            trace.append(outcome)
            self.store.append_event(task_id, {
                "type": "step", "step": number, "tool": outcome["tool"],
                "node": outcome["node"], "ok": outcome["ok"],
                "status": outcome["status"],
                "acceptance": outcome.get("acceptance", ""),
                "goal": outcome.get("goal", ""),
                "detail": _truncate(outcome["detail"], 500)})
            if outcome.get("status") == "awaiting_approval":
                pending_approval = outcome.get("approval")
                break
            if outcome.get("status") == "denied":
                break

        delegated_unique = []
        for node in delegated:
            if node not in delegated_unique:
                delegated_unique.append(node)

        review = self._final_review(task, trace)
        status = self._final_status(trace, review, limited, pending_approval)
        summary = self._summary_text(task, trace, status, review)
        result = {
            "summary": summary,
            "outputs": [{"step": item["step"], "tool": item["tool"],
                         "node": item["node"], "ok": item["ok"],
                         "detail": item["detail"]} for item in trace],
            "review": review,
        }
        self.store.update_task(task_id, {
            "status": status,
            "result_summary": _truncate(summary, 2000),
            "delegated": delegated_unique,
        })
        self.store.append_event(task_id, {
            "type": "finish", "status": status,
            "steps": len(trace), "limited": limited})
        return {
            "task_id": task_id, "status": status, "steps": trace,
            "result": result, "reflections": reflections,
            "delegated": delegated_unique, "plan": plan,
            "plan_source": plan_source, "review": review,
            "limited": limited, "pending_approval": pending_approval,
        }

    def _execute_step(self, step, number, task, reflections):
        """执行单步（含重试/升级/策略），返回 ``(outcome, [远端节点])``。"""
        tool = step["tool"]
        node = step["assignee"] or "local"
        args = dict(step.get("args") or {})
        delegated = [] if _is_local_node(node, self.agent_name) else [node]

        if tool in ("summary", "review", "noop"):
            return self._internal_step(step, number), delegated
        if tool == "note":
            note = args.get("note") or step["goal"]
            reflections.append({"at_step": number, "note": note,
                                "next": "继续后续步骤"})
            return self._outcome(step, number, "local", True, "ok", note,
                                 attempts=0), delegated

        verdict, reason = self._check_policy(tool, args)
        if not _is_local_node(node, self.agent_name):
            # 远端步骤实际走 peer_call：额外检查传输层策略，取更严格的结果，
            # 否则 {"ask": ["peer_call"]} 这类策略会被逻辑工具名绕过。
            remote_tool = "peer_call"
            remote_args = {"peer": node, "tool": tool, "arguments": args}
            remote_verdict, remote_reason = self._check_policy(remote_tool,
                                                               remote_args)
            if _verdict_rank(remote_verdict) > _verdict_rank(verdict):
                verdict, reason = remote_verdict, remote_reason
        if verdict == "deny":
            detail = "策略拒绝执行 %s：%s" % (tool, reason or "命中 deny 规则")
            reflections.append({"at_step": number, "note": detail,
                                "next": "换用允许的工具，或让有权限的人处理"})
            return self._outcome(step, number, node, False, "denied", detail), []
        if verdict == "ask":
            detail = "需要人工确认后才能执行 %s：%s" % (tool, reason or "命中 ask 规则")
            approval = {"tool": tool, "node": node, "args": args,
                        "reason": reason or "命中 ask 策略",
                        "task_id": task.get("task_id")}
            reflections.append({"at_step": number, "note": detail,
                                "next": "调用方/人工确认后 resume 继续"})
            return self._outcome(step, number, node, False, "awaiting_approval",
                                 detail, extra={"approval": approval}), delegated

        attempts = self.max_retries + 1
        last = None
        for attempt in range(1, attempts + 1):
            try:
                result = self._invoke_step(node, tool, args)
            except Exception as exc:
                result = {"ok": False, "text": redact(str(exc))}
            last = result
            if result.get("ok"):
                return self._outcome(step, number, node, True, "ok",
                                     result.get("text") or "执行成功",
                                     attempts=attempt), delegated
            if attempt < attempts:
                reflections.append({
                    "at_step": number,
                    "note": "步骤失败，准备重试（第 %d/%d 次）：%s"
                            % (attempt + 1, attempts,
                               _truncate(redact(result.get("text")), 160)),
                    "next": "重试相同语义、不同超时/参数"})

        # 失败升级：换同能力备用节点
        for alternate in self._alternate_nodes(step):
            if alternate == node:
                continue
            reflections.append({
                "at_step": number,
                "note": "节点 %s 执行失败，升级到备用节点 %s" % (node, alternate),
                "next": "在备用节点重放同一命令"})
            try:
                result = self._invoke_step(alternate, tool, args)
            except Exception as exc:
                result = {"ok": False, "text": redact(str(exc))}
            delegated.append(alternate)
            if result.get("ok"):
                return self._outcome(step, number, alternate, True, "ok",
                                     result.get("text") or "备用节点执行成功",
                                     attempts=attempts,
                                     extra={"escalated_from": node}), delegated
            last = result

        detail = "步骤失败：%s" % _truncate(redact(last.get("text") if last else ""),
                                            300)
        reflections.append({"at_step": number, "note": detail,
                            "next": "收集证据后用 fleet_reflect 记录并升级"})
        return self._outcome(step, number, node, False, "failed", detail,
                             attempts=attempts), delegated

    def _invoke_step(self, node, tool, args):
        """调用一次工具并归一化结果（兼容只提供 call/call_tool 的客户端）。"""
        client = self.client
        if hasattr(client, "call") and callable(client.call):
            value = client.call(node, tool, args)
        elif hasattr(client, "call_tool") and callable(client.call_tool):
            value = client.call_tool(tool, args)
        else:
            raise RuntimeError("客户端既没有 call 也没有 call_tool")
        return normalize_tool_result(value, tool=tool, node=node)

    @staticmethod
    def _internal_step(step, number):
        return {"step": number, "tool": step["tool"], "node": "local",
                "ok": True, "status": "ok",
                "detail": "内部步骤：%s" % step["goal"],
                "goal": step["goal"], "acceptance": step["acceptance"],
                "attempts": 0}

    @staticmethod
    def _outcome(step, number, node, ok, status, detail, attempts=1, extra=None):
        outcome = {
            "step": number, "tool": step["tool"], "node": node,
            "ok": bool(ok), "status": status,
            "detail": _truncate(redact(detail or ""), 1200),
            "goal": step["goal"], "acceptance": step["acceptance"],
            "attempts": attempts,
        }
        if extra:
            outcome.update(extra)
        return outcome

    # -- 反思/验证/汇报 --
    def _final_review(self, task, trace):
        acceptance = task.get("acceptance")
        evidence = []
        for item in trace:
            evidence.append({
                "criterion": item.get("acceptance"),
                "goal": item.get("goal"),
                "step": item["step"], "tool": item["tool"],
                "node": item["node"], "ok": item["ok"],
                "detail": item["detail"],
            })
        if acceptance:
            return review_acceptance(acceptance, evidence)
        ok = all(item["ok"] for item in trace) if trace else False
        delegated = any(not _is_local_node(item["node"], self.agent_name)
                        for item in trace)
        return {
            "verdict": "pass" if ok else "fail",
            "items": [],
            "summary": ("执行结果%s；%s" %
                        ("全部成功" if ok else "存在失败步骤",
                         "含跨设备委派" if delegated else "仅本机执行")),
        }

    def _final_status(self, trace, review, limited, pending_approval):
        if pending_approval:
            return "awaiting_approval"
        if any(item["status"] == "denied" for item in trace):
            return "failed"
        if any(not item["ok"] for item in trace):
            return "partial" if limited else "failed"
        if limited:
            return "partial"
        if review and review.get("verdict") == "fail":
            return "failed"
        if review and review.get("verdict") == "inconclusive":
            return "partial"
        return "completed"

    def _summary_text(self, task, trace, status, review):
        goal = task.get("goal") or "未命名任务"
        ok_steps = [item for item in trace if item["ok"]]
        nodes = sorted(set(item["node"] for item in trace
                           if not _is_local_node(item["node"], self.agent_name)))
        bits = ["任务「%s」执行完成，状态=%s（%d/%d 步成功）"
                % (goal, status, len(ok_steps), len(trace))]
        if nodes:
            bits.append("委派节点：%s" % "、".join(nodes))
        if trace:
            last = trace[-1]
            bits.append("最后一步 %s@%s：%s"
                        % (last["tool"], last["node"],
                           _truncate(last["detail"], 200)))
        if review and review.get("summary"):
            bits.append("验收：%s" % review["summary"])
        return "；".join(bits)


# ===========================================================================
# 八、MCP initialize instructions（Markdown ≤ 6000 字符）
# ===========================================================================

_INSTRUCTIONS_TEMPLATE = """# MCP Fleet 节点 · Agent 操作手册

> 你在操作节点 **$NAME$**（fleet $VERSION$，工具前缀 `$PREFIX$`）。$READONLY$
> 本手册是常驻工作方式：先观察、再计划，动手前查安全边界，结束后自检并把结论写回任务日志。

## 1. 这个节点是什么，什么时候用它
- 本节点 = 一台真实设备上的执行代理（shell/文件/服务/容器）+ mesh 网络入口。
- 直接操作本机：`$PREFIX$exec` / `$PREFIX$read` / `$PREFIX$write` / `$PREFIX$list_dir` / `$PREFIX$sysinfo`。
- 需要另一台设备（NAS/软路由/云主机）的算力或数据：先 `$PREFIX$caps` / `$PREFIX$mesh_caps` / `$PREFIX$peers`，再 `$PREFIX$peer_call`（peer=节点名）。节点不分主副。
- 需要别的 agent 协作或留证据：邮箱 `$PREFIX$msg_send` / `$PREFIX$msg_recv`，正文可放结构化信封（见第 8 节）。
- 不确定该做什么：先 `$PREFIX$fleet_help` 看手册，再 `$PREFIX$fleet_think` 把观察写进任务日志。
- 不在职责内：绕过鉴权、改别人配置、无边界删除/格式化——直接拒绝并说明原因。$PEERS_LINE$

## 2. 工具选择矩阵
| 你的意图 | 首选工具 | 备注 |
|---|---|---|
| 看清现状 | sysinfo / ps / docker_ps / net_check | 只读，先做这一步 |
| 执行一次性命令 | exec | 长任务用 exec_async + exec_poll/exec_result |
| 改文件 | read → edit(dryRun 预览) → write | 先读后写，别盲改 |
| 查服务/容器日志 | service_logs / docker_logs | 先看日志再重启 |
| 重启服务/容器 | systemctl restart / docker_restart | 高风险，需确认 |
| 跨设备执行 | peers → peer_call | peer 用节点名，可多跳进内网 |
| 按能力找人 | mesh_caps / delegate | 能力如 docker / gpu / tailscale |
| 多步任务 | fleet_plan → msg_send(信封) | 每步必须有验收标准 |
| 收尾 | fleet_review | 逐条判定验收，不臆断通过 |
| 失败复盘 | fleet_reflect | 记录教训再升级 |

## 3. 安全边界（红线）
- 写操作/重启/删除前确认：目标节点、路径、影响面；不确定就问。
- 禁止 `rm -rf /`、`dd of=/dev/sd*`、`mkfs`、清空生产库、改 SSH/防火墙导致失联。
- 密钥、token、私钥、邀请码不写进消息、日志、代码或回复；需要时只说“已配置”。
- read_only 节点拒绝一切变更类工具；策略 deny 时不要换工具绕过。
- 是否越权拿不准：用 `$PREFIX$fleet_handoff` 交给人/调用方确认（ask 路径）。
- 同一命令连续失败不要反复重试（最多 1 次），改为收集证据并升级。

## 4. 自对话四步法 Observe → Plan → Act → Reflect
1. Observe：sysinfo / peers / caps / 日志 / read，先收集事实，不猜。
2. Plan：`$PREFIX$fleet_plan` 写出步骤，每步 = goal + tool + assignee + acceptance，缺一不可。
3. Act：一次只做一个步骤；高风险先 dryRun 或确认；保留真实输出。
4. Reflect：`$PREFIX$fleet_reflect` 记录结果与教训，再 `$PREFIX$fleet_review` 逐条验收。
四步之间用 `$PREFIX$fleet_think` 把关键观察写进任务日志（JSONL/0600），别只留在上下文里。

## 5. 跨设备委派
- 发现：peers(check=true) 看在线；mesh_caps 看能力；节点名才可作为 peer。
- 委派：fleet_plan 的 assignee 写节点名；执行用 peer_call(peer=…, tool=…, arguments=…)。
- 任务消息：fleet_handoff 生成 handoff 信封（thread_id / task_id / deadline），msg_send(topic=fleet-task)。
- 汇总：收敛成「谁、做了什么、结果、证据」四要素，不贴大段原始日志。

## 6. 失败升级路径
1) 同语义重试 1 次（只换超时/参数）；2) 换同能力备用节点；3) 降级为局部结论；
4) fleet_reflect 记录阻塞点 + fleet_handoff 交人；5) 明确说明已完成/未完成/继续所需条件。

## 7. 常见任务模板
- 诊断：sysinfo → docker_ps/ps → 相关日志 → 结论（现象/根因假设/下一步）
- 变更：read 配置 → dryRun edit → 备份 → restart → 健康检查(http_get) → 回滚方案
- 巡检：多节点 sysinfo/ps/磁盘 → 异常表 → 建议清单
- 跨设备排障：本机诊断 → mesh_caps → 远端执行 → 汇总 → fleet_review

## 8. 结构化消息与自检
- 信封 kind：propose / accept / counter / progress / result / fail / review / handoff / hello；
  字段 v / kind / thread_id / from / to / payload / task_id / deadline_ms；普通文本仍可直接发（兼容）。
- 自检：目标写清了吗？每步有验收标准吗？输出可复现吗？密钥外泄了吗？结论有证据吗？下一步谁做？

> 完整手册 references/agent-playbook.md；协议 references/self-dialogue-protocol.md；
> 可粘贴提示词 references/prompt-pack.md。
"""


def build_instructions(cfg=None):
    """生成 MCP ``initialize`` 的 instructions（Markdown，≤6000 字符）。

    :param cfg: 可选配置，支持 ``name/node_name``、``prefix/tool_prefix``、
        ``version``、``peers``（list 或 dict）、``read_only``。
    :return: 中文 Markdown 文本，长度不超过 :data:`MAX_INSTRUCTIONS`。

    内容覆盖契约要求：本节点是什么/何时用、工具选择矩阵、安全边界、四步自对话、
    跨设备委派、失败升级、常见任务模板、结构化消息速查。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    name = str(cfg.get("name") or cfg.get("node_name") or "fleet")[:64]
    prefix = str(cfg.get("prefix") or cfg.get("tool_prefix") or "")
    version = str(cfg.get("version") or VERSION)
    read_only = bool(cfg.get("read_only"))
    read_note = ("本节点为**只读模式**：变更类工具会被拒绝，请只用观察类工具。\n"
                 if read_only else "")
    peers = cfg.get("peers")
    peer_names = []
    if isinstance(peers, dict):
        peer_names = [str(k) for k in peers.keys()]
    elif isinstance(peers, (list, tuple, set)):
        peer_names = [str(p) for p in peers]
    peer_names = [p for p in peer_names if p]
    peers_line = ""
    if peer_names:
        shown = "、".join(peer_names[:12])
        more = "等 %d 个" % len(peer_names) if len(peer_names) > 12 else ""
        peers_line = "\n- 已知对等节点（%s%s）：可直接作为 peer 名使用。" % (shown, more)

    text = (_INSTRUCTIONS_TEMPLATE
            .replace("$NAME$", name)
            .replace("$VERSION$", version)
            .replace("$PREFIX$", prefix)
            .replace("$READONLY$", read_note)
            .replace("$PEERS_LINE$", peers_line))
    return _fit_instructions(text)


def _fit_instructions(text):
    """把 instructions 收敛到 ``MAX_INSTRUCTIONS`` 以内（按章节边界截断）。"""
    if len(text) <= MAX_INSTRUCTIONS:
        return text
    marker = ("\n\n> 手册已按 MCP instructions 上限截断；完整版见 "
              "references/agent-playbook.md。\n")
    budget = MAX_INSTRUCTIONS - len(marker) - 1
    cut = text.rfind("\n## ", 0, budget)
    if cut <= 0:
        cut = budget
    return text[:cut].rstrip() + marker


# ===========================================================================
# 九、MCP prompts：playbook / diagnose / plan / self-review / delegate /
#     incident / onboard
# ===========================================================================

PROMPTS = {
    "fleet-playbook": {
        "description": "MCP Fleet 总操作手册：节点定位、工具矩阵、四步自对话、安全红线与升级路径。",
        "arguments": [
            {"name": "goal", "required": False,
             "description": "本次要达成的目标（可留空）"},
            {"name": "node", "required": False,
             "description": "目标节点名（默认本机）"},
        ],
        "template": (
            "你是 MCP Fleet 的操作 agent，当前节点：{{node}}。\n"
            "目标：{{goal}}\n\n"
            "请严格按以下工作方式执行，不要跳步：\n"
            "1. Observe：调用 sysinfo / peers / caps 收集事实；不确定时先 fleet_help。\n"
            "2. Plan：调用 fleet_plan，每个步骤必须给出 goal、tool、assignee、acceptance。\n"
            "3. Act：一次执行一个步骤；高风险操作先 dryRun 或用 peer_call 前确认节点；\n"
            "   失败最多同语义重试 1 次，然后换备用节点或升级。\n"
            "4. Reflect：调用 fleet_reflect 记录结果，再 fleet_review 逐条验收。\n\n"
            "安全红线：不执行 rm -rf /、mkfs、dd of=/dev/*；不改 SSH/防火墙导致失联；\n"
            "密钥/token/邀请码不外泄；只读节点不做任何变更。\n"
            "输出要求：最终给「结论 + 证据 + 下一步」，不要贴超长原始日志。"
        ),
    },
    "fleet-diagnose": {
        "description": "诊断某节点或服务异常：按证据链定位现象、假设与下一步。",
        "arguments": [
            {"name": "symptom", "required": True,
             "description": "现象描述，如“nas 上 nginx 容器反复重启”"},
            {"name": "node", "required": False, "description": "目标节点，默认本机"},
            {"name": "since", "required": False, "description": "开始时间或最近多久"},
            {"name": "scope", "required": False,
             "description": "范围：container / service / disk / network / all"},
        ],
        "template": (
            "诊断任务：{{symptom}}\n节点：{{node}}；时间范围：{{since}}；范围：{{scope}}\n\n"
            "按顺序执行，每步记录证据：\n"
            "1. fleet_think 记录现象原文（不要改写）。\n"
            "2. Observe：sysinfo → ps/docker_ps → 对应服务或容器日志（service_logs / docker_logs）。\n"
            "3. 若涉及网络：net_check(action=listen/port/dns)；若涉及磁盘：exec 查看 df -h / du。\n"
            "4. 只读定位后，给出最多 3 个根因假设，并按证据强弱排序。\n"
            "5. 需要变更时：fleet_plan 写「变更步骤 + 回滚方案 + 验收标准」，高风险用 fleet_handoff 请人确认。\n"
            "6. 收尾：fleet_review 用证据逐条判定；fleet_reflect 记录结论与下一步。\n\n"
            "禁止：未看日志就重启；未备份就改配置；把猜测当结论。"
        ),
    },
    "fleet-plan": {
        "description": "把目标拆成可执行、可验收的确定性步骤计划。",
        "arguments": [
            {"name": "goal", "required": True, "description": "要达成的可验证目标"},
            {"name": "constraints", "required": False,
             "description": "约束，如“只读”“不改生产”“15 分钟内完成”"},
            {"name": "nodes", "required": False,
             "description": "可用节点/能力，如“nas(docker), pi(gpio)”"},
        ],
        "template": (
            "规划任务：{{goal}}\n约束：{{constraints}}\n可用节点/能力：{{nodes}}\n\n"
            "请输出可执行计划，并调用 fleet_plan 落盘：\n"
            "- 每步字段：goal（这一步要达成什么）、tool（fleet 工具名）、\n"
            "  assignee（local 或节点名）、acceptance（可判定标准，如“exit=0 且输出含 active”）。\n"
            "- 步骤顺序：观察 → 变更（含备份/回滚）→ 验证 → 汇报；单步只做一件事。\n"
            "- 高风险步骤单独列出，标注“需要人工确认”。\n"
            "- 给出最多 8 步；超出的拆成子任务，用 fleet_handoff 交给其他节点/agent。\n"
            "规划完成后先 dry-run 检查路径与影响面，再执行第一步。"
        ),
    },
    "fleet-self-review": {
        "description": "对已完成工作做自审：按验收标准逐条判定 passed/failed/unknown 并给证据。",
        "arguments": [
            {"name": "goal", "required": True, "description": "原任务目标"},
            {"name": "acceptance", "required": False, "description": "验收标准，分号分隔"},
            {"name": "evidence", "required": False, "description": "执行证据/输出摘要"},
        ],
        "template": (
            "自审任务：{{goal}}\n验收标准：{{acceptance}}\n已有证据：{{evidence}}\n\n"
            "要求：\n"
            "1. 逐条判定，状态只能是 passed / failed / unknown；unknown 时必须说明缺什么证据。\n"
            "2. 每条结论必须引用证据（步骤号 / 工具 / 节点 / 关键输出），不得臆断。\n"
            "3. 调用 fleet_review 落盘判定结果。\n"
            "4. 存在 failed/unknown 时：说明影响面、补救动作、是否需要 fleet_handoff 升级。\n"
            "5. 最后给一句话结论：完成 / 部分完成 / 未完成，以及下一步责任人。"
        ),
    },
    "fleet-delegate": {
        "description": "把子任务委派给有相应能力的远端节点，并生成结构化 handoff 信封。",
        "arguments": [
            {"name": "goal", "required": True, "description": "要委派的子任务目标"},
            {"name": "capability", "required": False,
             "description": "所需能力，如 docker / gpu / tailscale"},
            {"name": "target", "required": False, "description": "指定节点（可留空自动选）"},
            {"name": "deadline_s", "required": False, "description": "截止秒数"},
        ],
        "template": (
            "委派任务：{{goal}}\n所需能力：{{capability}}；目标节点：{{target}}；截止：{{deadline_s}} 秒\n\n"
            "步骤：\n"
            "1. 发现：peers(check=true) 与 mesh_caps 确认谁在线、谁有该能力。\n"
            "2. 选择：优先同能力节点中负载最低/最近的一个；写清选择理由。\n"
            "3. 计划：fleet_plan 中 assignee 写目标节点，acceptance 必须可判定。\n"
            "4. 发送：fleet_handoff(to=目标节点, goal=…, deadline_s=…) 生成信封，\n"
            "   再用 msg_send(topic=fleet-task) 投递；记录 thread_id 方便追踪。\n"
            "5. 跟踪：msg_recv 收 progress/result/fail；超时未回则 fleet_reflect 后升级。\n"
            "6. 汇总：把远端结果翻译成「谁、做了什么、结果、证据」，不要只转发原文。\n"
            "注意：委派不等于免责；调用方仍要验收。"
        ),
    },
    "fleet-incident": {
        "description": "事故响应：先止损、再定位、保留证据、按升级路径通报。",
        "arguments": [
            {"name": "symptom", "required": True, "description": "事故现象"},
            {"name": "severity", "required": False,
             "description": "P0/P1/P2/P3，默认 P1"},
            {"name": "affected", "required": False, "description": "影响面（服务/用户/节点）"},
        ],
        "template": (
            "事故响应：{{symptom}}\n等级：{{severity}}；影响面：{{affected}}\n\n"
            "原则：先止损、后定位、再复盘；每一步留证据。\n"
            "1. fleet_think 记录时间线起点与已知事实（现象、影响、最近变更）。\n"
            "2. Observe（只读优先）：sysinfo/ps/docker_ps、服务日志、net_check、df -h。\n"
            "3. 止损：只做可回滚的最小动作（重启单容器 / 切只读 / 摘流量），\n"
            "   先 fleet_plan 写清动作+回滚+验收；P0 变更必须 fleet_handoff 请人确认。\n"
            "4. 定位：按「最近变更 → 资源耗尽 → 依赖故障」顺序排除，每步记录证据。\n"
            "5. 通报：fleet_handoff 生成 incident 信封，说明现状/已做动作/需要什么支持/下次更新时间。\n"
            "6. 复盘：fleet_reflect 记录根因、止损有效性、改进项与责任人；fleet_review 验收。\n"
            "禁止：未确认根因就大规模重启；删除日志；对外承诺未验证的恢复时间。"
        ),
    },
    "fleet-onboard": {
        "description": "把一台新设备接入 mesh：安装、配对、验证能力、登记到工具矩阵。",
        "arguments": [
            {"name": "node", "required": True, "description": "新节点名"},
            {"name": "host", "required": False, "description": "主机/IP"},
            {"name": "platform", "required": False,
             "description": "平台：linux/macos/windows/nas/termux/docker"},
            {"name": "mode", "required": False,
             "description": "接入方式：direct/assisted；只读节点写 read_only"},
        ],
        "template": (
            "入网任务：节点名 {{node}}，主机 {{host}}，平台 {{platform}}，模式 {{mode}}\n\n"
            "1. 计划：fleet_plan 写入网步骤（安装 → 启动 → 配对 → 验证 → 登记），每步有验收标准。\n"
            "2. 安装：优先一条命令安装器（fleet_install.py），记录版本与数据目录；不擅自装系统包。\n"
            "3. 配对：生成/解析邀请 URI（fleet://join/...），核对指纹后 peer_add；\n"
            "   邀请码属于密钥，不得写入日志或聊天正文。\n"
            "4. 验证：peer_call 到新节点执行 sysinfo；确认 auth、只读标记、超时与重试符合预期。\n"
            "5. 登记：peer_info/caps 结果记录到 mesh 清单；告知调用方节点名与能力。\n"
            "6. 复盘：fleet_reflect 记录平台差异/坑点；fleet_review 逐条验收。\n"
            "安全：先确认目标机器身份再配对；不要让新节点获得超出需要的权限。"
        ),
    },
}


def _render_template(template, arguments, arg_specs):
    """替换 ``{{name}}`` 占位符；可选参数缺失时填“（未提供）”。"""
    text = template or ""
    for spec in arg_specs or []:
        key = spec.get("name")
        if not key:
            continue
        value = arguments.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = "" if spec.get("required") else "（未提供）"
        text = text.replace("{{%s}}" % key, str(value))
    # 清掉模板中未声明的占位符，避免模型看到原始花括号
    text = re.sub(r"\{\{[A-Za-z0-9_\-]+\}\}", "（未提供）", text)
    return text


def list_prompts():
    """返回 MCP ``prompts/list`` 的 prompts 数组（name/description/arguments）。"""
    prompts = []
    for name in sorted(PROMPTS.keys()):
        spec = PROMPTS[name]
        prompts.append({
            "name": name,
            "description": spec.get("description", ""),
            "arguments": [dict(arg) for arg in spec.get("arguments", [])],
        })
    return prompts


def get_prompt(name, arguments=None):
    """返回 MCP ``prompts/get`` 结构：``{"description","messages":[...]}``。

    :raises KeyError: 未知提示词（错误信息列出可用名称）
    :raises ValueError: 缺少必填参数（错误信息列出缺哪些）
    """
    spec = PROMPTS.get(name)
    if spec is None:
        raise KeyError("未知提示词 %r；可用：%s"
                       % (name, ", ".join(sorted(PROMPTS.keys()))))
    args = dict(arguments or {})
    missing = []
    for arg in spec.get("arguments", []):
        if not arg.get("required"):
            continue
        value = args.get(arg["name"])
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(arg["name"])
    if missing:
        raise ValueError("提示词 %s 缺少必填参数: %s（示例：%s）"
                         % (name, ", ".join(missing),
                            ", ".join('"%s":"..."' % m for m in missing)))
    text = _render_template(spec.get("template", ""), args,
                            spec.get("arguments", []))
    return {
        "description": spec.get("description", ""),
        "messages": [{"role": "user",
                      "content": {"type": "text", "text": text}}],
    }


# ===========================================================================
# 十、自对话工具（fleet_help / think / plan / reflect / review / handoff）
# ===========================================================================

def _ok(text, structured=None):
    payload = {"ok": True}
    if structured:
        payload.update(structured)
    return {"content": [{"type": "text", "text": text}],
            "isError": False, "structuredContent": payload}


def _err(message, structured=None):
    payload = {"ok": False, "error": _truncate(redact(message), 800)}
    if structured:
        payload.update(structured)
    return {"content": [{"type": "text", "text": _truncate(redact(message), 2000)}],
            "isError": True, "structuredContent": payload}


def _ctx_store(ctx):
    """从 ctx 取 TaskStore；缺省使用 ``FLEET_AGENT_HOME`` 或 ~/.fleet/agent。"""
    store = (ctx or {}).get("store")
    if store is not None:
        return store
    base = os.environ.get("FLEET_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".fleet", "agent")
    return TaskStore(base)


def _ctx_agent(ctx):
    ctx = ctx or {}
    return str(ctx.get("agent") or ctx.get("node") or "agent")


def _ensure_task(store, task_id, goal="", origin="tool"):
    """任务不存在则创建（让 think/plan 可以先于 create 使用）。"""
    if store.has_task(task_id):
        return store.get_task(task_id)
    return store.create_task({"task_id": task_id, "goal": goal or task_id,
                              "status": "open", "origin": origin})


def _journal_path(store, task_id):
    try:
        return store._events_path(task_id)
    except (AttributeError, ValueError):
        return ""


def _has_event_type(store, task_id, event_type):
    for event in store.read_events(task_id):
        if event.get("type") == event_type:
            return True
    return False


_FAIL_WORDS_CN = ("失败", "报错", "错误", "不通", "超时", "拒绝", "异常",
                  "重启", "退出", "崩溃", "挂了", "掉线", "卡住", "不工作",
                  "error", "fail", "timeout", "denied", "crash", "oom",
                  "restart", "502", "503", "nok")
_DONE_WORDS_CN = ("成功", "完成", "通过", "恢复", "ok", "done", "success",
                  "resolved")


def _next_hint(thought, has_plan, has_review):
    """根据一句观察给出确定性的下一步建议。"""
    low = (thought or "").lower()
    if any(word in low for word in _FAIL_WORDS_CN):
        return {
            "next_action": "reflect",
            "suggested_tool": "fleet_reflect",
            "reason": "观察里出现失败/异常信号，先记录并定位，不要盲目重试",
            "questions": ["哪一步开始失败？", "是否有可复现命令与原始输出？",
                          "同能力备用节点是否在线？"],
        }
    if any(word in low for word in _DONE_WORDS_CN):
        return {
            "next_action": "review",
            "suggested_tool": "fleet_review",
            "reason": "看起来已达成目标，用验收标准逐条判定",
            "questions": ["验收标准是否都有证据？", "是否需要通知调用方？"],
        }
    if not has_plan:
        return {
            "next_action": "plan",
            "suggested_tool": "fleet_plan",
            "reason": "还没有计划，先把目标拆成带验收标准的步骤",
            "questions": ["目标的可判定结果是什么？", "需要哪些节点/能力？",
                          "哪些步骤需要人工确认？"],
        }
    if not has_review:
        return {
            "next_action": "act",
            "suggested_tool": "sysinfo",
            "reason": "已有计划，先执行下一步只读观察，再决定变更",
            "questions": ["下一步的验收标准是什么？", "失败时回滚路径是什么？"],
        }
    return {
        "next_action": "handoff",
        "suggested_tool": "fleet_handoff",
        "reason": "计划与验收都已完成，把结论交给调用方/其他节点",
        "questions": ["需要谁继续跟进？", "截止时间是什么？"],
    }


def _fleet_help(args, ctx):
    """fleet_help：返回操作手册摘要与下一步建议。"""
    topic = str((args or {}).get("topic") or "").strip().lower()
    topics = {
        "tools": "工具选择：只读(sysinfo/ps/docker_ps/net_check) → 执行(exec/exec_async) → "
                 "变更(systemctl/docker_restart/edit) → 跨设备(peers/peer_call/mesh_caps) → "
                 "协作(msg_send/msg_recv) → 规划与验收(fleet_plan/fleet_review/fleet_reflect)。",
        "plan": "规划：fleet_plan 的每一步必须包含 goal/tool/assignee/acceptance；"
                "assignee 写 local 或对端节点名；acceptance 必须可判定。",
        "delegate": "跨设备：peers(check=true) → mesh_caps → fleet_plan(assignee=节点名) → "
                    "peer_call / fleet_handoff+msg_send；收 progress/result/fail 后汇总。",
        "review": "验收：fleet_review 对每条标准给 passed/failed/unknown + 证据；"
                  "unknown 必须说明缺什么证据。",
        "safety": "安全：写/重启/删除前确认目标与影响面；禁止 rm -rf /、mkfs、dd of=/dev/*；"
                  "密钥/邀请码不外泄；read_only 节点拒绝变更；deny 不绕过；ask 先确认。",
        "protocol": "协议：信封 kind ∈ propose/accept/counter/progress/result/fail/review/"
                    "handoff/hello，含 thread_id/from/to/payload/task_id/deadline_ms；"
                    "普通文本仍兼容。",
        "onboard": "入网：安装器 → 启动 → 邀请/指纹核对 → peer_add → peer_call sysinfo 验证 → "
                   "登记能力；邀请码按密钥处理。",
    }
    body = topics.get(topic) or (
        "MCP Fleet Agent 操作手册：\n"
        "1) Observe：sysinfo/peers/caps/日志，先收集事实。\n"
        "2) Plan：fleet_plan，每步 goal/tool/assignee/acceptance。\n"
        "3) Act：一次一步，高风险先 dryRun 或确认。\n"
        "4) Reflect：fleet_reflect + fleet_review。\n"
        "失败升级：同语义重试 1 次 → 换备用节点 → 降级结论 → fleet_handoff 交人。")
    suggested = {
        "next_action": "plan",
        "suggested_tool": "fleet_plan",
        "reason": "先定目标并拆出可验收步骤",
        "questions": ["目标是什么？", "涉及哪些节点/能力？"],
    }
    text = "【fleet_help%s】\n%s\n\n下一步：调用 %s(%s)" % (
        ("/%s" % topic) if topic else "", body, suggested["suggested_tool"],
        ", ".join(suggested["questions"]))
    docs = ["references/agent-playbook.md",
            "references/self-dialogue-protocol.md",
            "references/prompt-pack.md"]
    return _ok(text, {"topic": topic, "topics": sorted(topics.keys()),
                      "suggested_next": suggested, "docs": docs})


def _fleet_think(args, ctx):
    """fleet_think：把一条观察写入任务日志，并返回确定性的下一步。"""
    args = args or {}
    task_id = str(args.get("task_id") or "").strip()
    thought = str(args.get("thought") or args.get("note") or "").strip()
    if not task_id:
        return _err("缺少 task_id：请传入任务 ID（可先用 fleet_plan 创建，或自定义如 "
                    "\"t-demo-1\"）。", {"field": "task_id"})
    if not thought:
        return _err("缺少 thought：把这次观察/判断用一句话写清楚，例如 "
                    "\"nas 上 nginx 容器 5 分钟内重启 3 次\"。", {"field": "thought"})
    store = _ctx_store(ctx)
    if not store.has_task(task_id):
        _ensure_task(store, task_id, goal=_truncate(thought, 120), origin="fleet_think")
    evidence = args.get("evidence")
    if not isinstance(evidence, list):
        evidence = [] if evidence is None else [evidence]
    event = {"type": "think", "agent": _ctx_agent(ctx), "thought": thought,
             "evidence": [_truncate(redact(str(e)), 300) for e in evidence[:20]],
             "at_ms": now_ms()}
    store.append_event(task_id, event)
    has_plan = _has_event_type(store, task_id, "plan")
    has_review = _has_event_type(store, task_id, "review")
    hint = _next_hint(thought, has_plan, has_review)
    events = store.read_events(task_id)
    think_count = sum(1 for e in events if e.get("type") == "think")
    structured = {
        "task_id": task_id,
        "journal": _journal_path(store, task_id),
        "think_count": think_count,
        "next_action": hint["next_action"],
        "suggested_tool": hint["suggested_tool"],
        "reason": hint["reason"],
        "questions": hint["questions"],
        "hints": [hint["reason"]] + ["调用 %s" % hint["suggested_tool"]],
    }
    text = ("已记录观察（task=%s，第 %d 条）。下一步：%s —— %s"
            % (task_id, think_count, hint["suggested_tool"], hint["reason"]))
    return _ok(text, structured)


def _validate_plan_steps(goal, steps):
    """校验计划步骤，返回 ``(normalized_steps, errors)``。"""
    errors = []
    if not goal:
        errors.append({"step": 0, "field": "goal",
                       "message": "缺少 goal：用一句话写清要达成的可验证结果"})
    if not isinstance(steps, list) or not steps:
        errors.append({"step": 0, "field": "steps",
                       "message": "缺少 steps：至少给出一个步骤，"
                                  "例如 [{\"goal\":\"确认容器状态\",\"tool\":\"docker_ps\","
                                  "\"assignee\":\"local\",\"acceptance\":\"列出目标容器且状态为 running\"}]"})
        return [], errors
    normalized = []
    for index, raw in enumerate(steps):
        number = index + 1
        if not isinstance(raw, dict):
            errors.append({"step": number, "field": "step",
                           "message": "步骤 %d 必须是对象（含 goal/tool/assignee/acceptance）"
                                      % number})
            continue
        goal_text = str(raw.get("goal") or raw.get("description") or "").strip()
        tool_text = str(raw.get("tool") or raw.get("action") or "").strip()
        assignee = str(raw.get("assignee") or raw.get("node") or "").strip()
        acceptance = raw.get("acceptance") or raw.get("acceptance_criteria") or ""
        if isinstance(acceptance, (list, tuple)):
            acceptance = "；".join(str(a) for a in acceptance if str(a).strip())
        acceptance = str(acceptance).strip()
        if not goal_text:
            errors.append({"step": number, "field": "goal",
                           "message": "步骤 %d 缺少 goal（这一步要达成什么）" % number})
        if not tool_text:
            errors.append({"step": number, "field": "tool",
                           "message": "步骤 %d 缺少 tool（要调用的工具名，如 sysinfo/exec/"
                                      "peer_call）" % number})
        if not assignee:
            errors.append({"step": number, "field": "assignee",
                           "message": "步骤 %d 缺少 assignee（执行者：local 或对端节点名）"
                                      % number})
        if not acceptance:
            errors.append({"step": number, "field": "acceptance",
                           "message": "步骤 %d 缺少 acceptance（可判定的验收标准，如 "
                                      "\"命令 exit=0 且输出含 running\"）" % number})
        normalized.append({
            "step": number, "goal": goal_text, "tool": tool_text,
            "assignee": assignee or "local", "acceptance": acceptance,
            "args": dict(raw.get("args") or raw.get("arguments") or {}),
            "capability": raw.get("capability"),
        })
    return normalized, errors


def _fleet_plan(args, ctx):
    """fleet_plan：校验并落盘执行计划，返回规范化计划与下一步。"""
    args = args or {}
    task_id = str(args.get("task_id") or "").strip()
    goal = str(args.get("goal") or args.get("objective") or "").strip()
    steps = args.get("steps")
    normalized, errors = _validate_plan_steps(goal, steps)
    if errors:
        example = {"goal": "确认 nas 上 nginx 容器状态", "tool": "docker_ps",
                   "assignee": "nas", "acceptance": "返回容器列表且 nginx 状态为 running"}
        text = ("计划不合法，已拒绝（可操作修复）：\n- "
                + "\n- ".join(item["message"] for item in errors)
                + "\n\n单步示例：" + json.dumps(example, ensure_ascii=False)
                + "\n每个步骤都必须同时具备 goal/tool/assignee/acceptance。")
        return _err(text, {"errors": errors, "example": example,
                           "field": errors[0]["field"]})
    store = _ctx_store(ctx)
    if not task_id:
        task_id = TaskStore.new_task_id()
    _ensure_task(store, task_id, goal=goal, origin="fleet_plan")
    store.append_event(task_id, {
        "type": "plan", "agent": _ctx_agent(ctx), "goal": goal,
        "steps": normalized, "at_ms": now_ms()})
    first = normalized[0]
    suggested = {"tool": first["tool"], "assignee": first["assignee"],
                 "args": first.get("args") or {}, "acceptance": first["acceptance"]}
    structured = {
        "task_id": task_id,
        "goal": goal,
        "plan": normalized,
        "step_count": len(normalized),
        "order": [step["step"] for step in normalized],
        "next_action": "act",
        "suggested_next": suggested,
        "journal": _journal_path(store, task_id),
    }
    lines = ["计划已保存（task=%s，共 %d 步）：" % (task_id, len(normalized))]
    for step in normalized:
        lines.append("  %d. [%s@%s] %s → 验收：%s"
                     % (step["step"], step["tool"], step["assignee"],
                        step["goal"], step["acceptance"]))
    lines.append("下一步：执行第 1 步 %s@%s，成功标准：%s"
                 % (first["tool"], first["assignee"], first["acceptance"]))
    return _ok("\n".join(lines), structured)


def _fleet_reflect(args, ctx):
    """fleet_reflect：记录执行结果与教训，返回升级/收敛建议。"""
    args = args or {}
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _err("缺少 task_id：反思要挂在具体任务上，例如 {\"task_id\":\"t-demo-1\"}。",
                    {"field": "task_id"})
    outcome = str(args.get("outcome") or "").strip().lower()
    if not outcome:
        outcome = "unknown"
    if outcome not in ("success", "partial", "fail", "unknown"):
        return _err("非法 outcome %r：只能是 success / partial / fail / unknown。"
                    % (args.get("outcome"),), {"field": "outcome"})
    lessons = args.get("lessons")
    if isinstance(lessons, str):
        lessons = [lessons]
    if not isinstance(lessons, list):
        lessons = []
    lessons = [str(item) for item in lessons if str(item).strip()][:20]
    what_happened = str(args.get("what_happened")
                        or args.get("summary") or "").strip()
    if not what_happened and not lessons:
        return _err("缺少 what_happened 或 lessons：至少写一句发生了什么，"
                    "例如 \"重启后进程 30s 内退出，日志报 OOM\"。",
                    {"field": "what_happened"})
    next_step = str(args.get("next") or "").strip()
    store = _ctx_store(ctx)
    if not store.has_task(task_id):
        _ensure_task(store, task_id, goal=_truncate(what_happened, 120),
                     origin="fleet_reflect")
    store.append_event(task_id, {
        "type": "reflect", "agent": _ctx_agent(ctx), "outcome": outcome,
        "what_happened": _truncate(redact(what_happened), 1000),
        "lessons": [_truncate(redact(item), 300) for item in lessons],
        "next": _truncate(redact(next_step), 500), "at_ms": now_ms()})
    if outcome == "success":
        plan = {"next_action": "review", "suggested_tool": "fleet_review",
                "escalate": False,
                "reason": "执行成功，逐条验收后再宣布完成"}
    elif outcome == "partial":
        plan = {"next_action": "narrow_scope", "suggested_tool": "fleet_plan",
                "escalate": False,
                "reason": "只完成了一部分，缩小范围或补步骤后继续"}
    elif outcome == "fail":
        plan = {"next_action": "escalate", "suggested_tool": "fleet_handoff",
                "escalate": True,
                "reason": "失败且无自动恢复路径，交给人或备用节点"}
    else:
        plan = {"next_action": "diagnose", "suggested_tool": "fleet_think",
                "escalate": False, "reason": "结果不确定，先补证据再判定"}
    questions = []
    if outcome in ("fail", "unknown"):
        questions = ["是否已同语义重试 1 次？", "同能力备用节点是否在线？",
                     "是否需要人工确认（ask 策略）？"]
    elif outcome == "partial":
        questions = ["已完成部分是否有证据？", "剩余步骤能否拆小？"]
    else:
        questions = ["验收标准是否逐条有证据？", "是否需要通知调用方？"]
    if next_step:
        questions.insert(0, "下一步：%s" % next_step)
    structured = {
        "task_id": task_id,
        "outcome": outcome,
        "next_action": plan["next_action"],
        "suggested_tool": plan["suggested_tool"],
        "escalate": plan["escalate"],
        "reason": plan["reason"],
        "questions": questions,
        "journal": _journal_path(store, task_id),
    }
    text = ("已记录反思（task=%s，outcome=%s）。建议：%s —— %s"
            % (task_id, outcome, plan["suggested_tool"], plan["reason"]))
    return _ok(text, structured)


def _review_evidence_from_store(store, task_id):
    """从任务事件日志重建证据（step 事件 + think 事件）。"""
    evidence = []
    for event in store.read_events(task_id):
        if event.get("type") == "step":
            evidence.append({
                "step": event.get("step"), "tool": event.get("tool"),
                "node": event.get("node"), "ok": event.get("ok"),
                "status": event.get("status"), "detail": event.get("detail"),
                "criterion": event.get("acceptance"),
            })
        elif event.get("type") == "think":
            evidence.append({"step": "think", "tool": "fleet_think",
                             "ok": None, "detail": event.get("thought")})
    return evidence


def _fleet_review(args, ctx):
    """fleet_review：按验收标准逐条判定并与证据绑定。"""
    args = args or {}
    acceptance = args.get("acceptance") or args.get("criteria")
    if acceptance is None or (isinstance(acceptance, (list, tuple))
                              and not acceptance) \
            or (isinstance(acceptance, str) and not acceptance.strip()):
        return _err("缺少 acceptance：给出验收标准（字符串或字符串数组），"
                    "例如 [\"nginx 状态为 running\", \"健康检查返回 200\"]。",
                    {"field": "acceptance"})
    task_id = str(args.get("task_id") or "").strip()
    evidence = args.get("evidence")
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    store = None
    if task_id:
        try:
            store = _ctx_store(ctx)
        except (ValueError, OSError):
            store = None
    if not evidence and store is not None and store.has_task(task_id):
        evidence = _review_evidence_from_store(store, task_id)
    review = review_acceptance(acceptance, evidence)
    structured = {
        "task_id": task_id or None,
        "verdict": review["verdict"],
        "items": review["items"],
        "summary": review["summary"],
        "evidence_count": len(evidence),
    }
    if store is not None and task_id:
        try:
            _ensure_task(store, task_id, goal=review["summary"][:120],
                         origin="fleet_review")
            store.append_event(task_id, {
                "type": "review", "agent": _ctx_agent(ctx),
                "verdict": review["verdict"],
                "items": review["items"], "at_ms": now_ms()})
            structured["journal"] = _journal_path(store, task_id)
        except (KeyError, ValueError, OSError):
            pass
    lines = [review["summary"]]
    for item in review["items"]:
        lines.append("- [%s] %s（证据：%s）"
                     % (item["status"], item["criterion"],
                        "; ".join(item["evidence"]) or "无"))
    if review["verdict"] != "pass":
        lines.append("存在未通过/无法判定项：补充证据后重新 review，"
                     "或调用 fleet_reflect 记录阻塞点。")
    return _ok("\n".join(lines), structured)


def _handoff_policy_verdict(ctx):
    """检查 msg_send 策略，返回 (verdict, reason, raw_policy)。"""
    policy = (ctx or {}).get("policy")
    if policy is None:
        return "allow", "未配置策略", None
    try:
        if hasattr(policy, "check") and callable(policy.check):
            result = policy.check("msg_send", {})
            if isinstance(result, tuple) and len(result) >= 2:
                return str(result[0]).lower(), str(result[1] or ""), policy
            return str(result).lower(), "", policy
        if callable(policy):
            result = policy("msg_send", {})
            if isinstance(result, tuple) and len(result) >= 2:
                return str(result[0]).lower(), str(result[1] or ""), policy
            return str(result).lower(), "", policy
        if isinstance(policy, dict):
            for key in ("deny", "ask"):
                for pattern in policy.get(key) or []:
                    needle = str(pattern).lower()
                    if needle and needle in "msg_send":
                        return key, "命中策略 %s: %s" % (key, needle), policy
            if isinstance(policy.get("tools"), dict):
                value = policy["tools"].get("msg_send")
                if isinstance(value, str):
                    return value.lower(), "工具级策略 msg_send", policy
            return "allow", "未命中策略规则", policy
    except Exception as exc:
        return "deny", "策略检查异常，按拒绝处理: %s" % redact(str(exc)), policy
    return "deny", "无法识别的策略类型，按拒绝处理", policy


def _fleet_handoff(args, ctx):
    """fleet_handoff：生成（并可选投递）结构化 handoff 信封。"""
    args = args or {}
    to = str(args.get("to") or args.get("target") or "").strip()
    goal = str(args.get("goal") or args.get("objective") or "").strip()
    if not to:
        return _err("缺少 to：接收方 agent/节点名，例如 {\"to\":\"nas\"}。",
                    {"field": "to"})
    if not goal:
        return _err("缺少 goal：handoff 必须说明要对方做什么，以及验收标准。",
                    {"field": "goal"})
    task_id = str(args.get("task_id") or "").strip() or None
    thread_id = str(args.get("thread_id") or "").strip() or None
    deadline_s = args.get("deadline_s")
    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return _err("payload 必须是对象（dict）。", {"field": "payload"})
    payload = dict(payload or {})
    payload.setdefault("goal", goal)
    if args.get("acceptance"):
        payload.setdefault("acceptance", args.get("acceptance"))
    if args.get("note"):
        payload.setdefault("note", _truncate(redact(str(args["note"])), 500))
    if args.get("context"):
        payload.setdefault("context", _truncate(redact(str(args["context"])), 1000))
    try:
        envelope = make_envelope("handoff", thread_id, _ctx_agent(ctx), to,
                                 payload, task_id=task_id, deadline_s=deadline_s)
    except ValueError as exc:
        return _err("handoff 信封不合法：%s" % exc, {"field": "envelope"})
    envelope_text = encode_envelope(envelope)
    store = None
    if task_id:
        try:
            store = _ctx_store(ctx)
        except (ValueError, OSError):
            store = None
    verdict, reason, _ = _handoff_policy_verdict(ctx)
    if verdict == "deny":
        return _err("策略拒绝发送 handoff：%s" % (reason or "命中 deny"),
                    {"policy": "deny", "envelope": envelope})
    if verdict == "ask":
        structured = {
            "policy": "ask",
            "status": "awaiting_approval",
            "requires_approval": True,
            "reason": reason or "命中 ask 策略",
            "envelope": envelope,
            "envelope_text": envelope_text,
            "sent": False,
            "next_steps": ["展示批准理由并征得确认",
                           "确认后再次调用 fleet_handoff(send=true)",
                           "或用 policy 放行 msg_send"],
        }
        return _ok("已生成 handoff 信封，但发送需要人工确认：%s\n"
                   "确认后再次调用即可发送。"
                   % (reason or "命中 ask 策略"), structured)
    client = (ctx or {}).get("client")
    should_send = args.get("send")
    if should_send is None:
        should_send = True
    sent = False
    send_text = "仅生成信封（未注入 FleetClient）"
    topic = str(args.get("topic") or "fleet-task")
    if should_send and client is not None:
        try:
            result = client.send_msg(to, envelope_text, topic=topic)
            ok = bool(result.get("ok")) if isinstance(result, dict) else True
            sent = ok
            send_text = (result.get("text") if isinstance(result, dict)
                         else str(result)) or ("已发送" if ok else "发送失败")
            send_text = redact(str(send_text))
        except Exception as exc:
            return _err("handoff 信封已生成，但发送失败：%s\n信封：%s"
                        % (redact(str(exc)), envelope_text),
                        {"envelope": envelope, "sent": False})
    elif not should_send:
        send_text = "按 send=false 只生成信封"
    if store is not None and task_id:
        try:
            _ensure_task(store, task_id, goal=goal, origin="fleet_handoff")
            store.append_event(task_id, {
                "type": "handoff", "agent": _ctx_agent(ctx), "to": to,
                "thread_id": envelope["thread_id"], "sent": sent,
                "goal": _truncate(goal, 300), "at_ms": now_ms()})
        except (KeyError, ValueError, OSError):
            pass
    structured = {
        "task_id": task_id,
        "to": to,
        "thread_id": envelope["thread_id"],
        "envelope": envelope,
        "envelope_text": envelope_text,
        "sent": sent,
        "send_result": send_text,
        "next_steps": ["用 msg_recv 等待对方 progress/result/fail",
                       "超时未回则 fleet_reflect 后升级",
                       "收到结果后 fleet_review 验收"],
    }
    text = ("handoff 已生成：thread=%s，to=%s，goal=%s；发送状态：%s"
            % (envelope["thread_id"], to, goal,
               "已发送" if sent else send_text))
    return _ok(text, structured)


SELF_DIALOGUE_TOOLS = [
    {
        "name": "fleet_help",
        "description": "查看 MCP Fleet 操作手册与下一步建议；topic 可选 "
                       "tools/plan/delegate/review/safety/protocol/onboard。",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string",
                                     "description": "手册章节（可留空看总览）"}},
        },
    },
    {
        "name": "fleet_think",
        "description": "把一条关键观察写入任务日志（JSONL/0600），并返回结构化的下一步建议。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "thought": {"type": "string", "description": "观察/判断（一句话）"},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "可选证据（命令输出摘要等）"},
            },
            "required": ["task_id", "thought"],
        },
    },
    {
        "name": "fleet_plan",
        "description": "校验并保存执行计划：每个步骤必须包含 goal/tool/assignee/acceptance，"
                       "缺一不可；返回规范化计划与下一步。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "可选任务 ID（缺省自动生成）"},
                "goal": {"type": "string", "description": "任务目标（可验证的结果）"},
                "steps": {
                    "type": "array",
                    "description": "步骤数组，每步含 goal/tool/assignee/acceptance/args",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "tool": {"type": "string"},
                            "assignee": {"type": "string"},
                            "acceptance": {"type": "string"},
                            "args": {"type": "object"},
                        },
                    },
                },
            },
            "required": ["goal", "steps"],
        },
    },
    {
        "name": "fleet_reflect",
        "description": "记录执行结果与教训（outcome=success/partial/fail/unknown），"
                       "返回升级或收敛建议。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "outcome": {"type": "string",
                            "enum": ["success", "partial", "fail", "unknown"]},
                "what_happened": {"type": "string"},
                "lessons": {"type": "array", "items": {"type": "string"}},
                "next": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "fleet_review",
        "description": "按验收标准逐条判定 passed/failed/unknown 并绑定证据；"
                       "无证据不臆断通过。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "可选；给出时从任务日志取证据"},
                "acceptance": {"description": "验收标准（字符串或数组）"},
                "evidence": {"type": "array", "items": {"type": "object"},
                             "description": "证据（含 status/result/ok/criterion/detail 等）"},
            },
            "required": ["acceptance"],
        },
    },
    {
        "name": "fleet_handoff",
        "description": "生成结构化 handoff 信封并可投递（msg_send topic=fleet-task）；"
                       "受 policy 的 allow/ask/deny 约束。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "接收方节点/agent 名"},
                "goal": {"type": "string", "description": "要对方完成什么"},
                "task_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "payload": {"type": "object"},
                "acceptance": {"description": "验收标准"},
                "note": {"type": "string"},
                "deadline_s": {"type": "number", "description": "截止秒数"},
                "send": {"type": "boolean", "description": "是否立即投递，默认 true"},
                "topic": {"type": "string", "description": "邮箱主题，默认 fleet-task"},
            },
            "required": ["to", "goal"],
        },
    },
]

SELF_DIALOGUE_FUNCS = {
    "fleet_help": _fleet_help,
    "fleet_think": _fleet_think,
    "fleet_plan": _fleet_plan,
    "fleet_reflect": _fleet_reflect,
    "fleet_review": _fleet_review,
    "fleet_handoff": _fleet_handoff,
}


def dispatch_tool(name, args=None, ctx=None):
    """统一入口：分发 fleet_* 自对话工具，捕获一切异常并返回 MCP 工具结果。

    集成方在 ``mcp_agent.py`` 的工具分发里调用::

        return dispatch_tool(name, args, {"store": store, "client": client,
                                          "policy": policy, "agent": SERVER_NAME})
    """
    func = SELF_DIALOGUE_FUNCS.get(name)
    if func is None:
        return _err("未知工具 %r；可用：%s"
                    % (name, ", ".join(sorted(SELF_DIALOGUE_FUNCS.keys()))))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _err("工具参数必须是 JSON 对象")
    try:
        return func(args, ctx or {})
    except Exception as exc:
        return _err("工具 %s 执行失败：%s" % (name, redact(str(exc))),
                    {"tool": name})


__all__ = [
    "VERSION", "MAX_INSTRUCTIONS", "ENVELOPE_KINDS",
    "build_instructions", "PROMPTS", "list_prompts", "get_prompt",
    "SELF_DIALOGUE_TOOLS", "SELF_DIALOGUE_FUNCS", "dispatch_tool",
    "TaskStore", "make_envelope", "parse_envelope", "validate_envelope",
    "encode_envelope", "envelope_expired", "FleetClient", "Orchestrator",
    "LLMAdapter", "LLMError", "normalize_tool_result", "review_acceptance",
    "build_caps_index", "redact", "now_ms", "atomic_write", "append_jsonl",
    "read_jsonl",
]
