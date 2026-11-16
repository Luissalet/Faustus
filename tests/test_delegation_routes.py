"""src/delegation_routes.py: named routes the coordinator picks per sub-agent."""
import json

import pytest

from src import delegation_routes as dr
from src.agent_tools import subagent_tools as st

ROUTES = [
    {"name": "rápido", "description": "Consultas y resúmenes cortos", "model": "small-model", "effort": "low"},
    {"name": "profundo", "description": "Depuración y diseño difíciles", "endpoint_id": "cloud", "model": "big-model",
     "effort": "high", "skills": ["tdd"], "category": "files,git"},
    {"name": "revisor", "description": "Revisión sin editar", "agent": "explorer"},
]


@pytest.fixture
def settings(monkeypatch):
    store = {}
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr("src.settings.update_settings", lambda patch, expected_revision=None: store.update(patch) or store)
    return store


def test_validate_rejects_bad_routes():
    with pytest.raises(ValueError, match="no spaces"):
        dr.validate([{"name": "con espacio", "description": "x", "model": "m"}])
    with pytest.raises(ValueError, match="description"):
        dr.validate([{"name": "a", "model": "m"}])
    with pytest.raises(ValueError, match="twice"):
        dr.validate([{"name": "a", "description": "x", "model": "m"}, {"name": "A", "description": "y", "model": "n"}])
    with pytest.raises(ValueError, match="runs on"):
        dr.validate([{"name": "a", "description": "x"}])
    with pytest.raises(ValueError, match="default"):
        dr.validate(ROUTES, "otro")
    assert dr.validate(ROUTES, "PROFUNDO")["default"] == "profundo"


def test_schema_shows_only_names_and_descriptions(settings):
    assert dr.schema_property() is None
    dr.save(ROUTES, "rápido")
    prop = dr.schema_property()
    assert prop["enum"] == ["rápido", "profundo", "revisor"]
    assert "Consultas y resúmenes cortos" in prop["description"] and "`rápido`" in prop["description"]
    assert "big-model" not in json.dumps(prop) and "cloud" not in json.dumps(prop)


def test_decorate_adds_route_to_delegate_agents_only(settings):
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schemas = [s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] in ("delegate_agents", "todowrite")]
    assert dr.decorate_schemas(schemas) is schemas          # no routes: untouched
    dr.save(ROUTES)
    out = dr.decorate_schemas(schemas)
    da = next(s for s in out if s["function"]["name"] == "delegate_agents")
    assert "route" in da["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
    original = next(s for s in schemas if s["function"]["name"] == "delegate_agents")
    assert "route" not in original["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_parse_resolves_a_named_route(settings):
    dr.save(ROUTES)
    args = st.parse_delegation_args(json.dumps({"tasks": [
        {"name": "a", "instruction": "arregla el fallo", "route": "profundo"},
        {"name": "b", "instruction": "resume", "route": "rápido", "model": "mine"},
        {"name": "c", "instruction": "revisa", "route": "revisor"},
    ]}))
    a, b, c = args["tasks"]
    assert (a["route"], a["model"], a["endpoint_id"], a["effort"], a["category"]) == ("profundo", "big-model", "cloud", "high", ["files", "git"])
    assert "`tdd`" in a["instruction"]
    assert (b["route"], b["model"], b["effort"]) == ("rápido", "mine", "low")     # the task's own model wins
    assert c["agent"] == "explorer"


def test_unknown_route_refuses_the_delegation(settings):
    dr.save(ROUTES)
    with pytest.raises(ValueError, match="not configured"):
        st.parse_delegation_args(json.dumps({"tasks": [{"instruction": "x", "route": "inventada"}]}))


def test_default_route_only_when_the_task_chose_nothing(settings):
    dr.save(ROUTES, "rápido")
    args = st.parse_delegation_args(json.dumps({"tasks": [
        {"instruction": "uno"}, {"instruction": "dos", "tier": "fast"}]}))
    assert args["tasks"][0]["route"] == "rápido" and args["tasks"][0]["model"] == "small-model"
    assert "route" not in args["tasks"][1]


def test_no_routes_keeps_the_parser_output_unchanged(settings):
    payload = json.dumps({"tasks": [{"name": "a", "instruction": "x", "model": "m"}]})
    row = st.parse_delegation_args(payload)["tasks"][0]
    assert "route" not in row and row["model"] == "m"


def test_worker_report_names_its_route():
    run = st.SubagentRun(0, {"name": "a", "instruction": "x", "route": "profundo"})
    assert run.report()["route"] == "profundo"
    assert "route" not in st.SubagentRun(0, {"name": "a", "instruction": "x"}).report()
