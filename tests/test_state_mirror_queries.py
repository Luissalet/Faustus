"""tests/test_state_mirror_queries.py -- what a read answers, and when it lies.

Every test here is about one claim: **a stored rating is not an answer**. The
mirror writes `fresh` onto a field when it folds an observation, and an hour
later that word is still in the database and is still wrong. `queries` re-rates
before it answers and `projection` builds on `queries`, so the same row read at
two times gives two different -- and both honest -- answers.

The rows this file covers:

* a state stored `fresh` reads `stale` an hour later, and the STORED value is
  untouched, because the difference between the two is the whole design;
* a projection over a stale field is not `sufficient()` and carries the action
  that would fix it, so a consumer that cannot act is told what to do instead;
* `token_budget` drops the least fresh first and names what it dropped -- a
  projection that quietly fitted itself into a budget would be one whose
  consumer thinks it saw everything;
* `unknown` survives the round trip: a field nobody has observed comes back as
  a field nobody has observed, never as `False` or a missing key;
* the situation queries are deterministic filters over the schemas, and they
  change their answer when the machine changes and not otherwise;
* no query defaults its owner to `ANY_OWNER`, and one owner's read never
  reaches another's row.
"""

from __future__ import annotations

import inspect

import pytest

from src.state_mirror import contracts as C
from src.state_mirror import ingest as ingest_mod
from src.state_mirror import persistence as P
from src.state_mirror import projection as projection_mod
from src.state_mirror import queries as Q
from src.state_mirror.projection import key_for

OWNER = "alice"
OTHER = "mallory"

#: One hour apart. `run_state.v1.status` is guaranteed for 15 seconds and
#: `label` for a day, so at T1 the first is stale and the second is fresh --
#: which is exactly the pair `token_budget` has to choose between.
T0 = "2026-09-06T12:00:00Z"
T1 = "2026-09-06T13:00:00Z"

RUN = C.entity_id("run", OWNER, "dispatch:job1")
OTHER_RUN = C.entity_id("run", OTHER, "dispatch:job9")
SERVICE = C.entity_id("service", OWNER, "backend:comfy")


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    """Own database, and nothing shared with another test."""
    P.use_path(str(tmp_path / "state_mirror.db"))
    try:
        yield
    finally:
        P.use_path(None)


def _observe(entity_id: str, body: dict, *, source: str = "runs",
             schema: str = "run_state.v1", epistemic: str = "observed",
             at: str = T0, owner: str = OWNER) -> dict:
    return {"entity_id": entity_id, "owner": owner, "source": source,
            "schema": schema, "epistemic": epistemic, "observed_at": at,
            "state": dict(body)}


def _fold(*observations, now: str = T0):
    """Put observations into the mirror the only way anything ever does."""
    return ingest_mod.ingest(list(observations), publisher=_Silent(), now=now)


class _Silent:
    """A publisher that records nothing. These tests are about reads."""

    def publish(self, name, **payload):
        return None


# -- the whole point: a read re-rates ---------------------------------------

def test_a_state_stored_fresh_reads_stale_an_hour_later():
    """The stored word and the answered word are different values.

    `FieldState.freshness` is what the rating WAS when the observation was
    folded. A query that returned it would tell a caller at 13:00 that a probe
    from 12:00 is fresh, because it was, an hour ago. Both halves are asserted:
    the read decays, and the row underneath it does not, because a read that
    rewrote the store on every call would make two readers race each other.
    """
    _fold(_observe(RUN, {"status": "running"}), now=T0)

    at_the_time = Q.get(RUN, owner=OWNER, now=T0)
    assert at_the_time.fields["status"].freshness == "fresh"

    an_hour_later = Q.get(RUN, owner=OWNER, now=T1)
    assert an_hour_later.fields["status"].freshness == "stale", (
        "a 15-second guarantee read an hour later is not current state")

    stored = P.store().get_state(RUN)
    assert stored.fields["status"].freshness == "fresh", (
        "re-rating is derived on read; it must not write back")
    assert an_hour_later.revision == stored.revision, (
        "ageing is not a change to the state and must not move the revision")


def test_get_fields_answers_unknown_for_a_field_nobody_has_observed():
    """`unknown` is a real answer and is never `False`, `0` or a missing key.

    A caller handed a dict that simply lacked `dirty` would have to guess
    whether the working tree is clean or whether nobody has looked, and only
    one of those is safe to act on.
    """
    _fold(_observe(RUN, {"status": "running"}), now=T0)

    answered = Q.get_fields(RUN, ["status", "progress", "not_a_field"],
                            owner=OWNER, now=T0)
    assert set(answered) == {"status", "progress"}, (
        "a name the schema does not declare is not a field at all")
    assert answered["status"].epistemic == "observed"
    assert answered["progress"].epistemic == "unknown"
    assert answered["progress"].freshness == "unknown"
    assert answered["progress"].value is None
    assert answered["progress"].source == ""


