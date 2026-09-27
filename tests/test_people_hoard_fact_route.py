"""Explicit person facts must reach the live CRM before a generic note tool."""

from src.agent_loop import _people_hoard_brief_tools, _people_hoard_fact_tools


def _tools(server="People's Hoard", disabled=False):
    return [
        {"server_name": server, "server_id": "people1", "name": name,
         "qualified_name": f"mcp__people1__{name}", "is_disabled": disabled}
        for name in ("find_people", "get_person", "add_fact")
    ]


def test_explicit_agenda_fact_routes_to_connected_people_tools():
    assert _people_hoard_fact_tools(
        "Apunta en mi agenda que Ana tiene alergia a los cacahuetes.", _tools()
    ) == {"mcp__people1__find_people", "mcp__people1__get_person", "mcp__people1__add_fact"}


def test_fact_route_requires_connected_unambiguous_crm():
    query = "Apunta en mi agenda que Ana tiene alergia a los cacahuetes."
    assert _people_hoard_fact_tools(query, []) == set()
    assert _people_hoard_fact_tools(query, _tools(disabled=True)) == set()
    assert _people_hoard_fact_tools(query, _tools(server="Links Hoard")) == set()
    assert _people_hoard_fact_tools(query, _tools() + [
        {**row, "server_id": "people2", "qualified_name": row["qualified_name"].replace("people1", "people2")}
        for row in _tools()]) == set()


def test_agenda_event_and_generic_notes_do_not_get_person_fact_route():
    assert _people_hoard_fact_tools("Apunta en mi agenda que Ana tiene una cita mañana.", _tools()) == set()
    assert _people_hoard_fact_tools("Apunta en mis notas que Ana tiene alergia.", _tools()) == set()


def test_explicit_people_brief_routes_to_connected_tool():
    tools = _tools() + [{"server_name": "People's Hoard", "server_id": "people1",
                         "name": "prepare_person_chat", "qualified_name": "mcp__people1__prepare_person_chat",
                         "is_disabled": False}]
    query = "Usa People's Hoard: prepárame para hablar con Irene Castaño."
    assert _people_hoard_brief_tools(query, tools) == {
        "mcp__people1__find_people", "mcp__people1__prepare_person_chat"}
    assert _people_hoard_brief_tools("Escribe un cuento sobre Irene.", tools) == set()
    assert _people_hoard_brief_tools(query, [{**row, "is_disabled": True} for row in tools]) == set()
    other = [{**row, "server_id": "people2",
              "qualified_name": row["qualified_name"].replace("people1", "people2")}
             for row in tools]
    assert _people_hoard_brief_tools(query, tools + other) == set()
