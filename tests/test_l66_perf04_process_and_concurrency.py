"""L66/PERF-04 — Faustus's own resource footprint, and an explicit admission
ceiling for CPU-heavy work, end to end.

Three things pinned here:

  1. `routes/system_usage_routes.py::_collect_process` reports THIS process's
     own RSS/thread count/fd(or handle)-count via `psutil.Process()` — not
     the whole-machine gauge `_collect_host` already had — and `collect_usage`
     wires it into the document under `"process"`.
  2. `src/bg_jobs.py` gets an explicit, configurable concurrent-session
     ceiling (0 = unlimited): at capacity, a new job is queued
     (`queued_for_concurrency: True`) rather than spawned, and — the
     "prueba de presión" the backlog asks for — a job already running is
     never touched, never killed, just left alone while the new one waits.
  3. the shared `cpu_heavy` resource (EXEC-04) that `src/project_tests.py`
     and (per this lote's own notes) research/builds are meant to draw from
     together: an explicit ceiling, atomic acquire, and a `#!bg` job that is
     actually running counts against the same budget for free.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import routes.system_usage_routes as sur
from src import bg_jobs


@pytest.fixture(autouse=True)
def _clean_cpu_heavy():
    with bg_jobs._CPU_HEAVY_LOCK:
        bg_jobs._CPU_HEAVY_HOLDERS.clear()
    yield
    with bg_jobs._CPU_HEAVY_LOCK:
        bg_jobs._CPU_HEAVY_HOLDERS.clear()


# ── 1. Faustus's own process footprint ──────────────────────────────────────


def test_collect_process_reports_this_process_rss_and_threads():
    out = sur._collect_process()
    assert "error" not in out, out.get("error")
    assert out["pid"] > 0
    assert out["rss_bytes"] > 0
    assert out["num_threads"] >= 1
    # Exactly one of the two platform-specific handle counts is present —
    # POSIX reports num_fds, Windows reports num_handles, never both.
    assert ("num_fds" in out) != ("num_handles" in out)


def test_collect_process_never_raises_when_psutil_is_unavailable(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil in this test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    out = sur._collect_process()
    assert "error" in out
    assert "rss_bytes" not in out


@pytest.fixture
def usage_box(monkeypatch):
    async def _ollama(client):
        return {"reachable": False, "base": "http://127.0.0.1:11434", "error": "test", "models": []}

    monkeypatch.setattr(sur, "_collect_ollama", _ollama)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: ([], None))
    monkeypatch.setattr(sur, "_collect_host", lambda: {"cpu": {"percent": 1.0, "count": 8},
                                                        "ram": {"used": 1, "total": 2, "percent": 50.0}})
    monkeypatch.setattr(sur.gpu_shared_memory, "collect", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(sur, "_collect_policy", lambda: {"exposed": False})
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    yield
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None


def test_collect_usage_carries_the_process_block(usage_box):
    data = asyncio.run(sur.collect_usage())
    assert "process" in data
    assert data["process"]["pid"] == sur._collect_process()["pid"]
    assert data["process"]["rss_bytes"] > 0


# ── 2. bg_jobs: explicit, configurable concurrency ceiling ─────────────────


class _FakePopen:
    _next_pid = 9000

    def __init__(self, *a, **kw):
        _FakePopen._next_pid += 1
        self.pid = _FakePopen._next_pid


@pytest.fixture
def store(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "bg_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_is_paused_for_resource_pressure", lambda: False)
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **kw: _FakePopen())
    monkeypatch.setattr(bg_jobs.process_ownership, "creation_time", lambda pid: 999.0)
    monkeypatch.setattr(bg_jobs.process_ownership, "process_group_id", lambda pid: 42)
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: True)
    return jobs_dir


def test_default_zero_means_unlimited(store, monkeypatch):
    monkeypatch.setattr(bg_jobs, "_max_concurrent_jobs", lambda: 0)
    a = bg_jobs.launch("echo a", session_id="s")
    b = bg_jobs.launch("echo b", session_id="s")
    assert a["status"] == "running"
    assert b["status"] == "running"


def test_at_the_ceiling_a_new_job_is_queued_not_spawned_or_dropped(store, monkeypatch):
    monkeypatch.setattr(bg_jobs, "_max_concurrent_jobs", lambda: 1)

    first = bg_jobs.launch("echo first", session_id="s")
    assert first["status"] == "running"

    kill_calls = []
    monkeypatch.setattr(bg_jobs, "_kill_job", lambda rec: kill_calls.append(rec["id"]))

    second = bg_jobs.launch("echo second", session_id="s")

    assert second["status"] == "queued"
    assert second["queued_for_concurrency"] is True
    assert second["queued_for_pressure"] is False
    assert second["command"] == "echo second"  # capability preserved, not dropped
    # "prueba de presión que aplaza trabajo auxiliar sin matar procesos
    # ajenos": the FIRST job (already running, unrelated to this admission
    # decision) is never touched — no kill, no signal — it just keeps running
    # while the second one waits its turn.
    assert kill_calls == []
    jobs = bg_jobs._load()
    assert jobs[first["id"]]["status"] == "running"
    assert jobs[first["id"]]["pid"] == first["pid"]


def test_refresh_starts_a_queued_job_once_a_slot_frees_up(store, monkeypatch):
    monkeypatch.setattr(bg_jobs, "_max_concurrent_jobs", lambda: 1)
    first = bg_jobs.launch("echo first", session_id="s")
    second = bg_jobs.launch("echo second", session_id="s")
    assert second["status"] == "queued"

    # The first job finishes (its exit file appears) — refresh() reconciles
    # it to "done" and, in the SAME pass, promotes the queued one now that a
    # slot is free.
    from pathlib import Path
    Path(first["exit_path"]).write_text("0", encoding="utf-8")

    jobs = bg_jobs.refresh()

    assert jobs[first["id"]]["status"] == "done"
    assert jobs[second["id"]]["status"] == "running"
    assert jobs[second["id"]]["queued_for_concurrency"] is False


def test_refresh_leaves_extra_queued_jobs_alone_until_their_own_slot_frees(store, monkeypatch):
    monkeypatch.setattr(bg_jobs, "_max_concurrent_jobs", lambda: 1)
    first = bg_jobs.launch("echo first", session_id="s")
    second = bg_jobs.launch("echo second", session_id="s")
    third = bg_jobs.launch("echo third", session_id="s")
    assert second["status"] == "queued"
    assert third["status"] == "queued"

    from pathlib import Path
    Path(first["exit_path"]).write_text("0", encoding="utf-8")
    jobs = bg_jobs.refresh()

    # Only ONE slot freed: only one of the two queued jobs may start.
    started = [j for j in (jobs[second["id"]], jobs[third["id"]]) if j["status"] == "running"]
    still_queued = [j for j in (jobs[second["id"]], jobs[third["id"]]) if j["status"] == "queued"]
    assert len(started) == 1
    assert len(still_queued) == 1


# ── 3. EXEC-04: the shared cpu_heavy resource ───────────────────────────────


def test_cpu_heavy_try_acquire_is_atomic_under_concurrent_threads(monkeypatch, store):
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i):
        barrier.wait()
        results[i] = bg_jobs.try_acquire_cpu_heavy("test_suite", owner=f"w{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([r for r in results if r is not None]) == 1


def test_cpu_heavy_release_frees_the_slot_for_the_next_caller(monkeypatch, store):
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)
    t1 = bg_jobs.try_acquire_cpu_heavy("research", owner="r1")
    assert t1 is not None
    assert bg_jobs.try_acquire_cpu_heavy("test_suite", owner="t1") is None  # no room yet

    bg_jobs.release_cpu_heavy(t1)
    t2 = bg_jobs.try_acquire_cpu_heavy("test_suite", owner="t1")
    assert t2 is not None
    bg_jobs.release_cpu_heavy(t2)


def test_a_running_bg_job_counts_against_the_shared_cpu_heavy_budget(store, monkeypatch):
    """The literal EXEC-04 acceptance: a local research run must not be able
    to also fight several test suites and a build for the same budget — a
    `#!bg` build already counts without needing its own explicit ticket."""
    monkeypatch.setattr(bg_jobs, "_max_concurrent_jobs", lambda: 0)  # bg_jobs' own ceiling stays off
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)

    build = bg_jobs.launch("make build", session_id="s")
    assert build["status"] == "running"

    # A "research" caller asking for the shared budget finds no room: the
    # running build already spent the one slot.
    assert bg_jobs.try_acquire_cpu_heavy("research", owner="deep_research") is None
    assert bg_jobs.cpu_heavy_active_count() == 1


def test_acquire_cpu_heavy_sync_waits_then_succeeds_once_a_slot_frees(monkeypatch, store):
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)
    holder = bg_jobs.try_acquire_cpu_heavy("build", owner="b1")
    assert holder is not None

    def _release_soon():
        time.sleep(0.05)
        bg_jobs.release_cpu_heavy(holder)

    threading.Thread(target=_release_soon).start()
    ticket = bg_jobs.acquire_cpu_heavy_sync("test_suite", owner="t1", timeout=2.0, poll_interval=0.02)
    assert ticket is not None
    bg_jobs.release_cpu_heavy(ticket)


@pytest.mark.asyncio
async def test_acquire_cpu_heavy_async_times_out_without_starving_forever(monkeypatch, store):
    monkeypatch.setattr(bg_jobs, "cpu_heavy_max_concurrent", lambda: 1)
    holder = bg_jobs.try_acquire_cpu_heavy("research", owner="r1")
    assert holder is not None
    with pytest.raises(TimeoutError):
        await bg_jobs.acquire_cpu_heavy("test_suite", owner="t1", timeout=0.05, poll_interval=0.01)
    bg_jobs.release_cpu_heavy(holder)
