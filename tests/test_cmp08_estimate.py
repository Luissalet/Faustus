"""tests/test_cmp08_estimate.py — CMP-08 (W2-B, `CONTRATO_CMP_W2.md`).

`src.workflow_cost_estimate.estimate_detailed`, `src.plan_compare.compare`,
and the routes that expose them (`/api/workflows/estimate?detail=1`,
`/api/workflows/compare-plans`). The four decisive checks the contract asks
for:

1. Separate accounts — a non-`skill` activation is never counted as a model
   call, and a `deliver`/`artifact_store` activation is an external
   operation, not a model call either.
2. Two composite skills naming the SAME model do not collapse into the same
   estimate — a declared `calls_profile` (or its absence) tells them apart,
   and an uncharacterized composite skill's calls stay `"unknown"`, never
   silently `1`.
3. `structural_bounds` (no assumption) / `forecast_with_assumptions`
   (assumed_iterations applied, listed) / `measured` (from a real run, or
   `None` with a documented reason) are three different things, never one
   blended number.
4. Price is always structured and sourced (`{amount_*, unit, currency,
   source, as_of}`) and never invented — an unpriced model contributes $0
   and is named, never guessed.

No network calls anywhere in this file.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from src.contracts.workflow import WorkflowDefinition, WorkflowNode
from src.plan_compare import compare
from src.workflow_cost_estimate import (
    ModelPrice, StructuredPrice, calls_profile_from_mapping,
    estimate_detailed, price_from_openrouter_raw,
)

MODEL = "local:test-7b"


def _definition(nodes, *, wf_id="wf.test"):
    return WorkflowDefinition(id=wf_id, version="1.0.0", title="Test", nodes=tuple(nodes))


# ── 1. separate accounts ────────────────────────────────────────────────────

def test_non_skill_and_effectful_activations_are_never_counted_as_model_calls():
    definition = _definition([
        WorkflowNode(id="start", type="manual"),
        WorkflowNode(id="work", type="skill", needs=("start",), config={"model": MODEL}),
        WorkflowNode(id="send", type="deliver", needs=("work",), config={"backend": "email"}),
    ])
    result = estimate_detailed(definition)
    assert result.node_activations == {"min": 3, "max": 3}
    assert result.model_calls == {"min": 1, "max": 1}
    assert result.external_ops == {"min": 1, "max": 1}
    rows = {row["node_id"]: row for row in result.to_dict()["per_node"]}
    assert rows["start"]["is_model_call"] is False and rows["start"]["is_external_op"] is False
    assert rows["work"]["is_model_call"] is True
    assert rows["send"]["is_external_op"] is True and rows["send"]["is_model_call"] is False


# ── 2. composite skills with the same model do not collapse ────────────────

def test_two_composite_skills_on_the_same_model_get_different_estimates():
    definition = _definition([
        WorkflowNode(id="deep", type="skill", config={"skill": "research_pack", "model": MODEL}),
        WorkflowNode(id="quick", type="skill", config={"skill": "quick_pack", "model": MODEL}),
        WorkflowNode(id="mystery", type="skill", config={"skill": "mystery_pack", "model": MODEL}),
    ])
    profiles = {
        "research_pack": {"model_calls": 3, "external_ops": 1, "tokens_in": 1000, "tokens_out": 500},
        "quick_pack": {"model_calls": 1, "external_ops": 0, "tokens_in": 200, "tokens_out": 100},
    }
    result = estimate_detailed(definition, skill_calls_profiles=profiles)
    rows = {row["node_id"]: row for row in result.to_dict()["per_node"]}

    assert rows["deep"]["model_calls"] == {"min": 3, "max": 3}
    assert rows["deep"]["calls_profile_source"] == "declared"
    assert rows["quick"]["model_calls"] == {"min": 1, "max": 1}
    assert rows["deep"]["tokens_in"] != rows["quick"]["tokens_in"]

    # An uncharacterized composite skill is unknown, never defaulted to "1 call".
    assert rows["mystery"]["calls_profile_source"] == "unknown"
    assert rows["mystery"]["model_calls"] == {"min": 0, "max": 0}
    assert any("mystery_pack" in reason for reason in result.cost_unestimable)

    assert result.model_calls == {"min": 4, "max": 4}  # 3 + 1, mystery excluded


def test_calls_profile_falls_back_to_history_then_unknown():
    assert calls_profile_from_mapping({"model_calls": 2, "tokens_in": 10, "tokens_out": 5}, source="history").source == "history"
    assert calls_profile_from_mapping("not-a-mapping", source="history") is None
    assert calls_profile_from_mapping(None, source="declared") is None

    definition = _definition([WorkflowNode(id="a", type="skill", config={"skill": "s", "model": MODEL})])
    result = estimate_detailed(definition, skill_call_history={"s": {"model_calls": 2, "tokens_in": 10, "tokens_out": 5}})
    row = result.to_dict()["per_node"][0]
    assert row["calls_profile_source"] == "history"
    assert row["model_calls"] == {"min": 2, "max": 2}


# ── 3. structural / forecast / measured are three different things ─────────

def test_structural_bounds_stays_unbounded_while_forecast_applies_the_assumption():
    a = WorkflowNode(id="a", type="skill", needs=("b",), config={"skill": "x", "model": MODEL})
    b = WorkflowNode(id="b", type="skill", needs=("a",), config={"skill": "y", "model": MODEL})
    definition = _definition([a, b], wf_id="loopy")

    result = estimate_detailed(definition, assumed_iterations=3)
    as_dict = result.to_dict()

    assert as_dict["structural_bounds"]["node_activations"]["max"] == "unbounded"
    assert as_dict["forecast_with_assumptions"]["node_activations"]["max"] == 6  # 3 iters x 2 nodes
    assert as_dict["forecast_with_assumptions"]["assumed_iterations"] == 3
    assert any("cycles" in a and "unbounded" in a for a in result.assumptions)
    assert result.measured is None


def test_measured_is_none_and_documented_without_a_run_but_populated_with_one():
    definition = _definition([WorkflowNode(id="start", type="manual")])
    without_run = estimate_detailed(definition)
    assert without_run.measured is None

    with_run = estimate_detailed(definition, run_measured={"tool_calls": 5, "active_seconds": 12.0})
    assert with_run.measured["tool_calls"] == 5
    assert with_run.measured["active_seconds"] == 12.0
    # Never backfilled from the forecast — the module is honest that no
    # per-node token/cost ledger exists for a workflow run today.
    assert with_run.measured["tokens_in"] is None
    assert with_run.measured["cost_usd"] is None
    assert "usage_so_far" in with_run.measured["note"]


# ── 4. price is structured, sourced, and never invented ────────────────────

def test_price_from_openrouter_raw_is_structured_and_per_million():
    price = price_from_openrouter_raw({"pricing": {"prompt": "0.000002", "completion": "0.000006"}},
                                       as_of="2026-09-11")
    assert isinstance(price, StructuredPrice)
    as_dict = price.to_dict()
    assert as_dict["amount_prompt_per_1m"] == pytest.approx(2.0)
    assert as_dict["amount_completion_per_1m"] == pytest.approx(6.0)
    assert as_dict["unit"] == "per_1M_tokens"
    assert as_dict["source"] == "openrouter_pricing_field"
    assert as_dict["as_of"] == "2026-09-11"

    assert price_from_openrouter_raw({"pricing": {"prompt": "not-a-number"}}) is None
    assert price_from_openrouter_raw({}) is None


def test_unpriced_model_costs_zero_and_is_named_not_guessed():
    definition = _definition([WorkflowNode(id="work", type="skill", config={"model": MODEL})])
    result = estimate_detailed(definition)  # no prices, no capability_pricing at all
    assert result.cost_known_usd == {"min": 0.0, "max": 0.0}
    assert any(MODEL in reason for reason in result.cost_unestimable)
    assert result.model_calls == {"min": 1, "max": 1}  # the call itself is still counted


def test_capability_pricing_wins_only_when_actually_present_never_assumed():
    definition = _definition([WorkflowNode(id="work", type="skill", config={"model": MODEL})])
    priced = estimate_detailed(
        definition,
        capability_pricing={MODEL: {"pricing": {"prompt": "0.000001", "completion": "0.000002"}}},
    )
    assert priced.cost_known_usd["max"] > 0
    assert priced.prices_used[MODEL]["source"] == "openrouter_pricing_field"

    fallback_prices = {MODEL: ModelPrice(prompt_usd_per_1k=0.001, completion_usd_per_1k=0.002)}
    via_caller = estimate_detailed(definition, prices=fallback_prices)
    assert via_caller.cost_known_usd["max"] > 0
    assert via_caller.prices_used[MODEL]["source"] == "caller_prices"


# ── plan_compare: "por qué este plan" ───────────────────────────────────────

def test_plan_compare_tags_each_cell_computed_estimated_or_unknown():
    single_model = _definition([WorkflowNode(id="work", type="skill", config={"model": MODEL})],
                                wf_id="single_model")
    deterministic_steps = _definition([WorkflowNode(id="start", type="manual"),
                                        WorkflowNode(id="wait", type="wait", needs=("start",))],
                                       wf_id="deterministic_steps")
    agent_team = _definition([
        WorkflowNode(id="a", type="skill", config={"skill": "research_pack", "model": MODEL}),
        WorkflowNode(id="b", type="skill", config={"skill": "unknown_pack", "model": MODEL}),
    ], wf_id="agent_team")

    result = compare(
        "answer a research question",
        [
            {"id": "single_model", "label": "One model", "definition": single_model},
            {"id": "deterministic_steps", "label": "Deterministic", "definition": deterministic_steps},
            {"id": "agent_team", "label": "Agent team", "definition": agent_team},
        ],
        prices={MODEL: ModelPrice(prompt_usd_per_1k=0.001, completion_usd_per_1k=0.002)},
    )
    as_dict = result.to_dict()
    assert as_dict["plan_ids"] == ["single_model", "deterministic_steps", "agent_team"]
    rows = {row["metric"]: row["cells"] for row in as_dict["rows"]}

    assert rows["model_calls_max"]["deterministic_steps"] == {"value": 0, "basis": "computed"}
    assert rows["model_calls_max"]["single_model"]["basis"] == "computed"
    # agent_team has an uncharacterized skill ("unknown_pack") touching model_calls.
    assert rows["model_calls_max"]["agent_team"]["basis"] == "unknown"
    assert any("unknown_pack" in r for r in as_dict["reasons"]["agent_team"])


def test_plan_compare_reports_a_bad_plan_without_failing_the_others():
    good = _definition([WorkflowNode(id="start", type="manual")], wf_id="good")
    result = compare("goal", [
        {"id": "good", "definition": good},
        {"id": "bad", "definition": {"id": "bad"}},  # missing required fields -> parse fails
    ])
    assert "good" in result.detail
    assert "bad" in result.errors
    assert "bad" not in result.detail


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def workflows_client(tmp_path, monkeypatch):
    from routes.workflows_routes import setup_workflows_routes
    url = "sqlite:///" + (tmp_path / "wf_cmp08.db").as_posix()
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
        {"id": "work", "type": "skill", "needs": ["start"], "config": {"model": MODEL}},
        {"id": "send", "type": "deliver", "needs": ["work"], "config": {"backend": "email"}},
    ],
}


def test_route_estimate_plain_shape_is_unchanged_without_detail(workflows_client):
    response = workflows_client.post("/api/workflows/estimate", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert "calls_min" in body and "node_activations" not in body


def test_route_estimate_detail_separates_accounts(workflows_client):
    response = workflows_client.post("/api/workflows/estimate?detail=1", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert body["model_calls"] == {"min": 1, "max": 1}
    assert body["external_ops"] == {"min": 1, "max": 1}
    assert body["node_activations"] == {"min": 3, "max": 3}
    assert body["measured"] is None


def test_route_compare_plans(workflows_client):
    response = workflows_client.post("/api/workflows/compare-plans", json={
        "goal": "publish a report",
        "plans": [
            {"id": "single_model", "definition": WORKFLOW_FLOW},
            {"id": "deterministic_steps", "definition": {
                "id": "det", "version": "1.0.0", "title": "Det",
                "nodes": [{"id": "start", "type": "manual", "config": {}}],
            }},
        ],
    })
    assert response.status_code == 200
    body = response.json()["comparison"]
    assert body["plan_ids"] == ["single_model", "deterministic_steps"]
    assert len(body["rows"]) == 6


def test_route_compare_plans_rejects_empty_plans(workflows_client):
    response = workflows_client.post("/api/workflows/compare-plans", json={"goal": "g", "plans": []})
    assert response.status_code == 400


def test_route_preflight_carries_cost_detail(workflows_client):
    response = workflows_client.post("/api/workflows/preflight", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()["preflight"]
    assert "cost_detail" in body
    assert body["cost_detail"]["model_calls"] == {"min": 1, "max": 1}
