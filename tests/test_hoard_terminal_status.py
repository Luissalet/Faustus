"""The existing copilot can observe a finished run without starting it again."""
import asyncio
import json

import pytest

from src import agent_runs, constants


CAPACITY = {
    "error": "The request is longer than the model's context window, even after compacting the conversation. Start a new chat, shorten the input, or use a model with a larger window.",
    "status": 400, "error_class": "context_length_exceeded", "retryable": False,
    "request_preparation": True, "fallback_eligible": True,
    "provider_error": "The intact current request does not fit the model context window after output and tool reserves (estimated required=1052441, available=1044456).",
}


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_runs, "_ledger_run_event", lambda *args: None)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


async def finish(sid="owned", error=False):
    async def events():
        if error:
            yield "event: error\ndata: " + json.dumps(CAPACITY) + "\n\n"
        else:
            yield 'data: {"delta":"CPU result"}\n\n'
        yield "data: [DONE]\n\n"
    run = agent_runs.start(sid, events())
    await run.task
    return run


def error_payload(run):
    return next(json.loads(event.split("data:", 1)[1]) for event in run.buffer if event.startswith("event: error"))


def test_running_status_and_old_marker_cannot_mask_the_current_run():
    old = asyncio.run(finish())
    current = agent_runs._Run()
    agent_runs._RUNS["owned"] = current
    status = agent_runs.run_status_snapshot("owned")
    assert status["run_id"] == current.run_id != old.run_id
    assert status["status"] == "streaming" and status["state"] == "running" and status["active"]
    assert "error" not in status


@pytest.mark.parametrize("error", [False, True])
def test_terminal_state_and_exact_error_survive_eviction_and_restart(error, monkeypatch):
    async def run_it():
        monkeypatch.setattr(agent_runs, "_EVICT_GRACE_S", .01)
        run = await finish(error=error)
        status = agent_runs.run_status_snapshot("owned", run.run_id)
        assert status["status"] == ("error" if error else "done")
        if error:
            assert status["error"] == error_payload(run)
            assert status["error"]["provider_error"] == CAPACITY["provider_error"]
        await asyncio.sleep(.03)
        assert "owned" not in agent_runs._RUNS
        return run, status
    run, live = asyncio.run(run_it())
    restored = agent_runs.run_status_snapshot("owned", run.run_id)
    assert restored["source"] == "finished_marker" and restored["status"] == live["status"]
    assert restored.get("error") == live.get("error")
    assert not restored["active"]


def test_old_marker_reads_the_existing_exact_error_journal_without_writing(tmp_path):
    run = asyncio.run(finish(error=True))
    from pathlib import Path
    path = Path(agent_runs._finished_marker_path())
    markers = json.loads(path.read_text())
    del markers["owned"]["error"]  # Old product version's marker format.
    path.write_text(json.dumps(markers))
    agent_runs._RUNS.clear()
    before = {file: file.read_bytes() for file in path.parent.rglob("*") if file.is_file()}
    status = agent_runs.run_status_snapshot("owned", run.run_id)
    assert status["source"] == "journal" and status["error"] == error_payload(run)
    assert all(file.read_bytes() == content for file, content in before.items())
    assert set(before) == {file for file in path.parent.rglob("*") if file.is_file()}


def test_exact_older_run_never_returns_the_newer_run_or_another_session():
    old = asyncio.run(finish(error=True))
    new = asyncio.run(finish())
    agent_runs._RUNS.clear()
    assert agent_runs.run_status_snapshot("owned")["run_id"] == new.run_id
    exact = agent_runs.run_status_snapshot("owned", old.run_id)
    assert exact["run_id"] == old.run_id and exact["status"] == "error"
    assert exact["error"] == error_payload(old)
    assert agent_runs.run_status_snapshot("other", old.run_id) is None
    assert agent_runs.run_status_snapshot("owned", "0" * 32) is None
    assert agent_runs.run_status_snapshot("missing") is None
    with pytest.raises(ValueError):
        agent_runs.run_status_snapshot("owned", "../foreign")


