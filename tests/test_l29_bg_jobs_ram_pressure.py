"""L29 (integrates L27's "necessary in bg_jobs.py" note): `bg_jobs.launch()`
must not spawn another process into a box already under RAM/commit pressure
(src/bg_monitor.py, PERF-04) — but it must not DISCARD the command either
(COMUN.md rule 3: no capability lost). It queues the job instead
(`status='queued'`, nothing spawned) and `refresh()` starts it on a later
poll once `bg_monitor.is_paused_for_resource_pressure()` clears — the same
"deferred, not dropped" pattern `bg_monitor._loop` already uses for
follow-ups (see tests/test_hw_bg_monitor_ram_pressure.py).

Drives the real `bg_monitor.is_paused_for_resource_pressure()` guard through
its injectable memory reader (no mocking of bg_jobs' own decision logic) and
a real subprocess for the "pressure clears" path, per COMUN.md's own-module
testing expectations; only the process spawn itself is faked where a test
needs to assert it was/was not invoked.
"""
from __future__ import annotations

import time

import pytest

from src import bg_jobs, bg_monitor


def _reader(available_gb, total_gb):
    return lambda: (int(available_gb * 2**30), int(total_gb * 2**30))


@pytest.fixture(autouse=True)
def _clean_pressure():
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()
    yield
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "bg_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    return jobs_dir


class _FakePopen:
    def __init__(self, *a, **kw):
        self.pid = 4321


def test_launch_queues_instead_of_spawning_under_pressure(store, monkeypatch):
    bg_monitor.set_memory_reader(_reader(1, 32))  # 31/32 ~ 0.97: critical

    def _boom(*a, **kw):
        raise AssertionError("launch() must not spawn a process while RAM pressure is critical")
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", _boom)

    rec = bg_jobs.launch("echo hi", session_id="sess-a")

    assert rec["status"] == "queued"
    assert rec["pid"] is None
    assert rec["queued_for_pressure"] is True
    assert rec["command"] == "echo hi"  # the command survives, ready to run later
    reloaded = bg_jobs._load()[rec["id"]]
    assert reloaded["status"] == "queued"


def test_refresh_launches_a_queued_job_once_pressure_clears(store, monkeypatch):
    bg_monitor.set_memory_reader(_reader(1, 32))  # critical

    def _boom(*a, **kw):
        raise AssertionError("must not spawn while still under pressure")
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", _boom)
    rec = bg_jobs.launch("echo hi", session_id="sess-a")
    assert rec["status"] == "queued"

    # Pressure clears (below RAM_PRESSURE_LOW) -> the next poll starts it for
    # real, through the exact same spawn path launch() itself uses.
    bg_monitor.set_memory_reader(_reader(30, 32))  # ~0.06 used
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **kw: _FakePopen())
    monkeypatch.setattr(bg_jobs.process_ownership, "creation_time", lambda pid: 999.0)
    monkeypatch.setattr(bg_jobs.process_ownership, "process_group_id", lambda pid: 42)
    # The fake pid is not a real process on this machine; without this the
    # same refresh() pass that just started it would immediately reconcile it
    # as "died" (see refresh()'s liveness check) — a fixture artifact, not
    # the behavior under test.
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: True)

    jobs = bg_jobs.refresh()

    assert jobs[rec["id"]]["status"] == "running"
    assert jobs[rec["id"]]["pid"] == 4321
    assert jobs[rec["id"]]["started_at"] is not None
    reloaded = bg_jobs._load()[rec["id"]]
    assert reloaded["status"] == "running", "the transition must be persisted, not just returned"


def test_refresh_leaves_a_queued_job_alone_while_pressure_persists(store, monkeypatch):
    bg_monitor.set_memory_reader(_reader(1, 32))  # critical

    def _boom(*a, **kw):
        raise AssertionError("must not spawn while pressure has not cleared")
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", _boom)

    rec = bg_jobs.launch("echo hi", session_id="sess-a")
    jobs = bg_jobs.refresh()  # still critical: must not touch _boom

    assert jobs[rec["id"]]["status"] == "queued"


def test_killing_a_queued_job_marks_it_failed_without_touching_a_process(store, monkeypatch):
    bg_monitor.set_memory_reader(_reader(1, 32))  # critical

    def _boom(*a, **kw):
        raise AssertionError("a queued job has no process to spawn or signal")
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", _boom)

    rec = bg_jobs.launch("echo hi", session_id="sess-a")
    killed = bg_jobs.kill(rec["id"])

    assert killed["status"] == "failed"
    assert killed["killed"] is True
    assert killed["followed_up"] is True
    # No misleading "process ownership" message: nothing was ever spawned.
    assert "kill_refused" not in killed
