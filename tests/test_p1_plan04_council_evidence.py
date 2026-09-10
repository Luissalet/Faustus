"""PLAN-04 — council with evidence, not a headcount.

Acceptance: three models repeating the same claim with no source do not
substitute for a check.
"""
from __future__ import annotations

from src.council import synthesis
from src.council.contracts import CouncilDecision, CouncilMessage


def _decision(supporters):
    return CouncilDecision.parse({
        "session_id": "s1", "question": "which fix?", "status": "decided",
        "chosen": "patch A", "supporters": supporters,
    })


def _message(author_id, decision_id, evidence_refs=None):
    meta = {"evidence_refs": evidence_refs} if evidence_refs else {}
    return CouncilMessage.parse({
        "session_id": "s1", "author_id": author_id, "decision_id": decision_id,
        "content": "patch A is correct", "metadata": meta,
    })


def test_three_supporters_with_no_evidence_are_unanimous_without_evidence():
    dec = _decision(["m1", "m2", "m3"])
    messages = [_message("m1", dec.id), _message("m2", dec.id), _message("m3", dec.id)]
    result = synthesis.evidence_weighted_support(dec, messages)
    assert result["raw_count"] == 3
    assert result["with_evidence"] == 0
    assert result["unanimous_without_evidence"] is True
    # Discounted, never a full headcount, for the exact case the acceptance
    # criterion names: repetition without a source is not a stronger tally.
    assert result["weighted_count"] < result["raw_count"]


def test_one_supporter_with_evidence_outweighs_bare_repetition():
    dec = _decision(["m1", "m2"])
    messages = [
        _message("m1", dec.id, evidence_refs=["run#42 output: test suite green"]),
        _message("m2", dec.id),  # no source
    ]
    result = synthesis.evidence_weighted_support(dec, messages)
    assert result["with_evidence"] == 1
    assert result["unanimous_without_evidence"] is False
    row_by_id = {r["id"]: r for r in result["supporters"]}
    assert row_by_id["m1"]["has_evidence"] is True
    assert row_by_id["m2"]["has_evidence"] is False
    assert row_by_id["m1"]["weight"] > row_by_id["m2"]["weight"]


def test_a_later_evidence_free_message_does_not_inherit_an_earlier_citation():
    """Only the LATEST message from a given author on this decision counts —
    a restatement that drops the source must not still read as evidenced."""
    dec = _decision(["m1"])
    messages = [
        _message("m1", dec.id, evidence_refs=["earlier check"]),
        _message("m1", dec.id),  # same author, later, no evidence this time
    ]
    result = synthesis.evidence_weighted_support(dec, messages)
    assert result["with_evidence"] == 0
    assert result["unanimous_without_evidence"] is True


def test_no_supporters_is_not_reported_as_unanimous_without_evidence():
    dec = _decision([])
    result = synthesis.evidence_weighted_support(dec, [])
    assert result["raw_count"] == 0
    assert result["unanimous_without_evidence"] is False
