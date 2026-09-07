"""Fold one observation into one state, deterministically and idempotently.

This is the only place in State Mirror where a belief changes. Everything else
observes, stores, queries or projects; this decides what the mirror now thinks.
It is a pure function of (previous state, observation, clock) -- no I/O, no
module state, no randomness -- because section 8.2 requires that replaying the
same observations rebuilds the same state, and a reducer that read anything
outside its arguments could not promise that.

The six rules, each with the failure it prevents:

1.  **A partial observation never deletes a field.** A probe that only sampled
    `health` does not know whether `queue_pending` is still there. Only a
    complete snapshot of a schema declared complete may remove fields
    (section 8.3). This is the difference between "the queue emptied" and "we
    did not ask about the queue".

2.  **A weaker epistemology never overwrites a stronger fresh one.** A model
    saying "the service is up" cannot displace a probe that failed to reach it
    30 seconds ago. `contracts.epistemic_rank` is the order and
    `_should_replace` is the gate (section 9.1).

3.  **A disagreement between two claims of equal standing is recorded, not
    settled.** Two `observed` values from different sources produce a
    `StateConflict` with both claims and a `next_check`, and the field keeps
    the one that arrived with the higher standing or, failing that, the older
    incumbent. Picking silently by timestamp is the failure section 9.2 names.

4.  **A late observation does not rewind the present.** An event that was
    observed BEFORE what we already hold is stored -- it is part of the record
    -- and changes nothing. Without this, one slow webhook turns a finished run
    back into a running one.

5.  **Namespaces never mix.** An observation about `branch:b7` cannot touch the
    state of `real`. The guard is one line and it is the whole of section 1.6.

6.  **Confirming what we knew is not a change.** The revision only moves when a
    value actually moved, so `revision 42 -> 43` always means something is
    different and never means "we looked again". Delta Engine compares those
    numbers (section 1.5) and a revision that ticked on every poll would make
    every comparison a false positive.

The output is a `Reduction`: the new state, whether anything changed, the
per-field diff, and any conflicts opened. Returning the diff rather than
logging it is what lets `state.changed` carry a compact change set (section 18)
instead of the whole state.
"""

from __future__ import annotations

import logging
import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.state_mirror import freshness as _freshness
from src.state_mirror.contracts import (
    OBSERVED_EPISTEMICS,
    FieldState,
    MaterializedState,
    StateConflict,
    StateObservation,
    epistemic_rank,
    schema_fields,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Reduction",
    "FieldChange",
    "reduce_observation",
    "reduce_many",
    "rerate",
    "empty_state",
    "CONFLICTING_EPISTEMICS",
]

#: Epistemologies whose disagreement is worth a conflict row. Two `observed`
#: values that differ is a real problem: two systems both looked and saw
#: different things. Two `inferred` ones differing is just two guesses, and
#: recording every pair of those would bury the ones that matter.
CONFLICTING_EPISTEMICS: Tuple[str, ...] = OBSERVED_EPISTEMICS


@dataclass(frozen=True)
class FieldChange:
    """One field that moved, in the shape `state.changed` carries.

    `before` and `after` are the VALUES, not the `FieldState`s: a consumer
    reading a change event wants to know that `status` went `running ->
    completed`, and the metadata is already on the state it can fetch.
    """

    field: str
    before: Any = None
    after: Any = None
    reason: str = "observed"

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after,
                "reason": self.reason}


@dataclass(frozen=True)
class Reduction:
    """What one fold produced. Never raises; a refusal is a value.

    `applied=False` with a populated `refusal` is a normal outcome, not an
    error: a late observation, a namespace mismatch and an observation for
    another entity are all things that legitimately happen and all things the
    caller wants named rather than swallowed.
    """

    state: MaterializedState
    applied: bool = False
    changes: Tuple[FieldChange, ...] = ()
    conflicts: Tuple[StateConflict, ...] = ()
    refusal: str = ""

    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.state.entity_id,
            "revision": self.state.revision,
            "applied": self.applied,
            "changes": [c.to_dict() for c in self.changes],
            "conflicts": [c.id for c in self.conflicts],
            "refusal": self.refusal,
        }


def empty_state(observation: StateObservation) -> MaterializedState:
    """The state an entity has before anything has been observed about it.

    Revision 0 with no fields, which is distinguishable from revision 1 with
    every field unknown. The first is "we have never looked"; the second would
    be "we looked and learned nothing", and only the first is true here.
    """
    return MaterializedState(
        entity_id=observation.entity_id,
        owner=observation.owner,
        namespace=observation.namespace,
        project_id=observation.project_id,
        schema=observation.schema,
        revision=0,
        fields={},
        conflicts=(),
        updated_at="",
    )


