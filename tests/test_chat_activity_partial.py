"""GET /api/chat/activity never reports a running turn as finished because an
ownership check failed transiently (a busy database): it retries once, and
when the check keeps failing it lists the other runs and says the answer is
partial (`unverified`), instead of answering "nothing is running"."""

from types import SimpleNamespace

import pytest


class _Req:
    def __init__(self):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")


def _activity(monkeypatch, verify):
    from routes import chat_routes
    from src import agent_runs
    monkeypatch.setattr(chat_routes, "_verify_session_owner", verify)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: ["s1", "s2"])
    monkeypatch.setattr(agent_runs, "activity_details", lambda: {})
    monkeypatch.setattr(agent_runs, "get_run_id", lambda sid: None)
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    return next(r.endpoint for r in reversed(router.routes) if r.path == "/api/chat/activity")


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried(monkeypatch):
    calls = []

    def verify(request, sid, *a, **k):
        calls.append(sid)
        if sid == "s1" and calls.count("s1") == 1:
            raise RuntimeError("database is locked")
    out = await _activity(monkeypatch, verify)(_Req())
    assert out["running"] == ["s1", "s2"]
    assert out["unverified"] == 0


@pytest.mark.asyncio
async def test_a_persistent_failure_is_reported_not_hidden(monkeypatch):
    def verify(request, sid, *a, **k):
        if sid == "s1":
            raise RuntimeError("database is locked")
    out = await _activity(monkeypatch, verify)(_Req())
    assert out["running"] == ["s2"]          # the other run is still listed
    assert out["unverified"] == 1            # and the answer says it is partial


@pytest.mark.asyncio
async def test_other_users_runs_stay_hidden_without_counting_as_errors(monkeypatch):
    from fastapi import HTTPException

    def verify(request, sid, *a, **k):
        if sid == "s1":
            raise HTTPException(404, "nope")
    out = await _activity(monkeypatch, verify)(_Req())
    assert out["running"] == ["s2"] and out["unverified"] == 0
