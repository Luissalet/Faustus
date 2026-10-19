"""Native conversation reads follow the existing single-user owner contract."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database
from core.models import ChatMessage
from routes import hoard_copilot_routes as routes
from services import hoard_copilot
from src import agent_runs
from src.owner_identity import DEFAULT_LOCAL_OWNER


@pytest.fixture
def facade(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "sessions.db"))
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    store = hoard_copilot.HoardCopilotStore(str(tmp_path / "copilot.db"))
    monkeypatch.setattr(hoard_copilot, "get_store", lambda: store)
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: None)
    sessions = {}
    manager = SimpleNamespace(get_session=sessions.__getitem__)
    router = routes.setup_hoard_copilot_routes(manager)
    get = next(route.endpoint for route in router.routes if route.path.endswith("/conversations/{sid}"))
    listed = next(route.endpoint for route in router.routes
                  if route.path.endswith("/conversations") and "GET" in route.methods)

    def add(owner, sid="normal"):
        with factory() as db:
            db.add(database.Session(id=sid, name="Native chat", owner=owner, model="fixture",
                                    endpoint_url="http://invalid.example/v1"))
            db.commit()
        session = SimpleNamespace(id=sid, name="Native chat", project_id=None, archived=False,
                                  history=[ChatMessage(role="user", content="Complete synthetic human: á\nline two"),
                                           ChatMessage(role="assistant", content='{"answer": 42}')])
        sessions[sid] = session
        return session

    def request(user=None):
        auth = SimpleNamespace(is_configured=True, is_admin=lambda name: name in {"alice", "bob"})
        app = SimpleNamespace(state=SimpleNamespace(auth_manager=auth))
        value = Request({"type": "http", "headers": [], "app": app, "client": ("127.0.0.1", 1)})
        value.state.current_user = user
        return value

    yield SimpleNamespace(add=add, get=get, listed=listed, request=request, store=store)
    engine.dispose()


@pytest.mark.parametrize("stored", ["admin", DEFAULT_LOCAL_OWNER, None])
def test_single_user_can_read_every_existing_owner_bucket(facade, monkeypatch, stored):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    session = facade.add(stored)
    result = facade.get(facade.request(), "normal")
    assert result["session_id"] == session.id
    assert result["messages"] == [message.to_dict() for message in session.history]
    assert result["context"] is None and result["origin"] is None
    assert result["participants"] == [] and result["run_id"] is None


@pytest.mark.parametrize("stored", ["alice", None, ""])
def test_login_preserves_matching_owner_and_legacy_ownerless_reads(facade, monkeypatch, stored):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    facade.add(stored)
    assert facade.get(facade.request("alice"), "normal")["session_id"] == "normal"


def test_login_still_hides_a_foreign_owner(facade, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    facade.add("bob")
    with pytest.raises(HTTPException) as error:
        facade.get(facade.request("alice"), "normal")
    assert error.value.status_code == 404


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_missing_session_remains_not_found(facade, monkeypatch, enabled):
    monkeypatch.setenv("AUTH_ENABLED", enabled)
    with pytest.raises(HTTPException) as error:
        facade.get(facade.request("alice" if enabled == "true" else None), "missing")
    assert error.value.status_code == 404


@pytest.mark.parametrize("user", [None, "reader"])
def test_login_admin_gate_remains_in_force(facade, monkeypatch, user):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    facade.add("alice")
    with pytest.raises(HTTPException) as error:
        facade.get(facade.request(user), "normal")
    assert error.value.status_code == 403


def test_single_user_list_uses_the_same_existing_session_check(facade, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    facade.add("admin")
    context = {"origin": {"app_id": "fixture", "data_space_id": "private"}}
    facade.store.attach("normal", context, DEFAULT_LOCAL_OWNER)
    result = facade.listed(facade.request())
    assert [item["session_id"] for item in result["conversations"]] == ["normal"]
