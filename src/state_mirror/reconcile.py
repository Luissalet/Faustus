"""Section 8.4: the sweep that asks every source what is true and ages the rest.

Events tell the mirror what changed; this tells it what it MISSED. A dropped
webhook, a process that died before it could report, a file somebody deleted by
hand, a service that stopped without saying so -- none of those produce an
event, and a mirror fed only by events drifts in exactly the direction that
matters: it keeps believing things are still there.

The sweep does four things, in this order, and the order is load-bearing:

1.  **Ask.** `discover()`, then `observe()`, then `relations()` on every
    adapter. Discovery first because an observation about an entity nothing
    discovered still lands (ingest mints the row), but a retirement decision
    needs the discovery set.
2.  **Fold.** Everything observed goes through `ingest`, which dedupes,
    reduces and publishes. The sweep has no reducer of its own: two folds in
    one subsystem is two answers to "what do we believe".
3.  **Retire.** An entity a discovering adapter no longer reports is marked
    gone. This is the dangerous half of the file and it has one guard:
    **only an adapter that ran successfully may retire anything.** An adapter
    that raised, or that is not in this build, discovers nothing -- and an
    unguarded sweep would read "nothing" as "everything you knew is gone" and
    retire the world on one bad import. Ownership is decided by the source of
    the entity's fields: an adapter retires what it alone has been writing, and
    an entity two adapters both write to is nobody's to retire.
4.  **Age.** Every state is re-rated so that `state_became_stale` fires for
    fields that have just crossed out of usefulness. Nothing is deleted and no
    revision moves -- ageing is not a change to the state, it is a change to
    what the state is worth -- but a consumer watching the stream learns that
    the thing it was about to act on stopped being current, which is a fact no
    source will ever send an event about.

Retirement is a timestamp and never a delete. "This service used to exist" is
an answer a sweep has to be able to give: a row that vanished and a row that
was retired look identical to a consumer, and only one of them means the sweep
worked.

The sweep NEVER raises. Every adapter call, every store write and the whole
ingest are wrapped, and what went wrong comes back in `SweepReport.errors`. A
sweep is a background job over eleven sources; one of them being broken is the
normal state of the world and must cost that source alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.state_mirror import freshness as _freshness
from src.state_mirror import ingest as _ingest
from src.state_mirror import persistence as _persistence
from src.state_mirror import reducers as _reducers
from src.state_mirror.contracts import (
    REAL_NAMESPACE,
    MaterializedState,
    new_id,
)
from src.state_mirror.ingest import IngestReport

logger = logging.getLogger(__name__)

__all__ = ["SweepReport", "sweep"]

#: How many entities and states one sweep will walk when retiring and ageing.
#: High enough that a busy machine's whole mirror fits, bounded so that a store
#: nobody has compacted cannot turn a background job into a full table scan
#: that outlives its own interval.
SWEEP_CEILING = 2000

#: Health words this file writes, from `contracts.SOURCE_HEALTH`. `unknown` is
#: for an adapter that is not in this build -- which is not a fault and must
#: not read as one -- and `degraded` is for one that raised.
HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_UNKNOWN = "unknown"
_BAD_HEALTH: Tuple[str, ...] = ("degraded", "down")


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did, in the shape a diagnostics screen renders.

    `adapters_failed` and `adapters_skipped` are separate because they mean
    different things to a person: a skipped adapter is a source this build does
    not have (ComfyUI is not installed, there is no council database), and a
    failed one is a source that is here and broke. Only the second is worth
    waking somebody for, and a report that collapsed them would make every
    optional feature look like an outage.
    """

    owner: str = ""
    namespace: str = REAL_NAMESPACE
    sweep_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    adapters_run: Tuple[str, ...] = ()
    adapters_failed: Tuple[str, ...] = ()
    adapters_skipped: Tuple[str, ...] = ()
    entities_seen: int = 0
    observations_seen: int = 0
    relations_seen: int = 0
    retired: Tuple[str, ...] = ()
    became_stale: Tuple[str, ...] = ()
    degraded: Tuple[str, ...] = ()
    recovered: Tuple[str, ...] = ()
    ingest: IngestReport = dataclass_field(default_factory=IngestReport)
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner, "namespace": self.namespace,
            "sweep_id": self.sweep_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "duration_ms": self.duration_ms,
            "adapters_run": list(self.adapters_run),
            "adapters_failed": list(self.adapters_failed),
            "adapters_skipped": list(self.adapters_skipped),
            "entities_seen": self.entities_seen,
            "observations_seen": self.observations_seen,
            "relations_seen": self.relations_seen,
            "retired": list(self.retired),
            "became_stale": list(self.became_stale),
            "degraded": list(self.degraded),
            "recovered": list(self.recovered),
            "ingest": self.ingest.to_dict(),
            "errors": list(self.errors),
        }


