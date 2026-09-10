"""plan_state — structured steps parsed from (and rendered back to) the
plan's markdown checklist, without ever truncating or dropping a step
(TASK-01). These tests pin the round-trip and the >8,192-character case that
the old `plan = plan[:8192]` line in UpdatePlanTool used to break silently.
"""
from src.plan_state import (
    Plan,
    PlanStep,
    from_markdown,
    parse_steps_input,
    stable_step_id,
    to_markdown,
)


def test_basic_checklist_parses_status_and_ids():
    plan = from_markdown("- [x] one\n- [ ] two\n- [ ] three")
    assert [s.status for s in plan.steps] == ["done", "pending", "pending"]
    assert [s.title for s in plan.steps] == ["one", "two", "three"]
    ids = {s.id for s in plan.steps}
    assert len(ids) == 3  # all distinct


def test_step_id_is_stable_across_reparses():
    a = from_markdown("- [ ] write the report")
    b = from_markdown("- [ ] write the report")
    assert a.steps[0].id == b.steps[0].id == stable_step_id("write the report", 0)


def test_step_id_differs_by_position_for_identical_text():
    plan = from_markdown("- [ ] retry\n- [ ] retry")
    assert plan.steps[0].id != plan.steps[1].id


def test_nested_items_carry_depends_on_their_parent():
    plan = from_markdown("- [ ] parent\n  - [ ] child\n  - [x] child2")
    parent, child, child2 = plan.steps
    assert parent.depends_on == []
    assert child.depends_on == [parent.id]
    assert child2.depends_on == [parent.id]


def test_numbered_list_is_accepted():
    plan = from_markdown("1. [x] first\n2. [ ] second")
    assert [s.status for s in plan.steps] == ["done", "pending"]


def test_unparseable_bullet_becomes_a_warning_not_a_dropped_step():
    plan = from_markdown("- just a note, no checkbox\n- [ ] real step")
    assert len(plan.steps) == 1
    assert plan.steps[0].title == "real step"
    assert plan.warnings  # the unparseable line was recorded, not silently gone


def test_long_plan_keeps_every_step():
    # TASK-01: this is the exact shape of plan the 8,192-char truncation used
    # to cut off mid-step. 400 steps of ~30 chars each is well past the cap.
    lines = [f"- [{'x' if i % 3 == 0 else ' '}] step number {i:04d} with some detail text"
             for i in range(400)]
    text = "\n".join(lines)
    assert len(text) > 8192
    plan = from_markdown(text)
    assert len(plan.steps) == 400
    assert plan.steps[-1].title.endswith("0399 with some detail text")


def test_done_step_from_markdown_defaults_unverified():
    # A checkbox the model just ticked is a claim, not evidence: parsed
    # straight off markdown it is always `verified: false` until something
    # (structured input, evidence) says otherwise.
    plan = from_markdown("- [x] shipped it")
    assert plan.steps[0].verified is False


def test_unverified_marker_round_trips_through_to_markdown():
    step = PlanStep(id="step_abc", title="ran the migration", status="done", verified=False)
    md = to_markdown(Plan(steps=[step]))
    assert "[x] ran the migration" in md
    assert "<!-- unverified -->" in md
    reparsed = from_markdown(md)
    assert reparsed.steps[0].status == "done"
    assert reparsed.steps[0].verified is False  # the flag survived the round trip


def test_verified_done_step_has_no_marker_in_markdown():
    step = PlanStep(id="step_abc", title="shipped", status="done", verified=True)
    md = to_markdown(Plan(steps=[step]))
    assert "<!-- unverified -->" not in md


def test_blocked_status_round_trips():
    step = PlanStep(id="step_x", title="waiting on API key", status="blocked")
    md = to_markdown(Plan(steps=[step]))
    reparsed = from_markdown(md)
    assert reparsed.steps[0].status == "blocked"
    assert reparsed.steps[0].title == "waiting on API key"


def test_to_markdown_preserves_nesting_from_depends_on():
    parent = PlanStep(id="p", title="parent", status="pending")
    child = PlanStep(id="c", title="child", status="pending", depends_on=["p"])
    md = to_markdown(Plan(steps=[parent, child]))
    lines = md.splitlines()
    assert lines[0] == "- [ ] parent"
    assert lines[1] == "  - [ ] child"


def test_parse_steps_input_from_json_form():
    plan = parse_steps_input({"steps": [
        {"title": "step one", "status": "done"},
        {"title": "step two"},
    ], "revision": 5})
    assert [s.title for s in plan.steps] == ["step one", "step two"]
    assert plan.steps[0].status == "done"
    assert plan.steps[1].status == "pending"
    assert plan.revision == 5


def test_parse_steps_input_returns_none_for_non_matching_shape():
    assert parse_steps_input({"plan": "- [ ] a"}) is None
    assert parse_steps_input("not a dict") is None


def test_plan_counts():
    plan = from_markdown("- [x] a\n- [x] b\n- [ ] c")
    assert plan.counts() == (2, 3)


def test_a_pending_step_is_never_verified():
    """Seen live on the 7001: a fresh 12-step plan came back with every
    pending step `verified: true`, which reads as "already checked". The flag
    belongs to done steps only, whatever the input claims."""
    plan = from_markdown("- [ ] write the migration\n- [x] read the schema\n")
    assert [s.verified for s in plan.steps] == [False, False]
    step = PlanStep.from_dict({"title": "later", "status": "pending", "verified": True}, order=0)
    assert step.verified is False
    done = PlanStep.from_dict({"title": "shipped", "status": "done", "verified": True}, order=1)
    assert done.verified is True
