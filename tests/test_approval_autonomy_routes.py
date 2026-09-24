"""tests/test_approval_autonomy_routes.py — the read-only stats and manual
promote/demote surface for `src.approval_autonomy` (feature 2's API
requirement). Same asymmetry as `test_approvals_routes.py`: reading is
`require_admin` (the agent's own loopback token opens it), changing a
family's promotion is `require_human` (the model must not promote its own
tool families).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.approval_autonomy_routes import setup_approval_autonomy_routes

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_approval_autonomy_routes())
    return TestClient(app)


@pytest.fixture()
def ce_db(tmp_path):
    from src.context_engine import store as ce_store
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        ce_store.use_path(None)


def test_mode_endpoint_reports_off_by_default(client, monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None:
                        default if key != "approval_autonomy" else "off")
    resp = client.get("/api/approval-autonomy/mode", headers=TOOL_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "off"
    assert "shadow" in body["modes"] and "active" in body["modes"]
    assert 0 < body["advise_threshold"] < body["act_threshold"] <= 1.0


def test_stats_endpoint_returns_family_rows(client, monkeypatch):
    from src import approval_autonomy as autonomy
    monkeypatch.setattr(autonomy, "family_stats", lambda owner="": [
        {"owner": "alice", "family": "read_file", "total": 25, "act_total": 22,
         "act_agree": 21, "destructive_disagree": 0, "agreement_rate": 0.9545,
         "override": None, "promoted": False},
    ])
    resp = client.get("/api/approval-autonomy/stats?owner=alice", headers=TOOL_HEADERS)
    assert resp.status_code == 200
    rows = resp.json()["families"]
    assert rows[0]["family"] == "read_file"
    assert rows[0]["act_total"] == 22


def test_the_model_cannot_promote_a_family_itself(client):
    """The one that matters, mirroring test_approvals_routes.py's grant test:
    the agent's own loopback token must not be able to flip a family to
    'promoted' through this API."""
    refused = client.post(
        "/api/approval-autonomy/family",
        json={"owner": "alice", "family": "read_file", "status": "promoted"},
        headers=TOOL_HEADERS,
    )
    assert refused.status_code == 403
    assert "by a person" in refused.json()["detail"]


def test_a_person_can_promote_and_demote_a_family(client, ce_db):
    promoted = client.post(
        "/api/approval-autonomy/family",
        json={"owner": "alice", "family": "read_file", "status": "promoted"},
    )
    assert promoted.status_code == 200
    assert promoted.json() == {
        "ok": True, "owner": "alice", "family": "read_file", "status": "promoted",
    }

    from src import approval_autonomy as autonomy
    assert autonomy.is_family_promoted("alice", "read_file") is True

    cleared = client.post(
        "/api/approval-autonomy/family",
        json={"owner": "alice", "family": "read_file", "status": ""},
    )
    assert cleared.status_code == 200
    assert autonomy.is_family_promoted("alice", "read_file") is False


def test_an_invalid_status_is_a_400_not_a_500(client):
    resp = client.post(
        "/api/approval-autonomy/family",
        json={"owner": "alice", "family": "read_file", "status": "sideways"},
    )
    assert resp.status_code == 400