class _Run:
    """One sweep in progress. Mutable so the phases below read as one pass."""

    def __init__(self, owner: str, namespace: str) -> None:
        self.owner = owner
        self.namespace = namespace
        self.scope: Any = None
        self.project_id = ''
        self.retirement_filters: Dict[str, Any] = {}
        self.sweep_id = new_id("sweep")
        self.started_at = now_iso()
        self.began = time.monotonic()
        self.ran: List[str] = []
        self.failed: List[str] = []
        self.skipped: List[str] = []
        #: adapter name -> the entity ids it discovered THIS sweep. Only an
        #: adapter present in this map may retire anything, which is the whole
        #: of guard 3 in the module docstring.
        self.discovered: Dict[str, set] = {}
        self.observations: List[Any] = []
        self.relations: List[Any] = []
        self.entities = 0
        self.retired: List[str] = []
        self.stale: List[str] = []
        self.degraded: List[str] = []
        self.recovered: List[str] = []
        self.errors: List[str] = []

    def fail(self, where: str, exc: BaseException) -> None:
        self.errors.append(f"{where}: {type(exc).__name__}: {exc}")


def _publish(publisher: Any, owner: str, name: str, **payload: Any) -> None:
    """Announce one event, and never let the announcement break the sweep."""
    try:
        stream = publisher
        if stream is None:
            from src.state_mirror import events as _events

            stream = _events.stream_for(owner)
        stream.publish(name, **payload)
    except Exception as exc:  # noqa: BLE001 - the stream is not the sweep
        logger.debug("state reconcile: publishing %s failed: %s", name, exc)


def _scope_for(owner: str, scope: Any) -> Any:
    """An `adapters.base.Scope` whose owner is the caller's, whatever arrived.

    The import is function-local because `state_mirror.adapters` builds eleven
    adapter modules on import, and a module that only wanted `SweepReport` must
    not pay for that.

    The owner is FORCED onto the scope rather than read from it. A scope that
    could name its own owner is a sweep anybody could run as anybody, which is
    the same mistake as an owner in a request body -- and this one would file
    another person's machine under the caller's name.
    """
    from src.state_mirror.adapters.base import Scope

    if scope is None:
        return _with_default_folder(Scope(owner=owner))
    if isinstance(scope, Mapping):
        allowed = ("project_id", "workspace", "namespace", "limit")
        body = {k: scope[k] for k in allowed if k in scope}
        return _with_default_folder(Scope(owner=owner, **body))
    if getattr(scope, "owner", None) != owner:
        logger.debug("state reconcile: the scope named owner %r and the sweep "
                     "is for %r; the caller's wins",
                     getattr(scope, "owner", None), owner)
    return _with_default_folder(Scope(
        owner=owner,
        project_id=str(getattr(scope, "project_id", "") or ""),
        workspace=str(getattr(scope, "workspace", "") or ""),
        namespace=str(getattr(scope, "namespace", "") or REAL_NAMESPACE),
        limit=getattr(scope, "limit", 100)))


