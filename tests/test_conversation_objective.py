import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import FastAPI
import httpx
import pytest

from src import conversation_objective as objectives


def brief(statement="Make the export", **extra):
    return {"statement": statement, "context": "Use the two saved sources", "deliverable": "report.pdf", "refs": [], **extra}


def test_restart_preserves_identity_brief_native_links_and_old_run_revision(tmp_path):
    path = tmp_path / "objectives.db"
    first = objectives.ObjectiveStore(path)
    saved = first.save("alice", "chat", 0, brief(refs=[{"kind": "task", "project_id": "p", "id": "native-task-7"}, {"kind": "schedule", "id": "native-schedule-8"}]))
    first.link_run("alice", "chat", "a" * 32)
    first.save("alice", "chat", 1, brief("Make the revised export"))
    restored = objectives.ObjectiveStore(path).get("alice", "chat")
    assert restored["id"] == saved["id"]
    assert restored["statement"] == "Make the revised export"
    assert restored["runs"][0]["revision"] == 1
    assert restored["runs"][0]["brief"]["statement"] == "Make the export"
    assert restored["runs"][0]["brief"]["refs"][0]["id"] == "native-task-7"
    assert first.get("bob", "chat") is None


def test_cas_is_transactional_across_independent_connections(tmp_path):
    path = tmp_path / "objectives.db"
    objectives.ObjectiveStore(path).save("alice", "chat", 0, brief())
    gate = threading.Barrier(2)
    def edit(statement):
        store = objectives.ObjectiveStore(path)
        gate.wait()
        try:
            return store.save("alice", "chat", 1, brief(statement))["statement"]
        except objectives.RevisionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["first edit", "second edit"]))
    assert results.count("conflict") == 1
    saved = objectives.ObjectiveStore(path).get("alice", "chat")
    assert saved["revision"] == 2 and saved["statement"] in results


def test_pause_time_survives_restart_and_is_not_generation_completion(tmp_path):
    now = [100.0]
    path = tmp_path / "objectives.db"
    store = objectives.ObjectiveStore(path, clock=lambda: now[0])
    store.save("alice", "chat", 0, brief())
    now[0] = 160
    paused = store.transition("alice", "chat", 1, "paused")
    now[0] = 1000
    restarted = objectives.ObjectiveStore(path, clock=lambda: now[0])
    assert restarted.get("alice", "chat")["active_seconds"] == 60
    assert paused["status"] == "paused"
    resumed = restarted.reserve_continuation("alice", "chat", 2)
    now[0] = 1030
    assert restarted.get("alice", "chat")["active_seconds"] == 90
    # A run link is execution evidence, not authority to complete the objective.
    restarted.link_run("alice", "chat", "b" * 32)
    assert restarted.get("alice", "chat")["status"] == "active"
    with pytest.raises(ValueError):
        restarted.transition("alice", "chat", resumed["revision"], "complete")


def test_continue_retry_keeps_one_key_but_a_new_explicit_intent_gets_another(tmp_path):
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    store.save("alice", "chat", 0, brief())
    accepted = store.reserve_continuation("alice", "chat", 1)
    duplicate = objectives.ObjectiveStore(store.path).reserve_continuation("alice", "chat", 1)
    assert duplicate["continuation_id"] == accepted["continuation_id"]
    assert duplicate["revision"] == accepted["revision"] == 2
    another = store.reserve_continuation("alice", "chat", 2)
    assert another["continuation_id"] != accepted["continuation_id"]
    with pytest.raises(objectives.RevisionConflict):
        store.reserve_continuation("alice", "chat", 1)


@pytest.mark.parametrize("ref", [
    {"kind": "change", "id": "repo", "workspace": "work", "path": "a.py"},
    {"kind": "change", "id": "repo", "workspace": "work", "path": "a.py", "scope": "all"},
    {"kind": "task", "id": "task-without-project"},
    {"kind": "resource", "path": "a.py"},
])
def test_ambiguous_native_reference_is_rejected(ref, tmp_path):
    with pytest.raises(ValueError):
        objectives.ObjectiveStore(tmp_path / "objectives.db").save("alice", "chat", 0, brief(refs=[ref]))


