"""H11: one lifecycle for a process, its stdin and its output.

Real subprocesses (``sys.executable`` scripts, so the same tests run on
Windows). The store is redirected to a temporary file for every test.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time

import psutil
import pytest

from src import process_manager as pm
from src.process_manager import Caller, ProcessManager

PY = sys.executable
ME = Caller(owner="luis", session_id="s1")


def _argv(script: str):
    return [PY, "-u", "-c", script]


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_PROCESS_STORE", str(tmp_path / "handles.json"))
    values = {}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    manager = ProcessManager()
    manager.settings = values
    yield manager
    for handle, rec in list(manager._records.items()):
        if rec.get("state") in pm.LIVE_STATES:
            manager.stop(handle, Caller(owner=rec.get("owner", ""), session_id=rec.get("session_id", "")))


def _wait_state(mgr, handle, states, timeout=15.0, caller=ME):
    end = time.time() + timeout
    while time.time() < end:
        r = mgr.read(handle, caller)
        if r.get("state") in states:
            return r
        time.sleep(0.05)
    raise AssertionError(f"{handle} never reached {states}: {mgr.read(handle, caller)}")


def _alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


# ── output ─────────────────────────────────────────────────────────────────

def test_short_command_returns_its_output_and_a_handle(mgr):
    r = mgr.start(argv=_argv("print('hello')"), caller=ME, yield_ms=10_000)
    assert r["ok"] and r["completed"] and r["state"] == "exited"
    assert r["output"].strip() == "hello" and r["exit_code"] == 0 and r["handle"].startswith("ph_")


def test_long_command_returns_a_handle_while_running(mgr):
    r = mgr.start(argv=_argv("import time;print('up', flush=True);time.sleep(30)"), caller=ME, yield_ms=800)
    assert r["ok"] and not r["completed"] and r["state"] == "running"
    assert "up" in r["output"] and r["next_cursor"] >= 3


def test_big_output_is_bounded_and_the_cursor_reports_the_gap(mgr):
    mgr.settings[pm.SETTING_BUFFER] = 2048
    r = mgr.start(argv=_argv("import sys\nfor i in range(3000): sys.stdout.buffer.write(b'line %05d\\n' % i)"),
                  caller=ME, yield_ms=15_000)
    assert r["completed"] and r["exit_code"] == 0
    live = mgr._live[r["handle"]]
    assert len(live.buffer._buf) <= 2048, "buffer must stay within its cap"
    assert r["output_total_bytes"] == 3000 * 11
    again = mgr.read(r["handle"], ME, cursor=0)
    assert again["dropped_bytes"] == again["buffer_start"] > 0
    assert again["output"].rstrip().endswith("line 02999")


def test_reading_with_the_cursor_loses_nothing_when_keeping_up(mgr):
    script = "import time\nfor i in range(40):\n    print('n', i, flush=True); time.sleep(0.02)"
    r = mgr.start(argv=_argv(script), caller=ME, yield_ms=0)
    seen, cursor = "", 0
    end = time.time() + 15
    while time.time() < end:
        part = mgr.read(r["handle"], ME, cursor=cursor, wait_ms=500)
        seen += part["output"]
        cursor = part["next_cursor"]
        assert part["dropped_bytes"] == 0
        if part["state"] == "exited" and not part["more"]:
            break
    assert [line for line in seen.splitlines()] == [f"n {i}" for i in range(40)]


def test_a_read_never_splits_a_utf8_character(mgr):
    r = mgr.start(argv=_argv("import sys;sys.stdout.buffer.write(('é' * 500).encode());sys.stdout.flush()"),
                  caller=ME, yield_ms=10_000)
    handle, cursor, text = r["handle"], 0, ""
    while True:
        part = mgr.read(handle, ME, cursor=cursor, max_bytes=7)
        text += part["output"]
        if part["next_cursor"] == cursor:
            break
        cursor = part["next_cursor"]
    assert text == "é" * 500 and "�" not in text


def test_a_silent_process_stays_valid(mgr):
    r = mgr.start(argv=_argv("import time;time.sleep(2.5);print('done')"), caller=ME, yield_ms=300)
    time.sleep(1.2)
    quiet = mgr.read(r["handle"], ME)
    assert quiet["state"] == "running" and quiet["output"] == ""
    final = _wait_state(mgr, r["handle"], ("exited",))
    assert "done" in mgr.read(r["handle"], ME, cursor=0)["output"] and final["exit_code"] == 0


def test_cursor_reads_of_a_finished_handle_come_from_the_persisted_tail(mgr):
    r = mgr.start(argv=_argv("print('kept')"), caller=ME, yield_ms=10_000)
    mgr._live.pop(r["handle"])
    tail = mgr.read(r["handle"], ME, cursor=0)
    assert tail["buffer_source"] == "persisted_tail" and "kept" in tail["output"]


# ── the UI / the request does not own the process ──────────────────────────

def test_closing_the_calling_event_loop_does_not_stop_the_process(mgr):
    async def start():
        return await asyncio.to_thread(
            mgr.start, None, ME, argv=_argv("import time;time.sleep(30)"), yield_ms=100)

    handle = asyncio.run(start())["handle"]          # the loop that started it is gone now
    assert mgr.read(handle, ME)["state"] == "running"
    assert _alive(mgr._records[handle]["pid"])


def test_cancelling_the_tool_call_task_does_not_stop_the_process(mgr, monkeypatch):
    from src.agent_tools.process_tools import ProcessReadTool, ProcessStartTool
    monkeypatch.setattr(pm, "manager", lambda: mgr)
    ctx = {"owner": "luis", "session_id": "s1"}

    async def scenario():
        start = await ProcessStartTool().execute(
            {"argv": _argv("import time;time.sleep(30)"), "yield_ms": 100}, ctx)
        task = asyncio.ensure_future(ProcessReadTool().execute(
            {"handle": start["handle"], "wait_ms": 10_000}, ctx))
        await asyncio.sleep(0.2)
        task.cancel()                                   # the client went away mid-wait
        with pytest.raises(asyncio.CancelledError):
            await task
        return start["handle"]

    handle = asyncio.run(scenario())
    assert mgr.read(handle, ME)["state"] == "running"


# ── stdin and permissions ──────────────────────────────────────────────────

_ECHO = "import sys\nfor line in sys.stdin:\n    print('got:' + line.strip(), flush=True)"


def test_stdin_round_trip_and_close(mgr):
    r = mgr.start(argv=_argv(_ECHO), caller=ME, yield_ms=200)
    assert mgr.write_stdin(r["handle"], ME, data="hello\n")["written"] == 6
    out = mgr.read(r["handle"], ME, cursor=0, wait_ms=5000)
    assert "got:hello" in out["output"]
    assert mgr.write_stdin(r["handle"], ME, data="", close_stdin=True)["stdin_closed"] is True
    _wait_state(mgr, r["handle"], ("exited",))
    refused = mgr.write_stdin(r["handle"], ME, data="late\n")
    assert refused["ok"] is False and refused["code"] == "not_running"


def test_another_session_or_owner_cannot_use_the_handle(mgr):
    r = mgr.start(argv=_argv(_ECHO), caller=ME, yield_ms=100)
    for other in (Caller(owner="luis", session_id="s2"), Caller(owner="eve", session_id="s1")):
        for result in (mgr.read(r["handle"], other), mgr.write_stdin(r["handle"], other, data="x\n"),
                       mgr.stop(r["handle"], other)):
            assert result["ok"] is False and result["code"] == "not_owner"
    assert mgr.read(r["handle"], ME)["state"] == "running"
    assert mgr.list(Caller(owner="luis", session_id="s2"))["handles"] == []


def test_permission_is_rechecked_on_every_write(mgr):
    r = mgr.start(argv=_argv(_ECHO), caller=ME, yield_ms=100)
    assert mgr.write_stdin(r["handle"], ME, data="a\n")["ok"]
    disabled = Caller(owner="luis", session_id="s1", disabled_tools=frozenset({"process_write_stdin"}))
    assert mgr.write_stdin(r["handle"], disabled, data="b\n")["code"] == "tool_disabled"
    mgr.settings[pm.SETTING_ENABLED] = False
    assert mgr.write_stdin(r["handle"], ME, data="c\n")["code"] == "disabled"
    mgr.settings[pm.SETTING_ENABLED] = True
    pm.permission_hook = lambda rec, caller, action: (False, "vetoed by policy")
    try:
        assert mgr.write_stdin(r["handle"], ME, data="d\n")["error"] == "vetoed by policy"
    finally:
        pm.permission_hook = None
    assert "got:a" in mgr.read(r["handle"], ME, cursor=0, wait_ms=3000)["output"]
    assert "got:b" not in mgr.read(r["handle"], ME, cursor=0)["output"]


def test_confinement_becoming_required_blocks_stdin_to_a_host_process(mgr, monkeypatch):
    from src import sandbox_exec
    r = mgr.start(argv=_argv(_ECHO), caller=ME, yield_ms=100)
    values = {"agent_sandbox_execution": True, "agent_sandbox_mode": "required"}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    blocked = mgr.write_stdin(r["handle"], ME, data="x\n")
    assert blocked["ok"] is False and blocked["code"] == "confinement_required"
    assert sandbox_exec.confinement_required()


@pytest.mark.parametrize("mode,windows,refused", [("required", False, True), ("required", True, True),
                                                   ("strict", False, True), ("strict", True, False),
                                                   ("auto", False, False)])
def test_start_is_refused_not_redirected_when_confinement_requires_a_container(
        mgr, monkeypatch, mode, windows, refused):
    from src import sandbox_exec
    values = {"agent_sandbox_execution": True, "agent_sandbox_mode": mode}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: windows)
    started = []
    real = subprocess.Popen
    monkeypatch.setattr(pm.subprocess, "Popen", lambda *a, **k: started.append(a) or real(*a, **k))
    r = mgr.start(argv=_argv("print(1)"), caller=ME, yield_ms=5000)
    if refused:
        assert r["ok"] is False and r["code"] == "confinement_required" and r["exit_code"] == 126
        assert r["effective_policy"] == "not_executed" and started == []
    else:
        assert r["ok"] is True and r["effective_policy"] == "host"
        assert "host" in r["requested_policy"] or r["requested_policy"].startswith("sandbox_")


def test_handle_limit_and_bad_requests(mgr):
    mgr.settings[pm.SETTING_MAX_HANDLES] = 2
    sleeper = _argv("import time;time.sleep(30)")
    assert mgr.start(argv=sleeper, caller=ME, yield_ms=0)["ok"]
    assert mgr.start(argv=sleeper, caller=ME, yield_ms=0)["ok"]
    third = mgr.start(argv=sleeper, caller=ME, yield_ms=0)
    assert third["ok"] is False and third["code"] == "too_many"
    assert mgr.start(caller=ME)["code"] == "bad_request"
    mgr.settings[pm.SETTING_MAX_HANDLES] = 16
    missing = mgr.start(argv=["definitely-not-a-program-xyz"], caller=ME)
    assert missing["code"] == "launch_failed" and missing["state"] == "failed_to_start"
    assert mgr.read(missing["handle"], ME)["termination_reason"].startswith("launch_failed")


# ── stop ───────────────────────────────────────────────────────────────────

_SPAWNS_CHILD = ("import subprocess, sys, time\n"
                 "child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(300)'])\n"
                 "print(child.pid, flush=True)\n"
                 "time.sleep(300)\n")


def test_stop_kills_the_process_and_its_own_children(mgr):
    r = mgr.start(argv=_argv(_SPAWNS_CHILD), caller=ME, yield_ms=2000)
    child_pid = int(r["output"].split()[0])
    root_pid = mgr._records[r["handle"]]["pid"]
    assert _alive(child_pid)
    stopped = mgr.stop(r["handle"], ME, reason="test")
    assert stopped["ok"] and stopped["state"] == "stopped" and stopped["signalled"]
    end = time.time() + 10
    while time.time() < end and (_alive(child_pid) or _alive(root_pid)):
        time.sleep(0.1)
    assert not _alive(child_pid) and not _alive(root_pid)
    info = mgr.read(r["handle"], ME)
    assert info["state"] == "stopped" and info["termination_reason"] == "stopped_by_request: test"


def test_stop_never_touches_an_unrelated_process_with_the_same_program(mgr):
    bystander = subprocess.Popen([PY, "-u", "-c", "import time;time.sleep(300)"])
    try:
        r = mgr.start(argv=[PY, "-u", "-c", "import time;time.sleep(300)"], caller=ME, yield_ms=0)
        mgr.stop(r["handle"], ME)
        time.sleep(0.5)
        assert bystander.poll() is None, "a process that only shares a name must survive"
        # and there is no way to address it: handles only.
        assert mgr.stop("ph_000000000000000", ME)["code"] == "unknown_handle"
        assert mgr.stop(str(bystander.pid), ME)["code"] == "unknown_handle"
    finally:
        bystander.kill()
        bystander.wait()


def test_a_recycled_pid_is_not_signalled(mgr):
    bystander = subprocess.Popen([PY, "-u", "-c", "import time;time.sleep(300)"])
    try:
        mgr._load()
        mgr._records["ph_recycled"] = {
            "handle": "ph_recycled", "kind": "managed", "state": "orphaned", "owner": "luis",
            "session_id": "s1", "pid": bystander.pid, "pid_created_at": time.time() - 99999,
            "pgid": None, "started_at": time.time() - 99999, "tail": ""}
        result = mgr.stop("ph_recycled", ME)
        assert result["ok"] is False and not result["signalled"]
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_max_runtime_ends_the_process_with_a_reason(mgr):
    r = mgr.start(argv=_argv(_SPAWNS_CHILD), caller=ME, yield_ms=1500, max_runtime_s=1)
    child_pid = int(r["output"].split()[0]) if r["output"] else None
    info = _wait_state(mgr, r["handle"], ("timed_out",))
    assert info["termination_reason"].startswith("max_runtime_exceeded")
    if child_pid:
        end = time.time() + 10
        while time.time() < end and _alive(child_pid):
            time.sleep(0.1)
        assert not _alive(child_pid)


def test_cancel_for_session_stops_only_that_sessions_handles(mgr):
    mine = mgr.start(argv=_argv("import time;time.sleep(300)"), caller=ME, yield_ms=0)
    other_caller = Caller(owner="luis", session_id="s2")
    other = mgr.start(argv=_argv("import time;time.sleep(300)"), caller=other_caller, yield_ms=0)
    stopped = mgr.cancel_for_session("s1")
    assert [row["handle"] for row in stopped] == [mine["handle"]]
    assert mgr.read(other["handle"], other_caller)["state"] == "running"


# ── restart: handles survive as records, never re-spawned ──────────────────

def test_after_a_restart_a_live_process_is_orphaned_and_a_dead_one_is_lost(tmp_path, monkeypatch, mgr):
    alive = mgr.start(argv=_argv("import time;time.sleep(300)"), caller=ME, yield_ms=200)
    doomed = mgr.start(argv=_argv("import time;time.sleep(300)"), caller=ME, yield_ms=200)
    doomed_pid = mgr._records[doomed["handle"]]["pid"]
    psutil.Process(doomed_pid).kill()                     # died while the server was down
    end = time.time() + 5
    while time.time() < end and mgr._records[doomed["handle"]]["state"] == "running":
        time.sleep(0.05)
    mgr._records[doomed["handle"]]["state"] = "running"   # as the previous server last wrote it
    mgr._records[doomed["handle"]].pop("exit_code", None)
    mgr._save_locked()

    spawned = []
    monkeypatch.setattr(pm.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    restarted = ProcessManager()                           # a fresh interpreter: no live handles
    rows = {r["handle"]: r for r in restarted.list(ME)["handles"]}
    assert rows[alive["handle"]]["state"] == "orphaned"
    assert rows[doomed["handle"]]["state"] == "lost"
    assert rows[doomed["handle"]]["outcome_unknown"] is True
    assert "exit status is unknown" in rows[doomed["handle"]]["termination_reason"]
    assert spawned == [], "a record must never be re-spawned"
    read = restarted.read(doomed["handle"], ME)
    assert read["state"] == "lost" and read["outcome_unknown"] and read["uncertainty"]["reconcile_action"]
    assert restarted.write_stdin(alive["handle"], ME, data="x")["code"] == "not_running"
    # the orphan is still ours to stop, proven by pid + creation time
    monkeypatch.undo()
    stop = restarted.stop(alive["handle"], ME)
    assert stop["signalled"] is True
    end = time.time() + 10
    while time.time() < end and _alive(mgr._records[alive["handle"]]["pid"]):
        time.sleep(0.1)
    assert not _alive(mgr._records[alive["handle"]]["pid"])


# ── remote runners ─────────────────────────────────────────────────────────

def test_remote_disconnect_makes_running_remote_handles_uncertain(mgr):
    a = mgr.register_remote(ME, "make build", "runner-1")
    b = mgr.register_remote(ME, "make test", "runner-1")
    c = mgr.register_remote(ME, "make docs", "runner-2")
    mgr.remote_finished(b["handle"], 0, "ok")
    changed = mgr.remote_disconnected("runner-1")
    assert changed == [a["handle"]]
    read = mgr.read(a["handle"], ME)
    assert read["state"] == "uncertain" and read["outcome_unknown"] is True
    assert "remote_disconnected" in read["termination_reason"]
    assert mgr.read(b["handle"], ME)["state"] == "exited"
    assert mgr.read(c["handle"], ME)["state"] == "running"
    assert mgr.write_stdin(a["handle"], ME, data="x")["code"] == "not_running"


# ── background jobs are handles too ────────────────────────────────────────

@pytest.fixture
def bg(tmp_path, monkeypatch):
    from src import bg_jobs
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "bg")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    return bg_jobs


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script")
def test_a_background_job_is_listed_read_and_stopped_through_its_handle(mgr, bg, tmp_path):
    rec = bg.launch("echo first; sleep 30", session_id="s1", cwd=str(tmp_path))
    handle = f"bg:{rec['id']}"
    end = time.time() + 10
    while time.time() < end and "first" not in mgr.read(handle, ME).get("output", ""):
        time.sleep(0.1)
    rows = {r["handle"]: r for r in mgr.list(ME)["handles"]}
    assert rows[handle]["kind"] == "bg_job" and rows[handle]["state"] == "running"
    read = mgr.read(handle, ME)
    assert "first" in read["output"] and read["buffer_source"] == "job_log_file"
    assert mgr.read(handle, Caller(owner="luis", session_id="other"))["code"] == "not_owner"
    assert mgr.write_stdin(handle, ME, data="x")["code"] == "no_stdin"
    stop = mgr.stop(handle, ME)
    assert stop["signalled"] is True and stop["state"] == "stopped"
    assert mgr.list(ME)["handles"][0]["termination_reason"] == "stopped_by_request"


# ── Windows containment wiring (no Windows needed to check the call order) ──

def test_windows_containment_uses_a_kill_on_close_job_and_closes_it_before_the_tree_kill(mgr, monkeypatch):
    from types import SimpleNamespace
    from src.code_mode import windows_job_bootstrap as wjb
    events = []

    class Job:
        handle = 77
        name = "job"

        def close(self):
            events.append("job_closed")

    kernel = SimpleNamespace(AssignProcessToJobObject=lambda job, proc: events.append(("assign", job, proc)) or 1)
    monkeypatch.setattr(pm, "IS_WINDOWS", True)
    monkeypatch.setattr(wjb, "create_owned_job", lambda: Job())
    monkeypatch.setattr(wjb, "_kernel", lambda: kernel)
    live = pm._Live(SimpleNamespace(poll=lambda: None), pm.OutputBuffer(1024))
    assert mgr._contain(live, SimpleNamespace(_handle=1234)) == "job_object"
    assert events == [("assign", 77, 1234)] and live.job is not None
    monkeypatch.setattr(pm.process_ownership, "terminate_tree",
                        lambda *a, **k: events.append("tree_kill") or pm.process_ownership.TreeKill())
    mgr._kill({"pid": 1, "pid_created_at": None, "pgid": None}, live)
    assert events[1:] == ["job_closed", "tree_kill"]


# ── the tools ──────────────────────────────────────────────────────────────

def test_tools_round_trip(mgr, monkeypatch):
    from src.agent_tools.process_tools import (
        ProcessListTool, ProcessReadTool, ProcessStartTool, ProcessStopTool, ProcessWriteStdinTool)
    monkeypatch.setattr(pm, "manager", lambda: mgr)
    ctx = {"owner": "luis", "session_id": "s1"}

    async def scenario():
        start = await ProcessStartTool().execute({"argv": _argv(_ECHO), "yield_ms": 200}, ctx)
        assert start["exit_code"] == 0 and "still running" in start["output"]
        handle = start["handle"]
        assert (await ProcessWriteStdinTool().execute({"handle": handle, "data": "hi\n"}, ctx))["exit_code"] == 0
        read = await ProcessReadTool().execute({"handle": handle, "cursor": 0, "wait_ms": 5000}, ctx)
        assert "got:hi" in read["output"]
        listed = await ProcessListTool().execute({}, ctx)
        assert handle in listed["output"]
        foreign = await ProcessReadTool().execute({"handle": handle}, {"owner": "luis", "session_id": "zz"})
        assert foreign["exit_code"] == 1 and "another session" in foreign["error"]
        missing = await ProcessStopTool().execute({}, ctx)
        assert missing["exit_code"] == 1
        stopped = await ProcessStopTool().execute({"handle": handle}, ctx)
        assert stopped["exit_code"] == 0 and "stopped" in stopped["output"]

    asyncio.run(scenario())


def test_the_handle_store_is_written_atomically_and_holds_no_output_beyond_the_tail(mgr, tmp_path):
    r = mgr.start(argv=_argv("print('x' * 100000)"), caller=ME, yield_ms=10_000)
    import json
    data = json.loads((tmp_path / "handles.json").read_text())
    rec = data[r["handle"]]
    assert len(rec["tail"]) <= pm.TAIL_PERSIST_BYTES and rec["output_total_bytes"] > 100000
    assert rec["owner"] == "luis" and rec["env_policy"].startswith("native_host_environment")
    assert rec["command"] and rec["state"] == "exited"
