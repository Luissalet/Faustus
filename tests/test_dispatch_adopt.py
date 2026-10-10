"""Dispatch jobs whose external agents outlive a restart of Faustus
(src/external_runs.py + src/dispatch.py, OBJ-53, Ágora #232).

A dispatched external agent is a real OS process; a restart of Faustus does not
stop it. Before this, the job read back from its mirror was `interrupted`
while the agent kept changing the workspace. Now its output goes to a log
file with a record (pid + creation time) next to it, and a restarted Faustus
re-attaches to it, follows it to its end and settles the job honestly.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from src import agent_runners as reg
from src import constants
from src import dispatch
from src import external_runs

SLOW_AGENT = (
    "import sys, time, json\n"
    "print('agent: reading the code', flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
    "print('agent: wrote cart.py', flush=True)\n"
    "print(json.dumps({'type': 'result', 'is_error': False}), flush=True)\n"
)


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    monkeypatch.setattr(dispatch, "ADOPT_POLL_S", 0.1)
    dispatch.reset_for_tests()
    procs = []
    yield SimpleNamespace(tmp=tmp_path, procs=procs)
    for p in procs:
        try:
            p.kill()
            p.wait(5)
        except Exception:
            pass
    dispatch.reset_for_tests()


def _spawn_agent(ns, run_id: str, seconds: float = 1.5, body: str = SLOW_AGENT,
                 limit_s=None, deadline=None) -> subprocess.Popen:
    """An agent started 'by the previous Faustus': output to its log file and
    its record written, exactly as src/external_worker.py does."""
    os.makedirs(external_runs.runs_dir(), exist_ok=True)
    script = ns.tmp / f"{run_id}.py"
    script.write_text(body, encoding="utf-8")
    with open(external_runs.log_path(run_id), "wb") as fh:
        proc = subprocess.Popen([sys.executable, "-u", str(script), str(seconds)], stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    ns.procs.append(proc)
    external_runs.start(run_id, pid=proc.pid, runner="fake", label="Fake agent", gated=False, cwd=str(ns.tmp),
                        limit_s=limit_s, deadline=deadline)
    return proc


def _mirror(ns, job_id: str, status: str = "running", ws: str = "") -> None:
    d = ns.tmp / "dispatch"
    d.mkdir(exist_ok=True)
    ws = ws or str(ns.tmp / "ws")
    os.makedirs(ws, exist_ok=True)
    (d / f"{job_id}.json").write_text(json.dumps({
        "id": job_id, "owner": "luis", "status": status, "workspace": ws, "created": time.time(),
        "started": time.time(), "title": "Workers", "verify": "none", "runners": ["fake"],
        "tasks": [{"instruction": "add apply_tax", "runner": "fake", "name": "taxer"}],
    }), encoding="utf-8")


# ── the run record ──────────────────────────────────────────────────────────

def test_a_run_record_proves_identity_by_pid_and_creation_time(runs):
    proc = _spawn_agent(runs, "aaaaaaaaaaaa-0", seconds=20)
    rec = external_runs.load("aaaaaaaaaaaa-0")
    assert rec and rec["pid"] == proc.pid and rec["status"] == "running" and rec["created"]
    assert external_runs.alive(rec)
    # a bare pid, or the same pid with another creation time, is not the run
    assert not external_runs.alive(dict(rec, created=None))
    assert not external_runs.alive(dict(rec, created=rec["created"] - 100))
    assert [r["run_id"] for r in external_runs.live_for_job("aaaaaaaaaaaa")] == ["aaaaaaaaaaaa-0"]
    assert external_runs.terminate(rec) is True
    proc.wait(10)
    assert not external_runs.alive(rec)
    external_runs.finish("aaaaaaaaaaaa-0", status="cancelled", adopted=True)
    rec = external_runs.load("aaaaaaaaaaaa-0")
    assert rec["status"] == "cancelled" and rec["finished"] and rec["adopted"] is True
    assert not external_runs.alive(rec) and external_runs.live_for_job("aaaaaaaaaaaa") == []


def test_bad_run_ids_are_refused(runs):
    for bad in ("", "../x", "a/b", "x" * 200, None):
        assert not external_runs.valid_run_id(bad)
        assert external_runs.load(bad) is None
    assert external_runs.live_for_job("../etc") == []


def test_the_log_is_read_in_complete_lines(runs, tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes("uno\ndós\npart".encode("utf-8"))
    text, off = external_runs.read_from(str(p), 0)
    assert text == "uno\ndós\n" and off == len("uno\ndós\n".encode("utf-8"))
    assert external_runs.read_from(str(p), off) == ("", off)          # the half line waits
    with open(p, "ab") as fh:
        fh.write(b"ial\n")
    assert external_runs.read_from(str(p), off)[0] == "partial\n"
    assert external_runs.read_from(str(tmp_path / "missing.log"), 0) == ("", 0)


def test_the_outcome_is_only_what_the_agent_itself_reported():
    ok = json.dumps({"type": "result", "is_error": False})
    assert external_runs.outcome_from_log("work\n" + ok + "\n")[0] == "done"
    assert external_runs.outcome_from_log(json.dumps({"type": "turn.completed"}))[0] == "done"
    assert external_runs.outcome_from_log(json.dumps({"type": "turn.failed", "error": "boom"})) == ("error", "boom")
    assert external_runs.outcome_from_log(json.dumps({"type": "result", "is_error": True,
                                                      "subtype": "error_max_turns"}))[0] == "error"
    status, why = external_runs.outcome_from_log("I did it, all done!\n")
    assert status == "unknown" and "exit code" in why


# ── a live run with an id writes a log and a record ─────────────────────────

def test_a_dispatched_external_run_leaves_its_log_and_record(runs, monkeypatch):
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: SimpleNamespace(
        create_session=lambda *a, **k: SimpleNamespace(id="s", headers=None), get_session=lambda sid: None,
        add_message=lambda *a, **k: None, save_sessions=lambda: None))
    monkeypatch.setattr(dispatch, "resolve_route", lambda owner, model=None: ("http://127.0.0.1:1/v1", "m", None))
    script = runs.tmp / "agent.py"
    script.write_text("import sys\nprint('agent working on:', sys.argv[1])\nprint('wrote nothing')\n", encoding="utf-8")
    row = reg.Runner(key="fake", label="Fake fake", kind="cli", licence="open", install="x",
                     argv=(sys.executable, str(script), "{task}"), detect=(sys.executable,), notes="test")
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 30)
    monkeypatch.setattr(reg, "get", lambda key, **kw: row if key == "fake" else None)
    monkeypatch.setattr(reg, "runners", lambda **kw: [row])
    ws = runs.tmp / "ws"
    ws.mkdir()

    async def run():
        job = await dispatch.start("luis", {"tasks": [{"instruction": "look", "runner": "fake"}],
                                            "workspace": str(ws), "verify": "none"})
        assert await dispatch.wait(job, 30)
        return job

    job = asyncio.run(run())
    assert job.status == "done"
    # the worker still saw the output (the board, the summary) ...
    w = dispatch.compact(job)["result"]["workers"][0]
    assert "agent working on: look" in w["summary"]
    # ... and it is on disk too, where a restarted Faustus can find it
    rid = f"{job.id}-0"
    rec = external_runs.load(rid)
    assert rec and rec["status"] == "done" and rec["exit_code"] == 0 and rec["finished"]
    # the effective limit (the smaller of the job's and the operator's, here 30 s)
    # is persisted as a wall-clock deadline an adopter can enforce
    assert rec["limit_s"] == 30 and abs(rec["deadline"] - rec["started"] - 30) < 2
    assert "agent working on: look" in open(external_runs.log_path(rid), encoding="utf-8").read()
    assert external_runs.live_for_job(job.id) == []


# ── adoption after a restart ────────────────────────────────────────────────

def test_a_restart_adopts_a_job_whose_agent_is_still_running(runs):
    jid = "abcdef123456"
    _spawn_agent(runs, f"{jid}-0", seconds=1.5)
    _mirror(runs, jid)

    async def run():
        assert dispatch.adopt_live_jobs() == [jid]
        job = dispatch.get(jid)
        assert job.status == "running" and job.adopted_runs == [f"{jid}-0"]
        assert job.to_dict()["adopted_runs"] == [f"{jid}-0"]
        assert any("adopted after a restart" in str(e.get("message")) for e in job.events)
        # the workspace is held while the agent still writes in it
        assert dispatch._ws_busy(dispatch._ws_key(job.workspace)) == jid
        assert await dispatch.wait(job, 20)
        return job

    job = asyncio.run(run())
    assert job.status == "partial"
    assert "verification and any later steps did not run" in job.verdict
    rep = job.result["subagents"][0]
    assert rep["status"] == "done" and rep["name"] == "taxer" and rep["role"] == "external"
    assert rep["unguarded"] is True
    tails = " ".join(str(e.get("tail") or "") for e in job.events)
    assert "agent: wrote cart.py" in tails                    # followed onto the board
    assert dispatch._ws_busy(dispatch._ws_key(job.workspace)) is None
    on_disk = json.loads((runs.tmp / "dispatch" / f"{jid}.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "partial" and on_disk["adopted_runs"] == [f"{jid}-0"]
    rec = external_runs.load(f"{jid}-0")
    assert rec["adopted"] is True and rec["status"] == "done"
    # a second restart finds nothing left to adopt
    dispatch.reset_for_tests()
    assert dispatch.adopt_live_jobs() == []
    assert dispatch.get(jid).status == "partial"


def _adopt_and_wait(jid, timeout=20):
    async def run():
        job = dispatch.get(jid)
        assert job.adopted_runs
        assert await dispatch.wait(job, timeout)
        return job
    return asyncio.run(run())


def test_an_adopted_run_already_past_its_deadline_is_stopped_at_once(runs):
    jid = "dead11ee0001"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=30, limit_s=10, deadline=time.time() - 1)
    _mirror(runs, jid)
    t0 = time.time()
    job = _adopt_and_wait(jid)
    assert time.time() - t0 < 10, "no fresh window after the restart"
    assert proc.wait(10) is not None
    rep = job.result["subagents"][0]
    assert rep["status"] == "timeout" and rep["timed_out"] is True and "stopped after 10s" in rep["error"]
    assert job.status == "partial"
    assert external_runs.load(f"{jid}-0")["status"] == "timeout"


def test_an_adopted_run_is_stopped_when_its_remaining_time_runs_out(runs):
    jid = "dead11ee0002"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=30, limit_s=60, deadline=time.time() + 1.5)
    _mirror(runs, jid)
    t0 = time.time()
    job = _adopt_and_wait(jid)
    took = time.time() - t0
    assert 1.0 <= took < 10, took
    assert proc.wait(10) is not None
    assert job.result["subagents"][0]["status"] == "timeout"


def test_an_adopted_run_with_time_left_finishes_normally(runs):
    jid = "dead11ee0003"
    _spawn_agent(runs, f"{jid}-0", seconds=0.5, limit_s=60, deadline=time.time() + 50)
    _mirror(runs, jid)
    job = _adopt_and_wait(jid)
    rep = job.result["subagents"][0]
    assert rep["status"] == "done" and rep["timed_out"] is False


def test_a_record_without_a_deadline_is_never_overdue():
    assert not external_runs.overdue({"pid": 1})
    assert not external_runs.overdue({"deadline": None})
    assert external_runs.overdue({"deadline": 5}, now=6) and not external_runs.overdue({"deadline": 5}, now=4)


def test_an_agent_that_failed_while_adopted_is_reported_as_an_error(runs):
    jid = "abcdef654321"
    body = ("import sys, json, time\ntime.sleep(float(sys.argv[1]))\n"
            "print(json.dumps({'type': 'turn.failed', 'error': 'model refused'}), flush=True)\n")
    _spawn_agent(runs, f"{jid}-0", seconds=0.8, body=body)
    _mirror(runs, jid)

    async def run():
        job = dispatch.get(jid)
        assert job.adopted_runs
        assert await dispatch.wait(job, 20)
        return job

    job = asyncio.run(run())
    rep = job.result["subagents"][0]
    assert job.status == "partial" and rep["status"] == "error" and "model refused" in rep["error"]
    assert job.result["exit_code"] == 1


def test_cancelling_an_adopted_job_stops_its_agent(runs):
    jid = "0123456789ab"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=30)
    _mirror(runs, jid)

    async def run():
        job = dispatch.get(jid)
        assert job.status == "running"
        await asyncio.sleep(0.3)
        assert dispatch.cancel(job) is True and job.status == "cancelling"
        assert await dispatch.wait(job, 15)
        return job

    job = asyncio.run(run())
    assert job.status == "cancelled" and "stopped" in job.verdict
    assert proc.wait(10) is not None
    assert job.result["subagents"][0]["status"] == "cancelled"
    assert external_runs.load(f"{jid}-0")["status"] == "cancelled"


def test_an_agent_that_ended_while_faustus_was_down_is_settled_from_its_log(runs):
    """No Faustus saw it end, but its log says how: the job is settled on the
    agent's own report instead of being written off as interrupted."""
    jid = "fedcba987654"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=0)
    proc.wait(10)
    _mirror(runs, jid)

    async def run():
        assert dispatch.adopt_live_jobs() == [jid]
        job = dispatch.get(jid)
        assert any("ended while Faustus was down" in str(e.get("message")) for e in job.events)
        assert await dispatch.wait(job, 10)
        return job

    job = asyncio.run(run())
    assert job.status == "partial" and "had ended while Faustus was down" in job.verdict
    assert job.result["subagents"][0]["status"] == "done"
    assert external_runs.load(f"{jid}-0")["finished"]


