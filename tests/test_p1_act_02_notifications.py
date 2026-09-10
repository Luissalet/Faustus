"""ACT-02: notification dedupe, per-type/channel prefs, quiet hours, and the
"no sensitive data on a lock-screen preview" scrub. The acceptance line —
a reconnect must not re-notify the same pending permission twenty times —
is `test_reconnect_storm_on_the_same_pending_item_stores_one_notification`.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core import middleware
from routes.notifications_routes import setup_notification_routes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.notifications_routes.NOTIFICATIONS_FILE", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User", "alice")
        return await call_next(request)

    app.include_router(setup_notification_routes())
    return TestClient(app)


APPROVAL_EMIT = {
    "type": "approval", "dedupe_key": "approval:card-1",
    "title": "Approve publish to youtube", "detail": "Wants to publish a video",
    "screen": "activity", "target": "approval-card-1",
}


def test_reconnect_storm_on_the_same_pending_item_stores_one_notification(client):
    for _ in range(20):
        r = client.post("/api/notifications/emit", json=APPROVAL_EMIT)
        assert r.status_code == 200
    rows = client.get("/api/notifications").json()["notifications"]
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "approval:card-1"


def test_marking_read_survives_a_repeated_emit(client):
    client.post("/api/notifications/emit", json=APPROVAL_EMIT)
    nid = client.get("/api/notifications").json()["notifications"][0]["id"]
    client.post(f"/api/notifications/{nid}/read")
    # A reconnect re-observing the same pending approval must not resurrect it.
    client.post("/api/notifications/emit", json=APPROVAL_EMIT)
    row = client.get("/api/notifications").json()["notifications"][0]
    assert row["read"] is True


def test_disabled_type_is_never_stored(client):
    client.put("/api/notifications/prefs", json={"types": {"approval": {"enabled": False}}})
    r = client.post("/api/notifications/emit", json=APPROVAL_EMIT)
    assert r.json()["stored"] is False
    assert client.get("/api/notifications").json()["notifications"] == []


def test_quiet_hours_suppress_desktop_but_keep_the_in_app_row(client, monkeypatch):
    client.put("/api/notifications/prefs", json={"quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"}})
    monkeypatch.setattr(time, "localtime", lambda *a: time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, 0)))
    r = client.post("/api/notifications/emit", json=APPROVAL_EMIT)
    body = r.json()["notification"]
    assert body["suppressed_quiet_hours"] is True
    assert body["desktop_allowed"] is False
    rows = client.get("/api/notifications").json()["notifications"]
    assert len(rows) == 1  # still in the in-app tray


def test_token_like_text_is_scrubbed_before_storage(client):
    payload = dict(APPROVAL_EMIT, dedupe_key="approval:card-2",
                   detail="Uses secret " + "sk_live_" + "A" * 28 + " to publish")
    client.post("/api/notifications/emit", json=payload)
    row = client.get("/api/notifications").json()["notifications"][0]
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in row["detail"]
    assert "[redacted]" in row["detail"]


def test_two_different_owners_do_not_share_notifications(client):
    client.post("/api/notifications/emit", json=APPROVAL_EMIT, headers={"X-Test-User": "alice"})
    bob_rows = client.get("/api/notifications", headers={"X-Test-User": "bob"}).json()["notifications"]
    assert bob_rows == []
