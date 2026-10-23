"""Native admission races: receipt identity and the brief actually injected."""
import asyncio

from fastapi import FastAPI, APIRouter
import httpx
import pytest

from src import conversation_objective as objectives, agent_runs, chat_outbox
from routes import chat_routes, conversation_objective_routes as routes
from tests.test_foreground_model_routing import _chat_stream_endpoint


@pytest.fixture
def admission(tmp_path, monkeypatch):
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    store.save("alice", "session-1", 0, {"statement": "Original intent"})
    monkeypatch.setattr(objectives, "get_store", lambda: store)
    monkeypatch.setattr(routes, "objective_owner", lambda req, sid: "alice")
    monkeypatch.setattr(chat_outbox, "default_path", lambda: tmp_path / "outbox.db")
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    routers = []
    def capture(*args, **kwargs):
        router = APIRouter(*args, **kwargs); routers.append(router); return router
    monkeypatch.setattr(chat_routes, "APIRouter", capture)
    _chat_stream_endpoint(monkeypatch, "agent", {}, agent_chunks=['data: [DONE]\n\n'])
    app = FastAPI(); app.include_router(routers[-1]); app.include_router(routes.setup_conversation_objective_routes())
    yield app, store
    for run in agent_runs._RUNS.values():
        if run.evict_task: run.evict_task.cancel()


@pytest.mark.asyncio
async def test_repeat_while_preparing_never_returns_predecessor_or_dispatches_another(admission, monkeypatch):
    app, store = admission
    async def old_stream():
        yield 'data: [DONE]\n\n'
    old = agent_runs.start("session-1", old_stream()); await old.task
    original = chat_routes.build_chat_context
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated(*args, **kwargs):
        entered.set(); await release.wait(); return await original(*args, **kwargs)
    monkeypatch.setattr(chat_routes, "build_chat_context", gated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1}))
        await asyncio.wait_for(entered.wait(), 5)
        for revision in (1, 2):
            duplicate = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": revision})
            assert duplicate.status_code == 202 and duplicate.json()["pending"]
            assert duplicate.json()["run_id"] is None
        release.set(); first = await pending
        retry = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert first.status_code == retry.status_code == 200
        assert first.json()["run_id"] == retry.json()["run_id"] != old.run_id
        assert first.json()["client_message_id"] == duplicate.json()["client_message_id"]
        assert len(store.get("alice", "session-1")["runs"]) == 1
    if old.evict_task: old.evict_task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "paused", "complete"])
async def test_brief_captured_before_admission_matches_run_despite_later_changes(admission, monkeypatch, change):
    app, store = admission
    original = chat_routes.run_preparation
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated(function, *args, **kwargs):
        if function.__name__ == "_resolve_think_mode":
            entered.set(); await release.wait()
        return await original(function, *args, **kwargs)
    monkeypatch.setattr(chat_routes, "run_preparation", gated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1}))
        await asyncio.wait_for(entered.wait(), 5)
        if change == "edit":
            response = await client.put("/api/session/session-1/objective", json={"expected_revision": 2, "brief": {"statement": "Future intent"}})
        else:
            response = await client.post("/api/session/session-1/objective/state", json={"expected_revision": 2, "status": change, "result": "Explicit delivered result"})
        assert response.status_code == 200
        release.set(); result = await pending
        assert result.status_code == 200, result.text
        binding = store.get("alice", "session-1")["runs"][0]
        assert binding["revision"] == 2 and binding["brief"]["statement"] == "Original intent"
        retry = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert retry.status_code == 200 and retry.json()["run_id"] == result.json()["run_id"]
        if change != "edit": assert store.get("alice", "session-1")["status"] == change


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "complete"])
async def test_state_changed_before_context_keeps_accepted_receipt_after_outbox_expiry(admission, monkeypatch, status):
    app, store = admission
    original = chat_routes.build_chat_context
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated(*args, **kwargs):
        entered.set(); await release.wait(); return await original(*args, **kwargs)
    monkeypatch.setattr(chat_routes, "build_chat_context", gated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1}))
        await asyncio.wait_for(entered.wait(), 5)
        changed = await client.post("/api/session/session-1/objective/state", json={"expected_revision": 2, "status": status, "result": "Explicit delivered result"})
        assert changed.status_code == 200
        release.set(); result = await pending
        assert result.status_code == 200 and result.json()["run_id"]
        for _ in range(100):
            if not agent_runs.is_active("session-1"): break
            await asyncio.sleep(.01)
        binding = store.get("alice", "session-1")["runs"][0]
        assert binding["brief"]["status"] == status and binding["revision"] == 3
        import sqlite3
        with sqlite3.connect(chat_outbox.default_path()) as db:
            db.execute("UPDATE outbox SET updated_at='2000-01-01T00:00:00Z'")
        chat_outbox.purge_stale()
        assert chat_outbox.get(owner="alice",session_id="session-1",client_message_id=result.json()["client_message_id"]) is None
        retry = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert retry.status_code == 200 and retry.json()["run_id"] == result.json()["run_id"]
        assert store.get("alice", "session-1")["status"] == status
