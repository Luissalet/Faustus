"""routes/mobile_routes.py — the native Android app's server surface.

Covers: `_mobile_owner`'s two auth paths (a real admin cookie vs a bearer
token — the whole point of that helper is that require_admin alone does NOT
accept the second one, see the module docstring), the bootstrap/notifications/
sessions/messages shapes, and the WebSocket handshake (hello frame + the
4401 auth-failure close, since AuthMiddleware never runs on a WS scope).
"""
from __future__ import annotations

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import ApiToken, Base
from core.database import ChatMessage as DbChatMessage
from core.database import Session as DbSession
from routes.mobile_routes import setup_mobile_routes
from src import notifications as N


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "mobile.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    Base.metadata.create_all(bind=engine)

    # Cookie-path requests: bypass the real admin check the same way
    # tests/test_approvals_routes.py does, so this file tests _mobile_owner's
    # OWN branching (token vs cookie) rather than re-testing AuthManager.
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    # Isolate the notification ring from any other test/process state.
    monkeypatch.setattr(N, "NOTIFICATIONS_FILE", str(tmp_path / "notifications.jsonl"))
    N._ring.clear()
    N._subscribers.clear()
    N._next_id = 1

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        # Mirrors exactly what app.py's AuthMiddleware stamps for each of
        # the two credential kinds this router has to distinguish.
        bearer_owner = request.headers.get("X-Test-Bearer-Owner")
        if bearer_owner:
            request.state.api_token = True
            request.state.api_token_owner = bearer_owner
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("X-Test-User", "luis")
        return await call_next(request)

    app.include_router(setup_mobile_routes())
    client = TestClient(app)
    yield client, db_mod.SessionLocal
    engine.dispose()


def _mint_token(SessionLocal, owner: str, raw_token: str = "ody_" + "a" * 40) -> str:
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()
    db = SessionLocal()
    try:
        db.add(ApiToken(
            id="tok1", owner=owner, name="test", token_hash=token_hash,
            token_prefix=raw_token[:8], scopes="chat", is_active=True,
        ))
        db.commit()
    finally:
        db.close()
    return raw_token


# ── bootstrap ────────────────────────────────────────────────────────────

