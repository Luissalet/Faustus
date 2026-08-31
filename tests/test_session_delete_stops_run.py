"""BUG 3 — deleting a chat did not stop its agent run.

`delete_session`, `bulk_delete_sessions` and `delete_all_sessions` never told
`src.agent_runs` anything. The run kept executing tools and writing files, it
kept its queue-lane slot (with `agent_queue_local_concurrency=1` that blocks
every other chat), and it was UNSTOPPABLE from the UI: /api/chat/activity and
/api/chat/stop 404 as soon as the session is gone.

`agent_runs.stop()` deliberately fails closed (it demands the run_id so a stale
tab cannot cancel the run that replaced its own), so the routes get an explicit
`stop_for_session()` instead — `stop()` itself is untouched.
"""

import asyncio
import json
import sys
import types
from unittest.mock import MagicMock

import pytest

from src import agent_runs


# ── helpers ────────────────────────────────────────────────────────────────

def _route(router, path, method="GET"):
    """The MOST RECENTLY registered handler for a path (setup_session_routes is
    called once per test and appends to the module-level router)."""
    for r in reversed(router.routes):
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _stub_multipart_if_missing(monkeypatch):
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


def _no_db_row():
    """SessionLocal whose query(...).filter(...).first() yields no row: the
    starred-session guard passes and no ORM work happens."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return MagicMock(return_value=db)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(agent_runs, "_setting",
                        lambda key, default=None: {"agent_runs_persist": False,
                                                   "agent_queue_local_concurrency": 1}.get(key, default),
                        raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._EXTERNAL_BUSY.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._EXTERNAL_BUSY.clear()


@pytest.fixture
def routes(monkeypatch):
    import routes.session_routes as sr
    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setattr(sr, "SessionLocal", _no_db_row())
    monkeypatch.setattr(sr, "_verify_session_owner", lambda *a, **kw: None)
    manager = MagicMock()
    manager.delete_session.return_value = True
    # setup_session_routes() APPENDS to the module-level `router`. Snapshot and
    # restore it so this file never leaves stale handlers (bound to our mock
    # manager) behind for sibling test modules that look routes up by path.
    snapshot = list(sr.router.routes)
    try:
        yield sr.setup_session_routes(manager, {}), manager
    finally:
        sr.router.routes[:] = snapshot


async def _never_ending(started: asyncio.Event, tool_calls: list):
    """A run that keeps calling tools until it is cancelled."""
    started.set()
    try:
        while True:
            tool_calls.append(1)
            await asyncio.sleep(0.01)
            yield 'data: {"type": "tool_output", "tool": "write_file"}\n\n'
    except asyncio.CancelledError:
        raise


async def _start_run(sid, lane="local"):
    started, tool_calls = asyncio.Event(), []
    run = agent_runs.start(sid, _never_ending(started, tool_calls), lane=lane, label=sid)
    await asyncio.wait_for(started.wait(), 2)
    assert agent_runs.is_active(sid)
    return run, tool_calls


# ── DELETE /api/session/{sid} ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_session_stops_the_run_and_frees_the_lane(routes):
    router, manager = routes
    delete_session = _route(router, "/api/session/{sid}", "DELETE")

    run, tool_calls = await _start_run("doomed")
    lane = agent_runs._LANES["local"]
    assert lane.active == 1

    # FastAPI runs a `def` route in a threadpool — exercise that path too.
    result = await asyncio.to_thread(delete_session, MagicMock(), "doomed")
    assert result == {"status": "deleted"}
    manager.delete_session.assert_called_once_with("doomed")

    for _ in range(200):
        await asyncio.sleep(0.01)
        if run.task.done():
            break
    assert run.task.done(), "the run of a deleted chat kept going"
    assert run.status == "stopped"
    assert not agent_runs.is_active("doomed")
    assert "doomed" not in agent_runs.active_session_ids()
    assert lane.active == 0, "the deleted chat kept its queue-lane slot"

    # The lane is genuinely usable again: a queued chat gets in.
    before = len(tool_calls)
    other, _ = await _start_run("other")
    assert other.queued_position == 0
    await asyncio.sleep(0.05)
    assert len(tool_calls) == before, "the stopped run is still calling tools"
    other.task.cancel()
    await asyncio.gather(other.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_delete_session_without_a_run_is_unaffected(routes):
    router, manager = routes
    delete_session = _route(router, "/api/session/{sid}", "DELETE")
    assert delete_session(MagicMock(), "quiet") == {"status": "deleted"}
    manager.delete_session.assert_called_once_with("quiet")


# ── POST /api/sessions/bulk-delete ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_delete_stops_every_run(routes):
    router, manager = routes
    bulk = _route(router, "/api/sessions/bulk-delete", "POST")
    r1, _ = await _start_run("a")
    r2, _ = await _start_run("b", lane=None)

    request = MagicMock()

    async def _json():
        return {"ids": ["a", "b"]}
    request.json = _json
    assert (await bulk(request))["deleted"] == 2

    for _ in range(200):
        await asyncio.sleep(0.01)
        if r1.task.done() and r2.task.done():
            break
    assert r1.task.done() and r2.task.done()
    assert agent_runs.active_session_ids() == []
    assert agent_runs._LANES["local"].active == 0


# ── DELETE /api/sessions/all ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_all_sessions_stops_every_run(routes, monkeypatch):
    import routes.session_routes as sr
    router, manager = routes
    delete_all = _route(router, "/api/sessions/all", "DELETE")
    monkeypatch.setitem(sys.modules, "core.middleware",
                        types.SimpleNamespace(require_admin=lambda request: None))
    run, _ = await _start_run("everything")
    agent_runs.mark_busy("a-subagent-worker")

    db = MagicMock()
    db.query.return_value.all.return_value = []
    db.query.return_value.count.return_value = 0
    db.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr(sr, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(sr, "session_image_refs", lambda db, sid: (set(), set()))

    delete_all(MagicMock())

    for _ in range(200):
        await asyncio.sleep(0.01)
        if run.task.done():
            break
    assert run.task.done() and run.status == "stopped"
    assert agent_runs.active_session_ids() == [], "sub-agent workers stayed marked busy"


# ── stop() must stay fail-closed ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_still_requires_the_run_id():
    run, _ = await _start_run("s", lane=None)
    assert agent_runs.stop("s", None) is False
    assert agent_runs.stop("s", "not-the-right-id") is False
    assert not run.task.done()
    assert agent_runs.stop("s", run.run_id) is True
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_for_session_reports_nothing_to_stop():
    assert agent_runs.stop_for_session("never-existed") is False
    agent_runs.mark_busy("worker")
    assert agent_runs.stop_for_session("worker") is True
    assert agent_runs.stop_for_session("worker") is False
