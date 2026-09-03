"""Robot mode — the uniform envelope on the machine-facing reads
(src/robot_envelope.py + the eight endpoints that opt in).

Two things are being defended here.

1. **The browser did not change.** Every touched endpoint is called with NO
   query parameters and the raw bytes are compared against the shape written
   into this file — the pre-change answer, spelled out, not re-derived from
   the code under test. If robot mode ever leaks into the default path a page
   in Faustus breaks, so that assertion is the point of the file.
2. **A coordinator gets one shape.** `?robot=1` wraps the very same payload in
   {ok, data, error_code, error, elapsed_ms, schema_version}; `?format=toon`
   sends that envelope as text/plain TOON that `toon.decode` parses back into
   exactly the JSON one; and a failure (missing job, missing project, a
   collector that blew up) comes back as an envelope with ok:false and an
   error_code, never as FastAPI's bare {"detail": …}.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import robot_envelope, toon


def _body(payload) -> bytes:
    """The bytes Starlette writes for a route that returns `payload` — the
    pre-change answer, so a mismatch means the default path moved."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _check(client, path, expected, params=None):
    """The three modes of one endpoint: default byte-identical, ?robot=1
    enveloped, ?format=toon the same envelope as TOON text."""
    params = dict(params or {})
    plain = client.get(path, params=params)
    assert plain.status_code == 200
    assert plain.content == _body(expected), plain.content
    assert plain.headers["content-type"].startswith("application/json")

    robot = client.get(path, params={**params, "robot": "1"})
    assert robot.status_code == 200
    envelope = robot.json()
    assert envelope["ok"] is True and envelope["data"] == expected
    assert envelope["error_code"] is None and envelope["error"] is None
    assert envelope["schema_version"] == 1 and isinstance(envelope["elapsed_ms"], int)
    assert set(envelope) == {"ok", "data", "error_code", "error", "elapsed_ms",
                             "schema_version"}

    compact = client.get(path, params={**params, "format": "toon"})
    assert compact.status_code == 200
    assert compact.headers["content-type"] == "text/plain; charset=utf-8"
    parsed = toon.decode(compact.text)
    assert parsed["ok"] is True and parsed["data"] == expected
    assert parsed["schema_version"] == 1
    return compact.text


def _check_error(client, path, *, status, code, params=None):
    """The same failure in both modes: still the HTTP status, but a body a
    machine can read the same way it reads a success."""
    plain = client.get(path, params=params or {})
    assert plain.status_code == status
    assert "detail" in plain.json()

    envelope = client.get(path, params={**(params or {}), "robot": "1"})
    assert envelope.status_code == status
    body = envelope.json()
    assert body["ok"] is False and body["error_code"] == code and body["data"] is None
    assert isinstance(body["error"], str) and body["error"]

    compact = client.get(path, params={**(params or {}), "format": "toon"})
    assert compact.status_code == status
    parsed = toon.decode(compact.text)
    assert parsed["ok"] is False and parsed["error_code"] == code


# ── the envelope itself ─────────────────────────────────────────────────────

def test_the_envelope_has_exactly_the_six_fields_and_ok_tracks_error_code():
    ok = robot_envelope.envelope({"a": 1})
    assert ok == {"ok": True, "data": {"a": 1}, "error_code": None, "error": None,
                  "elapsed_ms": 0, "schema_version": 1}
    bad = robot_envelope.envelope(error_code="http_404", error="no such dispatch job")
    assert bad == {"ok": False, "data": None, "error_code": "http_404",
                   "error": "no such dispatch job", "elapsed_ms": 0, "schema_version": 1}
    # ok is exactly "no error_code" — an error string alone does not flip it
    assert robot_envelope.envelope(None, error="odd")["ok"] is True
    assert robot_envelope.envelope(None, error_code="")["ok"] is False