def test_bootstrap_cookie_path(env):
    client, _ = env
    resp = client.get("/api/mobile/bootstrap", headers={"X-Test-User": "luis"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["me"] == "luis"
    assert body["server_name"] == "Faustus"
    assert set(body["capabilities"]) == {
        "chat", "calendar", "notes", "approvals", "whatsapp", "cards",
    }
    assert set(body["counts"]) == {"sessions", "pending_approvals", "active_turns"}


def test_bootstrap_bearer_path_uses_token_owner_not_current_user(env):
    """The whole point of `_mobile_owner`: a bearer caller's `current_user`
    is never set to a real username by AuthMiddleware (it's always the
    sandboxed "api" pseudo-user) — this router must read `api_token_owner`
    instead, never fall through to a cookie-shaped check."""
    client, _ = env
    resp = client.get("/api/mobile/bootstrap", headers={"X-Test-Bearer-Owner": "luis"})
    assert resp.status_code == 200
    assert resp.json()["me"] == "luis"


def test_bearer_with_no_owner_stamped_is_refused(env):
    """A malformed/edge-case token stamp (api_token=True but no owner) must
    not silently fall through to admin-cookie logic."""
    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = None
        return await call_next(request)

    app.include_router(setup_mobile_routes())
    resp = TestClient(app).get("/api/mobile/bootstrap")
    assert resp.status_code == 403


# ── notifications ────────────────────────────────────────────────────────

def test_notifications_since_id_and_owner_scoping(env):
    client, _ = env
    N.emit("turn_finished", owner="luis", title="mine", body="hello")
    N.emit("turn_finished", owner="someone_else", title="not mine", body="hi")

    resp = client.get("/api/mobile/notifications", headers={"X-Test-User": "luis"})
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "mine"
    assert body["last_id"] == body["items"][0]["id"]

    resp2 = client.get(
        f"/api/mobile/notifications?since_id={body['last_id']}",
        headers={"X-Test-User": "luis"},
    )
    assert resp2.json()["items"] == []


# ── sessions ─────────────────────────────────────────────────────────────

def test_sessions_compact_shape(env):
    client, SessionLocal = env
    db = SessionLocal()
    try:
        db.add(DbSession(
            id="s1", name="Weather chat", endpoint_url="http://x", model="qwen3",
            owner="luis", mode="agent",
        ))
        db.add(DbChatMessage(id="m1", session_id="s1", role="user", content="what's the weather"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/mobile/sessions", headers={"X-Test-User": "luis"})
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"id", "name", "updated_at", "model", "mode", "running", "last_preview"}
    assert row["id"] == "s1"
    assert row["name"] == "Weather chat"
    assert row["mode"] == "agent"
    assert row["running"] is False
    assert "weather" in row["last_preview"]


def test_sessions_only_shows_the_callers_own(env):
    client, SessionLocal = env
    db = SessionLocal()
    try:
        db.add(DbSession(id="mine", name="Mine", endpoint_url="x", model="m", owner="luis"))
        db.add(DbSession(id="theirs", name="Theirs", endpoint_url="x", model="m", owner="someone_else"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/mobile/sessions", headers={"X-Test-User": "luis"})
    ids = {row["id"] for row in resp.json()["sessions"]}
    assert ids == {"mine"}


# ── messages ─────────────────────────────────────────────────────────────

def test_messages_returned_oldest_first_newest_last(env):
    client, SessionLocal = env
    db = SessionLocal()
    try:
        db.add(DbSession(id="s1", name="Chat", endpoint_url="x", model="m", owner="luis"))
        db.add(DbChatMessage(id="m1", session_id="s1", role="user", content="first"))
        db.add(DbChatMessage(id="m2", session_id="s1", role="assistant", content="second"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/mobile/session/s1/messages", headers={"X-Test-User": "luis"})
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["content"] for m in msgs] == ["first", "second"]
    assert all("created_at" in m and "role" in m and "id" in m for m in msgs)


def test_messages_for_another_owners_session_is_404(env):
    client, SessionLocal = env
    db = SessionLocal()
    try:
        db.add(DbSession(id="s1", name="Chat", endpoint_url="x", model="m", owner="someone_else"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/mobile/session/s1/messages", headers={"X-Test-User": "luis"})
    assert resp.status_code == 404


def test_messages_carries_tool_calls_summary_when_present(env):
    client, SessionLocal = env
    import json as _json
    db = SessionLocal()
    try:
        db.add(DbSession(id="s1", name="Chat", endpoint_url="x", model="m", owner="luis"))
        db.add(DbChatMessage(
            id="m1", session_id="s1", role="assistant", content="done",
            meta_data=_json.dumps({"tool_events": [{"tool": "web_search"}, {"tool": "read_file"}]}),
        ))
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/mobile/session/s1/messages", headers={"X-Test-User": "luis"})
    msg = resp.json()["messages"][0]
    assert msg["tool_calls_summary"] == "used: web_search, read_file"


# ── WebSocket ────────────────────────────────────────────────────────────

def test_ws_auth_failure_closes_4401(env):
    client, _ = env
    with pytest.raises(Exception):
        # starlette's TestClient websocket_connect raises when the server
        # closes before accept(); the important assertion is the code.
        with client.websocket_connect("/api/mobile/ws?token=not_a_real_token") as ws:
            ws.receive_json()


def test_ws_hello_on_valid_token(env):
    client, SessionLocal = env
    raw = _mint_token(SessionLocal, owner="luis")
    N.emit("turn_finished", owner="luis", title="before connect", body="")

    with client.websocket_connect(f"/api/mobile/ws?token={raw}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["last_id"] >= 1


def test_ws_pushes_a_live_event_to_the_right_owner(env):
    client, SessionLocal = env
    raw = _mint_token(SessionLocal, owner="luis")

    with client.websocket_connect(f"/api/mobile/ws?token={raw}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        N.emit("approval_pending", owner="luis", title="Approve X", body="wants to do X")
        frame = ws.receive_json()
        assert frame["type"] == "event"
        assert frame["event"]["kind"] == "approval_pending"
        assert frame["event"]["title"] == "Approve X"


def test_ws_authenticates_via_header_too(env):
    client, SessionLocal = env
    raw = _mint_token(SessionLocal, owner="luis")
    with client.websocket_connect(
        "/api/mobile/ws", headers={"Authorization": f"Bearer {raw}"},
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
