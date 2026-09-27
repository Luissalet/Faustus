"""Named Funes requests can reach bounded time tools without discovery."""

from src.agent_loop import _funes_hoard_activity_tools


def _tools(server="Funes's Hoard", server_id="funes1", disabled=False):
    return [
        {"server_name": server, "server_id": server_id, "name": name,
         "qualified_name": f"mcp__{server_id}__{name}", "is_disabled": disabled}
        for name in ("activity_summary", "activity_where_was_i", "activity_pause")
    ]


def test_named_funes_offers_read_tools_without_pause():
    assert _funes_hoard_activity_tools(
        "En Funes, ayer qué proyecto ocupó más tiempo y dónde lo dejé", _tools()
    ) == {"mcp__funes1__activity_summary", "mcp__funes1__activity_where_was_i"}


def test_funes_route_requires_unique_enabled_server():
    query = "En Funes, qué hice ayer"
    assert _funes_hoard_activity_tools(query, []) == set()
    assert _funes_hoard_activity_tools(query, _tools(disabled=True)) == set()
    assert _funes_hoard_activity_tools(query, _tools(server="Links Hoard")) == set()
    assert _funes_hoard_activity_tools(query, _tools() + _tools(server_id="funes2")) == set()
    assert _funes_hoard_activity_tools("Qué hice ayer", _tools()) == set()
