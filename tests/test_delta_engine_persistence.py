"""The delta store: what it refuses to lose, and what it refuses to change.

Every test here fixes a RULE, not a snapshot. The columns will grow and the
assessment vocabulary may too; none of that may quietly let two live answers to
one question coexist, let one owner read another's comparison, or let a human
edit what a machine observed.

The guarantees, one test each:

* a file that is not a database is moved aside and the store opens clean --
  written as a real corrupt file, because a quarantine nobody triggers is dead
  code that every test of it passes by accident;
* two live deltas with one fingerprint cannot coexist: recomputing REPLACES,
  and the first is left superseded rather than deleted;
* `reclassify` changes the interpretation and NOTHING else -- operation,
  before, after, method, tier and confidence come out identical -- and it
  leaves the actor and the reason behind;
* a reclassification that would contradict the observation is refused, and the
  stored delta is untouched;
* another owner's row answers `None` by id and is invisible to `list_deltas`;
* a read on a broken database answers empty and a write on one raises.

Plus the properties the rest rests on: a frozen intent is never rewritten, a
delta survives a reopen whole, the denormalised projections follow the payload
instead of accumulating beside it, and invalidating a revision retires the
comparisons that read it from either end.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from src.delta_engine import persistence as P
from src.delta_engine.contracts import (
    DeltaRequest,
    IntentContract,
    RevisionRef,
    UniversalDelta,
)

OWNER = "alice"
OTHER = "bob"
FROZEN = "2026-09-06T12:00:00Z"


@pytest.fixture(autouse=True)
def delta_db(tmp_path):
    """Every test gets its own database file."""
    P.use_path(str(tmp_path / "delta_engine.db"))
    try:
        yield
    finally:
        P.use_path(None)


def store() -> P.DeltaStore:
    return P.store()


def digest(seed: str) -> str:
    """A real sha256, because `RevisionRef` refuses anything else."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def revision(name: str) -> RevisionRef:
    return RevisionRef.parse({"kind": "checkpoint", "ref": name,
                              "hash": digest(name)})


def intent(**over) -> IntentContract:
    payload = {"id": "intent_1", "owner": OWNER, "domain": "code",
               "frozen_at": FROZEN,
               "requested": [{"path": "src/auth.py", "condition": "changes"}]}
    payload.update(over)
    return IntentContract.parse(payload)


def request(**over) -> DeltaRequest:
    payload = {"id": "delta_request_1", "owner": OWNER, "domain": "code",
               "source": revision("before").to_dict(),
               "target": revision("after").to_dict(),
               "intent_contract_id": "intent_1"}
    payload.update(over)
    return DeltaRequest.parse(payload)


def assertion(**over) -> dict:
    """One observed change, already interpreted as `unknown`."""
    payload = {"id": "assertion_1", "path": "src/auth.py#consume_state",
               "operation": "modified", "classification": "unknown",
               "confidence": "exact", "tier": "parser", "before": "one_time",
               "after": "reusable", "method": "python_ast",
               "detail": "the state token is no longer consumed"}
    payload.update(over)
    return payload


def delta(**over) -> UniversalDelta:
    payload = {"id": "delta_1", "request_id": "delta_request_1", "owner": OWNER,
               "domain": "code", "source": revision("before").to_dict(),
               "target": revision("after").to_dict(), "assessment": "partial",
               "intent_contract_id": "intent_1",
               "intent_fingerprint": intent().fingerprint(),
               "assertions": [assertion()],
               "coverage": {"source_readable": True, "target_readable": True,
                            "dimensions": {"structural": 1.0}},
               "extractor_versions": {"python_ast": "1.0"}}
    payload.update(over)
    return UniversalDelta.parse(payload)


# -- a damaged file costs the file, not the process -------------------------


def test_a_file_that_is_not_a_database_is_quarantined_and_the_store_opens_clean(
        tmp_path):
    """sqlite opens a non-database happily and only complains on the first read.

    So the corruption is real here -- bytes on disk, not a mocked error -- which
    is the only version of this test that would fail if the probe in `_connect`
    ever moved inside the `contextlib.suppress` above it.
    """
    path = tmp_path / "delta_engine.db"
    path.write_bytes(b"POST /this-is-not-a-database HTTP/1.1\n" * 200)

    stored = store().save_delta(delta())

    assert stored == "delta_1"
    assert (tmp_path / "delta_engine.db.corrupt").exists(), (
        "the unreadable file must be kept beside the new one, not deleted")
    assert store().get_delta("delta_1", owner=OWNER) is not None


# -- §22: one live answer per question --------------------------------------


