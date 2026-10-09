"""A connected catalogue must offer its annotated reads without granting writes."""
import pytest

from src import autonomy_budget, tool_capabilities, tool_serve, tool_utils
from src.mcp_manager import McpManager
from src.tool_policy import ToolPolicy


@pytest.fixture
def manager(monkeypatch):
    mgr = object.__new__(McpManager)
    mgr._connections = {"hub": {"status": "connected", "transport": "stdio", "name": "Hub"}}
    mgr._tools = {"hub": [
        {"name": "hub_agora_task", "description": "Read a coordination task", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}},
         "annotations": {"readOnlyHint": True, "destructiveHint": False}},
        {"name": "hub_agora_post", "description": "Post a message", "input_schema": {"type": "object"},
         "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    ]}
    monkeypatch.setattr(tool_utils, "get_mcp_manager", lambda: mgr)
    # Keep the catalog test independent of browser settings and database state.
    monkeypatch.setattr(mgr, "get_all_openai_schemas", lambda *a: [
        {"type": "function", "function": {"name": f"mcp__{sid}__{t['name']}", "description": t["description"], "parameters": t["input_schema"]}}
        for sid, tools in mgr._tools.items() if mgr._connections.get(sid, {}).get("status") == "connected" for t in tools])
    return mgr


def test_readonly_offer_and_action_have_same_effects(manager):
    name = "mcp__hub__hub_agora_task"
    assert autonomy_budget.is_read_only_tool(name)
    assert autonomy_budget.is_read_only_action(name, '{"task_id":136}')
    caps = tool_capabilities.capabilities_for_tool(name)
    assert caps.known
    assert caps.result_integrity is tool_capabilities.ResultIntegrity.EXTERNAL_UNTRUSTED
    assert tool_capabilities.ToolEffect.BROKERED_NETWORK_READ in caps.effects
    assert not autonomy_budget.is_read_only_tool("mcp__hub__hub_agora_post")
    assert not autonomy_budget.is_read_only_action("mcp__hub__hub_agora_post", '{"body":"unexpected"}')
    policy = ToolPolicy(read_only=True)
    result = tool_serve.serve(names=["hub_agora_task"], detail="schema", tool_policy=policy)
    assert result["promote"] == [name]
    assert result["tools"][0]["schema"]["function"]["parameters"]["properties"]["task_id"]["type"] == "integer"
    assert tool_serve.serve(names=["hub_agora_post"], tool_policy=policy)["promote"] == []


@pytest.mark.parametrize("annotation", [None, {}, {"readOnlyHint": False}, {"readOnlyHint": "true"},
                                       {"readOnlyHint": True, "destructiveHint": True}])
def test_missing_ambiguous_and_destructive_annotations_fail_closed(manager, annotation):
    manager._tools["hub"][0]["annotations"] = annotation
    assert not autonomy_budget.is_read_only_tool("mcp__hub__hub_agora_task")


def test_disconnect_revokes_read_and_unknown_name_does_not_get_a_schema(manager):
    name = "mcp__hub__hub_agora_task"
    manager._connections["hub"]["status"] = "error"
    assert not autonomy_budget.is_read_only_action(name, "{}")
    assert tool_serve.serve(names=[name], tool_policy=ToolPolicy(read_only=True))["promote"] == []
    assert not manager.annotated_readonly_tool_allowed("hub_agora_task")
    assert not manager.annotated_readonly_tool_allowed("mcp__hub__get_all_secrets")


def test_ambiguous_short_name_requires_qualified_choice_from_permitted_pool(manager):
    manager._connections["other"] = {"status": "connected", "transport": "http", "name": "Other"}
    manager._tools["other"] = [dict(manager._tools["hub"][0])]
    result = tool_serve.serve(names=["hub_agora_task"], tool_policy=ToolPolicy(read_only=True))
    assert result["promote"] == []
    assert result["ambiguous"][0]["choices"] == ["mcp__hub__hub_agora_task", "mcp__other__hub_agora_task"]
    chosen = tool_serve.serve(names=["mcp__hub__hub_agora_task"], tool_policy=ToolPolicy(read_only=True))
    assert chosen["promote"] == ["mcp__hub__hub_agora_task"]
    disabled = tool_serve.serve(names=["hub_agora_task"], disabled=["mcp__other__hub_agora_task"], tool_policy=ToolPolicy(read_only=True))
    assert disabled["promote"] == ["mcp__hub__hub_agora_task"] and "ambiguous" not in disabled
    scoped = tool_serve.serve(names=["hub_agora_task"], category="mcp:hub", tool_policy=ToolPolicy(read_only=True))
    assert scoped["promote"] == ["mcp__hub__hub_agora_task"] and "ambiguous" not in scoped