def _with_default_folder(scope: Any) -> Any:
    """Give a scope that names no folder the owner's default project.

    Without this the sweep is silent about the one thing a coding session cares
    about most. `workspace.py` needs a folder to read a branch, a working tree
    and a checkpoint head; `objectives.py` needs a project to read its goals.
    Neither invents one, correctly -- an adapter that guessed a folder would be
    reporting about a repository nobody asked about. So the resolution belongs
    HERE, once, where a caller's identity is already known.

    Found the way the failure shows up: a sweep on the real machine ran all
    eleven adapters, reported no failures, and produced nothing at all from
    `workspace` and `objectives`. Both were working. Both had been handed a
    scope with no folder in it, which is a question they cannot answer rather
    than a question with the answer "nothing" -- and from outside those two
    look identical.

    Never raises and never guesses. An install with no projects, or one whose
    project store cannot be read, keeps the empty scope it arrived with, and
    the two adapters go on reporting nothing -- which is then the truth.
    """
    if getattr(scope, "workspace", "") or getattr(scope, "project_id", ""):
        return scope
    owner = str(getattr(scope, "owner", "") or "")
    try:
        from services.projects import get_store

        # `ProjectStore.list(owner)`, named directly rather than looked up with
        # a `getattr` fallback. The first version of this function guessed
        # `list_projects`, guarded the guess with `if callable(...)`, and so
        # did nothing at all on the real machine while reporting a clean sweep
        # -- the same class of silence it was written to fix, one layer up. A
        # method that moves should raise here and be caught by the line below,
        # not be quietly skipped by a guard.
        projects = list(get_store().list(owner) or ())
    except Exception as exc:  # noqa: BLE001 - a sweep never dies on a lookup
        logger.debug("state reconcile: no project store for %r (%s); the sweep "
                     "keeps its empty scope", owner, exc)
        return scope
    for project in projects:
        if not isinstance(project, Mapping):
            continue
        # `workspace` is the absolute path and `folder` is the DISPLAY NAME --
        # a project record carries `{"folder": "LocalAI", "workspace":
        # "D:\\LocalAI"}`. Reading them the other way round is a mistake with
        # no symptom: the adapter is handed "LocalAI", finds no such directory,
        # and honestly reports `workspace_available: false` about a repository
        # that is sitting right there.
        folder = str(project.get("workspace") or "")
        ident = str(project.get("id") or "")
        if folder or ident:
            # The FIRST project, not a merge of all of them. A scope names one
            # workspace, and a sweep that silently picked a different project
            # each time would make `project_state` mean a different repository
            # from one revision to the next -- which is worse than reporting
            # about one and saying which.
            import dataclasses

            return dataclasses.replace(scope, workspace=folder, project_id=ident)
    return scope


def _adapters(adapters: Any) -> List[Any]:
    """The adapters to sweep. `None` means every registered one.

    Function-local for the same reason as the scope, and total: a registry that
    will not build is a sweep with nothing to ask rather than a sweep that
    raises out of a background job.
    """
    if adapters is not None:
        return list(adapters)
    try:
        from src.state_mirror.adapters.base import all_adapters

        return list(all_adapters())
    except Exception as exc:  # noqa: BLE001 - no adapters is not a crash
        logger.warning("state reconcile: no adapters could be built: %s", exc)
        return []


def _owning_source(state: Optional[MaterializedState]) -> str:
    """Which adapter has been writing this entity, or `""` when more than one.

    An entity does not record the adapter that discovered it, and it should
    not: two adapters may legitimately observe the same thing. What CAN be read
    is who has been writing its fields, and unanimity is the test. An entity
    whose fields come from two sources is not either one's to retire -- one of
    them going quiet is not evidence the thing is gone -- so `""` is returned
    and nothing retires it.
    """
    if state is None:
        return ""
    sources = {held.source for held in state.fields.values() if held.source}
    return sources.pop() if len(sources) == 1 else ""


