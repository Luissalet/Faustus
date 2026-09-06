"""tests/test_delta_engine_state.py -- §15's four sentences, kept apart.

Every test fixes a RULE and not a snapshot. The wording of a limitation will
change and the mirror will grow schemas; none of that may turn a datum that
merely aged into a change, a stronger observation into a claim about the world,
or an open conflict into `preserved`.

The guarantees, one test each:

* a value that changed with the SAME source and a newer observation is a change
  in the WORLD, and nothing about that row is hedged;
* an inference replaced by an observation is a change in what we KNOW, says so
  in its own `detail`, and is bounded below `exact` -- because whether the world
  moved as well is not something two read models can say;
* a value that only grew stale is `unchanged`. Ageing is a change in what the
  state is worth, not in what it says, and reporting it as a delta would bury
  every real one;
* a source that contradicts itself about one instant is a `correction`, at a
  confidence dropped to `low`: a witness that disagrees with itself about a
  moment is a weak witness about that field from then on;
* an open conflict produces `unknown` and never `preserved` -- the mirror
  recorded the disagreement instead of resolving it (§9.2), and this adapter is
  not the one to settle it;
* a field that appears is `added` and one that goes is `missing`;
* `registry.status()` lists this domain with nobody adding it to a list;
* the invariant id this module points at is one `invariants.py` declares.
"""

from __future__ import annotations

import copy

import pytest

from src.delta_engine import invariants as invariants_mod
from src.delta_engine import registry, sources
from src.delta_engine.adapters import state as state_mod
from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import (
    Budget,
    IntentContract,
    RevisionRef,
    confidence_rank,
)

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"
ENTITY = "service://alice/real/comfyui"
FIELD_KEY = f"field:{ENTITY}#health"
ENTITY_KEY = f"entity:{ENTITY}"

PROVENANCE = {"id": state_mod.PROVENANCE_INVARIANT, "class": "provenance"}


@pytest.fixture()
def scope() -> Scope:
    return Scope(owner=OWNER, budget=Budget())


@pytest.fixture()
def adapter() -> state_mod.StateAdapter:
    return state_mod.StateAdapter()


def state(*, value="ok", epistemic="observed", source="probe",
          observed_at="2026-09-06T10:00:00Z", freshness="fresh", revision=7,
          conflicts=(), extra=None):
    """One `MaterializedState.to_dict()` for `service://alice/real/comfyui`.

    Built through the mapping the mirror itself writes, so the adapter reads it
    through `MaterializedState.parse` exactly as it would read a revision that
    came out of the store -- a test that hand-built elements would be testing
    the test.
    """
    fields = {
        "health": {"value": value, "epistemic": epistemic, "freshness": freshness,
                   "observed_at": observed_at, "source": source,
                   "ttl_seconds": 60, "observation_id": "obs_1"},
    }
    fields.update(extra or {})
    return {
        "entity_id": ENTITY, "owner": OWNER, "namespace": "real",
        "schema": "service_state.v1", "revision": revision,
        "conflicts": list(conflicts), "fields": fields,
        "updated_at": observed_at,
    }


def compare(adapter, scope, source_body, target_body):
    """`(extraction, source snapshot, target snapshot)` for two revisions."""
    source = adapter.snapshot(sources.stash(source_body), scope=scope)
    target = adapter.snapshot(sources.stash(target_body), scope=scope)
    return adapter.compare(source, target, scope=scope), source, target


def findings_by_path(extraction) -> dict:
    return {finding.path: finding for finding in extraction.findings}


def results_by_id(results) -> dict:
    return {result.invariant_id: result for result in results}


def intent_with(*invariants) -> IntentContract:
    return IntentContract.parse({
        "id": "intent_state", "owner": OWNER, "domain": "state",
        "frozen_at": FROZEN, "invariants": list(invariants),
    })


# -- §15: a change in the world --------------------------------------------


