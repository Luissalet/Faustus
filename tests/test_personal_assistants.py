import json
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import personal_assistants as assistants


@pytest.fixture
def database(tmp_path, monkeypatch):
    from src import constants, agent_runs
    from core import database as db
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    engine = create_engine("sqlite:///" + str(tmp_path / "canonical.sqlite3"))
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [])
    with factory() as s:
        s.add(db.Session(id="chat-a", name="Native chat", endpoint_url="", model="", owner="luis", project_id="project-a"))
        s.add(db.Session(id="chat-b", name="Other owner", endpoint_url="", model="", owner="other"))
        s.add(db.ChatMessage(id="message-a", session_id="chat-a", role="assistant", content=json.dumps("Original result"), timestamp=datetime(2026, 10, 17)))
        s.commit()
    yield factory
    engine.dispose()


def test_assistant_uses_same_session_and_survives_new_store(database, monkeypatch):
    from src import agent_runs
    assistants.save("chat-a", "My helper", "Continuing work", "luis")
    # New store/SQLite connection on every call, ordinary chat remains canonical.
    rows = assistants.list_assistants("luis")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "chat-a"
    assert rows[0]["project_id"] == "project-a"
    assert rows[0]["preview"] == "Original result"
    assert rows[0]["state"] == "ready"
    assert assistants.list_assistants("other") == []
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: ["chat-a"])
    monkeypatch.setattr(agent_runs, "get_run_id", lambda sid: "actual-run")
    assert assistants.list_assistants("luis")[0]["run_id"] == "actual-run"
    assistants.save("chat-a", "Renamed", "New notes", "luis")
    assert len(assistants.list_assistants("luis")) == 1
    assert assistants.list_assistants("luis")[0]["name"] == "Renamed"


def test_remove_preserves_chat_messages_and_can_restore(database):
    from core.database import Session, ChatMessage
    assistants.save("chat-a", "Helper", "", "luis")
    assistants.retire("chat-a", "luis")
    assert assistants.list_assistants("luis") == []
    with database() as db:
        assert db.query(Session).filter_by(id="chat-a").count() == 1
        assert db.query(ChatMessage).filter_by(session_id="chat-a").one().content == json.dumps("Original result")
    assistants.save("chat-a", "Restored helper", "", "luis")
    assert assistants.list_assistants("luis")[0]["preview"] == "Original result"


def test_missing_or_other_owned_chat_is_not_replaced(database):
    from core.database import Session
    with pytest.raises(KeyError):
        assistants.save("chat-b", "Stolen", "", "luis")
    assistants.save("chat-a", "Helper", "", "luis")
    with database() as db:
        db.query(Session).filter_by(id="chat-a").delete()
        db.commit()
    assert assistants.list_assistants("luis")[0]["state"] == "unavailable"
    with database() as db:
        assert db.query(Session).filter_by(id="chat-a").count() == 0


def client(monkeypatch):
    import routes.mobile_routes as routes
    monkeypatch.setattr(routes, "_mobile_owner", lambda request: "luis")
    app = FastAPI()
    app.include_router(routes.setup_mobile_routes())
    return TestClient(app)


def test_native_api_adoption_edit_and_remove_preserve_canonical_project(database, monkeypatch):
    with client(monkeypatch) as c:
        r = c.post("/api/mobile/assistants", json={"name": "Personal helper", "session_id": "chat-a", "project_id": "wrong"})
        assert r.status_code == 200
        assert r.json()["project_id"] == "project-a"
        assert c.get("/api/mobile/assistants").json()["assistants"][0]["preview"] == "Original result"
        assert c.patch("/api/mobile/assistants/chat-a", json={"name": "Renamed"}).status_code == 200
        assert c.post("/api/mobile/assistants", json={"name": "No", "session_id": "chat-b"}).status_code == 404
        assert c.delete("/api/mobile/assistants/chat-a").json()["chat_preserved"]
        assert c.get("/api/mobile/assistants").json() == {"assistants": []}


def test_creation_uses_ordinary_session_route_without_running_model(database, monkeypatch):
    import routes.mobile_routes as routes
    from core.database import Session
    calls = []
    class NativeSessionAPI:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, path, data, headers):
            calls.append((path, data, headers))
            with database() as db:
                db.add(Session(id="new-chat", name=data["name"], owner="luis", project_id=data["project_id"], endpoint_url="", model=""))
                db.commit()
            return httpx.Response(200, json={"id": "new-chat"})
    monkeypatch.setattr(routes.httpx, "AsyncClient", NativeSessionAPI)
    with client(monkeypatch) as c:
        assert c.post("/api/mobile/assistants", json={"name": "   "}).status_code == 422
        assert not calls
        r = c.post("/api/mobile/assistants", json={"name": "New helper", "project_id": "project-a"})
        assert r.status_code == 200
        assert r.json()["session_id"] == "new-chat"
        assert len(calls) == 1
        assert calls[0][0] == "/api/session"
        assert calls[0][1]["skip_validation"] == "true"
        assert calls[0][2]["X-Odysseus-Owner"] == "luis"
