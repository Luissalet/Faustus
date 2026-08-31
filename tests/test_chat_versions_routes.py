"""Truncate captures the tail; restore puts it back — through the real routes
and a real SessionManager, because the value of this feature is entirely in
whether the messages actually come back."""
import importlib
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import core.database as database
    importlib.reload(database)
    database.Base.metadata.create_all(bind=database.engine)
    import core.session_manager as sm_mod
    importlib.reload(sm_mod)
    sm = sm_mod.SessionManager()

    from src import chat_versions as cv
    monkeypatch.setattr(cv, "_dir", lambda: str(tmp_path / "versions"))

    import routes.history.history_routes as hr
    importlib.reload(hr)
    monkeypatch.setattr(hr, "_verify_session_owner", lambda request, sid, *a, **k: None)

    app = FastAPI()
    app.include_router(hr.setup_history_routes(sm))
    return TestClient(app), sm, cv


def _seed(sm, sid, n=4):
    from core.models import ChatMessage
    sm.create_session(session_id=sid, name="t", endpoint_url="x", model="m", rag=False, owner="u")
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        sm.add_message(sid, ChatMessage(role, f"m{i}"))


def test_truncate_saves_the_dropped_tail_and_restore_brings_it_back(env):
    client, sm, cv = env
    _seed(sm, "s1", 4)

    r = client.post("/api/session/s1/truncate", json={"keep_count": 2})
    assert r.status_code == 200, r.text
    assert r.json()["version"]["count"] == 2
    assert [m.content for m in sm.get_session("s1").history] == ["m0", "m1"]

    from core.models import ChatMessage
    sm.add_message("s1", ChatMessage("assistant", "a different answer"))

    vid = client.get("/api/session/s1/versions").json()["versions"][0]["id"]
    r = client.post(f"/api/session/s1/versions/{vid}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == 2 and r.json()["replaced"] == 1
    assert [m.content for m in sm.get_session("s1").history] == ["m0", "m1", "m2", "m3"]


def test_restoring_saves_what_it_replaces_so_you_can_switch_back(env):
    client, sm, cv = env
    _seed(sm, "s1", 4)
    client.post("/api/session/s1/truncate", json={"keep_count": 2})
    from core.models import ChatMessage
    sm.add_message("s1", ChatMessage("assistant", "second take"))

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
