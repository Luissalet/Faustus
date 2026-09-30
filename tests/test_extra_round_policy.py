"""H13: every extra round has a cause and a budget (src/extra_round_policy.py)."""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from src import loop_decisions as ld
from src.extra_round_policy import CAUSES, FAMILIES, ExtraRounds, decide_extra_round

LOOP = Path(__file__).parents[1] / "src" / "agent_loop.py"


# -- the pure policy -----------------------------------------------------------
def test_a_round_is_granted_while_the_budget_lasts_and_refused_after():
    assert decide_extra_round("length", used=0, limit=2).granted
    last = decide_extra_round("length", used=1, limit=2)
    assert last.granted and last.remaining == 1 and last.family == "transport"
    done = decide_extra_round("length", used=2, limit=2)
    assert not done.granted and done.reason == "budget_exhausted" and done.remaining == 0


@pytest.mark.parametrize("cause", [None, "", "made_up", 3, ("length",)])
def test_an_unknown_cause_is_refused(cause):
    decision = decide_extra_round(cause, used=0, limit=5)
    assert not decision.granted and decision.reason == "unknown_cause" and decision.family == "unknown"


@pytest.mark.parametrize("used,limit", [(None, 2), (0, None), (-1, 2), (0, -2), (1.5, 2), ("1", 2), (0, "2")])
def test_a_budget_that_is_not_a_non_negative_integer_is_refused(used, limit):
    decision = decide_extra_round("length", used=used, limit=limit)
    assert not decision.granted and decision.reason == "bad_budget"


def test_the_decision_reports_what_an_event_carries():
    assert decide_extra_round("tests_fix", used=1, limit=3).fields() == {
        "cause": "tests_fix", "family": "verification", "budget_used": 1, "budget_limit": 3, "budget_remaining": 2}


def test_every_cause_belongs_to_a_known_family_and_is_described():
    for name, cause in CAUSES.items():
        assert cause.family in FAMILIES, name
        assert cause.description.strip(), name
        assert name == name.lower() and " " not in name


def test_the_policy_equals_the_legacy_used_below_limit_rule_for_every_cause():
    for cause in CAUSES:
        for used, limit in itertools.product(range(0, 6), range(0, 6)):
            assert decide_extra_round(cause, used=used, limit=limit).granted == (used < limit), (cause, used, limit)


# -- the modes -------------------------------------------------------------------------
def test_enforce_applies_the_policy_and_refuses_unknown_causes():
    xr = ExtraRounds("enforce")
    assert xr.permits("length", 1, 2) and not xr.permits("length", 2, 2)
    assert not xr.permits("invented", 0, 9)
    assert xr.unknown_causes == ["invented"] and xr.refusals == [{"cause": "invented", "reason": "unknown_cause"}]
    assert not xr.permits("length", None, 2)
    assert xr.refusals[-1] == {"cause": "length", "reason": "bad_budget"}


def test_shadow_keeps_the_legacy_answer_and_records_a_divergence():
    xr = ExtraRounds("shadow")
    assert xr.permits("length", 1, 2) is True and xr.divergences == []
    assert xr.permits("invented", 0, 2) is True  # legacy would grant; the policy would not
    assert xr.divergences == [{"cause": "invented", "used": 0, "limit": 2, "policy": False, "legacy": True}]
    assert xr.summary()["unknown_causes"] == ["invented"]


def test_legacy_mode_is_the_old_rule_and_records_nothing():
    xr = ExtraRounds("legacy")
    assert xr.permits("invented", 0, 2) is True and xr.permits("length", 2, 2) is False
    assert xr.event("length", used=1, limit=2, round_num=3) == {}
    assert xr.extension_event("progress", cycles_left=0, rounds_granted=10) == {}
    assert xr.records == [] and xr.total() == 0


def test_an_unrecognised_mode_falls_back_to_enforce():
    assert ExtraRounds("bogus").mode == "enforce"


def test_events_carry_cause_budget_and_a_running_total():
    xr = ExtraRounds("enforce")
    first = xr.event("length", used=1, limit=2, round_num=1)
    second = xr.event("tests_fix", used=1, limit=1, round_num=4)
    assert first["extra_rounds_total"] == 1 and second["extra_rounds_total"] == 2
    assert second["budget_remaining"] == 0 and second["family"] == "verification"
    assert xr.by_cause() == {"length": 1, "tests_fix": 1}
    assert [r["round"] for r in xr.records] == [1, 4]
    assert xr.summary()["total"] == 2


def test_an_event_for_an_unknown_cause_is_recorded_as_unknown_so_the_audit_sees_it():
    xr = ExtraRounds("enforce")
    assert xr.event("invented", used=1, limit=1)["cause"] == "unknown"
    assert xr.by_cause() == {"unknown": 1}


def test_a_budget_extension_records_only_the_two_budget_causes():
    xr = ExtraRounds("enforce")
    assert xr.extension_event("configured_cycle", cycles_left=0, rounds_granted=10, round_num=10)["cause"] == "configured_cycle"
    assert xr.extension_event("progress", cycles_left=0, rounds_granted=10)["family"] == "budget"
    assert xr.extension_event("loop_recovery", cycles_left=0, rounds_granted=10)["cause"] == "unknown"
    assert xr.extension_event(None, cycles_left=-1, rounds_granted=10)["family"] == "unknown"


# -- the closing stage as a pure transition table ------------------------------------------
def _state(**over):
    base = dict(finish_reason="stop", length_continues=0, length_limit=2, dropped_tool_calls=False,
                unknown_tool_nudges=0, unknown_tool_limit=2, empty_give_up=False, empty_nudges=0, empty_limit=3,
                wrong_language=False, language_nudges=0, round_num=2, max_rounds=8)
    base.update(over)
    return ld.TextRoundState(**base)


