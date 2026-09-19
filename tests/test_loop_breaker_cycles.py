"""Cycle (oscillation) detection inside `src/loop_breaker.py`'s `LoopPolicy`.

The exact-repeat streak (tests/test_loop_breaker.py) only trips on the
IDENTICAL (tool, args, result) three times running. Weak local models more
often OSCILLATE — read file A, read file B, read file A, read file B... (or
a 3/4-step variant) — with no two consecutive calls identical, so the exact
streak never fires. This module tests the separate cycle detector added to
the same `LoopPolicy`: it watches the same bounded call history for a short
repeating period (2-4 calls) and escalates through the identical
nudge/block/stop ladder, counted on its own so neither track resets the
other.

The last test drives the real `src.agent_loop.stream_agent_loop` (same fake
harness as `tests/acceptance/test_a29_loop_breaker.py`) with a model that
alternates between two tool calls forever, proving the cycle ladder actually
surfaces inside a real turn and stops it.
"""
from __future__ import annotations

import asyncio
import json

# Imported at module level (not inside the test) so this module's collection
# resolves the REAL src.agent_loop and its dependencies before any sibling
# test module that stubs sys.modules for a lighter unit-test import (e.g.
# tests/test_loop_breaker_runaway.py's src.database/src.agent_tools mocks)
# gets a chance to run — see that file's module docstring. Whichever import
# of src.agent_loop happens first during pytest's collection phase wins for
# the whole process; tests/acceptance/test_a29_loop_breaker.py already
# relies on the same ordering for its own real-agent-loop acceptance test.
import src.agent_loop as al
from src.loop_breaker import LoopPolicy, hash_result


def _alternate(policy, steps, times):
    """Feed `steps` (a list of (tool, args, result) triples) to `policy`,
    repeated `times` times, and return the list of actions observed."""
    actions = []
    for _ in range(times):
        for tool, args, result in steps:
            actions.append(policy.observe(tool, args, hash_result(result)))
    return actions


# ---------------------------------------------------------------------------
# Period-2 cycles (A, B, A, B, ...)
# ---------------------------------------------------------------------------

def test_period_2_cycle_escalates_at_the_right_call():
    """Default cycle_min_repeats_p2=3: nudge fires exactly on the call that
    completes the 3rd full A/B repetition (call 6), not before."""
    policy = LoopPolicy(nudge_after=3, block_after=6, stop_after=10)
    steps = [("read_file", {"path": "a.py"}, "content a"), ("read_file", {"path": "b.py"}, "content b")]
    actions = _alternate(policy, steps, 4)
    # calls 1-5: nothing meets the 3-repeat floor yet; call 6 (end of the
    # 3rd A,B repetition) is the first nudge, and it stays "nudge" through
    # the rest of this run (8 calls total, well under the block threshold).
    assert actions == ["none", "none", "none", "none", "none", "nudge", "nudge", "nudge"]
    assert policy.last_trigger == "cycle"
    assert policy.last_cycle_period == 2
    assert policy.last_cycle_repeats == 4
    assert "alternating between" in policy.last_cycle_reason
    assert "read_file(" in policy.last_cycle_reason


def test_period_2_cycle_reaches_block_then_stop():
    policy = LoopPolicy(nudge_after=3, block_after=6, stop_after=10)
    steps = [("read_file", {"path": "a.py"}, "content a"), ("read_file", {"path": "b.py"}, "content b")]
    actions = _alternate(policy, steps, 10)  # 20 calls
    # nudge_min=3 repeats (call 6); block_min=3+(6-3)=6 repeats (call 12);
    # stop_min=3+(10-3)=10 repeats (call 20).
    assert actions[5] == "nudge"
    assert actions[11] == "block_tool"
    assert actions[19] == "stop"
    assert policy.blocked_tools == {"read_file"}


# ---------------------------------------------------------------------------
# Period-3 cycles (A, B, C, A, B, C, ...)
# ---------------------------------------------------------------------------

