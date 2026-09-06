"""Blackboard behaviour (plan §11, required tests in §22 "Multiagente").

What these pin, in the order the plan states them: findings are append-only, a
correction creates `supersedes`, search filters by scope, a finding with no
evidence is not promoted to a validated fact, and ending a task expires the
provisional rows.  Plus the two rules the plan states as prose and the module
states as refusals: nobody edits somebody else's row, and `promote()` promotes
nothing.
"""

from __future__ import annotations

import pytest

from src.context_engine import shared_memory as bb
from src.context_engine import store


@pytest.fixture(autouse=True)
def context_store(tmp_path):
    """Every test gets its own database file and gives the path back."""
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _post(**over):
    payload = {
        "scope": "council-7",
        "owner": "alice",
        "author": "worker-security",
        "topic": "oauth-state",
        "kind": "fact",
        "claim": "the state is consumed after the exchange",
        "evidence_refs": ["file:src/auth/oauth.py#L120"],
        "tags": ["security", "oauth"],
    }
    payload.update(over)
    return bb.post(**payload)


# ── append-only ───────────────────────────────────────────────────────────

def test_the_board_has_no_update_verb():
    assert not hasattr(bb, "update")
    assert not hasattr(bb, "edit")
    assert "update" not in bb.__all__


def test_a_correction_is_a_new_row_that_names_the_old_one():
    original = _post()
    correction = bb.supersede(
        original.id, author="worker-security",
        claim="the state is consumed BEFORE the exchange",
        evidence_refs=["file:src/auth/oauth.py#L131"],
        reason="misread the order")

    assert correction.id != original.id
    assert correction.supersedes == original.id
    assert correction.kind == "correction"
    assert bb.get(original.id).status == "superseded"
    assert bb.get(original.id).claim == original.claim, "the old claim is untouched"


def test_the_superseded_row_stops_being_served_but_still_exists():
    original = _post()
    bb.supersede(original.id, author="worker-security", claim="not that",
                 evidence_refs=["file:src/auth/oauth.py#L131"])

    served = bb.search(scope="council-7")
    assert original.id not in [f.id for f in served]
    assert bb.get(original.id) is not None


def test_a_peer_may_not_supersede_or_withdraw_another_authors_finding():
    original = _post()

    with pytest.raises(bb.FindingRejected) as superseding:
        bb.supersede(original.id, author="worker-tests", claim="wrong",
                     evidence_refs=["file:tests/test_oauth.py#L4"])
    assert "worker-tests" in str(superseding.value)

    with pytest.raises(bb.FindingRejected):
        bb.withdraw(original.id, author="worker-tests")

    assert bb.get(original.id).status == "open", "no foreign write touched the row"


def test_an_author_withdraws_their_own_and_the_row_survives():
    original = _post()
    withdrawn = bb.withdraw(original.id, author="worker-security",
                            reason="I was reading the wrong branch")

    assert withdrawn.status == "withdrawn"
    assert bb.get(original.id).status == "withdrawn"
    assert bb.get(original.id).claim == original.claim
    assert bb.search(scope="council-7") == []


def test_supersedes_cannot_be_forged_through_post():
    original = _post()
    with pytest.raises(bb.FindingRejected):
        _post(supersedes=original.id, claim="sneaky")
    assert bb.get(original.id).status == "open"


# ── evidence ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["fact", "result"])
def test_a_claim_about_code_or_a_result_needs_evidence(kind):
    with pytest.raises(bb.FindingRejected) as rejected:
        _post(kind=kind, evidence_refs=[])
    assert "evidence_refs" in rejected.value.path
    assert bb.stats(scope="council-7")["total"] == 0


@pytest.mark.parametrize("kind", ["question", "proposal", "objection"])
def test_a_draft_does_not_need_evidence(kind):
    finding = _post(kind=kind, evidence_refs=[], claim="should we cache this?")
    assert finding.kind == kind
    assert finding.evidence_refs == ()


