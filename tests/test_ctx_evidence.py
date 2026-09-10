"""CTX-03 — working window and exact evidence (docs/spec/v2 backlog).

`evidence_for_read` pins a file window with its line range and a hash of
what was actually read, so a later edit can demand a matching
`base_revision` (EDIT-01) instead of trusting the file has not moved.
`evidence_index` says which of those refs are actually in this round's
context, the way `build_ledger` already reports everything else about it.
"""

from src.context_ledger import (
    build_ledger,
    evidence_for_read,
    evidence_index,
    verify_read_evidence,
)
from src.contracts.tool import EvidenceRef


class TestEvidenceForRead:
    def test_returns_a_well_formed_evidence_ref(self):
        ev = evidence_for_read("src/foo.py", 10, 42, "some file content\nline two", owner_id="me")
        assert isinstance(ev, EvidenceRef)
        assert ev.source_type == "file"
        assert ev.source_ref == "src/foo.py"
        assert ev.locator.kind == "lines"
        assert ev.locator.value == "10-42"
        assert len(ev.content_sha256) == 64
        assert ev.owner_id == "me"

    def test_evidence_id_is_stable_for_the_same_read(self):
        ev1 = evidence_for_read("src/foo.py", 1, 5, "same content")
        ev2 = evidence_for_read("src/foo.py", 1, 5, "same content")
        assert ev1.evidence_id == ev2.evidence_id

    def test_evidence_id_changes_when_the_range_or_content_changes(self):
        base = evidence_for_read("src/foo.py", 1, 5, "content A")
        other_range = evidence_for_read("src/foo.py", 6, 10, "content A")
        other_content = evidence_for_read("src/foo.py", 1, 5, "content B")
        assert base.evidence_id != other_range.evidence_id
        assert base.evidence_id != other_content.evidence_id

    def test_to_mapping_round_trips_through_from_mapping(self):
        ev = evidence_for_read("src/foo.py", 1, 3, "abc", owner_id="me", project_id="proj1")
        mapping = ev.to_mapping()
        rebuilt = EvidenceRef.from_mapping(mapping)
        assert rebuilt == ev


class TestVerifyReadEvidence:
    def test_matching_content_verifies(self):
        ev = evidence_for_read("src/foo.py", 1, 1, "unchanged line")
        assert verify_read_evidence(ev, "unchanged line") is True

    def test_changed_content_fails_verification(self):
        """This is the EDIT-01 hook: a file that moved under the agent's feet
        must not be reported as still matching the captured evidence."""
        ev = evidence_for_read("src/foo.py", 1, 1, "original line")
        assert verify_read_evidence(ev, "a DIFFERENT line now") is False

    def test_non_evidence_ref_never_verifies(self):
        assert verify_read_evidence(None, "text") is False
        assert verify_read_evidence("not-an-evidence-ref", "text") is False


class TestEvidenceIndex:
    def test_empty_for_messages_with_no_evidence(self):
        assert evidence_index([{"role": "user", "content": "hi"}]) == {}
        assert evidence_index(None) == {}
        assert evidence_index([]) == {}

    def test_finds_evidence_stamped_on_a_message(self):
        ev = evidence_for_read("src/foo.py", 1, 10, "content")
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "here is the file",
             "metadata": {"evidence_refs": [ev.to_mapping()]}},
        ]
        idx = evidence_index(messages)
        assert ev.evidence_id in idx
        assert idx[ev.evidence_id]["message_index"] == 1

    def test_an_evidence_id_not_in_the_prompt_is_simply_absent(self):
        """A compacted-away evidence is not "gone" — it is just not part of
        THIS round's window, the same way build_ledger only reports what is
        actually being sent."""
        ev = evidence_for_read("src/foo.py", 1, 10, "content")
        idx = evidence_index([{"role": "user", "content": "no evidence here"}])
        assert ev.evidence_id not in idx

    def test_build_ledger_reports_evidence_in_window(self):
        ev = evidence_for_read("src/foo.py", 1, 10, "content")
        messages = [
            {"role": "user", "content": "here is the file",
             "metadata": {"evidence_refs": [ev.to_mapping()]}},
        ]
        ledger = build_ledger(messages)
        assert "evidence" in ledger
        assert ev.evidence_id in ledger["evidence"]

    def test_malformed_metadata_is_ignored_not_crashed_on(self):
        messages = [
            {"role": "user", "content": "x", "metadata": {"evidence_refs": "not-a-list"}},
            {"role": "user", "content": "y", "metadata": {"evidence_refs": [None, 42, "str"]}},
            "not-a-dict",
        ]
        assert evidence_index(messages) == {}