def _note(store: Any, source: str, health: str, *, detail: str = "",
          failed: bool = False) -> None:
    """Record what a source did this sweep. `note_source` never raises.

    Written BEFORE the ingest on purpose. Ingest downgrades a source whose
    every observation was refused, and that is the worse news of the two: an
    adapter that answered and answered rubbish is more broken than one that
    answered nothing, and writing this second would hide it.
    """
    store.note_source(source, health=health, detail=detail, failed=failed)


def _health_table(store: Any) -> Dict[str, str]:
    """source -> health, for the un-scoped rows this package writes."""
    try:
        return {str(row.get("source") or ""): str(row.get("health") or "")
                for row in store.sources() if not str(row.get("scope") or "")}
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state reconcile: reading source health failed")
        return {}


_NOT_RUN = object()


def _read_adapter(adapter: Any, method: str, scope: Any, run: _Run, name: str) -> list:
    from src.state_mirror.read_health import capture_failures
    with capture_failures() as failures:
        rows = list(getattr(adapter, method)(scope) or [])
    if failures:
        run.failed.append(name)
        run.errors.append(f'{name}.{method}: read fallback after ' + ', '.join(sorted(failures)))
    return rows


def _ask(store: Any, run: _Run, adapters: Sequence[Any], scope: Any,
         publisher: Any) -> None:
    """Phase 1: discover, observe and relate, one adapter at a time.

    Every call is wrapped separately. An adapter whose `discover` throws may
    still have a working `observe` -- the two read different things -- and
    losing both because one broke would cost facts the machine could still have
    told us. What the failure DOES cost is the right to retire: an adapter
    missing from `run.discovered` cannot take anything away.
    """
    for adapter in adapters:
        name = str(getattr(adapter, "name", "") or type(adapter).__name__)
        try:
            if not adapter.available():
                run.skipped.append(name)
                _note(store, name, HEALTH_UNKNOWN,
                      detail="not available in this build")
                continue
        except Exception as exc:  # noqa: BLE001 - an adapter that cannot say
            run.failed.append(name)
            run.fail(f"{name}.available", exc)
            _note(store, name, HEALTH_DEGRADED, detail=str(exc)[:200], failed=True)
            continue

        run.ran.append(name)
        predicate = getattr(adapter, 'can_retire', None)
        if callable(predicate):
            run.retirement_filters[name] = predicate
        raised = False

        found: Any = _NOT_RUN
        try:
            found = _read_adapter(adapter, 'discover', scope, run, name)
        except Exception as exc:  # noqa: BLE001
            raised = True
            run.fail(f"{name}.discover", exc)
        if found is not _NOT_RUN:
            run.discovered[name] = _record(store, run, found, scope, publisher,
                                           source=name)

        try:
            observed = _read_adapter(adapter, 'observe', scope, run, name)
        except Exception as exc:  # noqa: BLE001
            raised = True
            run.fail(f"{name}.observe", exc)
            observed = []
        run.observations.extend(
            [o for o in observed if _belongs(o, scope, run, name)])

        try:
            edges = _read_adapter(adapter, 'relations', scope, run, name)
        except Exception as exc:  # noqa: BLE001
            raised = True
            run.fail(f"{name}.relations", exc)
            edges = []
        run.relations.extend(
            [e for e in edges if _belongs(e, scope, run, name)])

        raised = raised or name in run.failed
        if raised:
            run.failed.append(name)
        _note(store, name, HEALTH_DEGRADED if raised else HEALTH_OK,
              detail="an adapter call raised" if raised else "", failed=raised)


def _belongs(row: Any, scope: Any, run: _Run, source: str) -> bool:
    """Whether a row an adapter produced is about the owner we swept for.

    `adapters.base` builds every id from the scope, so this can only fail for a
    hand-built double or an adapter that stopped using the builders. It is
    checked anyway, and loudly: an adapter that could file one person's state
    under another's name is the one mistake in this package with a blast radius
    outside the mirror, and a guard that only exists in another module is a
    guard one refactor from being gone.
    """
    owner = str(getattr(row, "owner", "") or "")
    if owner == run.owner:
        return True
    run.errors.append(f"{source}: a row for owner {owner!r} was produced by a "
                      f"sweep for {run.owner!r} and was dropped")
    logger.warning("state reconcile: %s produced a row owned by %r during a "
                   "sweep for %r; dropped", source, owner, run.owner)
    return False


