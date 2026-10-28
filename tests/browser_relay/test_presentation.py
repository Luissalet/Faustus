"""Snapshot projection only. Synthetic trees and real MCP in-memory messages, no browser."""
import asyncio
from contextlib import suppress
from copy import deepcopy
import json

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from src.browser_relay.server import create_server, mcp_result


def node(ref, role, name="", children=(), *, value="", properties=None, ignored=False):
    return {"ref": ref, "role": role, "name": name, "value": value,
            "properties": properties or {}, "ignored": ignored, "child_refs": list(children)}


def snapshot():
    return {"tab": {"id": 17, "active": False, "title": "Owned page"}, "generation": 9,
            "document": {"frame_id": "frame", "loader_id": "loader", "url": "https://example.test"}, "scope": "main_frame", "nodes": [
                node("root", "RootWebArea", "Owned page", ["ignored", "value"]),
                node("ignored", "none", children=["generic", "unique"], ignored=True),
                node("generic", "generic", children=["text", "button"]),
                node("text", "StaticText", "Élodie 李明", ["duplicate"]),
                node("duplicate", "InlineTextBox", "Élodie 李明"),
                node("button", "button", "Open Library", properties={"focusable": True, "disabled": False}),
                node("unique", "InlineTextBox", "Unique 🐚"),
                node("value", "textbox", "Search", value="Exact α", properties={"editable": "plaintext", "readonly": False}),
            ]}


def payload(value=None):
    return {"ok": True, "phase": "readback_complete", "result": deepcopy(value if value is not None else snapshot())}


def assert_graph(value):
    refs = [entry["ref"] for entry in value["nodes"]]
    assert len(refs) == len(set(refs))
    assert all(child in refs for entry in value["nodes"] for child in entry.get("child_refs", []))


def test_default_compact_preserves_controls_unique_text_and_promotes_order():
    original = payload()
    before = deepcopy(original)
    result = mcp_result(original)
    assert not result.isError and original == before  # Existing full mode is a healthy control.
    projected = result.structuredContent["result"]
    assert [entry["ref"] for entry in projected["nodes"]] == ["root", "text", "button", "unique", "value"], "DEFAULT_COMPACT_SNAPSHOT_MISSING"
    by_ref = {entry["ref"]: entry for entry in projected["nodes"]}
    assert by_ref["root"]["child_refs"] == ["text", "button", "unique", "value"]
    assert by_ref["text"]["name"] == "Élodie 李明" and by_ref["unique"]["name"] == "Unique 🐚"
    assert by_ref["button"]["properties"] == {"focusable": True, "disabled": False}
    assert by_ref["value"]["value"] == "Exact α"
    assert by_ref["value"]["properties"]["readonly"] is False
    assert {key: value for key, value in projected.items() if key != "nodes"} == {key: value for key, value in snapshot().items() if key != "nodes"}
    assert_graph(projected)
    assert json.loads(result.content[0].text) == result.structuredContent


def test_full_semantically_exact_and_nested_mutation_snapshot_compact():
    original = payload({"tab": {"id": 17}, "action": "click", "snapshot": snapshot(), "extra": {"kept": True}})
    before = deepcopy(original)
    full = mcp_result(original, detail="full")
    assert full.structuredContent == original and json.loads(full.content[0].text) == original
    assert full.content[0].text == json.dumps(original, ensure_ascii=False)
    compact = mcp_result(original)
    assert original == before
    assert compact.structuredContent["result"]["extra"] == {"kept": True}
    assert compact.structuredContent["result"]["action"] == "click"
    assert len(compact.structuredContent["result"]["snapshot"]["nodes"]) == 5
    assert_graph(compact.structuredContent["result"]["snapshot"])


def test_inline_segments_only_removed_if_text_already_represented_and_properties_survive():
    tree = snapshot()
    tree["nodes"] = [
        node("root", "RootWebArea", children=["static", "ignoredNamed", "genericProperties"]),
        node("static", "StaticText", "Line α beta", ["segment1", "segment2", "newText", "propertyText"]),
        node("segment1", "InlineTextBox", "Line α"), node("segment2", "InlineTextBox", " beta"),
        node("newText", "InlineTextBox", "New text only here"),
        node("propertyText", "InlineTextBox", "Line α", properties={"editable": True}),
        node("ignoredNamed", "none", "Useful ignored name", ignored=True),
        node("genericProperties", "generic", properties={"focusable": False}),
    ]
    compact = mcp_result(payload(tree)).structuredContent["result"]
    refs = [entry["ref"] for entry in compact["nodes"]]
    assert refs == ["root", "static", "newText", "propertyText", "ignoredNamed", "genericProperties"]
    assert compact["nodes"][1]["child_refs"] == ["newText", "propertyText"]
    assert compact["nodes"][-2]["name"] == "Useful ignored name"
    assert compact["nodes"][-1]["properties"] == {"focusable": False}
    assert_graph(compact)


