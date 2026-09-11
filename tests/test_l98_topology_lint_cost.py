"""tests/test_l98_topology_lint_cost.py — Lote A4 (aigraphstudio in Faustus).

Three pure modules and the routes that expose them:

* `src.topology_export` — Mermaid diagrams of a real future's branch tree and
  a real workflow's node graph.
* `src.agent_profile_lint` — findings over real `agent_profiles.catalog`
  profiles and a real `WorkflowDefinition`'s `needs` graph.
* `src.workflow_cost_estimate` — a cost estimate over the same definition.

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
from src.agent_profile_lint import find_cycles, lint_all, lint_profile, lint_workflow
from src.agent_profiles import catalog
from src.agent_profiles.catalog import BudgetProfile, VerificationProfile
from src.contracts.workflow import WorkflowDefinition, WorkflowNode
from src.durable_feature_store import DurableFeatureStore
from src.topology_export import future_to_mermaid, workflow_to_mermaid
from src.workflow_cost_estimate import ModelPrice, estimate


# ── topology_export.future_to_mermaid ───────────────────────────────────────

def _future(branches):
    return {"id": "future_abc123", "title": 'Pick "the" best path', "status": "evaluating",
            "branches": branches}


def test_future_to_mermaid_draws_the_root_and_every_branch_status():
    mermaid = future_to_mermaid(_future([
        {"id": "branch_1", "status": "pending", "strategy": {"id": "s1", "title": "Small"}},
        {"id": "branch_2", "status": "running", "strategy": {"id": "s2", "title": "Fast"}},
        {"id": "branch_3", "status": "completed", "strategy": {"id": "s3", "title": "Safe"}},
    ]))
    assert mermaid.startswith("flowchart TD")
    for branch_id in ("branch_1", "branch_2", "branch_3"):
        assert branch_id in mermaid
    for status in ("bstatus_pending", "bstatus_running", "bstatus_completed"):
        assert status in mermaid
    # Root -> each non-fusion branch.
    assert mermaid.count("-->") == 3


def test_future_to_mermaid_draws_a_fusion_branch_from_every_parent():
    mermaid = future_to_mermaid(_future([
        {"id": "branch_1", "status": "completed", "strategy": {"id": "s1", "title": "A"}},
        {"id": "branch_2", "status": "completed", "strategy": {"id": "s2", "title": "B"}},
        {"id": "branch_3", "status": "pending", "strategy": {"id": "fusion", "title": "A+B"},
         "parent_branch_ids": ["branch_1", "branch_2"]},
    ]))
    lines = mermaid.splitlines()
    # branch_3 (the fusion) is reached from BOTH branch_1 and branch_2, never
    # from the future root directly — two arrow lines end at its box, and
    # neither of them starts at the future's own node.
    fusion_box = next(ln for ln in lines if "branch_3" in ln and "[" in ln)
    fusion_node_id = fusion_box.strip().split("[", 1)[0]
    arrows_into_fusion = [ln for ln in lines
                          if "-->" in ln and ln.strip().endswith(fusion_node_id)]
    assert len(arrows_into_fusion) == 2
    assert not any(ln.strip().startswith("future_abc123") for ln in arrows_into_fusion)


def test_future_to_mermaid_escapes_quotes_and_brackets_in_labels():
    mermaid = future_to_mermaid(_future([
        {"id": "branch_1", "status": "failed",
         "strategy": {"id": "s1", "title": 'Use "raw" [SQL] | pipes'}},
    ]))
    # The literal quote/bracket/pipe characters from the title never reach
    # the diagram unescaped — each becomes a Mermaid character reference, so
    # they cannot be mistaken for Mermaid's own label-delimiter syntax.
    assert "#quot;" in mermaid
    assert "#91;" in mermaid and "#93;" in mermaid
    assert "#124;" in mermaid
    branch_line = next(ln for ln in mermaid.splitlines() if "branch_1[" in ln)
    assert '"raw"' not in branch_line
    assert "[SQL]" not in branch_line


# ── topology_export.workflow_to_mermaid ─────────────────────────────────────

CONDITIONAL_FLOW = {
    "id": "report.publish", "version": "1.0.0", "title": "Write and send",
    "nodes": [
        {"id": "start", "type": "manual", "config": {}},
        {"id": "check", "type": "condition", "needs": ["start"],
         "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
        {"id": "send", "type": "deliver", "needs": ["check"], "config": {"to": "ana@example.com"}},
    ],
}


def test_workflow_to_mermaid_draws_nodes_and_labels_the_conditional_edge():
    definition = WorkflowDefinition.parse(CONDITIONAL_FLOW)
    mermaid = workflow_to_mermaid(definition)
    assert mermaid.startswith("flowchart TD")
    assert "start" in mermaid and "check" in mermaid and "send" in mermaid
    # The edge OUT of the condition node names its check.
    assert "inputs.score" in mermaid
    assert "gte" in mermaid


def test_workflow_to_mermaid_accepts_a_raw_dict_like_validate_does():
    mermaid = workflow_to_mermaid(dict(CONDITIONAL_FLOW))
    assert "flowchart TD" in mermaid


def test_workflow_to_mermaid_escapes_a_quoted_pipe_in_a_title():
    flow = {**CONDITIONAL_FLOW, "nodes": [
        {"id": "start", "type": "manual", "title": 'Kick "off" | go'},
    ]}
    mermaid = workflow_to_mermaid(flow)
    assert "#quot;" in mermaid and "#124;" in mermaid


# ── agent_profile_lint: profile rules ───────────────────────────────────────

def test_lint_profile_flags_a_budget_with_no_cap():
    uncapped = BudgetProfile(
        id="uncapped_test", version="v1", max_tokens=0, max_seconds=900,
        max_rounds=10, max_tool_calls=50, max_branches=1, max_retries=1,
        allows_network=False, allows_gpu=False, description="test",
    )
    findings = lint_profile("budget", uncapped)
    codes = {f.code for f in findings}
    assert "LINT-BUDGET-NO-CAP" in codes
    hit = next(f for f in findings if f.code == "LINT-BUDGET-NO-CAP")
    assert hit.severity == "warn"
    assert "max_tokens" in hit.subject


def test_lint_profile_escalates_uncapped_spend_with_network_allowed():
    dangerous = BudgetProfile(
        id="dangerous_test", version="v1", max_tokens=0, max_seconds=900,
        max_rounds=10, max_tool_calls=50, max_branches=1, max_retries=1,
        allows_network=True, allows_gpu=False, description="test",
    )
    findings = lint_profile("budget", dangerous)
    codes = {f.code for f in findings}
    assert "LINT-BUDGET-UNCAPPED-NETWORK-SPEND" in codes
    hit = next(f for f in findings if f.code == "LINT-BUDGET-UNCAPPED-NETWORK-SPEND")
    assert hit.severity == "error"


def test_lint_profile_flags_verification_with_no_blocking_check():
    lax = VerificationProfile(
        id="lax_test", version="v1", checks=("changeset",), blocking=(),
        requires_reviewer=False, description="test",
    )
    findings = lint_profile("verification", lax)
    assert any(f.code == "LINT-VERIF-NO-BLOCKING" and f.severity == "warn" for f in findings)


def test_lint_profile_ok_budget_and_verification_have_no_findings():
    ok_budget = catalog.get("budget", "standard_v1")
    assert lint_profile("budget", ok_budget) == []
    ok_verification = catalog.get("verification", "full_delivery_v1")
    assert lint_profile("verification", ok_verification) == []


def test_lint_profile_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        lint_profile("not_a_kind", {})


def test_lint_all_walks_the_real_catalogue_and_returns_findings():
    findings = lint_all()
    assert isinstance(findings, list)
    # The shipped catalogue has no uncapped budgets — every LINT-BUDGET-*
    # finding, if any, would be a real bug in the catalogue file itself.
    assert not any(f.code.startswith("LINT-BUDGET") for f in findings)


# ── agent_profile_lint: workflow rules ──────────────────────────────────────

def test_find_cycles_and_lint_workflow_detect_a_hand_built_cycle():
    # WorkflowDefinition.parse() itself REFUSES a cycle, so this constructs
    # the dataclass directly — the case that bypassed validation, which is
    # exactly what a linter is for.
    a = WorkflowNode(id="a", type="wait", needs=("b",))
    b = WorkflowNode(id="b", type="wait", needs=("a",))
    definition = WorkflowDefinition(id="cyclic", version="1.0.0", title="Cyclic", nodes=(a, b))

    cycles = find_cycles(definition.nodes)
    assert len(cycles) == 1 and set(cycles[0]) == {"a", "b"}

    findings = lint_workflow(definition)
    hit = next(f for f in findings if f.code == "LINT-WF-CYCLE-NO-BOUND")
    assert hit.severity == "error"
    assert "a" in hit.subject and "b" in hit.subject


def test_lint_workflow_flags_a_dangling_need_as_unreachable():
    # "ghost" is not a node in this definition — a hand-edited typo that
    # WorkflowDefinition.parse() would refuse ("names ['ghost'], which no
    # node ... defines"). Constructed directly for the same reason as above.
    orphan = WorkflowNode(id="orphan", type="wait", needs=("ghost",))
    definition = WorkflowDefinition(id="dangling", version="1.0.0", title="Dangling",
                                    nodes=(orphan,))
    findings = lint_workflow(definition)
    hit = next(f for f in findings if f.code == "LINT-WF-UNREACHABLE")
    assert hit.severity == "error"
    assert "orphan" in hit.subject


def test_lint_workflow_flags_a_deliver_node_with_no_upstream_evaluator():
    definition = WorkflowDefinition.parse({
        "id": "no.gate", "version": "1.0.0", "title": "No gate",
        "nodes": [
            {"id": "start", "type": "manual", "config": {}},
            {"id": "send", "type": "deliver", "needs": ["start"], "config": {"to": "x@example.com"}},
        ],
    })
    findings = lint_workflow(definition)
    codes = {f.code for f in findings}
    assert "LINT-WF-OUTPUT-NO-EVALUATOR" in codes
    assert "LINT-WF-EFFECT-NO-HUMAN" in codes


def test_lint_workflow_is_quiet_when_a_condition_and_approval_gate_the_output():
    definition = WorkflowDefinition.parse({
        "id": "gated", "version": "1.0.0", "title": "Gated",
        "nodes": [
            {"id": "start", "type": "manual", "config": {}},
            {"id": "check", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": {"path": "x"}, "op": "gte", "right": 1}}},
            {"id": "approve", "type": "human_approval", "needs": ["check"], "config": {}},
            {"id": "send", "type": "deliver", "needs": ["approve"], "config": {"to": "x@example.com"}},
        ],
    })
    findings = lint_workflow(definition)
    codes = {f.code for f in findings}
    assert "LINT-WF-OUTPUT-NO-EVALUATOR" not in codes
    assert "LINT-WF-EFFECT-NO-HUMAN" not in codes
    assert "LINT-WF-CYCLE-NO-BOUND" not in codes
    assert "LINT-WF-UNREACHABLE" not in codes


# ── workflow_cost_estimate ───────────────────────────────────────────────────

PRICED_MODEL = "local:test-7b"
PRICES = {PRICED_MODEL: ModelPrice(prompt_usd_per_1k=0.001, completion_usd_per_1k=0.002)}


def test_estimate_prices_a_simple_bounded_workflow():
    definition = WorkflowDefinition.parse({
        "id": "priced", "version": "1.0.0", "title": "Priced",
        "nodes": [
            {"id": "start", "type": "manual", "config": {}},
            {"id": "work", "type": "skill", "needs": ["start"],
             "config": {"skill": "summarize", "model": PRICED_MODEL}},
        ],
    })
    result = estimate(definition, prices=PRICES, default_tokens_per_call=(1000, 200))
    assert result.calls_min == result.calls_max == 2
    expected_per_call = (1000 / 1000.0) * 0.001 + (200 / 1000.0) * 0.002
    assert result.total_usd_min == pytest.approx(expected_per_call)
    assert result.total_usd_max == pytest.approx(expected_per_call)
    assert result.unpriced_models == ()
    assert result.unbounded_loops == ()
    work_row = next(r for r in result.per_node if r["node_id"] == "work")
    assert work_row["model"] == PRICED_MODEL


def test_estimate_reports_an_unpriced_model_without_guessing_its_cost():
    definition = WorkflowDefinition.parse({
        "id": "unpriced", "version": "1.0.0", "title": "Unpriced",
        "nodes": [
            {"id": "work", "type": "skill", "config": {"skill": "summarize", "model": "mystery/model"}},
        ],
    })
    result = estimate(definition, prices=PRICES)
    assert result.unpriced_models == ("mystery/model",)
    assert result.total_usd_min == 0.0 and result.total_usd_max == 0.0


def test_estimate_bounds_an_unbounded_loop_with_assumed_iterations():
    a = WorkflowNode(id="a", type="skill", needs=("b",), config={"skill": "x", "model": PRICED_MODEL})
    b = WorkflowNode(id="b", type="skill", needs=("a",), config={"skill": "y", "model": PRICED_MODEL})
    definition = WorkflowDefinition(id="loopy", version="1.0.0", title="Loopy", nodes=(a, b))
    result = estimate(definition, prices=PRICES, assumed_iterations=4)
    assert len(result.unbounded_loops) == 1
    loop = result.unbounded_loops[0]
    assert set(loop["nodes"]) == {"a", "b"} and loop["assumed_iterations"] == 4
    for row in result.per_node:
        assert row["calls_min"] == 1 and row["calls_max"] == 4
    assert result.calls_max == 8 and result.calls_min == 2


def test_estimate_gives_a_zero_floor_to_a_node_gated_by_a_condition():
    definition = WorkflowDefinition.parse({
        "id": "gated_cost", "version": "1.0.0", "title": "Gated cost",
        "nodes": [
            {"id": "start", "type": "manual", "config": {}},
            {"id": "check", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": 1, "op": "gte", "right": 1}}},
            {"id": "maybe", "type": "skill", "needs": ["check"],
             "config": {"skill": "x", "model": PRICED_MODEL}},
        ],
    })
    result = estimate(definition, prices=PRICES)
    maybe_row = next(r for r in result.per_node if r["node_id"] == "maybe")
    assert maybe_row["calls_min"] == 0
    assert maybe_row["calls_max"] == 1
    start_row = next(r for r in result.per_node if r["node_id"] == "start")
    assert start_row["calls_min"] == start_row["calls_max"] == 1


# ── routes ───────────────────────────────────────────────────────────────────

WORKFLOW_FLOW = {
    "id": "route.flow", "version": "1.0.0", "title": "Route flow",
    "nodes": [
        {"id": "start", "type": "manual", "config": {}},
        {"id": "work", "type": "skill", "needs": ["start"],
         "config": {"skill": "summarize", "model": PRICED_MODEL}},
    ],
}


@pytest.fixture()
def workflows_client(tmp_path, monkeypatch):
    from routes.workflows_routes import setup_workflows_routes
    url = "sqlite:///" + (tmp_path / "wf_topology.db").as_posix()
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


def test_workflows_route_mermaid_renders_a_flowchart(workflows_client):
    response = workflows_client.post("/api/workflows/mermaid", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mermaid"].startswith("flowchart TD")
    assert "work" in body["mermaid"]


def test_workflows_route_mermaid_refuses_an_invalid_definition_like_validate_does(workflows_client):
    bad = {**WORKFLOW_FLOW, "nodes": [{"id": "x", "type": "not_a_real_type"}]}
    response = workflows_client.post("/api/workflows/mermaid", json={"definition": bad})
    assert response.status_code == 400


def test_workflows_route_estimate_prices_named_models(workflows_client):
    response = workflows_client.post("/api/workflows/estimate", json={
        "definition": WORKFLOW_FLOW,
        "prices": {PRICED_MODEL: {"prompt_usd_per_1k": 0.001, "completion_usd_per_1k": 0.002}},
    })
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert body["calls_min"] == body["calls_max"] == 2
    assert body["total_usd_min"] > 0
    assert body["unpriced_models"] == []


def test_workflows_route_estimate_without_prices_reports_unpriced(workflows_client):
    response = workflows_client.post("/api/workflows/estimate", json={"definition": WORKFLOW_FLOW})
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert body["unpriced_models"] == [PRICED_MODEL]
    assert body["total_usd_min"] == 0.0


@pytest.fixture()
def branching_client(tmp_path, monkeypatch):
    import routes.branching_futures_routes as branching_routes
    from src.branching_futures.service import BranchingService
    service = BranchingService(DurableFeatureStore(str(tmp_path / "futures_topology.db")))
    monkeypatch.setattr(branching_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(branching_routes, "_owner", lambda _request: "alice")
    monkeypatch.setattr(branching_routes, "enabled", lambda: True)
    monkeypatch.setattr(branching_routes, "branching_service", lambda: service)
    app = FastAPI()
    app.include_router(branching_routes.setup_branching_futures_routes())
    return TestClient(app)


def test_branching_route_mermaid_renders_the_future_tree(branching_client):
    created = branching_client.post("/api/futures", json={
        "title": "Choose", "intent": "Compare implementations", "mode": "simulate",
        "strategies": [{"id": "small", "title": "Small"}, {"id": "fast", "title": "Fast"}],
    })
    assert created.status_code == 200
    future_id = created.json()["future"]["id"]
    response = branching_client.get(f"/api/futures/{future_id}/mermaid")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mermaid"].startswith("flowchart TD")


def test_branching_route_mermaid_404s_for_an_unknown_future(branching_client):
    response = branching_client.get("/api/futures/does_not_exist/mermaid")
    assert response.status_code == 404


@pytest.fixture()
def agent_profiles_client(monkeypatch):
    from routes.agent_profiles_routes import setup_agent_profiles_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_agent_profiles_routes())
    return TestClient(app)


def test_agent_profiles_route_lint_returns_the_catalogue_findings(agent_profiles_client):
    response = agent_profiles_client.get("/api/agent-profiles/lint")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["findings"], list)


def test_agent_profiles_route_lint_one_profile(agent_profiles_client):
    response = agent_profiles_client.get("/api/agent-profiles/lint/budget/standard_v1")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["findings"] == []


def test_agent_profiles_route_lint_one_unknown_profile_is_a_named_refusal(agent_profiles_client):
    response = agent_profiles_client.get("/api/agent-profiles/lint/budget/does_not_exist")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "does_not_exist" in body["error"]["message"]