def test_elapsed_ms_comes_from_a_monotonic_stamp_and_never_goes_backwards():
    started = time.monotonic()
    time.sleep(0.01)
    assert robot_envelope.envelope(None, started_at=started)["elapsed_ms"] >= 5
    # a stamp from the future (clock games, a bad caller) floors at 0
    assert robot_envelope.envelope(None, started_at=time.monotonic() + 5)["elapsed_ms"] == 0
    assert robot_envelope.envelope(None, started_at="nonsense")["elapsed_ms"] == 0


def test_schema_version_is_declared_and_overridable():
    assert robot_envelope.SCHEMA_VERSION == 1
    assert robot_envelope.envelope(None, schema_version=7)["schema_version"] == 7
    assert robot_envelope.envelope(None, schema_version="nope")["schema_version"] == 1


def test_render_picks_the_format_and_falls_back_to_json():
    payload = robot_envelope.envelope({"rows": [{"a": 1}, {"a": 2}]})
    assert robot_envelope.render(payload, "toon") == toon.encode(payload)
    assert json.loads(robot_envelope.render(payload, "json")) == payload
    assert json.loads(robot_envelope.render(payload, "yaml")) == payload
    assert json.loads(robot_envelope.render(payload, None)) == payload


def test_wants_is_off_for_a_browser_call_and_on_for_the_two_switches():
    def req(query):
        return SimpleNamespace(query_params=query)

    assert robot_envelope.wants(req({})) is False
    assert robot_envelope.wants(req({"limit": "50"})) is False
    assert robot_envelope.wants(req({"robot": "0"})) is False
    assert robot_envelope.wants(req({"format": "json"})) is False
    for on in ("1", "true", "yes", "on", "TRUE"):
        assert robot_envelope.wants(req({"robot": on})) is True
    assert robot_envelope.wants(req({"format": "TOON"})) is True
    assert robot_envelope.fmt_of(req({"format": "toon"})) == "toon"
    assert robot_envelope.fmt_of(req({"robot": "1"})) == "json"
    # a request object that cannot be read must not take the route down
    assert robot_envelope.wants(SimpleNamespace()) is False


# ── GET /api/dispatch/{id} and /{id}/events ─────────────────────────────────

COMPACT_JOB = {
    "id": "abc123", "status": "partial", "title": "Workers · add apply_tax",
    "verdict": "1/2 workers done (timeout) · 2 files changed on disk",
    "workspace": "/srv/proj", "model": "qwen3.5:9b", "duration_s": 60.0,
    "chat_url": "/#sess-1", "error": "",
    "result": {"workers": [{"name": "w1", "status": "done", "rounds": 4},
                           {"name": "w2", "status": "error", "rounds": 4}],
               "files_changed": ["cart.py", "tests/test_cart.py"],
               "totals": {"tool_calls": 9, "rounds": 8, "errors": 1}},
}
JOB_EVENTS = [
    {"event": "job", "message": "checkpointing the workspace", "ts": 1.0},
    {"event": "started", "message": "w1", "ts": 2.0},
    {"event": "tick", "message": "w1 round 2", "ts": 3.0},
]


@pytest.fixture()
def dispatch_client(monkeypatch):
    import routes.dispatch_routes as dr
    from src import dispatch

    job = SimpleNamespace(id="abc123", status="partial", events=list(JOB_EVENTS))
    monkeypatch.setattr(dr, "require_user", lambda request: "luis")
    monkeypatch.setattr(dr, "_is_admin", lambda owner: True)
    monkeypatch.setattr(dispatch, "get", lambda job_id: job if job_id == job.id else None)
    monkeypatch.setattr(dispatch, "visible_to", lambda j, owner: True)
    monkeypatch.setattr(dispatch, "compact", lambda j: COMPACT_JOB)
    app = FastAPI()
    app.include_router(dr.setup_dispatch_routes())
    return TestClient(app)