def _values_equal(left: Any, right: Any) -> bool:
    """Whether two field values are the same fact.

    Lists and dicts compare by content, because an adapter that rebuilds its
    payload every poll produces a new object with identical contents every
    time, and treating that as a change would make the revision tick forever.
    A bool is never equal to the int that shares its truthiness: `True` and `1`
    are different answers to "is this loaded", and one of them is a count.
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return (set(left) == set(right)
                and all(_values_equal(left[k], right[k]) for k in left))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(_values_equal(a, b) for a, b in zip(left, right)))
    return left == right


def _should_replace(current: Optional[FieldState], incoming_rank: int,
                    *, now: Any, schema: str, field: str) -> Tuple[bool, str]:
    """Whether an incoming claim outranks what we hold. `(decision, reason)`.

    The ladder, in order, and each rung is a rule from section 9.1:

    1.  Nothing held -> take it. Anything beats nothing.
    2.  Stronger epistemology -> take it. An observation displaces a report.
    3.  Same epistemology -> take it. Two equally-standing sources: the newer
        one wins the FIELD, and if they disagree the caller opens a conflict so
        the disagreement is visible rather than resolved by arrival order.
    4.  Weaker epistemology and the incumbent is no longer fresh -> take it. A
        guess about now beats a measurement from an hour ago, but only once
        that measurement has stopped being current.
    5.  Weaker epistemology and the incumbent is still fresh -> refuse. This is
        rule 2 of the module docstring and the reason this function exists.
    """
    if current is None:
        return True, "first observation"
    held = epistemic_rank(current.epistemic)
    if incoming_rank < held:
        return True, "stronger epistemology"
    if incoming_rank == held:
        return True, "same epistemology, newer observation"
    rating = _freshness.rate_field(current, schema=schema, field=field, now=now)
    if rating in _freshness.acceptable("decision_safe"):
        return False, (f"a {current.epistemic} value observed at "
                       f"{current.observed_at or 'an unknown time'} is still "
                       f"{rating}; a weaker claim does not displace it")
    return True, f"the {current.epistemic} value it replaces is {rating}"


def _conflict_between(entity_id: str, name: str, owner: str, namespace: str,
                      current: FieldState, incoming: FieldState,
                      observation: StateObservation, *, now: Any) -> Optional[StateConflict]:
    """A conflict row when two equally-standing sources disagree, else `None`.

    Three conditions, all required, and each one removes a category of noise:
    the values must actually differ; both claims must be strong enough that a
    disagreement means something (`CONFLICTING_EPISTEMICS`); and they must come
    from DIFFERENT sources -- one source changing its mind is a change, not a
    conflict, and is by far the commonest case.
    """
    if _values_equal(current.value, incoming.value):
        return None
    if (current.epistemic not in CONFLICTING_EPISTEMICS
            or incoming.epistemic not in CONFLICTING_EPISTEMICS):
        return None
    if not current.source or current.source == incoming.source:
        return None
    identity = json.dumps([entity_id, owner, namespace, name,
        current.observation_id, current.source, current.observed_at,
        observation.id, incoming.source, incoming.observed_at],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return StateConflict.parse({
        "id": "cfl_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        "entity_id": entity_id,
        "field": name,
        "owner": owner,
        "namespace": namespace,
        "status": "reconciling",
        "claims": [
            {"source": current.source, "value": current.value,
             "epistemic": current.epistemic, "observed_at": current.observed_at,
             "observation_id": current.observation_id},
            {"source": incoming.source, "value": incoming.value,
             "epistemic": incoming.epistemic, "observed_at": incoming.observed_at,
             "observation_id": incoming.observation_id},
        ],
        # What would settle it. Named, not run: the reducer is pure, and
        # deciding to spend a probe is a policy call that belongs upstream.
        "next_check": f"{incoming.source} vs {current.source} on {name}",
        "detected_at": _stamp(now),
    })


def _stamp(now: Any) -> str:
    """`now` as an ISO string, whatever it arrived as."""
    if isinstance(now, str) and now.strip():
        return now.strip()
    if now is None:
        return now_iso()
    try:
        return _freshness._now(now).isoformat().replace("+00:00", "Z")
    except Exception:  # noqa: BLE001 - a clock that will not format is not fatal
        return now_iso()


def reduce_observation(state: Optional[MaterializedState],
                       observation: StateObservation,
                       *, now: Any = None) -> Reduction:
    """Fold one observation into one state. Pure, total, idempotent.

    Idempotent in the strict sense: reducing the same observation twice leaves
    the second reduction with `changes == ()` and the revision untouched. That
    property is what makes replay (section 26) and a retried webhook safe, and
    it falls out of rule 6 rather than being checked for separately.
    """
    current = state if state is not None else empty_state(observation)

    if current.entity_id and current.entity_id != observation.entity_id:
        return Reduction(state=current, applied=False,
                         refusal=f"observation is about {observation.entity_id}, "
                                 f"this state is about {current.entity_id}")

    if current.owner != observation.owner:
        return Reduction(state=current, applied=False, refusal="observation and state have different owners")

    # Rule 5. One line, and it is the whole of section 1.6: a branch's
    # observation may not touch the real world's state, and vice versa.
    if current.namespace != observation.namespace:
        return Reduction(state=current, applied=False,
                         refusal=f"observation is in namespace "
                                 f"{observation.namespace!r} and this state is in "
                                 f"{current.namespace!r}; they are separate worlds")

    if current.schema and observation.schema != current.schema:
        return Reduction(state=current, applied=False,
                         refusal=f"observation carries {observation.schema} and "
                                 f"this state is {current.schema}; a schema change "
                                 "is a new entity, not an update")

    declared = schema_fields(observation.schema)
    incoming_rank = epistemic_rank(observation.epistemic)
    stamp = _stamp(now)
    ttl_default = observation.valid_for_seconds

    fields: Dict[str, FieldState] = dict(current.fields)
    changes: List[FieldChange] = []
    conflicts: List[StateConflict] = []

    for name, value in observation.state.items():
        if name not in declared:
            # `StateObservation.parse` already refused this; the check is here
            # for a caller that built one by hand in a test.
            continue
        held = fields.get(name)
        ttl = ttl_default or _freshness.ttl_for(observation.schema, name)
        candidate = FieldState(
            value=value,
            epistemic=observation.epistemic,
            freshness=_freshness.rate(observation.observed_at,
                                      ttl_seconds=ttl, now=now),
            observed_at=observation.observed_at,
            observation_id=observation.id,
            source=observation.source,
            ttl_seconds=ttl,
            computed_at=stamp,
        )

        # Rule 4. A late observation is kept by the store and changes nothing
        # here: the present is not rewound by a slow messenger.
        if held is not None and _is_older(candidate, held):
            continue

        conflict = _conflict_between(current.entity_id, name, current.owner,
                                     current.namespace, held, candidate,
                                     observation, now=now) if held else None
        if conflict is not None:
            conflicts.append(conflict)

        take, reason = _should_replace(held, incoming_rank, now=now,
                                       schema=observation.schema, field=name)
        if not take:
            logger.debug("state mirror: %s.%s kept its value (%s)",
                         current.entity_id, name, reason)
            continue

        if held is not None and _values_equal(held.value, candidate.value):
            # Rule 6. Same value, newer look: refresh the metadata so freshness
            # improves, and do NOT count it as a change.
            fields[name] = candidate
            continue

        fields[name] = candidate
        changes.append(FieldChange(field=name,
                                   before=held.value if held else None,
                                   after=candidate.value,
                                   reason=reason))

    # Rule 1's other half. Only a complete snapshot of a snapshot schema may
    # remove a field, and then only fields the schema declares -- so a bug in
    # an adapter can drop a value, never a whole shape.
    if observation.complete():
        for name in sorted(set(fields) - set(observation.state)):
            held = fields[name]
            # Absence is a claim too: a late snapshot or weaker guess must
            # not erase a newer, measured fact that normal replacement keeps.
            if _is_older(FieldState(observed_at=observation.observed_at), held):
                continue
            take, _ = _should_replace(held, incoming_rank, now=now,
                                      schema=observation.schema, field=name)
            if not take:
                continue
            fields.pop(name)
            changes.append(FieldChange(field=name, before=held.value, after=None,
                                       reason="absent from a complete snapshot"))

    if not changes:
        # Nothing material moved. The state still gets the refreshed field
        # metadata (freshness improved) but keeps its revision, because a
        # revision that ticked on every poll would make every Delta comparison
        # a false positive.
        settled = MaterializedState(
            entity_id=current.entity_id, owner=current.owner,
            namespace=current.namespace, project_id=current.project_id or
            observation.project_id, schema=observation.schema,
            revision=current.revision, fields=fields,
            conflicts=_merged_conflicts(current, conflicts),
            updated_at=stamp if fields != dict(current.fields) else current.updated_at,
        )
        return Reduction(state=settled, applied=True, changes=(),
                         conflicts=tuple(conflicts))

    reduced = MaterializedState(
        entity_id=current.entity_id,
        owner=current.owner,
        namespace=current.namespace,
        project_id=current.project_id or observation.project_id,
        schema=observation.schema,
        revision=current.revision + 1,
        fields=fields,
        conflicts=_merged_conflicts(current, conflicts),
        updated_at=stamp,
    )
    return Reduction(state=reduced, applied=True, changes=tuple(changes),
                     conflicts=tuple(conflicts))


def _merged_conflicts(current: MaterializedState,
                      opened: Sequence[StateConflict]) -> Tuple[str, ...]:
    """The state's conflict ids, plus any this fold opened, deduplicated."""
    out = list(current.conflicts)
    for conflict in opened:
        if conflict.id not in out:
            out.append(conflict.id)
    return tuple(out)


