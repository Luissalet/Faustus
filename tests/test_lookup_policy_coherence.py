"""Native lookup payloads and audit fallback respect the existing permission predicate."""
import asyncio
import json

import pytest

from src import tool_discovery as discovery, tool_security, tool_serve as serve
from src.agent_tools import TOOL_HANDLERS
from src.tool_policy import ToolPolicy


@pytest.fixture(autouse=True)
def synthetic_runtime(monkeypatch):
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: owner != "nonadmin")
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)


def lookup(args, ctx=None):
    _, result = asyncio.run(TOOL_HANDLERS["lookup_tools"](json.dumps(args), ctx or {}))
    assert result["exit_code"] == 0
    payload = json.loads(result["output"])
    assert payload == result["lookup_tools"]
    assert result["promote"] == payload["promote"]
    return payload


@pytest.mark.parametrize("detail", ["schema", "catalog"])
def test_policy_denied_exact_name_is_removed_from_payload_and_promotion(detail):
    payload = lookup({"names": ["read_file", "ask_user"], "detail": detail},
                     {"tool_policy": ToolPolicy(disabled_tools=frozenset({"read_file"}))})
    assert payload["promote"] == ["ask_user"]
    assert [row["name"] for row in payload["tools"]] == ["ask_user"]
    assert "read_file" not in json.dumps(payload)


def test_all_denied_result_does_not_claim_callable_tools():
    payload = lookup({"names": ["read_file"]}, {"tool_policy": ToolPolicy(block_all_tool_calls=True)})
    assert payload["tools"] == [] and payload["promote"] == []
    assert "callable this turn" not in payload["hint"]


def test_policy_absent_retains_normal_schema():
    payload = lookup({"names": ["read_file"], "detail": "schema"})
    assert payload["promote"] == ["read_file"]
    assert payload["tools"][0]["schema"]["function"]["name"] == "read_file"


@pytest.mark.parametrize("denial", ["policy", "disabled"])
@pytest.mark.parametrize("name", ["send_email", "mcp__email__send_email"])
def test_email_denial_aliases_filter_lookup(denial, name):
    qualified = "mcp__email__send_email"
    schema = {"type": "function", "function": {"name": qualified, "parameters": {}}}

    class Mcp:
        def get_all_openai_schemas(self, ctx):
            return [schema]

    # The MCP manager is synthetic; no connection or actual send occurs.
    from unittest.mock import patch
    opposite = qualified if name == "send_email" else "send_email"
    ctx = ({"tool_policy": ToolPolicy(disabled_tools=frozenset({opposite}))}
           if denial == "policy" else {"disabled_tools": {opposite}})
    with patch("src.tool_utils.get_mcp_manager", return_value=Mcp()):
        payload = lookup({"names": [name]}, ctx)
    assert payload["tools"] == [] and payload["promote"] == []


@pytest.mark.parametrize("args", [{"category": "synthetic"}, {"categories": True}, {}])
def test_categories_filter_policy_before_rows_and_counts(monkeypatch, args):
    monkeypatch.setattr(serve, "tool_categories", lambda: {"synthetic": ["read_file", "ask_user"], "denied": ["read_file"]})
    payload = lookup(args, {"tool_policy": ToolPolicy(disabled_tools=frozenset({"read_file"}))})
    assert "read_file" not in json.dumps(payload)
    if "category" in args:
        assert payload["promote"] == ["ask_user"]
    else:
        assert len(payload["categories"]) == 1
        assert payload["categories"][0]["count"] == 1


def test_nonadmin_payload_filters_existing_admin_denylist():
    payload = lookup({"names": ["manage_settings", "ask_user"]}, {"owner": "nonadmin"})
    assert payload["promote"] == ["ask_user"]


def test_bounded_fallback_ranks_only_permitted_names():
    policy = ToolPolicy(disabled_tools=frozenset({"read_file"}))
    audit = discovery.audit_selection("read_file", ["read_file"], tool_policy=policy,
                                     fallback_pool=["read_file", "ask_user"])
    assert audit.resolved == [] and audit.fallback == ["ask_user"]


def test_empty_permitted_fallback_does_not_repopulate_catalog():
    audit = discovery.audit_selection("read_file", ["read_file"],
                                     tool_policy=ToolPolicy(block_all_tool_calls=True),
                                     fallback_pool=["read_file"])
    assert audit.resolved == [] and audit.fallback == []
    assert discovery.nearest_by_name_or_capability("read", pool=[]) == []


def test_explicit_empty_fallback_pool_is_preserved():
    audit = discovery.audit_selection("missing", [], fallback_pool=[])
    assert audit.fallback == []


def test_fallback_disabled_generator_is_not_consumed_by_resolved_filter():
    audit = discovery.audit_selection("read_file", ["read_file"],
                                     disabled_tools=iter(["read_file"]),
                                     fallback_pool=["read_file", "ask_user"])
    assert audit.resolved == [] and audit.fallback == ["ask_user"]
