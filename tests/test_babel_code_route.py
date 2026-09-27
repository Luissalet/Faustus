from src.agent_loop import _babel_code_tools


def _tools(server="Babel's Hoard", server_id="babel", disabled=False):
    return [{"server_name": server, "server_id": server_id, "is_disabled": disabled,
             "name": name, "qualified_name": f"mcp__{server_id}__{name}"}
            for name in ("docs_libraries", "api_lookup", "api_check_code")]


def test_babel_offered_for_coding_request():
    assert _babel_code_tools("Corrige este código Python con httpx", _tools()) == {
        "mcp__babel__docs_libraries", "mcp__babel__api_lookup", "mcp__babel__api_check_code"}
    assert _babel_code_tools("Is this API valid in my installed package?", _tools()) == {
        "mcp__babel__docs_libraries", "mcp__babel__api_lookup", "mcp__babel__api_check_code"}


def test_babel_route_requires_one_live_server_and_coding_intent():
    assert _babel_code_tools("Resume mi reunión", _tools()) == set()
    assert _babel_code_tools("Corrige este código", _tools(disabled=True)) == set()
    assert _babel_code_tools("Corrige este código", _tools(server="Other")) == set()
    assert _babel_code_tools("Corrige este código", _tools() + _tools(server_id="second")) == set()
