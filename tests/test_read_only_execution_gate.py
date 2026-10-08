"""Enforce autonomy at the real dispatcher, independently of tool offers."""
import asyncio
import json

import pytest

from src.agent_tools import ToolBlock
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_policy import ToolPolicy
from src.tools import research


@pytest.mark.parametrize("name", ["manage_research", "vault_unlock", "mcp__late__write", "future_mutator"])
def test_unenumerated_actions_are_denied_by_real_executor(name, tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", str(tmp_path))
    report = tmp_path / "saved.json"
    original = b'{"query":"isolated","summary":"preserve me"}'
    report.write_bytes(original)
    _, result = asyncio.run(execute_tool_block(
        ToolBlock(name, json.dumps({"action": "delete", "id": "saved"})),
        tool_policy=ToolPolicy(read_only=True),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert result.get("blocked") and result.get("exit_code") == 1
    assert report.read_bytes() == original


def test_mixed_research_reads_work_but_delete_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DEEP_RESEARCH_DIR", str(tmp_path))
    report = tmp_path / "saved.json"
    report.write_text('{"query":"isolated","summary":"preserve me"}', encoding="utf-8")
    for action in ("list", "read"):
        _, result = asyncio.run(execute_tool_block(
            ToolBlock("manage_research", json.dumps({"action": action, "id": "saved"})),
            tool_policy=ToolPolicy(read_only=True),
            security_context=NO_TOOL_SECURITY_CONTEXT,
        ))
        assert result.get("exit_code") == 0 and "isolated" in result.get("output", "")
    _, result = asyncio.run(execute_tool_block(
        ToolBlock("manage_research", '{"action":"delete","id":"saved"}'),
        tool_policy=ToolPolicy(), security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert result.get("exit_code") == 0 and not report.exists()


def test_exempting_workspace_tools_keeps_read_only_constraint():
    policy = ToolPolicy(read_only=True, disabled_tools=frozenset({"read_file"}))
    reconciled = policy.exempting({"read_file"})
    assert not reconciled.blocks("read_file")
    assert reconciled.blocks_action("mcp__late__write", "{}")
    assert reconciled.blocks_action("manage_research", '{"action":"delete"}')
    assert not reconciled.blocks_action("manage_research", '{"action":"list"}')


def test_argument_read_cannot_override_explicit_request_deny():
    policy = ToolPolicy(read_only=True, disabled_tools=frozenset({"manage_research"}))
    assert policy.blocks_action("manage_research", '{"action":"list"}')