def test_dispatch_status_and_events_answer_in_all_three_modes(dispatch_client):
    text = _check(dispatch_client, "/api/dispatch/abc123", COMPACT_JOB)
    # the workers rows collapsed into a table instead of repeating the keys
    assert "workers[2]{name,status,rounds}:" in text
    events = {"id": "abc123", "status": "partial", "events": JOB_EVENTS}
    _check(dispatch_client, "/api/dispatch/abc123/events", events)


def test_an_unknown_dispatch_job_is_a_404_envelope_not_a_bare_detail(dispatch_client):
    _check_error(dispatch_client, "/api/dispatch/nope", status=404, code="http_404")
    _check_error(dispatch_client, "/api/dispatch/nope/events", status=404, code="http_404")


def test_robot_mode_does_not_hand_a_job_to_someone_who_may_not_see_it(monkeypatch):
    import routes.dispatch_routes as dr
    from src import dispatch

    monkeypatch.setattr(dr, "require_user", lambda request: "eve")
    monkeypatch.setattr(dr, "_is_admin", lambda owner: False)
    monkeypatch.setattr(dispatch, "get", lambda job_id: SimpleNamespace(id=job_id))
    app = FastAPI()
    app.include_router(dr.setup_dispatch_routes())
    client = TestClient(app)
    for params in ({}, {"robot": "1"}, {"format": "toon"}):
        answer = client.get("/api/dispatch/abc123", params=params)
        assert answer.status_code == 403
        assert "admins only" in answer.text and "Workers" not in answer.text


# ── GET /api/projects/{id}/objectives ───────────────────────────────────────

DASHBOARD = {
    "objectives": [
        {"t": "obj", "id": "OBJ-1", "title": "Ship the API", "status": "open",
         "priority": 1, "owner": "user", "notes": "", "deps": []},
        {"t": "obj", "id": "OBJ-2", "title": "Write the docs", "status": "done",
         "priority": 2, "owner": "user", "notes": "", "deps": []},
    ],
    "edges": [],
    "scores": {"OBJ-1": {"score": 0.61, "hint": None}, "OBJ-2": {"score": 0.2, "hint": None}},
    "log": [{"ts": "2026-08-30T12:34:56+00:00", "kind": "delta", "op": "ADD", "id": "OBJ-1"}],
}


@pytest.fixture()
def objectives_client(monkeypatch):
    from routes import project_routes
    import services.objectives as objectives

    class Store:
        def get(self, project_id, owner):
            if project_id == "p1":
                return {"id": "p1", "name": "Covernet", "workspace": "/srv/proj"}
            if project_id == "p-no-folder":
                return {"id": "p-no-folder", "name": "Loose", "workspace": ""}
            return None

    monkeypatch.setattr(project_routes, "get_store", lambda: Store())
    monkeypatch.setattr(project_routes, "effective_user", lambda request: "luis")
    monkeypatch.setattr(objectives, "dashboard_payload",
                        lambda project, log_limit=50: DASHBOARD)
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    # Keyed on the router's OWN reference: an earlier test in the suite may
    # have dropped core.middleware from sys.modules and re-imported it, which
    # leaves a fresh require_admin object that would not match this override.
    app.dependency_overrides[project_routes.require_admin] = lambda: None
    return TestClient(app)


def test_the_objectives_dashboard_answers_in_all_three_modes(objectives_client):
    text = _check(objectives_client, "/api/projects/p1/objectives", DASHBOARD)
    # No table here, and that is the documented limit rather than a bug: every
    # objective carries a `deps` list, so the rows are not all-scalar and the
    # array is written out as items. The dashboard still round-trips exactly.
    assert "objectives:" in text and "\n    -\n" in text
    assert toon.decode(text)["data"]["objectives"] == DASHBOARD["objectives"]


def test_a_missing_project_and_a_folderless_one_come_back_as_envelopes(objectives_client):
    _check_error(objectives_client, "/api/projects/zzz/objectives",
                 status=404, code="http_404")
    _check_error(objectives_client, "/api/projects/p-no-folder/objectives",
                 status=400, code="http_400")


