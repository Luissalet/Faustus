"""H19 orphan workers and H23 per-turn cost view."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from src import agent_runs, exec_ledger as xl, orphan_workers, turn_cost
from src.agent_tools import subagent_tools as st


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    xl.set_db_path(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(st, "_ACTIVE_WORKERS", {})
    monkeypatch.setattr(st, "_WORKER_RUNS", {})
    agent_runs._RUNS.clear()
    yield
    agent_runs._RUNS.clear()
    xl.set_db_path(None)


def _worker(sid, parent, parent_run, *, age=600.0):
    run = st.SubagentRun(0, {"name": f"w-{sid}", "instruction": "x"})
    run.session_id, run.parent_session_id, run.parent_run_id = sid, parent, parent_run
    run.started = time.time() - age
    run.last_event_at = run.started
    return run


async def _register(run):
    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
    task = asyncio.create_task(forever())
    st._ACTIVE_WORKERS[run.session_id] = task
    st._WORKER_RUNS[run.session_id] = run
    await asyncio.sleep(0)
    return task


def _parent_run(sid, status="running"):
    run = agent_runs._Run()
    run.status = status
    agent_runs._RUNS[sid] = run
    return run


@pytest.mark.asyncio
async def test_workers_of_a_live_parent_are_not_orphans():
    parent = _parent_run("p1")
    task = await _register(_worker("c1", "p1", parent.run_id))
    assert orphan_workers.scan() == []
    task.cancel()


@pytest.mark.asyncio
async def test_a_worker_whose_parent_run_ended_is_listed_and_killable():
    parent = _parent_run("p2", status="done")
    task = await _register(_worker("c2", "p2", parent.run_id))
    found = orphan_workers.scan()
    assert [(o["session_id"], o["parent_state"]) for o in found] == [("c2", "finished")]
    assert found[0]["killable"] and "ended" in found[0]["reason"]
    out = orphan_workers.kill("c2")
    assert out["stopped"] is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orphan_workers.scan() == []


@pytest.mark.asyncio
async def test_a_replaced_parent_run_makes_the_worker_an_orphan():
    _parent_run("p3")  # a different run now owns the session
    task = await _register(_worker("c3", "p3", "the-old-run-id"))
    assert [o["parent_state"] for o in orphan_workers.scan()] == ["replaced"]
    task.cancel()


@pytest.mark.asyncio
async def test_a_parent_with_no_known_run_is_judged_only_after_the_grace_period():
    young = await _register(_worker("c4", "ghost", "r", age=5.0))
    old = await _register(_worker("c5", "ghost", "r", age=orphan_workers.GRACE_S + 30))
    assert [o["session_id"] for o in orphan_workers.scan()] == ["c5"]
    young.cancel()
    old.cancel()


@pytest.mark.asyncio
async def test_kill_refuses_anything_that_is_not_an_orphan_or_not_yours():
    parent = _parent_run("p6")
    task = await _register(_worker("c6", "p6", parent.run_id))
    assert orphan_workers.kill("c6")["stopped"] is False      # its parent is alive
    assert not task.cancelled()
    parent.status = "done"
    assert orphan_workers.kill("c6", owns=lambda sid: False)["stopped"] is False  # not this caller's
    assert not task.cancelled()
    task.cancel()


@pytest.mark.asyncio
async def test_a_stale_dispatch_job_is_reported_and_closed_as_interrupted(monkeypatch):
    from src import dispatch
    job = SimpleNamespace(id="abc123def456", status="running", task=None, session_id="s9", title="Workers",
                          started=time.time() - 300, created=time.time() - 300, verdict=None, finished=None,
                          _persist=lambda: None)
    monkeypatch.setattr(dispatch, "_jobs", {job.id: job})
    found = orphan_workers.scan()
    assert [(o["kind"], o["worker_id"]) for o in found] == [("dispatch_job", job.id)]
    assert orphan_workers.kill(job.id)["stopped"] is True
    assert job.status == "interrupted" and "orphaned" in job.verdict


# ---------------------------------------------------------------------------
# H23
# ---------------------------------------------------------------------------

def _seed_turn(run="r1", sid="s1"):
    xl.run_started(run, sid, label="t")
    usage = {"input_tokens": 1000, "output_tokens": 200, "cost_state": "unknown"}
    xl.model_call(run, sid, transport="stream", model="m1", usage=usage, duration_ms=4000)
    xl.model_call(run, sid, transport="stream", model="m1", usage={"input_tokens": 1500}, duration_ms=3000)
    xl.model_call(run, sid, phase="compaction", transport="call", model="m2", usage=usage, duration_ms=1500)
    xl.model_call(run, sid, phase="recovery", transport="call", model="m1", usage={}, duration_ms=None)
    xl.model_call(run, sid, phase="advisor", transport="call", model="adv",
                  usage={"input_tokens": 300, "output_tokens": 80, "usage_source": "estimated"}, duration_ms=900)
    xl.model_call(run, sid, transport="stream", model="m1", usage={}, duration_ms=700, error="timeout")
    xl.model_call(run, sid, transport="call", model="judge", usage={"input_tokens": 50}, duration_ms=200)
    xl.call_requested(run, sid, "c1", tool="bash", args_sha256="a")
    xl.attempt_started(run, sid, "c1", "att_1")
    xl.attempt_result(run, sid, "c1", "att_1", status="failed", effect_certainty="unknown", duration_ms=2500)
    xl.attempt_started(run, sid, "c1", "att_2")
    xl.attempt_result(run, sid, "c1", "att_2", status="succeeded", effect_certainty="confirmed", duration_ms=1800)
    xl.call_requested(run, sid, "c2", tool="read_file", args_sha256="b")
    xl.attempt_started(run, sid, "c2", "att_3")
    xl.attempt_result(run, sid, "c2", "att_3", status="succeeded", effect_certainty="none", duration_ms=None)
    xl.call_requested(run, sid, "c3", tool="delegate_agents", args_sha256="c")
    xl.attempt_started(run, sid, "c3", "att_4")
    xl.attempt_result(run, sid, "c3", "att_4", status="succeeded", effect_certainty="none", duration_ms=9000)
    xl.child_result(run, sid, child_session_id="kid", worker_id="sa1", name="w", outcome="succeeded",
                    input_tokens=4000, output_tokens=600, duration_ms=8800, tool_calls=3, parent_call_id="c3")
    xl.child_result(run, sid, child_session_id="kid2", worker_id="sa2", name="w2", outcome="cancelled",
                    input_tokens=None, output_tokens=None, duration_ms=8000, parent_call_id="c3")
    xl.run_state(run, sid, "done")


def _phase(view, key):
    return next(p for p in view["phases"] if p["key"] == key)


def test_cost_view_groups_the_turn_by_cause_and_keeps_unknowns_unknown():
    _seed_turn()
    view = turn_cost.build("s1")
    assert view["found"] and view["run_id"] == "r1"
    main = _phase(view, "main_rounds")
    assert main["calls"] == 2 and main["duration_ms"] == 7000
    assert main["input_tokens"] == 2500 and main["output_tokens"] == 200
    assert main["output_tokens_unknown_calls"] == 1      # one round never reported output
    assert main["cost_usd"] is None and main["cost_state"] == "unknown"
    assert _phase(view, "compaction")["duration_ms"] == 1500
    recovery = _phase(view, "recovery")
    assert recovery["calls"] == 1 and recovery["duration_ms"] is None       # not 0
    assert recovery["duration_unknown_calls"] == 1 and recovery["input_tokens"] is None
    advisor = _phase(view, "advisor")
    assert advisor["estimated_calls"] == 1 and advisor["input_tokens"] == 300
    tools = _phase(view, "tools")
    assert tools["calls"] == 2 and tools["duration_ms"] == 2500             # first attempts; read_file has no duration
    assert tools["duration_unknown_calls"] == 1
    assert {i["tool"] for i in tools["items"]} == {"bash", "read_file"}
    assert _phase(view, "workers")["duration_ms"] == 9000                   # the parent's wait, not double counted in tools
    workers = _phase(view, "workers")
    assert workers["calls"] == 2 and workers["input_tokens"] == 4000 and workers["input_tokens_unknown_calls"] == 1
    retries = _phase(view, "retries")
    assert retries["calls"] == 2 and retries["tool_attempts"] == 1           # the failed model call + bash's second attempt
    assert retries["duration_ms"] == 700 + 1800
    assert _phase(view, "other_model")["calls"] == 1                          # the unlabelled judge call
    assert view["state"] == "done"


def test_a_turn_with_no_recorded_work_says_so_instead_of_inventing_zeroes():
    assert turn_cost.build("nothing")["found"] is False
    xl.run_started("r9", "s9")
    xl.run_state("r9", "s9", "done")
    view = turn_cost.build("s9", "r9")
    assert view["found"] and _phase(view, "main_rounds")["duration_ms"] is None
    assert any("no model call was recorded" in n for n in view["notes"])


def test_turn_selection_and_render():
    _seed_turn("older", "s2")
    time.sleep(0.01)
    xl.run_started("newer", "s2")
    xl.model_call("newer", "s2", transport="stream", model="m", usage={"input_tokens": 5}, duration_ms=10)
    xl.run_state("newer", "s2", "done")
    assert turn_cost.build("s2")["run_id"] == "newer"
    assert turn_cost.build("s2", turn=1)["run_id"] == "older"
    text = turn_cost.render_text(turn_cost.build("s2", turn=1))
    assert "main_rounds" in text and "unknown" in text and "tools" in text


def test_model_calls_reach_the_ledger_even_when_tracing_is_off(monkeypatch):
    from src import llm_trace
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: False)
    token = None
    from src.run_causality import bind_run, reset_run
    token = bind_run("s-live", "run-live")
    try:
        llm_trace.record_call(session_id="s-live", model="m", usage={"input_tokens": 12, "output_tokens": 3},
                              duration_ms=50.0, transport="stream")
        llm_trace.flush_for_tests()
    finally:
        reset_run(token)
    rows = [e for e in xl.events("run-live") if e["kind"] == "model_call"]
    assert len(rows) == 1 and rows[0]["payload"]["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert rows[0]["payload"]["transport"] == "stream"


@pytest.mark.asyncio
async def test_advisor_call_is_attributed_to_the_run_that_asked(monkeypatch):
    from src import advisor
    from src.run_causality import bind_run, reset_run
    result = advisor.AdvisorResult(trigger="loop", model="adv-model", tokens_in=300, tokens_out=80,
                                   tokens_source="reported", latency_ms=900.0)
    token = bind_run("s-adv", "run-adv")
    try:
        advisor._ledger_call(result)
    finally:
        reset_run(token)
    row, = [e for e in xl.events("run-adv") if e["kind"] == "model_call"]
    assert row["payload"]["phase"] == "advisor" and row["payload"]["usage"]["usage_source"] == "reported_engine"


@pytest.mark.asyncio
async def test_worker_ending_is_recorded_under_the_parent_run():
    run = _worker("kid", "p", "parent-run")
    run.input_tokens, run.output_tokens, run.tool_calls = 100, 20, 2
    run.parent_call_id = "call-9"
    run.finished = run.started + 5
    st._ledger_child_result(run)
    row, = [e for e in xl.events("parent-run") if e["kind"] == "child_result"]
    assert row["payload"]["child_session_id"] == "kid" and row["payload"]["input_tokens"] == 100
    assert row["payload"]["duration_ms"] == 5000.0 and row["call_id"] == "call-9"


# --- run_report tool + routes -------------------------------------------------

@pytest.mark.asyncio
async def test_run_report_tool_reads_each_view(monkeypatch, tmp_path):
    from src import effect_outbox as eo
    import importlib
    # Patch the module object itself: other tests reload the agent_tools package,
    # and a dotted-path patch then cannot find the submodule as an attribute.
    rr = importlib.import_module("src.agent_tools.run_report_tool")
    RunReportTool = rr.RunReportTool
    eo.set_db_path(str(tmp_path / "effects.sqlite3"))
    try:
        _seed_turn("rr", "s-tool")
        monkeypatch.setattr(rr, "_owns", lambda owner: (lambda sid: True))
        tool = RunReportTool()
        ctx = {"session_id": "s-tool", "owner": "alice"}
        cost = await tool.execute(json.dumps({"view": "cost"}), ctx)
        assert cost["exit_code"] == 0 and "main_rounds" in cost["output"] and cost["cost"]["found"]
        ledger = await tool.execute(json.dumps({"view": "ledger"}), ctx)
        assert "c1" in ledger["output"] and "succeeded" in ledger["output"]
        row = eo.prepare(kind="email.smtp", owner="alice", destination="a@example.invalid")
        eo.admit(row["id"])
        eo.begin_dispatch(row["id"])
        eo.settle(row["id"], "outcome_unknown", reason="connection cut after the data phase")
        effects = await tool.execute(json.dumps({"view": "effects", "unresolved": True}), ctx)
        assert row["id"] in effects["output"] and "outcome_unknown" in effects["output"]
        steering = await tool.execute(json.dumps({"view": "steering"}), ctx)
        assert steering["exit_code"] == 0
        orphans = await tool.execute(json.dumps({"view": "orphans"}), ctx)
        assert "No orphaned workers" in orphans["output"]
        bad = await tool.execute(json.dumps({"view": "nope"}), ctx)
        assert bad["exit_code"] == 1
        monkeypatch.setattr(rr, "_owns", lambda owner: (lambda sid: False))
        denied = await tool.execute(json.dumps({"view": "cost", "session_id": "someone-elses"}), ctx)
        assert denied["exit_code"] == 1
    finally:
        eo.set_db_path(None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import run_report_routes as rr
    from src import effect_outbox as eo
    eo.set_db_path(str(tmp_path / "effects.sqlite3"))
    monkeypatch.setattr(rr, "_verify_session_owner", lambda request, sid, *a, **k: None)
    monkeypatch.setattr(rr, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.dependency_overrides[rr.require_user] = lambda: "alice"
    app.include_router(rr.setup_run_report_routes())
    yield TestClient(app)
    eo.set_db_path(None)


def test_routes_turn_cost_ledger_effects_and_orphans(client):
    from src import effect_outbox as eo
    _seed_turn("rt", "s-http")
    cost = client.get("/api/runs/s-http/turn-cost").json()
    assert cost["found"] and cost["text"].startswith("Turn rt")
    assert client.get("/api/runs/s-http/turn-cost", params={"run_id": "other"}).json()["found"] is False
    listing = client.get("/api/runs/s-http/ledger").json()
    assert [r["run_id"] for r in listing["runs"]] == ["rt"]
    replay = client.get("/api/runs/s-http/ledger", params={"run_id": "rt", "events": True}).json()
    assert replay["replay"]["state"] == "done" and replay["events"]
    assert client.get("/api/runs/another/ledger", params={"run_id": "rt"}).status_code == 404

    row = eo.prepare(kind="webhook.deliver", owner="alice", destination="https://example.invalid/h")
    eo.admit(row["id"])
    eo.begin_dispatch(row["id"])
    eo.settle(row["id"], "outcome_unknown", reason="read timeout")
    other = eo.prepare(kind="webhook.deliver", owner="bob", destination="https://example.invalid/b")
    eff = client.get("/api/effects", params={"unresolved": True}).json()
    assert [e["id"] for e in eff["effects"]] == [row["id"]] and other["id"] not in eff["text"]
    assert client.get(f"/api/effects/{other['id']}").status_code == 404   # another owner's
    detail = client.get(f"/api/effects/{row['id']}").json()
    assert detail["effect"]["state"] == "outcome_unknown" and detail["events"]
    assert client.post(f"/api/effects/{row['id']}/resolve", json={"landed": "yes"}).status_code == 400
    done = client.post(f"/api/effects/{row['id']}/resolve", json={"landed": False, "note": "checked the receiver"}).json()
    assert done["ok"] and client.get(f"/api/effects/{row['id']}").json()["effect"]["state"] == "reconciled"
    assert client.get("/api/agent/orphans").json()["count"] == 0
    assert client.post("/api/agent/orphans/nope/kill").status_code == 404
