"""lookup_tools browses the catalog by category (radar #319): an empty call
lists the groups, {"category": ...} lists one, MCP servers are one group each."""
import json

import pytest

import src.tool_serve as ts


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    """Single-user/admin install: nothing is hidden for being admin-only."""
    import src.tool_security as sec
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda owner: True)


def test_an_empty_call_lists_categories(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: ["mcp__notes__add", "mcp__notes__find"])
    desc, result = ts.execute_lookup("{}", {"owner": None})
    payload = result["lookup_tools"]
    names = {c["name"]: c for c in payload["categories"]}
    assert result["exit_code"] == 0 and "categories" in desc
    assert "email" in names and names["email"]["count"] >= 3
    assert "git" in names
    assert names["mcp:notes"]["count"] == 2
    assert payload["promote"] == []


def test_a_category_lists_and_promotes_its_tools(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: [])
    _desc, result = ts.execute_lookup(json.dumps({"category": "email"}), {"owner": None})
    payload = result["lookup_tools"]
    assert payload["category"] == "email"
    assert "send_email" in payload["promote"]
    assert all("summary" in t for t in payload["tools"])


def test_an_mcp_server_is_a_category(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: ["mcp__notes__add", "mcp__notes__find"])
    _desc, result = ts.execute_lookup(json.dumps({"category": "notes"}), {"owner": None})
    assert result["promote"] == ["mcp__notes__add", "mcp__notes__find"]


def test_disabled_tools_stay_out_and_unknown_category_says_what_exists(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: [])
    _d, result = ts.execute_lookup(json.dumps({"category": "email"}), {"owner": None, "disabled_tools": ["send_email"]})
    assert "send_email" not in result["promote"]
    _d, result = ts.execute_lookup(json.dumps({"category": "nope"}), {"owner": None})
    assert result["promote"] == [] and "Known:" in result["lookup_tools"]["hint"]


def test_query_still_searches(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: [])
    _d, result = ts.execute_lookup(json.dumps({"names": ["read_file"]}), {"owner": None})
    assert "read_file" in result["promote"]


def test_query_takes_priority_over_guessed_category(monkeypatch):
    monkeypatch.setattr(ts, "serve", lambda **kwargs: {
        "tools": [{"name": "mcp__cook__list_recipes"}],
        "promote": ["mcp__cook__list_recipes"],
        "detail": "catalog",
    })
    _d, result = ts.execute_lookup(json.dumps({
        "query": "CookHoard recipes", "category": "cookbook",
    }), {"owner": None})
    assert result["promote"] == ["mcp__cook__list_recipes"]
