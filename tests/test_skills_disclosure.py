"""Job D — explicit progressive disclosure for skills
(src/skills_runtime/disclosure.py): level 0 (index + trigger, budgeted),
level 1 (procedure body for selected skills only, budgeted), and that level
2 (a referenced file) stays a lazy, per-file read with no budget of its own.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.skills_runtime import disclosure  # noqa: E402


def _index(n, desc_len=10):
    return [
        {"name": f"skill-{i}", "description": "d" * desc_len, "category": "general",
         "when_to_use": f"when doing task {i}", "status": "published"}
        for i in range(n)
    ]


def test_level0_includes_trigger_and_stays_under_budget():
    result = disclosure.render_level0(_index(3), budget_tokens=1000)
    assert result.level == 0
    assert result.dropped == []
    assert "when: when doing task 0" in result.text
    assert result.tokens <= 1000


def test_level0_drops_whole_entries_never_truncates_a_line():
    idx = _index(50, desc_len=40)
    result = disclosure.render_level0(idx, budget_tokens=120)
    assert result.dropped, "a small budget over 50 entries must drop something"
    assert result.included
    # Everything that made it in is a complete, well-formed line.
    for name in result.included:
        assert f"`{name}`" in result.text
    for name in result.dropped:
        assert f"`{name}`" not in result.text
    assert disclosure._tokens(result.text) <= 120 + 1  # rounding slack


def test_level0_empty_index_is_empty_text():
    result = disclosure.render_level0([], budget_tokens=400)
    assert result.text == ""
    assert result.included == []
    assert result.dropped == []


def _skills(n, body_len=100):
    return [
        {"name": f"sel-{i}", "description": "x" * body_len,
         "when_to_use": "when relevant", "procedure": ["step one", "step two"],
         "pitfalls": ["watch out"]}
        for i in range(n)
    ]


def test_level1_renders_full_procedure_for_selected_skills():
    result = disclosure.render_level1(_skills(2), budget_tokens=1000)
    assert result.level == 1
    assert "step one" in result.text
    assert "watch out" in result.text
    assert result.dropped == []


def test_level1_drops_whole_skills_never_truncates_one():
    result = disclosure.render_level1(_skills(10, body_len=200), budget_tokens=150)
    assert result.included, "at least the first skill must fit"
    assert result.dropped
    # The one skill kept is rendered whole (its pitfalls line is present).
    for name in result.included:
        assert f"### {name}" in result.text
        assert "watch out" in result.text.split(f"### {name}")[1].split("###")[0]


def test_level1_keeps_at_least_one_skill_even_if_it_alone_exceeds_budget():
    result = disclosure.render_level1(_skills(1, body_len=5000), budget_tokens=10)
    assert result.included == ["sel-0"]
    assert result.dropped == []


def test_level1_empty_selection_is_empty_text():
    result = disclosure.render_level1([], budget_tokens=500)
    assert result.text == ""


def test_log_line_reports_included_and_dropped_counts():
    result = disclosure.render_level0(_index(20, desc_len=30), budget_tokens=80)
    line = result.log_line()
    assert "level 0" in line
    assert str(len(result.included)) in line
    if result.dropped:
        assert "dropped for budget" in line


def test_index_for_carries_the_when_to_use_trigger(tmp_path, monkeypatch):
    from services.memory.skills import SkillsManager

    sm = SkillsManager(str(tmp_path))
    sm.add_skill(
        name="my-skill",
        title="My skill",
        description="Does a thing",
        category="general",
        procedure=["do the thing"],
        when_to_use="when the user asks for the thing",
        status="published",
        source="user",
    )
    idx = sm.index_for(owner=None)
    assert idx
    row = next(r for r in idx if r["name"] == "my-skill")
    assert row["when_to_use"] == "when the user asks for the thing"


def test_log_level2_use_never_raises(caplog):
    disclosure.log_level2_use("some-skill", "reference.md")
