"""
tests/test_workflows_routes.py — workflows over HTTP.

Two things are worth pinning here beyond "the endpoints answer".

**A refusal comes back as a refusal, not a 500.** A definition with a cycle or
a retry on an email node is a user mistake, and `/validate` has to name the
field so the person can fix the line they wrote.

**Advancing twice is safe over HTTP too.** The idempotency work is in the
store, but a route that re-ran a claimed node would undo all of it, and a
double-clicked button is exactly how that gets discovered in production.
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
    url = "sqlite:///" + (tmp_path / "wf_routes.db").as_posix()
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


def test_validate_describes_a_good_definition_without_storing_anything(client):
    ok = client.post("/api/workflows/validate", json={"definition": FLOW})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert body["nodes"] == 3 and body["roots"] == ["start"]
    assert len(body["fingerprint"]) > 16

    # Nothing was created by looking at it.
    assert client.get("/api/workflows/runs/wfr_nothing").status_code == 404


def test_a_definition_that_could_never_start_is_refused_by_field(client):
    """A cycle is a mistake in the file someone wrote. It comes back as a
    named field and a message about the circle, not as a stack trace."""
    broken = {**FLOW, "nodes": [
        {"id": "a", "type": "condition", "needs": ["b"], "config": {}},
        {"id": "b", "type": "condition", "needs": ["a"], "config": {}},
    ]}
    out = client.post("/api/workflows/validate", json={"definition": broken})
    assert out.status_code == 200          # the endpoint worked; the file did not
    assert out.json()["ok"] is False
    assert "circle" in out.json()["reason"]
    assert "a" in out.json()["reason"] and "b" in out.json()["reason"]


def test_retrying_an_email_node_is_refused_when_the_run_is_created(client):
    broken = {**FLOW, "nodes": [
        {"id": "send", "type": "deliver", "max_attempts": 3,
         "config": {"to": "ana@example.com"}},
    ]}
    out = client.post("/api/workflows/runs", json={"definition": broken})
    assert out.status_code == 400
    assert "sends the email again" in out.json()["detail"]


def test_plan_says_what_would_run_first(client):
    out = client.post("/api/workflows/plan", json={"definition": FLOW})
    assert out.status_code == 200
    assert out.json()["starts_with"] == ["start"]
    assert {"id": "check", "needs": ["start"]} in out.json()["waiting"]


def test_a_run_advances_and_a_second_advance_does_not_redo_it(client):
    """The double-clicked button. The unwired `deliver` node fails the run —
    which is the honest outcome for this process — and the second call reads
    that instead of trying again."""
    created = client.post("/api/workflows/runs",
                          json={"definition": FLOW, "inputs": {"score": 90},
                                "advance": True})
    assert created.status_code == 200
    body = created.json()
    run_id = body["run_id"]
    first = body["result"]

    assert first["status"] == "failed"
    assert first["failed_nodes"] == ["send"]
    assert "no sender" in first["ran"][-1]["reason"]

    again = client.post(f"/api/workflows/runs/{run_id}/advance")
    assert again.status_code == 200
    assert again.json()["reason"] == "already_failed"
    assert again.json()["ran"] == []


def test_a_branch_not_taken_is_reported_rather_than_silently_missing(client):
    created = client.post("/api/workflows/runs",
                          json={"definition": FLOW, "inputs": {"score": 3},
                                "advance": True})
    result = created.json()["result"]
    assert result["status"] == "completed"
    assert result["not_taken"] == ["send"]


def test_a_redelivered_trigger_is_one_run(client):
    first = client.post("/api/workflows/runs",
                        json={"definition": FLOW, "dedupe_key": "evt-42"})
    second = client.post("/api/workflows/runs",
                         json={"definition": FLOW, "dedupe_key": "evt-42"})
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]


def test_reading_a_run_shows_the_definition_it_started_with(client):
    run_id = client.post("/api/workflows/runs",
                         json={"definition": FLOW, "inputs": {"score": 90}}
                         ).json()["run_id"]
    out = client.get(f"/api/workflows/runs/{run_id}")
    assert out.status_code == 200
    assert out.json()["definition"]["id"] == "report.publish"
    assert out.json()["runnable_now"] == ["start"]


def test_cancelling_a_run_stops_it_and_saying_it_twice_is_not_an_error(client):
    run_id = client.post("/api/workflows/runs", json={"definition": FLOW}).json()["run_id"]
    first = client.post(f"/api/workflows/runs/{run_id}/cancel",
                        json={"reason": "changed my mind"})
    assert first.json()["status"] == "cancelled"

    second = client.post(f"/api/workflows/runs/{run_id}/cancel")
    assert second.status_code == 200
    assert second.json()["reason"] == "already_cancelled"

    # And a cancelled run does not quietly carry on when something advances it.
    assert client.post(f"/api/workflows/runs/{run_id}/advance"
                       ).json()["reason"] == "already_cancelled"


def test_advancing_a_run_that_does_not_exist_is_a_404_not_a_crash(client):
    assert client.post("/api/workflows/runs/wfr_nope/advance").status_code == 404


def test_resuming_a_node_that_is_not_paused_is_a_conflict_not_a_500(client):
    run_id = client.post("/api/workflows/runs", json={"definition": FLOW}).json()["run_id"]
    out = client.post(f"/api/workflows/runs/{run_id}/resume/start")
    assert out.status_code == 409
    assert out.json()["detail"] == "not_paused"
