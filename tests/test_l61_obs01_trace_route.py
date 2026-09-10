"""Lote 61 — OBS-01: `GET /api/observability/trace/{call_id}`, the HTTP door
onto `src.agent_runs.trace_for_call` (the function itself: commit a674903,
tests/test_l60_obs01_trace_for_call.py). Before this lote there was no route
at all -- `grep -rn "trace_for_call" routes/` on the pre-lote tree had no
matches outside `src/agent_runs.py` itself.

Auth is the thing worth testing here, not `trace_for_call`'s own logic
(already covered): a caller who names a `session_id` may only trace within a
session they own, and a caller who omits it (an instance-wide search) must be
an admin.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.observability_routes as observability_routes
import src.agent_runs as agent_runs


def _build_app(monkeypatch, *, trace_result=None, verify_raises=None, admin_raises=None):
    def _fake_trace_for_call(call_id, *, session_id=None):
        return trace_result or {
            "call_id": call_id, "events": [], "artifacts": [], "receipt": None,
            "found": False, "session_id_used": session_id,
        }

    monkeypatch.setattr(agent_runs, "trace_for_call", _fake_trace_for_call)

    def _verify_session_owner(request, session_id):
        if verify_raises is not None:
            raise verify_raises

    def _require_admin(request):
        if admin_raises is not None:
            raise admin_raises

    monkeypatch.setattr(observability_routes, "_verify_session_owner", _verify_session_owner)
    monkeypatch.setattr(observability_routes, "require_admin", _require_admin)

    app = FastAPI()
    app.include_router(observability_routes.setup_observability_routes())
    return app


def _client(monkeypatch, **kw):
    return TestClient(_build_app(monkeypatch, **kw))


def test_session_scoped_lookup_calls_trace_for_call_with_that_session(monkeypatch):
    seen = {}

    def _fake(call_id, *, session_id=None):
        seen["call_id"], seen["session_id"] = call_id, session_id
        return {"call_id": call_id, "events": [], "artifacts": [], "receipt": None, "found": True}

    monkeypatch.setattr(agent_runs, "trace_for_call", _fake)
    monkeypatch.setattr(observability_routes, "_verify_session_owner", lambda request, sid: None)
    monkeypatch.setattr(observability_routes, "require_admin",
                         lambda request: (_ for _ in ()).throw(AssertionError("must not require admin")))

    app = FastAPI()
    app.include_router(observability_routes.setup_observability_routes())
    response = TestClient(app).get("/api/observability/trace/call_1", params={"session_id": "sid-a"})

    assert response.status_code == 200
    assert response.json()["found"] is True
    assert seen == {"call_id": "call_1", "session_id": "sid-a"}


def test_session_scoped_lookup_for_a_session_the_caller_does_not_own_is_rejected(monkeypatch):
    app = _build_app(monkeypatch, verify_raises=HTTPException(404, "Session not found"))
    response = TestClient(app).get(
        "/api/observability/trace/call_1", params={"session_id": "someone-elses-session"},
    )
    assert response.status_code == 404


def test_unscoped_lookup_requires_admin_and_is_rejected_for_a_non_admin(monkeypatch):
    app = _build_app(monkeypatch, admin_raises=HTTPException(403, "Admin only"))
    response = TestClient(app).get("/api/observability/trace/call_1")
    assert response.status_code == 403


def test_unscoped_lookup_reaches_trace_for_call_for_an_admin(monkeypatch):
    seen = {}

    def _fake(call_id, *, session_id=None):
        seen["session_id"] = session_id
        return {"call_id": call_id, "events": [], "artifacts": [], "receipt": None, "found": False}

    monkeypatch.setattr(agent_runs, "trace_for_call", _fake)
    monkeypatch.setattr(observability_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(
        observability_routes, "_verify_session_owner",
        lambda request, sid: (_ for _ in ()).throw(AssertionError("must not be called without session_id")),
    )

    app = FastAPI()
    app.include_router(observability_routes.setup_observability_routes())
    response = TestClient(app).get("/api/observability/trace/call_never_happened")

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert seen["session_id"] is None


def test_unknown_call_id_answers_honestly_not_found_not_an_error(monkeypatch):
    """Same real function `test_l60_obs01_trace_for_call.py` already proves
    never raises for an unknown id -- this proves the route surfaces that
    answer as a clean 200, not a 404/500."""
    app = _build_app(
        monkeypatch,
        trace_result={"call_id": "nope", "events": [], "artifacts": [], "receipt": None, "found": False},
    )
    response = TestClient(app).get(
        "/api/observability/trace/nope", params={"session_id": "sid-a"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "call_id": "nope", "events": [], "artifacts": [], "receipt": None, "found": False,
    }