def test_correcting_a_fact_needs_evidence_of_its_own():
    original = _post()
    with pytest.raises(bb.FindingRejected):
        bb.supersede(original.id, author="worker-security", claim="no it is not")
    assert bb.get(original.id).status == "open"


# ── isolation ─────────────────────────────────────────────────────────────

def test_search_filters_by_scope():
    here = _post(scope="council-7")
    _post(scope="council-9", claim="a different council entirely")

    found = bb.search(scope="council-7")
    assert [f.id for f in found] == [here.id]


def test_search_does_not_mix_owners():
    mine = _post(owner="alice")
    _post(owner="bob", claim="bob's own reading of the same file")

    assert [f.id for f in bb.search(scope="council-7", owner="alice")] == [mine.id]
    assert [f.id for f in bb.search(scope="council-7", owner="bob")] != [mine.id]


def test_search_filters_by_kind_topic_tags_and_words():
    risk = _post(kind="risk", topic="logging", tags=["security"],
                 claim="the refresh token is written to the log",
                 evidence_refs=["file:src/auth/log.py#L9"])
    _post(kind="proposal", topic="caching", tags=["perf"], evidence_refs=[],
          claim="we could memoise the discovery document")

    assert [f.id for f in bb.search(scope="council-7", kind="risk")] == [risk.id]
    assert [f.id for f in bb.search(scope="council-7", topic="logging")] == [risk.id]
    assert [f.id for f in bb.search(scope="council-7", tags=["security"])] == [risk.id]
    assert [f.id for f in bb.search(scope="council-7", query="token log")] == [risk.id]
    assert bb.search(scope="council-7", kind="not-a-kind") == []


def test_search_honours_k():
    for n in range(5):
        _post(claim=f"observation number {n}")
    assert len(bb.search(scope="council-7", k=2)) == 2


# ── TTL ───────────────────────────────────────────────────────────────────

def test_ending_the_execution_expires_the_provisional_rows():
    provisional = _post(scope="run-1", kind="risk", topic="logging",
                        evidence_refs=[], claim="the refresh token reaches the log")
    kept = _post(scope="run-1", kind="result", topic="tests", status="resolved",
                 claim="the suite passes on the fix", evidence_refs=["run:1234"])

    changed = bb.expire("run-1")
    assert changed == 2

    served = bb.search(scope="run-1", status="")
    assert [f.id for f in served] == [kept.id]
    assert bb.get(provisional.id) is not None, "expiry marks, it does not delete"
    assert bb.get(provisional.id).status == "open"


def test_expiry_does_not_reach_into_another_scope():
    elsewhere = _post(scope="run-2", kind="risk", evidence_refs=[],
                      claim="unrelated observation")
    bb.expire("run-1")
    assert [f.id for f in bb.search(scope="run-2")] == [elsewhere.id]


def test_a_resolved_finding_without_evidence_does_not_survive_the_ttl():
    tidy = _post(scope="run-3", kind="question", status="resolved",
                 evidence_refs=[], claim="we agreed to move on")
    bb.expire("run-3")
    assert bb.search(scope="run-3", status="") == []
    assert bb.get(tidy.id) is not None


# ── promotion is a proposal, not an act ───────────────────────────────────

def test_promote_returns_a_proposal_and_writes_nothing():
    finding = _post(status="resolved")
    before = store.table_counts()

    result = bb.promote(finding.id, target="memory")

    assert result["ok"] is True
    assert result["reason"] == ""
    assert result["proposal"]["finding_id"] == finding.id
    assert result["proposal"]["target"] == "memory"
    assert result["proposal"]["requires_approval"] is True
    assert result["proposal"]["evidence_refs"] == list(finding.evidence_refs)
    assert store.table_counts() == before, "promote() wrote to some store"
    assert bb.get(finding.id).to_dict() == finding.to_dict()


