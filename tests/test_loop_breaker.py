"""src/loop_breaker.py — the A29 deterministic, bounded escalation policy."""
from __future__ import annotations

from src.loop_breaker import LoopPolicy, hash_result, normalize_args


def test_normalize_args_ignores_key_order_and_whitespace():
    a = normalize_args({"path": "x.py", "old": "a", "new": "b"})
    b = normalize_args('{ "new": "b", "path": "x.py", "old":  "a" }')
    assert a == b


def test_normalize_args_unparsable_string_is_stable_not_raising():
    assert normalize_args("not json at all") == "not json at all"
    assert normalize_args(normalize_args("not json at all")) == normalize_args("not json at all")


def test_hash_result_stable_for_equal_payloads_different_for_different_ones():
    assert hash_result({"output": "ok", "exit_code": 0}) == hash_result({"exit_code": 0, "output": "ok"})
    assert hash_result({"output": "ok"}) != hash_result({"output": "different"})


def test_identical_calls_escalate_none_then_nudge_then_block_then_stop():
    policy = LoopPolicy(nudge_after=3, block_after=5, stop_after=7)
    same_hash = hash_result("same output every time")
    actions = [policy.observe("bash", {"cmd": "ls"}, same_hash) for _ in range(7)]
    assert actions == ["none", "none", "nudge", "nudge", "block_tool", "block_tool", "stop"]
    assert policy.blocked_tools == {"bash"}


def test_progressing_calls_never_escalate_even_repeating_tool_and_args():
    """Same tool, same arguments, but a DIFFERENT result each time (real
    progress, e.g. a probe that finds something new) must never trip this —
    the whole point of hashing the result too, not just (tool, args)."""
    policy = LoopPolicy(nudge_after=2, block_after=4, stop_after=6)
    for i in range(20):
        action = policy.observe("bash", {"cmd": "pytest -q"}, hash_result(f"output #{i}"))
        assert action == "none", f"round {i} escalated on progress: {action}"
    assert policy.streak == 1
    assert policy.blocked_tools == set()


def test_a_different_result_resets_the_streak_mid_loop():
    policy = LoopPolicy(nudge_after=3, block_after=100, stop_after=200)
    same = hash_result("stuck")
    assert [policy.observe("read_file", {"path": "a.py"}, same) for _ in range(2)] == ["none", "none"]
    # progress arrives on round 3
    assert policy.observe("read_file", {"path": "a.py"}, hash_result("new content")) == "none"
    assert policy.streak == 1
    # and the loop can still trip later on its own merits
    assert [policy.observe("read_file", {"path": "a.py"}, same) for _ in range(3)] == ["none", "none", "nudge"]


def test_twenty_identical_calls_stop_within_stop_after_rounds_not_later():
    policy = LoopPolicy(nudge_after=3, block_after=6, stop_after=10)
    same = hash_result("identical every round")
    stopped_at = None
    for i in range(1, 21):
        action = policy.observe("python", "print(1)", same)
        if action == "stop":
            stopped_at = i
            break
    assert stopped_at == 10


def test_misconfigured_thresholds_are_clamped_to_a_monotonic_ladder():
    policy = LoopPolicy(nudge_after=5, block_after=3, stop_after=1)  # nonsensical input
    assert policy.nudge_after < policy.block_after < policy.stop_after


def test_reset_clears_streak_and_blocked_tools():
    policy = LoopPolicy(nudge_after=1, block_after=2, stop_after=3)
    same = hash_result("x")
    policy.observe("bash", {}, same)
    policy.observe("bash", {}, same)
    assert policy.blocked_tools == {"bash"}
    policy.reset()
    assert policy.streak == 0 and policy.blocked_tools == set()


def test_from_settings_reads_the_registered_setting_names():
    calls = {}

    def fake_get_setting(key, default=None):
        calls[key] = default
        return {"agent_loop_breaker_nudge_after": 2,
                "agent_loop_breaker_block_after": 4,
                "agent_loop_breaker_stop_after": 6}.get(key, default)

    policy = LoopPolicy.from_settings(fake_get_setting)
    assert (policy.nudge_after, policy.block_after, policy.stop_after) == (2, 4, 6)
    assert set(calls) == {
        "agent_loop_breaker_nudge_after", "agent_loop_breaker_block_after", "agent_loop_breaker_stop_after",
        "agent_loop_breaker_cycle_detection", "agent_loop_breaker_cycle_min_repeats_p2",
        "agent_loop_breaker_cycle_min_repeats_long",
    }
