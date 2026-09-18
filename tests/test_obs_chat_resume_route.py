"""ARCH-01 (version negotiation) and QA-09 (cursor-based replay) at the route
level: `GET /api/chat/resume/{session_id}`.

Calls the route function directly with a lightweight fake Request, the same
pattern tests/test_subagent_steer_routes.py already uses for chat_routes —
building the full FastAPI app + TestClient for this router pulls in far more
than this route touches.
"""
from types import SimpleNamespace

import pytest

from src import agent_runs, api_version


class _Req:
    def __init__(self, headers=None, user="alice"):
        self.headers = dict(headers or {})
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user=user)


@pytest.fixture
def routes(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **kw: None)
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )

    def _route(path, method):
        for r in reversed(router.routes):
            if r.path == path and method in getattr(r, "methods", set()):
                return r.endpoint
        raise AssertionError(f"route not found: {method} {path}")
    return _route


class _FakeRun:
    run_id = "run-42"


@pytest.mark.asyncio
async def test_no_version_header_is_unaffected(routes, monkeypatch):
    """Compatibility: a caller that has never heard of this scheme (every
    caller before this lot) still gets the stream, not a 426."""
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: _FakeRun())
    seen = {}

    def _fake_subscribe(sid, run, from_sequence=None):
        seen["from_sequence"] = from_sequence
        async def _gen():
            yield "data: [DONE]\n\n"
        return _gen()
    monkeypatch.setattr(agent_runs, "subscribe", _fake_subscribe)

    resume = routes("/api/chat/resume/{session_id}", "GET")
    # cursor=None is what FastAPI's own dependency resolution passes for an
    # omitted `?cursor=`; calling the route function directly (bypassing
    # FastAPI, per this fixture's whole approach) skips that resolution, so
    # it is given explicitly here rather than asserting on the unresolved
    # Query() sentinel object.
    resp = await resume(_Req(), "sid", cursor=None)
    assert resp.headers["X-Odysseus-Run-Id"] == "run-42"
    assert resp.headers[api_version.API_VERSION_HEADER] == api_version.API_VERSION
    assert seen["from_sequence"] is None


@pytest.mark.asyncio
async def test_an_old_client_gets_426_not_a_stream(routes, monkeypatch):
    """FAILS without the check: before this lot, a client identifying itself
    as an incompatible version would be handed the SSE stream regardless."""
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: _FakeRun())

    def _boom(sid, run, from_sequence=None):
        raise AssertionError("must not start streaming an unsupported client")
    monkeypatch.setattr(agent_runs, "subscribe", _boom)

    resume = routes("/api/chat/resume/{session_id}", "GET")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await resume(_Req(headers={api_version.CLIENT_VERSION_HEADER: "0.1"}), "sid")
    assert exc.value.status_code == 426
    assert "0.1" in exc.value.detail


@pytest.mark.asyncio
async def test_a_compatible_client_version_is_accepted(routes, monkeypatch):
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: _FakeRun())
    monkeypatch.setattr(agent_runs, "subscribe", lambda sid, run, from_sequence=None: _agen())

    async def _agen():
        yield "data: [DONE]\n\n"

    resume = routes("/api/chat/resume/{session_id}", "GET")
    resp = await resume(_Req(headers={api_version.CLIENT_VERSION_HEADER: api_version.API_VERSION}), "sid")
    assert resp.headers["X-Odysseus-Run-Id"] == "run-42"


@pytest.mark.asyncio
async def test_cursor_is_threaded_through_to_subscribe(routes, monkeypatch):
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: _FakeRun())
    seen = {}

    def _fake_subscribe(sid, run, from_sequence=None):
        seen["from_sequence"] = from_sequence
        async def _gen():
            yield "data: [DONE]\n\n"
        return _gen()
    monkeypatch.setattr(agent_runs, "subscribe", _fake_subscribe)

    resume = routes("/api/chat/resume/{session_id}", "GET")
    await resume(_Req(), "sid", cursor=7)
    assert seen["from_sequence"] == 7


@pytest.mark.asyncio
async def test_chat_stream_rejects_an_old_client_before_any_work(routes):
    """The negotiation check in POST /api/chat_stream is the very first
    statement in the route — an incompatible client must be rejected before
    form/body parsing (or anything heavier) ever runs. FAILS without the
    check: before this lot, an old-identifying client's request would be
    processed like any other."""
    chat_stream = routes("/api/chat_stream", "POST")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await chat_stream(_Req(headers={api_version.CLIENT_VERSION_HEADER: "0.1"}))
    assert exc.value.status_code == 426


@pytest.mark.asyncio
async def test_no_active_run_is_still_a_404(routes, monkeypatch):
    """Compatibility: this lot must not change the no-active-run answer."""
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: None)
    resume = routes("/api/chat/resume/{session_id}", "GET")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await resume(_Req(), "sid")
    assert exc.value.status_code == 404