def test_stale_lists_the_fields_below_a_level_and_how_to_fix_them():
    _fold(_observe(RUN, {"status": "running", "label": "build the thing"}),
          now=T0)

    at_the_time = Q.stale(owner=OWNER, minimum_freshness="decision_safe", now=T0)
    assert at_the_time == []

    later = Q.stale(owner=OWNER, minimum_freshness="decision_safe", now=T1)
    names = {row["field"] for row in later}
    assert names == {"status"}, (
        "label is guaranteed for a day; only the 15-second field aged out")
    row = later[0]
    assert row["freshness"] == "stale"
    assert row["refresh"]["how"] == "POST /api/state/refresh"
    assert row["refresh"]["entity_id"] == RUN
    assert row["refresh"]["ttl_seconds"] == 15
    assert "3600s ago" in row["why"] and "30s" in row["why"], (
        "a rating a user cannot interrogate is a rating they will over-trust")


# -- projections ------------------------------------------------------------

def test_a_projection_over_a_stale_field_is_not_sufficient_and_says_what_to_do():
    """The answer a consumer about to act gets, and the reason it must not.

    `sufficient()` is the one number a caller may branch on before an effect,
    and a refresh action is what makes the refusal actionable rather than a
    shrug.
    """
    _fold(_observe(RUN, {"status": "running"}), now=T0)
    key = key_for(RUN, "status")

    fresh_enough = projection_mod.project(
        owner=OWNER, entity_refs=[RUN], fields=["status"],
        minimum_freshness="action_safe", now=T0)
    assert fresh_enough.sufficient() is True
    assert fresh_enough.freshness == "fresh"
    assert fresh_enough.refresh_actions == ()

    too_old = projection_mod.project(
        owner=OWNER, entity_refs=[RUN], fields=["status"],
        minimum_freshness="action_safe", now=T1)
    assert too_old.sufficient() is False
    assert too_old.fields[key]["freshness"] == "stale"
    assert too_old.revision == f"state:{RUN}@1"
    actions = [a for a in too_old.refresh_actions if a.get("key") == key]
    assert actions, "a field that is not fresh enough must say how to fix it"
    assert actions[0]["reason"] == "below_minimum_freshness"
    assert actions[0]["ttl_seconds"] == 15


def test_token_budget_drops_the_least_fresh_first_and_names_what_it_dropped():
    """A budget must never leave a caller holding only what it may not act on.

    Asserted as a property over a range of budgets rather than against one
    magic number, because the number depends on how long a label happens to be
    and a test that pinned it would fail the day a field gained a key.
    """
    _fold(_observe(RUN, {"status": "running", "label": "build the thing"}),
          now=T0)
    stale_key = key_for(RUN, "status")
    fresh_key = key_for(RUN, "label")

    whole = projection_mod.project(owner=OWNER, entity_refs=[RUN],
                                   fields=["status", "label"],
                                   minimum_freshness="informational", now=T1)
    assert set(whole.fields) == {stale_key, fresh_key}
    assert whole.freshness == "mixed", (
        "half fresh and half stale is 'look at the fields', not an average")

    kept_by_budget = {}
    for budget in (10, 20, 40, 60, 80, 100, 140, 200, 400):
        trimmed = projection_mod.project(
            owner=OWNER, entity_refs=[RUN], fields=["status", "label"],
            minimum_freshness="informational", token_budget=budget, now=T1)
        kept_by_budget[budget] = set(trimmed.fields)
        for key in set(whole.fields) - set(trimmed.fields):
            named = [a for a in trimmed.refresh_actions
                     if a.get("key") == key and a.get("reason") == "token_budget"]
            assert named, f"{key} was dropped for budget and never named"

    for budget, kept in kept_by_budget.items():
        assert not (stale_key in kept and fresh_key not in kept), (
            f"at budget {budget} the stale field survived and the fresh one "
            "did not; the drop order is backwards")
    assert any(kept == {fresh_key} for kept in kept_by_budget.values()), (
        "no budget in the range trimmed to exactly the fresh field")


def test_a_projection_never_echoes_an_entity_the_caller_may_not_see():
    """Not-yours and not-there answer alike, in the echo as well as the body.

    A projection that listed the refs it was ASKED for would be a listing of
    somebody else's machine for anyone who guessed an id.
    """
    _fold(_observe(OTHER_RUN, {"status": "running"}, owner=OTHER), now=T0)

    built = projection_mod.project(owner=OWNER, entity_refs=[OTHER_RUN],
                                   fields=["status"],
                                   minimum_freshness="informational", now=T0)
    assert built.fields == {}
    assert built.entity_refs == ()
    assert built.revision == ""
    assert built.sufficient() is False, "nothing is not a proof of anything"


# -- the situation queries --------------------------------------------------

def test_running_work_is_a_deterministic_filter_that_follows_the_machine():
    scope = Q.Scope(owner=OWNER)
    _fold(_observe(RUN, {"status": "running", "label": "build"}), now=T0)

    running = Q.running_work(scope, now=T0)
    assert [row["entity_id"] for row in running] == [RUN]
    assert running[0]["waiting"] is False
    assert running[0]["freshness"] == "fresh"
    assert Q.running_work(scope, now=T0) == running, (
        "the same store at the same instant must give the same rows")

    # The run finishes. Nothing else changes, and the answer changes.
    _fold(_observe(RUN, {"status": "done"}, at=T1), now=T1)
    assert Q.running_work(scope, now=T1) == []