# ── GET /api/memory-engine/items and /pack ──────────────────────────────────

ITEMS = {
    "status": "success",
    "items": [{"id": "a" * 32, "id8": "aaaaaaaa", "text": "Always run the tests",
               "level": "procedural", "effective_score": 0.71, "harmful_ratio": 0.0},
              {"id": "b" * 32, "id8": "bbbbbbbb", "text": "Never touch the public API",
               "level": "procedural", "effective_score": 0.44, "harmful_ratio": 0.0}],
    "stats": {"active": 2, "semantic_lane": False},
    "levels": ["procedural", "semantic", "episodic"],
    "trust_classes": {"human_explicit": 0.85, "agent_assertion": 0.5},
}
PACK = {"status": "success", "pack": "# Learned\n- run the tests\n", "ids": ["a" * 32],
        "degraded": False, "chars": 26, "budget": 2000, "enabled": True}


@pytest.fixture()
def memory_client(monkeypatch):
    from routes import memory_engine_routes
    from src import memory_engine as engine

    monkeypatch.setattr(memory_engine_routes, "effective_user", lambda request: "luis")
    monkeypatch.setattr(engine, "list_items",
                        lambda **kw: [{"id": "a" * 32}, {"id": "b" * 32}])
    monkeypatch.setattr(engine, "_utcnow", lambda: "now")
    monkeypatch.setattr(engine, "public_item",
                        lambda item, now=None: next(i for i in ITEMS["items"]
                                                    if i["id"] == item["id"]))
    monkeypatch.setattr(engine, "stats", lambda owner, project: ITEMS["stats"])
    monkeypatch.setattr(engine, "LEVELS", tuple(ITEMS["levels"]))
    monkeypatch.setattr(engine, "TRUST_CLASSES", dict(ITEMS["trust_classes"]))
    monkeypatch.setattr(engine, "pack_detail",
                        lambda owner, project, query, budget: {
                            "text": PACK["pack"], "ids": PACK["ids"], "degraded": False})
    monkeypatch.setattr(engine, "injection_budget", lambda: PACK["budget"])
    monkeypatch.setattr(engine, "injection_enabled", lambda: True)
    app = FastAPI()
    app.include_router(memory_engine_routes.setup_memory_engine_routes())
    app.dependency_overrides[memory_engine_routes.require_admin] = lambda: None
    return TestClient(app)


def test_the_learned_items_and_the_pack_answer_in_all_three_modes(memory_client):
    text = _check(memory_client, "/api/memory-engine/items", ITEMS)
    assert "items[2]{id,id8,text,level,effective_score,harmful_ratio}:" in text
    packed = _check(memory_client, "/api/memory-engine/pack", PACK)
    # a multi-line block survives as one escaped scalar, newlines and all
    assert "\\n" in packed and toon.decode(packed)["data"]["pack"] == PACK["pack"]


def test_a_broken_memory_store_is_an_envelope_in_robot_mode(memory_client, monkeypatch):
    from src import memory_engine as engine

    def boom(**kw):
        raise engine.MemoryEngineError("level must be one of procedural, semantic")

    monkeypatch.setattr(engine, "list_items", boom)
    _check_error(memory_client, "/api/memory-engine/items", status=400, code="http_400")

    def explode(**kw):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(engine, "list_items", explode)
    answer = memory_client.get("/api/memory-engine/items", params={"robot": "1"})
    assert answer.status_code == 500
    assert answer.json()["error_code"] == "internal_error"
    assert "database is gone" in answer.json()["error"]


# ── GET /api/command-guard/log and /explain ─────────────────────────────────

