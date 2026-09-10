"""Lote 55 item 5 — ACT-05: `GET /api/queue` (routes/queue_routes.py) merges
the four existing queue/run authorities (`src.agent_runs` lanes, `src.bg_jobs`,
`research_handler._active_tasks`, `src.media_runs`) into one owner-scoped
list, and `POST /api/queue/{kind}/{id}/priority` reorders the one of them
that has a real ordered queue behind it.

Two layers, matching how `tests/test_agent_runs_queue_persist.py` already
tests lanes directly and `tests/test_contracts_mcp_and_routes.py` tests a
router mounted alone:
  1. `src.agent_runs.prioritize_run` / `_Lane.prioritize` against the lane
     machinery directly — no HTTP needed to prove the reorder itself works.
  2. `routes/queue_routes.py` over TestClient — merging, owner-scoping, and
     the "not an ordered queue" refusal for the other three kinds.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import agent_runs
import routes.queue_routes as queue_routes
from routes.queue_routes import setup_queue_routes


# ── 1. src.agent_runs.prioritize_run / _Lane.prioritize ────────────────────

@pytest.fixture(autouse=True)
def _isolated_lanes():
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


def _settings(monkeypatch, **values):
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: values.get(key, default), raising=False)


async def _gen(events, gate: asyncio.Event = None):
    if gate is not None:
        await gate.wait()
    for ev in events:
        yield ev


@pytest.mark.asyncio
async def test_prioritize_run_moves_a_waiting_run_to_the_front(monkeypatch):
    _settings(monkeypatch, agent_queue_local_concurrency=1)
    gate = asyncio.Event()
    r1 = agent_runs.start("s1", _gen(["data: [DONE]\n\n"], gate), lane="local", label="Chat 1")
    await asyncio.sleep(0.05)
    r2 = agent_runs.start("s2", _gen(["data: [DONE]\n\n"]), lane="local", label="Chat 2")
    r3 = agent_runs.start("s3", _gen(["data: [DONE]\n\n"]), lane="local", label="Chat 3")
    await asyncio.sleep(0.05)
    assert agent_runs.queued_positions() == {"s2": 1, "s3": 2}

    moved = await agent_runs.prioritize_run(r3.run_id)
    assert moved is True
    assert agent_runs.queued_positions() == {"s2": 2, "s3": 1}   # r3 jumped ahead of r2

    gate.set()
    await asyncio.wait_for(r1.task, 5)
    await asyncio.wait_for(r3.task, 5)   # r3 now admitted before r2, since it is first in line
    assert r3.status == "done"
    await asyncio.wait_for(r2.task, 5)


@pytest.mark.asyncio
async def test_prioritize_run_is_a_clean_no_op_for_a_run_not_waiting(monkeypatch):
    _settings(monkeypatch, agent_queue_local_concurrency=0)   # unlimited: nothing ever waits
    r1 = agent_runs.start("only", _gen(["data: [DONE]\n\n"]), lane="local")
    await asyncio.wait_for(r1.task, 5)
    assert await agent_runs.prioritize_run(r1.run_id) is False
    assert await agent_runs.prioritize_run("no-such-run-id") is False


# ── 2. routes/queue_routes.py over TestClient ───────────────────────────────

class _FakeResearchHandler:
    def __init__(self, tasks):
        self._active_tasks = tasks


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(queue_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(queue_routes, "effective_user", lambda request: "alice")
    # Ownership: alice owns s1/s2, not s9 — exercises the scoping without
    # standing up a real SessionManager/DB for this router-only test.
    monkeypatch.setattr(queue_routes, "_owned_session",
                        lambda request, sid, session_manager: sid != "s9")

    research_handler = _FakeResearchHandler({
        "r1": {"status": "running", "owner": "alice", "query": "market sizing", "started_at": time.time() - 5},
        "r2": {"status": "running", "owner": "bob", "query": "not alice's", "started_at": time.time()},
    })
    app = FastAPI()
    app.include_router(setup_queue_routes(research_handler, session_manager=object()))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_bg_and_media(monkeypatch):
    from src import bg_jobs, media_runs

    monkeypatch.setattr(bg_jobs, "refresh", lambda: {
        "job1": {"id": "job1", "session_id": "s1", "command": "pip install requests",
                 "status": "running", "started_at": time.time() - 3},
        "job2": {"id": "job2", "session_id": "s9", "command": "not alice's",
                 "status": "running", "started_at": time.time()},
        "job3": {"id": "job3", "session_id": "s1", "command": "done already",
                 "status": "done", "started_at": time.time() - 30},
    })
    monkeypatch.setattr(media_runs, "recent", lambda owner="", limit=100: [
        {"id": "m1", "status": "queued", "session_id": "s1", "workflow": "sdxl.image",
         "engine_job_id": "job-xyz", "engine_url": "", "started_at": time.time() - 1},
        {"id": "m2", "status": "completed", "session_id": "s1", "workflow": "sdxl.image",
         "engine_job_id": "", "engine_url": "", "started_at": time.time() - 90},
    ] if owner == "alice" else [])

    async def _no_live_position(row):
        return None   # engine unreachable in this test — the honest default

    monkeypatch.setattr(queue_routes, "_media_position", _no_live_position)


@pytest.mark.asyncio
async def test_get_queue_merges_four_sources_and_scopes_by_owner(client, monkeypatch):
    _settings_module = agent_runs
    monkeypatch.setattr(_settings_module, "_setting", lambda key, default=None: {"agent_queue_local_concurrency": 1}.get(key, default), raising=False)
    r1 = agent_runs.start("s1", _gen(["data: [DONE]\n\n"], asyncio.Event()), lane="local", label="Chat 1")
    r2 = agent_runs.start("s2", _gen(["data: [DONE]\n\n"]), lane="local", label="Chat 2")
    await asyncio.sleep(0.05)
    try:
        response = client.get("/api/queue")
        assert response.status_code == 200
        body = response.json()
        by_kind = {}
        for item in body["items"]:
            by_kind.setdefault(item["kind"], []).append(item)

        agent_ids = {it["id"] for it in by_kind.get("agent_run", [])}
        assert r1.run_id in agent_ids and r2.run_id in agent_ids

        bg_ids = {it["id"] for it in by_kind.get("bg_job", [])}
        assert bg_ids == {"job1"}   # job2 belongs to s9 (not alice's), job3 is not running/queued

        research_ids = {it["id"] for it in by_kind.get("research", [])}
        assert research_ids == {"r1"}   # r2 belongs to bob

        media_ids = {it["id"] for it in by_kind.get("media_run", [])}
        assert media_ids == {"m1"}   # m2 is completed, filtered out

        m1 = next(it for it in by_kind["media_run"] if it["id"] == "m1")
        assert m1["position"] is None   # engine unreachable in this test — honest, not fabricated
        assert m1["reorderable"] is False
    finally:
        r1.task.cancel()
        try:
            await asyncio.wait_for(r2.task, 5)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_priority_route_reorders_the_agent_run_queue(client, monkeypatch):
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: {"agent_queue_local_concurrency": 1}.get(key, default), raising=False)
    gate = asyncio.Event()
    r1 = agent_runs.start("s1", _gen(["data: [DONE]\n\n"], gate), lane="local")
    r2 = agent_runs.start("s2", _gen(["data: [DONE]\n\n"]), lane="local")
    await asyncio.sleep(0.05)
    assert agent_runs.queued_positions() == {"s2": 1}

    response = client.post(f"/api/queue/agent_run/{r2.run_id}/priority")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": r2.run_id, "position": 1}

    gate.set()
    await asyncio.wait_for(r1.task, 5)
    await asyncio.wait_for(r2.task, 5)


def test_priority_route_refuses_kinds_with_no_ordered_queue(client):
    for kind in ("bg_job", "research", "media_run"):
        response = client.post(f"/api/queue/{kind}/whatever/priority")
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "no_ordered_queue"


def test_priority_route_404s_an_unknown_kind(client):
    response = client.post("/api/queue/not-a-real-kind/whatever/priority")
    assert response.status_code == 404


def test_priority_route_refuses_a_run_that_is_not_this_owners(client, monkeypatch):
    # _owned_session is stubbed to deny s9 — a run on s9 must not be reorderable
    # through this endpoint even if the id happens to be right.
    monkeypatch.setattr(queue_routes.agent_runs if hasattr(queue_routes, "agent_runs") else agent_runs,
                        "active_session_ids", lambda: ["s9"])
    monkeypatch.setattr(agent_runs, "get_run_id", lambda sid: "some-run-id" if sid == "s9" else None)
    response = client.post("/api/queue/agent_run/some-run-id/priority")
    assert response.status_code == 404
