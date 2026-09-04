"""The approval endpoints — and the asymmetry that is the whole design.

Reading and requesting are `require_admin`, which the agent's loopback token
opens on purpose: asking for permission is not giving it. Granting and denying
are `require_human`, which that token does not open. The test that matters is
the one where the model tries to grant its own card and gets a 403.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from routes.approvals_routes import setup_approvals_routes

PLAN = {
    "action": "publish", "skill_id": "media.publish", "skill_version": "2.0.0",
    "backend": "media_worker", "recipients": ["youtube:channel-1"],
    "cost_units": 12, "secret_names": ["youtube"], "output_kinds": ["video"],
    "detail": "Publish the September clip.",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "approvals.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_approvals_routes())
    yield TestClient(app)
    engine.dispose()


TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


def test_the_model_can_open_a_card_and_cannot_grant_it(client):
    """The one that matters. The agent reaches admin routes through loopback
    by design, so if grant sat behind the same gate as request, the model
    would approve its own plan."""
    opened = client.post("/api/approvals/request",
                         json={"plan": PLAN, "owner": "luis"},
                         headers=TOOL_HEADERS)
    assert opened.status_code == 200
    card_id = opened.json()["approval"]["id"]

    refused = client.post(f"/api/approvals/{card_id}/grant",
                          json={"by": "the model"}, headers=TOOL_HEADERS)
    assert refused.status_code == 403
    assert "by a person" in refused.json()["detail"]

    denied_too = client.post(f"/api/approvals/{card_id}/deny", headers=TOOL_HEADERS)
    assert denied_too.status_code == 403

    # And the card is untouched — a refused call decides nothing.
    listed = client.get("/api/approvals/pending", headers=TOOL_HEADERS).json()
    assert [c["id"] for c in listed["pending"]] == [card_id]
    assert listed["pending"][0]["status"] == "pending"


def test_a_person_grants_it_and_then_it_covers_the_plan(client):
    card_id = client.post("/api/approvals/request",
                          json={"plan": PLAN, "owner": "luis"}).json()["approval"]["id"]
    assert client.post("/api/approvals/check",
                       json={"plan": PLAN, "owner": "luis"}).json()["ok"] is False

    granted = client.post(f"/api/approvals/{card_id}/grant", json={"by": "luis"})
    assert granted.status_code == 200 and granted.json()["ok"] is True

    covered = client.post("/api/approvals/check", json={"plan": PLAN, "owner": "luis"}).json()
    assert covered["ok"] is True and covered["approval_id"] == card_id


def test_the_check_says_which_field_moved(client):
    card_id = client.post("/api/approvals/request",
                          json={"plan": PLAN, "owner": "luis"}).json()["approval"]["id"]
    client.post(f"/api/approvals/{card_id}/grant", json={"by": "luis"})

    drifted = {**PLAN, "cost_units": 400}
    out = client.post("/api/approvals/check", json={"plan": drifted, "owner": "luis"}).json()
    assert out["ok"] is False and out["reason"] == "plan_changed"
    assert out["changes"] == [{"field": "cost_units", "approved": 12, "now": 400}]


def test_a_grant_with_no_body_is_still_a_grant(client):
    card_id = client.post("/api/approvals/request",
                          json={"plan": PLAN}).json()["approval"]["id"]
    out = client.post(f"/api/approvals/{card_id}/grant")
    assert out.status_code == 200 and out.json()["ok"] is True
    assert out.json()["approval"]["decided_by"]      # someone is always recorded


def test_a_malformed_plan_is_a_400_that_names_the_field(client):
    bad = client.post("/api/approvals/request",
                      json={"plan": {**PLAN, "skill_version": "2"}})
    assert bad.status_code == 400
    assert "skill_version" in bad.json()["detail"]
    assert "semantic version" in bad.json()["detail"]

    assert client.post("/api/approvals/request", content=b"nope").status_code == 400
    assert client.post("/api/approvals/check", json=[1, 2]).status_code == 400


def test_listing_expires_the_stale_ones_on_the_way_past(client):
    fresh = client.post("/api/approvals/request",
                        json={"plan": PLAN, "ttl_seconds": None}).json()["approval"]["id"]
    stale = client.post("/api/approvals/request",
                        json={"plan": {**PLAN, "detail": "older"},
                              "ttl_seconds": 1}).json()["approval"]["id"]

    from src import approval_store
    assert approval_store.expire_stale(now="2099-01-01T00:00:00Z") == 1

    listed = client.get("/api/approvals/pending").json()
    assert [c["id"] for c in listed["pending"]] == [fresh]
    assert stale not in [c["id"] for c in listed["pending"]]
