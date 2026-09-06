"""The completion store: what it refuses to lose, and what it refuses to blend.

Every test here fixes a RULE, not a snapshot. The columns will grow and the stop
vocabulary may too; none of that may quietly let a shadow measurement be counted
as work that happened, let one owner read another's account, or let a promise be
rewritten once the result is visible.

The guarantees, one test each:

* a file that is not a database is moved aside and the store opens clean --
  written as a real corrupt file, because a quarantine nobody triggers is dead
  code that every test of it passes by accident, and because on Windows the
  quarantine only works if `_connect` closed its handle before the exception
  left;
* a SHADOW decision never appears in a query for the real ones, nor the reverse,
  and `rejection_stats` and `spend_stats` never add the two together;
* another owner's row answers `None` by id and is invisible to every list;
* a read on a broken database answers empty and a write on one raises;
* the denormalised projections match the payload that was saved, and follow it
  when it is re-saved instead of accumulating beside it;
* what was promised before the run is never rewritten afterwards.

Plus the properties the rest rests on: a decision survives a reopen whole, the
page cursor resumes where it stopped, an unreadable cursor does not raise, and
two candidates sharing an id are refused by name.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.completion_engine import persistence as P
from src.completion_engine.contracts import (
    CompletionContract,
    CompletionDecision,
    ImprovementCandidate,
    ScopeEnvelope,
)

OWNER = "alice"
OTHER = "bob"
FROZEN = "2026-09-06T12:00:00Z"


@pytest.fixture(autouse=True)
def completion_db(tmp_path):
    """Every test gets its own database file."""
    P.use_path(str(tmp_path / "completion_engine.db"))
    try:
        yield
    finally:
        P.use_path(None)


def store() -> P.CompletionStore:
    return P.store()


def scope(**over) -> ScopeEnvelope:
    payload = {"id": "scope_1", "owner": OWNER, "project_id": "faustus",
               "goal": "fix the login redirect", "created_at": FROZEN,
               "allowed_resources": ["src/auth.py"],
               "protected_resources": ["src/settings.py"]}
    payload.update(over)
    return ScopeEnvelope.parse(payload)


def contract(**over) -> CompletionContract:
    payload = {"id": "completion_1", "owner": OWNER, "project_id": "faustus",
               "mode": "greedy", "run_id": "run_1", "session_id": "session_1",
               "scope_envelope_id": "scope_1", "created_at": FROZEN,
               "core_deliverables": ["the redirect lands on the asked page"],
               "definition_of_done": ["a test covers the redirect"],
               "verification": ["pytest tests/test_auth.py"]}
    payload.update(over)
    return CompletionContract.parse(payload)


def candidate(**over) -> dict:
    """One improvement, on `bonus` so it needs no evidence to be well formed."""
    payload = {"id": "improvement_1", "title": "add a regression test",
               "layer": "bonus", "category": "coverage", "relation": "direct",
               "source": "tests", "expected_value": 0.9, "estimated_cost": 0.1,
               "risk": 0.05, "confidence": 0.9, "status": "done",
               "resources": ["src/auth.py"],
               "verification": ["pytest tests/test_auth.py"],
               "created_at": FROZEN}
    payload.update(over)
    return payload


#: A budget with room in every pot, so `CompletionDecision.parse` accepts the
#: honest stops. Rule 2 of `contracts.py` refuses `converged` beside an
#: exhausted line, and a fixture that tripped it would be testing the contract
#: rather than the store.
BUDGET = {
    "total": {"rounds": 100, "tool_calls": 100, "tokens": 100_000,
              "seconds": 1000.0},
    "reserve_share": 0.15,
    "bonus_share": 0.35,
    "spent": {"core": {"rounds": 3, "tool_calls": 7, "tokens": 1200,
                       "seconds": 12.5},
              "bonus": {"rounds": 2, "tool_calls": 4, "tokens": 800,
                        "seconds": 6.25}},
}


def decision(**over) -> CompletionDecision:
    payload = {"id": "decision_1", "contract_id": "completion_1",
               "scope_envelope_id": "scope_1", "owner": OWNER,
               "project_id": "faustus", "run_id": "run_1",
               "session_id": "session_1", "mode": "greedy",
               "stop_reason": "converged",
               "completed_layers": ["core", "professional", "bonus"],
               "executed": [candidate()],
               "rejected": [candidate(id="improvement_2", title="rename the flag",
                                      status="rejected", rejection_reason="budget")],
               "budget": BUDGET, "shadow": False, "created_at": FROZEN}
    payload.update(over)
    return CompletionDecision.parse(payload)


def shadow_decision(**over) -> CompletionDecision:
    """The same run, measured instead of done."""
    payload = {"id": "decision_shadow", "shadow": True,
               "rejected": [candidate(id="improvement_9", title="split the module",
                                      status="rejected", rejection_reason="risk"),
                            candidate(id="improvement_8", title="inline the guard",
                                      status="rejected", rejection_reason="risk")],
               "budget": {"total": BUDGET["total"], "reserve_share": 0.15,
                          "bonus_share": 0.35,
                          "spent": {"core": {"rounds": 40, "tool_calls": 40,
                                             "tokens": 40_000, "seconds": 400.0}}}}
    payload.update(over)
    return decision(**payload)


# -- a damaged file costs the file, not the process -------------------------


def test_a_file_that_is_not_a_database_is_quarantined_and_the_store_opens_clean(
        tmp_path):
    """sqlite opens a non-database happily and only complains on the first read.

    So the corruption is real here -- bytes on disk, not a mocked error -- which
    is the only version of this test that would fail if the probe in `_connect`
    ever moved inside the `contextlib.suppress` above it.

    It is also the test that holds the `conn.close()` in `_connect`'s `except`.
    On Windows the quarantine's `os.replace` cannot move a file this process
    still has open: without that close the rename fails with WinError 32, the
    corrupt bytes stay exactly where they were, the second `_connect` fails the
    same way the first did, and `save_decision` raises instead of returning an
    id. Both assertions below are that failure, from opposite sides.
    """
    path = tmp_path / "completion_engine.db"
    path.write_bytes(b"POST /this-is-not-a-database HTTP/1.1\n" * 200)

    stored = store().save_decision(decision())

    assert stored == "decision_1"
    assert (tmp_path / "completion_engine.db.corrupt").exists(), (
        "the unreadable file must be kept beside the new one, not deleted")
    assert store().get_decision("decision_1", owner=OWNER) is not None, (
        "the store has to be usable afterwards, which it is not if the corrupt "
        "bytes were never moved out of the way")


# -- the rule this subsystem exists to hold ---------------------------------


def test_a_shadow_decision_never_appears_in_a_query_for_the_real_ones():
    """Shadow mode measures what the engine WOULD have done. Counting those
    measurements beside what actually happened ruins the measurement in both
    directions, and the ruin is silent because both halves are well formed."""
    store().save_decision(decision())
    store().save_decision(shadow_decision())

    assert [d.id for d in store().list_decisions(owner=OWNER)] == ["decision_1"], (
        "the default is what really happened; a caller that says nothing must "
        "never be handed a prediction")
    assert [d.id for d in store().list_decisions(owner=OWNER, shadow=True)] == \
        ["decision_shadow"]
    assert {d.id for d in store().list_decisions(owner=OWNER, shadow=None)} == \
        {"decision_1", "decision_shadow"}, (
        "None is the diagnostic answer and is the only way to see both")


def test_one_run_holds_both_kinds_and_decision_for_run_asks_which():
    """The same `run_id` carries the real decision and the measurement taken
    beside it, so `decision_for_run` is not a question until the caller says
    which of the two it means."""
    store().save_decision(decision())
    store().save_decision(shadow_decision())

    real = store().decision_for_run("run_1", owner=OWNER)
    measured = store().decision_for_run("run_1", owner=OWNER, shadow=True)

    assert real is not None and real.id == "decision_1" and real.shadow is False
    assert measured is not None and measured.id == "decision_shadow"
    assert measured.shadow is True


def test_a_decision_is_readable_by_id_whichever_kind_it_is():
    """One row is never a statistic: the caller already named what it wants and
    `shadow` on the object it gets back says which kind arrived."""
    store().save_decision(shadow_decision())
    found = store().get_decision("decision_shadow", owner=OWNER)
    assert found is not None and found.shadow is True


def test_the_statistics_never_add_a_measurement_to_something_that_happened():
    """A refusal a shadow run predicted is not a refusal anybody suffered, and a
    spend it recorded is money nobody spent. Adding either to the real totals
    produces a number that describes no run that ever existed."""
    store().save_decision(decision())
    store().save_decision(shadow_decision())

    assert store().rejection_stats(owner=OWNER) == {"budget": 1}
    assert store().rejection_stats(owner=OWNER, shadow=True) == {"risk": 2}
    assert store().rejection_stats(owner=OWNER, shadow=None) == \
        {"budget": 1, "risk": 2}

    real = store().spend_stats(owner=OWNER)
    measured = store().spend_stats(owner=OWNER, shadow=True)
    assert real["core"]["rounds"] == 3.0 and real["bonus"]["tokens"] == 800.0
    assert measured["core"]["rounds"] == 40.0
    assert "bonus" not in measured, (
        "only the lines that were actually spent on appear")
    assert store().spend_stats(owner=OWNER, shadow=None)["core"]["rounds"] == \
        43.0, "None adds them, which is why it is documented as diagnostic only"


def test_counts_reports_the_two_kinds_apart_rather_than_as_one_total():
    """A single `decisions` number is exactly the blend this store exists to
    prevent, so the split is what a caller reads and the row count sits beside
    it for the question that really is about file size."""
    store().save_decision(decision())
    store().save_decision(shadow_decision())

    counts = store().counts(owner=OWNER)
    assert counts["real_decisions"] == 1
    assert counts["shadow_decisions"] == 1
    assert counts["decisions"] == 2


# -- one owner's account is not another's -----------------------------------


def test_another_owners_rows_are_not_returned_by_id_and_are_not_listed():
    """A decision quotes a goal somebody wrote and names the paths it touched.
    Reading one by guessing its id has to answer the same thing as reading one
    that does not exist, or the answer confirms it exists."""
    store().save_scope(scope())
    store().save_contract(contract())
    store().save_decision(decision())

    assert store().get_scope("scope_1", owner=OTHER) is None
    assert store().get_contract("completion_1", owner=OTHER) is None
    assert store().get_decision("decision_1", owner=OTHER) is None
    assert store().contract_for_run("run_1", owner=OTHER) is None
    assert store().decision_for_run("run_1", owner=OTHER) is None
    assert store().list_decisions(owner=OTHER) == []
    assert store().list_decisions(owner=OTHER, shadow=None) == []
    assert store().rejection_stats(owner=OTHER) == {}
    assert store().spend_stats(owner=OTHER) == {}
    assert store().counts(owner=OTHER)["decisions"] == 0

    assert store().get_decision("decision_1", owner=OWNER) is not None


def test_any_owner_is_the_only_way_across_and_nothing_defaults_to_it():
    store().save_decision(decision(id="mine", owner=OWNER))
    store().save_decision(decision(id="theirs", owner=OTHER, run_id="run_2"))

    assert [d.id for d in store().list_decisions(owner=OWNER)] == ["mine"]
    assert {d.id for d in store().list_decisions(owner=P.ANY_OWNER)} == \
        {"mine", "theirs"}
    with pytest.raises(TypeError):
        store().list_decisions()  # type: ignore[call-arg]


# -- reads never raise, writes do -------------------------------------------


def test_a_read_on_a_broken_database_answers_empty_and_a_write_raises(tmp_path):
    """The asymmetry the whole package rests on, proved on one broken file.

    A read that raised here would let the account of a run break the run it was
    accounting for; a write that stayed silent would lose the account and let
    the caller believe it was kept.
    """
    store().save_decision(decision())

    # Another connection drops the table under the open store, which is what a
    # half-finished migration or a hand-edited file looks like from in here.
    broken = sqlite3.connect(str(tmp_path / "completion_engine.db"))
    broken.execute("DROP TABLE completion_decisions")
    broken.commit()
    broken.close()

    assert store().get_decision("decision_1", owner=OWNER) is None
    assert store().list_decisions(owner=OWNER) == []
    assert store().decision_for_run("run_1", owner=OWNER) is None
    assert store().rejection_stats(owner=OWNER) == {}
    assert store().spend_stats(owner=OWNER) == {}
    assert store().counts(owner=OWNER)["decisions"] == -1, (
        "a count that could not be taken is -1, not 0: 'we could not look' and "
        "'there is nothing' are different answers")

    with pytest.raises(P.CompletionStoreError):
        store().save_decision(decision(id="decision_2"))


# -- the projections are an index over the payload, never a second record ---


def test_the_projections_match_the_payload_that_was_saved(tmp_path):
    """The payload is the truth and these rows are a convenience, so the day
    they disagree the projection is the bug. This test is what notices."""
    store().save_decision(decision())
    saved = store().get_decision("decision_1", owner=OWNER)
    assert saved is not None

    with sqlite3.connect(str(tmp_path / "completion_engine.db")) as conn:
        rows = conn.execute(
            "SELECT candidate_id, layer, category, relation, source, status, "
            "rejection_reason, expected_value FROM completion_candidates "
            "WHERE decision_id = 'decision_1' ORDER BY candidate_id").fetchall()
        spends = conn.execute(
            "SELECT line, rounds, tool_calls, tokens, seconds FROM "
            "completion_spends WHERE decision_id = 'decision_1' "
            "ORDER BY line").fetchall()

    projected = {row[0]: row for row in rows}
    everything = saved.executed + saved.rejected + saved.deferred
    assert set(projected) == {c.id for c in everything}, (
        "every candidate in the payload has exactly one row, and no row has a "
        "candidate the payload does not carry")
    for item in everything:
        row = projected[item.id]
        assert row[1:] == (item.layer, item.category, item.relation, item.source,
                           item.status, item.rejection_reason,
                           item.expected_value)

    assert spends == [
        (line, spend.rounds, spend.tool_calls, spend.tokens, spend.seconds)
        for line, spend in sorted(saved.budget.spent.items())]


def test_the_projections_follow_the_payload_instead_of_accumulating(tmp_path):
    """A candidate a re-saved decision no longer carries has to disappear from
    the index too; an upsert would leave the old row behind and
    `rejection_stats` would go on counting a refusal the account no longer
    makes."""
    store().save_decision(decision())
    assert store().rejection_stats(owner=OWNER) == {"budget": 1}

    # The same decision, rewritten by a closeout that no longer refuses anything.
    store().save_decision(decision(rejected=[]))

    with sqlite3.connect(str(tmp_path / "completion_engine.db")) as conn:
        rows = conn.execute(
            "SELECT candidate_id FROM completion_candidates "
            "ORDER BY candidate_id").fetchall()
    assert rows == [("improvement_1",)]
    assert store().rejection_stats(owner=OWNER) == {}


def test_a_decision_whose_candidates_share_an_id_is_refused_by_name():
    """Two candidates with one id project onto one row and the second erases the
    first -- and because the executed and the rejected share this table, the row
    that vanishes can be the rejection."""
    with pytest.raises(P.CompletionStoreError) as caught:
        store().save_decision(decision(
            rejected=[candidate(id="improvement_1", title="the same id",
                                status="rejected", rejection_reason="risk")]))
    assert "improvement_1" in str(caught.value)


# -- what was promised is not rewritten once the result is visible ----------


def test_a_contract_is_never_rewritten_and_a_retry_is_not_an_error():
    """Rule 5 of `contracts.py`, at the storage layer. A `definition_of_done`
    edited once the work is visible is a checklist that always passes, and the
    whole point of writing it first was that it could fail."""
    store().save_contract(contract())
    assert store().save_contract(contract()) == "completion_1", (
        "a retried write is not an error")

    with pytest.raises(P.CompletionStoreError) as caught:
        store().save_contract(contract(
            definition_of_done=["whatever we ended up doing"]))
    assert "completion_1" in str(caught.value)

    kept = store().get_contract("completion_1", owner=OWNER)
    assert kept is not None
    assert kept.definition_of_done == ("a test covers the redirect",)


def test_the_newest_contract_is_the_one_the_run_was_judged_under():
    store().save_contract(contract())
    store().save_contract(contract(id="completion_2", created_at="2026-09-06T13:00:00Z",
                                   mode="literal"))

    found = store().contract_for_run("run_1", owner=OWNER)
    assert found is not None and found.id == "completion_2"
    assert store().get_contract("completion_1", owner=OWNER) is not None, (
        "'what did we originally say we would do' still has to be answerable")


def test_a_narrowed_envelope_replaces_the_wider_one_under_its_own_id():
    """`ScopeEnvelope.narrow()` returns the SAME id by design, because §1.12.1
    narrows in place and never widens. A store that refused the second write
    would make the only legal mutation of an envelope unstorable."""
    store().save_scope(scope())
    narrowed = scope().narrow(resources=["src/auth.py"], reason="only this file")
    assert narrowed.id == "scope_1"
    store().save_scope(narrowed)

    kept = store().get_scope("scope_1", owner=OWNER)
    assert kept is not None
    assert kept.allowed_resources == ("src/auth.py",)
    assert "narrowed: only this file" in kept.ambiguities
    assert store().counts(owner=OWNER)["scopes"] == 1, "it replaced, not added"


# -- the properties everything else rests on --------------------------------


def test_a_decision_comes_back_whole_after_the_store_is_reopened():
    """The payload is the truth, so it has to round-trip through JSON, through
    sqlite and through a cold open without losing a field."""
    original, promise, envelope = decision(), contract(), scope()
    store().save_scope(envelope)
    store().save_contract(promise)
    store().save_decision(original)
    P.reset_store()

    again = store().get_decision("decision_1", owner=OWNER)
    assert again is not None
    assert again.to_dict() == original.to_dict()
    assert store().get_contract("completion_1", owner=OWNER).to_dict() == \
        promise.to_dict()
    assert store().get_scope("scope_1", owner=OWNER).to_dict() == \
        envelope.to_dict()


def test_the_page_cursor_resumes_exactly_where_the_last_page_stopped():
    for index in range(5):
        store().save_decision(decision(
            id=f"decision_{index}", run_id=f"run_{index}",
            created_at=f"2026-09-06T1{index}:00:00Z"))

    first = store().list_decisions(owner=OWNER, limit=2)
    second = store().list_decisions(
        owner=OWNER, limit=2, cursor=P.CompletionStore.page_cursor(first[-1]))

    assert len(first) == 2 and len(second) == 2
    assert not ({d.id for d in first} & {d.id for d in second}), (
        "a page must not repeat what the previous one already handed over")


def test_a_page_of_real_decisions_stays_real_all_the_way_through_the_cursor():
    """The separation has to survive paging, or a long enough listing quietly
    becomes a mixture halfway down."""
    for index in range(3):
        store().save_decision(decision(id=f"real_{index}", run_id=f"run_{index}",
                                       created_at=f"2026-09-06T1{index}:00:00Z"))
        store().save_decision(shadow_decision(
            id=f"shadow_{index}", run_id=f"run_{index}",
            created_at=f"2026-09-06T1{index}:30:00Z"))

    seen = []
    cursor = ""
    while True:
        page = store().list_decisions(owner=OWNER, limit=2, cursor=cursor)
        if not page:
            break
        seen.extend(page)
        cursor = P.CompletionStore.page_cursor(page[-1])

    assert [d.id for d in seen] == ["real_2", "real_1", "real_0"]
    assert all(d.shadow is False for d in seen)


def test_an_unreadable_cursor_does_not_raise_on_a_read():
    store().save_decision(decision())
    assert [d.id for d in store().list_decisions(owner=OWNER, cursor="nonsense")] \
        == ["decision_1"]


def test_filters_narrow_a_listing_without_crossing_owners_or_kinds():
    store().save_decision(decision(id="greedy_one"))
    store().save_decision(decision(id="literal_one", mode="literal",
                                   run_id="run_2", stop_reason="core_only",
                                   created_at="2026-09-06T13:00:00Z"))
    store().save_decision(shadow_decision(id="greedy_shadow"))

    assert [d.id for d in store().list_decisions(owner=OWNER, mode="literal")] == \
        ["literal_one"]
    assert [d.id for d in store().list_decisions(owner=OWNER,
                                                 stop_reason="core_only")] == \
        ["literal_one"]
    assert [d.id for d in store().list_decisions(owner=OWNER,
                                                 project_id="faustus")] == \
        ["literal_one", "greedy_one"]
    assert store().list_decisions(owner=OWNER, mode="greedy", shadow=True) == \
        [d for d in store().list_decisions(owner=OWNER, shadow=True)]
    assert store().list_decisions(owner=OTHER, mode="greedy") == []
