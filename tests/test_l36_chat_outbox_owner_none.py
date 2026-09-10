"""Regression for the lot-36 priority bug: `routes/chat_routes.py::chat_stream`
resolved `owner = effective_user(request)`, which is `None` — not `""` — when
`AUTH_ENABLED=false` (no auth middleware ever populates
`request.state.current_user` in that mode, unlike `require_user()`, which
explicitly falls back to `""`). Studio always sends `client_message_id`
(TASK-03 idempotency), so every real turn hit
`chat_outbox.record_intent(owner=None, ...)`, which violated the `outbox`
table's `owner TEXT NOT NULL` column: an unhandled `sqlite3.IntegrityError`
that a real deployment sees as a plain 500 on every send.

This test goes through an actual `TestClient` HTTP POST against the real
`@router.post("/api/chat_stream")` function (rule 7 of COMUN.md: a behavior
that crosses an HTTP route must be proven through TestClient, not only a
direct function call) — the route is mounted on a bare FastAPI app with the
SAME dependency stubs `tests/test_foreground_model_routing.py::
_chat_stream_endpoint` already uses for every other chat_stream test (reused,
not duplicated — hard rule 4), with exactly one difference: `effective_user`
is left as the REAL function instead of being patched to always return
"alice", and no auth middleware is mounted — so `request.state.current_user`
is genuinely absent, the same as a production request under
`AUTH_ENABLED=false`.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.chat_routes as chat_routes
from src import chat_outbox
from src.auth_helpers import effective_user as real_effective_user
from tests.test_foreground_model_routing import _chat_stream_endpoint


@pytest.fixture()
def outbox_dir(tmp_path, monkeypatch):
    """Same isolation as tests/test_chat_idempotency.py::outbox_dir."""
    from src import agent_runs
    path = tmp_path / "chat_outbox.sqlite3"
    monkeypatch.setattr(chat_outbox, "default_path", lambda: path)
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    return path


def _client_with_real_owner_resolution(monkeypatch) -> TestClient:
    """Mount the real `/api/chat_stream` function on a bare TestClient app.

    Reuses `_chat_stream_endpoint`'s full stub surface (session, model
    streaming, foreground routing, …) and then undoes JUST its
    `effective_user` patch, so the route resolves owner the same way a real,
    unauthenticated-mode request does.
    """
    captured: dict = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    monkeypatch.setattr(chat_routes, "effective_user", real_effective_user)

    app = FastAPI()
    app.add_api_route("/api/chat_stream", endpoint, methods=["POST"])
    return TestClient(app)


def test_chat_stream_with_client_message_id_and_no_auth_state_returns_200(monkeypatch, outbox_dir):
    """The exact repro: a POST with `client_message_id` and no
    `request.state.current_user` (AUTH_ENABLED=false in production) must
    succeed, not 500. Before the fix this raised `sqlite3.IntegrityError`
    from inside `chat_outbox.record_intent`, which FastAPI's default
    exception handling turns into a 500 for any real client."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = _client_with_real_owner_resolution(monkeypatch)

    response = client.post(
        "/api/chat_stream",
        data={
            "message": "hello",
            "session": "session-1",
            "mode": "chat",
            "client_message_id": "cid-owner-none",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    # The outbox row landed with the normalized "" owner, not a crash —
    # and a second POST with the same id now replays instead of retrying.
    row = chat_outbox.get(owner="", session_id="session-1", client_message_id="cid-owner-none")
    assert row is not None
    assert row["status"] == "finished"

    replay = client.post(
        "/api/chat_stream",
        data={
            "message": "hello",
            "session": "session-1",
            "mode": "chat",
            "client_message_id": "cid-owner-none",
        },
    )
    assert replay.status_code == 200
    assert replay.headers.get("X-Faustus-Idempotent-Replay") == "1"
