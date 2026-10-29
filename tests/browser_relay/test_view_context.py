"""Prepared permanent CPU/MCP controls. No Browser or model acceptance."""
import asyncio
from contextlib import suppress
from copy import deepcopy
import json

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from src.browser_relay.protocol import validate_arguments
from src.browser_relay.server import create_server
from test_presentation import node, payload, snapshot
from test_view import selected


def counter():
    parts = ["36", " ", "visibles", " ", "de", " ", "36", " · ", "Todos los documentos"]
    tree = snapshot()
    tree["nodes"] = [node("grandparent", "generic", children=["paragraph"]),
                     node("paragraph", "paragraph", children=["part-" + str(i) for i in range(len(parts))]),
                     *[node("part-" + str(i), "StaticText", text) for i, text in enumerate(parts)],
                     node(None, "InlineTextBox", "visibles"), node("elsewhere", "button", "Close")]
    return tree


def test_default_flat_rows_paging_and_context_false_are_exact():
    value = payload(counter())
    view = {"query": "visibles", "roles": ["StaticText"]}
    original = deepcopy(value)
    default = selected(value, view)
    assert default == selected(value, {**view, "context": False})
    assert default["selection"] == {"kind": "flat", "total": 13, "matches": 1, "offset": 0, "next_offset": None}
    assert default["nodes"] == [{key: item for key, item in counter()["nodes"][4].items() if key != "child_refs"}]
    assert "context_nodes" not in default
    assert value == original


def test_direct_counter_context_is_original_not_synthetic_text_or_subtree():
    value = payload(counter())
    original = deepcopy(value)
    view = {"query": "visibles", "roles": ["StaticText"]}
    default = selected(value, view)
    with_context = selected(value, {**view, "context": True})
    assert with_context["nodes"] == default["nodes"]
    assert with_context["selection"] == default["selection"]
    assert with_context["context_nodes"] == value["result"]["nodes"][1:11]
    parent, *parts = with_context["context_nodes"]
    assert parent["child_refs"] == [item["ref"] for item in parts]
    assert "part-2" in parent["child_refs"]
    assert [item["name"] for item in parts] == ["36", " ", "visibles", " ", "de", " ", "36", " · ", "Todos los documentos"]
    assert "context_nodes" not in selected(value, view)
    assert with_context == selected(value, {**view, "context": True}, "full")
    assert value == original


def test_context_keeps_duplicate_rows_and_null_parent_but_never_resolves_null_child():
    tree = snapshot()
    tree["nodes"] = [node("outside", "generic", children=["parent"]),
                     node("grandchild", "StaticText", "Do not descend"),
                     node("same", "StaticText", "visibles"),
                     node(None, "paragraph", children=["same", "sibling", None, "missing"]),
                     node("same", "StaticText", "visibles"),
                     node("sibling", "generic", children=["grandchild"]),
                     node(None, "StaticText", "No invented null ancestry")]
    original = deepcopy(tree)
    result = selected(payload(tree), {"query": "visibles", "context": True})
    assert result["context_nodes"] == [tree["nodes"][i] for i in [2, 3, 4, 5]]
    assert [item["ref"] for item in result["nodes"]] == ["same", "same"]
    assert result["context_nodes"][1]["child_refs"] == ["same", "sibling", None, "missing"]
    assert selected(payload(tree), {"query": "No invented", "context": True})["context_nodes"] == []
    assert tree == original


def test_context_is_for_selected_page_only_and_does_not_walk_cycles():
    tree = snapshot()
    tree["nodes"] = [node("p0", "paragraph", children=["a"]), node("a", "StaticText", "hit A"),
                     node("p1", "paragraph", children=["b"]), node("b", "StaticText", "hit B"),
                     node("cyclic", "StaticText", "cycle", children=["cyclic"])]
    page = selected(payload(tree), {"query": "hit", "offset": 1, "limit": 1, "context": True})
    assert page["context_nodes"] == tree["nodes"][2:4]
    assert page["selection"] == {"kind": "flat", "total": 5, "matches": 2, "offset": 1, "next_offset": None}
    assert selected(payload(tree), {"query": "hit", "offset": 2, "context": True})["context_nodes"] == []
    assert selected(payload(tree), {"query": "cycle", "context": True})["context_nodes"] == [tree["nodes"][4]]


def test_role_diagnostic_is_exact_case_query_only_unpaged_and_never_auto_selects():
    tree = snapshot()
    tree["nodes"] = [node("a", "StaticText", "visibles"), node(None, "InlineTextBox", "visibles"),
                     node("b", "StaticText", "visibles"), node("c", "button", "Other")]
    wrong = selected(payload(tree), {"query": "visibles", "roles": ["statictext"], "offset": 99, "limit": 1})
    assert wrong["nodes"] == []
    assert wrong["selection"] == {"kind": "flat", "total": 4, "matches": 0, "offset": 99, "next_offset": None,
                                  "matched_roles": {"StaticText": 2, "InlineTextBox": 1}}
    assert "matched_roles" not in selected(payload(tree), {"query": "missing", "roles": ["statictext"]})["selection"]
    assert "matched_roles" not in selected(payload(tree), {"query": "visibles", "roles": ["StaticText"], "offset": 99})["selection"]
    assert selected(payload(tree), {"query": "visibles", "roles": []})["selection"]["matched_roles"] == {"StaticText": 2, "InlineTextBox": 1}
    assert "matched_roles" not in selected(payload(tree), {"query": "visibles"})["selection"]


@pytest.mark.parametrize("context", [None, 0, 1, "true", [], {}])
def test_nonboolean_context_rejected_before_dispatch(context):
    with pytest.raises(ValueError):
        validate_arguments({"action": "snapshot", "tab_id": 17, "view": {"context": context}})


@pytest.mark.asyncio
async def test_public_context_schema_readback_and_stripping_without_browser():
    class SimulatedRelay:
        def __init__(self):
            self.calls = []

        async def call(self, arguments):
            self.calls.append(deepcopy(arguments))
            return payload({"tab": {"id": 17}, "action": "click", "snapshot": counter()})

    relay = SimulatedRelay()
    server = create_server(relay)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(server.run(*server_streams, server.create_initialization_options()))
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                catalog = await client.list_tools()
                schema = catalog.tools[0].inputSchema["properties"]["view"]["properties"]
                assert schema["context"]["type"] == "boolean" and schema["context"]["default"] is False
                assert schema["roles"]["examples"] == [["StaticText"], ["button"]]
                args = {"action": "click", "tab_id": 17, "ref": "button"}
                response = await client.call_tool("browser_session", {**args, "view": {"query": "visibles", "roles": ["StaticText"], "context": True}})
                result = response.structuredContent
                assert not response.isError and result["phase"] == "readback_complete"
                assert result["result"]["action"] == "click" and result["result"]["snapshot"]["context_nodes"] == counter()["nodes"][1:11]
                assert json.loads(response.content[0].text) == result
                assert relay.calls == [args]
                invalid = await client.call_tool("browser_session", {**args, "view": {"context": 1}})
                assert invalid.isError and invalid.structuredContent["error"]["code"] == "invalid_arguments"
                assert relay.calls == [args]
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