GUARD_LOG = {
    "status": "success",
    "receipts": [
        {"ts": "2026-08-30T12:34:56+00:00", "tool": "bash", "tier": "DANGEROUS",
         "rule": "fs.rm_rf", "action": "blocked", "command_head": "rm -rf build/"},
        {"ts": "2026-08-30T12:35:02+00:00", "tool": "bash", "tier": "SAFE",
         "rule": "", "action": "allowed", "command_head": "pytest -q"},
    ],
    "chain": {"ok": True, "entries": 2, "broken_at": None},
}
EXPLAIN = {"status": "success", "mode": "enforce", "packs": ["db", "fs"],
           "allowlisted": None, "tier": "DANGEROUS", "rule_id": "fs.rm_rf",
           "matched": "rm -rf", "command_head": "rm -rf build/", "trace": ["fs pack hit"],
           "rules_tested": 42, "fail_open": False}


@pytest.fixture()
def guard_client(monkeypatch):
    from routes import command_guard_routes as cgr
    from src import command_guard
    from src import tool_capabilities

    monkeypatch.setattr(cgr, "require_admin", lambda request: "admin")
    monkeypatch.setattr(command_guard, "tail_receipts", lambda limit: GUARD_LOG["receipts"])
    monkeypatch.setattr(command_guard, "verify_chain", lambda: GUARD_LOG["chain"])
    monkeypatch.setattr(command_guard, "explain", lambda command, packs=None: {
        "tier": EXPLAIN["tier"], "rule_id": EXPLAIN["rule_id"], "matched": EXPLAIN["matched"],
        "command_head": EXPLAIN["command_head"], "trace": EXPLAIN["trace"],
        "rules_tested": EXPLAIN["rules_tested"], "fail_open": EXPLAIN["fail_open"]})
    monkeypatch.setattr(command_guard, "is_allowlisted", lambda command: None)
    monkeypatch.setattr(tool_capabilities, "command_guard_mode", lambda: "enforce")
    monkeypatch.setattr(tool_capabilities, "_command_guard_packs", lambda: {"fs", "db"})
    app = FastAPI()
    app.include_router(cgr.setup_command_guard_routes())
    return TestClient(app)


def test_the_guard_log_and_explain_answer_in_all_three_modes(guard_client):
    text = _check(guard_client, "/api/command-guard/log", GUARD_LOG)
    assert "receipts[2]{ts,tool,tier,rule,action,command_head}:" in text
    _check(guard_client, "/api/command-guard/explain", EXPLAIN,
           params={"command": "rm -rf build/"})


def test_explain_without_a_command_is_a_400_envelope(guard_client):
    _check_error(guard_client, "/api/command-guard/explain", status=400, code="http_400")


def test_a_non_admin_is_refused_in_robot_mode_too(guard_client, monkeypatch):
    from routes import command_guard_routes as cgr

    def deny(request):
        raise HTTPException(403, "admin only")

    monkeypatch.setattr(cgr, "require_admin", deny)
    _check_error(guard_client, "/api/command-guard/log", status=403, code="http_403")
    answer = guard_client.get("/api/command-guard/log", params={"format": "toon"})
    assert "receipts" not in answer.text


# ── GET /api/system/usage ───────────────────────────────────────────────────

USAGE = {
    "ts": 1767268496.12,
    "host": {"cpu_percent": 12.5, "ram_percent": 31.2},
    "gpus": [{"index": 0, "name": "RTX 4090", "mem_total": 24564, "mem_used": 18234, "util": 87},
             {"index": 1, "name": "RTX 3090", "mem_total": 24576, "mem_used": 2048, "util": 3}],
    "gpu_count": 2,
    "models": [{"name": "qwen3.5:9b", "size_vram": 9123456789, "gpu": 0}],
    "orphans": [],
}


@pytest.fixture()
def usage_client(monkeypatch):
    import routes.system_usage_routes as sur

    async def collect():
        return USAGE

    monkeypatch.setattr(sur, "require_user", lambda request: "luis")
    monkeypatch.setattr(sur, "collect_usage", collect)
    app = FastAPI()
    app.include_router(sur.setup_system_usage_routes())
    return TestClient(app)


