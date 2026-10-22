# -*- coding: utf-8 -*-
"""fleet_agent（v5.2 Agent 智能层）测试。

覆盖：instructions 长度/关键内容、prompts 完整性与 MCP 结构、fleet_plan 校验、
think/reflect 持久化、任务存储原子写/并发、信封校验与向后兼容、FleetClient 路由、
Orchestrator（诊断→委派→汇总、预算上限、重试升级、策略 allow/ask/deny、LLM 降级）、
LLMAdapter（mock，不联网）、脱敏。
"""
import json
import os
import stat
import threading

import pytest

fa = pytest.importorskip("fleet_agent", reason="fleet_agent 导入失败")


# ===========================================================================
# 辅助
# ===========================================================================
@pytest.fixture()
def store(tmp_path):
    return fa.TaskStore(str(tmp_path / "agent"))


@pytest.fixture()
def ctx(store):
    return {"store": store, "agent": "mac"}


class FakeTools(object):
    """模拟 call_tool(name, args)：记录调用，按工具名返回预设结果。"""

    def __init__(self, results=None, fail=None):
        self.calls = []
        self.results = results or {}
        self.fail = fail or {}

    def __call__(self, name, args):
        self.calls.append((name, args))
        if name in self.fail:
            raise RuntimeError(self.fail[name])
        value = self.results.get(name, "ok")
        if callable(value):
            return value(args)
        return value

    def names(self):
        return [call[0] for call in self.calls]


def make_client(results=None, fail=None, name="mac"):
    handler = FakeTools(results=results, fail=fail)
    return fa.FleetClient(handler, name=name), handler


class FakeLLM(object):
    """可编程 LLM：返回固定文本或抛异常。"""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.text


# ===========================================================================
# 一、build_instructions
# ===========================================================================
class TestInstructions:
    def test_length_under_limit(self):
        text = fa.build_instructions({"name": "nas"})
        assert 0 < len(text) <= fa.MAX_INSTRUCTIONS
        assert fa.MAX_INSTRUCTIONS == 6000

    def test_required_sections_and_keywords(self):
        text = fa.build_instructions()
        for keyword in ("工具选择矩阵", "安全边界", "自对话", "Observe", "Plan",
                        "Act", "Reflect", "跨设备委派", "失败升级", "常见任务模板",
                        "结构化消息", "fleet_plan", "fleet_review"):
            assert keyword in text, "instructions 缺少: %s" % keyword

    def test_includes_node_name_and_prefix(self):
        text = fa.build_instructions({"name": "nas-01", "prefix": "fleet_"})
        assert "nas-01" in text
        assert "`fleet_exec`" in text
        assert "fleet_plan" in text

    def test_peers_line(self):
        text = fa.build_instructions({"name": "mac", "peers": ["nas", "pi"]})
        assert "已知对等节点" in text
        assert "nas" in text and "pi" in text

    def test_read_only_note(self):
        text = fa.build_instructions({"name": "ro", "read_only": True})
        assert "只读模式" in text

    def test_deterministic(self):
        cfg = {"name": "nas", "peers": ["pi"], "prefix": ""}
        assert fa.build_instructions(cfg) == fa.build_instructions(cfg)

    def test_none_cfg_ok(self):
        text = fa.build_instructions(None)
        assert "MCP Fleet" in text and len(text) <= fa.MAX_INSTRUCTIONS

    def test_long_cfg_is_clamped(self):
        cfg = {"name": "n" * 500, "prefix": "p" * 500,
               "peers": ["peer-%03d" % i for i in range(500)]}
        text = fa.build_instructions(cfg)
        assert len(text) <= fa.MAX_INSTRUCTIONS
        assert text.rstrip().endswith("。") or "agent-playbook.md" in text


# ===========================================================================
# 二、PROMPTS / list_prompts / get_prompt
# ===========================================================================
REQUIRED_PROMPTS = {"fleet-playbook", "fleet-diagnose", "fleet-plan",
                    "fleet-self-review", "fleet-delegate", "fleet-incident",
                    "fleet-onboard"}


