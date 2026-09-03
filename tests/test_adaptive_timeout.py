"""Adaptive idle timeout (src/adaptive_timeout.py) and its two call sites: the
bash/python idle watchdog (src/agent_tools/subprocess_tools.py) and the job
ceiling a dispatched job reports (src/dispatch.py).

A fixed 300 s idle bound kills a build that legitimately prints nothing for six
minutes. The recorder watches how long recent commands of the same kind really
took and scales the bound to that — 3 x the median, clamped to [30, 600] s,
falling back to the fixed value until there are three samples.

Two deliberate restrictions on the raw formula, both tested here: the adaptive
value never SHORTENS a configured bound (shortening kills the very work the
watchdog protects), and a bound configured below the adaptive window is a
tighter choice on purpose and is used verbatim. With
`agent_adaptive_idle_timeout` off nothing consults the recorder at all.
"""
from __future__ import annotations

import pytest

from src import adaptive_timeout as at
from src.agent_tools import subprocess_tools as sp


@pytest.fixture(autouse=True)
def clean_recorder():
    at.reset()
    yield
    at.reset()


@pytest.fixture
def settings(monkeypatch):
    import src.settings as settings_mod
    values = {}
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values[key] if key in values else real(key, default))
    return values


# ── the recorder ────────────────────────────────────────────────────────────

def test_fewer_than_three_samples_keep_the_default():
    at.record("bash", 100.0)
    at.record("bash", 100.0)
    assert at.idle_timeout("bash", 300.0) == 300.0
    assert at.idle_timeout("never-seen", 42.0) == 42.0
    at.record("bash", 100.0)
    assert at.idle_timeout("bash", 300.0) == 300.0     # 3 x 100 = 300, same here


def test_three_times_the_median_clamped_to_the_window():
    for _ in range(4):
        at.record("slow", 120.0)
    assert at.median("slow") == 120.0
    assert at.idle_timeout("slow", 300.0) == 360.0     # 3 x 120
    for _ in range(4):
        at.record("quick", 0.4)
    assert at.idle_timeout("quick", 300.0) == at.MIN_TIMEOUT_S      # 1.2 clamped up to 30
    for _ in range(4):
        at.record("very-slow", 900.0)
    assert at.idle_timeout("very-slow", 300.0) == at.MAX_TIMEOUT_S  # 2700 clamped down to 600


def test_the_window_is_the_callers_to_choose():
    for _ in range(3):
        at.record("job", 400.0)
    assert at.idle_timeout("job", 900.0, lo=900.0, hi=3600.0) == 1200.0
    assert at.idle_timeout("job", 900.0, lo=1500.0, hi=3600.0) == 1500.0


def test_only_the_last_twenty_samples_count():
    for _ in range(25):
        at.record("k", 1.0)
    for _ in range(20):
        at.record("k", 60.0)
    assert at.samples("k") == [60.0] * at.MAX_SAMPLES
    assert at.median("k") == 60.0


def test_the_median_is_the_middle_not_the_mean():
    for value in (10.0, 10.0, 10.0, 10.0, 6000.0):
        at.record("k", value)
    assert at.median("k") == 10.0                      # one huge outlier moves nothing
    assert at.idle_timeout("k", 300.0) == at.MIN_TIMEOUT_S


def test_junk_is_ignored_and_never_raises():
    for junk in (None, "abc", -1, 0, float("nan"), float("inf"), object()):
        at.record("k", junk)
    at.record("", 5.0)
    assert at.samples("k") == [] and at.median("k") is None
    assert at.idle_timeout("k", 300.0) == 300.0
    assert at.idle_timeout("k", "not a number") == 0.0
    at.note_difference("k", 60.0, 30.0)                # logging only, must not raise


def test_reset_clears_one_key_or_everything():
    at.record("a", 1.0)
    at.record("b", 1.0)
    at.reset("a")
    assert at.samples("a") == [] and at.samples("b") == [1.0]
    at.reset()
    assert at.samples("b") == []


# ── the bash / python idle watchdog ─────────────────────────────────────────

def test_the_watchdog_bound_only_ever_grows(settings, monkeypatch):
    settings["agent_adaptive_idle_timeout"] = True
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 300.0)
    for _ in range(3):
        at.record("bash", 150.0)                       # 3 x 150 = 450 > 300
    assert sp._effective_idle_timeout("bash") == 450.0
    at.reset("bash")
    for _ in range(3):
        at.record("bash", 2.0)                         # 3 x 2 → 30, shorter than the bound
    assert sp._effective_idle_timeout("bash") == 300.0, "the adaptive value must never shorten the bound"


