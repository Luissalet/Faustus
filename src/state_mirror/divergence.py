"""divergence.py — BASE-02: does the projection still agree with what each
domain's own registries say right now, and if not, put it back.

`reconcile.sweep()` (section 8.4) already does the general version of this —
ask every adapter, fold what they say, retire what a discovering adapter no
longer reports, age what got stale. This module is a narrower, synchronous
question a caller can ask about ONE scope right now and read the answer to
in the same call: "for `runs`/`approvals`/`models`/`sessions`, does the
projection already match the authority, and if not, exactly which fields
disagree?" `detect_divergence()` answers that WITHOUT writing anything;
`repair()` asks the identical question and then folds the authority's
observations through the real `ingest.ingest()` — the same front door
`reconcile.sweep` and every route already write through, never a second
store or a parallel repair path (hard rule 4).

The trick that keeps `detect_divergence()` side-effect-free is that
`reducers.reduce_many` is already a PURE function of (current state,
observations, clock) — replaying observations against a state is
deterministic and idempotent (see its own module docstring, rule 6): asking
"would folding this observation change anything" and actually folding it are
the same computation, one of them just stops before the write. So
`detect_divergence` runs the exact reducer `repair()` runs for real, against
a state read straight from the store, and reports only the entities where it
found something to move — a projection already in agreement with its
authority produces zero rows, which is what makes this cheap enough to ask
"is anything wrong?" on demand instead of only from a scheduled sweep.

DOMAINS names four adapters, not the eleven `reconcile.sweep` walks: the
council/workspace/hardware/connections/services adapters are that sweep's
job, and BASE-02 only asks for runs, approvals, models and sessions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.state_mirror import ingest as _ingest
from src.state_mirror import persistence as _persistence
from src.state_mirror import queries as _queries
from src.state_mirror import reducers as _reducers
from src.state_mirror.adapters import base as _adapters
from src.state_mirror.contracts import REAL_NAMESPACE
from src.state_mirror.ingest import IngestReport

logger = logging.getLogger(__name__)

__all__ = [
    "DOMAINS", "EntityDivergence", "DivergenceReport", "RepairReport",
    "detect_divergence", "repair",
]

#: The four domains BASE-02 names, each backed by the registered adapter of
#: the same name (`src/state_mirror/adapters/{runs,approvals,models,sessions}.py`).
DOMAINS: Tuple[str, ...] = ("runs", "approvals", "models", "sessions")


@dataclass(frozen=True)
class EntityDivergence:
    """One entity where the projection disagreed with its authority, and
    exactly which fields moved when the authority's observations were
    replayed against it — the same shape `reducers.FieldChange.to_dict()`
    already gives `state_changed` events, so a caller that already renders
    those knows how to render this."""

    entity_id: str
    domain: str
    fields: Tuple[Dict[str, Any], ...] = ()
    #: The projection had nothing recorded for this entity at all — not a
    #: field that moved, an entity `queries.get()` had never heard of.
    missing_from_projection: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "domain": self.domain,
                "fields": list(self.fields),
                "missing_from_projection": self.missing_from_projection}


@dataclass(frozen=True)
class DivergenceReport:
    owner: str
    checked_at: str = ""
    domains_checked: Tuple[str, ...] = ()
    domains_unavailable: Tuple[str, ...] = ()
    diverged: Tuple[EntityDivergence, ...] = ()
    errors: Tuple[str, ...] = ()

    def diverged_count(self) -> int:
        return len(self.diverged)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner, "checked_at": self.checked_at,
            "domains_checked": list(self.domains_checked),
            "domains_unavailable": list(self.domains_unavailable),
            "diverged": [d.to_dict() for d in self.diverged],
            "diverged_count": self.diverged_count(),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RepairReport:
    """What `repair()` found, and what it actually changed. Both travel
    together on purpose: `detected` is "this is what was wrong" and
    `ingest` (an `IngestReport`) is "this is what actually got fixed" —
    they can differ (an entity `detect_divergence` flagged can lose a race
    to a fresher observation arriving between the two reads), and a caller
    showing only one of them would be showing half the story."""

    detected: DivergenceReport
    ingest: IngestReport

    def to_dict(self) -> Dict[str, Any]:
        return {"detected": self.detected.to_dict(), "ingest": self.ingest.to_dict()}


def _adapter_for(domain: str) -> Optional[Any]:
    """The registered `StateAdapter` for `domain`. `all_adapters()` fills in
    the registry's defaults for whatever name nobody has claimed yet — the
    same call `reconcile.sweep` makes — so a test that registered a double
    with `adapters.register(...)` first gets that double back here, exactly
    as `reconcile.sweep` would."""
    _adapters.all_adapters()
    return _adapters.get(domain)


def _safe_available(adapter: Any) -> bool:
    try:
        return bool(adapter.available())
    except Exception:  # noqa: BLE001 - one adapter's failure is not the check's
        return False


def _collect(adapter: Any, scope: Any) -> Tuple[List[Any], List[str]]:
    """(observations, errors) for one adapter. Never raises — the same guard
    `reconcile.sweep` documents for the same reason: one broken source must
    cost itself, not the whole check."""
    try:
        return list(adapter.observe(scope) or []), []
    except Exception as exc:  # noqa: BLE001
        name = getattr(adapter, "name", "") or type(adapter).__name__
        return [], [f"{name}: {type(exc).__name__}: {exc}"]


def detect_divergence(*, owner: str, domains: Sequence[str] = DOMAINS,
                       namespace: str = REAL_NAMESPACE, project_id: str = "",
                       limit: int = 200, store: Any = None,
                       now: Any = None) -> DivergenceReport:
    """What each domain's authority says right now, compared field-by-field
    against the persisted projection — without writing anything.

    An entity is reported only when `reducers.reduce_many` finds something
    to move: a projection already in agreement with its authority produces
    no row for it. `domains_unavailable` names a domain whose adapter is not
    registered or whose `available()` says no (e.g. a build with none of
    `dispatch`/`agent_runs`/`media_runs`/`bg_jobs` importable) — it is
    skipped, never read as "checked and found no divergence", which would be
    the opposite claim.
    """
    db = store if store is not None else _persistence.store()
    scope = _adapters.Scope(owner=owner, project_id=project_id, namespace=namespace, limit=limit)
    checked: List[str] = []
    unavailable: List[str] = []
    errors: List[str] = []
    diverged: List[EntityDivergence] = []

    for domain in domains:
        adapter = _adapter_for(domain)
        if adapter is None or not _safe_available(adapter):
            unavailable.append(domain)
            continue
        checked.append(domain)
        observations, obs_errors = _collect(adapter, scope)
        errors.extend(obs_errors)

        by_entity: Dict[str, List[Any]] = {}
        for obs in observations:
            by_entity.setdefault(obs.entity_id, []).append(obs)

        for entity_id, obs_list in by_entity.items():
            try:
                current = _queries.get(entity_id, owner=owner, store=db, now=now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{domain}/{entity_id}: read failed: {type(exc).__name__}: {exc}")
                continue
            reduction = _reducers.reduce_many(current, obs_list, now=now)
            if reduction.changes:
                diverged.append(EntityDivergence(
                    entity_id=entity_id, domain=domain,
                    fields=tuple(c.to_dict() for c in reduction.changes),
                    missing_from_projection=current is None,
                ))

    return DivergenceReport(
        owner=owner, checked_at=now_iso(), domains_checked=tuple(checked),
        domains_unavailable=tuple(unavailable), diverged=tuple(diverged),
        errors=tuple(errors),
    )


def repair(*, owner: str, domains: Sequence[str] = DOMAINS,
           namespace: str = REAL_NAMESPACE, project_id: str = "",
           limit: int = 200, store: Any = None, publisher: Any = None,
           now: Any = None) -> RepairReport:
    """Reconstruct the projection from each domain's authority.

    Runs `detect_divergence()` first (so the report names what was wrong
    even if a race makes the repair itself a no-op), then re-asks the same
    adapters and folds every observation through the real `ingest.ingest` —
    the identical front door `reconcile.sweep` and every route already use.
    No retirement and no ageing happen here (that is `reconcile.sweep`'s
    job, over every source, on a schedule): this only ever ADDS or CORRECTS
    what an authority just asserted, which is exactly BASE-02's ask
    ("repair() reconstruye la proyección desde la autoridad").
    """
    db = store if store is not None else _persistence.store()
    scope = _adapters.Scope(owner=owner, project_id=project_id, namespace=namespace, limit=limit)
    detected = detect_divergence(owner=owner, domains=domains, namespace=namespace,
                                 project_id=project_id, limit=limit, store=db, now=now)

    observations: List[Any] = []
    for domain in detected.domains_checked:
        adapter = _adapter_for(domain)
        if adapter is None:
            continue
        obs, _errors = _collect(adapter, scope)
        observations.extend(obs)

    report = _ingest.ingest(observations, store=db, publisher=publisher, now=now)
    logger.info(
        "state divergence repair for %r: %d diverging field-change(s) across "
        "%d domain(s); ingest actually changed %d entit(y/ies)",
        owner or "(default)",
        sum(len(d.fields) for d in detected.diverged),
        len(detected.domains_checked), len(report.changed_entities),
    )
    return RepairReport(detected=detected, ingest=report)