def _record(store: Any, run: _Run, entities: Sequence[Any], scope: Any,
            publisher: Any, *, source: str) -> set:
    """Store what an adapter discovered and announce the ones that are new."""
    ids: set = set()
    for entity in entities:
        if not _belongs(entity, scope, run, source):
            continue
        try:
            existing = store.get_entity(entity.id)
            store.upsert_entity(entity)
        except Exception as exc:  # noqa: BLE001 - one row, not the sweep
            run.failed.append(source)
            run.fail(f"{source}.discover[{getattr(entity, 'id', '?')}]", exc)
            continue
        ids.add(entity.id)
        run.entities += 1
        if existing is not None and not existing.retired():
            continue
        _publish(publisher, run.owner, "state_entity_discovered",
                 entity_id=entity.id, namespace=entity.namespace,
                 project_id=entity.project_id, source=source,
                 kind=entity.kind, schema=entity.schema,
                 revived=existing is not None)
    return ids


def _store_relations(store: Any, run: _Run) -> None:
    """Phase 2b: the edges. One bad edge costs itself and nothing else."""
    for edge in run.relations:
        try:
            store.upsert_relation(edge)
        except Exception as exc:  # noqa: BLE001
            run.fail(f"relation[{getattr(edge, 'key', lambda: '?')()}]", exc)


def _retire(store: Any, run: _Run, publisher: Any) -> None:
    """Phase 3: mark gone what a working adapter has stopped reporting.

    Four conditions, all required, and each removes a way to retire something
    that is still there:

    * the entity's fields all come from ONE source (`_owning_source`);
    * that source ran its `discover` to completion in THIS sweep;
    * it did not report the entity;
    * and NO other adapter that ran reported it either.

    An adapter that raised is absent from `run.discovered` and so fails the
    second condition for everything -- which is the main guard: without it, one
    import error would retire every service, model and run that adapter owns,
    and the next sweep would rediscover them all with their history broken in
    half.

    The fourth condition is the subtler one. Ownership is decided by who has
    been WRITING the fields, and after a disagreement that is whoever wrote
    last -- so an entity two adapters both know about can end up "owned" by the
    one that happens to have spoken most recently. Retiring on that alone would
    take away a thing another source is still reporting, on the strength of a
    tie-break about who observed it last.
    """
    if not run.discovered:
        return
    reported = set()
    for ids in run.discovered.values():
        reported |= ids
    try:
        live = store.list_entities(owner=run.owner, namespace=run.namespace,
                                   limit=SWEEP_CEILING)
    except Exception as exc:  # noqa: BLE001 - nothing retires rather than all
        run.fail("retire.list_entities", exc)
        return
    for entity in live:
        if (entity.project_id != run.project_id
                and (entity.project_id or entity.kind in ('objective', 'project', 'repo'))):
            continue
        owning = _owning_source(store.get_state(entity.id))
        if not owning or owning not in run.discovered or owning in run.failed:
            continue
        if entity.id in reported:
            continue
        predicate = run.retirement_filters.get(owning)
        if predicate is not None:
            try:
                if not predicate(run.scope, entity):
                    continue
            except Exception as exc:
                run.fail(f'{owning}.can_retire', exc)
                continue
        try:
            gone = bool(store.retire_entity(entity.id))
        except Exception as exc:  # noqa: BLE001
            run.fail(f"retire[{entity.id}]", exc)
            continue
        if not gone:
            continue
        run.retired.append(entity.id)
        _publish(publisher, run.owner, "state_entity_retired",
                 entity_id=entity.id, namespace=entity.namespace,
                 project_id=entity.project_id, source=owning,
                 kind=entity.kind,
                 reason=f"{owning} no longer reports it")
        _abandon_conflicts(store, run, entity, publisher, owning)


