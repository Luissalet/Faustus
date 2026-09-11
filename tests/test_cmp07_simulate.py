"""tests/test_cmp07_simulate.py — CMP-07 (W2-E, CONTRATO_CMP_W2.md).

`src/workflows/simulate.py::simulate` is a structural, round-by-round walk
of a `WorkflowDefinition` — no handler is ever called, no store is ever
opened. These tests pin the four things the fiche's INFORME §3.6 asks for:

1. which nodes would activate, and in what round;
2. that a `human_approval` node reached is always a real wait, regardless
   of whether the caller supplied a guess for it;
3. which branches are NOT taken (a `condition`/`human_approval` node the
   caller told to fail/deny cascades to everything that needs it);
4. that `needs` are AND dependencies, never control branches — a node
   downstream of an undecided or denied gate never activates just because
   some OTHER path in the drawing looks like it reaches it.

`-p no:cacheprovider -W ignore` per COMUN.
"""
from __future__ import annotations

import pytest

from src.contracts.workflow import WorkflowDefinition
from src.workflows.simulate import simulate


def _gated_definition() -> dict:
    """start(manual) -> [audit(artifact_store), gate(condition)]
    gate -> approve(human_approval) -> send(deliver)

    `audit` is an independent branch off `start` alone, so it must activate
    even while `gate`'s branch stays undecided — proof the walk does not
    stall the whole graph on one unresolved gate."""
    return {
        "id": "cmp07.gated", "version": "1.0.0", "title": "Gated send",
        "nodes": [
            {"id": "start", "type": "manual"},
            {"id": "audit", "type": "artifact_store", "needs": ["start"]},
            {"id": "gate", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
            {"id": "approve", "type": "human_approval", "needs": ["gate"]},
            {"id": "send", "type": "deliver", "needs": ["approve"], "config": {"backend": "email"}},
        ],
    }


def test_no_choices_leaves_the_gated_branch_awaiting_but_activates_the_independent_one():
    result = simulate(_gated_definition())
    assert "start" in result.activated
    assert "audit" in result.activated
    # The condition itself was reached (its needs were satisfied) but no
    # choice was given for it — undecided, not guessed as passing.
    assert result.awaiting_choice["gate"]["kind"] == "condition"
    assert result.awaiting_choice["gate"]["blocked_by"] == "gate"
    # Everything downstream of the undecided gate points back at the SAME
    # blocking node, not at its own immediate (also-undecided) parent.
    assert result.awaiting_choice["approve"]["blocked_by"] == "gate"
    assert result.awaiting_choice["send"]["blocked_by"] == "gate"
    assert "approve" not in result.human_waits  # never reached: it's blocked upstream
    assert "gate" not in result.activated and "gate" not in result.not_taken


def test_condition_false_cascades_not_taken_through_the_whole_downstream_branch():
    result = simulate(_gated_definition(), choices={"gate": False})
    assert result.not_taken == ("approve", "gate", "send")
    assert "audit" in result.activated  # unrelated branch: unaffected
    assert "approve" not in result.human_waits  # never actually reached (not run at all)


def test_condition_true_reaches_the_approval_and_records_it_as_a_real_human_wait():
    result = simulate(_gated_definition(), choices={"gate": True})
    assert "gate" in result.activated
    # Reached, and a real run always pauses here — recorded even though no
    # choice was supplied for it (distinct from `activated`/`not_taken`).
    assert "approve" in result.human_waits
    assert result.awaiting_choice["approve"]["kind"] == "human_approval"
    assert "send" not in result.activated and "send" not in result.not_taken
    assert result.awaiting_choice["send"]["blocked_by"] == "approve"


def test_approval_granted_lets_the_final_node_activate():
    result = simulate(_gated_definition(), choices={"gate": True, "approve": True})
    assert result.activated == ("approve", "audit", "gate", "send", "start")
    assert "approve" in result.human_waits  # still a real wait, even though granted here
    assert result.not_taken == ()
    assert result.awaiting_choice == {}


def test_approval_denied_cascades_not_taken_but_still_counts_as_a_human_wait():
    result = simulate(_gated_definition(), choices={"gate": True, "approve": False})
    assert "send" in result.not_taken
    assert "approve" in result.not_taken
    assert "approve" in result.human_waits  # a denial is still a real pause that happened


def test_and_dependencies_are_never_bypassed_by_a_second_path():
    """A node with TWO needs — one clean, one gated by an undecided
    condition — must not activate just because its other dependency is
    fine. This is the literal "AND ≠ control branch" acceptance check."""
    definition = {
        "id": "cmp07.and", "version": "1.0.0", "title": "AND gate",
        "nodes": [
            {"id": "start", "type": "manual"},
            {"id": "gate", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": {"path": "inputs.x"}, "op": "truthy"}}},
            {"id": "clean", "type": "artifact_store", "needs": ["start"]},
            {"id": "join", "type": "deliver", "needs": ["clean", "gate"], "config": {"backend": "email"}},
        ],
    }
    result = simulate(definition)  # no choice for `gate`
    assert "clean" in result.activated
    assert "join" not in result.activated
    assert result.awaiting_choice["join"]["blocked_by"] == "gate"

    result_false = simulate(definition, choices={"gate": False})
    assert "join" in result_false.not_taken  # one AND leg failed -> the join never runs


def test_warnings_always_state_the_and_not_branch_rule():
    result = simulate(_gated_definition())
    assert any("AND dependencies" in w for w in result.warnings)


def test_rounds_max_caps_depth_and_is_reported_not_silent():
    result = simulate(_gated_definition(), rounds_max=1)
    assert result.rounds_used == 1
    # start resolves round 1; everything needing it is one layer deeper.
    assert "audit" in result.not_reached
    assert "gate" in result.not_reached
    assert any("never reached" in w for w in result.warnings)


def test_accepts_a_parsed_workflowdefinition_or_a_raw_mapping_identically():
    raw = _gated_definition()
    parsed = WorkflowDefinition.parse(raw)
    a = simulate(raw, choices={"gate": True})
    b = simulate(parsed, choices={"gate": True})
    assert a.activated == b.activated
    assert a.human_waits == b.human_waits


def test_invalid_rounds_max_is_refused():
    with pytest.raises(ValueError):
        simulate(_gated_definition(), rounds_max=0)
    with pytest.raises(ValueError):
        simulate(_gated_definition(), rounds_max=True)  # bool is not a real int here


def test_a_cyclic_hand_built_definition_stalls_honestly_instead_of_crashing():
    """`.parse()` refuses a cycle — this bypasses it with a hand-built
    dataclass, the same defensive case `agent_profile_lint`'s tests use, to
    confirm simulate() does not infinite-loop or raise on one, and reports
    the stuck nodes rather than crashing."""
    from src.contracts.workflow import WorkflowDefinition as WD, WorkflowNode as WN
    nodes = (
        WN(id="a", type="manual", needs=("b",)),
        WN(id="b", type="manual", needs=("a",)),
    )
    cyclic = WD(id="cmp07.cycle", version="1.0.0", title="cycle", nodes=nodes)
    result = simulate(cyclic, rounds_max=5)
    assert result.activated == ()
    assert set(result.not_reached) == {"a", "b"}


def test_simulate_never_calls_a_real_handler(monkeypatch):
    """Spy proof for the module docstring's central claim: simulate() does
    not import or call anything in workflows/handlers.py."""
    import src.workflows.handlers as handlers

    def _boom(*a, **k):
        raise AssertionError("simulate() must never call a real node handler")

    monkeypatch.setattr(handlers, "condition_handler", _boom)
    monkeypatch.setattr(handlers, "evaluate", _boom)
    result = simulate(_gated_definition(), choices={"gate": True, "approve": True})
    assert "send" in result.activated