@pytest.mark.parametrize("legacy", [False, True])
def test_later_marker_with_persistence_disabled_wins_over_older_journal(monkeypatch, legacy):
    old = asyncio.run(finish())
    if legacy:
        from pathlib import Path
        Path(agent_runs._session_log_paths("owned")[0]).rename(agent_runs._legacy_log_path("owned"))
    monkeypatch.setattr(agent_runs, "persistence_enabled", lambda: False)
    new = asyncio.run(finish(error=True))
    agent_runs._RUNS.clear()
    status = agent_runs.run_status_snapshot("owned")
    assert status["run_id"] == new.run_id != old.run_id
    assert status["status"] == "error" and status["error"] == error_payload(new)
    assert agent_runs.run_status_snapshot("owned", new.run_id)["run_id"] == new.run_id


def test_stop_retains_cancellation_and_stale_stop_identity():
    async def run_it():
        entered = asyncio.Event()
        async def events():
            entered.set()
            await asyncio.Future()
            yield  # pragma: no cover
        run = agent_runs.start("owned", events())
        await entered.wait()
        assert not agent_runs.stop("owned", "0" * 32)
        assert agent_runs.stop("owned", run.run_id)
        await run.task
        return run
    run = asyncio.run(run_it())
    status = agent_runs.run_status_snapshot("owned", run.run_id)
    assert status["status"] == "stopped" and not status["active"] and "error" not in status
    agent_runs._RUNS.clear()
    assert agent_runs.run_status_snapshot("owned", run.run_id)["status"] == "stopped"


