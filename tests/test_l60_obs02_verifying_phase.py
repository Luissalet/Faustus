"""OBS-02 — the eighth canonical phase, `verifying`, actually reachable.

Before this lote, `agent_runs.CANONICAL_PHASES` already listed "verifying"
as one of the eight buckets (lote 21), but nothing in `_observe_activity`
ever set it: `src/agent_loop.py`'s reliability harness (tests, static
analysis, an independent reviewer model, a claims check — real verification
work after a tool call) publishes `harness_check` SSE events, and that
event type had no branch at all in `_observe_activity` — a run running
tests or awaiting a reviewer model's verdict was reported as whatever its
PREVIOUS phase happened to be (usually "tool"), never "verifying".

Gap proof: before this lote,
`agent_runs._canonical_phase("verifying") == "admission"` (the sticky
default — "verifying" was not a key in `_PHASE_CANON` at all), and
`harness_check` events were silently dropped by `_observe_activity` (no
`elif event_type == "harness_check"` branch existed).
"""
import json

import pytest

from src import agent_runs


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(agent_runs, "_setting",
                         lambda key, default=None: {"agent_runs_persist": False}.get(key, default),
                         raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()


async def _never():
    import asyncio
    await asyncio.sleep(3600)
    yield "data: [DONE]\n\n"  # pragma: no cover


async def _quiesce(run):
    for t in (run.task, run.evict_task):
        if t is not None and not t.done():
            t.cancel()
    import asyncio
    await asyncio.sleep(0.01)


def _harness_check(status, **extra):
    payload = {"type": "harness_check", "status": status, "round": 1}
    payload.update(extra)
    return "data: " + json.dumps(payload) + "\n\n"


def _tool_start(tool="bash", round_=1):
    return "data: " + json.dumps({"type": "tool_start", "tool": tool, "command": "x", "round": round_}) + "\n\n"


def test_verifying_is_in_the_canonical_set():
    assert "verifying" in agent_runs.CANONICAL_PHASES


@pytest.mark.parametrize("status", [
    "static_analysis", "syntax_error", "tests_running", "tests_failed",
    "review_running", "review_issues", "verified", "unverified",
])
@pytest.mark.asyncio
async def test_each_real_verification_status_reaches_the_verifying_phase(status):
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start())  # phase="tool" first, as a real turn would
    agent_runs._publish(run, _harness_check(status))
    snap = agent_runs.activity_snapshot("sid")
    assert snap["phase_canonical"] == "verifying"
    assert snap["phase_raw"] == "verifying"
    assert snap["detail"] == status
    await _quiesce(run)


@pytest.mark.parametrize("status", [
    "checkpoint", "auto_continue", "think_cutoff", "rejected",
    "empty_round", "unknown_tool", "required_action",
    "required_action_unavailable", "target_substituted",
])
@pytest.mark.asyncio
async def test_a_harness_nudge_that_is_not_verification_leaves_the_phase_alone(status):
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start())  # phase_canonical -> "tool"
    before = agent_runs.activity_snapshot("sid")["phase_canonical"]
    agent_runs._publish(run, _harness_check(status))
    after = agent_runs.activity_snapshot("sid")
    assert after["phase_canonical"] == before == "tool"
    assert after["phase"] == "tool", "a non-verification harness_check must not overwrite the legacy phase either"
    await _quiesce(run)


def test_canonical_phase_of_verifying_word_is_itself():
    assert agent_runs._canonical_phase("verifying") == "verifying"


def test_every_known_ad_hoc_phase_still_maps_into_the_closed_canonical_set():
    """Extends the lote-21 pin with the new word, rather than duplicating it."""
    for raw in ("starting", "queued", "waiting_model", "thinking", "writing", "tool",
                "research", "awaiting_user", "finishing", "vram_blocked",
                "unloading_model", "loading_model", "probing", "verifying"):
        assert agent_runs._canonical_phase(raw) in agent_runs.CANONICAL_PHASES, raw
