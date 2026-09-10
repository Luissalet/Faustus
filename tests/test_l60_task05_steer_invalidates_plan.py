"""TASK-05 — Steering y cambios del usuario.

Acceptance: "La corrección del usuario durante un test queda en el estado
antes del siguiente cambio de archivos" — a steer that arrives mid-turn
(`agent_runs.queue_steer` / `take_steers`) while some plan steps are still
`pending` must be reflected in the plan BEFORE the next round can act on
those steps as if nothing had changed.

`plan_state.apply_steer` is the deterministic decision (word-overlap, no
model, mirrors `coverage()`'s own contract); `agent_loop._apply_steer_to_plan_update`
is the thin wiring the round loop calls at its steer safe-point (between
rounds, never mid tool call — the same place `queue_steer`'s own docstring
names). These tests demonstrate the gap this closes: before this lote,
neither function existed and a steer never touched `PlanStep.status` at all
(`grep -c 'needs_review' src/plan_state.py` on the pre-lote file is 0).
"""
from __future__ import annotations

from src import agent_loop, plan_state
from src.plan_state import Plan, PlanStep, apply_steer, from_markdown, to_markdown


def _plan_update(markdown: str) -> dict:
    plan = from_markdown(markdown)
    payload = {"plan": markdown}
    payload.update(plan.to_dict())
    return payload


# --- plan_state.apply_steer: the deterministic decision -------------------

def test_steer_marks_the_pending_step_it_contradicts_needs_review():
    plan = from_markdown("- [ ] write config.py with the old auth flow\n- [ ] write the changelog")
    new_plan, affected = apply_steer(plan, "actually, skip the old auth flow entirely, use OAuth instead")
    by_id = {s.id: s for s in new_plan.steps}
    assert len(affected) == 1
    touched = by_id[affected[0]]
    assert touched.status == "needs_review"
    assert "old auth flow" in touched.title
    assert "steer:" in touched.notes
    # the unrelated step is untouched
    other = [s for s in new_plan.steps if s.id not in affected][0]
    assert other.status == "pending"


def test_steer_leaves_done_and_blocked_steps_alone():
    plan = from_markdown("- [x] write config.py with the old auth flow\n- [ ] [blocked] write the changelog")
    new_plan, affected = apply_steer(plan, "skip the old auth flow entirely")
    assert affected == []
    assert [s.status for s in new_plan.steps] == ["done", "blocked"]


def test_unrelated_steer_touches_nothing():
    plan = from_markdown("- [ ] write config.py\n- [ ] write the changelog")
    new_plan, affected = apply_steer(plan, "what's the weather like today")
    assert affected == []
    assert new_plan is plan  # no-op returns the same object, not a copy


def test_steer_bumps_revision_only_when_something_changed():
    plan = from_markdown("- [ ] write config.py with the old auth flow")
    unrelated_plan, _ = apply_steer(plan, "unrelated text entirely")
    assert unrelated_plan.revision == plan.revision
    changed_plan, affected = apply_steer(plan, "drop the old auth flow")
    assert affected
    assert changed_plan.revision == plan.revision + 1


def test_needs_review_round_trips_through_markdown():
    plan = from_markdown("- [ ] write config.py with the old auth flow")
    new_plan, affected = apply_steer(plan, "drop the old auth flow")
    assert affected
    markdown = to_markdown(new_plan)
    assert plan_state.NEEDS_REVIEW_MARKER in markdown
    reparsed = from_markdown(markdown)
    assert reparsed.steps[0].status == "needs_review"


def test_empty_plan_and_empty_steer_are_no_ops():
    plan = Plan(steps=[])
    assert apply_steer(plan, "anything") == (plan, [])
    non_empty = from_markdown("- [ ] write config.py")
    assert apply_steer(non_empty, "   ") == (non_empty, [])


# --- agent_loop._apply_steer_to_plan_update: the round-loop wiring --------

def test_wiring_marks_pending_step_before_the_next_tool_call():
    """The exact scenario TASK-05 names: two write tool calls with a steer
    queued in between. Round 1 ran `update_plan` (the payload the loop keeps
    in `_latest_plan_update`) and a first write tool call for step 1; a
    steer then arrives contradicting the SECOND (still pending) step before
    round 2's write tool call would otherwise run against the stale plan."""
    plan_update = _plan_update(
        "- [x] write config.py\n- [ ] write auth.py using the legacy session tokens"
    )
    # Simulates: round 1 already executed one write tool call (config.py,
    # done) and update_plan reported the state above; NOW a steer drains at
    # round 2's safe point, before that round's write tool call for auth.py.
    new_payload, affected = agent_loop._apply_steer_to_plan_update(
        plan_update, ["forget legacy session tokens, use JWT for auth.py instead"])
    assert len(affected) == 1
    steps_by_id = {s["id"]: s for s in new_payload["steps"]}
    assert steps_by_id[affected[0]]["status"] == "needs_review"
    # the already-done step is untouched — TASK-05 only protects work that
    # has not happened yet, not settled history
    done_steps = [s for s in new_payload["steps"] if s["status"] == "done"]
    assert len(done_steps) == 1
    assert done_steps[0]["title"] == "write config.py"
    # the payload's own `plan` markdown reflects it too (what a client that
    # only reads `plan` — not `steps` — still sees)
    assert plan_state.NEEDS_REVIEW_MARKER in new_payload["plan"]


def test_wiring_is_a_no_op_with_no_plan_yet():
    assert agent_loop._apply_steer_to_plan_update(None, ["some steer"]) == (None, [])


def test_wiring_is_a_no_op_with_no_steer_text():
    plan_update = _plan_update("- [ ] write config.py")
    assert agent_loop._apply_steer_to_plan_update(plan_update, []) == (plan_update, [])


def test_wiring_never_raises_on_a_malformed_plan_update():
    assert agent_loop._apply_steer_to_plan_update({"plan": None, "steps": "not a list"}, ["x"]) == (
        {"plan": None, "steps": "not a list"}, [])


def test_wiring_applies_multiple_steers_in_order_without_double_counting():
    plan_update = _plan_update("- [ ] write auth.py using legacy session tokens")
    new_payload, affected = agent_loop._apply_steer_to_plan_update(
        plan_update, ["drop legacy session tokens", "use JWT for auth.py"])
    assert affected == [new_payload["steps"][0]["id"]]  # touched once, not twice
