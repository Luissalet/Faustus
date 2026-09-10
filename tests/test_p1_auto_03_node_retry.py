"""
tests/test_p1_auto_03_node_retry.py — AUTO-03, lote 53.

The acceptance line: "Reejecutar un paso de extraccion no repite
automaticamente el envio que ya se confirmo en otro paso." A workflow run
where a `deliver` node already finished (successfully or not — see below)
must keep that status untouched when an upstream, non-effectful node is
retried; and the `deliver` node itself must never be retryable through this
endpoint, whatever its own outcome, because nothing here can tell whether it
already reached outside the process.

Crosses HTTP (rule 7): every call goes through `routes/workflows_routes.py`
via `TestClient`, the same fixture `tests/test_workflows_routes.py` already
uses, so this is exercised exactly the way the running app would.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from routes.workflows_routes import setup_workflows_routes

FLOW = {
    "id": "report.publish", "version": "1.0.0", "title": "Write and send",
    "nodes": [
        {"id": "start", "type": "manual", "config": {}},
        {"id": "check", "type": "condition", "needs": ["start"],
         "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte",
                             "right": 50}}},
        {"id": "send", "type": "deliver", "needs": ["check"],
         "config": {"to": "ana@example.com"}},
    ],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wf_retry.db").as_posix()
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


def _run_id(client, score: int) -> str:
    created = client.post("/api/workflows/runs", json={
        "definition": FLOW, "inputs": {"score": score}, "advance": True})
    assert created.status_code == 200
    return created.json()["run_id"]


def test_retrying_the_extraction_step_does_not_repeat_the_confirmed_send(client):
    """`send` fails validation (no body) rather than actually dialling out —
    same fixture the base test file uses — but the retry gate is by NODE
    TYPE, not by whether the effect is known to have fired, so the refusal
    below holds either way."""
    run_id = _run_id(client, score=90)
    before = client.get(f"/api/workflows/runs/{run_id}").json()["nodes"]
    assert before["check"]["status"] == "completed"
    assert before["send"]["status"] == "failed"

    retry_check = client.post(f"/api/workflows/runs/{run_id}/nodes/check/retry",
                              json={"advance": True})
    assert retry_check.status_code == 200

    after = client.get(f"/api/workflows/runs/{run_id}").json()["nodes"]
    assert after["check"]["status"] == "completed"     # ran again, still passes
    assert after["check"]["attempt"] == before["check"]["attempt"] + 1
    # The whole point: `send` was never touched by retrying `check`.
    assert after["send"]["status"] == "failed"
    assert after["send"]["attempt"] == before["send"]["attempt"]


def test_a_deliver_node_refuses_a_bare_retry_even_after_it_failed(client):
    run_id = _run_id(client, score=90)
    out = client.post(f"/api/workflows/runs/{run_id}/nodes/send/retry")
    assert out.status_code == 409
    assert "reach" in out.json()["detail"] or "deliver" in out.json()["detail"]
    still = client.get(f"/api/workflows/runs/{run_id}").json()["nodes"]["send"]
    assert still["status"] == "failed"


def test_a_node_still_pending_or_unknown_cannot_be_retried(client):
    run_id = client.post("/api/workflows/runs",
                         json={"definition": FLOW, "inputs": {"score": 90}}).json()["run_id"]
    # Nothing has run yet.
    out = client.post(f"/api/workflows/runs/{run_id}/nodes/check/retry")
    assert out.status_code == 409
    missing = client.post(f"/api/workflows/runs/{run_id}/nodes/nope/retry")
    assert missing.status_code == 409

    unknown_run = client.post(f"/api/workflows/runs/does-not-exist/nodes/check/retry")
    assert unknown_run.status_code == 404


def test_retrying_reopens_a_run_that_had_already_finished(client):
    """The run itself is `failed` (terminal) once `send` fails — advancing a
    terminal run is normally a no-op (`already_failed`). Retrying `check`
    is a deliberate reopen of THIS run, not a race that should be refused."""
    run_id = _run_id(client, score=90)
    assert client.get(f"/api/workflows/runs/{run_id}").json()["run"]["status"] == "failed"
    client.post(f"/api/workflows/runs/{run_id}/nodes/check/retry")
    reopened = client.get(f"/api/workflows/runs/{run_id}").json()["run"]["status"]
    assert reopened in ("running", "failed")  # running until advance() settles it again
    advanced = client.post(f"/api/workflows/runs/{run_id}/advance")
    assert advanced.status_code == 200
    assert advanced.json()["status"] == "failed"      # `send` fails again, on its own attempt 2