class TestPrompts:
    def test_required_prompts_present(self):
        assert REQUIRED_PROMPTS <= set(fa.PROMPTS.keys())
        assert len(fa.PROMPTS) >= 7

    def test_prompt_structure(self):
        for name, spec in fa.PROMPTS.items():
            assert isinstance(spec.get("description"), str) and spec["description"]
            assert isinstance(spec.get("arguments"), list)
            for arg in spec["arguments"]:
                assert isinstance(arg["name"], str) and arg["name"]
                assert isinstance(arg.get("required"), bool)
            assert isinstance(spec.get("template"), str)
            assert spec["template"].strip()
            assert "fleet_" in spec["template"]

    def test_list_prompts_shape(self):
        prompts = fa.list_prompts()
        assert isinstance(prompts, list)
        names = [item["name"] for item in prompts]
        assert names == sorted(names)
        assert set(names) == set(fa.PROMPTS.keys())
        for item in prompts:
            assert set(item.keys()) == {"name", "description", "arguments"}
            assert isinstance(item["arguments"], list)

    def test_get_prompt_messages_shape(self):
        result = fa.get_prompt("fleet-playbook", {"goal": "巡检"})
        assert isinstance(result["description"], str)
        messages = result["messages"]
        assert isinstance(messages, list) and len(messages) == 1
        message = messages[0]
        assert message["role"] == "user"
        assert message["content"]["type"] == "text"
        assert "巡检" in message["content"]["text"]

    def test_get_prompt_renders_all_args(self):
        result = fa.get_prompt("fleet-diagnose", {
            "symptom": "nginx 502", "node": "nas", "since": "10m",
            "scope": "container"})
        text = result["messages"][0]["content"]["text"]
        for value in ("nginx 502", "nas", "10m", "container"):
            assert value in text
        assert "{{" not in text

    def test_missing_required_argument_raises(self):
        with pytest.raises(ValueError) as excinfo:
            fa.get_prompt("fleet-diagnose", {})
        assert "symptom" in str(excinfo.value)

    def test_unknown_prompt_raises(self):
        with pytest.raises(KeyError) as excinfo:
            fa.get_prompt("fleet-nope", {})
        assert "fleet-playbook" in str(excinfo.value)

    def test_all_prompts_render_without_leftover_placeholders(self):
        for name, spec in fa.PROMPTS.items():
            args = {arg["name"]: "v" for arg in spec["arguments"]}
            result = fa.get_prompt(name, args)
            text = result["messages"][0]["content"]["text"]
            assert "{{" not in text
            assert text.strip()