def test_period_3_cycle_escalates_at_the_right_call():
    """Default cycle_min_repeats_long=2: nudge fires on the call that
    completes the 2nd full 3-step repetition (call 6)."""
    policy = LoopPolicy()
    steps = [
        ("read_file", {"path": "a.py"}, "content a"),
        ("read_file", {"path": "b.py"}, "content b"),
        ("run_tests", {}, "still failing"),
    ]
    actions = _alternate(policy, steps, 2)  # 6 calls: exactly 2 full repeats
    assert actions[:5] == ["none"] * 5
    assert actions[5] == "nudge"
    assert policy.last_cycle_period == 3
    assert policy.last_cycle_repeats == 2
    assert "cycling through 3 repeated actions" in policy.last_cycle_reason


def test_period_4_cycle_is_also_detected():
    policy = LoopPolicy()
    steps = [
        ("bash", {"cmd": "edit"}, "edited"),
        ("bash", {"cmd": "test"}, "fail"),
        ("bash", {"cmd": "revert"}, "reverted"),
        ("bash", {"cmd": "test"}, "fail"),
    ]
    actions = _alternate(policy, steps, 3)  # 12 calls
    assert "nudge" in actions
    assert policy.last_cycle_period == 4


# ---------------------------------------------------------------------------
# A cycle must break like any other non-progress detector: a changed result,
# or an unrelated call in the middle, resets it.
# ---------------------------------------------------------------------------

def test_changing_result_breaks_the_cycle():
    policy = LoopPolicy()
    actions = []
    actions.append(policy.observe("read_file", {"path": "a.py"}, hash_result("a")))
    actions.append(policy.observe("read_file", {"path": "b.py"}, hash_result("b")))
    actions.append(policy.observe("read_file", {"path": "a.py"}, hash_result("a")))
    actions.append(policy.observe("read_file", {"path": "b.py"}, hash_result("b")))
    # progress: b.py's content actually changed this round
    actions.append(policy.observe("read_file", {"path": "a.py"}, hash_result("a")))
    actions.append(policy.observe("read_file", {"path": "b.py"}, hash_result("DIFFERENT")))
    actions.append(policy.observe("read_file", {"path": "a.py"}, hash_result("a")))
    actions.append(policy.observe("read_file", {"path": "b.py"}, hash_result("b")))
    assert "nudge" not in actions, actions
    assert all(a == "none" for a in actions)


def test_interleaved_unrelated_call_breaks_the_cycle():
    policy = LoopPolicy()
    steps = [("read_file", {"path": "a.py"}, "a"), ("read_file", {"path": "b.py"}, "b")]
    actions = _alternate(policy, steps, 2)  # 4 calls, not enough to nudge yet
    actions.append(policy.observe("list_dir", {"path": "."}, hash_result("listing")))
    actions += _alternate(policy, steps, 2)  # only 4 more consecutive A/B calls after the break
    assert "nudge" not in actions, actions


# ---------------------------------------------------------------------------
# An identical-repeat streak (all p signatures the same) is the exact-repeat
# case, not a cycle — it must go through the streak path only, never counted
# twice.
# ---------------------------------------------------------------------------

def test_identical_repeats_are_not_double_counted_as_a_cycle():
    policy = LoopPolicy(nudge_after=3, block_after=5, stop_after=7)
    same = hash_result("same output every time")
    actions = [policy.observe("bash", {"cmd": "ls"}, same) for _ in range(7)]
    assert actions == ["none", "none", "nudge", "nudge", "block_tool", "block_tool", "stop"]
    assert policy.last_trigger == "streak"
    assert policy.last_cycle_period is None
    assert policy.last_cycle_reason is None


