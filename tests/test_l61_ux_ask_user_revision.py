"""Lote 61 — the "revisión del ask_user en vivo" gap named in PENDIENTES.md
§ Spec v2 (M1): `src/question_store.py` has accepted a `revision` kwarg on
`resolve_question`/`Store.resolve` since it was written (see
`tests/test_ask_user_answer_route.py::test_stale_revision_question_id_is_rejected_with_409`,
which exercises `question_store` directly because "a route-level revision
isn't sent by today's Studio"). What was missing was the route actually
reading a `revision` field off the request and threading it through --
`routes/chat_routes.py`'s question_id gate called
`question_store.resolve_question(question_id, answer, owner=owner)` with no
`revision` at all, so a real client sending one had it silently ignored and
a genuinely stale answer was accepted instead of rejected.

This proves the real, end-to-end path (no monkeypatching resolve_question
itself): a client sends `revision`, a stale one is rejected 409
`stale_revision`, a matching one proceeds, and an absent one (every client
before this lote existed) behaves exactly as before.
"""
from __future__ import annotations

import json

import pytest

import src.agent_tools  # noqa: F401 - resolves circular schema imports first
import routes.chat_routes as chat_routes
import src.agent_runs as agent_runs
from src import question_store
from tests.test_ask_user_answer_route import (
    _RouteRequest,
    _chat_stream_endpoint,
    _drain,
    question_db,  # noqa: F401 - reused fixture
)

_TEST_SESSION_ID = "askuser-route-test-session"


@pytest.fixture(autouse=True)
def _clean_agent_runs():
    agent_runs.stop_for_session(_TEST_SESSION_ID)
    yield
    agent_runs.stop_for_session(_TEST_SESSION_ID)


@pytest.mark.asyncio
async def test_a_stale_revision_from_a_real_client_is_rejected_with_409(monkeypatch, question_db):
    """`question_id` alone would let this through -- only reading `revision`
    catches it: the question is still open, un-cancelled, un-answered, but
    at a HIGHER revision than what the client last rendered (e.g. it was
    re-asked with new options after the client already had the card open)."""
    q = question_db.open(
        "Which library, for real?", session_id=_TEST_SESSION_ID,
        options=[{"label": "SQLite", "id": "opt_a"}], revision=5,
    )

    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({
        "question_id": q["question_id"], "option_ids": json.dumps(["opt_a"]),
        "revision": "4",
    }))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body["reason"] == "stale_revision"
    assert body["question_id"] == q["question_id"]
    assert "chat_called" not in captured and "agent_called" not in captured

    resolved = question_db.get(q["question_id"])
    assert resolved["status"] == "open"  # never resolved by the stale answer


@pytest.mark.asyncio
async def test_a_matching_revision_from_a_real_client_resolves_normally(monkeypatch, question_db):
    q = question_db.open(
        "Which library?", session_id=_TEST_SESSION_ID,
        options=[{"label": "SQLite", "id": "opt_a"}], revision=3,
    )
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({
        "question_id": q["question_id"], "option_ids": json.dumps(["opt_a"]),
        "revision": "3",
    }))
    await _drain(response)

    assert captured.get("chat_called") or captured.get("agent_called")
    resolved = question_db.get(q["question_id"])
    assert resolved["status"] == "answered"


@pytest.mark.asyncio
async def test_no_revision_field_behaves_exactly_as_before(monkeypatch, question_db):
    """A client that never sends `revision` (every client before this lote,
    and today's Studio per PENDIENTES.md) is completely unaffected -- the
    question resolves on question_id alone, exactly as it always did."""
    q = question_db.open(
        "Which library?", session_id=_TEST_SESSION_ID,
        options=[{"label": "SQLite", "id": "opt_a"}], revision=7,
    )
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({
        "question_id": q["question_id"], "option_ids": json.dumps(["opt_a"]),
    }))
    await _drain(response)

    assert captured.get("chat_called") or captured.get("agent_called")
    assert question_db.get(q["question_id"])["status"] == "answered"


@pytest.mark.asyncio
async def test_malformed_revision_is_treated_as_absent_not_a_crash(monkeypatch, question_db):
    q = question_db.open(
        "Which library?", session_id=_TEST_SESSION_ID,
        options=[{"label": "SQLite", "id": "opt_a"}], revision=1,
    )
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({
        "question_id": q["question_id"], "option_ids": json.dumps(["opt_a"]),
        "revision": "not-a-number",
    }))
    await _drain(response)

    assert captured.get("chat_called") or captured.get("agent_called")
    assert question_db.get(q["question_id"])["status"] == "answered"


def test_parse_question_revision_helper():
    from routes.chat_routes import _parse_question_revision

    assert chat_routes._parse_question_revision(None) is None
    assert chat_routes._parse_question_revision("") is None
    assert chat_routes._parse_question_revision("3") == 3
    assert chat_routes._parse_question_revision(3) == 3
    assert chat_routes._parse_question_revision("nope") is None