ENFORCE = ExtraRounds("enforce").permits


@pytest.mark.parametrize("changes,expected", [
    ({}, ld.CHECK_CLAIMS),
    ({"finish_reason": "length"}, ld.CONTINUE_LENGTH),
    ({"finish_reason": "length", "length_continues": 2}, ld.CHECK_CLAIMS),
    ({"finish_reason": "length", "dropped_tool_calls": True}, ld.CONTINUE_LENGTH),
    ({"dropped_tool_calls": True}, ld.CORRECT_UNKNOWN_TOOL),
    ({"dropped_tool_calls": True, "unknown_tool_nudges": 2}, ld.CHECK_CLAIMS),
    ({"dropped_tool_calls": True, "empty_give_up": True}, ld.CORRECT_UNKNOWN_TOOL),
    ({"empty_give_up": True}, ld.NUDGE_EMPTY),
    ({"empty_give_up": True, "empty_nudges": 2}, ld.NUDGE_EMPTY),
    ({"empty_give_up": True, "empty_nudges": 3}, ld.ASK_EMPTY_EXHAUSTED),
    ({"empty_give_up": True, "empty_nudges": 3, "wrong_language": True}, ld.ASK_EMPTY_EXHAUSTED),
    ({"wrong_language": True}, ld.CORRECT_LANGUAGE),
    ({"wrong_language": True, "language_nudges": 1}, ld.CHECK_CLAIMS),
    ({"wrong_language": True, "round_num": 8}, ld.CHECK_CLAIMS),
    ({"wrong_language": True, "finish_reason": "length"}, ld.CONTINUE_LENGTH),
])
def test_the_text_round_decision_order(changes, expected):
    assert ld.decide_text_round(_state(**changes), ENFORCE) == expected


def test_the_text_round_decision_asks_the_injected_policy_and_nothing_else():
    asked = []

    def permits(cause, used, limit):
        asked.append((cause, used, limit))
        return False
    state = _state(finish_reason="length", dropped_tool_calls=True, empty_give_up=True, wrong_language=True)
    assert ld.decide_text_round(state, permits) == ld.CHECK_CLAIMS
    assert [a[0] for a in asked] == ["length", "hallucinated_tool", "empty_completion", "language_mismatch"]
    assert ld.decide_text_round(_state(), lambda *a: True) == ld.CHECK_CLAIMS  # nothing to decide on


def test_every_action_is_reachable_and_named():
    reached = {ld.decide_text_round(_state(**c), ENFORCE) for c in (
        {"finish_reason": "length"}, {"dropped_tool_calls": True}, {"empty_give_up": True},
        {"empty_give_up": True, "empty_nudges": 3}, {"wrong_language": True}, {})}
    assert reached == set(ld.ACTIONS)


# -- audit of agent_loop.py: no extra round without a registered cause ------------------------
def _loop_tree():
    return ast.parse(LOOP.read_text(encoding="utf-8-sig"))


def _xr_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "_xr"):
            yield node


def test_every_cause_the_loop_names_is_registered():
    names = []
    for call in _xr_calls(_loop_tree()):
        if call.func.attr in ("permits", "event") and call.args and isinstance(call.args[0], ast.Constant):
            names.append(call.args[0].value)
    assert names, "the loop no longer asks the policy"
    assert sorted(set(names) - set(CAUSES)) == []


def test_every_registered_cause_is_used_by_the_loop():
    source = LOOP.read_text(encoding="utf-8-sig")
    unused = [name for name in CAUSES if f'"{name}"' not in source]
    assert unused == [], unused


def _step_sites(tree):
    """(line, recorded) for every place that announces a further round and continues."""
    def is_step_yield(stmt):
        text = ast.unparse(stmt.value) if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield) else ""
        return "agent_step" in text and "round_num + 1" in text

    found = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for first, second in zip(block, block[1:]):
                if is_step_yield(first) and isinstance(second, ast.Continue):
                    found.append((first.lineno, any("_xr." in ast.unparse(stmt) for stmt in block)))
    return found


def test_every_extra_round_site_goes_through_the_policy():
    """Each place the loop announces a further round and `continue`s records
    the round in the same block: its cause and budget."""
    sites = _step_sites(_loop_tree())
    assert len(sites) >= 25, len(sites)
    assert [line for line, recorded in sites if not recorded] == []


def test_the_audit_itself_notices_a_round_with_no_record():
    bare = ast.parse("""
while True:
    if a:
        yield f'data: {json.dumps({"type": "agent_step", "round": round_num + 1})}\\n\\n'
        continue
    if b:
        _xr.event("length", used=1, limit=2)
        yield f'data: {json.dumps({"type": "agent_step", "round": round_num + 1})}\\n\\n'
        continue
""")
    assert [recorded for _, recorded in _step_sites(bare)] == [False, True]


def test_a_preview_carries_the_fields_without_recording_a_round():
    xr = ExtraRounds("enforce")
    assert xr.preview("engine_lost_recovered", used=1, limit=1)["family"] == "transport"
    assert xr.total() == 0
    assert ExtraRounds("legacy").preview("length", used=1, limit=2) == {}
    assert xr.preview("invented", used=1, limit=1)["cause"] == "unknown"


def test_every_auto_continue_event_reports_its_cause():
    tree = _loop_tree()
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
        if "status" not in keys:
            continue
        status = node.values[keys.index("status")]
        if not (isinstance(status, ast.Constant) and status.value == "auto_continue"):
            continue
        checked += 1
        spread = [v for k, v in zip(node.keys, node.values) if k is None]
        assert spread and any("_xr" in ast.unparse(v) for v in spread), ast.unparse(node)[:160]
    assert checked >= 9