def test_unfinished_newer_journal_never_claims_live_work_or_the_prior_success():
    old = asyncio.run(finish())
    current = agent_runs._Run()
    log = agent_runs._RunLog("owned", current)
    log.orphan()
    agent_runs._RUNS.clear()
    status = agent_runs.run_status_snapshot("owned")
    assert status["run_id"] == current.run_id != old.run_id
    assert status["status"] == "unknown" and not status["active"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database, session_manager as managers
    from core.session_manager import SessionManager
    from routes import hoard_copilot_routes, session_routes
    engine = create_engine("sqlite:///" + str(tmp_path / "canonical.db"))
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(managers, "SessionLocal", factory)
    monkeypatch.setattr(session_routes, "SessionLocal", factory)
    monkeypatch.setattr(session_routes, "effective_user", lambda request: request.headers.get("x-test-owner"))
    monkeypatch.setattr(session_routes, "_auth_disabled", lambda: False)
    def no_admin_gate(*args):
        raise AssertionError("The existing user-owned status contract is not admin-only")
    monkeypatch.setattr(hoard_copilot_routes, "require_admin", no_admin_gate)
    manager = SessionManager()
    manager.create_session(session_id="owned", name="CPU", model="CPU", endpoint_url="http://127.0.0.1:9/v1", owner="alice")
    app = FastAPI()
    app.include_router(hoard_copilot_routes.setup_hoard_copilot_routes(manager))
    with TestClient(app) as client:
        yield client


def test_api_terminal_error_owner_unknown_and_exact_run(api):
    run = asyncio.run(finish(error=True))
    url = "/api/hoard-copilot/turns/owned/status"
    response = api.get(url, params={"run_id": run.run_id}, headers={"x-test-owner": "alice"})
    assert response.status_code == 200 and response.json()["error"] == error_payload(run)
    assert api.get(url, headers={"x-test-owner": "bob"}).status_code == 404
    assert api.get(url).status_code == 401
    assert api.get(url, params={"run_id": "0" * 32}, headers={"x-test-owner": "alice"}).status_code == 404
    assert api.get(url, params={"run_id": "bad"}, headers={"x-test-owner": "alice"}).status_code == 400
    assert api.get("/api/hoard-copilot/turns/missing/status", headers={"x-test-owner": "alice"}).status_code == 404


def test_api_preserves_active_partial_and_does_not_mix_replacement(api, monkeypatch):
    from routes import chat_routes
    run = agent_runs._Run()
    agent_runs._RUNS["owned"] = run
    monkeypatch.setitem(chat_routes._active_streams, "owned", {"run_id": run.run_id, "status": "streaming", "partial": "existing response", "query": "original query", "mode": "agent"})
    url = "/api/hoard-copilot/turns/owned/status"
    response = api.get(url, headers={"x-test-owner": "alice"}).json()
    assert response["partial"] == "existing response" and response["query"] == "original query"
    assert response["status"] == "streaming" and response["run_id"] == run.run_id
    ids = iter([run.run_id, "0" * 32])
    monkeypatch.setattr(agent_runs, "get_run_id", lambda sid: next(ids))
    replaced = api.get(url, headers={"x-test-owner": "alice"}).json()
    assert replaced["run_id"] == run.run_id and "partial" not in replaced and "query" not in replaced


def native_stream_registration(chat_routes):
    """Use the real nested registration statements under a native run context."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(chat_routes))
    stream = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_with_save")
    statements = []
    for statement in stream.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "src.run_causality":
            statements.append(statement)
        if (isinstance(statement, ast.Assign)
                and isinstance(statement.targets[0], ast.Subscript)
                and isinstance(statement.targets[0].value, ast.Name)
                and statement.targets[0].value.id == "_active_streams"):
            statements.append(statement)
            break
    assert statements and isinstance(statements[-1], ast.Assign)
    module = ast.parse("def register(session, message, effective_do_research=False, _effective_mode='agent'): pass")
    module.body[0].body = statements
    namespace = {"_active_streams": chat_routes._active_streams}
    exec(compile(ast.fix_missing_locations(module), chat_routes.__file__, "exec"), namespace)
    return namespace["register"]


@pytest.mark.parametrize("queued", [False, True])
def test_actual_replacement_and_queue_never_attach_the_predecessor_partial(api, monkeypatch, queued):
    from routes import chat_routes
    from src.run_causality import capture_call
    register = native_stream_registration(chat_routes)
    monkeypatch.setitem(chat_routes._active_streams, "owned", {})
    url = "/api/hoard-copilot/turns/owned/status"

    def read():
        response = api.get(url, headers={"x-test-owner": "alice"})
        assert response.status_code == 200
        return response.json()

    async def run_it():
        entered, cleaning, release_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
        new_entered, release_new = asyncio.Event(), asyncio.Event()

        async def predecessor():
            register("owned", "predecessor query")
            chat_routes._active_streams["owned"]["partial"] = "predecessor partial"
            entered.set()
            try:
                yield 'data: {"delta":"predecessor partial"}\n\n'
                await asyncio.Future()
            finally:
                assert capture_call("owned", None).run_id == old.run_id
                cleaning.set()
                await release_old.wait()
                chat_routes._active_streams.pop("owned", None)

        async def replacement():
            register("owned", "replacement query")
            chat_routes._active_streams["owned"]["partial"] = "replacement partial"
            new_entered.set()
            yield 'data: {"delta":"replacement partial"}\n\n'
            await release_new.wait()
            chat_routes._active_streams.pop("owned", None)

        old = agent_runs.start("owned", predecessor())
        await entered.wait()
        lane = agent_runs._lane("local") if queued else None
        if lane is not None:
            lane.active = lane.limit
        new = agent_runs.start("owned", replacement(), lane="local" if queued else None)
        try:
            await cleaning.wait()
            assert agent_runs.get_run_id("owned") == new.run_id
            assert chat_routes._active_streams["owned"]["run_id"] == old.run_id
            status = read()
            assert status["run_id"] == new.run_id and status["active"]
            assert "partial" not in status and "query" not in status
            release_old.set()
            await old.task
            if lane is not None:
                await asyncio.sleep(0)
                assert not new_entered.is_set()
                status = read()
                assert status["run_id"] == new.run_id and "partial" not in status and "query" not in status
                await lane.release(new)
            await new_entered.wait()
            status = read()
            assert status["run_id"] == new.run_id
            assert status["partial"] == "replacement partial" and status["query"] == "replacement query"
        finally:
            release_old.set()
            release_new.set()
            if lane is not None and lane.active:
                await lane.release(new)
            await asyncio.gather(old.task, new.task)

    asyncio.run(run_it())


def test_mcp_status_uses_native_copilot_get_without_starting_or_replaying(monkeypatch):
    from mcp_servers import hoard_copilot_server as mcp
    import httpx
    calls = []
    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(200, json={"status": "error", "error": CAPACITY}, request=httpx.Request(method, url))
    monkeypatch.setattr(mcp.httpx, "AsyncClient", Client)
    monkeypatch.setenv("FAUSTUS_URL", "http://127.0.0.1:64214")
    monkeypatch.setenv("FAUSTUS_TOKEN", "")
    monkeypatch.setenv("FAUSTUS_TOKEN_FILE", "")
    result = asyncio.run(mcp.call_tool("hoard_turn_status", {"session_id": "owned", "run_id": "a" * 32}))
    assert json.loads(result[0].text)["error"] == CAPACITY
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert calls[0][1].endswith("/api/hoard-copilot/turns/owned/status")
    assert calls[0][2]["params"] == {"run_id": "a" * 32}
    assert mcp.OPERATIONS["hoard_turn_start"][:2] == ("POST", "/api/chat_stream")
    assert mcp.OPERATIONS["hoard_turn_stop"][:2] == ("POST", "/api/chat/stop/{session_id}")


@pytest.mark.parametrize("case", ["explicit_stale", "compat", "missing", "no_identity", "finished_work", "missing_work"])
def test_mcp_stop_passes_one_native_identity_and_preserves_the_native_result(monkeypatch, case):
    from mcp_servers import hoard_copilot_server as mcp
    import httpx
    calls = []
    observed = {"run_id": "b" * 32, "status": "streaming", "active": True}
    native = {"stopped": False, "reason": "run_id_mismatch"}
    if case in ("missing", "no_identity", "missing_work"):
        observed = {}
        native = {"stopped": False, "reason": "no_run_id_supplied"}
    if case in ("finished_work", "missing_work"):
        if case == "finished_work":
            observed.update(status="done", active=False)
        native = {"scope": "work", "stopped": False, "reason": "no_active_run",
                  "cleanup": {"bg_jobs_cancelled": ["owned-job"], "process_handles_stopped": ["owned-process"]}}

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(404 if method == "GET" and case in ("missing", "missing_work") else 200,
                                  json=observed if method == "GET" else native,
                                  request=httpx.Request(method, url))

    monkeypatch.setattr(mcp.httpx, "AsyncClient", Client)
    monkeypatch.setenv("FAUSTUS_URL", "http://127.0.0.1:64214")
    monkeypatch.setenv("FAUSTUS_TOKEN", "")
    monkeypatch.setenv("FAUSTUS_TOKEN_FILE", "")
    arguments = {"session_id": "owned", "scope": "work" if case.endswith("_work") else "task"}
    if case == "explicit_stale":
        arguments["run_id"] = "a" * 32
    result = asyncio.run(mcp.call_tool("hoard_turn_stop", arguments))
    assert json.loads(result[0].text) == native
    assert [method for method, _, _ in calls] == (["POST"] if case == "explicit_stale" else ["GET", "POST"])
    post = calls[-1]
    assert post[1] == "http://127.0.0.1:64214/api/chat/stop/owned"
    assert post[2]["json"] == {"scope": arguments["scope"]}
    expected = "a" * 32 if case == "explicit_stale" else observed.get("run_id")
    assert post[2]["headers"].get("X-Odysseus-Run-Id") == expected
    if case != "explicit_stale":
        assert calls[0][1] == "http://127.0.0.1:64214/api/hoard-copilot/turns/owned/status"
    assert "run_id" in mcp.OPERATIONS["hoard_turn_stop"][3]
