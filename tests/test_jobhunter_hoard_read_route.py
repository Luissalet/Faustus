"""A named job Hoard exposes its two read tools in the first model round."""

from src.agent_loop import _jobhunter_hoard_read_tools


def _tools(server="JobHunter's Hoard", server_id="jobs1", disabled=False):
    return [
        {"server_name": server, "server_id": server_id, "name": name,
         "qualified_name": f"mcp__{server_id}__{name}", "is_disabled": disabled}
        for name in ("list_jobs", "get_application", "start_application")
    ]


def test_named_job_hoard_offers_only_read_path():
    assert _jobhunter_hoard_read_tools(
        "Resume en JobHunter's Hoard la oferta de Nube Ejemplo", _tools()
    ) == {"mcp__jobs1__list_jobs", "mcp__jobs1__get_application"}
    assert _jobhunter_hoard_read_tools(
        "Busca mi candidatura en Jubhunter", _tools(server="Jubhunter's Hoard")
    ) == {"mcp__jobs1__list_jobs", "mcp__jobs1__get_application"}


def test_job_route_requires_named_unique_connected_server():
    query = "Resume en JobHunter's Hoard la oferta"
    assert _jobhunter_hoard_read_tools(query, []) == set()
    assert _jobhunter_hoard_read_tools(query, _tools(disabled=True)) == set()
    assert _jobhunter_hoard_read_tools(query, _tools(server="Links Hoard")) == set()
    assert _jobhunter_hoard_read_tools(query, _tools() + _tools(server_id="jobs2")) == set()
    assert _jobhunter_hoard_read_tools("Resume esta oferta", _tools()) == set()
