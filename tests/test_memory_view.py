"""The memory view (src/memory_view) — what one run was shown, and what it was not.

The dropped list is the reason this module exists. Without it, "the model knew
about the brand voice" cannot be checked; with it, a wrong answer splits into
an entry that was included and one that was cut for budget, which are two
different bugs with two different fixes.

The one security property here is that scope is a wall: a project entry does
not reach a run in another project even when that run declared `project`
readable. Everything else is determinism and honesty about the cut.
"""
from __future__ import annotations

import pytest

from src import memory_view
from src.contracts import MemoryEntry


def entry(eid, scope="user", body="a rule", trust="candidate", **over):
    body_dict = {"id": eid, "scope": scope, "body": body, "trust": trust}
    body_dict.update(over)
    return MemoryEntry.parse(body_dict)


def test_scope_is_a_wall_and_the_wall_is_reported():
    entries = [
        entry("m1", "user", "always answer in Spanish"),
        entry("m2", "project", "the brand voice is dry", project_id="campaign-a"),
        entry("m3", "project", "another client's voice", project_id="campaign-b"),
        entry("m4", "skill", "video skill note", skill_id="media.clip"),
    ]
    view, kept = memory_view.build(entries, scopes=("user", "project"),
                                   project_id="campaign-a", run_id="r1")
    assert [e.id for e in kept] == ["m1", "m2"]
    reasons = {d["id"]: d["reason"] for d in view.dropped}
    assert reasons == {"m3": "scope", "m4": "scope"}
    # And it says which wall, not just "scope".
    detail = next(d["detail"] for d in view.dropped if d["id"] == "m3")
    assert "campaign-a" in detail


def test_the_budget_cut_is_named_rather_than_silent():
    # Distinct bodies on purpose: identical ones are cut as duplicates before
    # the budget is ever consulted, which is a different reason entirely.
    entries = [entry(f"m{i}", body=f"{i}" + "x" * 99, trust="proven") for i in range(10)]
    view, kept = memory_view.build(entries, scopes=("user",), budget_chars=250)
    assert len(kept) == 2
    assert view.used_chars == 202
    dropped = [d for d in view.dropped if d["reason"] == "budget"]
    assert len(dropped) == 8
    assert "would pass the 250 budget" in dropped[0]["detail"]


def test_anti_patterns_are_spent_on_first():
    entries = [
        entry("candidate", body="maybe do this"),
        entry("proven", body="do this", trust="proven"),
        entry("avoid", body="do not do this", trust="anti_pattern",
              inverted_from="proven"),
    ]
    view, kept = memory_view.build(entries, scopes=("user",), budget_chars=20)
    assert [e.id for e in kept] == ["avoid"]
    assert memory_view.render(kept) == "AVOID: do not do this  (inverted from proven)"


def test_a_retired_rule_is_dropped_and_says_so():
    view, kept = memory_view.build(
        [entry("gone", trust="retired"), entry("live", trust="proven")],
        scopes=("user",))
    assert [e.id for e in kept] == ["live"]
    assert view.dropped[0] == {"id": "gone", "reason": "retired", "detail": ""}


def test_the_same_rule_twice_is_one_rule_and_the_other_is_named():
    view, kept = memory_view.build([
        entry("first", body="Prefer short sentences.", trust="proven"),
        entry("second", body="  prefer   SHORT sentences.  ", trust="proven"),
    ], scopes=("user",))
    assert [e.id for e in kept] == ["first"]
    assert view.dropped[0]["reason"] == "duplicate"
    assert "same text as first" in view.dropped[0]["detail"]


def test_the_same_inputs_give_the_same_view_twice():
    entries = [entry(f"m{i}", body=f"rule {i}", trust="proven",
                     created_at="2026-09-01T00:00:00Z") for i in range(5)]
    a, _ = memory_view.build(entries, scopes=("user",), budget_chars=100)
    b, _ = memory_view.build(list(reversed(entries)), scopes=("user",), budget_chars=100)
    assert a.entry_ids == b.entry_ids
    assert a.fingerprint() == b.fingerprint()


def test_a_view_that_saw_less_than_it_should_has_to_say_why():
    from src.contracts import ContractError
    with pytest.raises(ContractError):
        memory_view.build([], scopes=("user",), degraded=True)
    view, _ = memory_view.build([], scopes=("user",), degraded=True,
                                degraded_reason="no vector store; lexical only")
    assert view.degraded is True
    assert "lexical only" in memory_view.explain(view)


def test_the_explanation_names_every_reason_it_cut_something():
    entries = [
        entry("kept", body="short", trust="proven"),
        entry("other-project", "project", "x", project_id="b"),
        entry("retired-one", trust="retired"),
        entry("too-big", body="y" * 500, trust="proven"),
    ]
    view, kept = memory_view.build(entries, scopes=("user", "project"),
                                   project_id="a", budget_chars=50)
    text = memory_view.explain(view)
    assert text.startswith("1 entries in scope ['user', 'project']")
    for reason in ("budget", "retired", "scope"):
        assert reason in text
    assert "nothing was dropped" not in text


def test_nothing_dropped_says_so_rather_than_leaving_a_blank():
    view, _ = memory_view.build([entry("only", trust="proven")], scopes=("user",))
    assert "nothing was dropped" in memory_view.explain(view)
    assert memory_view.render([]) == ""
