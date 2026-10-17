# -*- coding: utf-8 -*-
"""mcp_agent 工具表完整性测试（import 安全：load_config 只读无副作用）。"""
import pytest

mp = pytest.importorskip("mcp_agent", reason="mcp_agent 导入失败")


class TestToolRegistry:
    def test_tool_count(self):
        assert len(mp.TOOLS) == 41

    def test_tool_schema_structure(self):
        """每个工具必须有 name/description/inputSchema(dict)。"""
        for t in mp.TOOLS:
            assert isinstance(t["name"], str) and t["name"], t
            assert isinstance(t["description"], str) and t["description"]
            assert isinstance(t["inputSchema"], dict)
            assert t["inputSchema"].get("type") == "object"

    def test_no_duplicate_names(self):
        names = [t["name"] for t in mp.TOOLS]
        assert len(names) == len(set(names))

    def test_every_tool_has_impl(self):
        """每个注册工具都有可调用实现（TOOL_FUNCS 全覆盖）。"""
        missing = [t["name"] for t in mp.TOOLS
                   if t["name"] not in mp.TOOL_FUNCS]
        assert missing == []

    def test_version_is_v5(self):
        assert mp.SERVER_VERSION.startswith("5.0")

    def test_core_tools_present(self):
        must = {"exec", "read", "write", "edit", "list_dir",
                "msg_send", "msg_recv", "peers", "peer_call", "mesh",
                "delegate", "caps", "mesh_caps", "sysinfo", "search"}
        have = set(mp.TOOL_FUNCS.keys())
        assert must <= have, "缺少: %s" % (must - have)

    def test_backcompat_nas_prefix(self):
        """旧版 nas_ 前缀名仍可解析（向后兼容承诺）。"""
        assert "nas_exec" in mp.TOOL_FUNCS
        assert mp.TOOL_FUNCS["nas_exec"] is mp.TOOL_FUNCS["exec"]