def _abandon_conflicts(store: Any, run: _Run, entity: Any, publisher: Any,
                       owning: str) -> None:
    """Close the arguments about an entity that is gone. As `abandoned`.

    Not `resolved`, and the distinction is `contracts.CONFLICT_STATUSES`'s own:
    a conflict that settled itself ended with a value that won, and one that
    stopped mattering ended because the thing it was about went away. Recording
    the second as the first would read as if somebody had checked.

    It has to happen HERE because `idx_state_conflict_live` is unique per
    (entity, field) while the status is `reconciling`: a live conflict left on
    a retired entity would block that pair for ever, so if the service ever
    came back, its next genuine disagreement would silently join the old row
    instead of opening a new one.

    The event is `state_conflict_resolved`, which is the vocabulary's only name
    for a conflict leaving `reconciling`; the payload says which of the three
    endings it was, so nothing has to read the name as a claim about the value.
    """
    try:
        open_rows = store.conflicts(owner=run.owner, entity_id=entity.id,
                                    open_only=True, limit=128)
    except Exception as exc:  # noqa: BLE001 - a read path
        run.fail(f"retire.conflicts[{entity.id}]", exc)
        return
    for conflict in open_rows:
        try:
            settled = bool(store.settle_conflict(
                conflict.id, status="abandoned",
                resolution=f"{owning} no longer reports {entity.id}; nothing "
                           "checked which claim was right"))
        except Exception as exc:  # noqa: BLE001 - one conflict, not the sweep
            run.fail(f"retire.settle[{conflict.id}]", exc)
            continue
        if not settled:
            continue
        _publish(publisher, run.owner, "state_conflict_resolved",
                 entity_id=entity.id, namespace=entity.namespace,
                 project_id=entity.project_id, source=owning,
                 conflict_id=conflict.id, field=conflict.field,
                 status="abandoned",
                 reason="the entity it was about was retired")


def _age(store: Any, run: _Run, publisher: Any, now: Any) -> None:
    """Phase 4: re-rate everything, and say what just stopped being usable.

    The state is rewritten with `changed=False` so the change cursor does not
    move: a consumer following `changed_since` must not be woken to be told
    that time passed. `state_became_stale` is the event for that, and it fires
    on the CROSSING -- a field that was already stale last sweep fires nothing,
    because the news is the transition and a stream that repeated it every
    sweep would be a stream nobody reads.

    The threshold is `decision_safe`: the moment a field stops being good
    enough to decide on is the moment worth telling somebody about. `fresh` to
    `aging` is not that moment; `aging` to `stale` is.
    """
    usable = _freshness.acceptable("decision_safe")
    try:
        states = store.list_states(owner=run.owner, namespace=run.namespace,
                                   limit=SWEEP_CEILING)
    except Exception as exc:  # noqa: BLE001
        run.fail("age.list_states", exc)
        return
    for state in states:
        try:
            rerated = _reducers.rerate(state, now=now)
        except Exception as exc:  # noqa: BLE001 - one state, not the sweep
            run.fail(f"age[{state.entity_id}]", exc)
            continue
        aged: List[str] = []
        moved = False
        for name, held in state.fields.items():
            after = rerated.fields.get(name)
            if after is None or after.freshness == held.freshness:
                continue
            moved = True
            if held.freshness in usable and after.freshness not in usable:
                aged.append(name)
        if moved:
            try:
                store.put_state(rerated, changed=False)
            except Exception as exc:  # noqa: BLE001
                run.fail(f"age.put[{state.entity_id}]", exc)
        if not aged:
            continue
        run.stale.extend(f"{state.entity_id}#{name}" for name in sorted(aged))
        _publish(publisher, run.owner, "state_became_stale",
                 entity_id=state.entity_id, namespace=state.namespace,
                 project_id=state.project_id, schema=state.schema,
                 revision=state.revision, fields=sorted(aged),
                 ratings={name: rerated.fields[name].freshness
                          for name in sorted(aged)})


