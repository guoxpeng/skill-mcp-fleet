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


# ===========================================================================
# v5.0.3 连接/长期运行稳定性：mail 限流 + audit 轮转
# ===========================================================================
class TestMailRateLimit:
    def test_bucket_allows_then_blocks(self, monkeypatch):
        """独立令牌桶：cap 内放行，超 cap 拒绝；cap<=0 不限。"""
        monkeypatch.setattr(mp, "CFG", {"mail_rate_per_min": 3})
        monkeypatch.setattr(mp, "_mail_rate_bucket", {"tokens": 0, "last": 0})
        results = [mp.rate_limit_mail() for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_disabled_when_zero(self, monkeypatch):
        monkeypatch.setattr(mp, "CFG", {"mail_rate_per_min": 0})
        assert all(mp.rate_limit_mail() for _ in range(100))

    def test_independent_from_tool_bucket(self, monkeypatch):
        """mail 桶与工具层桶独立：工具桶打满不影响 mail 桶。"""
        monkeypatch.setattr(mp, "CFG", {"rate_limit_per_min": 1, "mail_rate_per_min": 5})
        monkeypatch.setattr(mp, "_rate_bucket", {"tokens": 0, "last": 0})
        monkeypatch.setattr(mp, "_mail_rate_bucket", {"tokens": 0, "last": 0})
        assert mp.rate_limit_allow() is True      # 工具桶第 1 个
        assert mp.rate_limit_allow() is False     # 工具桶空
        assert mp.rate_limit_mail() is True       # mail 桶不受影响


class TestAuditRotation:
    def test_rotates_when_over_limit(self, tmp_path, monkeypatch):
        """超过阈值轮转为 .1；小文件不动。"""
        audit = tmp_path / "audit.log"
        audit.write_text("x" * 300)
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        mp._audit_rotate_if_needed(max_bytes=100)
        assert not audit.exists()                       # 原文件被移走
        assert (tmp_path / "audit.log.1").read_text() == "x" * 300

    def test_no_rotate_when_small(self, tmp_path, monkeypatch):
        audit = tmp_path / "audit.log"
        audit.write_text("x" * 50)
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        mp._audit_rotate_if_needed(max_bytes=100)
        assert audit.exists() and audit.read_text() == "x" * 50
        assert not (tmp_path / "audit.log.1").exists()

    def test_audit_log_writes_and_rotates(self, tmp_path, monkeypatch):
        """完整路径：写日志 -> 超限自动轮转 -> 继续写新文件。"""
        audit = tmp_path / "audit.log"
        monkeypatch.setattr(mp, "AUDIT_PATH", str(audit))
        monkeypatch.setattr(mp, "CFG", {"audit_log": True})
        monkeypatch.setattr(mp, "AUDIT_ROTATE_BYTES", 100)   # 缩小阈值便于测试
        # 预置一个超限文件
        audit.write_text("old" * 100)
        mp.audit_log("peer_call", "a", "b", "exec", "ok")   # 应触发轮转并写入新文件
        assert (tmp_path / "audit.log.1").read_text() == "old" * 100
        assert "peer_call" in audit.read_text()