def test_system_usage_answers_in_all_three_modes(usage_client):
    text = _check(usage_client, "/api/system/usage", USAGE)
    assert "gpus[2]{index,name,mem_total,mem_used,util}:" in text


def test_a_failed_collector_is_a_500_envelope_in_robot_mode(usage_client, monkeypatch):
    import routes.system_usage_routes as sur

    async def boom():
        raise OSError("nvidia-smi vanished")

    monkeypatch.setattr(sur, "collect_usage", boom)
    _check_error(usage_client, "/api/system/usage", status=500, code="http_500")


# ── the guarantee that matters most ─────────────────────────────────────────

def test_no_query_parameters_means_no_envelope_anywhere(
        dispatch_client, objectives_client, memory_client, guard_client, usage_client):
    """One sweep over every touched endpoint: a browser call must come back as
    the payload itself, never wrapped, never text/plain."""
    calls = [
        (dispatch_client, "/api/dispatch/abc123", COMPACT_JOB, {}),
        (dispatch_client, "/api/dispatch/abc123/events",
         {"id": "abc123", "status": "partial", "events": JOB_EVENTS}, {}),
        (objectives_client, "/api/projects/p1/objectives", DASHBOARD, {}),
        (memory_client, "/api/memory-engine/items", ITEMS, {}),
        (memory_client, "/api/memory-engine/pack", PACK, {}),
        (guard_client, "/api/command-guard/log", GUARD_LOG, {}),
        (guard_client, "/api/command-guard/explain", EXPLAIN, {"command": "rm -rf build/"}),
        (usage_client, "/api/system/usage", USAGE, {}),
    ]
    for client, path, expected, params in calls:
        answer = client.get(path, params=params)
        assert answer.status_code == 200, path
        assert answer.content == _body(expected), path
        assert "ok" not in answer.json() or "ok" in expected, path
        assert answer.headers["content-type"].startswith("application/json"), path


def test_an_unrelated_query_parameter_still_means_no_envelope(memory_client, guard_client):
    """The pages pass their own parameters (limit, project, level…): none of
    them may be mistaken for the robot switch."""
    assert memory_client.get("/api/memory-engine/items",
                             params={"limit": 5, "level": "procedural"}).content == _body(ITEMS)
    assert guard_client.get("/api/command-guard/log",
                            params={"limit": 10}).content == _body(GUARD_LOG)
    assert memory_client.get("/api/memory-engine/items",
                             params={"format": "csv"}).content == _body(ITEMS)


# ── the MCP server asking for the compact form ──────────────────────────────