def test_the_same_source_observing_a_different_value_later_is_a_world_change(
        adapter, scope):
    """One witness, two observations, one of them newer. Nothing to hedge.

    The confidence is asserted as `exact` on purpose. This is the branch where
    the adapter is entitled to be certain: the same probe reported two different
    values at two ordered instants, and no interpretation was needed to say the
    second is not the first.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", observed_at="2026-09-06T10:00:00Z"),
        state(value="down", observed_at="2026-09-06T11:00:00Z", revision=8))

    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "modified"
    assert finding.confidence == "exact"
    assert finding.detail.startswith(state_mod.WORLD_CHANGE)
    assert state_mod.KNOWLEDGE_CHANGE not in finding.detail
    assert finding.limitations == ()


def test_a_revision_that_moved_is_reported_on_the_entity_too(adapter, scope):
    """`state_mirror/events.py`: "the revision is what Delta Engine compares".

    The counter only advances when a reduction CHANGED something, so the entity
    row is the cheap summary of whether anything material happened at all -- and
    it must move when a field does.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", observed_at="2026-09-06T10:00:00Z"),
        state(value="down", observed_at="2026-09-06T11:00:00Z", revision=8))
    entity = findings_by_path(extraction)[ENTITY_KEY]
    assert entity.operation == "modified"
    assert entity.before.startswith("revision 7")
    assert entity.after.startswith("revision 8")


# -- §15: a change in what we know -----------------------------------------


def test_an_inference_replaced_by_an_observation_is_a_change_of_knowledge(
        adapter, scope):
    """§15's second sentence, and the one an adapter is most tempted to skip.

    A model said `ok`; a probe says `down`. `epistemic_rank` fell, so what
    changed is what we KNOW -- and the row has to say that in its own `detail`
    rather than leave a reader to infer it from two metadata fields. The
    confidence is bounded below `exact` for the reason the detail gives: an
    inference overturned by an observation is not evidence that the world moved,
    and these two revisions cannot tell whether it did.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", epistemic="inferred", source="planner",
              observed_at="2026-09-06T09:00:00Z"),
        state(value="down", epistemic="observed", source="probe",
              observed_at="2026-09-06T11:00:00Z", revision=8))

    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "modified"
    assert finding.detail.startswith(state_mod.KNOWLEDGE_CHANGE)
    assert "what we KNOW" in finding.detail
    assert confidence_rank(finding.confidence) >= confidence_rank("medium")
    assert finding.confidence != "exact"


def test_a_stronger_source_wins_over_the_timestamps(adapter, scope):
    """The order of §15's questions, asserted rather than assumed.

    The epistemology is checked FIRST. The same probe pair with an older
    observation would be a correction; a stronger epistemology makes it a change
    of knowledge whatever the clock says, because an observation replacing an
    inference is not the inference's source contradicting itself.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", epistemic="inferred", source="probe",
              observed_at="2026-09-06T11:00:00Z"),
        state(value="down", epistemic="observed", source="probe",
              observed_at="2026-09-06T09:00:00Z", revision=8))
    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.detail.startswith(state_mod.KNOWLEDGE_CHANGE)
    assert state_mod.CORRECTION not in finding.detail


# -- §15: a datum that only aged -------------------------------------------


def test_a_value_that_only_grew_stale_is_not_a_change(adapter, scope):
    """§15's third sentence, and the one that decides whether anyone reads these.

    Same value, `fresh` -> `stale`. `unchanged`, every time. Ageing is a change
    in what the state is WORTH -- `state_mirror/queries.py` re-rates on every
    read for exactly that reason -- and classifying it as a change would put one
    row per field per hour in front of a reader looking for the one row where
    something moved.

    The decay is not swallowed either: it is a named limitation on the row, so a
    caller about to act still sees it. `unchanged` plus a stated limitation is
    the honest shape; a `modified` would be noise and a silent `unchanged` would
    be a half-truth.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", freshness="fresh"),
        state(value="ok", freshness="stale"))

    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "unchanged"
    assert finding.operation not in ("modified", "added", "missing")
    assert any(item.startswith(state_mod.DECAYED) for item in finding.limitations)
    assert "not a change" in finding.detail

    entity = findings_by_path(extraction)[ENTITY_KEY]
    assert entity.operation == "unchanged"

    assert all(finding.operation == "unchanged" for finding in extraction.findings)


def test_freshness_recovering_is_not_a_change_either(adapter, scope):
    """The mirror looked again and the value held. Still `unchanged`."""
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", freshness="stale"),
        state(value="ok", freshness="fresh"))
    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "unchanged"
    assert not any(item.startswith(state_mod.DECAYED)
                   for item in finding.limitations)


# -- §15: a correction -----------------------------------------------------


def test_a_source_that_contradicts_itself_about_one_instant_is_a_correction(
        adapter, scope):
    """§15's fourth sentence, with the confidence it costs.

    Same source, same `observed_at`, a different value. The world did not have
    two states at one moment, so one of the two readings was wrong -- and a
    witness that disagrees with itself about a moment is a weak witness about
    that field from then on. The confidence drops to `low` and the limitation
    says which of §15's sentences this is, so a consumer can filter on the label
    without a regex over English.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", observed_at="2026-09-06T10:00:00Z"),
        state(value="degraded", observed_at="2026-09-06T10:00:00Z", revision=8))

    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "modified"
    assert finding.detail.startswith(state_mod.CORRECTION)
    assert finding.confidence == "low"
    assert any(item.startswith(state_mod.CORRECTION)
               for item in finding.limitations)


