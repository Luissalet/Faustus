"""A named library can search and read cited passages in the first round."""

from src.agent_loop import _borges_hoard_read_tools


def _tools(server="Borges's Hoard", server_id="library1", disabled=False):
    return [
        {"server_name": server, "server_id": server_id, "name": name,
         "qualified_name": f"mcp__{server_id}__{name}", "is_disabled": disabled}
        for name in ("library_search", "library_read", "library_reindex")
    ]


def test_named_borges_offers_citation_read_path_only():
    assert _borges_hoard_read_tools(
        "En Borges's Hoard busca el acta y cita el pasaje", _tools()
    ) == {"mcp__library1__library_search", "mcp__library1__library_read"}


def test_borges_route_requires_unique_enabled_server():
    query = "Busca el acta en Borges"
    assert _borges_hoard_read_tools(query, []) == set()
    assert _borges_hoard_read_tools(query, _tools(disabled=True)) == set()
    assert _borges_hoard_read_tools(query, _tools(server="Links Hoard")) == set()
    assert _borges_hoard_read_tools(query, _tools() + _tools(server_id="library2")) == set()
    assert _borges_hoard_read_tools("Busca el acta", _tools()) == set()
