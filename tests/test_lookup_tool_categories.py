"""lookup_tools browses the catalog by category (radar #319): an empty call
lists the groups, {"category": ...} lists one, MCP servers are one group each."""
import json

import pytest

import src.tool_serve as ts
from src import tool_registry
from src.tool_policy import ToolPolicy


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    """Single-user/admin install: nothing is hidden for being admin-only."""
    import src.tool_security as sec
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)



@pytest.fixture
def notes_runtime(monkeypatch):
    """Connected synthetic manager with matching real schemas/registry rows."""
    class Notes:
        executions = 0
        def get_all_openai_schemas(self, ctx):
            return [{"type": "function", "function": {
                "name": f"mcp__notes__{name}", "description": f"Synthetic notes {name}",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
                for name in ("add", "find")]
        def get_all_tools(self):
            return [{"qualified_name": schema["function"]["name"], "server_id": "notes",
                     "description": schema["function"]["description"],
                     "input_schema": schema["function"]["parameters"]}
                    for schema in self.get_all_openai_schemas({})]
        async def call_tool(self, *args, **kwargs):
            self.executions += 1
            raise AssertionError("Discovery must not execute a connector")
    manager = Notes()
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: manager)
    rows = tool_registry.snapshot(mcp_manager=manager)
    notes = [row for row in rows if row.name.startswith("mcp__notes__")]
    assert [row.name for row in notes] == ["mcp__notes__add", "mcp__notes__find"]
    assert all(row.input_schema["type"] == "object" for row in notes)
    yield manager
    assert manager.executions == 0


def test_an_empty_call_lists_categories(notes_runtime):
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


def test_an_mcp_server_is_a_category(notes_runtime):
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


@pytest.mark.parametrize("context", [
    {"disabled_tools": ["mcp__notes__add"]},
    {"tool_policy": ToolPolicy(disabled_tools=frozenset({"mcp__notes__add"}))},
])
def test_real_mcp_category_filters_denied_tool_before_count_and_promotion(notes_runtime, context):
    _, listed = ts.execute_lookup("{}", context)
    category = next(row for row in listed["lookup_tools"]["categories"] if row["name"] == "mcp:notes")
    assert category["count"] == 1
    _, result = ts.execute_lookup(json.dumps({"category": "notes"}), context)
    assert result["promote"] == ["mcp__notes__find"]
    assert "mcp__notes__add" not in json.dumps(result)


def test_nonadmin_cannot_discover_registered_mcp_category(notes_runtime, monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: False)
    _, result = ts.execute_lookup(json.dumps({"category": "notes"}), {"owner": "synthetic-user"})
    assert result["promote"] == []
    assert result["lookup_tools"]["tools"] == []
    assert "mcp:notes" not in result["lookup_tools"]["hint"]


def test_advertised_mcp_name_without_schema_is_not_callable(monkeypatch):
    monkeypatch.setattr(ts, "connected_mcp_tool_names", lambda: ["mcp__ghost__missing"])
    _, result = ts.execute_lookup(json.dumps({"category": "ghost"}), {})
    assert result["promote"] == []
    assert result["lookup_tools"]["tools"] == []
    assert "mcp:ghost" not in result["lookup_tools"]["hint"]