def test_a_job_with_no_unsettled_run_is_still_interrupted(runs):
    jid = "fedcba000000"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=0)
    proc.wait(10)
    external_runs.finish(f"{jid}-0", status="done", exit_code=0)   # a Faustus saw it end
    _mirror(runs, jid)
    assert dispatch.adopt_live_jobs() == []
    job = dispatch.get(jid)
    assert job.status == "interrupted" and not job.adopted_runs


def test_a_reused_pid_is_never_followed_or_killed(runs):
    """The record names a live pid with another creation time (a reboot gave
    the number to somebody else): that process is not ours. Nothing is
    followed or killed; the run is settled from its log, which says nothing
    conclusive, so the outcome is `unknown`, never a guessed done."""
    jid = "a1b2c3d4e5f6"
    proc = _spawn_agent(runs, f"{jid}-0", seconds=20)
    rec = external_runs.load(f"{jid}-0")
    rec["created"] = rec["created"] - 3600
    with open(os.path.join(external_runs.runs_dir(), f"{jid}-0.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    _mirror(runs, jid)
    assert external_runs.live_for_job(jid) == []

    async def run():
        job = dispatch.get(jid)
        dispatch.cancel(job)                       # even a cancel must not touch it
        assert await dispatch.wait(job, 10)
        return job

    job = asyncio.run(run())
    assert job.status in ("partial", "cancelled")
    assert job.result["subagents"][0]["status"] in ("unknown", "cancelled")
    assert proc.poll() is None, "never kill what was not proven to be ours"


def test_a_finished_job_with_a_live_record_is_left_alone(runs):
    jid = "aabbccddeeff"
    _spawn_agent(runs, f"{jid}-0", seconds=20)
    _mirror(runs, jid, status="done")
    assert dispatch.adopt_live_jobs() == []
    job = dispatch.get(jid)
    assert job.status == "done" and not job.adopted_runs


def test_an_adopted_job_reports_what_changed_since_its_checkpoint(runs, monkeypatch):
    """The checkpoint taken before the restart is in the mirror: the files the
    agent changed are observed against it, as for any job."""
    jid = "c0ffee000001"
    _spawn_agent(runs, f"{jid}-0", seconds=0.5)
    _mirror(runs, jid)
    path = runs.tmp / "dispatch" / f"{jid}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["checkpoint"] = "deadbeef"
    path.write_text(json.dumps(doc), encoding="utf-8")
    seen = []

    def fake_changes(ws, sha):
        seen.append(sha)
        return dispatch._changes_block(["cart.py"], [], [], "checkpoint")

    monkeypatch.setattr(dispatch, "_changes_since", fake_changes)

    async def run():
        job = dispatch.get(jid)
        assert await dispatch.wait(job, 20)
        return job

    job = asyncio.run(run())
    assert seen == ["deadbeef"]
    c = dispatch.compact(job)["result"]
    assert c["files_changed"] == ["cart.py"] and c["changes"]["source"] == "checkpoint"