def test_two_live_deltas_with_one_fingerprint_cannot_coexist():
    """Recomputing the same comparison REPLACES; it does not accumulate."""
    first = delta(id="delta_1")
    second = delta(id="delta_2")
    assert first.fingerprint() == second.fingerprint(), (
        "same source, target, intent and extractors -- the fixture is wrong "
        "if these differ")

    store().save_delta(first)
    store().save_delta(second)

    live = store().find_delta(first.fingerprint(), owner=OWNER)
    assert live is not None and live.id == "delta_2"
    assert [d.id for d in store().list_deltas(owner=OWNER)] == ["delta_2"]

    counts = store().counts(owner=OWNER)
    assert counts["deltas"] == 2, "the superseded one is kept, not deleted"
    assert counts["live_deltas"] == 1
    assert store().get_delta("delta_1", owner=OWNER) is not None, (
        "a decision taken on the old delta must still be able to read it")


def test_a_superseded_delta_is_not_a_cache_hit():
    store().save_delta(delta(id="delta_1"))
    assert store().supersede("delta_1", owner=OWNER, reason="checkpoint redone")

    assert store().find_delta(delta().fingerprint(), owner=OWNER) is None
    assert store().list_deltas(owner=OWNER) == []
    assert store().supersede("delta_1", owner=OWNER) is False, (
        "retiring an already retired delta is not a second retirement")


def test_invalidating_a_revision_retires_the_deltas_that_read_it_either_end():
    """A mis-recorded checkpoint invalidates what compared against it, and it
    does not matter which end of the comparison it was."""
    store().save_delta(delta(id="as_source"))
    store().save_delta(delta(
        id="as_target", source=revision("older").to_dict(),
        target=revision("before").to_dict()))
    store().save_delta(delta(
        id="untouched", source=revision("x").to_dict(),
        target=revision("y").to_dict()))

    retired = store().invalidate_for_revision(digest("before"), owner=OWNER)

    assert retired == 2
    assert [d.id for d in store().list_deltas(owner=OWNER)] == ["untouched"]


def test_invalidating_the_empty_hash_is_refused():
    """An empty hash would match every row whose end was never recorded, which
    is not what a caller invalidating one revision meant."""
    with pytest.raises(P.DeltaStoreError) as caught:
        store().invalidate_for_revision("", owner=OWNER)
    assert "revision_hash" in str(caught.value)


# -- §23: a reclassification never touches the observation -------------------

OBSERVATION_FIELDS = ("path", "operation", "before", "after", "method", "tier",
                      "confidence")


def test_reclassify_changes_the_interpretation_and_nothing_else():
    """The whole point of §23, checked field by field.

    A human saying "that was intentional" is a statement about MEANING. If the
    same call could move `before` and `after`, the record would stop saying what
    the machine saw, and no later reader could tell an observation from an
    opinion about one.
    """
    store().save_delta(delta())
    before = store().get_delta("delta_1", owner=OWNER).assertions[0]

    revised = store().reclassify(
        "delta_1", "assertion_1", owner=OWNER, classification="regression",
        actor="alice", reason="the session fixation fix was undone")

    assert revised is not None
    after = revised.assertions[0]
    assert after.id == before.id, "the assertion keeps its identity"
    for field in OBSERVATION_FIELDS:
        assert getattr(after, field) == getattr(before, field), (
            f"{field} is an observation and must survive a reclassification")
    assert before.classification == "unknown"
    assert after.classification == "regression"
    assert after.severity == "material", (
        "the severity is derived from the new classification by the contract, "
        "not carried over from the old one")


def test_reclassify_supersedes_the_previous_reading_and_records_its_genealogy():
    store().save_delta(delta())
    revised = store().reclassify(
        "delta_1", "assertion_1", owner=OWNER, classification="incidental",
        actor="alice", reason="a rename the request implied")

    assert revised.id != "delta_1"
    assert [d.id for d in store().list_deltas(owner=OWNER)] == [revised.id]
    assert store().find_delta(revised.fingerprint(), owner=OWNER).id == revised.id
    assert store().get_delta("delta_1", owner=OWNER) is not None

    trail = store().reclassifications(revised.id, owner=OWNER)
    assert len(trail) == 1
    row = trail[0]
    assert row["assertion_id"] == "assertion_1"
    assert row["from_classification"] == "unknown"
    assert row["to_classification"] == "incidental"
    assert row["actor"] == "alice"
    assert "rename" in row["reason"]
    assert row["at"], "a reclassification without a time cannot be ordered"


def test_the_trail_follows_the_whole_chain_of_readings():
    """Two reclassifications are three deltas, and the history is about the
    chain rather than about its last link."""
    store().save_delta(delta())
    once = store().reclassify("delta_1", "assertion_1", owner=OWNER,
                              classification="incidental", actor="alice",
                              reason="first look")
    twice = store().reclassify(once.id, "assertion_1", owner=OWNER,
                               classification="regression", actor="bob",
                               reason="second look, it is a regression")

    trail = store().reclassifications(twice.id, owner=OWNER)
    assert len(trail) == 2, "the row written against the first revision is still ours"
    assert {r["to_classification"] for r in trail} == {"incidental", "regression"}
    assert {r["actor"] for r in trail} == {"alice", "bob"}
    assert store().reclassifications(once.id, owner=OWNER) == [
        r for r in trail if r["delta_id"] == once.id], (
        "an older id sees its own history and not what came after it")
    assert store().counts(owner=OWNER)["live_deltas"] == 1


