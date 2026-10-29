"""Public MCP view selection. Synthetic AX and in-memory MCP, never a browser."""
import asyncio
from contextlib import suppress
from copy import deepcopy
import json

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from src.browser_relay.protocol import validate_arguments
from src.browser_relay.server import create_server, mcp_result
from test_presentation import node, payload, snapshot


def selected(value, view, detail="compact"):
    result = mcp_result(value, detail=detail, view=view)
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent["result"]


def test_literal_unicode_and_roles_select_original_rows_not_compact_graph():
    original = payload()
    original["result"]["nodes"] += [node("literal", "button", "[QA].* Straße"),
                                     node("case", "Button", "[QA].* Straße")]
    before = deepcopy(original)
    result = selected(original, {"query": "[qa].* STRASSE", "roles": ["button"]})
    expected = {key: value for key, value in original["result"]["nodes"][-2].items() if key != "child_refs"}
    assert result["nodes"] == [expected]
    assert result["selection"] == {"kind": "flat", "total": 10, "matches": 1, "offset": 0, "next_offset": None}
    assert selected(original, {"query": "élodie 李明"})["selection"]["matches"] == 2
    assert selected(original, {"query": "elodie"})["nodes"] == []  # No accent stripping.
    duplicate = selected(original, {"roles": ["InlineTextBox"]})["nodes"]
    assert [item["ref"] for item in duplicate] == ["duplicate", "unique"]
    assert selected(original, {"roles": []})["nodes"] == []
    assert original == before


def test_zero_false_null_refs_duplicates_and_full_fields_survive():
    tree = snapshot()
    tree["nodes"] = [node("zero", "spinbutton", "Count", value=0),
                     node("false", "checkbox", "Flag", value=False, properties={"checked": False}),
                     node(None, "StaticText", "Repeated", properties={"selected": False}),
                     node(None, "StaticText", "Repeated"),
                     node("same", "StaticText", "Repeated"), node("same", "StaticText", "Repeated")]
    original, before = payload(tree), deepcopy(tree)
    assert selected(original, {"query": "0"})["nodes"][0]["value"] == 0
    false = selected(original, {"query": "false"})["nodes"]
    assert len(false) == 1 and false[0]["value"] is False and false[0]["properties"] == {"checked": False}
    repeated = selected(original, {"query": "repeated"})
    assert [item["ref"] for item in repeated["nodes"]] == [None, None, "same", "same"]
    assert repeated["nodes"][0]["properties"] == {"selected": False}
    assert repeated["selection"]["matches"] == 4
    all_rows = selected(original, {})
    assert all_rows["nodes"] == [{key: value for key, value in item.items() if key != "child_refs"}
                                 for item in tree["nodes"]]
    assert selected(original, {}, "full") == all_rows
    assert tree == before and original == payload(tree)


def test_home_and_library_selection_paginates_without_default_truncation():
    home = snapshot()
    home["nodes"] = [node("home-" + str(i), "button", "QA compacto Writer1095" if i == 104 else "Other")
                     for i in range(205)]
    chosen = selected(payload(home), {"query": "qa compacto", "roles": ["button"]})
    assert chosen["nodes"][0]["ref"] == "home-104" and chosen["selection"]["total"] == 205
    library = snapshot()
    library["nodes"] = [node("document-" + str(i), "button", "Document " + str(i)) for i in range(36)]
    library["nodes"] += [node(None, "StaticText", "Other " + str(i)) for i in range(897)]
    original, before = payload(library), deepcopy(library)
    offset, seen = 0, []
    while offset is not None:
        page = selected(original, {"query": "document", "roles": ["button"], "offset": offset, "limit": 7})
        assert page["selection"]["total"] == 933 and page["selection"]["matches"] == 36
        assert page["selection"]["offset"] == offset and 1 <= len(page["nodes"]) <= 7
        seen += page["nodes"]
        offset = page["selection"]["next_offset"]
    assert [item["ref"] for item in seen] == ["document-" + str(i) for i in range(36)]
    unlimited = selected(original, {"offset": 1})
    assert len(unlimited["nodes"]) == 932 and unlimited["selection"]["next_offset"] is None
    outside = selected(original, {"offset": 999})
    assert outside["nodes"] == [] and outside["selection"] == {
        "kind": "flat", "total": 933, "matches": 933, "offset": 999, "next_offset": None}
    empty = selected(payload({**library, "nodes": []}), {})
    assert empty["nodes"] == [] and empty["selection"]["matches"] == 0 and empty["selection"]["next_offset"] is None
    assert library == before


