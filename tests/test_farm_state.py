"""GET /api/farm/state: the merge, the grouping, the ETag, the auth.

`src/farm_state.py` takes every source as a keyword of `build()`, so the merge
is exercised here with fakes: no server, no model, no database.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.authz import KNOWN_SCOPES, api_token_allowed
from src import farm_state

NOW = 10_000.0


@pytest.fixture(autouse=True)
def _fresh_cache():
    farm_state.reset_for_tests()
    yield
    farm_state.reset_for_tests()


def _rows(**by_sid):
    return lambda ids: {sid: row for sid, row in by_sid.items()}


def _job(id_="j1", owner="luis", status="running", title="Port the parser", session_id="s-job",
         unattended=False, created=NOW - 60, tasks=2, model="local-27b"):
    return SimpleNamespace(id=id_, owner=owner, status=status, title=title, session_id=session_id,
                           unattended=unattended, created=created, started=created + 1, model=model,
                           args={"tasks": [{"name": f"w{i}"} for i in range(tasks)]},
                           events=[{"ts": NOW - 5, "name": "job"}])


def _build(**kw):
    kw.setdefault("now", NOW)
    kw.setdefault("runs", {})
    kw.setdefault("pending_approvals", [])
    kw.setdefault("workers", {})
    kw.setdefault("jobs", [])
    kw.setdefault("shifts", [])
    kw.setdefault("workflows", [])
    kw.setdefault("tasks", [])
    kw.setdefault("budget", {})
    kw.setdefault("session_rows", _rows())
    return farm_state.build(kw.pop("owner", "luis"), **kw)


def _flat(items):
    for it in items:
        yield it
        yield from _flat(it["children"])


def _by_id(body):
    return {it["id"]: it for it in _flat(body["items"])}


# -- the merge and the grouping -------------------------------------------------

def test_a_chat_run_carries_its_chat_name_model_link_and_never_a_message():
    body = _build(
        runs={"s1": {"run_id": "r1", "phase": "tool", "started_at": NOW - 30, "last_event_at": NOW - 2,
                     "round": 3, "tool": "read_file", "percent": 40.0, "label": "please wipe my disk"}},
        session_rows=_rows(s1={"name": "Refactor the importer", "model": "qwen-9b", "owner": "luis"}))
    (run,) = body["items"]
    assert run["id"] == "run:s1" and run["kind"] == "chat_run" and run["parent"] is None
    assert run["title"] == "Refactor the importer" and run["model"] == "qwen-9b"
    assert run["state"] == "running" and run["link"] == "/studio?s=s1"
    assert run["started_at"] == NOW - 30 and run["last_event_at"] == NOW - 2
    assert run["progress"] == {"percent": 40.0, "unit": "plan"}
    assert "wipe" not in str(body)


def test_workers_sit_under_the_run_that_started_them_and_orphans_become_roots():
    body = _build(
        runs={"s1": {"started_at": NOW - 90, "last_event_at": NOW - 1}},
        workers={"c1": {"parent": "s1", "name": "reader", "role": "explorer", "started_at": NOW - 60,
                        "round": 2, "last_event_at": NOW - 3, "stalled": False, "tool_calls": 4},
                 "c2": {"parent": "s1", "name": "writer", "started_at": NOW - 50, "last_event_at": NOW - 4, "stalled": True},
                 "c3": {"parent": "gone", "name": "lost", "started_at": NOW - 10}},
        session_rows=_rows(s1={"name": "Big task", "model": "m", "owner": "luis"},
                           c1={"name": "reader", "model": "w-model", "owner": "luis"},
                           c2={"name": "writer", "model": "w-model", "owner": "luis"},
                           c3={"name": "lost", "model": "w-model", "owner": "luis"}))
    roots = {it["id"]: it for it in body["items"]}
    assert set(roots) == {"run:s1", "worker:c3"}
    kids = {c["id"]: c for c in roots["run:s1"]["children"]}
    assert set(kids) == {"worker:c1", "worker:c2"}
    assert kids["worker:c1"]["parent"] == "run:s1" and kids["worker:c1"]["model"] == "w-model"
    assert kids["worker:c1"]["tool_calls"] == 4 and kids["worker:c2"]["state"] == "stalled"
    assert roots["worker:c3"]["parent"] is None
    assert body["counts"] == {"total": 4, "roots": 2, "by_kind": {"chat_run": 1, "worker": 3},
                              "by_state": {"running": 3, "stalled": 1}}


def test_a_dispatch_job_owns_its_workers_and_hides_the_chat_run_of_its_own_session():
    body = _build(
        jobs=[_job()],
        runs={"s-job": {"started_at": NOW - 59, "last_event_at": NOW - 1}},
        workers={"c1": {"parent": "s-job", "name": "w0", "started_at": NOW - 50, "last_event_at": NOW - 2}},
        session_rows=_rows(**{"s-job": {"name": "Port the parser", "model": "m", "owner": "luis"},
                              "c1": {"name": "w0", "model": "m", "owner": "luis"}}))
    (job,) = body["items"]
    assert job["id"] == "job:j1" and job["kind"] == "dispatch_job" and job["model"] == "local-27b"
    assert job["link"] == "/studio?s=s-job" and job["progress"]["total"] == 2
    assert [c["id"] for c in job["children"]] == ["worker:c1"]
    assert job["last_event_at"] == NOW - 5


def test_a_night_shift_holds_its_unattended_job_only_when_it_cannot_be_anyone_elses():
    shift = {"id": "n1", "owner": "luis", "state": "running", "started": NOW - 100, "created": NOW - 101,
             "tasks": ["a", "b", "c"], "results": [{"task": "a"}], "model": "m"}
    body = _build(shifts=[shift], jobs=[_job(unattended=True, created=NOW - 50),
                                        _job("j2", unattended=False, session_id="s2", created=NOW - 40)])
    items = _by_id(body)
    assert items["job:j1"]["parent"] == "shift:n1" and items["job:j2"]["parent"] is None
    assert items["shift:n1"]["progress"] == {"done": 1, "total": 3, "unit": "tasks", "percent": 33.3}
    assert items["shift:n1"]["title"] == "Night shift (3 tasks)"
    two = _build(shifts=[shift, dict(shift, id="n2")], jobs=[_job(unattended=True, created=NOW - 50)])
    assert _by_id(two)["job:j1"]["parent"] is None


def test_run_states_are_waiting_queued_stalled_never_running_by_inertia():
    body = _build(
        runs={"a": {"started_at": NOW - 500, "last_event_at": NOW - 1},
              "b": {"started_at": NOW - 500, "last_event_at": NOW - 1, "queued_position": 2},
              "c": {"started_at": NOW - 500, "last_event_at": NOW - farm_state.STALE_AFTER_S - 5},
              "d": {"started_at": NOW - 500, "last_event_at": NOW - 1, "phase": "awaiting_user"},
              "e": {"started_at": NOW - 500, "last_event_at": NOW - 1}},
        pending_approvals=["e"],
        session_rows=_rows(**{k: {"name": k, "model": "m", "owner": "luis"} for k in "abcde"}))
    states = {it["id"]: it["state"] for it in body["items"]}
    assert states == {"run:a": "running", "run:b": "queued", "run:c": "stalled",
                      "run:d": "waiting", "run:e": "waiting"}
    assert next(it for it in body["items"] if it["id"] == "run:b")["queue_position"] == 2


def test_workflows_and_scheduled_tasks_are_listed_with_progress_where_it_is_known():
    body = _build(
        workflows=[{"id": "w1", "title": "Nightly digest", "status": "paused", "owner": "luis",
                    "started_at": NOW - 300, "last_event_at": NOW - 20, "done": 2, "total": 5, "trigger": "schedule"}],
        tasks=[{"id": "t1", "name": "Morning mail check", "model": "m", "session_id": "s9", "attempt": 1,
                "last_event_at": NOW - 8, "task_type": "llm"}])
    items = _by_id(body)
    wf, task = items["workflow:w1"], items["task:t1"]
    assert wf["kind"] == "workflow_run" and wf["state"] == "paused" and wf["progress"]["percent"] == 40.0
    assert task["kind"] == "scheduled_task" and task["state"] == "running" and task["link"] == "/studio?s=s9"
    assert task["started_at"] is None and "attempt" not in task


def test_each_owner_sees_only_their_own_work_and_single_user_mode_sees_all():
    kw = dict(runs={"mine": {"started_at": NOW - 5}, "theirs": {"started_at": NOW - 5}},
              workers={"cw": {"parent": "theirs", "name": "x", "started_at": NOW - 4}},
              jobs=[_job("jm", owner="luis"), _job("jt", owner="eve", session_id="s-eve")],
              workflows=[{"id": "wf", "title": "t", "status": "running", "owner": "eve"}],
              session_rows=_rows(mine={"name": "mine", "model": "m", "owner": "luis"},
                                 theirs={"name": "theirs", "model": "m", "owner": "eve"},
                                 cw={"name": "x", "model": "m", "owner": "eve"}))
    assert set(_by_id(_build(owner="luis", **kw))) == {"run:mine", "job:jm"}
    assert set(_by_id(_build(owner="", **kw))) == {"run:mine", "run:theirs", "worker:cw", "job:jm", "job:jt", "workflow:wf"}


def test_a_source_that_fails_is_named_and_the_rest_still_answers(monkeypatch):
    def boom():
        raise RuntimeError("registry offline")
    monkeypatch.setattr(farm_state, "_src_workers", boom)
    body = farm_state.build("luis", now=NOW, runs={"s1": {"started_at": NOW - 5}}, pending_approvals=[],
                            jobs=[], shifts=[], workflows=[], tasks=[], budget={},
                            session_rows=_rows(s1={"name": "ok", "model": "m", "owner": "luis"}))
    assert body["errors"]["workers"].startswith("RuntimeError") and [i["id"] for i in body["items"]] == ["run:s1"]


def test_a_parent_cycle_and_duplicate_ids_do_not_loop_or_double_count():
    a = farm_state._item("x:a", "worker", "a", "running", parent="x:b", started_at=1)
    b = farm_state._item("x:b", "worker", "b", "running", parent="x:a", started_at=2)
    dup = farm_state._item("x:a", "worker", "dup", "running", started_at=3)
    roots = farm_state._assemble([a, b, dup])
    flat = list(_flat(roots))
    assert sorted(i["id"] for i in flat) == ["x:a", "x:b"]
    assert all(r["parent"] is None for r in roots)

# -- the budget block -----------------------------------------------------------

def test_the_budget_block_keeps_pace_gpu_breaker_and_cooldowns_and_drops_the_clock():
    state = {"enabled": True, "window": "week", "window_resets_at": NOW + 500, "interactive_active": True,
             "providers": [{"provider": "hosted", "metric": "usd", "window": "week", "used": 2.5, "target": 10.0,
                            "pace": {"allowed_fraction": 0.30012, "allowed": 3.0012, "elapsed_fraction": 0.2},
                            "paused": False, "paused_until": None, "reason": "ok", "window_resets_at": NOW + 500}],
             "gpu": {"provider": "local", "metric": "gpu_seconds", "window": "day", "used": 120.0, "target": 600,
                     "pace": {"allowed_fraction": 0.5, "allowed": 300.0}, "paused": True, "paused_until": NOW + 60,
                     "reason": "ahead of pace"},
             "breaker": {"enabled": True, "open": True, "consecutive_failures": 3, "threshold": 3,
                         "pause_until": NOW + 900, "last_failure": {"kind": "dispatch", "detail": "secret detail"}},
             "cooldowns": [{"endpoint": "api.example.com", "status": 429, "until": NOW + 120, "hits": 2,
                            "credential": "abc", "remaining_s": 120.0}]}
    block = _build(budget=state)["budget"]
    row, gpu = block["providers"][0], block["gpu"]
    assert row["used_fraction"] == 0.25 and row["pace_fraction"] == 0.3 and row["pace_allowed"] == 3.0012
    assert gpu["used"] == 120.0 and gpu["paused"] is True and gpu["paused_until"] == NOW + 60
    assert block["breaker"] == {"enabled": True, "open": True, "consecutive_failures": 3, "threshold": 3,
                                "reopens_at": NOW + 900, "last_failure_kind": "dispatch"}
    assert block["cooldowns"] == [{"endpoint": "api.example.com", "status": 429, "until": NOW + 120, "hits": 2}]
    assert block["interactive_active"] is True
    assert "secret detail" not in str(block) and "abc" not in str(block)


def test_no_budget_state_is_null_not_an_error():
    assert _build(budget={})["budget"] is None


# -- ETag and cache -------------------------------------------------------------

def test_the_etag_ignores_the_clock_and_moves_with_the_picture():
    runs = {"s1": {"started_at": NOW - 5, "last_event_at": NOW - 1}}
    rows = _rows(s1={"name": "A", "model": "m", "owner": "luis"})
    first = _build(runs=runs, session_rows=rows)
    later = _build(runs=runs, session_rows=rows, now=NOW + 3)
    assert first["generated_at"] != later["generated_at"]
    assert farm_state.etag_of(first) == farm_state.etag_of(later)
    changed = _build(runs={"s1": {"started_at": NOW - 5, "last_event_at": NOW + 2}}, session_rows=rows, now=NOW + 3)
    assert farm_state.etag_of(changed) != farm_state.etag_of(first)


def test_if_none_match_accepts_weak_strong_lists_and_star():
    tag = 'W/"farm-0123456789abcdef"'
    assert farm_state.etag_matches(tag, tag)
    assert farm_state.etag_matches('"farm-0123456789abcdef"', tag)
    assert farm_state.etag_matches('"x", W/"farm-0123456789abcdef"', tag)
    assert farm_state.etag_matches("*", tag)
    assert not farm_state.etag_matches('W/"farm-other"', tag) and not farm_state.etag_matches("", tag)
    assert not farm_state.etag_matches(None, tag)


def test_the_build_is_shared_for_a_second_per_owner_and_rebuilt_after(monkeypatch):
    calls = []
    monkeypatch.setattr(farm_state, "build", lambda owner="": calls.append(owner) or {"generated_at": len(calls), "items": [owner]})
    a1 = farm_state.cached("luis")
    a2 = farm_state.cached("luis")
    assert a1 is not None and a1[0] is a2[0] and a1[1] == a2[1] and calls == ["luis"]
    farm_state.cached("eve")
    assert calls == ["luis", "eve"]
    farm_state.cached("luis", ttl=0)
    assert calls == ["luis", "eve", "luis"]


# -- the route ------------------------------------------------------------------

def _client(monkeypatch, *, auth_off=False):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.farm_routes as fr

    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.current_user = request.headers.get("x-test-user") or ""
        return await call_next(request)

    monkeypatch.setattr(fr, "effective_user", lambda request: getattr(request.state, "current_user", None) or None)
    monkeypatch.setattr(fr, "_auth_disabled", lambda: auth_off)
    app.include_router(fr.setup_farm_routes())
    return TestClient(app)


def test_the_route_needs_a_user_and_serves_the_document_with_an_etag(monkeypatch):
    monkeypatch.setattr(farm_state, "build", lambda owner="": {"schema": 1, "generated_at": 1.0, "items": [], "owner": owner})
    c = _client(monkeypatch)
    assert c.get("/api/farm/state").status_code == 401
    r = c.get("/api/farm/state", headers={"x-test-user": "luis"})
    assert r.status_code == 200 and r.json()["owner"] == "luis" and r.headers["etag"].startswith('W/"farm-')
    assert r.headers["cache-control"] == "no-cache"


def test_a_repeat_with_the_etag_is_a_304_with_no_body_until_the_picture_changes(monkeypatch):
    state = {"n": 0}

    def fake_build(owner=""):
        return {"schema": 1, "generated_at": state.get("t", 0.0), "items": [], "n": state["n"]}

    monkeypatch.setattr(farm_state, "build", fake_build)
    c = _client(monkeypatch)
    h = {"x-test-user": "luis"}
    first = c.get("/api/farm/state", headers=h)
    tag = first.headers["etag"]
    state["t"] = 5.0
    farm_state.reset_for_tests()
    again = c.get("/api/farm/state", headers={**h, "if-none-match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    state["n"] = 1
    farm_state.reset_for_tests()
    changed = c.get("/api/farm/state", headers={**h, "if-none-match": tag})
    assert changed.status_code == 200 and changed.json()["n"] == 1 and changed.headers["etag"] != tag


def test_with_auth_off_the_route_answers_for_the_single_user(monkeypatch):
    monkeypatch.setattr(farm_state, "build", lambda owner="": {"schema": 1, "generated_at": 1.0, "items": [], "owner": owner})
    r = _client(monkeypatch, auth_off=True).get("/api/farm/state")
    assert r.status_code == 200 and r.json()["owner"] == ""


# -- the token scope --------------------------------------------------------------

def test_farm_read_is_a_known_and_mintable_scope():
    assert "farm:read" in KNOWN_SCOPES
    src = Path(__file__).resolve().parents[1] / "routes" / "api_token_routes.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    allowed = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ALLOWED_SCOPES" for t in node.targets):
            allowed = {elt.value for elt in node.value.elts}
    assert allowed is not None and "farm:read" in allowed


@pytest.mark.parametrize("scopes", [["farm:read"], ["sessions"]])
def test_the_farm_state_is_open_to_its_scope_and_to_sessions(scopes):
    allowed, why = api_token_allowed("GET", "/api/farm/state", scopes)
    assert allowed is True, why


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/farm/state"), ("DELETE", "/api/farm/state"), ("GET", "/api/farm"),
    ("GET", "/api/farm/state/extra"), ("GET", "/api/sessions"), ("POST", "/api/session"),
    ("GET", "/api/attention"), ("GET", "/api/budget/period"), ("GET", "/api/chat/activity"),
    ("POST", "/api/dispatch"), ("GET", "/api/approvals/pending"),
])
def test_the_farm_scope_opens_nothing_else(method, path):
    assert api_token_allowed(method, path, ["farm:read"])[0] is False


@pytest.mark.parametrize("scopes", [["chat"], ["attention:read"], ["agents:dispatch"], []])
def test_other_scopes_do_not_open_the_farm_state(scopes):
    assert api_token_allowed("GET", "/api/farm/state", scopes)[0] is False