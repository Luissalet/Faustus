"""The one road a fact takes into the mirror: section 8.1's pipeline, in order.

    receive -> resolve owner/scope -> validate -> normalise -> dedupe
            -> append -> reduce -> publish

One public function, `ingest`, and it NEVER raises. Everything upstream of it
is an adapter reading somebody else's registry, and everything downstream is a
consumer that has to keep working when one source is broken; a pipeline that
threw would let a renamed column in one registry take down the sweep that was
about to tell the user their disk is full.

So a refusal is a VALUE. `IngestReport` carries counts and reasons, the caller
logs or shows them, and the batch continues -- an observation this build cannot
read is one observation lost, never the other forty.

The four decisions that shape this file, each with what it prevents:

**A duplicate is free.** `store.append_observation` answers `(stored, id)` and
`stored=False` means the identity index already had this exact look. That is a
NORMAL outcome -- a replayed event, a retried webhook, a subscription that
reconnected and re-sent its backlog -- and the right response is to do nothing
at all: no reduce, no state write, no event. Reducing it again would be
harmless (the reducer is idempotent) and would still cost a database write and
a stream frame per replayed event, which is the difference between a replay
that is free and one that is merely survivable. "Publish always" in the plan
means regardless of whether anything CHANGED, not regardless of dedupe.

**The state is written even when nothing changed.** `reduce_observation`
refreshes every field's timestamps and freshness on a confirming observation
without touching the revision, and that refreshed metadata is the whole
product of a poll that found nothing new. It is stored with
`changed=False`, so the change cursor does not move and a consumer following
changes is not woken to be told nothing happened.

**An entity row is created if the observation names one we have never seen.**
`persistence.list_states` joins `state_entities`, so a materialised state with
no entity row exists and answers no query -- it is invisible to every listing,
every projection and every screen. The row is minted from the observation
itself (its id already carries kind, owner and namespace) and
`state_entity_discovered` says so. A RETIRED entity that gets a new
observation is revived rather than duplicated: a service that came back is the
same service, and leaving it retired would hide a machine that recovered.

**Source health is recorded once per source, not once per observation.** A
sweep can bring two hundred observations from one adapter and
`store.note_source` is a write each time. The counts are accumulated here and
written at the end, which also means a source whose every observation was
refused is recorded as `degraded` rather than as silently absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src.state_mirror import persistence as _persistence
from src.state_mirror import reducers as _reducers
from src.state_mirror.contracts import (
    StateEntity,
    StateError,
    StateObservation,
    parse_entity_id,
)

logger = logging.getLogger(__name__)

__all__ = ["IngestReport", "ingest"]


@dataclass(frozen=True)
class IngestReport:
    """What one batch did. Every field is an answer, none of them is an error.

    `refused` and `errors` are separate because a caller acts on them
    differently: a refusal is this build declining to read something (a schema
    it does not know, a namespace that does not match, an observation older
    than what we hold), and an error is the machinery underneath failing (the
    database would not take the write). The first is a bug in an adapter or a
    fact about the data; the second is a bug here or a disk that is full.
    """

    accepted: int = 0
    duplicates: int = 0
    refused: Tuple[str, ...] = ()
    changed_entities: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()

    def changed(self) -> bool:
        return bool(self.changed_entities)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "refused": list(self.refused),
            "changed_entities": list(self.changed_entities),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
        }


class _Batch:
    """The accumulator `ingest` fills in. Mutable so the pipeline reads as one
    pass rather than as six tuples being rebuilt per observation."""

    def __init__(self) -> None:
        self.accepted = 0
        self.duplicates = 0
        self.refused: List[str] = []
        self.changed: List[str] = []
        self.conflicts: List[str] = []
        self.errors: List[str] = []
        #: source -> [stored, duplicate, refused]. Duplicates are tracked apart
        #: from both: a source whose whole batch was a replay is working
        #: perfectly, and counting its observations again in the store's
        #: cumulative total would inflate what it has actually contributed.
        self.sources: Dict[str, List[int]] = {}

    def note(self, source: str, *, stored: int = 0, duplicate: int = 0,
             failed: int = 0) -> None:
        row = self.sources.setdefault(str(source or ""), [0, 0, 0])
        row[0] += stored
        row[1] += duplicate
        row[2] += failed

    def report(self) -> IngestReport:
        return IngestReport(
            accepted=self.accepted,
            duplicates=self.duplicates,
            refused=tuple(self.refused),
            # Deduplicated, order preserved: one entity that changed twice in a
            # batch is one entity that changed.
            changed_entities=tuple(dict.fromkeys(self.changed)),
            conflicts=tuple(dict.fromkeys(self.conflicts)),
            errors=tuple(self.errors),
        )


def _publish(publisher: Any, owner: str, name: str, **payload: Any) -> None:
    """Announce one event, and never let the announcement break the ingest.

    `publisher=None` resolves this owner's stream, which is the normal path: a
    sweep does not know which owners its observations will turn out to be
    about until it has read them. A publisher passed in is used verbatim -- it
    is how a test watches without a registry and how a caller batches several
    owners into one recorder.
    """
    try:
        stream = publisher
        if stream is None:
            from src.state_mirror import events as _events

            stream = _events.stream_for(owner)
        stream.publish(name, **payload)
    except Exception as exc:  # noqa: BLE001 - the stream is not the fact
        logger.debug("state ingest: publishing %s failed: %s", name, exc)


def _normalise(raw: Any) -> Tuple[Optional[StateObservation], str]:
    """One observation in the contract's shape, or `(None, why not)`.

    Mappings are parsed and objects are taken as they are, which is what lets a
    route accept JSON and a sweep pass the contract objects its adapters
    already built. The two checks after the parse are for hand-built objects
    only -- `StateObservation.parse` makes both impossible -- and they are here
    because a test double and a future caller can construct the dataclass
    directly, and an observation with no source cannot be revalidated while one
    with no fields is not a look at all.
    """
    if raw is None:
        return None, "an observation cannot be None"
    try:
        observation = (raw if isinstance(raw, StateObservation)
                       else StateObservation.parse(raw))
    except StateError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - a caller's shape is not our crash
        return None, f"{type(exc).__name__}: {exc}"
    if not str(observation.source or "").strip():
        return None, (f"{observation.entity_id}: an observation names the "
                      "adapter that made it; state with no source cannot be "
                      "revalidated")
    if not observation.state:
        return None, (f"{observation.entity_id}: the observation carries no "
                      "fields; a look that saw nothing is not a look")
    try:
        parts = parse_entity_id(observation.entity_id)
    except StateError as exc:
        return None, str(exc)
    if observation.owner != parts["owner"]:
        return None, (f"{observation.entity_id}: the observation is filed under "
                      f"owner {observation.owner!r} and its id says "
                      f"{parts['owner']!r}")
    return observation, ""


def _ensure_entity(store: Any, observation: StateObservation,
                   batch: _Batch, publisher: Any) -> None:
    """Make sure the entity row exists, and say so the first time it does.

    Without this a state row can exist that `list_states` -- which joins
    `state_entities` -- will never return, so the mirror would hold a fact it
    could not list. The row is minted from the observation because the id
    already carries kind, owner and namespace; nothing is invented.

    A retired entity that is observed again is REVIVED. A service that came
    back is the same service, and a second entity for it would split its
    history at the moment it recovered.
    """
    existing = store.get_entity(observation.entity_id)
    if existing is not None and not existing.retired():
        return
    reviving = existing is not None
    try:
        parts = parse_entity_id(observation.entity_id)
        row = StateEntity.parse({
            "id": observation.entity_id,
            "kind": parts["kind"],
            "owner": observation.owner,
            "namespace": observation.namespace,
            "project_id": observation.project_id,
            "display_name": existing.display_name if existing else "",
            "labels": list(existing.labels) if existing else [],
            "source_refs": list(existing.source_refs) if existing else [],
            "schema": observation.schema,
            "created_at": existing.created_at if existing else "",
            "sensitivity": observation.sensitivity,
        })
        store.upsert_entity(row)
    except Exception as exc:  # noqa: BLE001 - the state is still worth having
        batch.errors.append(f"{observation.entity_id}: the entity row could not "
                            f"be recorded ({type(exc).__name__}: {exc})")
        logger.warning("state ingest: could not record entity %s: %s",
                       observation.entity_id, exc)
        return
    _publish(publisher, observation.owner, "state_entity_discovered",
             entity_id=observation.entity_id, namespace=observation.namespace,
             project_id=observation.project_id, source=observation.source,
             kind=parts["kind"], schema=observation.schema,
             revived=bool(reviving))


def _record_conflicts(store: Any, reduction: Any, observation: StateObservation,
                      batch: _Batch, publisher: Any) -> Dict[str, str]:
    """Open the conflicts this fold found, announcing only the NEW ones.

    `store.open_conflict` answers `(opened, id)` and `opened=False` means one
    is already live for this (entity, field): the same two sources disagreeing
    again is the same argument continuing, not a new one. Publishing on every
    repeat would put a hundred frames on the stream for one disagreement, and a
    page showing a hundred rows for one argument is a page nobody reads.
    """
    identities = {}
    for conflict in reduction.conflicts:
        try:
            opened, conflict_id = store.open_conflict(conflict)
        except Exception as exc:  # noqa: BLE001 - a conflict is not the state
            batch.errors.append(f"{conflict.entity_id}.{conflict.field}: the "
                                f"conflict could not be opened "
                                f"({type(exc).__name__}: {exc})")
            continue
        batch.conflicts.append(conflict_id)
        identities[conflict.id] = conflict_id
        if not opened:
            continue
        _publish(publisher, observation.owner, "state_conflict_detected",
                 entity_id=conflict.entity_id, namespace=conflict.namespace,
                 project_id=observation.project_id, source=observation.source,
                 conflict_id=conflict_id, field=conflict.field,
                 claims=[dict(c) for c in conflict.claims],
                 next_check=conflict.next_check)
    return identities


def ingest(observations: Iterable[Any], *, store: Any = None,
           publisher: Any = None, now: Any = None) -> IngestReport:
    """Fold a batch of observations into the mirror. Never raises.

    `store=None` looks the shared store up per call rather than holding one,
    because `persistence.use_path` drops the memoised instance and a function
    that captured the old one would keep writing to a database a test just
    moved away from. `publisher=None` resolves each observation's owner stream.
    `now=None` is the wall clock; passing one is how a test ages a field by an
    hour without sleeping.
    """
    batch = _Batch()
    db = store if store is not None else _persistence.store()

    for raw in list(observations or ()):
        observation, refusal = _normalise(raw)
        if observation is None:
            batch.refused.append(refusal)
            # The source is only knowable when the observation parsed, so a
            # refusal at this stage is counted against the source when we can
            # read one off the raw input and against nothing when we cannot.
            batch.note(_source_of(raw), failed=1)
            continue
        try:
            _one(db, observation, batch, publisher, now)
        except Exception as exc:  # noqa: BLE001 - one observation, not the batch
            logger.exception("state ingest: %s from %s could not be folded",
                             observation.entity_id, observation.source)
            batch.errors.append(f"{observation.entity_id}: "
                                f"{type(exc).__name__}: {exc}")
            batch.note(observation.source, failed=1)

    _note_sources(db, batch)
    return batch.report()


def _source_of(raw: Any) -> str:
    """The adapter name off an observation that would not parse, or `""`."""
    if isinstance(raw, Mapping):
        return str(raw.get("source") or "")
    return str(getattr(raw, "source", "") or "")


def _note_sources(store: Any, batch: _Batch) -> None:
    """Write one health row per source. `note_source` never raises by design.

    A source whose every observation was refused is `degraded` and not absent:
    an adapter that answers nothing and an adapter that answers rubbish look
    the same from a listing, and only one of them is worth waking somebody for.
    """
    for source, (stored, duplicate, failed) in sorted(batch.sources.items()):
        if not source:
            continue
        answered = stored or duplicate
        health = "ok" if answered else ("degraded" if failed else "unknown")
        detail = "" if not failed else f"{failed} observation(s) refused"
        store.note_source(source, health=health, detail=detail,
                          observations=stored, failed=bool(failed and not answered))


#: How much of one changed value travels in a `state_changed` frame. A diff is
#: only compact if its values are: `project_state.v1.changed_paths` on a big
#: working tree is thousands of strings, and putting that on the stream twice
#: (before and after) for one boolean flip elsewhere in the same fold is how a
#: "compact diff" becomes the whole state by another name. What is dropped is
#: named in place, so a consumer knows it is looking at a summary.
MAX_DIFF_ITEMS = 20
MAX_DIFF_CHARS = 500


def _compact(value: Any) -> Any:
    """One field value, bounded, with any truncation stated rather than silent."""
    if isinstance(value, str) and len(value) > MAX_DIFF_CHARS:
        return {"truncated_chars": len(value),
                "sample": value[:MAX_DIFF_CHARS]}
    if isinstance(value, (list, tuple)) and len(value) > MAX_DIFF_ITEMS:
        return {"truncated_items": len(value),
                "sample": [_compact(v) for v in list(value)[:MAX_DIFF_ITEMS]]}
    if isinstance(value, Mapping) and len(value) > MAX_DIFF_ITEMS:
        keys = sorted(str(k) for k in value)[:MAX_DIFF_ITEMS]
        return {"truncated_keys": len(value),
                "sample": {k: _compact(value[k]) for k in keys}}
    return value


def _diff(reduction: Any) -> List[Dict[str, Any]]:
    """The change set `state_changed` carries: what moved, and to what."""
    out: List[Dict[str, Any]] = []
    for change in reduction.changes:
        row = change.to_dict()
        row["before"] = _compact(row.get("before"))
        row["after"] = _compact(row.get("after"))
        out.append(row)
    return out


def _one(store: Any, observation: StateObservation, batch: _Batch,
         publisher: Any, now: Any) -> None:
    """Steps 5 to 8 of the pipeline for one observation: dedupe, append,
    reduce, publish.

    The append comes BEFORE the reduce and that order is the contract: the log
    is the record, so an observation whose fold is refused -- a late one, one
    for the wrong namespace -- is still stored. Section 8.2 rebuilds the state
    by replaying that log, and a log missing the observations the reducer
    declined would replay into a different state than the one it produced.
    """
    stored, _observation_id = store.append_observation(observation)
    if not stored:
        # A replay, a retried webhook, a reconnecting subscription. Nothing to
        # reduce, nothing to write, nothing to announce: this is what makes a
        # replayed event free rather than merely harmless.
        batch.duplicates += 1
        batch.note(observation.source, duplicate=1)
        return

    batch.accepted += 1
    batch.note(observation.source, stored=1)
    _ensure_entity(store, observation, batch, publisher)

    previous = store.get_state(observation.entity_id)
    reduction = _reducers.reduce_observation(previous, observation, now=now)
    if reduction.refusal:
        batch.refused.append(f"{observation.entity_id}: {reduction.refusal}")
        return

    identities = _record_conflicts(store, reduction, observation, batch, publisher)
    reduction = replace(reduction, state=replace(reduction.state, conflicts=tuple(dict.fromkeys(
        identities.get(cid, cid) for cid in reduction.state.conflicts))))
    store.put_state(reduction.state, changed=reduction.changed())
    try:
        from src.state_mirror.conflicts import resolve_confirmed
        for conflict in resolve_confirmed(store, reduction.state, now=now):
            _publish(publisher, observation.owner, "state_conflict_resolved",
                     entity_id=observation.entity_id, namespace=observation.namespace,
                     project_id=observation.project_id, conflict_id=conflict.id,
                     field=conflict.field, status="resolved")
    except Exception as exc:
        batch.errors.append(f"{observation.entity_id}: conflict reconciliation failed ({type(exc).__name__})")

    # Always -- a poll that confirmed what we already knew still moved every
    # field's freshness forward, and a consumer watching a source's liveness
    # has nothing else to watch. The payload names the FIELDS and not their
    # values: what a field is now is a read away, and what it just became is
    # the next event's job.
    _publish(publisher, observation.owner, "state_observation_received",
             entity_id=observation.entity_id, namespace=observation.namespace,
             project_id=observation.project_id, source=observation.source,
             observation_id=observation.id, schema=observation.schema,
             epistemic=observation.epistemic, observed_at=observation.observed_at,
             partial=observation.partial, fields=sorted(observation.state))

    if not reduction.changed():
        return
    batch.changed.append(observation.entity_id)
    _publish(publisher, observation.owner, "state_changed",
             entity_id=observation.entity_id, namespace=observation.namespace,
             project_id=observation.project_id, source=observation.source,
             observation_id=observation.id, schema=reduction.state.schema,
             revision=reduction.state.revision,
             revision_ref=reduction.state.revision_ref(),
             changes=_diff(reduction))
