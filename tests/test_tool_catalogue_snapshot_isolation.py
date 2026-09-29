"""Catalogue exports and provider updates must not rewrite prior snapshots."""
from copy import deepcopy

import pytest

from src import tool_registry
from src.contracts.tool import ToolDescriptor
from src.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolated_native_source(monkeypatch):
    # A failing regression must not corrupt schemas used by later tests.
    monkeypatch.setattr(tool_registry, "FUNCTION_TOOL_SCHEMAS",
                        deepcopy(tool_registry.FUNCTION_TOOL_SCHEMAS))


def test_export_mutation_does_not_modify_snapshot_or_global_native_schema():
    rows = ToolRegistry.snapshot()
    row = ToolRegistry.by_name(rows, "read_file")
    before = row.fingerprint()
    exported = row.to_mapping()
    exported["input_schema"]["properties"]["__isolation_probe"] = {"type": "string"}
    assert row.fingerprint() == before
    assert all("__isolation_probe" not in entry.get("function", {}).get("parameters", {}).get("properties", {})
               for entry in tool_registry.FUNCTION_TOOL_SCHEMAS)


def test_provider_nested_update_does_not_change_old_snapshot():
    schema = {"type": "object", "properties": {"path": {"type": "string", "enum": ["old"]}}}
    class Manager:
        def get_all_tools(self):
            return [{"name": "read", "qualified_name": "mcp__test__read", "server_id": "test",
                     "description": "Read", "input_schema": schema}]
    manager = Manager()
    old = ToolRegistry.by_name(ToolRegistry.snapshot(mcp_manager=manager), "mcp__test__read")
    fingerprint = old.fingerprint()
    schema["properties"]["path"]["enum"].append("new")
    assert old.fingerprint() == fingerprint
    fresh = ToolRegistry.by_name(ToolRegistry.snapshot(mcp_manager=manager), "mcp__test__read")
    assert fresh.fingerprint() != fingerprint


def test_parsed_nested_input_and_output_schema_are_detached_both_directions():
    raw = ToolRegistry.by_name(ToolRegistry.snapshot(), "read_file").to_mapping()
    raw["output_schema"] = {"type": "object", "properties": {"items": {"enum": ["old"]}}}
    parsed = ToolDescriptor.from_runtime_mapping(raw)
    fingerprint = parsed.fingerprint()
    raw["input_schema"]["properties"].clear()
    raw["output_schema"]["properties"]["items"]["enum"].append("input mutation")
    exported = parsed.to_mapping()
    exported["output_schema"]["properties"]["items"]["enum"].append("output mutation")
    assert parsed.fingerprint() == fingerprint
