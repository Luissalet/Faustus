"""H05 partial: actual runtime catalogue survives its explicit wire profile."""
import json

import pytest

from src.tool_registry import ToolRegistry
from src.contracts.base import ContractError
from src.contracts.tool import ToolDescriptor, ToolInvocation


class Manager:
    def get_all_tools(self):
        return [{
            "name": "Fetch-Item.v2", "server_id": "hoard-1",
            "qualified_name": "mcp__hoard-1__Fetch-Item.v2",
            "description": "Fetch a record", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        }]


def wire(rows):
    return json.loads(json.dumps([r.to_mapping() for r in rows]))


def test_actual_runtime_catalogue_serializes_parses_and_preserves_fingerprint():
    rows = ToolRegistry.snapshot(mcp_manager=Manager())
    decoded = wire(rows)
    parsed = ToolRegistry.parse_snapshot(decoded)
    assert {r.name for r in rows} >= {"bash", "read_file", "mcp__hoard-1__Fetch-Item.v2"}
    assert len(rows) >= 220
    assert [r.to_mapping() for r in parsed] == decoded
    assert ToolRegistry.catalog_fingerprint(parsed) == ToolRegistry.catalog_fingerprint(rows)
    for before, after in zip(rows, parsed):
        assert before.fingerprint() == after.fingerprint()
        call = ToolInvocation.from_mapping({
            "schema_version": "1.0", "call_id": "call1", "attempt_id": "attempt1",
            "task_id": "task1", "run_id": "run1", "tool": {"name": after.name, "version": after.version},
            "arguments": {}, "idempotency_key": "key1", "authorization_ref": None,
        })
        assert call.tool.name == before.name
        assert ToolInvocation.from_mapping(json.loads(json.dumps(call.to_mapping()))).fingerprint() == call.fingerprint()


def test_runtime_profile_does_not_weaken_default_spec_parser():
    descriptor = ToolRegistry.by_name(ToolRegistry.snapshot(), "read_file").to_mapping()
    assert ToolDescriptor.from_runtime_mapping(descriptor).name == "read_file"
    with pytest.raises(ContractError, match="dotted"):
        ToolDescriptor.from_mapping(descriptor)
    descriptor["name"] = "fs.read_file"
    assert ToolDescriptor.from_mapping(descriptor).name == "fs.read_file"
    descriptor["description"] = ""
    assert ToolDescriptor.from_runtime_mapping(descriptor).description == ""
    with pytest.raises(ContractError, match="description"):
        ToolDescriptor.from_mapping(descriptor)
    del descriptor["description"]
    with pytest.raises(ContractError, match="description"):
        ToolDescriptor.from_runtime_mapping(descriptor)


@pytest.mark.parametrize("patch", [
    {"name": "bad/name"}, {"name": "mcp__server__"}, {"name": "mcp____tool"},
    {"name": "bad name"}, {"name": 3}, {"name": "x" * 513},
    {"unexpected": True}, {"input_schema": []}, {"output_schema": "{}"},
    {"timeout_ms": True}, {"timeout_ms": "30"}, {"timeout_ms": 0},
    {"schema_version": 1.0}, {"effect_class": "harmless"},
    {"required_scopes": "read"}, {"retry_policy": {"max_attempts": 99}},
    {"description": ["text"]}, {"description": "x" * 4001},
])
def test_invalid_third_party_runtime_payloads_still_rejected(patch):
    descriptor = ToolRegistry.by_name(ToolRegistry.snapshot(), "read_file").to_mapping()
    with pytest.raises(ContractError):
        ToolRegistry.parse_snapshot([{**descriptor, **patch}])


def test_parser_refuses_non_list_and_duplicate_names():
    with pytest.raises(ContractError):
        ToolRegistry.parse_snapshot({"name": "bash"})
    descriptor = ToolRegistry.by_name(ToolRegistry.snapshot(), "bash").to_mapping()
    with pytest.raises(ContractError, match="duplicate"):
        ToolRegistry.parse_snapshot([descriptor, descriptor])


def test_bad_mcp_entries_do_not_destroy_valid_catalogue():
    class MixedManager(Manager):
        def get_all_tools(self):
            valid = super().get_all_tools()
            return [None, {**valid[0], "input_schema": []}, {**valid[0], "qualified_name": "bad/name"}, *valid]

    rows = ToolRegistry.snapshot(mcp_manager=MixedManager())
    assert ToolRegistry.by_name(rows, "bash")
    assert ToolRegistry.by_name(rows, "mcp__hoard-1__Fetch-Item.v2")
    assert len(rows) == len(ToolRegistry.snapshot()) + 1
