"""`lookup_tools` with a plugin tool's bare name resolves to the qualified
`mcp__<server>__<tool>` the model can actually call.

Seen live: a skill named `screen_activity`, `budget_status`, `link_digest`…;
the model passed them as `names` and got stubs with no schema, then spent
seven more `lookup_tools` rounds (one per app, ~150 s of a 27B) finding
the qualified names one at a time.
"""
from src import tool_serve as ts


class _Mcp:
    def __init__(self, names):
        self._names = names

    def get_all_openai_schemas(self, _ctx):
        return [{"type": "function", "function": {"name": n, "description": f"[MCP] {n}", "parameters": {}}}
                for n in self._names]


CONNECTED = [
    "mcp__df4c1bc4__screen_activity", "mcp__df4c1bc4__screen_status",
    "mcp__10d9867d__summary", "mcp__10d9867d__budget_status",
    "mcp__41bff0fe__link_digest", "mcp__9f071b84__upcoming", "mcp__8fd35623__scribe_sessions",
    # two servers that happen to share a tool name
    "mcp__aaaa__search", "mcp__bbbb__search",
]


def test_bare_plugin_names_resolve_to_qualified_tools(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp(CONNECTED))
    found = ts.search_catalog(names=["screen_activity", "budget_status", "link_digest", "upcoming", "scribe_sessions"], k=8)
    assert found == [
        "mcp__df4c1bc4__screen_activity", "mcp__10d9867d__budget_status",
        "mcp__41bff0fe__link_digest", "mcp__9f071b84__upcoming", "mcp__8fd35623__scribe_sessions",
    ]
    payload = ts.serve(names=["screen_activity"], detail="schema")
    assert payload["promote"] == ["mcp__df4c1bc4__screen_activity"]
    assert payload["tools"][0].get("schema"), "the qualified tool carries its schema"


def test_a_name_shared_by_two_servers_lists_both(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp(CONNECTED))
    assert ts.resolve_bare_name("search") == ["mcp__aaaa__search", "mcp__bbbb__search"]


def test_builtins_qualified_and_unknown_names_are_untouched(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp(CONNECTED))
    assert ts.resolve_bare_name("ask_user") == ["ask_user"]
    assert ts.resolve_bare_name("mcp__df4c1bc4__screen_activity") == ["mcp__df4c1bc4__screen_activity"]
    assert ts.resolve_bare_name("nope_not_a_tool") == ["nope_not_a_tool"]
    assert ts.resolve_bare_name("") == []


def test_no_mcp_manager_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    assert ts.resolve_bare_name("screen_activity") == ["screen_activity"]
