"""Lote 44 (closes an L42 gap): `src/agent_loop.py::stream_agent_loop` closes
the run's WEB-03 per-task browser session (`src.browser_sessions`, wired
into launches by Lote 42's `src/mcp_manager.py`/`src/builtin_mcp.py`) once
the run ends, for every way a run can end: it completes, it raises, or the
consumer disconnects mid-stream (`.aclose()`).

`src/mcp_manager.py`'s `close_browser_session_for_task` docstring named the
exact gap this closes: "the caller this lote could not reach is the point
where an agent run ends... out of this lote's file scope". This lote's file
list allows exactly one change to agent_loop.py ("SOLO cerrar la sesión de
navegador al terminar el run") — implemented as a rename-and-wrap
(`_stream_agent_loop_body` does the real work unchanged;
`stream_agent_loop` is the thin wrapper under test here), so these tests
replace `_stream_agent_loop_body` with a tiny fake generator rather than
driving the whole (five-thousand-line) real agent loop.

Revert proof (COMUN.md rule 5): with the `functools.wraps` wrapper this lote
added removed and `_stream_agent_loop_body` renamed back to
`stream_agent_loop` directly (`cp`-backed, never git), every test below
fails — the browser session opened in each test's setup is never closed,
because there is no caller of `close_browser_session_for_task` left at all
(confirmed by hand during development; see the report).
"""
from __future__ import annotations

import asyncio

import pytest

import src.agent_loop as agent_loop
from src import browser_sessions as bs


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    bs._SESSIONS.clear()
    yield
    bs._SESSIONS.clear()


def _open(owner="alice", task="sess-1"):
    return bs.open_session(owner, task, ephemeral=True)


def _run(coro):
    return asyncio.run(coro)


def test_a_completed_run_closes_the_session(monkeypatch):
    _open()

    async def fake_body(*a, owner=None, session_id=None, **k):
        yield 'data: {"delta": "hi"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", fake_body)

    async def go():
        return [c async for c in agent_loop.stream_agent_loop(
            "https://api.example/v1", "m", [], owner="alice", session_id="sess-1")]

    chunks = _run(go())
    assert chunks == ['data: {"delta": "hi"}\n\n', "data: [DONE]\n\n"]
    assert bs.get_session("alice", "sess-1").is_open is False


def test_a_run_that_raises_still_closes_the_session(monkeypatch):
    _open()

    async def fake_body(*a, owner=None, session_id=None, **k):
        yield 'data: {"delta": "partial"}\n\n'
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", fake_body)

    async def go():
        chunks = []
        async for c in agent_loop.stream_agent_loop(
            "https://api.example/v1", "m", [], owner="alice", session_id="sess-1"):
            chunks.append(c)
        return chunks

    with pytest.raises(RuntimeError, match="provider exploded"):
        _run(go())
    assert bs.get_session("alice", "sess-1").is_open is False


def test_a_disconnect_mid_stream_closes_the_session(monkeypatch):
    """`.aclose()` on the outer generator (what a dropped SSE connection
    triggers) must close the session synchronously, not whenever garbage
    collection eventually gets to the abandoned inner generator."""
    _open()
    inner_cancelled = {"v": False}

    async def fake_body(*a, owner=None, session_id=None, **k):
        try:
            yield 'data: {"delta": "still going"}\n\n'
            await asyncio.sleep(10)
            yield 'data: {"delta": "never reached"}\n\n'
        except GeneratorExit:
            inner_cancelled["v"] = True
            raise

    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", fake_body)

    async def go():
        gen = agent_loop.stream_agent_loop(
            "https://api.example/v1", "m", [], owner="alice", session_id="sess-1")
        first = await gen.__anext__()
        assert first == 'data: {"delta": "still going"}\n\n'
        await gen.aclose()

    _run(go())
    assert inner_cancelled["v"] is True, "the inner generator's own GeneratorExit handling must run synchronously"
    assert bs.get_session("alice", "sess-1").is_open is False


def test_no_session_open_is_a_quiet_no_op(monkeypatch):
    """The common case (no browser tool ever used this run): closing an
    already-nonexistent session must not raise or change the stream."""
    async def fake_body(*a, owner=None, session_id=None, **k):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", fake_body)

    async def go():
        return [c async for c in agent_loop.stream_agent_loop(
            "https://api.example/v1", "m", [], owner="bob", session_id="never-opened")]

    assert _run(go()) == ["data: [DONE]\n\n"]


def test_a_close_side_failure_never_masks_the_real_outcome(monkeypatch):
    """A broken `close_browser_session_for_task` must not turn a clean
    stream into an error, and must not swallow a real one either."""
    import src.builtin_mcp as builtin_mcp

    def boom(owner_id, task_id):
        raise RuntimeError("browser_sessions is down")

    monkeypatch.setattr(builtin_mcp, "close_browser_session_for_task", boom)

    async def fake_body(*a, owner=None, session_id=None, **k):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", fake_body)

    async def go():
        return [c async for c in agent_loop.stream_agent_loop(
            "https://api.example/v1", "m", [], owner="alice", session_id="sess-1")]

    # Does not raise, and the stream is delivered untouched.
    assert _run(go()) == ["data: [DONE]\n\n"]


def test_the_public_signature_still_shows_every_real_parameter():
    """`@functools.wraps` must keep `stream_agent_loop` introspectable as the
    real function — other lotes' own tests already assert this
    (tests/test_steer_routes.py, tests/test_subagent_steer_routes.py) for
    specific parameters; this pins the wrapping mechanism itself."""
    import inspect
    sig = inspect.signature(agent_loop.stream_agent_loop)
    assert "pending_pause" in sig.parameters
    assert "pending_user_messages" in sig.parameters
    assert "owner" in sig.parameters
    assert "session_id" in sig.parameters
