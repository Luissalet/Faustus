"""WP22 — HTTP layer over production_plan, real TestClient.

Same `AUTH_ENABLED=false` no-login mode every other route test uses; real
sqlite for core.database/production_plans/documents (own files); the fake
adapter (WP36) in place of a real engine.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from services.projects import ProjectStore
from src import budget_account
from src.creator import preflight as pf
from src.creator import production_plan as pp
from src.creator import storyboard
from src.creator.store import DocumentStore

from tests.creator_harness.fake_engines import FakeAdapter

OWNER = "__odysseus_local__"


@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    url = "sqlite:///" + (tmp_path / "wp22_routes.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.sqlite3")
    monkeypatch.setattr(pf, "_capabilities_for", lambda deployment_id: {"operations": {"noop": {}}})
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: {"ok": True})

    from src.creator import store as store_mod
    doc_path = str(tmp_path / "docs.sqlite3")
    monkeypatch.setattr(store_mod, "default_path", lambda: doc_path)
    monkeypatch.setattr(store_mod, "_store", None)
    doc_store = DocumentStore(doc_path)

    monkeypatch.setattr(pp, "default_path", lambda: str(tmp_path / "plans.sqlite3"))
    monkeypatch.setattr(pp, "_store", None)

    adapter = FakeAdapter(slow_seconds=0.0)
    monkeypatch.setattr(pp, "_resolve_adapter", lambda adapter_id: adapter)

    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("HTTP Plan Proj", owner=OWNER, scaffold_memory=False)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    doc = storyboard.create(doc_store, OWNER, project["id"], [
        storyboard.new_scene("s1", "opening shot", start_ticks=0, duration_ticks=1000),
    ])

    from routes.creator_plan_routes import setup_creator_plan_routes
    app = FastAPI()
    app.include_router(setup_creator_plan_routes())
    client = TestClient(app)
    return client, project["id"], doc


def _node(node_id, *, depends_on=(), scenario="success"):
    return {
        "id": node_id, "adapter_id": "fake", "operation": "noop",
        "parameters": {"scenario": scenario}, "input_assets": [],
        "depends_on": list(depends_on),
    }


def test_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_plan_routes import setup_creator_plan_routes
    app = FastAPI()
    app.include_router(setup_creator_plan_routes())
    client = TestClient(app)
    resp = client.post("/api/creator/plans", json={"project_id": "whatever", "nodes": []})
    assert resp.status_code == 404


def test_create_get_execute_and_status_over_http(route_client):
    client, project_id, doc = route_client
    create = client.post("/api/creator/plans", json={
        "project_id": project_id,
        "brief": {"document_id": doc.id, "revision": doc.revision},
        "nodes": [_node("a")],
    })
    assert create.status_code == 200
    plan = create.json()
    assert plan["revision"] == 1

    get_resp = client.get(f"/api/creator/plans/{plan['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == plan["id"]

    execute = client.post(f"/api/creator/plans/{plan['id']}/execute",
                          json={"idempotency_key": "http-run-1"})
    assert execute.status_code == 200
    production = execute.json()
    assert production["plan_id"] == plan["id"]

    status_resp = client.get(f"/api/creator/productions/{production['production_id']}")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] in ("running", "paused", "completed")

    cancel_resp = client.post(f"/api/creator/productions/{production['production_id']}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"


def test_execute_missing_idempotency_key_is_400(route_client):
    client, project_id, doc = route_client
    create = client.post("/api/creator/plans", json={
        "project_id": project_id,
        "brief": {"document_id": doc.id, "revision": doc.revision},
        "nodes": [_node("a")],
    })
    plan = create.json()
    execute = client.post(f"/api/creator/plans/{plan['id']}/execute", json={})
    assert execute.status_code == 400


def test_unknown_plan_is_404(route_client):
    client, project_id, doc = route_client
    resp = client.get("/api/creator/plans/plan_nope")
    assert resp.status_code == 404
