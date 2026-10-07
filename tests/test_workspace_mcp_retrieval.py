from __future__ import annotations

import asyncio
import json
from unittest import mock

import src.agent_tools  # noqa: F401
import src.agent_loop as al
import src.tool_index as tool_index
from src.tool_policy import build_effective_tool_policy

ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
SELECTED = {
    "mcp__fixture__server__inspect",
    "mcp__fixture__server__transform",
}
ALL_MCP = SELECTED | {"mcp__fixture__server__unrelated"}


def schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Synthetic fixture operation {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class SyntheticMcpManager:
    def get_all_openai_schemas(self, disabled_map=None):
        disabled = set()
        for names in (disabled_map or {}).values():
            disabled.update(names)
        return [schema(name) for name in sorted(ALL_MCP - disabled)]

    def get_tool_descriptions_for_prompt(self, *args, **kwargs):
        return ""

    def has_remote_servers(self):
        return False

    def get_all_tools(self, disabled_map=None):
        return []


class DeterministicRetrieval:
    def __init__(self):
        self.queries = []
        self.indexed = 0

    def index_mcp_tools(self, manager, disabled_map=None):
        self.indexed += 1

    def get_tools_for_query(self, query, limit, **kwargs):
        self.queries.append((query, limit, kwargs))
        return set(SELECTED) | {"ask_user", "lookup_tools", "update_plan"}


def run_turn(workspace, *, mcp_only, disabled=()):
    offered = []
    retrieval = DeterministicRetrieval()
    manager = SyntheticMcpManager()
    message = "Update README.md using the fixture inspection and transformation operations."
    if mcp_only:
        message = "Use only MCP tools. " + message
    policy = build_effective_tool_policy(
        last_user_message=message,
        disabled_tools=disabled,
    )
    assert (policy.mode == "mcp_only") is mcp_only

    async def fake_stream(candidates, messages, **kwargs):
        offered.append(kwargs.get("tools") or [])
        yield "data: " + json.dumps({"delta": "Ready."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(*args, **kwargs):
        return ("ask_user", {"output": "ok"})

    patches = [
        mock.patch.object(al, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(al, "execute_tool_block", fake_execute),
        mock.patch.object(al, "get_mcp_manager", lambda: manager),
        mock.patch.object(al, "estimate_tokens", lambda *a, **k: 10),
        mock.patch.object(al, "get_setting", lambda key, default=None: default),
        mock.patch.object(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True)),
        mock.patch.object(al, "blocked_tools_for_owner", lambda owner: set()),
        mock.patch.object(tool_index, "get_tool_index", lambda: retrieval),
        mock.patch.object(tool_index, "tool_rerank_options", lambda owner: {}),
    ]
    for patch in patches:
        patch.start()
    try:
        async def drive():
            stream = al.stream_agent_loop(
                endpoint_url=ENDPOINT,
                model="test-model",
                messages=[{"role": "user", "content": message}],
                headers={},
                workspace=str(workspace),
                owner="admin",
                session_id="workspace-mcp-retrieval-test",
                max_rounds=1,
                context_length=32768,
                relevant_tools=None,
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
    assert retrieval.queries, "deterministic retrieval was not reached"
    names = {item["function"]["name"] for item in offered[0] if item.get("function")}
    return names, retrieval


def test_workspace_provider_payload_discards_retrieval_noise_unless_mcp_only(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic workspace.\n", encoding="utf-8")

    unrestricted, ordinary_retrieval = run_turn(tmp_path, mcp_only=False)
    assert ordinary_retrieval.indexed == 1 and ordinary_retrieval.queries
    assert not unrestricted & SELECTED
    assert "mcp__fixture__server__unrelated" not in unrestricted
    assert {"read_file", "edit_file", "ls"} <= unrestricted

    mcp_only, scoped_retrieval = run_turn(tmp_path, mcp_only=True)
    assert scoped_retrieval.indexed == 1 and scoped_retrieval.queries
    assert SELECTED <= mcp_only
    assert "mcp__fixture__server__unrelated" not in mcp_only
    assert not mcp_only & {"read_file", "edit_file", "bash", "python", "powershell"}


def test_workspace_mcp_only_retrieval_respects_disabled_filter(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic workspace.\n", encoding="utf-8")
    disabled = {"mcp__fixture__server__transform"}
    names, retrieval = run_turn(tmp_path, mcp_only=True, disabled=disabled)
    assert retrieval.queries
    assert "mcp__fixture__server__inspect" in names
    assert disabled.isdisjoint(names)
    assert not names & {"read_file", "edit_file", "bash", "python", "powershell"}
