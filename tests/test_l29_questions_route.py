"""L29 (integrates L28's ACT-03 note): GET /api/questions — the activity
tray's "answer this" queue. Owner-scoped: a question opened for one owner
must never be listed for another (SEC-06, the same fail-closed shape
`question_store.get`/`resolve` already give a single lookup).

Uses a real FastAPI app + TestClient against the actual `setup_chat_routes()`
router (COMUN.md rule 7: no mocking across an HTTP boundary), mirroring
tests/test_contracts_mcp_and_routes.py's pattern of mounting just the router
under test rather than the full app.py. `owner` comes from `request.state.
current_user`, which a real deployment's auth middleware sets; here a tiny
test-only middleware sets it from a header so both owners can be exercised
in one client.

Reverting routes/chat_routes.py's `/api/questions` route (or
src/question_store.py's `list_open`) makes every test below fail: either the
route 404s, or an open question never appears / a wrong owner's leaks in.
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


def _open(owner: str, question: str = "Which library?") -> dict:
    return question_store.open_question(
        question, session_id="sess-1", owner=owner,
        options=[{"label": "A", "id": "opt_a"}, {"label": "B", "id": "opt_b"}],
    )


def test_lists_the_callers_own_open_question(client):
    q = _open("alice")
    resp = client.get("/api/questions", headers={"x-test-user": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    row = body["questions"][0]
    assert row["question_id"] == q["question_id"]
    assert row["session"] == "sess-1"
    assert row["question"] == "Which library?"
    assert row["options"] == [{"label": "A", "id": "opt_a"}, {"label": "B", "id": "opt_b"}]
    assert row["revision"] == 1
    assert "expires_at" in row


def test_never_leaks_another_owners_open_question(client):
    _open("alice")
    resp = client.get("/api/questions", headers={"x-test-user": "bob"})
    assert resp.status_code == 200
    assert resp.json() == {"questions": [], "count": 0}


def test_an_answered_question_no_longer_appears(client):
    q = _open("alice")
    question_store.resolve_question(q["question_id"], {"option_id": "opt_a"}, owner="alice")
    resp = client.get("/api/questions", headers={"x-test-user": "alice"})
    assert resp.json() == {"questions": [], "count": 0}


def test_no_open_questions_is_an_empty_list_not_an_error(client):
    resp = client.get("/api/questions", headers={"x-test-user": "alice"})
    assert resp.status_code == 200
    assert resp.json() == {"questions": [], "count": 0}