def test_an_earlier_observation_that_disagrees_is_also_a_correction(
        adapter, scope):
    """"the same instant OR an earlier one" -- both halves of §15's wording."""
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", observed_at="2026-09-06T10:00:00Z"),
        state(value="degraded", observed_at="2026-09-06T09:00:00Z", revision=8))
    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.detail.startswith(state_mod.CORRECTION)
    assert finding.confidence == "low"


def test_two_different_sources_are_reported_as_a_disagreement(adapter, scope):
    """Neither a world change nor a correction: nobody here can tell which.

    Two witnesses, two values. Calling it a change in the world would credit one
    of them without a reason to, and calling it a correction would accuse a
    source of contradicting itself when it never did.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        state(value="ok", source="probe", observed_at="2026-09-06T10:00:00Z"),
        state(value="down", source="agent-report", epistemic="reported",
              observed_at="2026-09-06T11:00:00Z", revision=8))
    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "modified"
    assert finding.detail.startswith(state_mod.DISAGREEMENT)
    assert finding.confidence == "low"


# -- §15: an open conflict -------------------------------------------------


def test_an_open_conflict_produces_unknown_and_never_preserved(adapter, scope):
    """§9.2: the reducer records the disagreement instead of picking a winner.

    So this adapter must not pick one either. Every invariant over an entity
    carrying an open conflict comes back `unknown` with the conflict named, and
    the field rows carry the same sentence -- because a `preserved` here would
    certify a property the mirror itself is holding two claims about.

    The comparison is otherwise clean: same value, same source, same instant.
    Without the conflict this exact pair answers `preserved`, which is what the
    second half of the test pins down -- a rule that only ever answers `unknown`
    would pass this test and be useless.
    """
    intent = intent_with(PROVENANCE)

    extraction, source_snap, target_snap = compare(
        adapter, scope, state(), state(conflicts=["cfl_9f2c"]))

    result = results_by_id(adapter.check_invariants(
        intent, source_snap, target_snap, scope=scope))[
            state_mod.PROVENANCE_INVARIANT]
    assert result.status == "unknown"
    assert result.status != "preserved"
    assert result.confidence == "unknown"
    assert any(item.startswith(state_mod.OPEN_CONFLICT)
               for item in result.limitations)

    finding = findings_by_path(extraction)[FIELD_KEY]
    assert any(item.startswith(state_mod.OPEN_CONFLICT)
               for item in finding.limitations)
    assert finding.confidence != "exact"

    clean_extraction, clean_source, clean_target = compare(
        adapter, scope, state(), state())
    clean = results_by_id(adapter.check_invariants(
        intent, clean_source, clean_target, scope=scope))[
            state_mod.PROVENANCE_INVARIANT]
    assert clean.status == "preserved"
    assert clean.observations
    assert findings_by_path(clean_extraction)[FIELD_KEY].confidence == "exact"


def test_a_value_with_no_source_violates_provenance(adapter, scope):
    """`state.provenance_kept`, on the one thing `FieldState` cannot hold without.

    Rule 1 of `state_mirror/contracts.py` is that a value cannot be stored
    without a time, a source and an epistemology, so a field that arrives with a
    blank `source` is a real violation and not a reading error.
    """
    extraction, source_snap, target_snap = compare(
        adapter, scope, state(), state(source="", value="down", revision=8))
    result = results_by_id(adapter.check_invariants(
        intent_with(PROVENANCE), source_snap, target_snap, scope=scope))[
            state_mod.PROVENANCE_INVARIANT]
    assert result.status == "violated"
    assert result.observations
    assert extraction.findings


# -- entities and fields that come and go ----------------------------------


def test_a_field_only_in_the_target_is_added_and_one_only_in_the_source_is_missing(
        adapter, scope):
    """The two operations §15 asks for by name, on the mirror's own addresses."""
    with_latency = state(extra={"latency_ms": {
        "value": 42, "epistemic": "observed", "freshness": "fresh",
        "observed_at": "2026-09-06T10:00:00Z", "source": "probe",
        "ttl_seconds": 60}})
    extraction, _source, _target = compare(adapter, scope, state(), with_latency)
    assert findings_by_path(extraction)[f"field:{ENTITY}#latency_ms"].operation == "added"

    reverse, _source, _target = compare(adapter, scope, with_latency, state())
    assert findings_by_path(reverse)[f"field:{ENTITY}#latency_ms"].operation == "missing"


