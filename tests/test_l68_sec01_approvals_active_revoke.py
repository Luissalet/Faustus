"""SEC-01 · active concessions and immediate revocation.

`src/approval_store.py` could open a card and grant/deny/consume it, but
nothing listed *what is currently granted* (as opposed to `pending()`, which
is only cards nobody decided on) and nothing could end a card's authority
before its TTL/uses ran out. A model or a stale document claiming "I have
permission" must never outrun a human's decision to take that permission
back — that only holds if there is somewhere to see the standing grants and
a way to kill one immediately.

`GET /api/approvals/active` and `DELETE /api/approvals/{id}` close that:
the first lists granted-and-still-usable cards, the second revokes a card in
ANY non-terminal status (pending or granted) right now, gated `require_human`
like grant/deny — the model's own loopback token must not be able to erase
the trail of what it was allowed to do.
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
from src import approval_store

PLAN = {
    "action": "publish", "skill_id": "media.publish", "skill_version": "2.0.0",
    "backend": "media_worker", "recipients": ["youtube:channel-1"],
    "cost_units": 12, "secret_names": ["youtube"], "output_kinds": ["video"],
    "detail": "Publish the September clip.",
}

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


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


def _open_and_grant(client, **plan_overrides):
    plan = dict(PLAN, **plan_overrides)
    opened = client.post("/api/approvals/request", json={"plan": plan, "owner": "luis"},
                         headers=TOOL_HEADERS)
    assert opened.status_code == 200, opened.text
    card_id = opened.json()["approval"]["id"]
    granted = client.post(f"/api/approvals/{card_id}/grant", json={"by": "luis"})
    assert granted.status_code == 200, granted.text
    return card_id


# ---------------------------------------------------------------------------
# GET /api/approvals/active
# ---------------------------------------------------------------------------

def test_active_lists_a_granted_card(client):
    card_id = _open_and_grant(client)

    r = client.get("/api/approvals/active")
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["active"]}
    assert card_id in ids
    assert r.json()["count"] == len(r.json()["active"])


def test_active_excludes_a_card_that_is_only_pending(client):
    client.post("/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
               headers=TOOL_HEADERS)

    r = client.get("/api/approvals/active")
    assert r.json()["active"] == []


def test_active_excludes_an_already_denied_card(client):
    opened = client.post("/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
                         headers=TOOL_HEADERS)
    card_id = opened.json()["approval"]["id"]
    client.post(f"/api/approvals/{card_id}/deny", json={"by": "luis"})

    r = client.get("/api/approvals/active")
    assert r.json()["active"] == []


def test_active_excludes_a_card_that_expired_before_anyone_used_it(client):
    plan = dict(PLAN, detail="Publish the expiring clip.")
    opened = client.post("/api/approvals/request",
                         json={"plan": plan, "owner": "luis", "ttl_seconds": 1},
                         headers=TOOL_HEADERS)
    card_id = opened.json()["approval"]["id"]
    client.post(f"/api/approvals/{card_id}/grant", json={"by": "luis"})

    # Fast-forward the store's own clock rather than sleeping in a test.
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    original_now_iso = approval_store.now_iso
    try:
        approval_store.now_iso = lambda: future.isoformat().replace("+00:00", "Z")
        assert approval_store.active() == []
    finally:
        approval_store.now_iso = original_now_iso


def test_active_is_owner_scoped(client):
    opened = client.post("/api/approvals/request", json={"plan": PLAN, "owner": "alice"},
                         headers=TOOL_HEADERS)
    card_id = opened.json()["approval"]["id"]
    client.post(f"/api/approvals/{card_id}/grant", json={"by": "alice"})

    assert client.get("/api/approvals/active", params={"owner": "bob"}).json()["active"] == []
    mine = client.get("/api/approvals/active", params={"owner": "alice"}).json()["active"]
    assert {c["id"] for c in mine} == {card_id}


# ---------------------------------------------------------------------------
# DELETE /api/approvals/{id}
# ---------------------------------------------------------------------------

def test_revoking_a_granted_card_removes_it_from_active_and_from_covering_the_plan(client):
    card_id = _open_and_grant(client)
    assert card_id in {c["id"] for c in client.get("/api/approvals/active").json()["active"]}

    r = client.request("DELETE", f"/api/approvals/{card_id}", json={"by": "luis", "reason": "changed my mind"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["approval"]["status"] == "revoked"

    assert client.get("/api/approvals/active").json()["active"] == []
    # A plan that USED to be covered by this card no longer is.
    check = client.post("/api/approvals/check", json={"plan": PLAN, "owner": "luis"},
                        headers=TOOL_HEADERS)
    assert check.json()["ok"] is False


def test_revoking_a_pending_card_takes_it_away_before_anyone_decides(client):
    """The literal SEC-01 point: revocation is not limited to cards already
    granted — an open request is a standing possibility of access too."""
    opened = client.post("/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
                         headers=TOOL_HEADERS)
    card_id = opened.json()["approval"]["id"]

    r = client.request("DELETE", f"/api/approvals/{card_id}", json={"by": "luis"})
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "revoked"

    # Nobody can grant it after the fact.
    granted = client.post(f"/api/approvals/{card_id}/grant", json={"by": "luis"})
    assert granted.json()["ok"] is False
    assert granted.json()["reason"] == "already_revoked"


def test_the_model_cannot_revoke_its_own_card(client):
    """Same asymmetry as grant/deny: the loopback token opens require_admin,
    not require_human — a model must not be able to erase what it was
    allowed to do by revoking the record of it."""
    card_id = _open_and_grant(client)

    r = client.request("DELETE", f"/api/approvals/{card_id}", json={"by": "the model"}, headers=TOOL_HEADERS)
    assert r.status_code == 403


def test_revoking_an_unknown_id_is_404(client):
    r = client.request("DELETE", "/api/approvals/apr_does_not_exist", json={"by": "luis"})
    assert r.status_code == 404


def test_revoking_an_already_denied_card_is_a_clear_no_op(client):
    opened = client.post("/api/approvals/request", json={"plan": PLAN, "owner": "luis"},
                         headers=TOOL_HEADERS)
    card_id = opened.json()["approval"]["id"]
    client.post(f"/api/approvals/{card_id}/deny", json={"by": "luis"})

    r = client.request("DELETE", f"/api/approvals/{card_id}", json={"by": "luis"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "already_denied"


# ---------------------------------------------------------------------------
# src/approval_store.py — pure function unit coverage
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "approvals_unit.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


def test_revoke_requires_a_named_decider(store):
    from src.contracts import ApprovalPlan
    card = approval_store.request(ApprovalPlan.parse(PLAN), owner="luis")
    result = approval_store.revoke(card.id, by="")
    assert result == {"ok": False, "reason": "no_decider",
                      "detail": "a revocation has to record who revoked it"}


def test_revoke_on_an_unknown_id_is_not_found(store):
    result = approval_store.revoke("apr_missing", by="luis")
    assert result == {"ok": False, "reason": "not_found", "detail": "apr_missing"}
