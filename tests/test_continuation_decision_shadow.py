"""Characterize the real legacy gate without booting a model or full turn."""
import ast
import copy
import itertools
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.extra_round_policy import ExtraRounds
from src.continuation_decision import RoundExtensionInput, decide_round_extension


BASE = RoundExtensionInput(11, 10, 10, 0, True, 40, 1, 1, 0, False)


@pytest.mark.parametrize("changes,expected", [
    ({"round_num": 10}, ("not_due", "within_budget", 0, 0, 0)),
    ({"cycles_left": 1}, ("extend", "configured_cycle", 10, 0, 1)),
    ({"cycles_left": -1}, ("extend", "configured_cycle", 10, -1, 1)),
    ({}, ("extend", "progress", 10, 0, 1)),
    ({"no_progress_streak": 2}, ("stop", "no_progress", 0, 0, 3)),
    ({"no_progress_streak": 2, "events_now": 2}, ("extend", "progress", 10, 0, 0)),
    ({"recovery_active": True, "cycles_left": -1}, ("stop", "loop_recovery", 0, -1, 1)),
    ({"progress_ceiling": 10}, ("stop", "progress_ceiling", 0, 0, 1)),
    ({"progress_enabled": False}, ("stop", "cycles_exhausted", 0, 0, 1)),
])
def test_boundary_decisions(changes, expected):
    result = decide_round_extension(replace(BASE, **changes))
    assert (result.action, result.reason, result.rounds_delta, result.cycles_after,
            result.streak_after) == expected


def legacy_gate(*, with_shadow):
    source = (Path(__file__).parents[1] / "src/agent_loop.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    gate = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "round_num > _rounds_budget")
    branch = next(n for n in gate.body if isinstance(n, ast.If)
                  and "_auto_cycles_left != 0" in ast.unparse(n.test))
    body = copy.deepcopy(gate.body[:gate.body.index(branch)])
    if not with_shadow:
        # Execute the actual old statements, not a second handwritten policy.
        body = [n for n in body if not any(
            isinstance(child, ast.Name) and ("shadow" in child.id or child.id == "_extension_matches")
            for child in ast.walk(n))]
    branch = copy.deepcopy(branch)
    if not with_shadow:
        # Promotion keeps the original condition as an explicit fallback.
        branch.test = copy.deepcopy(branch.test.orelse)
    branch.body = branch.body[:4 if with_shadow else 3]  # actual mutation plus optional observer
    branch.orelse = branch.orelse[:1] if with_shadow else []
    executable = ast.Module(body=[ast.If(test=copy.deepcopy(gate.test), body=body + [branch], orelse=[])], type_ignores=[])
    return compile(ast.fix_missing_locations(executable), "actual_agent_loop_gate", "exec")


def environment(state):
    logs = []
    return dict(round_num=state.round_num, _rounds_budget=state.rounds_budget,
                max_rounds=state.grant_rounds, _auto_cycles_left=state.cycles_left,
                _agent_auto_continue_on_progress=state.progress_enabled,
                _agent_auto_continue_max_rounds=state.progress_ceiling,
                _ledger=SimpleNamespace(events=[None] * state.events_now),
                _progress_events_at_last_check=state.events_last,
                _no_progress_streak=state.no_progress_streak,
                _loop_recovery_active=state.recovery_active,
                _xr=ExtraRounds("enforce"),
                _continuation_shadow_reports=0, _continuation_shadow_mismatches=0,
                logger=SimpleNamespace(debug=lambda *a, **k: logs.append(("debug", a)),
                                       warning=lambda *a, **k: logs.append(("warning", a))),
                logs=logs)


def test_shadow_matches_actual_legacy_transition_matrix():
    code = legacy_gate(with_shadow=False)
    for cycles, streak, events, enabled, ceiling, recovery, round_num in itertools.product(
            [-1, 0, 1], [0, 2, 3], [0, 1, 2], [False, True], [10, 11], [False, True], [10, 11]):
        state = replace(BASE, cycles_left=cycles, no_progress_streak=streak, events_now=events,
                        progress_enabled=enabled, progress_ceiling=ceiling,
                        recovery_active=recovery, round_num=round_num)
        actual = environment(state)
        exec(code, actual)
        predicted = decide_round_extension(state)
        assert actual["_rounds_budget"] == state.rounds_budget + predicted.rounds_delta
        assert actual["_auto_cycles_left"] == predicted.cycles_after
        assert actual["_no_progress_streak"] == predicted.streak_after


def test_real_wiring_valid_controller_action_governs_and_logs_are_bounded(monkeypatch):
    import src.continuation_decision as module
    code = legacy_gate(with_shadow=True)
    incorrect = decide_round_extension(replace(BASE, recovery_active=True))
    monkeypatch.setattr(module, "decide_round_extension", lambda state: incorrect)
    actual = environment(replace(BASE, cycles_left=-1))
    events = list(actual["_ledger"].events)
    for i in range(6):
        actual["round_num"] = actual["_rounds_budget"] + 1
        exec(code, actual)
    assert actual["_rounds_budget"] == 10  # valid controller stop is now authoritative
    assert actual["_auto_cycles_left"] == -1
    assert actual["_ledger"].events == events
    assert len(actual["logs"]) == 3 and all(level == "warning" for level, _ in actual["logs"])


def test_observer_failure_is_bounded_and_does_not_change_live_branch(monkeypatch):
    import src.continuation_decision as module

    def fail(state):
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(module, "decide_round_extension", fail)
    code = legacy_gate(with_shadow=True)
    actual = environment(replace(BASE, cycles_left=-1))
    for _ in range(5):
        actual["round_num"] = actual["_rounds_budget"] + 1
        exec(code, actual)
    assert actual["_rounds_budget"] == 60 and len(actual["logs"]) == 3


@pytest.mark.parametrize("extends", [False, True])
@pytest.mark.parametrize("failure", ["comparison", "malformed"])
def test_comparison_failure_cannot_change_either_branch(monkeypatch, extends, failure):
    import src.continuation_decision as module

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic comparison failure")

    if failure == "comparison":
        monkeypatch.setattr(module, "matches_legacy", fail)
    else:
        monkeypatch.setattr(module, "decide_round_extension", lambda state: object())
    code = legacy_gate(with_shadow=True)
    actual = environment(replace(BASE, cycles_left=-1 if extends else 0, recovery_active=not extends))
    for _ in range(5):
        actual["round_num"] = actual["_rounds_budget"] + 1
        exec(code, actual)
    assert actual["_rounds_budget"] == (60 if extends else 10)
    assert actual["_auto_cycles_left"] == (-1 if extends else 0)
    assert len(actual["logs"]) == 3