# ===========================================================================
# 三、TaskStore
# ===========================================================================
class TestTaskStore:
    def test_create_get_roundtrip(self, store):
        created = store.create_task({"goal": "检查容器", "owner": "mac"})
        assert created["task_id"]
        assert created["status"] == "open"
        loaded = store.get_task(created["task_id"])
        assert loaded["goal"] == "检查容器"
        assert loaded["owner"] == "mac"
        assert loaded["created_ms"] > 0

    def test_auto_task_id_unique(self, store):
        first = store.create_task({"goal": "a"})
        second = store.create_task({"goal": "b"})
        assert first["task_id"] != second["task_id"]

    def test_duplicate_task_id_rejected(self, store):
        store.create_task({"task_id": "t-dup", "goal": "a"})
        with pytest.raises(ValueError):
            store.create_task({"task_id": "t-dup", "goal": "b"})

    def test_missing_task_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.get_task("t-missing")

    def test_path_traversal_rejected(self, store):
        with pytest.raises(ValueError):
            store.create_task({"task_id": "../escape", "goal": "x"})
        with pytest.raises(ValueError):
            store.get_task("../../etc/passwd")
        assert not store.has_task("../escape")

    def test_append_and_read_events(self, store):
        task = store.create_task({"goal": "g"})
        store.append_event(task["task_id"], {"type": "think", "thought": "观察"})
        store.append_event(task["task_id"], {"type": "plan", "steps": []})
        events = store.read_events(task["task_id"])
        types = [event["type"] for event in events]
        assert types == ["created", "think", "plan"]
        assert events[-1]["steps"] == []
        assert all(event["task_id"] == task["task_id"] for event in events)

    def test_append_to_missing_task_raises(self, store):
        with pytest.raises(KeyError):
            store.append_event("t-missing", {"type": "think"})

    def test_file_and_dir_modes(self, store):
        task = store.create_task({"goal": "g"})
        task_path = os.path.join(store.tasks_dir, task["task_id"] + ".json")
        events_path = os.path.join(store.tasks_dir,
                                   task["task_id"] + ".events.jsonl")
        assert stat.S_IMODE(os.stat(task_path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(events_path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(store.base_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(store.tasks_dir).st_mode) == 0o700

    def test_list_and_filter(self, store):
        one = store.create_task({"goal": "one"})
        store.create_task({"goal": "two"})
        store.update_task(one["task_id"], {"status": "completed"})
        assert len(store.list_tasks()) == 2
        completed = store.list_tasks(status="completed")
        assert [task["task_id"] for task in completed] == [one["task_id"]]

    def test_update_task_merges(self, store):
        task = store.create_task({"goal": "g"})
        updated = store.update_task(task["task_id"], {"status": "completed",
                                                      "result": "ok"})
        assert updated["status"] == "completed"
        assert updated["result"] == "ok"
        assert updated["goal"] == "g"

    def test_concurrent_appends_do_not_lose_lines(self, store):
        task = store.create_task({"goal": "g"})
        task_id = task["task_id"]
        threads = []

        def worker(index):
            for seq in range(20):
                store.append_event(task_id, {"type": "race",
                                             "worker": index, "seq": seq})

        for index in range(6):
            thread = threading.Thread(target=worker, args=(index,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        events = store.read_events(task_id)
        race = [event for event in events if event.get("type") == "race"]
        assert len(race) == 120

    def test_no_temp_files_left(self, store):
        task = store.create_task({"goal": "g"})
        for index in range(5):
            store.append_event(task["task_id"], {"type": "x", "i": index})
        leftovers = [name for name in os.listdir(store.tasks_dir)
                     if name.endswith(".tmp")]
        assert leftovers == []


# ===========================================================================
# 四、信封协议
# ===========================================================================
class TestEnvelope:
    @pytest.mark.parametrize("kind", fa.ENVELOPE_KINDS)
    def test_all_kinds_roundtrip(self, kind):
        env = fa.make_envelope(kind, "th-1", "mac", "nas", {"goal": "g"},
                               task_id="t-1", deadline_s=60)
        ok, error = fa.validate_envelope(env)
        assert ok, error
        assert env["kind"] == kind
        assert env["thread_id"] == "th-1"
        assert env["payload"] == {"goal": "g"}
        assert env["deadline_ms"] > env["created_ms"]

    def test_validate_missing_version(self):
        ok, error = fa.validate_envelope({"kind": "propose", "thread_id": "th"})
        assert not ok and "v" in error

    def test_validate_missing_kind(self):
        ok, error = fa.validate_envelope({"v": "5.2.4", "thread_id": "th",
                                          "from": "a", "to": "b", "payload": {}})
        assert not ok and "kind" in error

    def test_validate_invalid_kind(self):
        ok, error = fa.validate_envelope({"v": "5.2.4", "kind": "bogus",
                                          "thread_id": "th", "from": "a",
                                          "to": "b", "payload": {}})
        assert not ok and "kind" in error

    def test_validate_missing_thread(self):
        ok, error = fa.validate_envelope({"v": "5.2.4", "kind": "propose",
                                          "from": "a", "to": "b", "payload": {}})
        assert not ok and "thread_id" in error

    def test_validate_bad_names(self):
        base = {"v": "5.2.4", "kind": "propose", "thread_id": "th-1",
                "from": "a b", "to": "b", "payload": {}}
        ok, error = fa.validate_envelope(base)
        assert not ok and "from" in error
        base["from"] = "a"
        base["to"] = "../evil"
        ok, error = fa.validate_envelope(base)
        assert not ok and "to" in error

    def test_validate_broadcast_to(self):
        env = fa.make_envelope("progress", "th-1", "mac", "*", {"pct": 50})
        assert fa.validate_envelope(env)[0]

    def test_validate_payload_must_be_dict(self):
        ok, error = fa.validate_envelope({"v": "5.2.4", "kind": "propose",
                                          "thread_id": "th", "from": "a",
                                          "to": "b", "payload": []})
        assert not ok and "payload" in error

    def test_validate_invalid_task_id(self):
        ok, error = fa.validate_envelope({"v": "5.2.4", "kind": "propose",
                                          "thread_id": "th", "from": "a",
                                          "to": "b", "payload": {},
                                          "task_id": "../x"})
        assert not ok and "task_id" in error

    def test_make_envelope_rejects_bad_input(self):
        with pytest.raises(ValueError):
            fa.make_envelope("bogus", "th", "a", "b", {})
        with pytest.raises(ValueError):
            fa.make_envelope("propose", "th", "a", "b", "not-dict")
        with pytest.raises(ValueError):
            fa.make_envelope("propose", "th", "bad name", "b", {})
        with pytest.raises(ValueError):
            fa.make_envelope("propose", "th", "a", "b", {}, deadline_s=-5)

    def test_make_envelope_auto_thread(self):
        env = fa.make_envelope("hello", None, "mac", "nas", {})
        assert env["thread_id"].startswith("th-")

    def test_expired(self):
        env = fa.make_envelope("propose", "th-1", "a", "b", {}, deadline_s=1)
        assert not fa.envelope_expired(env)
        assert fa.envelope_expired(env, at_ms=env["deadline_ms"] + 1)
        assert not fa.envelope_expired(fa.make_envelope("hello", "th-2", "a", "b", {}))

    def test_parse_plain_text_backward_compatible(self):
        parsed = fa.parse_envelope("你好，检查一下 nas")
        assert parsed["kind"] == "text"
        assert parsed["text"] == "你好，检查一下 nas"

    def test_parse_empty(self):
        assert fa.parse_envelope("")["kind"] == "text"
        assert fa.parse_envelope(None)["kind"] == "text"

    def test_parse_non_envelope_json_is_text(self):
        raw = json.dumps({"host": "mac", "load": 0.2})
        parsed = fa.parse_envelope(raw)
        assert parsed["kind"] == "text"
        assert parsed["text"] == raw

    def test_parse_valid_envelope(self):
        env = fa.make_envelope("propose", "th-9", "mac", "nas",
                               {"goal": "巡检"})
        parsed = fa.parse_envelope(json.dumps(env, ensure_ascii=False))
        assert parsed["kind"] == "propose"
        assert parsed["payload"]["goal"] == "巡检"
        assert parsed["thread_id"] == "th-9"

    def test_parse_invalid_envelope_reports_error(self):
        raw = json.dumps({"kind": "bogus", "thread_id": "th-1"})
        parsed = fa.parse_envelope(raw)
        assert parsed["kind"] == "invalid"
        assert parsed["error"]
        assert parsed["raw"] == raw

    def test_encode_roundtrip(self):
        env = fa.make_envelope("result", "th-3", "nas", "mac",
                               {"ok": True}, task_id="t-3")
        text = fa.encode_envelope(env)
        assert "\n" not in text
        assert fa.parse_envelope(text)["kind"] == "result"


# ===========================================================================
# 五、FleetClient / 结果归一化 / 脱敏
# ===========================================================================
class TestFleetClient:
    def test_caps_normalized(self):
        client, handler = make_client({"caps": json.dumps({"docker": True})})
        result = client.caps()
        assert result["ok"] is True
        assert result["tool"] == "caps"
        assert "docker" in result["text"]

    def test_exec_on_local_uses_exec(self):
        client, handler = make_client({"exec": "ok exit=0"})
        result = client.exec_on("local", "uptime")
        assert result["ok"] is True
        assert handler.calls[0][0] == "exec"
        assert handler.calls[0][1]["command"] == "uptime"

    def test_exec_on_remote_uses_peer_call(self):
        client, handler = make_client({"peer_call": "remote ok"})
        result = client.exec_on("nas", "docker ps", timeout=30)
        assert result["ok"] is True
        name, args = handler.calls[0]
        assert name == "peer_call"
        assert args["peer"] == "nas"
        assert args["tool"] == "exec"
        assert args["arguments"]["command"] == "docker ps"

    def test_call_routes_local_and_remote(self):
        client, handler = make_client({"sysinfo": "host", "peer_call": "remote"})
        assert client.call("local", "sysinfo", {})["ok"] is True
        assert handler.calls[-1][0] == "sysinfo"
        assert client.call("nas", "sysinfo", {})["ok"] is True
        assert handler.calls[-1][0] == "peer_call"
        assert client.call("nas", "sysinfo", {})["node"] == "nas"

    def test_send_and_recv_msg(self):
        client, handler = make_client({"msg_send": "已发送", "msg_recv": "无消息"})
        assert client.send_msg("nas", "hi", topic="fleet-task")["ok"] is True
        name, args = handler.calls[0]
        assert name == "msg_send"
        assert args == {"from": "mac", "to": "nas", "text": "hi",
                        "topic": "fleet-task"}
        assert client.recv_msg(wait=0)["ok"] is True
        assert handler.calls[1][1]["me"] == "mac"

    def test_normalize_tuple_error(self):
        result = fa.normalize_tool_result(("错误: 连接失败", True))
        assert result["ok"] is False
        assert "连接失败" in result["text"]

    def test_normalize_mcp_content(self):
        value = {"content": [{"type": "text", "text": "hello"}],
                 "isError": False}
        result = fa.normalize_tool_result(value)
        assert result["ok"] is True and result["text"] == "hello"
        value["isError"] = True
        assert fa.normalize_tool_result(value)["ok"] is False

    def test_normalize_error_string(self):
        assert fa.normalize_tool_result("错误: 超时")["ok"] is False
        assert fa.normalize_tool_result("ok all good")["ok"] is True

    def test_call_tool_exception_is_redacted(self):
        client = fa.FleetClient(FakeTools(fail={"exec": "boom token=abcd1234"}))
        with pytest.raises(RuntimeError) as excinfo:
            client.exec_on("local", "uptime")
        message = str(excinfo.value)
        assert "abcd1234" not in message

    def test_redact_patterns(self):
        text = ("token=abcdef123 password: hunter2 Bearer abc.def sk-1234567890ab "
                "fleet://join/QUJDRA==")
        cleaned = fa.redact(text)
        for secret in ("abcdef123", "hunter2", "abc.def", "sk-1234567890ab",
                       "QUJDRA=="):
            assert secret not in cleaned


# ===========================================================================
# 六、自对话工具
# ===========================================================================
class TestSelfDialogueTools:
    def test_registry(self):
        names = [tool["name"] for tool in fa.SELF_DIALOGUE_TOOLS]
        assert names == ["fleet_help", "fleet_think", "fleet_plan",
                         "fleet_reflect", "fleet_review", "fleet_handoff"]
        assert set(names) == set(fa.SELF_DIALOGUE_FUNCS.keys())
        for tool in fa.SELF_DIALOGUE_TOOLS:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"

    def test_dispatch_unknown(self):
        result = fa.dispatch_tool("fleet_nope", {}, {})
        assert result["isError"] is True

    def test_dispatch_never_raises_on_garbage(self, ctx):
        for name in fa.SELF_DIALOGUE_FUNCS:
            result = fa.dispatch_tool(name, {"task_id": 1, "steps": "x",
                                             "acceptance": 3}, ctx)
            assert "content" in result and "structuredContent" in result

    def test_help(self, ctx):
        result = fa.dispatch_tool("fleet_help", {"topic": "delegate"}, ctx)
        assert result["isError"] is False
        assert "peer_call" in result["content"][0]["text"]
        structured = result["structuredContent"]
        assert "delegate" in structured["topics"]
        assert structured["suggested_next"]["suggested_tool"] == "fleet_plan"

    def test_plan_valid(self, ctx):
        result = fa.dispatch_tool("fleet_plan", {
            "task_id": "t-plan-1", "goal": "确认 nginx 状态",
            "steps": [
                {"goal": "看容器", "tool": "docker_ps", "assignee": "nas",
                 "acceptance": "nginx 状态为 running"},
                {"goal": "看日志", "tool": "docker_logs", "assignee": "nas",
                 "acceptance": "日志无 ERROR", "args": {"name": "nginx"}},
            ]}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["task_id"] == "t-plan-1"
        assert structured["step_count"] == 2
        assert structured["next_action"] == "act"
        assert structured["suggested_next"]["tool"] == "docker_ps"
        events = ctx["store"].read_events("t-plan-1")
        assert any(event["type"] == "plan" for event in events)

    @pytest.mark.parametrize("bad_step,field", [
        ({"tool": "exec", "assignee": "local", "acceptance": "ok"}, "goal"),
        ({"goal": "g", "assignee": "local", "acceptance": "ok"}, "tool"),
        ({"goal": "g", "tool": "exec", "acceptance": "ok"}, "assignee"),
        ({"goal": "g", "tool": "exec", "assignee": "local"}, "acceptance"),
    ])
    def test_plan_rejects_incomplete_step(self, ctx, bad_step, field):
        result = fa.dispatch_tool("fleet_plan", {
            "goal": "g", "steps": [bad_step]}, ctx)
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert field in text
        assert "示例" in text
        assert result["structuredContent"]["errors"][0]["field"] == field

    def test_plan_rejects_empty_steps(self, ctx):
        result = fa.dispatch_tool("fleet_plan", {"goal": "g", "steps": []}, ctx)
        assert result["isError"] is True
        assert result["structuredContent"]["errors"][0]["field"] == "steps"

    def test_plan_rejects_missing_goal(self, ctx):
        result = fa.dispatch_tool("fleet_plan", {
            "steps": [{"goal": "a", "tool": "sysinfo", "assignee": "local",
                       "acceptance": "ok"}]}, ctx)
        assert result["isError"] is True
        assert result["structuredContent"]["errors"][0]["field"] == "goal"

    def test_think_persists_and_hints(self, ctx):
        result = fa.dispatch_tool("fleet_think", {
            "task_id": "t-think-1", "thought": "容器 5 分钟内重启 3 次，报 OOM",
            "evidence": ["docker logs: OOMKilled"]}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["next_action"] == "reflect"
        assert structured["suggested_tool"] == "fleet_reflect"
        journal = structured["journal"]
        assert os.path.exists(journal)
        assert stat.S_IMODE(os.stat(journal).st_mode) == 0o600
        events = ctx["store"].read_events("t-think-1")
        assert [event["type"] for event in events].count("think") == 1

    def test_think_appends_each_call(self, ctx):
        for index in range(2):
            fa.dispatch_tool("fleet_think", {
                "task_id": "t-think-2", "thought": "观察 %d" % index}, ctx)
        events = ctx["store"].read_events("t-think-2")
        assert [e["type"] for e in events].count("think") == 2
        second = fa.dispatch_tool("fleet_think", {
            "task_id": "t-think-2", "thought": "再看一眼"}, ctx)
        assert second["structuredContent"]["think_count"] == 3

    def test_think_missing_args(self, ctx):
        assert fa.dispatch_tool("fleet_think", {"task_id": "t"}, ctx)["isError"]
        result = fa.dispatch_tool("fleet_think", {"thought": "x"}, ctx)
        assert result["isError"] is True
        assert result["structuredContent"]["field"] == "task_id"

    def test_reflect_persists_and_escalates(self, ctx):
        result = fa.dispatch_tool("fleet_reflect", {
            "task_id": "t-ref-1", "outcome": "fail",
            "what_happened": "重启后 30s 内再次退出",
            "lessons": ["先看日志再重启", "需要更多内存"]}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["outcome"] == "fail"
        assert structured["escalate"] is True
        assert structured["next_action"] == "escalate"
        events = ctx["store"].read_events("t-ref-1")
        reflect = [event for event in events if event["type"] == "reflect"]
        assert len(reflect) == 1
        assert reflect[0]["outcome"] == "fail"
        assert reflect[0]["lessons"] == ["先看日志再重启", "需要更多内存"]

    def test_reflect_success_next_review(self, ctx):
        result = fa.dispatch_tool("fleet_reflect", {
            "task_id": "t-ref-2", "outcome": "success",
            "what_happened": "服务恢复"}, ctx)
        assert result["structuredContent"]["suggested_tool"] == "fleet_review"

    def test_reflect_invalid_outcome(self, ctx):
        result = fa.dispatch_tool("fleet_reflect", {
            "task_id": "t-ref-3", "outcome": "weird",
            "what_happened": "x"}, ctx)
        assert result["isError"] is True
        assert result["structuredContent"]["field"] == "outcome"

    def test_reflect_requires_content(self, ctx):
        result = fa.dispatch_tool("fleet_reflect", {
            "task_id": "t-ref-4", "outcome": "fail"}, ctx)
        assert result["isError"] is True

    def test_review_pass(self, ctx):
        result = fa.dispatch_tool("fleet_review", {
            "acceptance": ["nginx running", "健康检查 200"],
            "evidence": [
                {"criterion": "nginx running", "status": "passed",
                 "detail": "Up 3 hours"},
                {"criterion": "健康检查 200", "result": "pass"},
            ]}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["verdict"] == "pass"
        assert [item["status"] for item in structured["items"]] == \
            ["passed", "passed"]
        assert all(item["evidence"] for item in structured["items"])

    def test_review_fail_and_unknown(self, ctx):
        result = fa.dispatch_tool("fleet_review", {
            "acceptance": ["a", "b", "c"],
            "evidence": [
                {"criterion": "a", "ok": False, "detail": "exit=1"},
                {"criterion": "b", "ok": True},
            ]}, ctx)
        structured = result["structuredContent"]
        assert structured["verdict"] == "fail"
        statuses = [item["status"] for item in structured["items"]]
        assert statuses == ["failed", "passed", "unknown"]
        assert "没有对应证据" in structured["items"][2]["reason"]

    def test_review_no_evidence_does_not_pass(self, ctx):
        result = fa.dispatch_tool("fleet_review", {
            "acceptance": ["服务可用"]}, ctx)
        assert result["structuredContent"]["verdict"] == "inconclusive"
        assert result["structuredContent"]["items"][0]["status"] == "unknown"

    def test_review_requires_acceptance(self, ctx):
        result = fa.dispatch_tool("fleet_review", {}, ctx)
        assert result["isError"] is True
        assert result["structuredContent"]["field"] == "acceptance"

    def test_review_from_task_journal(self, ctx):
        client = fa.FleetClient(FakeTools({
            "sysinfo": json.dumps({"host": "mac"})}), name="mac")
        orch = fa.Orchestrator(ctx["store"], client)
        run = orch.run({"goal": "巡检"})
        result = fa.dispatch_tool("fleet_review", {
            "task_id": run["task_id"],
            "acceptance": ["sysinfo 返回主机/负载/内存/磁盘信息"]}, ctx)
        structured = result["structuredContent"]
        assert structured["evidence_count"] >= 1
        assert structured["verdict"] == "pass"

    def test_handoff_builds_and_sends(self, ctx):
        handler = FakeTools({"msg_send": "已发送(在线) -> nas"})
        ctx["client"] = fa.FleetClient(handler, name="mac")
        result = fa.dispatch_tool("fleet_handoff", {
            "to": "nas", "goal": "检查 nginx 为何重启", "task_id": "t-ho-1",
            "deadline_s": 300, "acceptance": "返回 exit=0 与日志摘要"}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["sent"] is True
        assert structured["thread_id"].startswith("th-")
        envelope = structured["envelope"]
        assert envelope["kind"] == "handoff"
        assert envelope["to"] == "nas"
        assert envelope["payload"]["goal"] == "检查 nginx 为何重启"
        assert envelope["deadline_ms"] > envelope["created_ms"]
        assert handler.calls[0][0] == "msg_send"
        assert handler.calls[0][1]["topic"] == "fleet-task"
        parsed = fa.parse_envelope(handler.calls[0][1]["text"])
        assert parsed["kind"] == "handoff"
        events = ctx["store"].read_events("t-ho-1")
        assert any(event["type"] == "handoff" for event in events)

    def test_handoff_policy_ask_does_not_send(self, ctx):
        handler = FakeTools()
        ctx["client"] = fa.FleetClient(handler, name="mac")
        ctx["policy"] = {"ask": ["msg_send"]}
        result = fa.dispatch_tool("fleet_handoff", {
            "to": "nas", "goal": "重启容器"}, ctx)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["requires_approval"] is True
        assert structured["sent"] is False
        assert handler.calls == []

    def test_handoff_without_client(self, ctx):
        result = fa.dispatch_tool("fleet_handoff", {
            "to": "nas", "goal": "检查"}, ctx)
        assert result["isError"] is False
        assert result["structuredContent"]["sent"] is False
        assert result["structuredContent"]["envelope"]["kind"] == "handoff"

    def test_handoff_missing_required(self, ctx):
        assert fa.dispatch_tool("fleet_handoff", {"goal": "g"}, ctx)["isError"]
        assert fa.dispatch_tool("fleet_handoff", {"to": "nas"}, ctx)["isError"]


# ===========================================================================
# 七、Orchestrator
# ===========================================================================
class TestOrchestrator:
    def test_diagnose_delegate_summarize(self, store):
        results = {
            "sysinfo": json.dumps({"host": "mac", "load": 0.3}),
            "peer_call": "CONTAINER nginx Up 3 hours",
        }
        handler = FakeTools(results)
        client = fa.FleetClient(handler, name="mac")
        orch = fa.Orchestrator(store, client, caps_index={"docker": ["nas"]})
        run = orch.run({"goal": "检查 nginx 容器", "capability": "docker",
                        "command": "docker ps"})

        assert run["status"] == "completed"
        assert run["plan_source"] == "deterministic"
        assert [step["tool"] for step in run["steps"]] == \
            ["sysinfo", "exec", "summary"]
        assert [step["node"] for step in run["steps"]] == \
            ["local", "nas", "local"]
        assert all(step["ok"] for step in run["steps"])
        assert run["delegated"] == ["nas"]
        assert "nginx" in run["result"]["summary"]

        orchestration = [(name, args) for name, args in handler.calls]
        assert orchestration[0][0] == "sysinfo"
        peer_names = [args["peer"] for name, args in orchestration
                      if name == "peer_call"]
        assert peer_names == ["nas"]
        remote = [args for name, args in orchestration if name == "peer_call"][0]
        assert remote["arguments"]["command"] == "docker ps"

        saved = store.get_task(run["task_id"])
        assert saved["status"] == "completed"
        event_types = [event["type"] for event in
                       store.read_events(run["task_id"])]
        assert event_types[0] == "created"
        assert "plan" in event_types and "finish" in event_types
        assert event_types.count("step") == 3
        assert run["result"]["outputs"]
        assert run["review"]["verdict"] == "pass"

    def test_dry_run_never_executes(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["nas"]})
        run = orch.run({"goal": "g", "capability": "docker"}, dry_run=True)
        assert run["status"] == "planned"
        assert len(run["steps"]) >= 3
        assert all(step["status"] == "planned" for step in run["steps"])
        assert handler.calls == []

    def test_max_steps_budget(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["nas"]}, max_steps=1)
        run = orch.run({"goal": "g", "capability": "docker"})
        assert run["limited"] is True
        assert run["status"] == "partial"
        assert len(run["steps"]) == 1
        assert any("max_steps" in note["note"] for note in run["reflections"])

    def test_time_budget(self, store):
        ticks = iter([0.0, 100.0])
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               max_seconds=10, clock=lambda: next(ticks))
        run = orch.run({"goal": "g", "capability": "docker", "node": "nas"})
        assert run["limited"] is True
        assert run["steps"] == []
        assert handler.calls == []
        assert any("max_seconds" in note["note"] for note in run["reflections"])

    def test_capability_routing_without_index_falls_back_local(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"))
        run = orch.run({"goal": "g", "capability": "gpu", "command": "nvidia-smi"})
        notes = [note["note"] for note in run["reflections"]]
        assert any("降级本机" in note for note in notes)
        exec_nodes = [step["node"] for step in run["steps"]
                      if step["tool"] == "exec"]
        assert exec_nodes == ["local"]

    def test_retry_then_success(self, store):
        state = {"count": 0}

        def flaky(args):
            state["count"] += 1
            if state["count"] == 1:
                raise RuntimeError("临时网络抖动")
            return "exit=0 all good"

        handler = FakeTools({"exec": flaky})
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"))
        run = orch.run({"goal": "g", "command": "uptime"})
        assert run["status"] == "completed"
        exec_steps = [step for step in run["steps"] if step["tool"] == "exec"]
        assert exec_steps[0]["attempts"] == 2
        assert any("重试" in note["note"] for note in run["reflections"])

    def test_escalation_to_backup_node(self, store):
        def peer_result(args):
            if args["peer"] == "bad":
                return "错误: 连接 bad 超时", True
            return "CONTAINER nginx Up", False

        handler = FakeTools({"peer_call": peer_result})
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["bad", "nas"]})
        run = orch.run({"goal": "g", "capability": "docker",
                        "command": "docker ps"})
        assert run["status"] == "completed"
        assert run["delegated"] == ["bad", "nas"]
        exec_steps = [step for step in run["steps"] if step["tool"] == "exec"]
        assert exec_steps[0]["node"] == "nas"
        assert exec_steps[0]["escalated_from"] == "bad"
        assert any("升级到备用节点" in note["note"]
                   for note in run["reflections"])

    def test_policy_deny_blocks_execution(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               policy={"deny": ["sysinfo"]})
        run = orch.run({"goal": "g"})
        assert run["status"] == "failed"
        assert run["steps"][0]["status"] == "denied"
        assert handler.calls == []

    def test_policy_ask_returns_pending_approval(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["nas"]},
                               policy={"ask": ["peer_call"]})
        run = orch.run({"goal": "g", "capability": "docker",
                        "command": "docker ps"})
        assert run["status"] == "awaiting_approval"
        approval = run["pending_approval"]
        assert approval["tool"] == "exec"
        assert approval["node"] == "nas"
        assert approval["args"]["command"] == "docker ps"
        assert [name for name, _args in handler.calls] == ["sysinfo"]
        assert store.get_task(run["task_id"])["status"] == "awaiting_approval"

    def test_policy_object_and_callable(self, store):
        class AskPolicy(object):
            def check(self, tool, args):
                if tool == "sysinfo":
                    return ("ask", "观察也需确认")
                return ("allow", "")

        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               policy=AskPolicy())
        run = orch.run({"goal": "g"})
        assert run["status"] == "awaiting_approval"
        assert handler.calls == []

        orch.policy = lambda tool, args: ("deny", "全部拒绝")
        run2 = orch.run({"goal": "g2"})
        assert run2["status"] == "failed"

    def test_resume_continues_after_approval(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["nas"]},
                               policy={"ask": ["peer_call"]})
        run = orch.run({"goal": "g", "capability": "docker",
                        "command": "docker ps"})
        assert run["status"] == "awaiting_approval"
        orch.policy = None
        resumed = orch.resume(run["task_id"])
        assert resumed["status"] == "completed"
        statuses = [(step["step"], step["status"]) for step in resumed["steps"]]
        assert statuses[0] == (1, "skipped")
        assert [name for name, _args in handler.calls].count("sysinfo") == 1

    def test_explicit_steps_are_used(self, store):
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"))
        run = orch.run({"goal": "g", "steps": [
            {"goal": "看本机", "tool": "sysinfo", "assignee": "local",
             "acceptance": "有输出"},
            {"goal": "远端执行", "tool": "exec", "assignee": "nas",
             "acceptance": "exit=0", "args": {"command": "uptime"}},
        ]})
        assert run["plan_source"] == "explicit"
        assert [step["tool"] for step in run["steps"]] == ["sysinfo", "exec"]
        assert run["delegated"] == ["nas"]
        assert run["status"] == "completed"

    def test_task_acceptance_reviewed(self, store):
        handler = FakeTools({"sysinfo": json.dumps({"host": "mac"}),
                             "peer_call": "nginx Up"})
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               caps_index={"docker": ["nas"]})
        run = orch.run({"goal": "g", "capability": "docker",
                        "command": "docker ps",
                        "acceptance": ["观察本机现状", "在目标节点执行命令"]})
        assert run["review"]["verdict"] == "pass"
        assert run["status"] == "completed"
        assert all(item["status"] == "passed"
                   for item in run["review"]["items"])

    def test_llm_plan_used_when_available(self, store):
        plan = {"steps": [
            {"goal": "观察", "tool": "sysinfo", "assignee": "local",
             "acceptance": "有输出"},
            {"goal": "远端", "tool": "exec", "assignee": "nas",
             "acceptance": "exit=0", "args": {"command": "uptime"}},
        ]}
        llm = FakeLLM(text="```json\n%s\n```" % json.dumps(plan,
                                                          ensure_ascii=False))
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               llm=llm)
        run = orch.run({"goal": "g"})
        assert run["plan_source"] == "llm"
        assert llm.calls
        assert [step["tool"] for step in run["steps"]] == ["sysinfo", "exec"]
        assert run["status"] == "completed"

    def test_llm_failure_degrades_to_deterministic(self, store):
        llm = FakeLLM(error=RuntimeError("401 unauthorized token=sk-secret123456"))
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               llm=llm)
        run = orch.run({"goal": "g", "command": "uptime"})
        assert run["plan_source"] == "deterministic"
        assert run["status"] == "completed"
        notes = [note["note"] for note in run["reflections"]]
        assert any("降级为确定性规划" in note for note in notes)
        dumped = json.dumps(run, ensure_ascii=False)
        assert "sk-secret123456" not in dumped

    def test_llm_invalid_json_degrades(self, store):
        llm = FakeLLM(text="我觉得应该先看看日志（不是 JSON）")
        handler = FakeTools()
        orch = fa.Orchestrator(store, fa.FleetClient(handler, name="mac"),
                               llm=llm)
        run = orch.run({"goal": "g"})
        assert run["plan_source"] == "deterministic"
        assert run["status"] == "completed"