def test_a_reclassification_that_would_contradict_the_observation_is_refused():
    """`preserved` on a row whose operation says it changed is the contract's
    refusal, and the store must not route around it."""
    store().save_delta(delta())

    with pytest.raises(P.DeltaStoreError) as caught:
        store().reclassify("delta_1", "assertion_1", owner=OWNER,
                           classification="preserved", actor="alice",
                           reason="looks fine to me")

    assert "classification" in str(caught.value)
    kept = store().get_delta("delta_1", owner=OWNER)
    assert kept.assertions[0].classification == "unknown"
    assert [d.id for d in store().list_deltas(owner=OWNER)] == ["delta_1"], (
        "the refused revision must not have superseded anything")


def test_a_reclassification_needs_a_name_a_reason_and_a_real_word():
    store().save_delta(delta())
    for kwargs in (
        {"classification": "probably_fine", "actor": "alice", "reason": "hm"},
        {"classification": "incidental", "actor": "", "reason": "hm"},
        {"classification": "incidental", "actor": "alice", "reason": "  "},
        {"classification": "unknown", "actor": "alice", "reason": "no change"},
    ):
        with pytest.raises(P.DeltaStoreError):
            store().reclassify("delta_1", "assertion_1", owner=OWNER, **kwargs)

    with pytest.raises(P.DeltaStoreError):
        store().reclassify("delta_1", "assertion_missing", owner=OWNER,
                           classification="incidental", actor="alice",
                           reason="typo in the id")


def test_reclassifying_a_delta_that_is_not_yours_answers_none():
    store().save_delta(delta())
    assert store().reclassify("delta_1", "assertion_1", owner=OTHER,
                              classification="incidental", actor="bob",
                              reason="not mine to read") is None


# -- one owner's comparison is not another's --------------------------------


def test_another_owners_rows_are_not_returned_by_id_and_are_not_listed():
    """A delta quotes `before` and `after` out of somebody's file. Reading one
    by guessing its id has to answer the same thing as reading one that does not
    exist, or the answer confirms it exists."""
    store().save_intent(intent())
    store().save_request(request())
    store().save_delta(delta())

    assert store().get_delta("delta_1", owner=OTHER) is None
    assert store().get_intent("intent_1", owner=OTHER) is None
    assert store().get_request("delta_request_1", owner=OTHER) is None
    assert store().find_delta(delta().fingerprint(), owner=OTHER) is None
    assert store().find_intent(intent().fingerprint(), owner=OTHER) is None
    assert store().list_deltas(owner=OTHER) == []
    assert store().counts(owner=OTHER)["deltas"] == 0

    assert store().get_delta("delta_1", owner=OWNER) is not None
    assert store().reclassifications("delta_1", owner=OTHER) == []


def test_the_cache_is_per_owner_and_any_owner_is_the_only_way_across():
    """Two owners asking the identical question hold two live deltas.

    The unique index is on `(owner, fingerprint)` and not on `fingerprint`
    alone: one owner's comparison must never supersede another's, however
    identical the two look from the outside.
    """
    store().save_delta(delta(id="mine", owner=OWNER))
    store().save_delta(delta(id="theirs", owner=OTHER))

    assert [d.id for d in store().list_deltas(owner=OWNER)] == ["mine"]
    assert [d.id for d in store().list_deltas(owner=OTHER)] == ["theirs"]
    assert {d.id for d in store().list_deltas(owner=P.ANY_OWNER)} == {"mine",
                                                                     "theirs"}


# -- reads never raise, writes do -------------------------------------------


def test_a_read_on_a_broken_database_answers_empty_and_a_write_raises(tmp_path):
    """The asymmetry the whole package rests on, proved on one broken file.

    A read that raised here would let the record of a comparison break the run
    it was describing; a write that stayed silent would lose the record and
    let the caller believe it was kept.
    """
    store().save_delta(delta())

    # Another connection drops the table under the open store, which is what a
    # half-finished migration or a hand-edited file looks like from in here.
    broken = sqlite3.connect(str(tmp_path / "delta_engine.db"))
    broken.execute("DROP TABLE deltas")
    broken.commit()
    broken.close()

    assert store().get_delta("delta_1", owner=OWNER) is None
    assert store().list_deltas(owner=OWNER) == []
    assert store().counts(owner=OWNER)["deltas"] == -1, (
        "a count that could not be taken is -1, not 0: 'we could not look' and "
        "'there is nothing' are different answers")

    with pytest.raises(P.DeltaStoreError):
        store().save_delta(delta(id="delta_2"))