def test_a_bound_below_the_window_or_disabled_is_used_verbatim(settings, monkeypatch):
    """A deliberately tight (or disabled) idle timeout is the admin's choice,
    and the tests that pin a 1.5 s watchdog must keep meaning 1.5 s."""
    settings["agent_adaptive_idle_timeout"] = True
    for _ in range(5):
        at.record("bash", 200.0)
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 1.5)
    assert sp._effective_idle_timeout("bash") == 1.5
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 0.0)
    assert sp._effective_idle_timeout("bash") == 0.0    # 0 = the watchdog is off


def test_with_the_setting_off_the_fixed_bound_is_used(settings, monkeypatch):
    settings["agent_adaptive_idle_timeout"] = False
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 300.0)
    for _ in range(5):
        at.record("bash", 400.0)
    assert sp._effective_idle_timeout("bash") == 300.0 == sp._idle_timeout_seconds()


def test_a_finished_command_feeds_the_recorder(settings):
    import time
    settings["agent_adaptive_idle_timeout"] = True
    sp._record_cycle("bash", time.time() - 12.0)
    assert 11.0 < (at.samples("bash") or [0])[0] < 14.0
    settings["agent_adaptive_idle_timeout"] = False
    sp._record_cycle("bash", time.time() - 5.0)
    assert len(at.samples("bash")) == 1                 # nothing is recorded while it is off


def test_bash_runs_a_command_with_the_effective_bound(settings, monkeypatch):
    """End to end through BashTool: the idle bound handed to the watchdog is
    the adaptive one, and the command's duration is remembered."""
    import asyncio
    settings["agent_adaptive_idle_timeout"] = True
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 300.0)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)     # no tmux: the plain path
    for _ in range(3):
        at.record("bash", 100.0)
    seen = {}
    real = sp._run_subprocess_streaming

    async def spy(proc, *, timeout, progress_cb=None, idle_timeout=None):
        seen["idle_timeout"] = idle_timeout
        return await real(proc, timeout=timeout, progress_cb=progress_cb, idle_timeout=idle_timeout)

    monkeypatch.setattr(sp, "_run_subprocess_streaming", spy)
    res = asyncio.run(sp.BashTool().execute("echo hello", {}))
    assert res["exit_code"] == 0 and "hello" in res["output"]
    assert seen["idle_timeout"] == 300.0                # 3 x 100 == the fixed value
    assert len(at.samples("bash")) == 4                 # the run was recorded


# ── the dispatched job's ceiling ────────────────────────────────────────────

def test_the_job_ceiling_grows_with_what_jobs_really_take(settings):
    from src import dispatch
    settings["agent_adaptive_idle_timeout"] = True
    job = dispatch.DispatchJob("luis", {"tasks": [{"name": "w", "instruction": "a"}], "timeout_s": 100},
                               "/ws", "", "m", None, "t", verify="none")
    fixed = job.ceiling_s()
    assert fixed == 100
    key = dispatch._timing_key("/ws")
    for _ in range(3):
        at.record(key, 90.0)                            # 3 x 90 = 270 > 100
    assert job.ceiling_s() == 270
    for _ in range(20):
        at.record(key, 5.0)                             # quick jobs must not shrink it
    assert job.ceiling_s() == fixed
    settings["agent_adaptive_idle_timeout"] = False
    at.reset(key)
    for _ in range(3):
        at.record(key, 90.0)
    assert job.ceiling_s() == fixed


def test_a_broken_recorder_never_breaks_a_job_or_a_command(settings, monkeypatch):
    from src import dispatch
    settings["agent_adaptive_idle_timeout"] = True
    monkeypatch.setattr(at, "idle_timeout", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(at, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sp, "_idle_timeout_seconds", lambda: 300.0)
    assert sp._effective_idle_timeout("bash") == 300.0
    sp._record_cycle("bash", 0.0)                       # swallowed
    assert dispatch._adaptive_ceiling("/ws", 900) == 900
    job = dispatch.DispatchJob("luis", {"tasks": []}, "/ws", "", "m", None, "t")
    job.started, job.finished = 1.0, 2.0
    dispatch._record_job_duration(job)                  # swallowed