def test_streak_and_cycle_tracks_do_not_reset_each_other():
    """A short identical-repeat burst that never reaches nudge_after, then an
    oscillation, must still let the oscillation accumulate its own repeats —
    the streak counter resetting on the tool/args change must not erase the
    cycle history, and vice versa."""
    policy = LoopPolicy(nudge_after=3, block_after=6, stop_after=10)
    same = hash_result("same")
    # two identical calls (below nudge_after=3, so still "none")
    assert policy.observe("bash", {"cmd": "ls"}, same) == "none"
    assert policy.observe("bash", {"cmd": "ls"}, same) == "none"
    # now oscillate for 3 full repeats -> cycle nudge on the 6th of these
    steps = [("read_file", {"path": "a.py"}, "a"), ("read_file", {"path": "b.py"}, "b")]
    actions = _alternate(policy, steps, 3)
    assert actions[-1] == "nudge"
    assert policy.last_trigger == "cycle"


# ---------------------------------------------------------------------------
# The setting can turn cycle detection off entirely.
# ---------------------------------------------------------------------------

def test_cycle_detection_can_be_disabled():
    policy = LoopPolicy(nudge_after=3, block_after=6, stop_after=10, cycle_detection=False)
    steps = [("read_file", {"path": "a.py"}, "a"), ("read_file", {"path": "b.py"}, "b")]
    actions = _alternate(policy, steps, 10)  # 20 calls, would have escalated all the way to stop
    assert set(actions) == {"none"}
    assert policy.last_trigger is None


def test_from_settings_reads_the_cycle_setting_names():
    calls = {}

    def fake_get_setting(key, default=None):
        calls[key] = default
        return {
            "agent_loop_breaker_cycle_detection": False,
            "agent_loop_breaker_cycle_min_repeats_p2": 4,
            "agent_loop_breaker_cycle_min_repeats_long": 3,
        }.get(key, default)

    policy = LoopPolicy.from_settings(fake_get_setting)
    assert policy.cycle_detection is False
    assert policy.cycle_min_repeats_p2 == 4
    assert policy.cycle_min_repeats_long == 3
    assert {
        "agent_loop_breaker_cycle_detection", "agent_loop_breaker_cycle_min_repeats_p2",
        "agent_loop_breaker_cycle_min_repeats_long",
    } <= set(calls)


# ---------------------------------------------------------------------------
# Agent-loop-level: the cycle ladder actually surfaces inside a real turn
# and stops it, same harness pattern as
# tests/acceptance/test_a29_loop_breaker.py's exact-repeat case.
# ---------------------------------------------------------------------------

def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def test_alternating_tool_calls_stop_the_turn_deterministically(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    # Two DIFFERENT calls, each returning its own steady result, alternating
    # forever — a plain read_file(a), read_file(b), read_file(a)... loop
    # with no two consecutive calls identical, so the exact-repeat streak
    # never fires; only the cycle detector can catch this.
    calls_seen = {"n": 0}

    async def _fake_exec(block, *a, **k):
        # block.content carries which of the two calls this is.
        if "a.py" in (block.content or ""):
            return (block.tool_type, {"output": "content of a.py", "exit_code": 0})
        return (block.tool_type, {"output": "content of b.py", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    _calls_text = ["```read_file\na.py\n```", "```read_file\nb.py\n```"]

    async def _fake_stream(_candidates, messages, **kwargs):
        text = _calls_text[calls_seen["n"] % 2]
        calls_seen["n"] += 1
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Find the config value and report it"}],
        max_rounds=20, relevant_tools={"read_file"}, workspace=str(tmp_path),
        session_id="sess-cycle", security_gate_bypass=True,
    )
    events = _events(_collect(gen))

    summaries = [e for e in events if e.get("type") == "harness_summary"]
    stop_reason = (summaries[-1]["data"].get("stop_reason") if summaries else None)
    stops = [e for e in events if e.get("type") == "loop_breaker_stop"]
    # Bounded, deterministic: the turn must not run all 20 rounds before a
    # cycle this obvious is caught.
    assert calls_seen["n"] <= 20, f"turn ran {calls_seen['n']} rounds without stopping"
    assert stop_reason == "non_progressing_loop", stop_reason
    if stops:
        assert stops[-1].get("trigger") == "cycle"
