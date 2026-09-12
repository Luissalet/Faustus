"""Tests for `routes/behavior_mode_routes.py` — CONTRATO_MODOS Lote A.

Real `SessionManager` over a temp file-backed SQLite DB, same harness
`tests/test_side_threads.py` uses (a `SimpleNamespace` double can't stand in
for real owner-scoped session lookups). No LLM anywhere in this suite.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path, monkeypatch):
    """`POST /api/behavior-modes/default` writes through `src.settings.
    update_settings` — point it at a throwaway file so this suite never
    touches the real `data/settings.json`."""
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    yield
    settings_mod._invalidate_caches()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as db_mod
    import core.session_manager as sm_mod
    import routes.session_routes as sr_mod
    import routes.behavior_mode_routes as bmr_mod

    url = "sqlite:///" + (tmp_path / "behavior_modes.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    monkeypatch.setattr(sr_mod, "SessionLocal", Session)
    monkeypatch.setattr(bmr_mod, "get_session_behavior_mode", db_mod.get_session_behavior_mode)
    monkeypatch.setattr(bmr_mod, "set_session_behavior_mode", db_mod.set_session_behavior_mode)
    yield Session
    engine.dispose()


@pytest.fixture()
def sm(db):
    from core.session_manager import SessionManager
    return SessionManager()


def _fake_effective_user(request):
    return request.headers.get("x-test-user", "alice")


@pytest.fixture()
def user_modes_file(tmp_path, monkeypatch):
    from src import behavior_modes
    path = tmp_path / "behavior_modes.json"
    monkeypatch.setattr(behavior_modes, "_user_modes_path", lambda: str(path))
    return path


@pytest.fixture()
def client(db, sm, user_modes_file, monkeypatch):
    import routes.session_routes as sr_mod
    import routes.behavior_mode_routes as bmr_mod

    monkeypatch.setattr(sr_mod, "effective_user", _fake_effective_user)
    monkeypatch.setattr(bmr_mod, "effective_user", _fake_effective_user)

    app = FastAPI()
    app.include_router(bmr_mod.setup_behavior_mode_routes(sm))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _hdr(user):
    return {"x-test-user": user} if user else {}


def _mk_session(sm, sid, *, owner="alice", name="Main", endpoint="http://ep", model="m1"):
    sm.create_session(session_id=sid, name=name, endpoint_url=endpoint, model=model, owner=owner)
    return sm.get_session(sid)


# ---------------------------------------------------------------------------
# GET /api/behavior-modes
# ---------------------------------------------------------------------------

def test_list_includes_builtins_default_first_and_global_default(client):
    r = client.get("/api/behavior-modes", headers=_hdr("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "default"
    ids = [m["id"] for m in body["modes"]]
    assert ids[0] == "default"
    assert "adversarial" in ids


def test_list_includes_only_callers_own_user_modes(client):
    client.post(
        "/api/behavior-modes",
        json={"id": "mine", "name": {"en": "Mine", "es": "Mío"},
              "description": {"en": "d", "es": "d"}, "prompt": "p"},
        headers=_hdr("alice"),
    )
    alice_ids = [m["id"] for m in client.get("/api/behavior-modes", headers=_hdr("alice")).json()["modes"]]
    bob_ids = [m["id"] for m in client.get("/api/behavior-modes", headers=_hdr("bob")).json()["modes"]]
    assert "mine" in alice_ids
    assert "mine" not in bob_ids


# ---------------------------------------------------------------------------
# POST/DELETE /api/behavior-modes
# ---------------------------------------------------------------------------

def test_save_mode_round_trips(client):
    r = client.post(
        "/api/behavior-modes",
        json={
            "id": "my_style", "name": {"en": "My Style", "es": "Mi Estilo"},
            "description": {"en": "d", "es": "d"}, "prompt": "Be brief.",
            "checks": {"max_words": 40},
        },
        headers=_hdr("alice"),
    )
    assert r.status_code == 200, r.text
    body = r.json()["mode"]
    assert body["id"] == "my_style"
    assert body["builtin"] is False
    assert body["owner"] == "alice"


def test_save_mode_rejects_builtin_id_with_409(client):
    r = client.post(
        "/api/behavior-modes",
        json={"id": "terse", "name": {"en": "X", "es": "X"},
              "description": {"en": "d", "es": "d"}, "prompt": "p"},
        headers=_hdr("alice"),
    )
    assert r.status_code == 409
    assert r.json() == {"error": r.json()["error"], "error_class": "modes.builtin"}


def test_save_mode_rejects_bad_slug_with_400(client):
    r = client.post(
        "/api/behavior-modes",
        json={"id": "Bad-Slug", "name": {"en": "X", "es": "X"},
              "description": {"en": "d", "es": "d"}, "prompt": "p"},
        headers=_hdr("alice"),
    )
    assert r.status_code == 400
    assert r.json()["error_class"] == "modes.invalid"


def test_delete_own_mode_then_gone(client):
    client.post(
        "/api/behavior-modes",
        json={"id": "temp_mode", "name": {"en": "X", "es": "X"},
              "description": {"en": "d", "es": "d"}, "prompt": "p"},
        headers=_hdr("alice"),
    )
    r = client.delete("/api/behavior-modes/temp_mode", headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    ids = [m["id"] for m in client.get("/api/behavior-modes", headers=_hdr("alice")).json()["modes"]]
    assert "temp_mode" not in ids


def test_delete_builtin_is_409(client):
    r = client.delete("/api/behavior-modes/mentor", headers=_hdr("alice"))
    assert r.status_code == 409
    assert r.json()["error_class"] == "modes.builtin"


def test_delete_missing_or_others_mode_is_404(client):
    r = client.delete("/api/behavior-modes/does_not_exist", headers=_hdr("alice"))
    assert r.status_code == 404
    assert r.json()["error_class"] == "modes.not_found"

    client.post(
        "/api/behavior-modes",
        json={"id": "alices_only", "name": {"en": "X", "es": "X"},
              "description": {"en": "d", "es": "d"}, "prompt": "p"},
        headers=_hdr("alice"),
    )
    r = client.delete("/api/behavior-modes/alices_only", headers=_hdr("bob"))
    assert r.status_code == 404
    assert r.json()["error_class"] == "modes.not_found"


# ---------------------------------------------------------------------------
# POST /api/behavior-modes/default (admin only)
# ---------------------------------------------------------------------------

def test_set_default_requires_admin(client):
    r = client.post("/api/behavior-modes/default", json={"mode": "terse"})
    assert r.status_code == 403


def test_set_default_admin_changes_global_setting(client, monkeypatch):
    import routes.behavior_mode_routes as bmr_mod
    monkeypatch.setattr(bmr_mod, "require_admin", lambda request: None)

    r = client.post("/api/behavior-modes/default", json={"mode": "terse"})
    assert r.status_code == 200, r.text
    assert r.json() == {"default": "terse"}
    assert client.get("/api/behavior-modes", headers=_hdr("alice")).json()["default"] == "terse"


def test_set_default_rejects_non_builtin(client, monkeypatch):
    import routes.behavior_mode_routes as bmr_mod
    monkeypatch.setattr(bmr_mod, "require_admin", lambda request: None)

    r = client.post("/api/behavior-modes/default", json={"mode": "not_a_real_mode"})
    assert r.status_code == 400
    assert r.json()["error_class"] == "modes.invalid"


# ---------------------------------------------------------------------------
# GET/POST /api/session/{id}/behavior-mode
# ---------------------------------------------------------------------------

def test_get_session_behavior_mode_defaults_to_global(client, sm):
    _mk_session(sm, "s1")
    r = client.get("/api/session/s1/behavior-mode", headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"mode": None, "effective": "default"}


def test_set_then_get_session_behavior_mode(client, sm):
    _mk_session(sm, "s1")
    r = client.post("/api/session/s1/behavior-mode", json={"mode": "adversarial"}, headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"mode": "adversarial"}

    r = client.get("/api/session/s1/behavior-mode", headers=_hdr("alice"))
    assert r.json() == {"mode": "adversarial", "effective": "adversarial"}


def test_set_session_behavior_mode_unknown_id_is_400(client, sm):
    _mk_session(sm, "s1")
    r = client.post("/api/session/s1/behavior-mode", json={"mode": "not_real"}, headers=_hdr("alice"))
    assert r.status_code == 400
    assert r.json()["error_class"] == "modes.invalid"


def test_set_session_behavior_mode_null_clears_it(client, sm):
    _mk_session(sm, "s1")
    client.post("/api/session/s1/behavior-mode", json={"mode": "adversarial"}, headers=_hdr("alice"))
    r = client.post("/api/session/s1/behavior-mode", json={"mode": None}, headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"mode": None}
    assert client.get("/api/session/s1/behavior-mode", headers=_hdr("alice")).json()["mode"] is None


def test_session_behavior_mode_routes_404_for_someone_elses_session(client, sm):
    _mk_session(sm, "s1", owner="alice")
    r = client.get("/api/session/s1/behavior-mode", headers=_hdr("bob"))
    assert r.status_code == 404
    r = client.post("/api/session/s1/behavior-mode", json={"mode": "terse"}, headers=_hdr("bob"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/behavior-modes/check
# ---------------------------------------------------------------------------

def test_check_route_returns_check_response_shape(client):
    r = client.post(
        "/api/behavior-modes/check",
        json={"mode": "adversarial", "text": "Great question, absolutely."},
        headers=_hdr("alice"),
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"checked", "violations"}
    assert any(v["rule"] == "forbidden_phrases" for v in body["violations"])


def test_check_route_unknown_mode_is_404(client):
    r = client.post(
        "/api/behavior-modes/check",
        json={"mode": "not_a_real_mode", "text": "hi"},
        headers=_hdr("alice"),
    )
    assert r.status_code == 404
    assert r.json()["error_class"] == "modes.not_found"