def test_available_capabilities_is_the_one_situation_that_filters_on_freshness():
    """"ComfyUI was available" is not a capability.

    The other five list a row whatever its age and say how old it is. This one
    drops it, because its rows are the ones a caller routes work to, and work
    sent to a service that was up an hour ago goes into a hole.
    """
    scope = Q.Scope(owner=OWNER)
    _fold(_observe(SERVICE, {"health": "available", "capabilities": ["txt2img"]},
                   source="services", schema="service_state.v1"), now=T0)

    now = Q.available_capabilities(scope, now=T0)
    assert [row["entity_id"] for row in now] == [SERVICE]
    assert now[0]["capabilities"] == ["txt2img"]

    assert Q.available_capabilities(scope, now=T1) == [], (
        "a health probe guaranteed for 30 seconds is not evidence an hour on")
    # ...and the entity has not gone anywhere; only the claim about it aged.
    assert Q.get(SERVICE, owner=OWNER, now=T1) is not None


def test_unverified_changes_finds_what_an_actor_claimed_and_nothing_checked():
    """Section 9.3: an agent writing "I finished" is `reported` until proved."""
    scope = Q.Scope(owner=OWNER)
    _fold(_observe(RUN, {"status": "running"}), now=T0)
    _fold(_observe(RUN, {"worker_states": {"w1": "done"}},
                   epistemic="reported", at=T0), now=T0)

    rows = Q.unverified_changes(scope, now=T0)
    assert [(r["field"], r["reason"]) for r in rows] == [
        ("worker_states", "reported")]
    assert rows[0]["refresh"]["entity_id"] == RUN


def test_resource_pressure_names_the_number_and_the_threshold_it_crossed():
    scope = Q.Scope(owner=OWNER)
    device = C.entity_id("device", OWNER, "box")
    gigabyte = 1024 * 1024 * 1024
    _fold(_observe(device,
                   {"ram_used_bytes": 31 * gigabyte,
                    "ram_total_bytes": 32 * gigabyte,
                    "disk_free_bytes": 40 * gigabyte},
                   source="hardware", schema="device_state.v1"), now=T0)

    rows = Q.resource_pressure(scope, now=T0)
    assert [row["resource"] for row in rows] == ["ram"], (
        "40GB free is not disk pressure and must not be reported as any")
    assert rows[0]["fraction"] == pytest.approx(0.9688), (
        "the fraction is rounded to four places, not truncated")
    assert rows[0]["threshold"] == Q.PRESSURE_THRESHOLDS["ram_fraction"]


def test_an_unknown_situation_answers_nothing_rather_than_raising():
    assert Q.situation("what_is_it_doing", Q.Scope(owner=OWNER)) == []
    assert set(Q.SITUATIONS) == set(Q._SITUATIONS), (
        "the tuple a route offers and the functions that exist must agree")


# -- ownership --------------------------------------------------------------

def test_no_query_defaults_its_owner_to_every_owner():
    """`ANY_OWNER` is `None` and it is what a maintenance sweep passes.

    A query that defaulted to it would answer every owner's rows to whoever
    forgot the argument, and the forgetting is the normal failure -- so `owner`
    is required on every one of them and the default that does exist, on
    `Scope`, is `""`, which is a REAL owner on a single-user install.
    """
    for fn in (Q.get, Q.get_fields, Q.list_entities, Q.list_states,
               Q.changed_since, Q.relations, Q.stale, Q.conflicts):
        owner = inspect.signature(fn).parameters["owner"]
        assert owner.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__
        assert owner.default is inspect.Parameter.empty, (
            f"{fn.__name__} has a default owner; every read must be told whose")

    assert P.ANY_OWNER is None
    assert Q.Scope().owner == "" and Q.Scope().owner is not P.ANY_OWNER


def test_one_owner_never_reads_another_through_the_exact_queries():
    _fold(_observe(OTHER_RUN, {"status": "running", "label": "secret work"},
                   owner=OTHER), now=T0)

    assert Q.get(OTHER_RUN, owner=OWNER, now=T0) is None
    assert Q.get_fields(OTHER_RUN, ["status"], owner=OWNER, now=T0) == {}
    assert Q.list_entities(owner=OWNER) == []
    assert Q.list_states(owner=OWNER, now=T0) == []
    assert Q.changed_since(0, owner=OWNER, now=T0) == ([], 0)
    assert Q.relations(OTHER_RUN, owner=OWNER) == []
    assert Q.stale(owner=OWNER, now=T1) == []
    assert Q.conflicts(owner=OWNER) == []
    assert Q.running_work(Q.Scope(owner=OWNER), now=T0) == []

    # ...and the row is really there for the owner it belongs to, so the
    # emptiness above is a refusal and not an empty database.
    assert Q.get(OTHER_RUN, owner=OTHER, now=T0) is not None
    assert len(Q.running_work(Q.Scope(owner=OTHER), now=T0)) == 1
