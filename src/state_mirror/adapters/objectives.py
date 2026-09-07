"""What the project committed to, and what is holding each piece up.

`services/objectives.py` owns the file: a JSONL of objectives plus a separate
edge list, with an audit log beside it. It already computes structural
importance (`impact_scores`) and already renders "blocked by OBJ-2" into a
prompt line, but nothing in the application can ask "which objectives are
blocked right now" as a query -- `render_lines` writes that sentence into text
and throws the structure away. That is what this adapter adds.

`blocked_by` is DERIVED, and it is the only field here that is. A dependency
edge is a thing a person declared; being blocked is arithmetic over that edge
and the current status of the objective at its far end, recomputed on every
sweep. A dep that is `done` blocks nothing -- that is the whole point of
marking it done -- and a dep this file has never heard of blocks its dependent
until somebody explains where it went, because an objective waiting on
something nobody can find is not an objective that is free to start.

`relations()` emits both edges. `depends_on` is `declared`: an actor wrote it
down. `blocked_by` is `observed`: nothing guessed it, it was read off the same
file, and `RELATION_ORIGINS` has no `derived` rung to put it in -- `inferred`
would say a heuristic proposed it, which is a worse lie than the rounding.

The objective record carries an `owner` field of its own whose value is the
literal word `user` or `agent`: it names who last touched the row, not who the
row belongs to. It is never read here. Every entity carries the scope's owner,
which on this install is the only owner objectives have.

Entity ids include a stable project/workspace suffix after `OBJ-3`. `source_refs` carry
`objective:OBJ-3`, the same scheme `context_engine/adapters/objectives.py`
mints, so the two subsystems name the same objective the same way.
"""

from __future__ import annotations

import logging
import hashlib
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
    relation,
)
from src.state_mirror.contracts import (
    StateEntity,
    StateObservation,
    StateRelation,
    entity_id,
)

logger = logging.getLogger(__name__)

SCHEMA = "objective_state.v1"


def objective_identifier(scope: Scope, oid: str) -> str:
    """OBJ numbers belong to a project, or to one canonical bare workspace."""
    identity = ('project:' + scope.project_id) if scope.project_id else (
        'workspace:' + os.path.normcase(os.path.realpath(scope.workspace)) if scope.workspace else '')
    return oid + '~' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24] if identity else oid

#: The status that stops an objective from blocking its dependents. One word,
#: and `dropped` is deliberately not in it: a dependency somebody abandoned
#: leaves its dependent stuck, and calling that "unblocked" would send an agent
#: to build on something nobody is going to deliver.
DONE_STATUS = "done"

