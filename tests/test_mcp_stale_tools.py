"""MCP results that report a failure in their body, unknown tools and the
catalog `lookup_tools` offers.

Seen live on the 27B: a study app renamed `cards_stats` to `study_stats`
without a `tools/list_changed`. Faustus kept offering the old name, the app
answered `{"error": "Unknown tool: cards_stats"}` as a plain text result and
the run, the harness and the export all counted it as a success.
"""
import asyncio
from types import SimpleNamespace

from src import mcp_manager as mm
from src import tool_serve as ts


def test_error_body_is_a_failure():
    assert mm._body_reports_error('{"error": "Unknown tool: cards_stats"}')
    assert mm._body_reports_error('{"ok": false, "error": "down", "app": "x"}')
    assert mm._body_reports_error('{"error": {"code": 404}}')


def test_data_that_merely_has_an_error_field_is_not():
    assert not mm._body_reports_error('{"ok": true, "error": "partial"}')
    assert not mm._body_reports_error('{"error": null, "items": []}')
    assert not mm._body_reports_error('{"items": [], "count": 0}')
    assert not mm._body_reports_error('not json {"error": "x"}')
    assert not mm._body_reports_error('[{"error": "x"}]')


class _Session:
    def __init__(self, tools, text):
        self._tools = tools
        self._text = text

    async def call_tool(self, name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)], isError=False)

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            tools=[SimpleNamespace(name=n, description="", inputSchema={}, annotations=None) for n in self._tools],
            nextCursor=None,
        )


def _manager(session, old_tools):
    mgr = mm.McpManager()
    mgr._sessions["abcd1234"] = session
    mgr._connections["abcd1234"] = {"name": "Study", "status": "connected"}
    mgr._tools["abcd1234"] = [{"name": n, "description": "", "input_schema": {}} for n in old_tools]
    return mgr


def test_unknown_tool_refreshes_the_list_and_names_the_current_tools():
    session = _Session(["study_stats", "cards_due"], '{"error": "Unknown tool: cards_stats"}')
    mgr = _manager(session, ["cards_stats", "cards_due"])
    gen = mgr._generation
    result = asyncio.run(mgr.call_tool("mcp__abcd1234__cards_stats", {}))
    assert result["exit_code"] == 1
    assert result.get("tools_refreshed") is True
    assert "mcp__abcd1234__study_stats" in result["stderr"]
    assert [t["name"] for t in mgr._tools["abcd1234"]] == ["study_stats", "cards_due"]
    assert mgr._generation > gen, "the tool index re-reads the MCP half"


def test_a_normal_error_does_not_refresh():
    session = _Session(["study_stats"], '{"error": "deck not found"}')
    mgr = _manager(session, ["study_stats"])
    result = asyncio.run(mgr.call_tool("mcp__abcd1234__study_stats", {}))
    assert result["exit_code"] == 1
    assert "tools_refreshed" not in result


class _Mcp:
    def __init__(self, names):
        self._names = names

    def get_all_openai_schemas(self, _ctx):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {}}} for n in self._names]


class _Index:
    def retrieve(self, q, k=8):
        return ["mcp__vitruvius__render_preview", "mcp__67b3358f__render_preview", "ask_user"]


def test_lookup_never_offers_a_name_no_server_has(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp(["mcp__67b3358f__render_preview"]))
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: _Index())
    found = ts.search_catalog(query="render the page preview", k=8)
    assert "mcp__67b3358f__render_preview" in found
    assert "mcp__vitruvius__render_preview" not in found
    assert "ask_user" in found