"""PLAN-06 — "ambitious but bounded" closing check.

Acceptance: a turn is not closed while its OWN plan still leaves a requested
requirement uncovered (`plan_state.coverage`), and the nudge this produces
never invents work beyond what the user already asked for.
"""
from __future__ import annotations

from src import agent_loop
from src import plan_state


def test_split_requirements_reads_bullets_as_separate_items():
    text = "- fix the login bug\n- add a regression test\n- update the changelog"
    reqs = agent_loop._split_requirements(text)
    assert reqs == ["fix the login bug", "add a regression test", "update the changelog"]


def test_split_requirements_keeps_a_plain_sentence_whole():
    text = "fix the failing login test"
    reqs = agent_loop._split_requirements(text)
    assert reqs == ["fix the failing login test"]


def _plan_update(markdown: str) -> dict:
    plan = plan_state.from_markdown(markdown)
    payload = {"plan": markdown}
    payload.update(plan.to_dict())
    return payload


def test_plan_coverage_gap_flags_a_requirement_no_step_addresses():
    instruction = "- fix the login bug\n- write a regression test for it"
    plan_update = _plan_update("- [x] fix the login bug in auth.py")
    gap = agent_loop._plan_coverage_gap(plan_update, instruction)
    assert any("regression test" in g for g in gap)


def test_plan_coverage_gap_is_empty_when_the_plan_is_fully_done():
    """A plan the model itself marked entirely done is never second-guessed
    by the fuzzy coverage check alone — only its own pending/blocked steps
    open the gate."""
    instruction = "- fix the login bug\n- write a regression test for it"
    plan_update = _plan_update(
        "- [x] fix the login bug in auth.py\n- [x] add a regression test for the login bug"
    )
    gap = agent_loop._plan_coverage_gap(plan_update, instruction)
    assert gap == []


def test_plan_coverage_gap_is_empty_with_no_plan():
    """No plan at all this turn: behaviour is untouched (COMUN rule 3)."""
    assert agent_loop._plan_coverage_gap(None, "fix the login bug") == []


def test_plan_coverage_gap_is_empty_when_plan_has_no_pending_steps_left():
    instruction = "fix the login bug"
    plan_update = _plan_update("- [x] fix the login bug")
    assert agent_loop._plan_coverage_gap(plan_update, instruction) == []
