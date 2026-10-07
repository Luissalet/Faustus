from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

import src.agent_tools  # noqa: F401
import src.agent_loop as al

ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
PINS = {
    "mcp__fixture__server__ingest",
    "mcp__fixture__server__transform",
    "mcp__fixture__server__export",
}


class ConnectedManager:
    def get_all_openai_schemas(self, disabled_map=None):
        disabled = set()
        for names in (disabled_map or {}).values():
            disabled.update(names)
        return [
            {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}
            for name in sorted(PINS - disabled)
        ]

    def get_tool_descriptions_for_prompt(self, *args, **kwargs):
        return ""

    def has_remote_servers(self):
        return False

    def get_all_tools(self, disabled_map=None):
        return []


def run_turn(workspace, *, pins=PINS, disabled=(), policy=None):
    offered = []
    manager = ConnectedManager()

    async def fake_stream(candidates, messages, **kwargs):
        offered.append(kwargs.get("tools") or [])
        yield "data: " + json.dumps({"delta": "Ready."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(*args, **kwargs):
        return ("read_file", {"output": "ok"})

    patches = [
        mock.patch.object(al, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(al, "execute_tool_block", fake_execute),
        mock.patch.object(al, "get_mcp_manager", lambda: manager),
        mock.patch.object(al, "estimate_tokens", lambda *a, **k: 10),
        mock.patch.object(al, "get_setting", lambda key, default=None: default),
        mock.patch.object(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True)),
        mock.patch.object(al, "blocked_tools_for_owner", lambda owner: set()),
    ]
    for patch in patches:
        patch.start()
    try:
        async def drive():
            stream = al.stream_agent_loop(
                endpoint_url=ENDPOINT,
                model="test-model",
                messages=[{"role": "user", "content": "Update the project README.md and explain the data workflow."}],
                headers={},
                workspace=str(workspace),
                owner="admin",
                session_id="workspace-pins-test",
                max_rounds=1,
                context_length=32768,
                relevant_tools=set(pins),
                disabled_tools=set(disabled),
                tool_policy=policy,
                harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
            )
            return [chunk async for chunk in stream]

        asyncio.run(drive())
    finally:
        for patch in reversed(patches):
            patch.stop()
    assert offered, "stream_agent_loop did not reach the provider"
    return {schema["function"]["name"] for schema in offered[0] if schema.get("function")}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic workspace.\n", encoding="utf-8")
    return tmp_path


def test_workspace_route_preserves_caller_pinned_mcp_schemas(workspace):
    names = run_turn(workspace)
    assert PINS <= names
    assert {"read_file", "edit_file", "ls"} <= names


def test_workspace_route_preserves_pins_but_disabled_filter_still_wins(workspace):
    disabled_name = "mcp__fixture__server__transform"
    names = run_turn(workspace, disabled={disabled_name})
    assert (PINS - {disabled_name}) <= names
    assert disabled_name not in names
    assert {"read_file", "edit_file", "ls"} <= names


def test_workspace_route_respects_disable_mcp_policy(workspace):
    from src.tool_policy import ToolPolicy

    policy = ToolPolicy(disable_mcp=True)
    names = run_turn(workspace, policy=policy)
    assert not names & PINS
    assert {"read_file", "edit_file", "ls"} <= names



def test_workspace_route_keeps_pins_under_mcp_only_policy(workspace):
    from src.tool_policy import build_effective_tool_policy

    policy = build_effective_tool_policy(last_user_message="Use only MCP tools.")
    assert policy.mode == "mcp_only"
    names = run_turn(workspace, policy=policy)
    assert PINS <= names
    assert not names & {"read_file", "edit_file", "bash", "python"}
