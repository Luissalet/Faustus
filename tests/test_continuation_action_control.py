"""Promoted action against the complete real gate, including emitted effects."""
import ast
import copy
import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import continuation_decision as decisions
from tests.test_continuation_decision_shadow import BASE, environment


@lru_cache(maxsize=2)
def gate_code(legacy):
    source = (Path(__file__).parents[1] / "src/agent_loop.py").read_text(encoding="utf-8-sig")
    gate = copy.deepcopy(next(node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If) and ast.unparse(node.test) == "round_num > _rounds_budget"))
    branch = next(node for node in gate.body if isinstance(node, ast.If)
        and "_auto_cycles_left != 0" in ast.unparse(node.test))
    if legacy:
        branch.test = copy.deepcopy(branch.test.orelse)
    names = sorted({node.id for node in ast.walk(gate)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)})
    wrapper = ast.FunctionDef(name="probe", args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=[ast.Global(names=names),
        ast.While(test=ast.Constant(True), body=[gate, ast.Break()], orelse=[])], decorator_list=[])
    return compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
        "actual_full_round_gate", "exec")


def run_gate(state, *, legacy=False):
    env = environment(state)
    env["_ledger"].notes = []
    env["_ledger"].progress = []
    env["_ledger"].mutated_paths = lambda: []
    env["_ledger"].stop_reason = "fixture_reason"
    env.update(json=json, _harness=SimpleNamespace(progress_list_is_complete=lambda _: False),
        _todo_refresh_nudged=False, disabled_tools=set(), messages=[], _lang_note=lambda text: text,
        full_response="", _progress_extension_used=True, _progress_unit_count=0,
        session_id="fixture", owner="fixture", _awaiting_user=False, _exhausted_rounds=False,
        _end_turn_with_question=lambda **kw: [("question", json.dumps({"reason": kw["reason"]}))])
    env["logger"].info = lambda *args, **kwargs: None
    exec(gate_code(legacy), env)
    emitted = list(env["probe"]())
    fields = ("_rounds_budget", "_auto_cycles_left", "_no_progress_streak",
        "_progress_events_at_last_check", "messages", "full_response", "_awaiting_user",
        "_exhausted_rounds", "_used_progress_gate", "_progress_extension_used")
    return {**{key: env.get(key) for key in fields}, "emitted": emitted,
        "notes": env["_ledger"].notes, "stop_reason": env["_ledger"].stop_reason}


SCENARIOS = [("configured", {"cycles_left": 1}), ("progress", {}),
    ("recovery", {"cycles_left": -1, "recovery_active": True}),
    ("streak", {"no_progress_streak": 2}), ("ceiling", {"progress_ceiling": 10})]


@pytest.mark.parametrize("name,changes", SCENARIOS)
def test_promoted_gate_preserves_real_legacy_transitions_and_effects(name, changes):
    state = replace(BASE, **changes)
    observed = run_gate(state)
    assert observed == run_gate(state, legacy=True)
    assert observed["stop_reason"] == "fixture_reason"
    if name in ("configured", "progress"):
        assert observed["_rounds_budget"] == 20
        assert observed["_auto_cycles_left"] == 0
        assert observed["notes"] == ["auto_continue_rounds@10"]
        assert "10 more steps" in observed["messages"][0]["content"]
    else:
        assert observed["_rounds_budget"] == 10 and observed["_exhausted_rounds"] is True
        assert observed["messages"] == [] and observed["notes"] == []
        assert json.loads(observed["emitted"][0])["reason"] == (
            "no_progress_streak" if name == "streak" else "auto_continue_ceiling")


@pytest.mark.parametrize("failure", ["exception", "object", "invalid_action", "malformed_fields"])
@pytest.mark.parametrize("changes", [{"cycles_left": 1}, {"recovery_active": True}])
def test_failure_or_malformed_result_preserves_full_legacy_gate(monkeypatch, failure, changes):
    state = replace(BASE, **changes)
    baseline = run_gate(state, legacy=True)

    def broken(value):
        if failure == "exception":
            raise RuntimeError("synthetic controller failure")
        if failure == "object":
            return SimpleNamespace(action="extend")
        raise AssertionError("invalid action uses the captured legitimate decision below")

    # Capture the legitimate function before monkeypatching recursion below.
    legitimate = decisions.decide_round_extension(state)
    if failure in ("invalid_action", "malformed_fields"):
        malformed = (replace(legitimate, action="not_due") if failure == "invalid_action"
                     else replace(legitimate, rounds_delta="invalid"))
        monkeypatch.setattr(decisions, "decide_round_extension", lambda _: malformed)
    else:
        monkeypatch.setattr(decisions, "decide_round_extension", broken)
    assert run_gate(state) == baseline


def test_valid_stop_action_governs_even_when_legacy_would_extend(monkeypatch):
    state = replace(BASE, cycles_left=1)
    answer = decisions.decide_round_extension(replace(state, recovery_active=True))
    monkeypatch.setattr(decisions, "decide_round_extension", lambda _: answer)
    observed = run_gate(state)
    assert observed["_rounds_budget"] == 10 and observed["_auto_cycles_left"] == 1
    assert observed["messages"] == [] and observed["notes"] == []
    assert observed["_exhausted_rounds"] is True
    assert observed["stop_reason"] == "fixture_reason"


def test_progress_still_emits_both_existing_events():
    observed = run_gate(BASE)
    events = [json.loads(chunk[6:]) for chunk in observed["emitted"]]
    assert [event["reason"] for event in events] == ["rounds", "progress_continue"]
    assert events[0]["max_attempts"] == 1 and events[0]["round"] == 10