def test_a_finding_without_evidence_is_not_promoted():
    draft = _post(kind="question", evidence_refs=[], claim="should we cache this?")
    result = bb.promote(draft.id, target="memory")
    assert result == {"ok": False, "reason": "no_evidence", "proposal": {}}


def test_a_draft_kind_is_not_promoted_even_with_evidence():
    draft = _post(kind="proposal", claim="we could memoise the discovery document",
                  evidence_refs=["file:src/auth/oauth.py#L4"])
    assert bb.promote(draft.id, target="memory")["reason"] == "kind_not_promotable"


def test_promote_refuses_an_unknown_target_and_a_missing_finding():
    finding = _post()
    assert bb.promote(finding.id, target="somewhere")["reason"] == "unknown_target"
    assert bb.promote("finding_nope", target="memory")["reason"] == "no_such_finding"


def test_a_withdrawn_finding_is_not_promoted():
    finding = _post()
    bb.withdraw(finding.id, author="worker-security")
    assert bb.promote(finding.id, target="memory")["reason"] == "status_withdrawn"


# ── candidates: dedupe helps, it does not delete ──────────────────────────

def test_similar_findings_are_grouped_and_both_are_kept():
    first = _post(author="worker-security",
                  claim="the oauth state is consumed after the token exchange")
    second = _post(author="worker-tests",
                   claim="the oauth state gets consumed after the token exchange")
    other = _post(author="worker-perf", topic="caching",
                  claim="discovery documents are refetched on every request",
                  evidence_refs=["file:src/auth/discovery.py#L20"])

    found = bb.search(scope="council-7", k=10)
    assert {f.id for f in found} == {first.id, second.id, other.id}

    by_id = {c.meta["finding_id"]: c for c in bb.as_candidates(found)}
    assert by_id[first.id].meta["similar_to"] == [second.id]
    assert by_id[second.id].meta["similar_to"] == [first.id]
    assert by_id[other.id].meta["similar_to"] == []


def test_candidates_carry_provenance_and_low_trust():
    resolved = _post(status="resolved")
    draft = _post(kind="question", evidence_refs=[], claim="what about PKCE?")

    candidates = {c.meta["finding_id"]: c for c in
                  bb.as_candidates([resolved, draft])}

    for candidate in candidates.values():
        assert candidate.source_type == "finding"
        assert candidate.section == "peer_findings"
        assert candidate.source_ref.startswith("finding:")
        assert candidate.authority == "agent_claim"

    assert candidates[resolved.id].trust_class == "agent_validated"
    assert candidates[draft.id].trust_class == "untrusted"
    assert "file:src/auth/oauth.py#L120" in candidates[resolved.id].body


# ── shape ─────────────────────────────────────────────────────────────────

def test_a_finding_round_trips_through_its_own_dict():
    finding = _post()
    assert bb.Finding.parse(finding.to_dict()) == finding


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(bb.FindingRejected):
        _post(kidn="fact")


def test_stats_counts_what_is_on_the_board():
    _post()
    _post(kind="question", evidence_refs=[], claim="what about PKCE?")
    _post(scope="council-9", claim="a different council entirely")

    scoped = bb.stats(scope="council-7")
    assert scoped["total"] == 2
    assert scoped["served"] == 2
    assert scoped["with_evidence"] == 1
    assert scoped["by_kind"] == {"fact": 1, "question": 1}
    assert scoped["by_status"] == {"open": 2}

    everything = bb.stats()
    assert everything["total"] == 3
    assert everything["scopes"] == ["council-7", "council-9"]


def test_history_is_readable_when_it_is_asked_for_by_name():
    original = _post()
    bb.supersede(original.id, author="worker-security", claim="the other way round",
                 evidence_refs=["file:src/auth/oauth.py#L131"])

    assert [f.id for f in bb.search(scope="council-7", status="superseded")] \
        == [original.id]
    assert original.id not in [f.id for f in bb.search(scope="council-7", status="")]
