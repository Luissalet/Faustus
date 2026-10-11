"""Facade and import use real canonical SQLite rows and SessionManager."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database, middleware, session_manager as managers
from core.session_manager import SessionManager
from services import hoard_copilot
from services.projects import ProjectStore
from routes import hoard_copilot_routes as routes


def context():
    return {"origin": {"app_id": "writer", "data_space_id": "private-qa"},
            "project": {"external_project_id": "book1", "title": "QA book"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "canonical.db"))
    database.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(managers, "SessionLocal", session_factory)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    monkeypatch.setattr(routes, "storage_owner_for_request", lambda _: "qa-owner")
    monkeypatch.setattr(hoard_copilot, "_store", hoard_copilot.HoardCopilotStore(str(tmp_path / "links.db"), ProjectStore(str(tmp_path / "project-data"))))
    monkeypatch.setattr(hoard_copilot, "global_model", lambda owner: {"model": "scripted", "endpoint_url": "http://127.0.0.1:1/v1/chat/completions", "headers": {}})
    manager = SessionManager()
    app = FastAPI()
    app.include_router(routes.setup_hoard_copilot_routes(manager))
    return TestClient(app)


def test_create_and_continue_are_the_same_canonical_session(client):
    created = client.post("/api/hoard-copilot/conversations", json={"context": context()}).json()
    sid = created["session_id"]
    with database.SessionLocal() as db:
        row = db.query(database.Session).filter(database.Session.id == sid).one()
        assert row.project_id == created["project_id"]
        assert row.model == "scripted"
    detail = client.get(f"/api/hoard-copilot/conversations/{sid}").json()
    assert detail["session_id"] == sid and detail["messages"] == []
    listed = client.get("/api/hoard-copilot/conversations", params={"app_id": "writer", "data_space_id": "private-qa", "external_project_id": "book1"}).json()
    assert [row["session_id"] for row in listed["conversations"]] == [sid]
    assert client.get("/api/hoard-copilot/conversations", params={"data_space_id": "production"}).json()["conversations"] == []


def test_import_repeats_without_duplicate_and_preserves_tool_metadata_and_dates(client):
    body = {"context": context(), "source_thread_id": "old-thread", "name": "Old conversation", "messages": [
        {"source_id": "u1", "role": "user", "content": "Cambia esto", "created_at": "2026-10-09T12:00:00+02:00"},
        {"source_id": "a1", "role": "assistant", "content": "Hecho", "created_at": "2026-10-09T12:01:00+02:00", "metadata": {"tool_events": [{"tool": "wh_update_chapter", "undo": "undo1"}], "usage": {"tokens": 20}}},
    ]}
    first = client.post("/api/hoard-copilot/imports", json=body)
    assert first.status_code == 200, first.text
    repeated = client.post("/api/hoard-copilot/imports", json=body).json()
    assert repeated["session_id"] == first.json()["session_id"]
    assert repeated["imported"] == 0 and repeated["message_count"] == 2
    with database.SessionLocal() as db:
        rows = db.query(database.ChatMessage).filter(database.ChatMessage.session_id == repeated["session_id"]).order_by(database.ChatMessage.timestamp).all()
        assert len(rows) == 2
        assert rows[0].timestamp.isoformat() == "2026-10-09T10:00:00"
        assert json.loads(rows[1].meta_data)["tool_events"][0]["undo"] == "undo1"
    body["messages"].append({"source_id": "u2", "role": "user", "content": "Continue", "created_at": "2026-10-10T10:00:00Z"})
    extension = client.post("/api/hoard-copilot/imports", json=body).json()
    assert extension["imported"] == 1 and extension["message_count"] == 3


def test_import_failure_rolls_back_all_new_messages(client):
    body = {"context": context(), "source_thread_id": "broken", "messages": [
        {"source_id": "a", "role": "user", "content": "first", "created_at": "2026-10-09T10:00:00Z"},
        {"source_id": "b", "role": "assistant", "content": "second", "created_at": "not a date"},
    ]}
    response = client.post("/api/hoard-copilot/imports", json=body)
    assert response.status_code == 400
    with database.SessionLocal() as db:
        assert db.query(database.ChatMessage).count() == 0
    body["messages"][1]["created_at"] = "2026-10-09T10:01:00Z"
    recovered = client.post("/api/hoard-copilot/imports", json=body).json()
    assert recovered["message_count"] == 2


def test_unknown_session_and_invalid_context_are_explicit(client):
    assert client.get("/api/hoard-copilot/conversations/missing").status_code == 404
    response = client.post("/api/hoard-copilot/conversations", json={"context": {"origin": {"app_id": "writer"}}})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_context"


def test_reimport_does_not_resurrect_deleted_conversation(client):
    body = {"context": context(), "source_thread_id": "deleted-chat", "messages": [
        {"source_id": "old", "role": "user", "content": "Historical"}]}
    first = client.post("/api/hoard-copilot/imports", json=body).json()
    with database.SessionLocal() as db:
        db.query(database.ChatMessage).filter(database.ChatMessage.session_id == first["session_id"]).delete()
        db.query(database.Session).filter(database.Session.id == first["session_id"]).delete()
        db.commit()
    replay = client.post("/api/hoard-copilot/imports", json=body).json()
    assert replay == {"session_id": None, "deleted": True, "imported": 0, "message_count": 0}
    with database.SessionLocal() as db:
        assert db.query(database.Session).count() == 0


def test_bad_json_shape_and_metadata_leave_no_messages(client):
    assert client.post("/api/hoard-copilot/conversations", json=[]).status_code == 400
    assert client.post("/api/hoard-copilot/imports", json={"context": context(), "source_thread_id": "bad-meta", "messages": [
        {"source_id": "one", "role": "user", "content": "text", "metadata": []}]}).status_code == 400


def test_model_projection_preserves_imported_tool_pairs_without_system_authority():
    from core.models import Session, ChatMessage
    session = Session("projection", "Projection", "", "")
    session.history = [
        ChatMessage("system", "Old instruction", {"source": "hoard_import"}),
        ChatMessage("assistant", "", {"source": "hoard_import", "tool_calls": [{"id": "call-one", "type": "function", "function": {"name": "wh_get_writing", "arguments": "{}"}}]}),
        ChatMessage("tool", "Original chapter content", {"source": "hoard_import", "tool_call_id": "call-one"}),
    ]
    projected = session.get_context_messages()
    assert projected[0]["role"] == "user" and "Old instruction" in projected[0]["content"]
    assert session.history[0].role == "system" and session.history[0].content == "Old instruction"
    assert projected[1]["tool_calls"][0]["id"] == "call-one"
    assert projected[2]["tool_call_id"] == "call-one"
    from src.llm_core import _sanitize_llm_messages
    clean = _sanitize_llm_messages(projected)
    assert any(message.get("tool_call_id") == "call-one" and message.get("content") == "Original chapter content" for message in clean)
