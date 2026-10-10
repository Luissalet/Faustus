"""Assign a todo to an agent (src/todo_agent.py, routes/todo_agent_routes.py).

No model is called: the session factory and the turn runner are replaced, so
what is pinned is the contract around them -- the prompt, the background run,
the two-way link, idempotency, autonomy never widened, and that a todo is
never reported done without a completed run.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

import core.database as cdb
import routes.todo_agent_routes as tar
from src import todo_agent as ta


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'notes.db'}", connect_args={"check_same_thread": False})
    cdb.Note.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(tar, "SessionLocal", factory)
    monkeypatch.setattr(tar, "require_user", lambda request: request.headers.get("x-user") or None)
    ta._LIVE.clear()

    state = SimpleNamespace(sessions=[], turns=[], gate=None, outcome=None, project=None)

    def fake_create(owner, title, model, project):
        state.sessions.append({"owner": owner, "title": title, "project": project})
        return f"sess-{len(state.sessions)}"

    async def fake_turn(request):
        state.turns.append(request)
        if state.gate is not None:
            await state.gate.wait()
        if isinstance(state.outcome, Exception):
            raise state.outcome
        return state.outcome or SimpleNamespace(text="Done: wrote the file.", error="", approvals=[],
                                                stop_reason="", tool_calls=3)

    class Store:
        def get(self, project_id, owner=None):
            return state.project if state.project and state.project["id"] == project_id else None

    monkeypatch.setattr(ta, "_create_session", fake_create)
    monkeypatch.setattr(ta, "_run_turn", fake_turn)
    import services.projects as sp
    monkeypatch.setattr(sp, "get_store", lambda: Store())

    def add_note(**kw):
        db = factory()
        try:
            note = cdb.Note(id=kw.pop("id", "n1"), owner=kw.pop("owner", "alice"),
                            title=kw.pop("title", "Rename the config key"),
                            content=kw.pop("content", "See https://example.org/spec for details"),
                            note_type="todo", items=json.dumps(kw.pop("items", [{"text": "update docs", "done": False},
                                                                              {"text": "old", "done": True}])), **kw)
            db.add(note)
            db.commit()
        finally:
            db.close()

    def read_note(note_id="n1"):
        db = factory()
        try:
            return db.query(cdb.Note).filter(cdb.Note.id == note_id).first()
        finally:
            db.close()

    app = FastAPI()
    app.include_router(tar.setup_todo_agent_routes())
    state.add_note, state.read_note, state.app = add_note, read_note, app
    return state


def _wait(client, note_id="n1", user="alice", timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/todos/{note_id}/agent", headers={"x-user": user}).json()["run"]
        if run and run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError("the run never finished")


def _post(client, body=None, note_id="n1", user="alice"):
    return client.post(f"/api/todos/{note_id}/assign-agent", json=body or {"mode": "agent"}, headers={"x-user": user})


def test_assign_runs_in_background_and_links_both_ways(env):
    env.add_note()
    with TestClient(env.app) as client:
        res = _post(client)
        assert res.status_code == 200
        run = res.json()["run"]
        assert run["session_id"] == "sess-1" and run["already_running"] is False
        done = _wait(client)
    assert done["status"] == "done" and "wrote the file" in done["summary"]
    note = env.read_note()
    assert note.agent_session_id == "sess-1"
    assert json.loads(note.agent_run)["status"] == "done"
    # the task prompt carries the todo, its pending item (not the done one) and its link
    prompt = env.turns[0].text
    assert "Rename the config key" in prompt and "- update docs" in prompt and "- old" not in prompt
    assert "https://example.org/spec" in prompt
    assert env.turns[0].source == "todo" and env.turns[0].mode == "agent"
    # the todo itself is never ticked off by the run
    assert json.loads(note.items)[0]["done"] is False


def test_assign_is_idempotent_while_running(env):
    env.add_note()
    with TestClient(env.app) as client:
        env.gate = None  # created inside the loop below
        import anyio

        async def gate():
            return asyncio.Event()
        env.gate = client.portal.call(gate)
        first = _post(client).json()["run"]
        second = _post(client).json()["run"]
        assert second["already_running"] is True and second["run_id"] == first["run_id"]
        assert len(env.sessions) == 1 and len(env.turns) <= 1
        client.portal.call(lambda: env.gate.set())
        _wait(client)
        # once finished, a new assignment is a new run
        env.gate = None
        third = _post(client).json()["run"]
        assert third["already_running"] is False and third["run_id"] != first["run_id"]
        _wait(client)
    assert len(env.sessions) == 2


def test_autonomy_is_never_widened(env):
    env.add_note()
    # no project: the baseline is supervised, a request for more is clamped
    with TestClient(env.app) as client:
        run = _post(client, {"mode": "agent", "autonomy_preset": "bounded_autonomous"}).json()["run"]
        _wait(client)
    assert run["autonomy_preset"] == "supervised" and run["autonomy_clamped"] is True
    assert env.turns[-1].autonomy_preset == "supervised"
    # a stricter request is honoured
    with TestClient(env.app) as client:
        run = _post(client, {"mode": "agent", "autonomy_preset": "read_only"}).json()["run"]
        _wait(client)
    assert run["autonomy_preset"] == "read_only" and run["autonomy_clamped"] is False
    # a read_only project caps even a supervised request, and its workspace is used
    env.project = {"id": "p1", "workspace": "D:/code/p", "folder": "p", "enabled": True,
                   "execution_defaults": {"autonomy_preset": "read_only"}}
    with TestClient(env.app) as client:
        run = _post(client, {"mode": "agent", "project_id": "p1", "autonomy_preset": "bounded_autonomous"}).json()["run"]
        _wait(client)
    assert run["autonomy_preset"] == "read_only" and run["project_id"] == "p1"
    assert env.turns[-1].workspace == "D:/code/p"
    # unknown preset and unknown project are refused
    with TestClient(env.app) as client:
        assert _post(client, {"mode": "agent", "autonomy_preset": "root"}).status_code == 400
        assert _post(client, {"mode": "agent", "project_id": "nope"}).status_code == 404


def test_clamp_table():
    for asked, base, want in [("bounded_autonomous", "bounded_autonomous", "bounded_autonomous"),
                              ("bounded_autonomous", "supervised", "supervised"),
                              ("supervised", "read_only", "read_only"),
                              (None, "bounded_autonomous", "bounded_autonomous"),
                              (None, None, "supervised"), ("read_only", "supervised", "read_only")]:
        assert ta.clamp_autonomy(asked, base)["effective"] == want


def test_failure_and_doubtful_runs_are_never_done(env):
    env.add_note()
    cases = [
        (RuntimeError("model down"), "failed"),
        (SimpleNamespace(text="", error="boom", approvals=[], stop_reason="", tool_calls=0), "failed"),
        (SimpleNamespace(text="I need to run bash", error="", approvals=[{"kind": "tool"}], stop_reason="approval", tool_calls=1), "needs_review"),
        (SimpleNamespace(text="", error="", approvals=[], stop_reason="", tool_calls=0), "needs_review"),
        (SimpleNamespace(text="partial", error="", approvals=[], stop_reason="timeout", tool_calls=9), "needs_review"),
    ]
    with TestClient(env.app) as client:
        for outcome, expected in cases:
            env.outcome = outcome
            _post(client)
            assert _wait(client)["status"] == expected
    note = env.read_note()
    assert json.loads(note.agent_run)["status"] == "needs_review"


def test_interrupted_run_is_repaired_and_owner_scoped(env):
    env.add_note(agent_run=json.dumps({"status": "running", "session_id": "old", "run_id": "r0"}))
    with TestClient(env.app) as client:
        run = client.get("/api/todos/n1/agent", headers={"x-user": "alice"}).json()["run"]
        assert run["status"] == "failed" and "interrupted" in run["error"]
        assert client.get("/api/todos/n1/agent", headers={"x-user": "mallory"}).status_code == 404
        assert _post(client, user="mallory").status_code == 404
        assert _post(client, note_id="missing").status_code == 404
    assert json.loads(env.read_note().agent_run)["status"] == "failed"


def test_empty_todo_and_wrong_mode_are_refused(env):
    env.add_note(id="e", title="", content="", items=[])
    with TestClient(env.app) as client:
        assert _post(client, note_id="e").status_code == 400
        env.add_note(id="n2")
        assert _post(client, {"mode": "chat"}, note_id="n2").status_code == 400
    assert env.sessions == []


def test_note_listing_exposes_the_run(env):
    from routes.note.note_routes import _note_to_dict
    env.add_note(agent_run=json.dumps({"status": "done", "summary": "ok"}))
    assert _note_to_dict(env.read_note())["agent_run"]["summary"] == "ok"


def test_api_token_rules():
    from core.authz import api_token_allowed
    path = "/api/todos/abc/assign-agent"
    assert api_token_allowed("POST", path, ["agents:dispatch", "todos:write"])[0]
    assert not api_token_allowed("POST", path, ["agents:dispatch"])[0]
    assert not api_token_allowed("POST", path, ["todos:write"])[0]
    assert api_token_allowed("GET", "/api/todos/abc/agent", ["todos:read"])[0]
    import routes.api_token_routes as atr
    assert atr._normalize_scopes(profile="todo_agent") == ["todos:read", "todos:write", "agents:dispatch"]


def test_mcp_tool_is_registered_and_calls_the_route(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    tool = next(t for t in ws.TOOLS if t.name == "todo_assign_agent")
    assert tool.inputSchema["required"] == ["todo_id"]
    calls = []

    def fake_request(method, path, body=None, *a, **k):
        calls.append((method, path, body))
        return {"run": {"status": "running", "session_id": "s9", "autonomy_preset": "supervised",
                        "autonomy_clamped": True, "already_running": False}}

    monkeypatch.setattr(ws, "_request", fake_request)
    out = asyncio.run(ws.call_tool("todo_assign_agent", {"todo_id": "n 1", "autonomy_preset": "bounded_autonomous"}))[0].text
    assert calls[0] == ("POST", "/api/todos/n%201/assign-agent", {"mode": "agent", "autonomy_preset": "bounded_autonomous"})
    assert "running" in out and "s9" in out and "tightened" in out
    assert "required" in asyncio.run(ws.call_tool("todo_assign_agent", {}))[0].text


def test_app_wires_the_router():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert re.search(r"include_router\(setup_todo_agent_routes\(\)\)", src)


def test_studio_check():
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    node = shutil.which("node")
    if not node or not (root / "node_modules" / "esbuild").exists():
        pytest.skip("node or esbuild not available")
    done = subprocess.run([node, "studio/checks/todo-agent.check.mjs"], cwd=root,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