def _is_older(candidate: FieldState, held: FieldState) -> bool:
    """Whether the candidate was observed strictly before what we hold.

    Both times unreadable -> not older: with no way to order them, refusing the
    new one would freeze the field at whatever arrived first, which is worse
    than taking the latest. A missing incoming time with a known held one IS
    older, because an undated claim should not displace a dated one.
    """
    new_age = _freshness._parse(candidate.observed_at)
    old_age = _freshness._parse(held.observed_at)
    if old_age is None:
        return False
    if new_age is None:
        return True
    return new_age < old_age


def reduce_many(state: Optional[MaterializedState],
                observations: Sequence[StateObservation],
                *, now: Any = None) -> Reduction:
    """Fold a batch in the order given, and report the NET change.

    The net is what a consumer wants: a run that went `queued -> running ->
    completed` in one batch produces one change from `queued` to `completed`,
    not three. The intermediate values are still in the observation log for
    anyone reconstructing the path.
    """
    current = state
    first_values: Dict[str, Any] = dict(state.values()) if state else {}
    conflicts: List[StateConflict] = []
    applied = False
    refusals: List[str] = []

    for observation in observations:
        step = reduce_observation(current, observation, now=now)
        conflicts.extend(step.conflicts)
        if step.refusal:
            refusals.append(step.refusal)
            continue
        applied = applied or step.applied
        current = step.state

    if current is None:
        return Reduction(state=empty_state(observations[0]) if observations
                         else MaterializedState(), applied=False,
                         refusal="; ".join(refusals))

    net: List[FieldChange] = []
    for name, held in current.fields.items():
        before = first_values.get(name, None)
        if name not in first_values or not _values_equal(before, held.value):
            net.append(FieldChange(field=name, before=before, after=held.value,
                                   reason="net of a batch"))
    for name, before in first_values.items():
        if name not in current.fields:
            net.append(FieldChange(field=name, before=before, after=None,
                                   reason="removed by a complete snapshot"))

    return Reduction(state=current, applied=applied, changes=tuple(net),
                     conflicts=tuple(conflicts), refusal="; ".join(refusals))


def rerate(state: MaterializedState, *, now: Any = None) -> MaterializedState:
    """Re-derive every field's freshness against the clock now.

    Called on read and after a restart (section 16). This is the function that
    makes "the server was up yesterday" stop reading as "the server is up": no
    observation arrives, nothing is deleted, and the ratings simply decay to
    what they have actually become.

    The revision does NOT move. Ageing is not a change to the state; it is a
    change to how much the state is worth, and a Delta comparison that fired
    because time passed would be reporting the clock.
    """
    rerated = {
        name: FieldState(
            value=held.value,
            epistemic=held.epistemic,
            freshness=_freshness.rate_field(held, schema=state.schema,
                                            field=name, now=now),
            observed_at=held.observed_at,
            observation_id=held.observation_id,
            source=held.source,
            ttl_seconds=held.ttl_seconds,
            computed_at=_stamp(now),
        )
        for name, held in state.fields.items()
    }
    return MaterializedState(
        entity_id=state.entity_id, owner=state.owner, namespace=state.namespace,
        project_id=state.project_id, schema=state.schema,
        revision=state.revision, fields=rerated, conflicts=state.conflicts,
        updated_at=state.updated_at,
    )