def test_an_entity_that_is_gone_is_missing_and_a_new_one_is_added(adapter, scope):
    """One revision may hold several entities; both directions are addressed."""
    other = copy.deepcopy(state())
    other["entity_id"] = "service://alice/real/ollama"
    both = {"states": [state(), other]}
    extraction, _source, _target = compare(adapter, scope, both, {"states": [state()]})
    assert findings_by_path(extraction)[
        "entity:service://alice/real/ollama"].operation == "missing"

    reverse, _source, _target = compare(adapter, scope, {"states": [state()]}, both)
    assert findings_by_path(reverse)[
        "entity:service://alice/real/ollama"].operation == "added"


# -- the shapes a revision arrives in --------------------------------------


def projection(*, value="ok", epistemic="observed", source="probe",
               observed_at="2026-09-06T10:00:00Z", freshness="fresh"):
    """A `StateProjection.to_dict()`, the shape §15 names first.

    Written out here with the keys `projection.project()` actually writes, so
    that a change to that assembly which dropped `epistemic` or `source` would
    fail this test rather than quietly turn every field into an undated one.
    """
    return {
        "projection_id": "stateproj_1", "id": "stateproj_1", "owner": OWNER,
        "project_id": "", "namespace": "real", "as_of": observed_at,
        "minimum_freshness": "informational", "entity_refs": [ENTITY],
        "fields": {f"{ENTITY}#health": {
            "entity_id": ENTITY, "schema": "service_state.v1", "field": "health",
            "value": value, "epistemic": epistemic, "freshness": freshness,
            "observed_at": observed_at, "source": source, "ttl_seconds": 60,
            "trusted": True, "revision": 7}},
        "source_observation_ids": ["obs_1"], "freshness": freshness,
        "unknown_fields": [], "conflicts": [], "refresh_actions": [],
        "revision": f"state:{ENTITY}@7",
    }


def test_a_projection_is_read_and_classified_the_same_way(adapter, scope):
    """§15's other shape, through `StateProjection.parse` and not a guess.

    The classification must not depend on which shape the revision arrived in: a
    world change read out of a projection is the same world change read out of a
    materialized state, or a consumer would get different answers about one pair
    of revisions depending on how it asked for them.
    """
    extraction, source_snap, _target = compare(
        adapter, scope,
        projection(value="ok", observed_at="2026-09-06T10:00:00Z"),
        projection(value="down", observed_at="2026-09-06T11:00:00Z"))
    assert source_snap.readable is True
    finding = findings_by_path(extraction)[FIELD_KEY]
    assert finding.operation == "modified"
    assert finding.detail.startswith(state_mod.WORLD_CHANGE)


