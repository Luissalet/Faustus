"""A12 follow-up: POST /api/questions/{question_id}/answer — the answer path
for a question opened under a SYNTHETIC session (`code_mode:<session>`, a
paused Code Mode approval, or `mcp:<server_id>`, an MCP elicitation/sampling
question) rather than a real chat session. Neither has a chat turn to
resume, so this route only resolves the question and starts nothing --
unlike the ordinary `POST /api/chat` `question_id` path, which is refused
here on purpose (`answer_via_chat`) because answering THAT question is also
what resumes the turn it ended.

Same harness shape as tests/test_l29_questions_route.py: a real FastAPI app
+ TestClient against the actual `setup_chat_routes()` router, a tiny
test-only auth middleware, and a real `question_store` pointed at a temp
sqlite file (COMUN.md rule 7: no mocking across an HTTP boundary).
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import routes.chat_routes as chat_routes
from src import question_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "questions.sqlite3")

    router = chat_routes.setup_chat_routes(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
    )
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user", "")
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _open(owner: str, session_id: str) -> dict:
    return question_store.open_question(
        "Approve fake_gated_tool?", session_id=session_id, owner=owner,
        options=[
            {"label": "Approve", "description": "", "id": "approve"},
            {"label": "Deny", "description": "", "id": "deny"},
        ],
        allow_free_text=False,
    )


def test_synthetic_code_mode_session_is_accepted(client):
    q = _open("alice", "code_mode:sess-1")
    resp = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["approve"]},
        headers={"x-test-user": "alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    row = question_store.get_question(q["question_id"], owner="alice")
    assert row["status"] == "answered"
    assert row["answer"]["option_ids"] == ["approve"]


def test_synthetic_mcp_session_is_accepted(client):
    q = _open("", "mcp:some-server")
    resp = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["deny"]},
        headers={"x-test-user": ""},
    )
    assert resp.status_code == 200, resp.text
    row = question_store.get_question(q["question_id"])
    assert row["status"] == "answered"


def test_a_real_chat_session_question_is_refused(client):
    q = _open("alice", "sess-1")
    resp = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["approve"]},
        headers={"x-test-user": "alice"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "answer_via_chat"

    # Refused, not silently consumed: still open for the ordinary path.
    row = question_store.get_question(q["question_id"], owner="alice")
    assert row["status"] == "open"


def test_wrong_owner_is_not_found(client):
    q = _open("alice", "code_mode:sess-1")
    resp = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["approve"]},
        headers={"x-test-user": "mallory"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"

    # Not consumed by the mismatched owner's attempt either.
    row = question_store.get_question(q["question_id"], owner="alice")
    assert row["status"] == "open"


def test_unknown_question_id_is_not_found(client):
    resp = client.post(
        "/api/questions/qst_does_not_exist/answer",
        json={"option_ids": ["approve"]},
        headers={"x-test-user": "alice"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_stale_revision_is_rejected(client):
    q = _open("alice", "code_mode:sess-1")
    resp = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["approve"], "revision": q["revision"] + 1},
        headers={"x-test-user": "alice"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "question_not_resolved"
    assert body["reason"] == "stale_revision"


def test_answering_twice_is_rejected(client):
    q = _open("alice", "code_mode:sess-1")
    first = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["approve"]},
        headers={"x-test-user": "alice"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/questions/{q['question_id']}/answer",
        json={"option_ids": ["deny"]},
        headers={"x-test-user": "alice"},
    )
    assert second.status_code == 409
    assert second.json()["reason"] == "already_answered"