def test_nested_readback_metadata_and_errors_keep_original_semantics():
    original = payload({"tab": {"id": 17}, "action": "click", "snapshot": snapshot(), "extra": {"kept": True}})
    before = deepcopy(original)
    result = selected(original, {"query": "library", "roles": ["button"]})
    assert result["action"] == "click" and result["tab"] == {"id": 17} and result["extra"] == {"kept": True}
    nested = result["snapshot"]
    assert len(nested["nodes"]) == 1 and nested["nodes"][0]["ref"] == "button"
    assert not any("child_refs" in item for item in nested["nodes"])
    assert {key: value for key, value in nested.items() if key not in {"nodes", "selection"}} == {
        key: value for key, value in snapshot().items() if key != "nodes"}
    error = {"ok": False, "phase": "action_confirmed", "error": {"code": "readback_failed", "message": "Observe, no replay"}}
    response = mcp_result(error, view={"query": "library"})
    assert response.isError and response.structuredContent == error
    assert original == before
    unknown = payload({"nodes": [None], "generation": 9})
    assert mcp_result(unknown, view={}).structuredContent == unknown


@pytest.mark.parametrize("view", [None, [], "library", {"query": 1}, {"roles": "button"},
                                       {"roles": [None]}, {"offset": -1}, {"offset": 1.5},
                                       {"offset": True}, {"limit": 0}, {"limit": -1},
                                       {"limit": False}, {"limit": 1.5}, {"limit": "2"}, {"unknown": 0}])
def test_malformed_views_fail_before_dispatch(view):
    with pytest.raises(ValueError):
        validate_arguments({"action": "snapshot", "tab_id": 17, "view": view})


@pytest.mark.parametrize("action", ["tabs", "screenshot", "release"])
def test_view_only_applies_to_actions_with_snapshots(action):
    with pytest.raises(ValueError):
        validate_arguments({"action": action, "tab_id": 17, "view": {}})


def test_options_are_detached_and_json_integer_forms_are_accepted():
    arguments = {"action": "snapshot", "tab_id": 17, "view": {"roles": ["button"], "offset": 0.0, "limit": 1.0}}
    detached = validate_arguments(arguments)
    arguments["view"]["roles"].clear()
    result = selected(payload(), detached["view"])
    assert result["nodes"][0]["ref"] == "button" and result["selection"]["offset"] == 0


@pytest.mark.asyncio
async def test_public_mcp_catalog_filters_readback_and_removes_view_before_relay():
    class SimulatedRelay:
        def __init__(self):
            self.calls = []

        async def call(self, arguments):
            self.calls.append(deepcopy(arguments))
            if arguments["action"] == "snapshot":
                return payload()
            return payload({"tab": {"id": 17}, "action": arguments["action"], "snapshot": snapshot()})

    relay = SimulatedRelay()
    server = create_server(relay)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(server.run(*server_streams, server.create_initialization_options()))
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                catalog = await client.list_tools()
                view_schema = catalog.tools[0].inputSchema["properties"]["view"]
                assert view_schema["properties"]["limit"]["minimum"] == 1
                assert view_schema["additionalProperties"] is False
                actions = [{"action": "snapshot", "tab_id": 17},
                           {"action": "click", "tab_id": 17, "ref": "button"},
                           {"action": "type", "tab_id": 17, "ref": "value", "text": "Élodie 李明"},
                           {"action": "press", "tab_id": 17, "key": "Enter"},
                           {"action": "navigate", "tab_id": 17, "url": "https://example.test"}]
                for arguments in actions:
                    response = await client.call_tool("browser_session", {
                        **arguments, "view": {"query": "library", "roles": ["button"]}, "detail": "full"})
                    assert not response.isError and response.structuredContent["phase"] == "readback_complete"
                    result = response.structuredContent["result"]
                    tree = result if arguments["action"] == "snapshot" else result["snapshot"]
                    assert len(tree["nodes"]) == 1 and tree["nodes"][0]["ref"] == "button"
                    assert tree["selection"]["total"] == 8
                    assert json.loads(response.content[0].text) == response.structuredContent
                assert relay.calls == actions  # Presentation is not extension wire ABI.
                invalid = await client.call_tool("browser_session", {"action": "snapshot", "tab_id": 17, "view": {"limit": 0}})
                assert invalid.isError and invalid.structuredContent["error"]["code"] == "invalid_arguments"
                assert relay.calls == actions
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
