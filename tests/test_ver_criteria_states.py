"""VER-01 — explicit acceptance-criterion states, evidence-only transitions.

Uses `contracts.task.AcceptanceCriterion` for the criterion shape and
`src.verification` for the richer state machine the lot asks for
(pending/verified/failed/not_verifiable/waived), which
`AcceptanceCriterion.state` itself does not carry (a different, frozen wire
vocabulary — see src/verification.py's module docstring)."""
import pytest

from src.contracts.task import AcceptanceCriterion
from src import verification as ver


def _criterion(cid="c1"):
    return AcceptanceCriterion(id=cid, description="the export button produces a file")


def test_no_evidence_stays_pending_even_when_nothing_contradicts_it():
    # verify_criterion has no "the model marked this done" parameter at all —
    # only evidence_ok, and it defaults to None (no evidence examined).
    v = ver.verify_criterion(_criterion())
    assert v.state == "pending"


def test_evidence_ok_true_verifies_and_false_fails():
    ok = ver.verify_criterion(_criterion("c1"), evidence_ok=True, evidence_refs=["out.txt"])
    bad = ver.verify_criterion(_criterion("c2"), evidence_ok=False, evidence_refs=["out.txt"])
    assert ok.state == "verified"
    assert bad.state == "failed"


def test_waive_requires_both_by_and_reason():
    with pytest.raises(ValueError):
        ver.waive_criterion(_criterion(), by="", reason="not needed for this build")
    with pytest.raises(ValueError):
        ver.waive_criterion(_criterion(), by="luis", reason="")
    v = ver.waive_criterion(_criterion(), by="luis", reason="covered manually in QA this sprint")
    assert v.state == "waived"
    assert v.waived_by == "luis"
    assert "covered manually" in v.waived_reason


def test_not_verifiable_requires_a_reason():
    with pytest.raises(ValueError):
        ver.mark_not_verifiable(_criterion(), reason="")
    v = ver.mark_not_verifiable(_criterion(), reason="requires a human to look at the rendered UI")
    assert v.state == "not_verifiable"


def test_criteria_summary_blocks_succeeded_only_while_something_is_pending():
    pending = ver.verify_criterion(_criterion("c1"))
    verified = ver.verify_criterion(_criterion("c2"), evidence_ok=True)
    summary = ver.criteria_summary([pending, verified])
    assert summary["total"] == 2
    assert summary["pending"] == 1
    assert summary["blocks_succeeded"] is True
    assert summary["all_settled"] is False

    settled = ver.criteria_summary([verified])
    assert settled["pending"] == 0
    assert settled["blocks_succeeded"] is False
    assert settled["all_settled"] is True


def test_a_failed_or_waived_criterion_does_not_block_succeeded_by_itself():
    # Only "pending" blocks — a failed/waived criterion is the caller's
    # decision to make (retry, accept partial, ...), not this function's.
    failed = ver.verify_criterion(_criterion("c1"), evidence_ok=False)
    waived = ver.waive_criterion(_criterion("c2"), by="luis", reason="out of scope for this release")
    summary = ver.criteria_summary([failed, waived])
    assert summary["pending"] == 0
    assert summary["blocks_succeeded"] is False
    assert summary["failed"] == 1
    assert summary["waived"] == 1