# -- the properties everything else rests on --------------------------------


def test_a_frozen_intent_is_never_rewritten():
    """Rule 3 of `contracts.py`, at the storage layer.

    Saving the same contract twice is a retry and must not be an error. Saving a
    DIFFERENT one under the same id is the edit-after-seeing-the-result that
    makes every evaluation score full marks, and it is refused.
    """
    store().save_intent(intent())
    assert store().save_intent(intent()) == "intent_1"

    with pytest.raises(P.DeltaStoreError) as caught:
        store().save_intent(intent(
            requested=[{"path": "src/auth.py", "condition": "preserved"}]))
    assert "intent.id" in str(caught.value)

    kept = store().get_intent("intent_1", owner=OWNER)
    assert kept.requested[0].condition == "changes"


def test_an_intent_is_found_by_what_it_asks_rather_than_by_its_id():
    """The §22 cache key needs the fingerprint, which is what was asked
    independent of when it was asked."""
    store().save_intent(intent())
    found = store().find_intent(intent().fingerprint(), owner=OWNER)
    assert found is not None and found.id == "intent_1"
    assert store().find_intent(digest("nothing asks this"), owner=OWNER) is None


def test_a_delta_comes_back_whole_after_the_store_is_reopened():
    """The payload is the truth, so it has to round-trip through JSON, through
    sqlite and through a cold open without losing a field."""
    original, job = delta(), request()
    store().save_delta(original)
    store().save_request(job, intent_id="intent_1")
    P.reset_store()

    again = store().get_delta("delta_1", owner=OWNER)
    assert again is not None
    assert again.to_dict() == original.to_dict()
    assert store().get_request("delta_request_1", owner=OWNER).to_dict() == \
        job.to_dict()


def test_the_projections_follow_the_payload_instead_of_accumulating(tmp_path):
    """`delta_assertions` is an index over the payload, not a second record.

    An assertion that a recomputed delta no longer makes has to disappear from
    the index too; an upsert would leave the old row behind and the index would
    answer with a finding the delta does not make.
    """
    store().save_delta(delta(assertions=[assertion(id="a1"),
                                         assertion(id="a2", path="src/other.py")]))
    # The same comparison, recomputed by a parser that no longer sees `a2`.
    store().save_delta(delta(assertions=[assertion(id="a1")]))

    with sqlite3.connect(str(tmp_path / "delta_engine.db")) as conn:
        rows = conn.execute(
            "SELECT delta_id, assertion_id FROM delta_assertions "
            "ORDER BY delta_id, assertion_id").fetchall()
    assert rows == [("delta_1", "a1")], (
        "a finding the recomputed delta no longer makes must not survive in "
        "the index that claims to summarise it")


def test_a_delta_whose_assertions_share_an_id_is_refused_by_name():
    """Two rows with one id project onto one row and the second erases the
    first -- the same failure `IntentContract.parse` already refuses for two
    invariants sharing an id."""
    with pytest.raises(P.DeltaStoreError) as caught:
        store().save_delta(delta(assertions=[assertion(id="a1"),
                                             assertion(id="a1", path="other")]))
    assert "a1" in str(caught.value)


def test_the_page_cursor_resumes_exactly_where_the_last_page_stopped():
    for index in range(5):
        store().save_delta(delta(id=f"delta_{index}",
                                 target=revision(f"after-{index}").to_dict()))

    first = store().list_deltas(owner=OWNER, limit=2)
    second = store().list_deltas(owner=OWNER, limit=2,
                                 cursor=P.DeltaStore.page_cursor(first[-1]))

    assert len(first) == 2 and len(second) == 2
    assert not ({d.id for d in first} & {d.id for d in second}), (
        "a page must not repeat what the previous one already handed over")


def test_an_unreadable_cursor_does_not_raise_on_a_read():
    store().save_delta(delta())
    assert [d.id for d in store().list_deltas(owner=OWNER, cursor="nonsense")] == \
        ["delta_1"]


def test_filters_narrow_a_listing_without_crossing_owners():
    store().save_delta(delta(id="code_one", domain="code"))
    store().save_delta(delta(id="doc_one", domain="document",
                             target=revision("doc").to_dict()))
    store().save_delta(delta(id="regressed", assessment="regressed",
                             target=revision("bad").to_dict()))

    assert [d.id for d in store().list_deltas(owner=OWNER, domain="document")] == \
        ["doc_one"]
    assert [d.id for d in store().list_deltas(owner=OWNER, assessment="regressed")] == \
        ["regressed"]
    assert store().list_deltas(owner=OTHER, domain="code") == []