def test_no_depth_or_text_truncation_and_no_references_regenerated():
    depth, text = 1500, "Élodie李明" * 2000
    tree = snapshot()
    tree["nodes"] = [node("root", "RootWebArea", children=["g0"])]
    tree["nodes"] += [node("g" + str(i), "generic", children=["g" + str(i + 1) if i + 1 < depth else "leaf"]) for i in range(depth)]
    tree["nodes"].append(node("leaf", "textbox", "Long content", value=text, properties={"editable": "plaintext"}))
    compact = mcp_result(payload(tree)).structuredContent["result"]
    assert [entry["ref"] for entry in compact["nodes"]] == ["root", "leaf"]
    assert compact["nodes"][0]["child_refs"] == ["leaf"]
    assert compact["nodes"][1]["value"] == text and len(text) > 10000
    assert_graph(compact)


def test_errors_tabs_and_screenshot_metadata_are_not_rewritten():
    error = {"ok": False, "phase": "outcome_unknown", "error": {"code": "lost", "message": "Observe before retry"}}
    assert mcp_result(error).structuredContent == error
    tabs = payload({"tabs": [{"id": 17, "active": False, "title": "Same tab"}]})
    assert mcp_result(tabs).structuredContent == tabs


def test_null_refs_preserved_without_inventing_ancestry_or_blocking_graph_compaction():
    tree = snapshot()
    # Real AX recordings expose unaddressable InlineTextBox nodes with ref:null
    # and no incoming child_refs. A matching global name does not prove ancestry.
    tree["nodes"] += [node(None, "InlineTextBox", "Élodie 李明"), node(None, "InlineTextBox", "Unique null text")]
    original = payload(tree)
    projected = mcp_result(original).structuredContent["result"]
    nulls = [entry for entry in projected["nodes"] if entry["ref"] is None]
    assert [entry["name"] for entry in nulls] == ["Élodie 李明", "Unique null text"]
    assert [entry["ref"] for entry in projected["nodes"] if entry["ref"] is not None] == ["root", "text", "button", "unique", "value"], "NULL_REFS_BLOCK_COMPACT_PROJECTION"
    assert projected["nodes"][0]["child_refs"] == ["text", "button", "unique", "value"]
    assert all(child is not None for entry in projected["nodes"] for child in entry.get("child_refs", []))
    assert original == payload(tree)


def test_unknown_node_shape_falls_back_to_original_full_payload():
    tree = snapshot()
    tree["nodes"] = [{"role": "generic"}]
    original = payload(tree)
    assert mcp_result(original).structuredContent == original


@pytest.mark.asyncio
async def test_public_mcp_detail_validated_and_not_sent_to_peer():
    class SimulatedRelay:
        def __init__(self):
            self.calls = []

        async def call(self, arguments):
            self.calls.append(deepcopy(arguments))
            return payload()

    relay = SimulatedRelay()
    server = create_server(relay)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(server.run(*server_streams, server.create_initialization_options()))
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                catalog = await client.list_tools()
                detail = catalog.tools[0].inputSchema["properties"]["detail"]
                assert detail["enum"] == ["compact", "full"] and detail["default"] == "compact"
                full = await client.call_tool("browser_session", {"action": "snapshot", "tab_id": 17, "detail": "full"})
                assert full.structuredContent == payload()
                compact = await client.call_tool("browser_session", {"action": "snapshot", "tab_id": 17})
                assert len(compact.structuredContent["result"]["nodes"]) == 5
                explicit = await client.call_tool("browser_session", {"action": "snapshot", "tab_id": 17, "detail": "compact"})
                assert explicit.structuredContent == compact.structuredContent
                assert relay.calls == [{"action": "snapshot", "tab_id": 17}] * 3
                invalid = await client.call_tool("browser_session", {"action": "snapshot", "tab_id": 17, "detail": "other"})
                assert invalid.isError and len(relay.calls) == 3
                assert invalid.structuredContent["error"]["code"] == "invalid_arguments"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
