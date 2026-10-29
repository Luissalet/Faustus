"""The event-loop watchdog names the call that blocked the loop (tasks #1725)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src import loop_watchdog


def _blocking_work(seconds: float) -> None:
    time.sleep(seconds)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(loop_watchdog, "POLL_S", 0.05)
    monkeypatch.setattr(loop_watchdog, "TICK_S", 0.05)
    monkeypatch.setattr(loop_watchdog, "REDUMP_S", 0.3)
    yield
    loop_watchdog.uninstall()


def test_a_blocked_loop_is_recorded_with_the_blocking_call(tmp_path):
    async def scenario():
        assert loop_watchdog.install(str(tmp_path), stall_s=0.3)
        await asyncio.sleep(0.2)
        _blocking_work(1.0)  # synchronous work inside a coroutine: what stalls the real server
        await asyncio.sleep(0.4)  # the loop runs again; the watchdog thread writes the stall
        return loop_watchdog.status()

    status = asyncio.run(scenario())
    assert status["enabled"] and status["stalls"] == 1
    assert status["max_lag_s"] >= 0.8
    row = status["recent"][-1]
    assert row["duration_s"] >= 0.8
    assert any("_blocking_work" in call or "time.sleep" in call for call in row["calls"])
    logged = [json.loads(line) for line in (tmp_path / "logs" / "loop_stalls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(logged) == 1 and logged[0]["stacks"] and logged[0]["threads"] is not None
    assert any("_blocking_work" in ln for ln in logged[0]["stacks"][0]["stack"])
    assert loop_watchdog.read_log(str(tmp_path))[-1]["duration_s"] >= 0.8


def test_work_in_a_thread_does_not_register_as_a_stall(tmp_path):
    async def scenario():
        assert loop_watchdog.install(str(tmp_path), stall_s=0.3)
        await asyncio.to_thread(_blocking_work, 1.0)
        await asyncio.sleep(0.3)
        return loop_watchdog.status()

    status = asyncio.run(scenario())
    assert status["stalls"] == 0
    assert not (tmp_path / "logs" / "loop_stalls.jsonl").exists()


def test_threshold_zero_turns_it_off(tmp_path):
    async def scenario():
        return loop_watchdog.install(str(tmp_path), stall_s=0)

    assert asyncio.run(scenario()) is False
    assert loop_watchdog.status()["enabled"] is False