__all__ = ["ObjectivesAdapter", "DONE_STATUS", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def resolve_project(scope: Scope) -> Optional[Dict[str, Any]]:
    """The project record for this scope, or `None`.

    An explicit project must resolve for the exact owner. Do not fall back to
    a workspace when lookup fails: that would hide an outage or bypass the
    project ownership check. The caller's _safe wrapper records that failure
    without taking down the sweep or treating the missing read as deletion.
    """
    if scope.project_id:
        from services.projects import get_store
        project = get_store().get(scope.project_id, scope.owner)
        if not isinstance(project, dict):
            raise LookupError('Project objectives are unavailable for this owner')
        return project
    if scope.workspace:
        return {"workspace": scope.workspace}
    return None


def _blocked_by(deps: List[str], statuses: Mapping[str, str]) -> List[str]:
    """The deps that are not done, in the order they were declared.

    A dep missing from `statuses` counts as blocking: it was declared and this
    file cannot find it, and the honest reading of that is "waiting on
    something we cannot see", never "free to go".
    """
    return [dep for dep in deps if statuses.get(dep, "") != DONE_STATUS]


def _evidence_counts(log: Any) -> Dict[str, int]:
    """Evidence records per objective id, from the audit log.

    Objectives keep evidence only in the log (`objectives.add_evidence`
    appends there and nowhere else), so this is a count over the window
    `dashboard_payload` returned and not a total for all time. That is what
    makes it `derived` rather than `observed`: it is arithmetic over a bounded
    read, and a consumer that needs the true total has to widen the window.
    """
    counts: Dict[str, int] = {}
    for record in list(log or []):
        if not isinstance(record, Mapping):
            continue
        if _word(record.get("kind"), 32) != "evidence":
            continue
        oid = _word(record.get("id"), 64)
        if oid:
            counts[oid] = counts.get(oid, 0) + 1
    return counts


class ObjectivesAdapter(ThreadedAdapter):
    """`services/objectives.py` -- the goals, their impact and their blockers."""

    name = "objectives"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        try:
            import services.objectives                         # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("objectives adapter: services.objectives unavailable: %s",
                         exc)
            return False
        return True

    def _payload(self, scope: Scope) -> Dict[str, Any]:
        """Objectives, edges, impact scores and the audit log, in one read.

        `dashboard_payload` is the call that already assembles all four from a
        single `load_state`. Calling `serialize_state` and `impact_scores`
        separately would read the file twice and could see two different
        versions of it.
        """
        import services.objectives as objectives

        project = resolve_project(scope)
        if not project:
            return {}
        payload = objectives.dashboard_payload(project, strict=True)
        if not isinstance(payload, Mapping) or not isinstance(payload.get('objectives'), list):
            raise ValueError('Objective source did not return an objective snapshot')
        return dict(payload)

    def _records(self, scope: Scope) -> Tuple[List[Mapping[str, Any]],
                                              Dict[str, Any], Dict[str, int]]:
        payload = self._safe(self._payload, scope, default={}) or {}
        raw = payload.get("objectives") if isinstance(payload, Mapping) else None
        records = [r for r in list(raw or []) if isinstance(r, Mapping)
                   and _word(r.get("id"), 64)]
        scores = payload.get("scores") if isinstance(payload, Mapping) else None
        counts = _evidence_counts(payload.get("log")
                                  if isinstance(payload, Mapping) else None)
        return records, dict(scores or {}), counts

    def discover(self, scope: Scope) -> List[StateEntity]:
        records, _, _ = self._records(scope)
        out: List[StateEntity] = []
        for record in records:
            oid = _word(record.get("id"), 64)
            made = entity("objective", objective_identifier(scope, oid), scope=scope, schema=SCHEMA,
                          display_name=_word(record.get("title"), 300),
                          labels=(_word(record.get("status"), 32),),
                          source_refs=(f"objective:{oid}",),
                          created_at=record.get("created_at") or "",
                          updated_at=record.get("updated_at") or "")
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        records, scores, counts = self._records(scope)
        statuses = {_word(r.get("id"), 64): _word(r.get("status"), 32)
                    for r in records}
        stamp = now_iso()
        out: List[StateObservation] = []
        for record in records:
            oid = _word(record.get("id"), 64)
            target = self._safe(entity_id, "objective", scope.owner, objective_identifier(scope, oid),
                                namespace=scope.namespace, default="")
            if not target:
                continue
            priority = record.get("priority")
            observed: Dict[str, Any] = {
                "status": statuses.get(oid) or None,
                "priority": priority if isinstance(priority, int)
                            and not isinstance(priority, bool) else None,
                "updated_at": _word(record.get("updated_at"), 64) or None,
            }
            deps = [_word(d, 64) for d in list(record.get("deps") or [])]
            derived: Dict[str, Any] = {
                "blocked_by": _blocked_by([d for d in deps if d], statuses),
                "impact": self._impact(scores.get(oid)),
                "evidence_count": counts.get(oid, 0),
            }
            for epistemic, body in (("observed", observed), ("derived", derived)):
                made = observation(target, self.name, body, scope=scope,
                                   schema=SCHEMA, epistemic=epistemic,
                                   observed_at=stamp,
                                   source_revision=_word(record.get("updated_at"), 64),
                                   evidence_refs=(f"objective:{oid}",))
                if made is not None:
                    out.append(made)
        return out

    @staticmethod
    def _impact(entry: Any) -> Optional[float]:
        """The structural score, or `None` when there is not one.

        Only the number travels. `impact_scores` also returns the five
        components and a prose hint, and copying those into the mirror would
        make this the second place they can be stale -- the score is the
        summary, and the dashboard is where the working is shown.
        """
        if not isinstance(entry, Mapping):
            return None
        score = entry.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        return round(float(score), 4)

    def relations(self, scope: Scope) -> List[StateRelation]:
        """`depends_on` for every declared edge, `blocked_by` for the live ones.

        Both directions are stored once, from the dependent to the dependency,
        because `StateRelation` reads either end of a single row: writing the
        inverse as a second row would create two rows that can disagree about
        the same fact.
        """
        records, _, _ = self._records(scope)
        statuses = {_word(r.get("id"), 64): _word(r.get("status"), 32)
                    for r in records}
        stamp = now_iso()
        out: List[StateRelation] = []
        for record in records:
            oid = _word(record.get("id"), 64)
            source_id = self._safe(entity_id, "objective", scope.owner, objective_identifier(scope, oid),
                                   namespace=scope.namespace, default="")
            if not source_id:
                continue
            deps = [d for d in (_word(x, 64) for x in list(record.get("deps") or [])) if d]
            blocking = set(_blocked_by(deps, statuses))
            for dep in deps:
                target = self._safe(entity_id, "objective", scope.owner, objective_identifier(scope, dep),
                                    namespace=scope.namespace, default="")
                if not target:
                    continue
                out.append(relation(source_id, "depends_on", target,
                                    source=self.name, scope=scope,
                                    origin="declared", observed_at=stamp))
                if dep in blocking:
                    out.append(relation(source_id, "blocked_by", target,
                                        source=self.name, scope=scope,
                                        origin="observed", observed_at=stamp))
        return [edge for edge in out if edge is not None]

    def can_retire(self, scope: Scope, row: StateEntity) -> bool:
        if scope.project_id:
            return row.project_id == scope.project_id
        if not scope.workspace:
            return False  # An unbound sweep has not enumerated any workspace.
        suffix = objective_identifier(scope, 'OBJ-1').split('~', 1)[1]
        return row.id.endswith('~' + suffix)
