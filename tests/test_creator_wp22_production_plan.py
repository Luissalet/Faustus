"""WP22 — ProductionPlan: validate/estimate/compile/execute/cancel/status.

Real sqlite for core.database (WorkflowRunRow/NodeRunRow/MediaRunRow/
ApprovalRow — own isolated engine), real ProductionPlanStore and
DocumentStore (own sqlite files under tmp_path), real budget_account (own
file). The only fakes are `tests/creator_harness/fake_engines.FakeAdapter`
(WP36 — a real, minimal AdapterPort implementation, never a no-op stub) in
place of a real ComfyUI/ffmpeg engine, and WP07's capability/param lookups
(not landed in this lot — same seam `test_creator_preflight.py` stubs).
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from services.projects import ProjectStore
from src import budget_account
from src.creator import preflight as pf
from src.creator import production_plan as pp
from src.creator import storyboard
from src.creator.errors import InvalidOperation
from src.creator.store import DocumentStore

from tests.creator_harness.fake_engines import FakeAdapter

OWNER = "alice"


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wp22.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def own_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.sqlite3")


@pytest.fixture()
def stub_wp07(monkeypatch):
    monkeypatch.setattr(pf, "_capabilities_for", lambda deployment_id: {"operations": {"noop": {}}})
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: {"ok": True})


@pytest.fixture()
def project(tmp_path):
    ps = ProjectStore(str(tmp_path / "projects"))
    return ps.create("Trailer project", owner=OWNER)


@pytest.fixture()
def doc_store(tmp_path, monkeypatch):
    """production_plan._revalidate_against_current reads the module-level
    ``src.creator.store.get_store()`` singleton (the real document store any
    other Creator route also reads) — point IT at this test's isolated file
    too, and reset its process-wide cache, so both this fixture and
    production_plan see the exact same rows."""
    from src.creator import store as store_mod

    path = str(tmp_path / "docs.sqlite3")
    monkeypatch.setattr(store_mod, "default_path", lambda: path)
    monkeypatch.setattr(store_mod, "_store", None)
    return DocumentStore(path)


@pytest.fixture()
def plan_store(tmp_path):
    return pp.ProductionPlanStore(str(tmp_path / "plans.sqlite3"))


@pytest.fixture()
def brief_doc(doc_store, project):
    doc = storyboard.create(doc_store, OWNER, project["id"], [
        storyboard.new_scene("s1", "the harbor at dawn", start_ticks=0, duration_ticks=4000),
        storyboard.new_scene("s2", "the letter on the table", start_ticks=4000, duration_ticks=3000),
    ])
    return doc


@pytest.fixture()
def shared_adapter(monkeypatch):
    """One FakeAdapter instance shared across a whole test — every job it
    ever accepted stays queryable for the life of the test, exactly like a
    real long-lived engine connection would be."""
    adapter = FakeAdapter(slow_seconds=0.0)
    monkeypatch.setattr(pp, "_resolve_adapter", lambda adapter_id: adapter)
    return adapter


def _node(node_id, *, depends_on=(), scenario="success"):
    return {
        "id": node_id, "adapter_id": "fake", "operation": "noop",
        "parameters": {"scenario": scenario}, "input_assets": [],
        "depends_on": list(depends_on),
    }


def _raw_plan(brief_doc, *, nodes, criteria=None):
    return {
        "brief": {"document_id": brief_doc.id, "revision": brief_doc.revision},
        "nodes": nodes,
        "criteria": criteria or [
            {"id": "c1", "kind": "artifact_decodes", "target_node": nodes[-1]["id"]},
        ],
        "budget": {},
    }


def _pump(run_id, *, passes=10, sleep_s=1.2):
    """Drives the SAME handler set the background WorkflowScheduler uses
    (`src.workflows.runtime.production_handlers`) — this is what makes a
    plan step actually progress across its submit/collect passes."""
    from src.workflows.engine import WorkflowEngine
    from src.workflows.store import WorkflowStore
    from src.workflows.runtime import production_handlers

    store = WorkflowStore()
    engine = WorkflowEngine(production_handlers(), store)
    last = engine.advance(run_id, max_nodes=10)
    for _ in range(passes):
        if last.get("status") in ("completed", "failed", "cancelled"):
            return last
        time.sleep(sleep_s)
        last = engine.advance(run_id, max_nodes=10)
    return last


# ---------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------

def test_validate_rejects_dangling_depends_on(brief_doc):
    with pytest.raises(InvalidOperation):
        pp.validate(_raw_plan(brief_doc, nodes=[_node("a", depends_on=["ghost"])]))


def test_validate_rejects_a_cycle(brief_doc):
    with pytest.raises(InvalidOperation):
        pp.validate(_raw_plan(brief_doc, nodes=[
            _node("a", depends_on=["b"]), _node("b", depends_on=["a"]),
        ]))


def test_validate_rejects_unknown_retry_policy(brief_doc):
    raw = _raw_plan(brief_doc, nodes=[_node("a")])
    raw["nodes"][0]["retry_policy"] = "just_retry_it"
    with pytest.raises(InvalidOperation):
        pp.validate(raw)


def test_validate_accepts_a_well_formed_dag(brief_doc):
    plan = pp.validate(_raw_plan(brief_doc, nodes=[
        _node("a"), _node("b", depends_on=["a"]),
    ]), owner=OWNER, project_id="proj1")
    assert [n["id"] for n in plan.nodes] == ["a", "b"]


# ---------------------------------------------------------------------
# create / get (persistence)
# ---------------------------------------------------------------------

def test_create_and_get_roundtrip(plan_store, brief_doc, project):
    raw = _raw_plan(brief_doc, nodes=[_node("a")])
    plan = pp.create(plan_store, OWNER, project["id"], raw)
    assert plan.revision == 1
    fetched = pp.get(plan_store, OWNER, plan.id)
    assert fetched.id == plan.id
    assert [n["id"] for n in fetched.nodes] == ["a"]


def test_get_unknown_plan_raises_not_found(plan_store):
    with pytest.raises(pp.PlanNotFound):
        pp.get(plan_store, OWNER, "plan_nope")


def test_get_someone_elses_plan_is_not_found_too(plan_store, brief_doc, project):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[_node("a")]))
    with pytest.raises(pp.PlanNotFound):
        pp.get(plan_store, "mallory", plan.id)


# ---------------------------------------------------------------------
# estimate() — pure, no side effects, "unknown" honestly
# ---------------------------------------------------------------------

def test_estimate_reports_missing_without_touching_any_adapter(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, monkeypatch,
):
    calls = []
    monkeypatch.setattr(pp, "_resolve_adapter", lambda adapter_id: calls.append(adapter_id) or (_ for _ in ()).throw(AssertionError("estimate must never touch an adapter")))
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[
        _node("a"), _node("b", depends_on=["a"]),
    ]))
    result = pp.estimate(plan)
    assert result["ok"] is True
    assert calls == []
    assert len(result["steps"]) == 2


# ---------------------------------------------------------------------
# compile_to_workflow()
# ---------------------------------------------------------------------

def test_compile_to_workflow_mirrors_depends_on_as_needs(brief_doc, project):
    plan = pp.validate(_raw_plan(brief_doc, nodes=[
        _node("a"), _node("b", depends_on=["a"]),
    ]), owner=OWNER, project_id=project["id"], plan_id="plan_test1")
    definition = pp.compile_to_workflow(plan)
    by_id = {n.id: n for n in definition.nodes}
    assert by_id["b"].needs == ("a",)
    assert by_id["a"].type == "skill"
    assert by_id["a"].config["creator_plan_step"] is True
    assert by_id["a"].config["adapter_id"] == "fake"


# ---------------------------------------------------------------------
# execute() — DAG order, idempotency, revision drift
# ---------------------------------------------------------------------

def test_execute_respects_dependencies_and_completes(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, shared_adapter,
):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[
        _node("a"), _node("b", depends_on=["a"]),
    ]))
    started = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="run-1")
    status = _pump(started["workflow_run_id"], passes=12)
    assert status["status"] == "completed"

    final = pp.production_status(plan_store, started["production_id"], owner=OWNER)
    assert final["status"] == "completed"
    by_step = {s["step_id"]: s for s in final["steps"]}
    assert by_step["a"]["status"] == "completed"
    assert by_step["b"]["status"] == "completed"
    # 'b' only ever ran because 'a' had already finished — its MediaRun row
    # exists and both produced artifacts via production_runs.link_outputs.
    assert by_step["a"]["artifact_ids"]
    assert by_step["b"]["artifact_ids"]


def test_execute_a_failing_step_blocks_its_dependents(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, shared_adapter,
):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[
        _node("a", scenario="reject_before_queue"), _node("b", depends_on=["a"]),
    ]))
    started = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="run-1")
    status = _pump(started["workflow_run_id"], passes=8)
    assert status["status"] == "failed"

    final = pp.production_status(plan_store, started["production_id"], owner=OWNER)
    by_step = {s["step_id"]: s for s in final["steps"]}
    assert by_step["a"]["status"] == "failed"
    assert by_step["b"]["status"] in ("pending", "cancelled")  # never ran


def test_execute_is_idempotent_on_the_same_key(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, shared_adapter,
):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[_node("a")]))
    first = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="same-key")
    second = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="same-key")
    assert first["production_id"] == second["production_id"]
    assert first["workflow_run_id"] == second["workflow_run_id"]


def test_execute_refuses_when_input_documents_changed(
    own_database, own_budget, stub_wp07, plan_store, doc_store, brief_doc, project, shared_adapter,
):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[_node("a")]))
    # Edit the brief after the plan was built — bumps its revision.
    doc_store.apply_command(OWNER, brief_doc.id, "edit-1", brief_doc.revision, {
        "type": "storyboard.update_scene", "object_id": "s1", "brief": "changed after planning",
    })
    with pytest.raises(pp.PlanRevisionConflict) as excinfo:
        pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="run-1")
    assert excinfo.value.changed[0]["document_id"] == brief_doc.id


# ---------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------

def test_cancel_reconciles_an_in_flight_step_and_stops_the_rest(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, shared_adapter,
):
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[
        _node("a"), _node("b", depends_on=["a"]),
    ]))
    started = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="run-1")
    # execute() stops synchronously right after 'a' is submitted and paused
    # (its wake time is not due yet) — 'a' is in flight, 'b' never started.
    outcome = pp.cancel(plan_store, started["production_id"], owner=OWNER)
    assert outcome["status"] == "cancelled"
    by_step = {s["step_id"]: s for s in outcome["steps"]}
    assert by_step["a"]["outcome"] == "confirmed"
    assert by_step["b"]["outcome"] == "requested"

    final = pp.production_status(plan_store, started["production_id"], owner=OWNER)
    assert final["status"] == "cancelled"


def test_cancel_a_step_that_already_completed_is_too_late(
    own_database, own_budget, stub_wp07, plan_store, brief_doc, project, monkeypatch,
):
    adapter = FakeAdapter(slow_seconds=0.0)
    monkeypatch.setattr(pp, "_resolve_adapter", lambda adapter_id: adapter)
    plan = pp.create(plan_store, OWNER, project["id"], _raw_plan(brief_doc, nodes=[
        _node("a", scenario="cancel_too_late"),
    ]))
    started = pp.execute(plan_store, plan.id, owner=OWNER, idempotency_key="run-1")
    outcome = pp.cancel(plan_store, started["production_id"], owner=OWNER)
    by_step = {s["step_id"]: s for s in outcome["steps"]}
    assert by_step["a"]["outcome"] == "too_late"