def test_a_state_kind_revision_is_unreadable_with_the_resolvers_reason(
        adapter, scope):
    """The documented hole, asserted so it cannot close by accident.

    `sources.RESOLVERS` has no reader for a State Mirror row -- "a State Mirror
    revision is a row and not a byte stream" -- and `sources.py` is not this
    adapter's to change. The refusal travels as a `readable=False` snapshot
    carrying that reason, which is what makes the delta `inconclusive` rather
    than an exception, and what says WHICH end failed.
    """
    revision = RevisionRef.parse({
        "kind": "state", "ref": f"state:{ENTITY}#7", "hash": "c" * 64})
    snapshot = adapter.snapshot(revision, scope=scope)
    assert snapshot.readable is False
    assert snapshot.elements == ()
    assert any("not a byte stream" in note for note in snapshot.notes)


def test_a_mapping_that_is_no_known_shape_is_refused_rather_than_emptied(
        adapter, scope):
    """An empty snapshot would report every entity as retired. Refuse instead."""
    snapshot = adapter.snapshot(sources.stash({"hello": "world"}), scope=scope)
    assert snapshot.readable is False
    assert any("MaterializedState" in note for note in snapshot.notes)


def test_an_unreadable_end_produces_no_findings_and_says_which_one(adapter, scope):
    """"We could not read it" and "every entity was retired" are opposite facts."""
    source = adapter.snapshot(sources.stash(state()), scope=scope)
    target = adapter.snapshot(RevisionRef.parse({
        "kind": "state", "ref": f"state:{ENTITY}#8", "hash": "d" * 64}),
        scope=scope)
    extraction = adapter.compare(source, target, scope=scope)
    assert extraction.findings == ()
    assert extraction.coverage.both_readable is False
    assert any("target revision could not be read" in item
               for item in extraction.limitations)


def test_coverage_reports_temporal_because_this_adapter_reads_observation_times(
        adapter, scope):
    """§17: coverage is what could be compared, and time IS an axis here.

    §15's four sentences are decided on `observed_at`, so the share of fields
    whose observations can actually be ordered is a real measurement of how much
    of the comparison could be made -- not a token zero, and not omitted.
    """
    extraction, _source, _target = compare(
        adapter, scope, state(), state(value="down", revision=8,
                                       observed_at="2026-09-06T11:00:00Z"))
    assert extraction.coverage.ratio("structural") == 1.0
    assert extraction.coverage.ratio("temporal") == 1.0

    undated, _source, _target = compare(
        adapter, scope, state(observed_at=""), state(observed_at=""))
    assert undated.coverage.ratio("temporal") == 0.0


def test_an_invariant_this_adapter_cannot_answer_is_never_preserved(adapter, scope):
    """Rule 1 of `contracts.py`, on a class no method here observes."""
    result = results_by_id(adapter.check_invariants(
        intent_with({"id": state_mod.PROVENANCE_INVARIANT, "class": "provenance"},
                    {"id": "security.permissions_not_widened",
                     "class": "permissions", "source": "security"}),
        adapter.snapshot(sources.stash(state()), scope=scope),
        adapter.snapshot(sources.stash(state()), scope=scope),
        scope=scope))["security.permissions_not_widened"]
    assert result.status == "unknown"
    assert result.confidence == "unknown"
    assert result.limitations


# -- discovery and the invariant catalogue ---------------------------------


def test_the_registry_finds_this_adapter_with_no_list_to_update():
    """Writing `ADAPTER_FACTORY` IS the registration. `registry.py`'s docstring
    records what a tuple cost the State Mirror: five correct adapters that did
    not exist as far as the running system was concerned."""
    registry.reset()
    assert state_mod.ADAPTER_FACTORY in registry.discover()
    status = registry.status()
    assert status["state"]["available"] is True
    assert "StateAdapter" in status["state"]["module"]
    assert registry.adapter_for("state").domain == "state"


def test_the_invariant_id_this_adapter_points_at_is_one_the_catalogue_declares():
    """A row pointing at an id nobody declares is a row nobody can act on."""
    declared = {item["id"]
                for item in invariants_mod.DOMAIN_DEFAULTS.get("state", ())}
    declared |= {item["id"] for item in invariants_mod.SECURITY_INVARIANTS}
    assert state_mod.PROVENANCE_INVARIANT in declared
