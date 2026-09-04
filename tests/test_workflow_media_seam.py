"""
tests/test_workflow_media_seam.py — a workflow that actually renders.

This is where Phase 3 and Phase 4 meet, and it is the first test in the repo
that walks the masterplan's product milestone end to end, in miniature:

    a brief → a render on the engine → a person approves it → done

with a real engine (the ComfyUI-shaped `ThreadingHTTPServer`), a real
database, a real artifact store, and the real workflow engine in between.

The two claims:

* **a render inside a workflow is a pause, not a block.** It takes minutes, so
  the node starts it and pauses with a wake time; nothing holds a thread, and
  a Faustus that dies mid-render comes back to a row that says which engine
  job it was waiting on;
* **the workflow does not start a second render.** Every pass that comes back
  recognises its own media run — which is the whole failure mode of the phase,
  seen from one level up.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import media_runs
from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers
from tests.test_comfyui_backend import FakeComfy
from tests.test_workflow_handlers import FakeApprovals


@pytest.fixture()
def world(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "seam.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)

    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))

    fake = FakeComfy()
    monkeypatch.setenv("COMFYUI_URL", fake.start())
    try:
        yield fake, WorkflowStore()
    finally:
        fake.stop()
        engine.dispose()


BRIEF = {
    "id": "brief.to.image", "version": "1.0.0", "title": "Brief to approved image",
    "nodes": [
        {"id": "brief", "type": "manual", "config": {}},
        {"id": "render", "type": "skill", "needs": ["brief"],
         "config": {"skill": "media:image.product",
                    "inputs": {"prompt": "a ceramic mug on white",
                               "aspect_ratio": "4:5"}}},
        {"id": "gate", "type": "human_approval", "needs": ["render"],
         "title": "Approve the image before it goes out",
         "config": {"action": "publish"}},
    ],
}


def test_a_workflow_renders_pauses_for_a_person_and_keeps_the_artifact(world):
    fake, store = world
    approvals = FakeApprovals()
    engine = WorkflowEngine(default_handlers(approvals=approvals), store)
    run_id = store.create_run(WorkflowDefinition.parse(BRIEF),
                              owner="luis")["run_id"]

    # First pass: the trigger runs, the render is queued, and the run pauses on
    # a WAKE TIME rather than holding anything open.
    first = engine.advance(run_id)
    assert first["status"] == "paused"
    assert first["waiting_on"] == "render"
    assert first["wake_at"] and not first["approval_id"]
    assert len(fake.submitted) == 1

    media_run_id = store.node_runs(run_id)["render"].result["media_run_id"]
    assert media_runs.get(media_run_id)["status"] == "queued"

    # Woken while the engine is still working: still one render, still waiting.
    _wake(store, run_id, "render")
    second = engine.advance(run_id)
    assert second["status"] == "paused" and second["waiting_on"] == "render"
    assert len(fake.submitted) == 1, "the workflow started a second render"

    # The engine finishes. The next wake collects the artifact and moves on to
    # the person.
    fake.finish("p1")
    _wake(store, run_id, "render")
    third = engine.advance(run_id)

    assert third["status"] == "paused" and third["waiting_on"] == "gate"
    assert third["approval_id"] == "apr_1"
    done = store.node_runs(run_id)["render"]
    assert done.status == "completed"
    assert len(done.result["artifact_ids"]) == 1

    # The artifact carries the recipe and the licence, from inside a workflow
    # just as it does from a direct render.
    art = done.result["artifacts"][0]
    assert art["provenance"]["recipe"] == "image.product"
    assert art["provenance"]["model_license"] == "CreativeML Open RAIL++-M"
    assert art["provenance"]["seed"] == media_runs.get(media_run_id)["values"]["seed"]

    # And the person says yes.
    approvals.cards["apr_1"]["status"] = "granted"
    finished = engine.resume(run_id, "gate")
    assert finished["status"] == "completed"


def test_a_render_that_fails_stops_the_workflow_with_the_engine_s_words(world):
    fake, store = world
    engine = WorkflowEngine(default_handlers(approvals=FakeApprovals()), store)
    run_id = store.create_run(WorkflowDefinition.parse(BRIEF))["run_id"]
    engine.advance(run_id)

    fake.fail("p1", node="KSampler", why="CUDA out of memory")
    _wake(store, run_id, "render")
    out = engine.advance(run_id)

    assert out["status"] == "failed"
    assert out["failed_nodes"] == ["render"]
    assert "out of memory" in str(out["ran"][0]["reason"])
    assert out["never_reached"] == ["gate"], "the gate was asked about a render that failed"


def test_a_template_the_render_node_names_wrongly_fails_before_the_engine(world):
    fake, store = world
    broken = {**BRIEF, "nodes": [
        {"id": "render", "type": "skill",
         "config": {"skill": "media:image.nope", "inputs": {"prompt": "x"}}}]}
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(WorkflowDefinition.parse(broken))["run_id"])

    assert out["status"] == "failed"
    assert "no_such_workflow" in str(out["ran"][0]["reason"])
    assert fake.submitted == []


def test_an_ordinary_skill_still_refuses_by_name(world):
    """Only `media:` is wired. Anything else says so, and says how — a node
    that silently did nothing would be the failure this whole design is for."""
    fake, store = world
    d = {**BRIEF, "nodes": [
        {"id": "write", "type": "skill", "config": {"skill": "document.report"}}]}
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(WorkflowDefinition.parse(d))["run_id"])

    assert out["status"] == "failed"
    reason = out["ran"][0]["reason"]
    assert "no runner is wired" in reason and "document.report" in reason
    assert "media:" in reason, "the refusal should point at what IS wired"


def _wake(store, run_id, node_id):
    """Pretend the wake time has arrived, the way a scheduler's clock would.

    Rewriting the stored wake time rather than sleeping: the engine wakes on a
    timestamp comparison, so a test that slept fifteen seconds would be
    testing `time.sleep`."""
    store.finish_node(run_id, node_id, status="paused",
                      result={**store.node_runs(run_id)[node_id].result,
                              "wake_at": "2020-01-01T00:00:00Z"})
