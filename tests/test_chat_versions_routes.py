"""Truncate captures the tail; restore puts it back — through the real routes.

The session manager is a stand-in over a plain list: core.database binds its
engine at import time, so a real one means reloading it, and a reloaded
core.database hands every later test in the run this test's database. The
storage layer is covered for real by the browser flow in
tests/e2e/test_composer_shortcuts.py.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
import routes.history.history_routes as hr


class _FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.history = []


class _FakeManager:
    """Only what the routes under test call."""

    def __init__(self):
        self.sessions = {}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def truncate_messages(self, sid, keep_count):
        s = self.sessions.get(sid)
        if s is None or keep_count < 0:
            return False
        s.history = s.history[:keep_count]
        return True

    def replace_messages(self, sid, messages):
        s = self.sessions.get(sid)
        if s is None:
            return False
        s.history = list(messages)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    from src import chat_versions as cv
    monkeypatch.setattr(cv, "_dir", lambda: str(tmp_path / "versions"))
    monkeypatch.setattr(hr, "_verify_session_owner", lambda request, sid, *a, **k: None)
    sm = _FakeManager()
    app = FastAPI()
    app.include_router(hr.setup_history_routes(sm))
    return TestClient(app), sm, cv


def _seed(sm, sid, n=4):
    sm.sessions[sid] = _FakeSession(sid)
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        sm.sessions[sid].history.append(ChatMessage(role, f"m{i}"))


def test_truncate_saves_the_dropped_tail_and_restore_brings_it_back(env):
    client, sm, cv = env
    _seed(sm, "s1", 4)

    r = client.post("/api/session/s1/truncate", json={"keep_count": 2})
    assert r.status_code == 200, r.text
    assert r.json()["version"]["count"] == 2
    assert [m.content for m in sm.get_session("s1").history] == ["m0", "m1"]

    sm.sessions["s1"].history.append(ChatMessage("assistant", "a different answer"))

    vid = client.get("/api/session/s1/versions").json()["versions"][0]["id"]
    r = client.post(f"/api/session/s1/versions/{vid}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == 2 and r.json()["replaced"] == 1
    assert [m.content for m in sm.get_session("s1").history] == ["m0", "m1", "m2", "m3"]


def test_restoring_saves_what_it_replaces_so_you_can_switch_back(env):
    client, sm, cv = env
    _seed(sm, "s1", 4)
    client.post("/api/session/s1/truncate", json={"keep_count": 2})
    sm.sessions["s1"].history.append(ChatMessage("assistant", "second take"))

    first = client.get("/api/session/s1/versions").json()["versions"][0]["id"]
    client.post(f"/api/session/s1/versions/{first}/restore")

    rows = client.get("/api/session/s1/versions").json()["versions"]
    assert len(rows) == 1 and rows[0]["reason"] == "replaced"
    client.post(f"/api/session/s1/versions/{rows[0]['id']}/restore")
    assert [m.content for m in sm.get_session("s1").history] == ["m0", "m1", "second take"]


def test_truncating_nothing_records_no_version(env):
    client, sm, cv = env
    _seed(sm, "s1", 2)
    r = client.post("/api/session/s1/truncate", json={"keep_count": 5})
    assert r.status_code == 200 and r.json()["version"] is None
    assert client.get("/api/session/s1/versions").json()["versions"] == []


def test_restoring_an_unknown_version_is_a_404(env):
    client, sm, cv = env
    _seed(sm, "s1", 2)
    assert client.post("/api/session/s1/versions/deadbeef/restore").status_code == 404


def test_clearing_versions(env):
    client, sm, cv = env
    _seed(sm, "s1", 4)
    client.post("/api/session/s1/truncate", json={"keep_count": 1})
    assert client.request("DELETE", "/api/session/s1/versions").json()["removed"] == 1
    assert client.get("/api/session/s1/versions").json()["versions"] == []


def test_a_capture_failure_never_blocks_the_truncation(env, monkeypatch):
    client, sm, cv = env
    _seed(sm, "s1", 4)
    monkeypatch.setattr(cv, "save", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    r = client.post("/api/session/s1/truncate", json={"keep_count": 2})
    assert r.status_code == 200 and r.json()["version"] is None
    assert len(sm.get_session("s1").history) == 2


def test_the_composer_routes_every_truncation_through_the_version_capture():
    """All three destructive flows (edit, regenerate, regenerate-variant) must
    go through the helper — a raw /truncate call would silently lose the tail."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("static", "js", "chat.js").read_text(
        encoding="utf-8", errors="replace")
    assert src.count("await _truncateWithVersion(sessionId, keepCount") == 3
    assert "async function _truncateWithVersion(" in src
    assert src.count("/api/session/${sessionId}/truncate") == 1   # only inside the helper
    # The Undo action has to hit the restore route, not just show a message.
    assert "/versions/${encodeURIComponent(saved.id)}/restore" in src
