"""Completing a report never certifies evidence the harness could not verify."""
import pytest

from src import scorecard
from src.agent_harness import TurnLedger


def entry(harness):
    return scorecard.build_entry(session_id="s", model="m", endpoint_label=None, workspace="/w",
        user_text="Report the work", duration_s=1, rounds=2, harness=harness)


def test_real_claim_advisory_is_complete_but_not_verified(tmp_path):
    ledger = TurnLedger(str(tmp_path), "Report the work")
    ledger.record_claim_advisory({"ok":False,"reasons":["unsupported_claims"],"bad_paths":["export.csv"]},2)
    row = entry(ledger.summary())
    assert row["stop_reason"] == "complete" and not row["verified"] and row["unverified"]
    assert scorecard.aggregate([row])[0]["verified_rate"] == 0


@pytest.mark.parametrize("note", ["claim_advisory:unsupported_claims", "answer_evidence_advisory:wrong_calculation",
    "answer_evidence_advisory@2", "calculation_evidence_advisory", "saved_links_evidence_advisory"])
def test_advisory_notes_are_honest_even_without_a_verification_section(note):
    row = entry({"stop_reason":"complete","notes":[note]})
    assert not row["verified"] and row["unverified"]


@pytest.mark.parametrize("outcome,verified", [("verified",True),("unverified",False),("uncertain",False)])
def test_scorecard_uses_the_persisted_evidence_outcome(outcome,verified):
    row = entry({"stop_reason":"complete","verification":{"outcome":outcome}})
    assert row["verified"] is verified and row["unverified"] is not verified
    assert row["evidence_outcome"] == outcome


def test_legacy_complete_without_new_evidence_retains_its_existing_grade():
    row = entry({"stop_reason":"complete"})
    assert row["verified"] and not row["unverified"] and "evidence_outcome" not in row
