"""/api/agent/progress/{session_id} must check session ownership.

Every sibling per-session route (routes/history/history_routes.py,
routes/chat_routes.py) runs `_verify_session_owner`; these two only called
`require_user`, so any authenticated caller could read or delete any chat's
to-do list by naming its id. Impact is nil in the single-user default, but the
asymmetry is real and it is the kind that survives until auth is switched on.
"""

import json
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.agent_progress_routes as apr


@pytest.fixture
def todo_dir(tmp_path, monkeypatch):
    d = tmp_path / "agent_todos"
    d.mkdir()
    monkeypatch.setattr(apr, "_TODO_DIR", str(d))
    return d


def _client(monkeypatch, *, owner_check, user=""):
    """A client whose ownership helper and current user are under test control."""
    monkeypatch.setattr(apr, "_verify_session_owner", owner_check)
    monkeypatch.setattr(apr, "require_user", lambda request: user)
    import src.auth_helpers as auth_helpers
    monkeypatch.setattr(auth_helpers, "effective_user", lambda request: user or None)
    app = FastAPI()
    app.include_router(apr.setup_agent_progress_routes())
    return TestClient(app)


def _write(todo_dir, session_id, todos):
    (todo_dir / f"{session_id}.json").write_text(
        json.dumps({"todos": todos}), encoding="utf-8")


# ── the helper is the shared one, not a private copy ───────────────────────

def test_the_route_uses_the_same_helper_as_its_siblings():
    from routes.session_routes import _verify_session_owner
    assert apr._verify_session_owner is _verify_session_owner


# ── GET ────────────────────────────────────────────────────────────────────

def test_get_returns_the_todos_of_a_session_you_own(monkeypatch, todo_dir):
    c = _client(monkeypatch, owner_check=lambda request, sid: None)
    _write(todo_dir, "mine", [{"content": "step", "status": "pending"}])
    r = c.get("/api/agent/progress/mine")
    assert r.status_code == 200
    assert r.json()["todos"] == [{"content": "step", "status": "pending"}]


def test_get_is_refused_for_someone_elses_session(monkeypatch, todo_dir):
    def deny(request, sid):
        raise HTTPException(404, f"Session {sid} not found")

    c = _client(monkeypatch, owner_check=deny, user="alice")
    _write(todo_dir, "bobs", [{"content": "secret plan", "status": "pending"}])
    r = c.get("/api/agent/progress/bobs")
    assert r.status_code == 404
    assert "secret plan" not in r.text


def test_get_runs_the_check_before_touching_the_file(monkeypatch, todo_dir):
    """The 404 must not depend on the file existing, or its presence leaks."""
    order = []

    def deny(request, sid):
        order.append("checked")
        raise HTTPException(404, "nope")

    c = _client(monkeypatch, owner_check=deny, user="alice")
    assert c.get("/api/agent/progress/whatever").status_code == 404
    assert order == ["checked"]


# ── DELETE ─────────────────────────────────────────────────────────────────

def test_delete_removes_the_file_for_a_session_you_own(monkeypatch, todo_dir):
    c = _client(monkeypatch, owner_check=lambda request, sid: None)
    _write(todo_dir, "mine", [{"content": "x", "status": "done"}])
    r = c.delete("/api/agent/progress/mine")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert not (todo_dir / "mine.json").exists()


def test_delete_still_cleans_up_orphan_progress_in_single_user_mode(monkeypatch, todo_dir):
    """The session is gone from the DB and from memory, so the helper 404s —
    but the leftover to-do file is exactly what this endpoint is for, and with
    no user there is nobody the 404 could be protecting. Refusing would strand
    the file forever: a deleted session never passes the check again."""
    def deny(request, sid):
        raise HTTPException(404, f"Session {sid} not found")

    c = _client(monkeypatch, owner_check=deny, user="")     # auth disabled
    _write(todo_dir, "ghost", [{"content": "stale", "status": "pending"}])
    r = c.delete("/api/agent/progress/ghost")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert not (todo_dir / "ghost.json").exists()


def test_delete_is_refused_for_someone_elses_session_when_auth_is_on(monkeypatch, todo_dir):
    """With a real authenticated user the 404 is ambiguous — it could be "not
    yours" — so it stands, and the other user's file is untouched."""
    def deny(request, sid):
        raise HTTPException(404, f"Session {sid} not found")

    c = _client(monkeypatch, owner_check=deny, user="alice")
    _write(todo_dir, "bobs", [{"content": "x", "status": "pending"}])
    r = c.delete("/api/agent/progress/bobs")
    assert r.status_code == 404
    assert (todo_dir / "bobs.json").exists(), "another user's progress was deleted"


def test_delete_never_swallows_a_401(monkeypatch, todo_dir):
    """Only a 404 gets the orphan treatment: an authentication failure must
    propagate untouched."""
    def unauthenticated(request, sid):
        raise HTTPException(401, "Authentication required")

    c = _client(monkeypatch, owner_check=unauthenticated, user="")
    _write(todo_dir, "s", [{"content": "x", "status": "pending"}])
    r = c.delete("/api/agent/progress/s")
    assert r.status_code == 401
    assert (todo_dir / "s.json").exists()


def test_delete_of_a_missing_file_is_still_ok(monkeypatch, todo_dir):
    c = _client(monkeypatch, owner_check=lambda request, sid: None)
    r = c.delete("/api/agent/progress/never-existed")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_the_session_id_is_still_sanitised_into_the_filename(monkeypatch, todo_dir):
    """Ownership is the new gate, but the path-traversal guard on the id has to
    stay: the two protect different things."""
    assert apr._safe_session_id("../../etc/passwd") == ".._.._etc_passwd"
    assert os.sep not in apr._safe_session_id("a/b\\c")
    # `..` survives as text but never as a separator, so the join stays inside
    # the to-do directory.
    joined = os.path.join("/data/agent_todos", apr._safe_session_id("../../etc/passwd") + ".json")
    assert os.path.dirname(joined) == "/data/agent_todos"