def test_native_refs_keep_explicit_change_scope_and_distinct_child_ids(tmp_path):
    refs = [{"kind": "change", "id": "repo", "workspace": "work", "path": "a.py", "scope": "staged"},
            {"kind": "agent", "id": "child-a"}, {"kind": "agent", "id": "child-b"}]
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    assert store.save("alice", "projectless-chat", 0, brief(refs=refs))["refs"] == refs


def test_paused_or_complete_objective_does_not_claim_later_unrelated_runs(tmp_path):
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    store.save("alice", "chat", 0, brief())
    store.link_run("alice", "chat", "a" * 32)
    store.transition("alice", "chat", 1, "paused")
    assert store.link_run("alice", "chat", "b" * 32) is None
    store.transition("alice", "chat", 2, "complete", "The result was delivered")
    assert store.link_run("alice", "chat", "c" * 32) is None
    assert [run["run_id"] for run in store.get("alice", "chat")["runs"]] == ["a" * 32]


def test_session_delete_cleans_its_brief_without_deleting_linked_artifact(tmp_path):
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    artifact = tmp_path / "report.pdf"; artifact.write_bytes(b"owned artifact")
    store.save("alice", "chat", 0, brief(refs=[{"kind": "resource", "workspace": str(tmp_path), "path": artifact.name}]))
    store.link_run("alice", "chat", "a" * 32)
    store.save("alice", "other-chat", 0, brief("Other work"))
    store.delete_session("chat")
    assert store.get("alice", "chat") is None
    assert store.get("alice", "other-chat")["statement"] == "Other work"
    assert artifact.read_bytes() == b"owned artifact"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM objective_runs").fetchone()[0] == 0


@pytest.mark.parametrize("shared_route_list", [True, False])
def test_context_retains_latest_user_message_and_does_not_duplicate_aliases(tmp_path, monkeypatch, shared_route_list):
    from types import SimpleNamespace
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    store.save("alice", "chat", 0, brief())
    monkeypatch.setattr(objectives, "get_store", lambda: store)
    latest = {"role": "user", "content": "Latest instruction"}
    messages = [{"role": "system", "content": "Policy"}, latest]
    routes = messages if shared_route_list else [dict(message) for message in messages]
    context = SimpleNamespace(messages=messages, route_messages=routes)
    objectives.inject_context(context, "alice", "chat")
    for values in (context.messages, context.route_messages):
        assert values[-1] == latest
        injected = [message for message in values if 'Saved conversation objective' in message['content']]
        assert len(injected) == 1 and injected[0]['role'] == 'user'
    assert objectives.context_message("bob", "chat") is None


