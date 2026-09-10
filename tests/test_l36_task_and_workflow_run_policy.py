"""Lot 36, requirement 4: `src.task_scheduler.set_task_policy` and
`WorkflowStore.create_run(budget_preset=..., permissions=...)` (AUTO-02) both
already existed and were already enforced (see
`tests/test_auto_02_task_budget_permissions.py`,
`tests/test_auto_02_workflow_budget_permissions.py`) — but neither ROUTE ever
forwarded the fields from the request body, so there was no way to actually
declare a policy through the API a real client uses.
`routes/task/task_routes.py::create_task` and
`routes/workflows_routes.py::create_run` now do.

Both tests go through a real `TestClient` HTTP POST against the real route
(rule 7 of COMUN.md), not a direct function call.
"""
from __future__ import annotations

import os
import tempfile
import uuid

# Same DATA_DIR-isolation idiom tests/test_auto_02_task_budget_permissions.py
# already uses: `set_task_policy`'s sqlite file path is computed once, at
# `src.task_scheduler` import time, from `src.constants.DATA_DIR` — this must
# run BEFORE that import, and `setdefault` so an earlier test module's choice
# (whichever imports first) always wins instead of two tmp dirs disagreeing.
_tmp_data = tempfile.mkdtemp(prefix="odysseus-l36-task-policy-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool, NullPool

from core import database as db_mod, middleware


# --------------------------------------------------------------------------
# POST /api/tasks — dst_ambiguity_policy / misfire_policy / budget_preset /
# permissions now reach `set_task_policy`.
# --------------------------------------------------------------------------

@pytest.fixture()
def task_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db_mod.Base.metadata.create_all(bind=engine)

    import routes.task.task_routes as task_routes
    monkeypatch.setattr(task_routes, "SessionLocal", session_factory)

    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-owner", "alice")
        return await call_next(request)

    app.include_router(task_routes.setup_task_routes(MagicMock()))
    client = TestClient(app)
    yield client
    engine.dispose()


def test_create_task_declares_policy_when_fields_are_sent(task_client):
    from src.task_scheduler import get_task_policy

    response = task_client.post("/api/tasks", json={
        "name": "policy-declared",
        "prompt": "do the thing",
        "task_type": "llm",
        "trigger_type": "event",
        "trigger_event": "session_created",
        "trigger_count": 1,
        "dst_ambiguity_policy": "run_once",
        "misfire_policy": "skip",
        "budget_preset": "bounded_autonomous",
        "permissions": ["web_search", "read_file"],
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["policy"] == {
        "dst_ambiguity_policy": "run_once",
        "misfire_policy": "skip",
        "budget_preset": "bounded_autonomous",
        "permissions": ["web_search", "read_file"],
    }
    # And it actually landed in the real policy store `TaskScheduler.
    # _execute_llm_task` reads from — not just echoed back in the response.
    persisted = get_task_policy(body["id"])
    assert persisted["budget_preset"] == "bounded_autonomous"
    assert persisted["permissions"] == ["web_search", "read_file"]
    assert persisted["dst_ambiguity_policy"] == "run_once"
    assert persisted["misfire_policy"] == "skip"


def test_create_task_without_policy_fields_declares_none(task_client):
    """No capability lost (COMUN.md rule 3): a client that sends none of the
    new fields — every client before this lot — gets back no `policy` key,
    same response shape as always."""
    response = task_client.post("/api/tasks", json={
        "name": "no-policy",
        "prompt": "do the thing",
        "task_type": "llm",
        "trigger_type": "event",
        "trigger_event": "session_created",
        "trigger_count": 1,
    })
    assert response.status_code == 200, response.text
    assert "policy" not in response.json()


def test_create_task_rejects_an_invalid_budget_preset_before_creating_the_row(task_client):
    response = task_client.post("/api/tasks", json={
        "name": "bad-preset",
        "prompt": "do the thing",
        "task_type": "llm",
        "trigger_type": "event",
        "trigger_event": "session_created",
        "trigger_count": 1,
        "budget_preset": "not-a-real-preset",
    })
    assert response.status_code == 400
    assert "budget_preset" in response.json()["detail"]


# --------------------------------------------------------------------------
# POST /api/workflows/runs — budget_preset / permissions now reach
# WorkflowStore.create_run (already implemented and enforced by
# WorkflowEngine.advance; only the route never forwarded them).
# --------------------------------------------------------------------------

def _definition():
    return {
        "id": "report.publish", "version": "1.0.0", "title": "Write and send",
        "nodes": [{"id": "gather", "type": "skill", "config": {"skill": "research"}}],
    }


@pytest.fixture()
def workflow_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    from routes.workflows_routes import setup_workflows_routes
    app = FastAPI()
    app.include_router(setup_workflows_routes())
    client = TestClient(app)
    yield client
    engine.dispose()


def test_create_run_forwards_budget_preset_and_permissions(workflow_client):
    from src.workflows import WorkflowStore

    response = workflow_client.post("/api/workflows/runs", json={
        "definition": _definition(),
        "owner": "alice",
        "budget_preset": "read_only",
        "permissions": ["skill"],
    })

    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    policy = WorkflowStore().get_policy(run_id)
    assert policy == {"budget_preset": "read_only", "permissions": ["skill"]}


def test_create_run_without_policy_fields_keeps_the_default(workflow_client):
    from src.autonomy_budget import DEFAULT_PRESET
    from src.workflows import WorkflowStore

    response = workflow_client.post("/api/workflows/runs", json={
        "definition": _definition(), "owner": "alice",
    })
    assert response.status_code == 200, response.text
    policy = WorkflowStore().get_policy(response.json()["run_id"])
    assert policy == {"budget_preset": DEFAULT_PRESET, "permissions": None}


def test_create_run_rejects_an_invalid_budget_preset(workflow_client):
    response = workflow_client.post("/api/workflows/runs", json={
        "definition": _definition(), "owner": "alice",
        "budget_preset": "not-a-real-preset",
    })
    assert response.status_code == 400