def _health(store: Any, run: _Run, before: Mapping[str, str],
            publisher: Any) -> None:
    """Phase 5: announce the sources that changed health, and only those.

    A source that was already degraded and still is announces nothing. The
    event is about the transition; repeating it every sweep would make the
    stream a heartbeat for broken things and bury the one that just broke.
    """
    after = _health_table(store)
    for name in run.ran + run.skipped:
        was = str(before.get(name, ""))
        current = str(after.get(name, ""))
        if was not in _BAD_HEALTH and current in _BAD_HEALTH:
            run.degraded.append(name)
            _publish(publisher, run.owner, "state_source_degraded",
                     source=name, health=current, previous=was or HEALTH_UNKNOWN)
        elif was in _BAD_HEALTH and current == HEALTH_OK:
            run.recovered.append(name)
            _publish(publisher, run.owner, "state_source_recovered",
                     source=name, health=current, previous=was)


def sweep(*, owner: str, scope: Any = None, adapters: Any = None,
          store: Any = None, publisher: Any = None,
          now: Any = None) -> SweepReport:
    """Ask every source what is true, fold it in, retire what is gone, age the
    rest. Never raises.

    `owner` is the authenticated caller's and is forced onto the scope; see
    `_scope_for`. `adapters=None` sweeps every registered adapter, and passing
    a list is how a targeted refresh asks two sources instead of eleven -- and
    how a test sweeps without building the real eleven against a real machine.
    """
    holder = str(owner or "")
    resolved = _scope_for(holder, scope)
    run = _Run(holder, str(getattr(resolved, "namespace", "") or REAL_NAMESPACE))
    run.scope = resolved
    run.project_id = str(getattr(resolved, 'project_id', '') or '')
    db = store if store is not None else _persistence.store()
    chosen = _adapters(adapters)

    _publish(publisher, holder, "state_reconcile_started",
             namespace=run.namespace,
             project_id=str(getattr(resolved, "project_id", "") or ""),
             sweep_id=run.sweep_id, adapters=[
                 str(getattr(a, "name", "") or type(a).__name__) for a in chosen])

    before = _health_table(db)
    report = _ingest.IngestReport()
    try:
        _ask(db, run, chosen, resolved, publisher)
        report = _ingest.ingest(run.observations, store=db,
                                publisher=publisher, now=now)
        _store_relations(db, run)
        _retire(db, run, publisher)
        _age(db, run, publisher, now)
        _health(db, run, before, publisher)
    except Exception as exc:  # noqa: BLE001 - a background job answers, it does not crash
        logger.exception("state reconcile: the sweep for %r failed", holder)
        run.fail("sweep", exc)

    finished = SweepReport(
        owner=holder,
        namespace=run.namespace,
        sweep_id=run.sweep_id,
        started_at=run.started_at,
        finished_at=now_iso(),
        duration_ms=int((time.monotonic() - run.began) * 1000),
        adapters_run=tuple(run.ran),
        adapters_failed=tuple(dict.fromkeys(run.failed)),
        adapters_skipped=tuple(run.skipped),
        entities_seen=run.entities,
        observations_seen=len(run.observations),
        relations_seen=len(run.relations),
        retired=tuple(run.retired),
        became_stale=tuple(run.stale),
        degraded=tuple(run.degraded),
        recovered=tuple(run.recovered),
        ingest=report,
        errors=tuple(run.errors),
    )
    _publish(publisher, holder, "state_reconcile_completed",
             namespace=run.namespace, sweep_id=run.sweep_id,
             **{k: v for k, v in finished.to_dict().items()
                if k not in ("owner", "namespace", "sweep_id")})
    logger.info("state reconcile %s: %d adapter(s), %d observation(s), "
                "%d changed, %d retired, %d newly stale, %d error(s) in %dms",
                run.sweep_id, len(run.ran), len(run.observations),
                len(report.changed_entities), len(run.retired), len(run.stale),
                len(run.errors), finished.duration_ms)
    return finished
