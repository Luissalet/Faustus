"""A11 -- acceptance-parity case (Lote T6).

Contract (docs/spec/paridad/, blueprint doc 02 "Code Mode"): a run_code
program that loops forever or emits unlimited output is terminated by
time / call-count / output-size quotas, the child process does not survive
past that, and the caller gets a diagnostic receipt naming which quota
fired (``terminated_by``), how far the program got (``calls_made``), how
long it ran (``elapsed_ms``) and what it was doing (``last_call``).

Exercises the REAL subprocess round trip: ``src.code_mode.runner.
run_code_mode`` spawns the real ``python -I src/code_mode/guest.py``
process (no mock of the module under test); only the quota SETTINGS are
overridden (via ``src.settings.get_setting``, which ``runner._limits()``
reads live) to keep the suite fast.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from tests.acceptance.conftest import record_evidence

pytestmark = pytest.mark.asyncio


def _patch_limits(monkeypatch, **overrides):
    import src.settings as settings_mod
    from src.settings import DEFAULT_SETTINGS

    def _fast(key, default=None):
        if key in overrides:
            return overrides[key]
        return DEFAULT_SETTINGS.get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", _fast)


@pytest.mark.acceptance("A11")
async def test_infinite_loop_is_killed_by_the_wall_time_quota_with_a_diagnostic_receipt(
    monkeypatch, request,
):
    _patch_limits(monkeypatch, agent_code_mode_timeout_seconds=2, agent_code_mode_max_calls=50,
                   agent_code_mode_max_output_bytes=200_000)
    from src.code_mode.runner import run_code_mode

    t0 = time.monotonic()
    result = await run_code_mode("while True:\n    pass\n", session_id="a11-session", owner="luis")
    elapsed = time.monotonic() - t0

    assert elapsed < 10, "the timeout quota must actually bound wall time, not just report it"
    assert result.get("terminated") is True
    receipt = result["receipt"]
    assert receipt["terminated_by"] == "timeout"
    assert receipt["calls_made"] == 0
    assert receipt["last_call"] is None
    assert isinstance(receipt["elapsed_ms"], (int, float)) and receipt["elapsed_ms"] > 0
    assert "output_bytes" in receipt

    record_evidence(request, module="src.code_mode.runner", function="run_code_mode", receipt=receipt)


@pytest.mark.acceptance("A11")
async def test_unbounded_print_output_is_killed_by_the_output_quota(monkeypatch, request):
    _patch_limits(monkeypatch, agent_code_mode_timeout_seconds=15, agent_code_mode_max_calls=50,
                   agent_code_mode_max_output_bytes=10_000)
    from src.code_mode.runner import run_code_mode

    result = await run_code_mode('print("x" * 10**7)\n', session_id="a11-session", owner="luis")

    assert result.get("terminated") is True
    receipt = result["receipt"]
    assert receipt["terminated_by"] == "output"
    assert receipt["output_bytes"] <= 10_000 + 1024  # capped near the configured limit
    assert receipt["calls_made"] == 0

    record_evidence(request, module="src.code_mode.runner", function="run_code_mode", receipt=receipt)


@pytest.mark.acceptance("A11")
async def test_a_thousand_tool_calls_are_stopped_by_the_max_calls_quota(monkeypatch, request):
    _patch_limits(monkeypatch, agent_code_mode_timeout_seconds=15, agent_code_mode_max_calls=5,
                   agent_code_mode_max_output_bytes=200_000)
    from src.code_mode.runner import run_code_mode

    code = "\n".join(f"tools.call('get_workspace', {{}})" for _ in range(1000)) + "\n"
    result = await run_code_mode(code, session_id="a11-session", owner="luis")

    assert result.get("terminated") is True
    receipt = result["receipt"]
    assert receipt["terminated_by"] == "max_calls"
    assert receipt["calls_made"] <= 5
    assert receipt["last_call"] == "get_workspace"

    record_evidence(request, module="src.code_mode.runner", function="run_code_mode", receipt=receipt)


@pytest.mark.acceptance("A11")
async def test_the_terminated_child_process_does_not_survive(monkeypatch, request):
    """Beyond the receipt: the pid the runner spawned must actually be gone
    once run_code_mode returns after a timeout kill -- not merely detached
    or left as a zombie."""
    _patch_limits(monkeypatch, agent_code_mode_timeout_seconds=2, agent_code_mode_max_calls=50,
                   agent_code_mode_max_output_bytes=200_000)

    import asyncio
    import src.code_mode.runner as runner_mod

    spawned_pids = []
    real_create = asyncio.create_subprocess_exec

    async def _spy_create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        spawned_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create)

    result = await runner_mod.run_code_mode(
        "while True:\n    pass\n", session_id="a11-session", owner="luis",
    )
    assert result["receipt"]["terminated_by"] == "timeout"
    assert len(spawned_pids) == 1
    pid = spawned_pids[0]

    # Give the OS a moment to finish reaping (the runner already awaited
    # proc.wait() with a timeout before returning).
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"pid {pid} is still alive after run_code_mode returned")

    record_evidence(request, module="src.code_mode.runner", function="run_code_mode", pid=pid)
