"""PERF-04 / QA-26 · RAM/commit pressure pauses auxiliary work before the cascade.

08-09-2026 the machine ran out of *commit* memory — two spilling 27B models
plus whatever auxiliary work (an embeddings pass, an audit, a research run)
kept drawing on it — and nothing watched that number or backed off. This pins
`src.bg_monitor`'s guard: `ram_pressure()` reads free/total RAM through an
injectable reader (no psutil, no real host state — a fake reader is enough),
`is_paused_for_resource_pressure()` turns that into a hysteresis-gated
pause/resume decision, and the follow-up loop (`_loop`) consults it every tick
— skipping an auxiliary background-job continuation instead of starting one,
the same way it already skips them during shutdown, and for the same reason:
`followed_up` stays False, so state is conserved and nothing is lost, only
deferred.
"""
from __future__ import annotations

import asyncio

import pytest

from src import bg_monitor


@pytest.fixture(autouse=True)
def _clean():
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()
    yield
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()


def _reader(available_gb, total_gb):
    return lambda: (int(available_gb * 2**30), int(total_gb * 2**30))


# ── the reading itself, through a mocked reader ─────────────────────────────

def test_ram_pressure_reads_through_the_injected_reader():
    bg_monitor.set_memory_reader(_reader(2, 32))  # 30/32 used ≈ 0.94
    p = bg_monitor.ram_pressure()
    assert p["available_bytes"] == 2 * 2**30
    assert p["total_bytes"] == 32 * 2**30
    assert p["used_fraction"] == pytest.approx(30 / 32, rel=1e-6)
    assert p["critical"] is True


def test_ram_pressure_is_unknown_not_critical_when_the_reader_fails():
    def _boom():
        raise OSError("no /proc/meminfo here")
    bg_monitor.set_memory_reader(_boom)
    p = bg_monitor.ram_pressure()
    assert p["used_fraction"] is None
    assert p["critical"] is False  # ignorance never pauses everything


def test_ram_pressure_is_unknown_when_the_reader_reports_no_total():
    bg_monitor.set_memory_reader(lambda: (0, 0))
    p = bg_monitor.ram_pressure()
    assert p["used_fraction"] is None
    assert p["critical"] is False


# ── hysteresis: pause at HIGH, only clear once back under LOW ──────────────

def test_pause_has_hysteresis_not_a_bare_threshold():
    bg_monitor.set_memory_reader(_reader(10, 100))   # 90% used: trips HIGH
    assert bg_monitor.is_paused_for_resource_pressure() is True

    bg_monitor.set_memory_reader(_reader(15, 100))   # 85% used: between LOW/HIGH
    assert bg_monitor.is_paused_for_resource_pressure() is True  # still paused

    bg_monitor.set_memory_reader(_reader(21, 100))   # 79% used: under LOW
    assert bg_monitor.is_paused_for_resource_pressure() is False

    bg_monitor.set_memory_reader(_reader(15, 100))   # 85% again: not yet HIGH
    assert bg_monitor.is_paused_for_resource_pressure() is False  # stays resumed


def test_unknown_reading_holds_the_last_known_state():
    bg_monitor.set_memory_reader(_reader(10, 100))   # trips HIGH
    assert bg_monitor.is_paused_for_resource_pressure() is True

    def _boom():
        raise OSError("gone")
    bg_monitor.set_memory_reader(_boom)
    assert bg_monitor.is_paused_for_resource_pressure() is True  # holds, doesn't guess


# ── conserved state: which jobs are parked, and pressure_state() ───────────

def test_note_paused_and_resumed_conserve_which_jobs_are_parked():
    assert bg_monitor.paused_job_ids() == []
    bg_monitor.note_paused("job-1", kind="research_followup")
    assert bg_monitor.paused_job_ids() == ["job-1"]
    bg_monitor.note_resumed("job-1")
    assert bg_monitor.paused_job_ids() == []


def test_pressure_state_reports_the_reading_and_the_parked_jobs():
    bg_monitor.set_memory_reader(_reader(5, 100))  # 95% used
    bg_monitor.note_paused("job-9", kind="bg_job_followup")
    state = bg_monitor.pressure_state()
    assert state["paused"] is True
    assert state["critical"] is True
    assert {j["job_id"] for j in state["paused_jobs"]} == {"job-9"}


# ── wired into the follow-up loop: a real tick actually defers work ────────

async def _run_one_tick(monkeypatch, *, pending, ran):
    """Drive bg_monitor._loop for exactly one iteration and stop it, without
    waiting out the real 5s POLL_INTERVAL_S."""
    monkeypatch.setattr(bg_monitor.bg_jobs, "pending_followups", lambda: list(pending))
    monkeypatch.setattr(bg_monitor.bg_jobs, "mark_followed_up", lambda jid: ran.append(("marked", jid)))

    async def _fake_followup(rec):
        ran.append(("ran", rec["id"]))
        return True
    monkeypatch.setattr(bg_monitor, "_run_followup", _fake_followup)

    stop = asyncio.Event()
    task = asyncio.create_task(bg_monitor._loop(stop))
    # Let the first iteration's synchronous work (pending_followups + the
    # pause/skip decision, or the awaited followup) actually run.
    for _ in range(20):
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_the_loop_defers_a_followup_under_ram_pressure(monkeypatch):
    bg_monitor.set_memory_reader(_reader(2, 32))  # critical
    ran = []
    await _run_one_tick(monkeypatch, pending=[{"id": "job-1", "status": "done"}], ran=ran)
    assert ran == []  # never invoked, and never marked followed_up
    assert "job-1" in bg_monitor.paused_job_ids()


async def test_the_loop_runs_a_followup_when_ram_is_fine(monkeypatch):
    bg_monitor.set_memory_reader(_reader(20, 32))  # plenty free
    ran = []
    await _run_one_tick(monkeypatch, pending=[{"id": "job-1", "status": "done"}], ran=ran)
    assert ("ran", "job-1") in ran
    assert ("marked", "job-1") in ran
    assert bg_monitor.paused_job_ids() == []


async def test_the_loop_resumes_once_pressure_clears(monkeypatch):
    bg_monitor.set_memory_reader(_reader(2, 32))  # critical: pause it
    ran = []
    await _run_one_tick(monkeypatch, pending=[{"id": "job-1", "status": "done"}], ran=ran)
    assert ran == []
    assert bg_monitor.paused_job_ids() == ["job-1"]

    bg_monitor.set_memory_reader(_reader(85, 100))  # 15% used: well under LOW
    ran2 = []
    await _run_one_tick(monkeypatch, pending=[{"id": "job-1", "status": "done"}], ran=ran2)
    assert ("ran", "job-1") in ran2
    assert bg_monitor.paused_job_ids() == []
