"""Dispatch — local workers for an outside coordinator (src/dispatch.py,
routes/dispatch_routes.py, mcp_servers/workers_server.py).

The point: Fable (Claude in Cowork / Claude Desktop) plans and reviews; the
mechanical tool loop runs on the local workers and comes back as a few
hundred tokens, never a transcript.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from src import dispatch

SUBAGENT_REPORT = {
    "id": 0, "name": "w1", "session_id": "child-1", "status": "done", "stop_reason": "complete", "error": None,
    "tool_calls": 7, "failed_calls": 1, "mutations": ["cart.py", "tests/test_cart.py"], "rejections": [],
    "rounds": 4, "static_checks": {"ok": True}, "git": {"clean": False}, "duration_s": 41.2,
    "final_text": "  Added   apply_tax(total, rate)\n\nand its test; 3 passed.  " + "x" * 3000,
    "role": "worker", "files": [], "model": "qwen3.5:9b", "instruction": "add apply_tax",
    "input_tokens": 12000, "output_tokens": 900, "started_at": 1.0, "ended_at": 42.2, "steered": [],
    "supervisor": [],
}
TOOL_RESULT = {
    "output": "…the long report the chat model would read…" * 50,
    "exit_code": 0,
    "subagents": [SUBAGENT_REPORT, dict(SUBAGENT_REPORT, name="w2", status="error", error="timeout", stop_reason="timeout", mutations=[],
                                        final_text="", input_tokens=500, output_tokens=10, tool_calls=2, failed_calls=0)],
    "duration_s": 60.0,
    "lock_conflicts": [{"worker": "w2", "path": "cart.py"}],
    "dropped_tasks": 0,
}


# ── the compact answer ──────────────────────────────────────────────────────

def test_compact_keeps_the_facts_and_drops_the_transcript():
    c = dispatch.compact_from_result(TOOL_RESULT)
    assert [w["name"] for w in c["workers"]] == ["w1", "w2"]
    w1 = c["workers"][0]
    assert w1["files_changed"] == ["cart.py", "tests/test_cart.py"] and w1["rounds"] == 4 and w1["tool_calls"] == 7
    assert w1["summary"].startswith("Added apply_tax(total, rate) and its test; 3 passed.")
    assert len(w1["summary"]) <= dispatch.SUMMARY_CHARS and w1["summary"].endswith("…")
    assert w1["static_checks"] == {"ok": True} and w1["git"] == {"clean": False}
    assert c["files_changed"] == ["cart.py", "tests/test_cart.py"]
    assert c["totals"] == {"tool_calls": 9, "failed_calls": 1, "rounds": 8, "input_tokens": 12500,
                           "output_tokens": 910, "errors": 1}
    assert c["lock_conflicts"] == ["w2 → cart.py"] and c["exit_code"] == 0
    # the report text — what the in-chat model reads — is not in the answer
    assert "the long report" not in json.dumps(c)
    # a few hundred tokens, not the transcript
    assert len(json.dumps(c)) < 2500


def test_compact_of_nothing_is_empty_not_an_error():
    assert dispatch.compact_from_result(None)["workers"] == []
    assert dispatch.compact_from_result({"error": "boom"})["totals"]["errors"] == 0


# ── build_args goes through the tool's own parser ───────────────────────────

def test_build_args_uses_the_delegation_parser_and_the_dispatch_defaults():
    args = dispatch.build_args({"tasks": ["fix the bug", {"instruction": "add tests", "files": ["t.py"]}],
                                "context": "repo uses pytest", "parallel": False})
    assert [t["instruction"] for t in args["tasks"]] == ["fix the bug", "add tests"]
    assert args["tasks"][1]["files"] == ["t.py"]
    assert args["parallel"] is False and args["shared_context"] == "repo uses pytest"
    assert args["max_rounds"] == dispatch._DEFAULT_MAX_ROUNDS and args["timeout_s"] == dispatch._DEFAULT_TIMEOUT_S
    with pytest.raises(ValueError):
        dispatch.build_args({"tasks": []})
    with pytest.raises(ValueError):
        dispatch.build_args({})


# ── a job end to end, with a fake delegate tool ─────────────────────────────

class _SM:
    def __init__(self):
        self.sessions = {}
        self.messages = []

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None):
        s = SimpleNamespace(id=session_id, name=name, endpoint_url=endpoint_url, model=model, owner=owner, headers=None)
        self.sessions[session_id] = s
        return s

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.messages.append((sid, msg))

    def save_sessions(self):
        self.saved = getattr(self, "saved", 0) + 1


@pytest.fixture
def box(tmp_path, monkeypatch):
    import src.ai_interaction as ai
    from src.agent_tools import subagent_tools as st
    sm = _SM()
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    monkeypatch.setattr(dispatch, "resolve_route", lambda owner, model=None: ("http://127.0.0.1:11434/v1", model or "qwen3.5:9b", None))
    state = {"executed": [], "ctx": None, "result": TOOL_RESULT, "delay": 0.0, "workspace_seen": None}

    class FakeTool:
        async def execute(self, content, ctx):
            from src import tool_execution as te
            state["executed"].append(json.loads(content))
            state["ctx"] = ctx
            state["workspace_seen"] = te.get_active_workspace()
            await ctx["progress_cb"]({"subagent": {"event": "started", "name": "w1", "session_id": "child-1"}})
            await ctx["progress_cb"]({"subagent": {"event": "tick", "name": "w1", "round": 2, "elapsed_s": 5, "last_tool": "read_file"}})
            if state["delay"]:
                await asyncio.sleep(state["delay"])
            return state["result"]

    monkeypatch.setattr(st, "DelegateAgentsTool", FakeTool)
    dispatch.reset_for_tests()
    (tmp_path / "ws").mkdir()
    state["ws"] = str(tmp_path / "ws")
    state["sm"] = sm
    yield state
    dispatch.reset_for_tests()


def test_start_runs_the_delegation_in_its_own_workers_chat_and_compacts(box):
    async def run():
        job = await dispatch.start("luis", {"tasks": ["add apply_tax"], "workspace": box["ws"], "model": "qwen3.5:9b"})
        assert job.status in ("queued", "running")
        assert await dispatch.wait(job, 5)
        return job
    job = asyncio.run(run())
    assert job.status == "done" and job.result is TOOL_RESULT
    # the delegation payload the tool got is the parsed one
    sent = box["executed"][0]
    assert sent["tasks"][0]["instruction"] == "add apply_tax" and sent["max_rounds"] == dispatch._DEFAULT_MAX_ROUNDS
    # it ran inside a Workers chat of the owner, on the resolved route, with the workspace bound
    assert box["ctx"]["session_id"] == job.session_id and box["ctx"]["owner"] == "luis"
    sess = box["sm"].sessions[job.session_id]
    assert sess.model == "qwen3.5:9b" and sess.name.startswith("Workers · add apply_tax") and sess.owner == "luis"
    assert box["workspace_seen"] == box["ws"]
    # the chat opens with a note saying where the job came from…
    assert box["sm"].messages[0][0] == job.session_id and "Dispatched from outside" in box["sm"].messages[0][1].content
    # …and ends with the turn a chat delegation would leave: the board's
    # evidence in tool_events.subagents, so the Workers chat shows the board
    last = box["sm"].messages[-1][1]
    assert last.role == "assistant" and "Dispatched job" in last.content and "changed cart.py" in last.content
    ev = last.metadata["tool_events"][0]
    assert ev["tool"] == "delegate_agents" and ev["dispatch_id"] == job.id and ev["exit_code"] == 0
    assert [r["name"] for r in ev["subagents"]] == ["w1", "w2"] and ev["subagents"][0]["mutations"] == ["cart.py", "tests/test_cart.py"]
    assert box["sm"].saved >= 1
    c = dispatch.compact(job)
    assert c["status"] == "done" and c["chat_url"] == f"/#{job.session_id}"
    assert c["result"]["workers"][0]["name"] == "w1" and "progress" not in c
    # and the JSON mirror lets it be read after a restart
    dispatch.reset_for_tests()
    again = dispatch.get(job.id)
    assert again is not None and again.status == "done" and again.result["subagents"][0]["name"] == "w1"


def test_progress_is_visible_while_running_and_wait_times_out_honestly(box):
    box["delay"] = 0.5

    async def run():
        job = await dispatch.start("luis", {"tasks": ["slow"], "workspace": box["ws"]})
        await asyncio.sleep(0.1)
        c = dispatch.compact(job)
        assert c["status"] == "running" and c["progress"]["w1"]["last_event"] == "tick"
        assert c["progress"]["w1"]["round"] == 2 and c["progress"]["w1"]["last_tool"] == "read_file"
        assert await dispatch.wait(job, 0.05) is False
        assert await dispatch.wait(job, 5) is True
        return job
    job = asyncio.run(run())
    assert job.status == "done"


def test_cancel_stops_a_running_job(box):
    box["delay"] = 5

    async def run():
        job = await dispatch.start("luis", {"tasks": ["slow"], "workspace": box["ws"]})
        await asyncio.sleep(0.1)
        assert dispatch.cancel(job) is True
        await asyncio.sleep(0.05)
        return job
    job = asyncio.run(run())
    assert job.status == "cancelled" and dispatch.cancel(job) is False


def test_a_bad_workspace_is_refused_before_anything_starts(box):
    with pytest.raises(ValueError):
        asyncio.run(dispatch.start("luis", {"tasks": ["x"], "workspace": box["ws"] + "-missing"}))
    assert not box["executed"]


def test_a_job_interrupted_by_a_restart_says_so(box, tmp_path):
    """The JSON mirror of a job that was still running when the process died
    says `running`; read back after a restart that is `interrupted`, never
    `done`, and never a job that looks alive."""
    async def run():
        box["delay"] = 5
        job = await dispatch.start("luis", {"tasks": ["slow"], "workspace": box["ws"]})
        await asyncio.sleep(0.1)
        job._persist()                   # what is on disk while it runs
        dispatch.reset_for_tests()       # "the server restarted": memory gone, file left
        again = dispatch.get(job.id)
        assert again is not None and again.status == "interrupted" and again.result is None
        assert dispatch.list_jobs("luis")[0]["id"] == job.id
        assert dispatch.compact(again)["status"] == "interrupted"
        job.task.cancel()
        await asyncio.sleep(0.05)
    asyncio.run(run())


# ── the route ───────────────────────────────────────────────────────────────

def _client(monkeypatch, *, token_scopes=None, cookie_user="luis"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.dispatch_routes as dr

    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        if token_scopes is not None:
            request.state.api_token = True
            request.state.api_token_scopes = list(token_scopes)
            request.state.api_token_owner = "luis"
        else:
            request.state.current_user = cookie_user
        return await call_next(request)

    monkeypatch.setattr(dr, "require_user", lambda request: getattr(request.state, "current_user", None) or "")
    app.include_router(dr.setup_dispatch_routes())
    return TestClient(app)


def test_route_needs_the_dispatch_scope_on_an_api_token(box, monkeypatch):
    c = _client(monkeypatch, token_scopes=["chat"])
    assert c.post("/api/dispatch", json={"tasks": ["x"], "workspace": box["ws"]}).status_code == 403
    assert c.get("/api/dispatch").status_code == 403


def test_route_runs_a_job_for_a_token_with_the_scope_and_hides_other_owners(box, monkeypatch):
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    r = c.post("/api/dispatch", json={"tasks": ["add apply_tax"], "workspace": box["ws"]})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    r = c.get(f"/api/dispatch/{job_id}/wait?timeout=5")
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert r.json()["result"]["workers"][0]["files_changed"] == ["cart.py", "tests/test_cart.py"]
    assert c.get("/api/dispatch").json()["jobs"][0]["id"] == job_id
    assert c.get(f"/api/dispatch/{job_id}/events").json()["events"][0]["event"] == "started"
    # another user's cookie session does not see it
    other = _client(monkeypatch, cookie_user="eve")
    assert other.get(f"/api/dispatch/{job_id}").status_code == 404
    assert other.get("/api/dispatch").json()["jobs"] == []
    # bad requests are 400s with a reason, not 500s
    r = c.post("/api/dispatch", json={"tasks": [], "workspace": box["ws"]})
    assert r.status_code == 400 and "tasks" in r.json()["detail"]
    assert c.post("/api/dispatch", json={"tasks": ["x"], "workspace": box["ws"] + "-nope"}).status_code == 400
    assert c.get("/api/dispatch/zzzz").status_code == 404


def test_the_token_profile_and_scope_exist():
    from routes import api_token_routes as atr
    assert "agents:dispatch" in atr.ALLOWED_SCOPES
    assert atr.TOKEN_PROFILES["fable_workers"] == ["agents:dispatch"]
    assert atr._normalize_scopes(profile="fable_workers") == ["agents:dispatch"]


@pytest.mark.asyncio
async def test_app_api_cannot_dispatch_from_inside_a_chat(monkeypatch):
    import httpx
    from src.tool_implementations import do_app_api

    class Unexpected:
        def __init__(self, *a, **k):
            raise AssertionError("must be refused before any loopback call")

    monkeypatch.setattr(httpx, "AsyncClient", Unexpected)
    result = await do_app_api(json.dumps({"action": "call", "method": "POST", "path": "/api/dispatch",
                                          "body": {"tasks": ["x"]}}), owner="admin")
    assert result["exit_code"] == 1 and "delegate_agents" in result["error"]


# ── the MCP server's rendering (no network) ─────────────────────────────────

def _load_workers_server(monkeypatch):
    # the mcp package may not be installed here: stub the three names it imports
    mcp = types.ModuleType("mcp")
    srv = types.ModuleType("mcp.server")
    stdio = types.ModuleType("mcp.server.stdio")
    typesmod = types.ModuleType("mcp.types")

    class Server:
        def __init__(self, name):
            self.name = name

        def list_tools(self):
            return lambda f: f

        def call_tool(self):
            return lambda f: f

    srv.Server = Server
    stdio.stdio_server = None
    typesmod.Tool = lambda **kw: SimpleNamespace(**kw)
    typesmod.TextContent = lambda **kw: SimpleNamespace(**kw)
    for name, mod in (("mcp", mcp), ("mcp.server", srv), ("mcp.server.stdio", stdio), ("mcp.types", typesmod)):
        monkeypatch.setitem(sys.modules, name, mod)
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("workers_server_under_test",
                                                  Path(__file__).resolve().parents[1] / "mcp_servers" / "workers_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_render_is_one_glance_and_names_the_board(monkeypatch):
    ws = _load_workers_server(monkeypatch)
    job = {"id": "abc123def456", "status": "done", "title": "Workers · add apply_tax", "workspace": "D:/proj",
           "model": "qwen3.5:9b", "duration_s": 60.0, "chat_url": "/#sess-1",
           "result": dispatch.compact_from_result(TOOL_RESULT)}
    text = ws.render(job)
    assert text.startswith("job abc123def456 · done · Workers · add apply_tax")
    assert "board: http://127.0.0.1:7000/#sess-1" in text
    assert "[w1] done · 4 rounds · 7 tools (1 failed) · 12000/900 tok" in text
    assert "changed: cart.py, tests/test_cart.py" in text and "says: Added apply_tax" in text
    assert "[w2] error (timeout)" in text and "error: timeout" in text
    assert "lock conflicts: w2 → cart.py" in text
    assert "totals: 9 tool calls, 8 rounds, 12500/910 local tokens, 1 errors" in text
    assert "the long report" not in text
    running = {"id": "abc123def456", "status": "running", "title": "t", "progress": {
        "w1": {"last_event": "tick", "round": 3, "last_tool": "bash", "elapsed_s": 40, "stalled": True, "stall_reason": "idle"}}}
    assert "w1: tick · round 3 · tool bash · 40 s · STALLED (idle)" in ws.render(running)
    names = [t.name for t in ws.TOOLS]
    assert names == ["dispatch_workers", "workers_wait", "workers_status", "workers_events", "workers_cancel",
                     "workers_guide", "workers_list"]


def test_the_guide_is_served_to_token_holders_and_says_the_essentials(box, monkeypatch):
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    r = c.get("/api/dispatch/guide")
    assert r.status_code == 200
    g = r.json()["guide"]
    for must in ("Self-contained", "parallel", "workspace", "files_changed", "never returns the\ntranscript",
                 "plan → dispatch → wait → check"):
        assert must in g, must
    assert _client(monkeypatch, token_scopes=["chat"]).get("/api/dispatch/guide").status_code == 403


def test_resolve_route_prefers_the_configured_dispatch_model_over_the_default(monkeypatch):
    """`dispatch_model` without `dispatch_endpoint_id` — the common case —
    must beat the utility/default model resolve_endpoint hands back (seen
    live: the 29 GB default model picked up a dispatched job)."""
    import src.endpoint_resolver as er
    from src import settings as settings_mod
    monkeypatch.setattr(er, "resolve_endpoint", lambda prefix, owner=None: ("http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0", None))
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: {"dispatch_model": "qwen3.5:9b"}.get(key, default))
    assert dispatch.resolve_route("luis") == ("http://127.0.0.1:11434/v1", "qwen3.5:9b", None)
    # the request's own model still wins
    assert dispatch.resolve_route("luis", "tiny:1b")[1] == "tiny:1b"
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    assert dispatch.resolve_route("luis")[1] == "qwen3.8:27b-q8_0"


def test_config_route_says_where_a_job_would_run(box, monkeypatch):
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    assert c.get("/api/dispatch/config").json() == {"model": "qwen3.5:9b", "server": "127.0.0.1:11434", "error": ""}
    monkeypatch.setattr(dispatch, "resolve_route", lambda owner, model=None: (_ for _ in ()).throw(ValueError("no model configured for dispatch")))
    assert c.get("/api/dispatch/config").json()["error"] == "no model configured for dispatch"