# ===========================================================================
# 八、LLMAdapter（全部 mock，不联网）
# ===========================================================================
class TestLLMAdapter:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(fa, "_http_post_json", lambda *a, **k: {
            "choices": [{"message": {"content": "plan ok"}}]})
        adapter = fa.LLMAdapter("http://127.0.0.1:9/v1", "mock-model")
        assert adapter.complete([{"role": "user", "content": "hi"}]) == "plan ok"
        assert adapter.calls == 1
        assert adapter.remaining == adapter.max_calls - 1

    def test_retry_then_fail_is_redacted(self, monkeypatch):
        calls = {"n": 0}

        def boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("network down Bearer sk-abcdefgh123456")

        monkeypatch.setattr(fa, "_http_post_json", boom)
        adapter = fa.LLMAdapter("http://127.0.0.1:9/v1", "m", api_key="secret",
                                retries=1, max_calls=8)
        with pytest.raises(fa.LLMError) as excinfo:
            adapter.complete([{"role": "user", "content": "hi"}])
        message = str(excinfo.value)
        assert calls["n"] == 2
        assert "sk-abcdefgh123456" not in message
        assert "secret" not in message

    def test_budget_exhausted(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("fail")

        monkeypatch.setattr(fa, "_http_post_json", boom)
        adapter = fa.LLMAdapter("http://127.0.0.1:9/v1", "m", retries=0,
                                max_calls=1)
        with pytest.raises(fa.LLMError):
            adapter.complete([{"role": "user", "content": "hi"}])
        assert adapter.remaining == 0
        with pytest.raises(fa.LLMError) as excinfo:
            adapter.complete([{"role": "user", "content": "hi"}])
        assert "预算" in str(excinfo.value)

    def test_missing_config(self):
        with pytest.raises(fa.LLMError):
            fa.LLMAdapter("", "m").complete([{"role": "user", "content": "x"}])
        with pytest.raises(fa.LLMError):
            fa.LLMAdapter("http://127.0.0.1:9/v1", "").complete(
                [{"role": "user", "content": "x"}])

    def test_extract_completion_validation(self):
        assert fa._extract_completion(
            {"choices": [{"message": {"content": "ok"}}]}) == "ok"
        for bad in ({}, {"choices": []}, {"choices": [{}]},
                    {"choices": [{"message": {"content": "  "}}]}):
            with pytest.raises(fa.LLMError):
                fa._extract_completion(bad)

    def test_endpoint(self):
        adapter = fa.LLMAdapter("http://127.0.0.1:9/v1/", "m")
        assert adapter.endpoint() == "http://127.0.0.1:9/v1/chat/completions"


# ===========================================================================
# 九、能力索引
# ===========================================================================
class TestCapsIndex:
    def test_canonical(self):
        index = fa.build_caps_index({"docker": ["nas"], "gpu": "pc"})
        assert index["docker"] == ["nas"]
        assert index["gpu"] == ["pc"]

    def test_list_form(self):
        index = fa.build_caps_index([
            {"node": "nas", "caps": ["docker", "tailscale"]},
            {"name": "pi", "capabilities": ["gpio"]},
        ])
        assert index["docker"] == ["nas"]
        assert index["tailscale"] == ["nas"]
        assert index["gpio"] == ["pi"]

    def test_empty(self):
        assert fa.build_caps_index(None) == {}
        assert fa.build_caps_index({}) == {}
