from __future__ import annotations

import asyncio
import json
from unittest import mock

import src.agent_tools  # noqa: F401
import src.agent_loop as al


ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
BOARD_TOOLS = {
    "board_list", "board_ready", "board_get", "board_create", "meeting_actions_to_board",
    "board_update", "board_comment", "board_link", "board_claim",
}
MCP_TOOLS = {"mcp__fixture__workbook__inspect", "mcp__fixture__workbook__export"}
USER_REQUEST = (
    "What tasks are pending for this project? Also update README.md to explain the data workflow."
)


class ConnectedManager:
    def get_all_openai_schemas(self, disabled_map=None):
        return [
            {
                "type": "function",
                "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
            }
            for name in sorted(MCP_TOOLS)
        ]

    def get_tool_descriptions_for_prompt(self, *args, **kwargs):
        return ""

    def has_remote_servers(self):
        return False

    def get_all_tools(self, disabled_map=None):
        return []


def offered_by_workspace_loop(workspace, *, project_id=None):
    offered = []
    manager = ConnectedManager()
    pins = BOARD_TOOLS | MCP_TOOLS

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
        mock.patch.object(al, "_project_board_block", lambda *a, **k: ""),
        mock.patch.object(al, "_project_repos_block", lambda *a, **k: ""),
    ]
    for patch in patches:
        patch.start()
    try:
        async def drive():
            stream = al.stream_agent_loop(
                endpoint_url=ENDPOINT,
                model="test-model",
                messages=[{"role": "user", "content": USER_REQUEST}],
                headers={},
                workspace=str(workspace),
                owner="admin",
                session_id="workspace-board-pins-test",
                max_rounds=1,
                context_length=32768,
                relevant_tools=set(pins),
                disabled_tools=set(),
                harness_options={
                    "checkpoints": False,
                    "run_tests": False,
                    "repo_map": False,
                    **({"project_id": project_id} if project_id else {}),
                },
            )
            return [chunk async for chunk in stream]

        asyncio.run(drive())
    finally:
        for patch in reversed(patches):
            patch.stop()

    assert offered, "stream_agent_loop did not reach the provider"
    return {schema["function"]["name"] for schema in offered[0] if schema.get("function")}


def test_workspace_selector_drops_pinned_board_tools_without_project_but_keeps_mcp(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic workspace.\n", encoding="utf-8")

    names = offered_by_workspace_loop(tmp_path)

    assert not names & BOARD_TOOLS
    assert MCP_TOOLS <= names
    assert {"read_file", "edit_file", "ls"} <= names


def test_workspace_selector_preserves_board_and_mcp_pins_with_project(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic workspace.\n", encoding="utf-8")

    names = offered_by_workspace_loop(tmp_path, project_id="synthetic-project")

    assert BOARD_TOOLS <= names
    assert MCP_TOOLS <= names
    assert {"read_file", "edit_file", "ls"} <= names
