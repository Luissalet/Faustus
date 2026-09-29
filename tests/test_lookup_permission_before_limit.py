"""Catalog filtering precedes its cap; the index's own cap stays unchanged."""
import asyncio
import json
import pytest
from src.agent_tools import TOOL_HANDLERS
from src import tool_serve
from src.tool_policy import ToolPolicy


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    monkeypatch.setattr(tool_serve, "_explicit_mcp_hits", lambda query: [])
    monkeypatch.setattr(tool_serve, "_keyword_hits", lambda query: [])


def lookup(args, policy=None):
    _, result = asyncio.run(TOOL_HANDLERS["lookup_tools"](json.dumps(args), {"tool_policy": policy}))
    assert result["exit_code"] == 0
    payload = result["lookup_tools"]
    assert result["promote"] == payload["promote"]
    return payload


@pytest.mark.parametrize("source", ["names", "keyword", "explicit", "retrieved_pool"])
def test_permitted_candidate_survives_catalog_limit(monkeypatch, source):
    ranked = ["read_file", "ask_user", "write_file"]
    args = {"query": "synthetic ranking", "k": 1}
    if source == "names":
        args = {"names": ranked, "k": 1}
    elif source == "keyword":
        monkeypatch.setattr(tool_serve, "_keyword_hits", lambda query: ranked)
    elif source == "explicit":
        monkeypatch.setattr(tool_serve, "_explicit_mcp_hits", lambda query: ranked)
    else:
        class Index:
            def retrieve(self, query, k):
                return ranked  # Returned pool, no hidden rows.
        monkeypatch.setattr("src.tool_index.get_tool_index", lambda: Index())
    payload = lookup(args, ToolPolicy(disabled_tools=frozenset({"read_file"})))
    assert payload["promote"] == ["ask_user"]
    assert payload["tools"][0]["schema"]["function"]["name"] == "ask_user"


def test_index_cutoff_remains_explicit_and_k_is_not_inflated(monkeypatch):
    requests = []
    class Index:
        def retrieve(self, query, k):
            requests.append(k)
            return ["read_file", "ask_user"][:k]
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: Index())
    policy = ToolPolicy(disabled_tools=frozenset({"read_file"}))
    assert lookup({"query": "synthetic ranking", "k": 1}, policy)["promote"] == []
    assert lookup({"query": "synthetic ranking", "k": 2}, policy)["promote"] == ["ask_user"]
    assert requests == [1, 2]


def test_policy_absent_and_direct_search_keep_order_and_contract():
    names = ["read_file", "ask_user", "write_file"]
    assert lookup({"names": names, "k": 2})["promote"] == names[:2]
    assert tool_serve.search_catalog(names=["unknown_synthetic", "ask_user"], k=1) == ["unknown_synthetic"]


def test_total_denial_does_not_repopulate_catalog():
    payload = lookup({"names": ["read_file", "ask_user"], "k": 1}, ToolPolicy(block_all_tool_calls=True))
    assert payload["promote"] == [] and payload["tools"] == []