def _load_workers_server(monkeypatch):
    """mcp_servers/workers_server.py with the `mcp` package stubbed — the same
    loader tests/test_dispatch.py uses, so no network and no MCP install."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

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
    for name, module in (("mcp", mcp), ("mcp.server", srv),
                         ("mcp.server.stdio", stdio), ("mcp.types", typesmod)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "workers_server_robot_mode",
        Path(__file__).resolve().parents[1] / "mcp_servers" / "workers_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mcp_format_switch_defaults_to_toon_and_honours_text(monkeypatch):
    ws = _load_workers_server(monkeypatch)
    monkeypatch.delenv("FAUSTUS_MCP_FORMAT", raising=False)
    assert ws.mcp_format() == "toon"
    monkeypatch.setenv("FAUSTUS_MCP_FORMAT", "TEXT")
    assert ws.mcp_format() == "text"
    monkeypatch.setenv("FAUSTUS_MCP_FORMAT", "")
    assert ws.mcp_format() == "toon"
    monkeypatch.setenv("FAUSTUS_MCP_FORMAT", "nonsense")
    assert ws.mcp_format() == "toon"


def test_the_mcp_asks_for_format_toon_and_falls_back_to_the_human_wording(monkeypatch):
    ws = _load_workers_server(monkeypatch)
    monkeypatch.delenv("FAUSTUS_MCP_FORMAT", raising=False)
    seen = []

    def fake(method, path, body=None, timeout=30.0, retries=1, as_text=False):
        seen.append(path)
        assert as_text is True
        return toon.encode(robot_envelope.envelope({"status": "success"}))

    monkeypatch.setattr(ws, "_request", fake)
    assert ws._toon("/api/dispatch/j1").startswith("ok: true")
    assert seen == ["/api/dispatch/j1?format=toon"]
    # an existing query string keeps its parameters
    ws._toon("/api/memory-engine/pack?project=x")
    assert seen[-1] == "/api/memory-engine/pack?project=x&format=toon"

    # FAUSTUS_MCP_FORMAT=text never even asks
    monkeypatch.setenv("FAUSTUS_MCP_FORMAT", "text")
    assert ws._toon("/api/dispatch/j1") is None
    assert len(seen) == 2
    monkeypatch.setenv("FAUSTUS_MCP_FORMAT", "toon")

    # an older Faustus ignores the parameter and answers JSON: not TOON, so the
    # caller renders the human form instead of shipping a JSON blob as "toon"
    monkeypatch.setattr(ws, "_request",
                        lambda *a, **k: '{"ok": true, "data": {}}')
    assert ws._toon("/api/dispatch/j1") is None

    # and a server that is not there at all is a fallback, never an exception
    def boom(*a, **k):
        raise RuntimeError("Faustus is not reachable at http://127.0.0.1:7000")

    monkeypatch.setattr(ws, "_request", boom)
    assert ws._toon("/api/dispatch/j1") is None


@pytest.mark.asyncio
async def test_the_row_shaped_tools_hand_the_toon_through_and_the_others_do_not(monkeypatch):
    ws = _load_workers_server(monkeypatch)
    monkeypatch.delenv("FAUSTUS_MCP_FORMAT", raising=False)
    compact = toon.encode(robot_envelope.envelope(GUARD_LOG))
    asked = []

    def fake(method, path, body=None, timeout=30.0, retries=1, as_text=False):
        asked.append((path, as_text))
        if as_text:
            return compact
        if path == "/api/projects":
            return [{"id": "p1", "name": "Covernet", "folder": "covernet",
                     "workspace": "/srv/proj"}]
        if path.endswith("/objectives"):
            return DASHBOARD
        if path.startswith("/api/memory-engine/pack"):
            return PACK
        if path.endswith("/events"):
            return {"id": "j1", "status": "done", "events": JOB_EVENTS}
        return COMPACT_JOB

    monkeypatch.setattr(ws, "_request", fake)

    rows = await ws.call_tool("objectives_list", {"project": "p1"})
    assert compact in rows[0].text and "Covernet" in rows[0].text
    status = await ws.call_tool("workers_status", {"job_id": "j1"})
    assert status[0].text == compact
    guard = await ws.call_tool("guard_explain", {"command": "rm -rf build/"})
    assert guard[0].text == compact
    assert all(path.endswith("format=toon") for path, as_text in asked if as_text)

    # the two whose answer is prose or a deliberate tail keep their rendering
    pack = await ws.call_tool("memory_pack", {"project": "p1"})
    assert pack[0].text.startswith("learned memory") and "\\n" not in pack[0].text
    events = await ws.call_tool("workers_events", {"job_id": "j1"})
    assert events[0].text.startswith("job j1 · done · 3 events")
    assert not any(as_text for path, as_text in asked
                   if "pack" in path or path.endswith("/events"))


@pytest.mark.asyncio
async def test_the_mcp_tool_roster_is_unchanged_by_the_format_work(monkeypatch):
    """The format switch is not a new tool and removes none: the roster
    tests/test_dispatch.py pins stays exactly as it was."""
    ws = _load_workers_server(monkeypatch)
    assert [t.name for t in ws.TOOLS] == [
        "dispatch_workers", "workers_wait", "workers_status", "workers_events",
        "workers_cancel", "workers_guide", "workers_list", "objectives_list",
        "guard_explain", "memory_pack", "objectives_apply"]