@pytest.mark.asyncio
async def test_resume_with_explicit_run_id_never_attaches_to_its_replacement(monkeypatch):
    from types import SimpleNamespace
    from fastapi import APIRouter
    from routes import chat_routes
    from src import agent_runs
    from tests.test_foreground_model_routing import _chat_stream_endpoint
    routers = []
    def capture(*args, **kwargs):
        router = APIRouter(*args, **kwargs); routers.append(router); return router
    monkeypatch.setattr(chat_routes, "APIRouter", capture)
    _chat_stream_endpoint(monkeypatch, "agent", {})
    monkeypatch.setattr(agent_runs, "get_active_run", lambda sid: SimpleNamespace(run_id="b" * 32))
    async def forbidden_subscribe(*args, **kwargs):
        raise AssertionError("Must not subscribe to the replacement")
        yield ""
    monkeypatch.setattr(agent_runs, "subscribe", forbidden_subscribe)
    app = FastAPI(); app.include_router(routers[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/chat/resume/session-1", params={"run_id": "a" * 32})
        assert result.status_code == 404


@pytest.fixture
def objective_api(tmp_path, monkeypatch):
    from routes import conversation_objective_routes as routes
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    monkeypatch.setattr(objectives, "get_store", lambda: store)
    monkeypatch.setattr(routes, "objective_owner", lambda request, sid: "alice")
    app = FastAPI()
    app.include_router(routes.setup_conversation_objective_routes())
    return app, store


@pytest.mark.asyncio
async def test_http_conflict_keeps_first_edit_and_pause_targets_exact_run(objective_api, monkeypatch):
    from src import agent_runs
    app, store = objective_api
    store.save("alice", "chat", 0, brief())
    calls = []
    monkeypatch.setattr(agent_runs, "run_status_snapshot", lambda *a: {"run_id": "a" * 32, "active": True, "state": "running"})
    monkeypatch.setattr(agent_runs, "request_pause", lambda sid, rid: calls.append((sid, rid)) or True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        paused = await client.post("/api/session/chat/objective/state", json={"expected_revision": 1, "status": "paused"})
        assert paused.status_code == 200 and paused.json()["pause_requested"] is True
        assert paused.json()["schedule_changed"] is False
        stale = await client.put("/api/session/chat/objective", json={"expected_revision": 1, "brief": brief("stale edit")})
        assert stale.status_code == 409
        assert calls == [("chat", "a" * 32)]
        assert store.get("alice", "chat")["statement"] == "Make the export"


@pytest.mark.asyncio
async def test_native_continue_retry_and_active_rejoin_do_not_execute_twice(tmp_path, monkeypatch):
    from fastapi import APIRouter
    from routes import chat_routes, conversation_objective_routes as routes
    from src import agent_runs, chat_outbox
    from tests.test_foreground_model_routing import _chat_stream_endpoint
    store = objectives.ObjectiveStore(tmp_path / "objectives.db")
    store.save("alice", "session-1", 0, brief())
    monkeypatch.setattr(objectives, "get_store", lambda: store)
    monkeypatch.setattr(routes, "objective_owner", lambda request, sid: "alice")
    monkeypatch.setattr(chat_outbox, "default_path", lambda: tmp_path / "outbox.db")
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    captured = {}; routers = []
    def capture_router(*args, **kwargs):
        router = APIRouter(*args, **kwargs); routers.append(router); return router
    monkeypatch.setattr(chat_routes, "APIRouter", capture_router)
    _chat_stream_endpoint(monkeypatch, "agent", captured, agent_chunks=['data: {"type":"chunk","content":"continued"}\n\n', 'data: [DONE]\n\n'])
    generated = []
    release = asyncio.Event()
    original_stream = chat_routes.stream_agent_loop
    async def counted_stream(*args, **kwargs):
        generated.append(True)
        await release.wait()
        async for event in original_stream(*args, **kwargs):
            yield event
    monkeypatch.setattr(chat_routes, "stream_agent_loop", counted_stream)
    app = FastAPI(); app.include_router(routers[-1]); app.include_router(routes.setup_conversation_objective_routes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert first.status_code == 200, first.text
        active_rejoin = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 2})
        assert active_rejoin.status_code == 200 and active_rejoin.json()["rejoined"] is True
        assert active_rejoin.json()["run_id"] == first.json()["run_id"]
        active_retry = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert active_retry.status_code == 200 and active_retry.json()["run_id"] == first.json()["run_id"]
        release.set()
        for _ in range(100):
            if not agent_runs.is_active("session-1"):
                break
            await asyncio.sleep(0.01)
        duplicate = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert duplicate.status_code == 200, duplicate.text
        assert first.json()["client_message_id"] == duplicate.json()["client_message_id"]
        assert first.json()["run_id"] == duplicate.json()["run_id"]
        assert len(store.get("alice", "session-1")["runs"]) == 1
        assert store.get("alice", "session-1")["status"] == "active"
        # The durable objective receipt outlives the ordinary send outbox TTL.
        import sqlite3
        with sqlite3.connect(tmp_path / "outbox.db") as db:
            db.execute("UPDATE outbox SET updated_at='2000-01-01T00:00:00Z'")
        chat_outbox.purge_stale()
        aged_retry = await client.post("/api/session/session-1/objective/continue", json={"expected_revision": 1})
        assert aged_retry.status_code == 200, aged_retry.text
        assert aged_retry.json()["run_id"] == first.json()["run_id"]
        assert len(generated) == 1
    for run in agent_runs._RUNS.values():
        if run.evict_task:
            run.evict_task.cancel()
