"""Execute the real closeout branches with scripted decisions, without models."""
import ast
import copy
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest


@lru_cache(maxsize=1)
def closeout_code():
    tree = ast.parse((Path(__file__).parents[1] / "src/agent_loop.py").read_text(encoding="utf-8-sig"))
    ce = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
              and ast.unparse(n.test) == "isinstance(_ce_decision, dict) and _ce_decision.get('ok')")
    plan = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test).startswith("_latest_plan_update and"))

    class EndRound(ast.NodeTransformer):
        def visit_Continue(self, node):
            return ast.Return(value=ast.Constant("continue"))

    body = [ast.Global(names=["_ce_completion_rounds", "_plan_coverage_rounds"]),
            EndRound().visit(copy.deepcopy(ce)), EndRound().visit(copy.deepcopy(plan)),
            ast.Return(value=ast.Constant("stop"))]
    function = ast.FunctionDef(name="run", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                               kw_defaults=[], defaults=[]), body=body, decorator_list=[])
    return compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "real_closeout", "exec")


def _extra_rounds():
    """The loop's single extra-round policy, in its default mode."""
    from src.extra_round_policy import ExtraRounds
    return ExtraRounds("enforce")


def run_case(used, *, proposed=True, shadow=False, plan=True, plan_used=0):
    plan_checks = []
    state = dict(json=json, _ce_decision={"ok": True, "shadow": shadow, "mode": "greedy",
                 "stop_reason": "unfinished", "continue_with": ["finish test"] if proposed else []},
                 _ce_completion_rounds=used, _CE_MAX_ROUNDS=2, _latest_plan_update={"pending": True} if plan else None,
                 _plan_coverage_rounds=plan_used, _PLAN_COVERAGE_MAX_ROUNDS=1,
                 _last_user="finish test", round_num=8, messages=[],
                 _ledger=SimpleNamespace(stop_reason="completion_continue"), _lang_note=lambda t: t,
                 _plan_coverage_gap=lambda *a: plan_checks.append(True) or ["finish test"],
                 _xr=_extra_rounds())
    exec(closeout_code(), state)
    generator = state["run"]()
    events = []
    while True:
        try:
            events.append(json.loads(next(generator).removeprefix("data: ").strip()))
        except StopIteration as finished:
            return state, events, finished.value, plan_checks


@pytest.mark.parametrize("used", [0, 1])
def test_available_ce_quota_preserves_continuation(used):
    state, events, action, plan_checks = run_case(used)
    assert action == "continue" and state["_ce_completion_rounds"] == used + 1
    assert state["_plan_coverage_rounds"] == 0 and not plan_checks
    assert len(state["messages"]) == 1 and state["_ledger"].stop_reason == "completion_continue"
    event = events[0]
    assert event["continuation_proposed"] and event["continuation_granted"]
    assert event["continuation_block_reason"] == ""
    assert event["completion_rounds_used"] == used and event["completion_rounds_limit"] == 2


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("used", [2, 3])
def test_exhausted_ce_quota_closes_honestly_without_plan_bypass(used, plan):
    state, events, action, plan_checks = run_case(used, plan=plan)
    assert action == "stop" and state["_ce_completion_rounds"] == used
    assert state["_plan_coverage_rounds"] == 0 and not plan_checks and not state["messages"]
    assert state["_ledger"].stop_reason == "completion_budget_exhausted"
    assert len(events) == 1
    event = events[0]
    assert event["would_continue"] == 1 and event["continuation_proposed"]
    assert not event["continuation_granted"]
    assert event["continuation_block_reason"] == "completion_round_limit"
    assert event["stop_reason"] == "completion_budget_exhausted"
    assert event["engine_stop_reason"] == "unfinished"


@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("plan_used", [0, 1])
def test_empty_proposal_preserves_existing_plan_gate(shadow, plan_used):
    state, events, action, plan_checks = run_case(2, proposed=False, shadow=shadow, plan_used=plan_used)
    assert len(plan_checks) == 1 and state["_ce_completion_rounds"] == 2
    assert action == ("continue" if plan_used == 0 else "stop")
    assert len(state["messages"]) == (1 if plan_used == 0 else 0)
    assert state["_plan_coverage_rounds"] == 1
    event = events[0]
    assert not event["continuation_proposed"] and not event["continuation_granted"]
    assert event["continuation_block_reason"] == ("shadow" if shadow else "no_proposal")
    assert event["stop_reason"] == "unfinished"


def test_runtime_reason_remains_an_expected_non_success_outcome():
    from src.tool_outcome import Outcome, classify_status
    assert classify_status("completion_budget_exhausted") is Outcome.EXPECTED_ERROR
