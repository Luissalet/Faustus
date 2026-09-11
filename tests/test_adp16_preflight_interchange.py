"""tests/test_adp16_preflight_interchange.py — ADP-16 + ADP-17 (W1-G).

* `src.workflows.preflight` — a dry-run report over a real
  `WorkflowDefinition`: connections, tools, permissions, inputs, outputs,
  human waits, a token estimate and a cost estimate, with zero LLM/script/
  network effects.
* `src.workflows.interchange` — `export_canonical` (a versioned envelope
  that round-trips without changing what a definition means) and
  `import_external` (accepts that envelope, or an aigraphstudio-shaped
  payload, mapping only Start/Input/Output/Tool/Human Approval/Router to a
  checked handler — everything else stays `design_only`, and a cycle or an
  unrecognized node type never becomes executable).

No network calls anywhere in this file.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from src.contracts.workflow import WorkflowDefinition, WorkflowNode
from src.workflow_cost_estimate import ModelPrice
from src.workflows.interchange import export_canonical, import_external
from src.workflows.preflight import preflight

PRICED_MODEL = "local:test-7b"
PRICES = {PRICED_MODEL: ModelPrice(prompt_usd_per_1k=0.001, completion_usd_per_1k=0.002)}


def _definition(**overrides):
    base = {
        "id": "report.publish", "version": "1.0.0", "title": "Write and send",
        "nodes": [
            {"id": "start", "type": "manual", "config": {}},
            {"id": "check", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
            {"id": "work", "type": "skill", "needs": ["check"],
             "config": {"skill": "summarize", "model": PRICED_MODEL,
                        "permissions": ["net.read"]}},
            {"id": "approve", "type": "human_approval", "needs": ["work"],
             "config": {"action": "publish"}},
            {"id": "send", "type": "deliver", "needs": ["approve"],
             "config": {"to": "ana@example.com", "backend": "email"}},
        ],
    }
    base.update(overrides)
    return WorkflowDefinition.parse(base)


# ── preflight: zero effects ─────────────────────────────────────────────────

def test_preflight_calls_no_llm_and_no_subprocess():
    """ADP-16's own acceptance criterion: a dry-run never executes an LLM, a
    script or a download. Spy on the two real seams (`llm_core.stream_llm`,
    every subprocess entry point) and prove preflight() never touches them,
    even on a definition that names a model and a skill."""
    with patch("src.llm_core.stream_llm", side_effect=AssertionError("LLM called")), \
         patch("src.llm_core.stream_llm_with_fallback", side_effect=AssertionError("LLM called")), \
         patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run called")), \
         patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess.Popen called")):
        result = preflight(_definition(), prices=PRICES)
    assert result.tools == ("summarize",)


# ── preflight: what it enumerates ───────────────────────────────────────────

def test_preflight_enumerates_connections_tools_permissions_waits_and_outputs():
    result = preflight(_definition(), prices=PRICES)
    assert result.connections == ("deliver:email", "skill:summarize")
    assert result.tools == ("summarize",)
    assert result.permissions_required == ("net.read",)
    assert result.human_waits == ("approve",)
    assert result.outputs == ({"node_id": "send", "type": "deliver", "title": "send"},)
    assert result.inputs == ("inputs.score",)


def test_preflight_cost_is_unknown_not_zero_for_an_unpriced_model():
    definition = _definition()
    result = preflight(definition, prices=None)  # nothing priced
    assert result.cost["scenario"] == "unknown"
    assert result.cost["conservative"] == "unknown"
    assert PRICED_MODEL in result.cost["unpriced"]
    # The per-node breakdown ("coste por token") survives even while the
    # whole-run total ("presupuesto total") reads unknown.
    work_row = next(r for r in result.cost["per_node"] if r["node_id"] == "work")
    assert work_row["usd_min"] == 0.0


def test_preflight_cost_is_unknown_for_a_skill_node_with_no_model_even_if_others_are_priced():
    definition = WorkflowDefinition.parse({
        "id": "mixed", "version": "1.0.0", "title": "Mixed",
        "nodes": [
            {"id": "priced", "type": "skill", "config": {"skill": "a", "model": PRICED_MODEL}},
            {"id": "no_model", "type": "skill", "config": {"skill": "b"}},
        ],
    })
    result = preflight(definition, prices=PRICES)
    assert result.cost["scenario"] == "unknown"
    assert any("no_model" in entry for entry in result.cost["unpriced"])


def test_preflight_cost_is_a_number_when_every_invoked_model_is_priced():
    definition = WorkflowDefinition.parse({
        "id": "fully_priced", "version": "1.0.0", "title": "Fully priced",
        "nodes": [{"id": "work", "type": "skill",
                   "config": {"skill": "a", "model": PRICED_MODEL}}],
    })
    result = preflight(definition, prices=PRICES)
    assert isinstance(result.cost["scenario"], float)
    assert isinstance(result.cost["conservative"], float)
    assert result.cost["unpriced"] == []


def test_preflight_scenario_and_conservative_diverge_under_a_condition_gate():
    result = preflight(_definition(), prices=PRICES)
    assert result.token_estimate["scenario_tokens"] < result.token_estimate["conservative_tokens"]


def test_preflight_scenario_and_conservative_diverge_on_an_unbounded_cycle():
    a = WorkflowNode(id="a", type="skill", needs=("b",), config={"skill": "x", "model": PRICED_MODEL})
    b = WorkflowNode(id="b", type="skill", needs=("a",), config={"skill": "y", "model": PRICED_MODEL})
    definition = WorkflowDefinition(id="loopy", version="1.0.0", title="Loopy", nodes=(a, b))
    result = preflight(definition, prices=PRICES, assumed_iterations=4)
    assert result.token_estimate["conservative_tokens"] > result.token_estimate["scenario_tokens"]
    assert any(w["code"] == "LINT-WF-CYCLE-NO-BOUND" for w in result.warnings)


def test_preflight_installed_models_only_annotates_never_changes_cost_or_connections():
    with_flag = preflight(_definition(), prices=PRICES, installed_models=[PRICED_MODEL])
    without_flag = preflight(_definition(), prices=PRICES, installed_models=None)
    assert with_flag.cost == without_flag.cost
    assert with_flag.connections == without_flag.connections
    row = next(r for r in with_flag.token_estimate["per_node"] if r["node_id"] == "work")
    assert row["model_installed_locally"] is True
    row_unchecked = next(r for r in without_flag.token_estimate["per_node"] if r["node_id"] == "work")
    assert row_unchecked["model_installed_locally"] is None


def test_preflight_warnings_reuse_lint_workflow_findings():
    definition = WorkflowDefinition.parse({
        "id": "no_approval", "version": "1.0.0", "title": "No approval",
        "nodes": [{"id": "send", "type": "deliver", "config": {"to": "x@example.com"}}],
    })
    result = preflight(definition)
    codes = {w["code"] for w in result.warnings}
    assert "LINT-WF-EFFECT-NO-HUMAN" in codes


# ── interchange: export round-trips without changing semantics ─────────────

def test_export_canonical_round_trips_through_import_with_the_same_fingerprint():
    definition = _definition()
    exported = export_canonical(definition, layout={"start": {"x": 1.0, "y": 2.0}})
    assert exported["schema_version"] == 1
    imported = import_external(exported)
    assert imported["executable"] is True
    assert imported["rejected"] == []
    round_tripped = WorkflowDefinition.parse(imported["definition"])
    assert round_tripped.fingerprint() == definition.fingerprint()


def test_export_canonical_drops_layout_for_unknown_or_non_numeric_positions():
    definition = _definition()
    exported = export_canonical(definition, layout={
        "start": {"x": 1, "y": 2},
        "not_a_real_node": {"x": 9, "y": 9},
        "check": {"x": "left", "y": 0},
    })
    assert set(exported["layout"]) == {"start"}


def test_import_canonical_with_a_cycle_is_rejected_not_crashed():
    a = WorkflowNode(id="a", type="manual", needs=("b",))
    b = WorkflowNode(id="b", type="manual", needs=("a",))
    cyclic = WorkflowDefinition(id="cyclic", version="1.0.0", title="Cyclic", nodes=(a, b))
    envelope = {"schema_version": 1, "definition": cyclic.to_dict(), "layout": {},
               "design_only": [], "provenance": {}}
    result = import_external(envelope)
    assert result["executable"] is False
    assert result["definition"] is None
    assert result["rejected"]


# ── interchange: aigraphstudio-shaped import ────────────────────────────────

_AIGS_GRAPH = {
    "schemaVersion": 1, "id": "demo-graph", "title": "Demo",
    "nodes": [
        {"id": "n1", "type": "Start", "data": {"label": "Begin"}},
        {"id": "n2", "type": "Tool", "data": {"tool": "web_search", "label": "Search"}},
        {"id": "n3", "type": "Router",
         "data": {"label": "score check",
                  "condition": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
        {"id": "n4", "type": "Human Approval", "data": {"label": "Approve publish"}},
        {"id": "n5", "type": "Output", "data": {"label": "Published report"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2"},
        {"id": "e2", "source": "n2", "target": "n3"},
        {"id": "e3", "source": "n3", "target": "n4"},
        {"id": "e4", "source": "n4", "target": "n5"},
    ],
}


def test_import_aigraphstudio_maps_the_six_named_types_and_is_executable():
    result = import_external(_AIGS_GRAPH)
    assert result["executable"] is True
    assert result["design_only"] == []
    types = {n["id"]: n["type"] for n in result["definition"]["nodes"]}
    assert set(types.values()) == {"manual", "skill", "condition", "human_approval", "artifact_store"}
    # The graph reparses cleanly through the real contract — never a second,
    # discrepant cycle/needs validator.
    WorkflowDefinition.parse(result["definition"])


def test_import_aigraphstudio_keeps_agent_llm_code_as_design_only():
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Start"},
            {"id": "n2", "type": "Agent", "data": {"label": "Researcher"}},
            {"id": "n3", "type": "LLM"},
            {"id": "n4", "type": "Parallel"},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }
    result = import_external(graph)
    design_types = {row["type"] for row in result["design_only"]}
    assert design_types == {"Agent", "LLM", "Parallel"}
    # n1 (Start -> manual) is the only node that made it in, on its own.
    assert [n["id"] for n in result["definition"]["nodes"]] == ["n1"]
    assert result["executable"] is True


def test_import_aigraphstudio_cascades_design_only_instead_of_a_false_root():
    """A Tool node downstream of an unsupported Agent node must NOT become a
    root that runs unconditionally — its real prerequisite was dropped, so
    it is demoted to design_only too rather than faking readiness."""
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Agent", "data": {"label": "Plan"}},
            {"id": "n2", "type": "Tool", "data": {"tool": "send_email"}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }
    result = import_external(graph)
    assert result["definition"] is None
    assert result["executable"] is False
    design_ids = {row["id"] for row in result["design_only"]}
    assert design_ids == {"n1", "n2"}
    n2_entry = next(row for row in result["design_only"] if row["id"] == "n2")
    assert "n1" in n2_entry["reason"]


def test_import_aigraphstudio_refuses_an_unexpressible_router_as_design_only():
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Start"},
            {"id": "n2", "type": "Router", "data": {"label": "multi-branch, no single rule"}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }
    result = import_external(graph)
    assert any(row["id"] == "n2" for row in result["design_only"])
    assert [n["id"] for n in result["definition"]["nodes"]] == ["n1"]


def test_import_aigraphstudio_an_unknown_node_type_is_never_executable():
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Start"},
            {"id": "n2", "type": "SomethingNobodyNamed"},
        ],
        "edges": [],
    }
    result = import_external(graph)
    assert result["executable"] is False
    assert any("unrecognized" in row["reason"] for row in result["rejected"])


def test_import_aigraphstudio_a_cycle_among_executable_nodes_is_never_habilitated():
    """`_find_cycle` is never bypassed for an import, even though the source
    graph never called it: two Tool nodes that need each other still refuse
    to parse."""
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Tool", "data": {"tool": "a"}},
            {"id": "n2", "type": "Tool", "data": {"tool": "b"}},
        ],
        "edges": [
            {"source": "n1", "target": "n2"},
            {"source": "n2", "target": "n1"},
        ],
    }
    result = import_external(graph)
    assert result["executable"] is False
    assert result["definition"] is None
    assert result["rejected"]


def test_import_external_refuses_an_unrecognized_top_level_shape():
    result = import_external({"not": "a recognized shape"})
    assert result["executable"] is False
    assert result["definition"] is None


def test_import_external_refuses_a_non_object_payload():
    result = import_external(["not", "an", "object"])
    assert result["executable"] is False


def test_import_aigraphstudio_rejects_a_duplicate_node_id_instead_of_silently_overwriting():
    graph = {
        "schemaVersion": 1, "nodes": [
            {"id": "n1", "type": "Start", "data": {"label": "first"}},
            {"id": "n1", "type": "Tool", "data": {"tool": "x", "label": "second"}},
        ],
        "edges": [],
    }
    result = import_external(graph)
    assert any("duplicate" in row["reason"] for row in result["rejected"])
    # The first occurrence survives; the second never silently replaces it.
    assert [n["type"] for n in result["definition"]["nodes"]] == ["manual"]


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def workflows_client(tmp_path, monkeypatch):
    from routes.workflows_routes import setup_workflows_routes
    url = "sqlite:///" + (tmp_path / "wf_preflight.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_workflows_routes())
    yield TestClient(app)
    engine.dispose()


WORKFLOW_FLOW = {
    "id": "route.flow", "version": "1.0.0", "title": "Route flow",
    "nodes": [
        {"id": "start", "type": "manual", "config": {}},
        {"id": "work", "type": "skill", "needs": ["start"],
         "config": {"skill": "summarize", "model": PRICED_MODEL}},
    ],
}


def test_route_preflight_reports_unpriced_and_zero_effects(workflows_client):
    response = workflows_client.post("/api/workflows/preflight", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()["preflight"]
    assert body["cost"]["scenario"] == "unknown"
    assert body["tools"] == ["summarize"]


def test_route_preflight_prices_when_given_a_catalogue(workflows_client):
    response = workflows_client.post("/api/workflows/preflight", json={
        "definition": WORKFLOW_FLOW,
        "prices": {PRICED_MODEL: {"prompt_usd_per_1k": 0.001, "completion_usd_per_1k": 0.002}},
    })
    body = response.json()["preflight"]
    assert isinstance(body["cost"]["scenario"], float)


def test_route_preflight_rejects_malformed_installed_models(workflows_client):
    response = workflows_client.post("/api/workflows/preflight", json={
        "definition": WORKFLOW_FLOW, "installed_models": "not-a-list",
    })
    assert response.status_code == 400


def test_route_export_then_import_round_trips_over_http(workflows_client):
    exported = workflows_client.post("/api/workflows/export", json={"definition": WORKFLOW_FLOW})
    assert exported.status_code == 200
    envelope = exported.json()["export"]
    assert envelope["schema_version"] == 1
    imported = workflows_client.post("/api/workflows/import", json=envelope)
    assert imported.status_code == 200
    body = imported.json()
    assert body["executable"] is True
    assert body["definition"]["id"] == WORKFLOW_FLOW["id"]


def test_route_export_run_definition_matches_the_pinned_version(workflows_client):
    created = workflows_client.post("/api/workflows/runs", json={"definition": WORKFLOW_FLOW}).json()
    run_id = created["run_id"]
    response = workflows_client.get(f"/api/workflows/runs/{run_id}/export")
    assert response.status_code == 200
    assert response.json()["export"]["definition"]["id"] == WORKFLOW_FLOW["id"]


def test_route_export_run_definition_404s_for_an_unknown_run(workflows_client):
    assert workflows_client.get("/api/workflows/runs/wfr_nothing/export").status_code == 404


def test_route_import_an_aigraphstudio_graph_over_http(workflows_client):
    response = workflows_client.post("/api/workflows/import", json=_AIGS_GRAPH)
    assert response.status_code == 200
    body = response.json()
    assert body["executable"] is True
    assert body["design_only"] == []


def test_route_import_of_an_unrecognized_shape_is_200_ok_not_a_crash(workflows_client):
    response = workflows_client.post("/api/workflows/import", json={"nonsense": True})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["executable"] is False
