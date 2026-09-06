"""Section 10: the questions this mirror can answer, and nothing that guesses.

Two families live here and the split is the point.

The **exact queries** -- `get`, `get_fields`, `list_entities`, `list_states`,
`changed_since`, `relations`, `stale`, `conflicts` -- hand back what the store
holds, scoped to one owner. The **situation queries** -- `running_work`,
`blocked_work`, `pending_approvals`, `available_capabilities`,
`unverified_changes`, `resource_pressure` -- answer the questions a person
actually asks, and every one of them is a DETERMINISTIC function over the
schemas in `contracts.SCHEMA_FIELDS`. No model is called, nothing is ranked by
a heuristic, and running the same query twice over the same store at the same
instant gives the same rows. A situation query that consulted a model would be
a mirror that could hallucinate what the machine is doing, which is the one
thing this subsystem exists to make impossible.

**Every read re-rates freshness before it answers.** `FieldState.freshness` is
stored -- it is what the rating was when the observation was folded -- and a
query that returned it would tell a caller at 10:00 that a probe from 09:00 is
`fresh`, because it was, an hour ago. `reducers.rerate` re-derives every
rating against the clock now, moves no revision and deletes nothing: ageing is
not a change to the state, it is a change to what the state is worth. This is
the whole reason a stored rating and a returned rating are different values.

**Every query takes an owner and hands it to the store.** None of them
defaults to `persistence.ANY_OWNER`, and none of them takes the owner from
anything but its own argument -- which comes from an authenticated caller. A
row belonging to somebody else is answered exactly as a row that does not
exist: an empty list, `None`, `{}`. `""` IS a real owner (a single-user
install), so nothing here reads an empty owner as "every owner".

**Reads never raise.** Not the store's reads, not the re-rating, not the
arithmetic in a situation query. A page that 500s because one row is corrupt
is a page that cannot show the user what is wrong with their machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.state_mirror import freshness as _freshness
from src.state_mirror import persistence as _persistence
from src.state_mirror import reducers as _reducers
from src.state_mirror.contracts import (
    REAL_NAMESPACE,
    FieldState,
    MaterializedState,
    StateConflict,
    StateEntity,
    StateRelation,
    schema_fields,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Scope",
    "RUNNING_RUN_STATUSES",
    "QUEUED_RUN_STATUSES",
    "BLOCKED_RUN_STATUSES",
    "BLOCKED_OBJECTIVE_STATUS",
    "PENDING_APPROVAL_STATUS",
    "AVAILABLE_HEALTH",
    "PROVEN_VERDICTS",
    "UNVERIFIED_EPISTEMICS",
    "PRESSURE_THRESHOLDS",
    "SITUATIONS",
    "get",
    "get_fields",
    "list_entities",
    "list_states",
    "changed_since",
    "relations",
    "stale",
    "conflicts",
    "running_work",
    "blocked_work",
    "pending_approvals",
    "available_capabilities",
    "unverified_changes",
    "resource_pressure",
    "situation",
]


@dataclass(frozen=True)
class Scope:
    """Who a situation query is for, and how much of the mirror to read.

    The same three fields `adapters.base.Scope` carries, redeclared here on
    purpose: importing that one would execute `state_mirror.adapters.__init__`,
    which imports all eleven adapter modules, and a query answering "what is
    running" must not drag the ComfyUI adapter into a process that only wanted
    to read a database. The two are not interchangeable and are not meant to
    be; this one has no `workspace`, because a read does not run a probe.

    `owner` has no default that means "everybody". `""` is the real owner of
    every row on a single-user install, and that is what it means here too.
    """

    owner: str = ""
    namespace: str = REAL_NAMESPACE
    project_id: str = ""
    limit: int = 200

    def capped(self, ceiling: int = 500) -> int:
        """`limit` as a number a store query can take, whatever arrived.

        Total, for the reason `adapters.base.Scope.capped` gives: the scope
        comes from a caller, and a query that died because somebody passed
        `limit="50"` would answer nothing about a machine that is fine.
        """
        try:
            wanted = int(self.limit)
        except (TypeError, ValueError):
            wanted = 200
        return max(1, min(wanted, max(1, int(ceiling))))


# -- the closed vocabularies these queries route on -------------------------
#
# The WORDS below are values of schema fields, not field names, so
# `contracts.SCHEMA_FIELDS` cannot hold them: it declares that a run has a
# `status`, not which statuses exist. Each tuple therefore names where its
# words come from, and a word that stops being minted upstream shows up here as
# a tuple with a dead entry rather than as a query that quietly answers
# nothing.

#: `run_state.v1.status` values that mean work is in flight. From
#: `src/dispatch.py` (`queued|running|verifying|done|partial|error|cancelling|
#: cancelled|interrupted`), `src/agent_runs.py` (`running`) and the media and
#: background registries behind `adapters/runs.py`. `cancelling` is in flight
#: on purpose: a job being cancelled is still holding a GPU and still writing
#: files, and a caller asking "what is running" needs to see it.
RUNNING_RUN_STATUSES: Tuple[str, ...] = (
    "running", "verifying", "in_progress", "cancelling", "streaming",
)

#: Waiting for a lane rather than doing anything. Separate from running
#: because the fix is different: a queue is a scheduling problem and a running
#: job is not.
QUEUED_RUN_STATUSES: Tuple[str, ...] = ("queued", "waiting", "pending")

#: A run stopped by something outside itself.
BLOCKED_RUN_STATUSES: Tuple[str, ...] = ("blocked",)

#: `objective_state.v1.status`, from `services/objectives.py::VALID_STATUSES`
#: (`open|in_progress|blocked|done|dropped`). Only the blocked rung is named
#: here; `blocked_by` being non-empty is the other half of the same question
#: and `adapters/objectives.py` derives it on every sweep.
BLOCKED_OBJECTIVE_STATUS = "blocked"

#: `approval_state.v1.status`, from `src/contracts/approval.py`. Only `pending`
#: matters to a situation query: a granted or denied card is a decision, not
#: something anybody is waiting on.
PENDING_APPROVAL_STATUS = "pending"

#: `service_state.v1.health` and `model_state.v1.availability` when the thing
#: can be used. From `adapters/services.py::HEALTH_BY_STATUS` and
#: `src/capability_registry.py::STATES`, which deliberately share the word.
AVAILABLE_HEALTH: Tuple[str, ...] = ("available",)

#: `run_state.v1.proof_status` values that mean somebody checked. From
#: `src/prove.py` (`proved|partial|unproved|contradicted`). `partial` is NOT in
#: here: a partially proved change is a change with an unproved half, and
#: section 9.3 is about not letting an actor's word stand as a verification.
PROVEN_VERDICTS: Tuple[str, ...] = ("proved",)

#: Epistemologies that mean an actor said so and nothing checked it
#: (section 2.1's `reported`). `inferred` sits beside it because a model's
#: proposal is not a verification either, and `unknown` does not: a field
#: nobody has looked at is not an unverified CHANGE, it is an absence, and
#: `MaterializedState.unknown_fields()` is where that lives.
UNVERIFIED_EPISTEMICS: Tuple[str, ...] = ("reported", "inferred")

#: When `device_state.v1` is worth telling somebody about. One rung, not two:
#: a second "warning" tier would need a second set of numbers to defend, and
#: the question this answers is "is the machine about to stop being able to do
#: the work", which has one answer. The fractions are of the total the device
#: reported; `disk_free_bytes` is absolute because a percentage of a 4TB drive
#: and of a 128GB one are not the same amount of headroom.
PRESSURE_THRESHOLDS: Dict[str, float] = {
    "cpu_percent": 90.0,
    "ram_fraction": 0.90,
    "gpu_memory_fraction": 0.90,
    "gpu_utilisation": 95.0,
    "disk_free_bytes": 5.0 * 1024 * 1024 * 1024,
}

#: The six situation queries by name, for a route that dispatches on a string
#: and for `service.situation()`. Closed, so a typo is answered with the list
#: of what exists instead of with an empty result that looks like good news.
#: It is BUILT at the foot of this file from the dispatch table, so the tuple a
#: route offers and the functions that exist cannot drift apart.
SITUATIONS: Tuple[str, ...] = ()


# -- plumbing ---------------------------------------------------------------

def _db(store: Any) -> Any:
    """The store to read, looked up per call when none was passed.

    Never held: `persistence.use_path` drops the memoised instance, and a
    module that captured the old one would keep answering from a database a
    test just moved away from.
    """
    return store if store is not None else _persistence.store()


def _owner(value: Any) -> str:
    """The caller's owner as the store spells it. `""` is a real owner."""
    return str(value).strip() if value is not None else ""


def _rerate(state: Optional[MaterializedState], *, now: Any
            ) -> Optional[MaterializedState]:
    """Re-derive every rating against the clock now. Total.

    A state whose re-rating throws is answered as it was stored rather than not
    at all: a wrong-but-conservative rating is recoverable and a read that
    raised on the turn path is not. The failure is logged with the entity on it.
    """
    if state is None:
        return None
    try:
        return _reducers.rerate(state, now=now)
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: re-rating %s failed", state.entity_id)
        return state


def _mine(state: Optional[MaterializedState], owner: str
          ) -> Optional[MaterializedState]:
    """The state, or `None` when it is somebody else's.

    `get_state` is keyed by entity id and is not owner-scoped, so this is the
    check. Absent and not-yours answer identically, which is the rule the whole
    package holds: a caller must not be able to learn that a row exists by the
    shape of the refusal.
    """
    if state is None:
        return None
    return state if state.owner == owner else None


def _meta(state: MaterializedState, name: str, held: FieldState) -> Dict[str, Any]:
    """One field as a row a route can serialise, metadata and all.

    The value never travels alone. Section 4.4's whole argument is that a
    number without its time, its source and its epistemology is a rumour with
    good posture, and a helper that returned the bare value would be the place
    that argument gets lost.
    """
    return {
        "entity_id": state.entity_id,
        "schema": state.schema,
        "field": name,
        "value": held.value,
        "epistemic": held.epistemic,
        "freshness": held.freshness,
        "observed_at": held.observed_at,
        "source": held.source,
        "ttl_seconds": held.ttl_seconds,
        "trusted": held.trusted(),
    }


def _value(state: MaterializedState, name: str) -> Any:
    held = state.fields.get(name)
    return held.value if held is not None else None


# -- the exact queries ------------------------------------------------------

def get(entity_id: str, *, owner: str, store: Any = None,
        now: Any = None) -> Optional[MaterializedState]:
    """What we believe about one entity right now, re-rated. `None` if not ours.

    This is the function the rest of section 10 is built on, and the re-rating
    is the reason it exists at all rather than callers reading the store: a
    state stored `fresh` an hour ago must read `stale` now, and the store has
    no clock.
    """
    ident = str(entity_id or "").strip()
    if not ident:
        return None
    try:
        state = _db(store).get_state(ident)
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: reading %s failed", ident)
        return None
    return _rerate(_mine(state, _owner(owner)), now=now)


def get_fields(entity_id: str, fields: Sequence[str], *, owner: str,
               store: Any = None, now: Any = None) -> Dict[str, FieldState]:
    """The named fields of one entity, re-rated, with `unknown` for the unseen.

    Every requested field the schema declares comes back, whether or not
    anything has ever observed it -- an unobserved one as a `FieldState` with
    `epistemic` and `freshness` both `unknown` and no source. That is
    deliberate and it is rule 3 of `contracts`: "we have never looked" and "we
    looked and it was empty" are different facts, and a caller handed a dict
    that simply lacked the key would have to guess which one it had.

    A field the schema does NOT declare is absent from the answer entirely. It
    is not a fact about the entity at all, and inventing an `unknown` for it
    would say the mirror might one day know it.
    """
    state = get(entity_id, owner=owner, store=store, now=now)
    if state is None:
        return {}
    declared = set(schema_fields(state.schema))
    out: Dict[str, FieldState] = {}
    for raw in fields or ():
        name = str(raw or "").strip()
        if not name or name not in declared:
            continue
        out[name] = state.fields.get(name) or FieldState()
    return out


def list_entities(*, owner: str, namespace: str = "", project_id: str = "",
                  kind: str = "", include_retired: bool = False,
                  limit: int = 200, store: Any = None) -> List[StateEntity]:
    """The entities this owner has, newest schema-order first. Never raises.

    No re-rating: an entity carries no freshness. What ages is what we believe
    ABOUT it, and that is `get` and `list_states`.
    """
    try:
        return _db(store).list_entities(
            owner=_owner(owner), namespace=str(namespace or ""),
            project_id=str(project_id or ""), kind=str(kind or ""),
            include_retired=bool(include_retired), limit=int(limit or 200))
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: listing entities failed")
        return []


def list_states(*, owner: str, namespace: str = "", project_id: str = "",
                kind: str = "", limit: int = 200, store: Any = None,
                now: Any = None) -> List[MaterializedState]:
    """The plural of `get`, re-rated, and the base every situation query uses.

    Retired entities are excluded by the store's own join, which is what makes
    "what is running" a question about the machine as it is rather than as it
    was.
    """
    try:
        rows = _db(store).list_states(
            owner=_owner(owner), namespace=str(namespace or ""),
            project_id=str(project_id or ""), kind=str(kind or ""),
            limit=int(limit or 200))
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: listing states failed")
        return []
    return [s for s in (_rerate(row, now=now) for row in rows) if s is not None]


def changed_since(cursor: int, *, owner: str, namespace: str = "",
                  project_id: str = "", limit: int = 200, store: Any = None,
                  now: Any = None) -> Tuple[List[MaterializedState], int]:
    """States that changed after `cursor`, re-rated, and the new cursor.

    The cursor moves only on a MATERIAL change -- `put_state(changed=False)`
    leaves it alone -- so a consumer polling this is woken for things that
    moved and not for things that were looked at again. On an empty page the
    cursor comes back unchanged rather than reset, because resetting is how a
    poller silently starts replaying history.
    """
    try:
        rows, head = _db(store).changed_since(
            int(cursor or 0), owner=_owner(owner),
            namespace=str(namespace or ""), project_id=str(project_id or ""),
            limit=int(limit or 200))
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: reading changes failed")
        return [], int(cursor or 0)
    states = [s for s in (_rerate(row, now=now) for row in rows) if s is not None]
    return states, int(head)


def relations(entity_id: str, kinds: Sequence[str] = (), *, owner: str,
              direction: str = "both", limit: int = 200,
              store: Any = None) -> List[StateRelation]:
    """The live edges touching this entity, from either end of the single row.

    Owner-filtered here rather than in the store, which indexes relations by
    entity: an edge is stored once and read from both ends, so scoping it in
    SQL would mean either two owner columns or a join per read. The filter is
    the same rule either way -- an edge whose owner is not the caller's is not
    returned, and neither is one that touches an entity they cannot see.
    """
    ident = str(entity_id or "").strip()
    if not ident:
        return []
    holder = _owner(owner)
    try:
        rows = _db(store).relations(
            ident, kinds=tuple(str(k) for k in (kinds or ()) if str(k or "")),
            direction=str(direction or "both"), limit=int(limit or 200))
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: reading relations of %s failed", ident)
        return []
    return [row for row in rows if row.owner == holder]


def conflicts(*, owner: str, namespace: str = "", entity_id: str = "",
              open_only: bool = True, limit: int = 100,
              store: Any = None) -> List[StateConflict]:
    """Disagreements this owner has, recorded rather than decided (section 9.2)."""
    try:
        return _db(store).conflicts(
            owner=_owner(owner), namespace=str(namespace or ""),
            entity_id=str(entity_id or ""), open_only=bool(open_only),
            limit=int(limit or 100))
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: reading conflicts failed")
        return []


def stale(*, owner: str, namespace: str = "", project_id: str = "",
          kind: str = "", minimum_freshness: str = "decision_safe",
          limit: int = 200, store: Any = None,
          now: Any = None) -> List[Dict[str, Any]]:
    """Every field that no longer meets a risk level, and how to fix it.

    The default is `decision_safe` rather than `action_safe` because this is
    the query a maintenance screen runs: at `action_safe` every field outside
    its TTL is listed and the answer is most of the mirror most of the time,
    which is a list nobody reads. The level is a parameter so a caller about to
    act can ask the strict question.

    Each row carries the rating, a sentence from `freshness.explain` and the
    refresh action that would settle it -- a description, never an execution:
    deciding to spend a probe is a policy call and belongs to the service.
    """
    level = str(minimum_freshness or "decision_safe").strip()
    allowed = _freshness.acceptable(level)
    if not allowed:
        # `acceptable` fails closed on a level it does not know, which would
        # make this list every field in the store. Naming the mistake is better
        # than answering it.
        logger.warning("state queries: %r is not a freshness level; the levels "
                       "are %s", minimum_freshness,
                       list(_freshness.MINIMUM_FRESHNESS_LEVELS))
        return []
    out: List[Dict[str, Any]] = []
    for state in list_states(owner=owner, namespace=namespace,
                             project_id=project_id, kind=kind, limit=limit,
                             store=store, now=now):
        for name, held in sorted(state.fields.items()):
            if held.freshness in allowed:
                continue
            row = _meta(state, name, held)
            row["minimum_freshness"] = level
            row["why"] = _freshness.explain(held.freshness,
                                            observed_at=held.observed_at,
                                            schema=state.schema, field=name,
                                            now=now)
            row["refresh"] = _freshness.refresh_action(
                state.schema, name, source=held.source,
                entity_id=state.entity_id)
            out.append(row)
    return out


# -- the situation queries --------------------------------------------------
#
# Deterministic functions over the schemas, never a model. Each one is a filter
# and an arithmetic, and the arithmetic is written out rather than hidden in a
# ranker so that "why is this in the list" has an answer somebody can check.
#
# They differ in one place, deliberately. `running_work`, `blocked_work`,
# `pending_approvals` and `resource_pressure` list a row whatever its freshness
# and SAY what the freshness is: a run whose status is an hour old is still
# worth showing, with "we last looked an hour ago" attached. Only
# `available_capabilities` filters on freshness, because it is the one whose
# rows a caller acts on -- routing work to a service that was up an hour ago is
# how work gets sent into a hole.

def _rows_for(scope: Scope, kind: str, store: Any, now: Any
              ) -> List[MaterializedState]:
    return list_states(owner=scope.owner, namespace=scope.namespace,
                       project_id=scope.project_id, kind=kind,
                       limit=scope.capped(), store=store, now=now)


def running_work(scope: Scope, *, store: Any = None,
                 now: Any = None) -> List[Dict[str, Any]]:
    """Runs and workflows in flight, with what each one is doing.

    `queued` is reported beside `running` under its own `waiting` flag rather
    than in a separate list: the question "what is the machine doing" includes
    work that is about to start, and a caller that only wants the busy half has
    one boolean to read.
    """
    out: List[Dict[str, Any]] = []
    for kind in ("run", "workflow"):
        for state in _rows_for(scope, kind, store, now):
            status = str(_value(state, "status") or "")
            waiting = status in QUEUED_RUN_STATUSES
            if status not in RUNNING_RUN_STATUSES and not waiting:
                continue
            held = state.fields.get("status") or FieldState()
            row = _meta(state, "status", held)
            row.update({
                "kind": kind,
                "waiting": waiting,
                "phase": _value(state, "phase"),
                "progress": _value(state, "progress"),
                "engine": _value(state, "engine"),
                "label": _value(state, "label"),
                "started_at": _value(state, "started_at"),
                "last_heartbeat": _value(state, "last_heartbeat"),
                "revision": state.revision,
            })
            out.append(row)
    return out


def blocked_work(scope: Scope, *, store: Any = None,
                 now: Any = None) -> List[Dict[str, Any]]:
    """Work that has stopped for a reason outside itself, and the reason.

    Three shapes of blocked, kept in one list because a person asking "what is
    stuck" does not care which registry it came from: a run waiting on an
    approval, a run or objective whose status says so, and an objective whose
    derived `blocked_by` names a dependency that is not done. `blocked_by` is
    the only one of the three that was computed rather than read, and
    `adapters/objectives.py` recomputes it on every sweep.
    """
    out: List[Dict[str, Any]] = []
    for kind in ("run", "workflow"):
        for state in _rows_for(scope, kind, store, now):
            status = str(_value(state, "status") or "")
            pending = _value(state, "approval_pending")
            if status not in BLOCKED_RUN_STATUSES and pending is not True:
                continue
            held = state.fields.get("status") or FieldState()
            row = _meta(state, "status", held)
            row.update({
                "kind": kind,
                "reason": "approval_pending" if pending is True else "status",
                "blocked_by": [],
                "approval_pending": pending,
                "label": _value(state, "label"),
                "revision": state.revision,
            })
            out.append(row)
    for state in _rows_for(scope, "objective", store, now):
        status = str(_value(state, "status") or "")
        blockers = _value(state, "blocked_by") or []
        if status != BLOCKED_OBJECTIVE_STATUS and not blockers:
            continue
        held = state.fields.get("status") or FieldState()
        row = _meta(state, "status", held)
        row.update({
            "kind": "objective",
            "reason": "blocked_by" if blockers else "status",
            "blocked_by": list(blockers) if isinstance(blockers, (list, tuple))
                          else [blockers],
            "approval_pending": None,
            "priority": _value(state, "priority"),
            "impact": _value(state, "impact"),
            "revision": state.revision,
        })
        out.append(row)
    return out


def pending_approvals(scope: Scope, *, store: Any = None,
                      now: Any = None) -> List[Dict[str, Any]]:
    """Cards still waiting for a person.

    The one row in this mirror whose whole value is that it is STILL waiting: a
    run parked on a question nobody was shown looks exactly like a run that is
    being slow, and this is the query that tells them apart. `expires_at` and
    `uses_left` travel with it because an approval that is about to expire and
    one that is not are different amounts of urgency.
    """
    out: List[Dict[str, Any]] = []
    for state in _rows_for(scope, "approval", store, now):
        if str(_value(state, "status") or "") != PENDING_APPROVAL_STATUS:
            continue
        held = state.fields.get("status") or FieldState()
        row = _meta(state, "status", held)
        row.update({
            "action": _value(state, "action"),
            "requested_at": _value(state, "requested_at"),
            "expires_at": _value(state, "expires_at"),
            "uses_left": _value(state, "uses_left"),
            "requires_human": _value(state, "requires_human"),
            "revision": state.revision,
        })
        out.append(row)
    return out


def available_capabilities(scope: Scope, *, store: Any = None, now: Any = None,
                           minimum_freshness: str = "decision_safe"
                           ) -> List[Dict[str, Any]]:
    """What this machine can actually do right now -- and only right now.

    The one situation query that FILTERS on freshness, because it is the one
    whose rows get acted on. "ComfyUI was available" is not a capability; a
    caller routing work to it needs the claim to still be inside its guarantee,
    and `service_state.v1.health` is guaranteed for thirty seconds. A service
    that fell out of that window is simply absent from this list, which is the
    honest answer: we do not currently know that it works.

    Three kinds in one list because "can I do X" spans them: a service answers,
    a model is loaded, a connection is authenticated and reachable.
    """
    allowed = _freshness.acceptable(minimum_freshness) or _freshness.acceptable(
        "decision_safe")
    out: List[Dict[str, Any]] = []

    for state in _rows_for(scope, "service", store, now):
        held = state.fields.get("health")
        if held is None or held.freshness not in allowed:
            continue
        if str(held.value or "") not in AVAILABLE_HEALTH:
            continue
        row = _meta(state, "health", held)
        row.update({"kind": "service",
                    "capabilities": _value(state, "capabilities") or [],
                    "latency_ms": _value(state, "latency_ms"),
                    "revision": state.revision})
        out.append(row)

    for state in _rows_for(scope, "model", store, now):
        held = state.fields.get("availability")
        if held is None or held.freshness not in allowed:
            continue
        if str(held.value or "") not in AVAILABLE_HEALTH:
            continue
        row = _meta(state, "availability", held)
        row.update({"kind": "model", "loaded": _value(state, "loaded"),
                    "backend": _value(state, "backend"),
                    "context_capacity": _value(state, "context_capacity"),
                    "revision": state.revision})
        out.append(row)

    for state in _rows_for(scope, "connection", store, now):
        held = state.fields.get("reachable")
        if held is None or held.freshness not in allowed or held.value is not True:
            continue
        row = _meta(state, "reachable", held)
        row.update({"kind": "connection",
                    "authenticated": _value(state, "authenticated"),
                    "granted_actions": _value(state, "granted_actions") or [],
                    "expires_at": _value(state, "expires_at"),
                    "revision": state.revision})
        out.append(row)
    return out


def unverified_changes(scope: Scope, *, store: Any = None,
                       now: Any = None) -> List[Dict[str, Any]]:
    """What somebody claimed and nothing checked (section 9.3).

    Two rows, one rule. Any field still held at `reported` or `inferred` is an
    actor's word or a model's proposal that no observation has displaced -- an
    agent writing "I finished" lands there and stays there until a ChangeSet,
    an artifact or `prove` moves it. And a run whose `proof_status` is anything
    but `proved` is a change whose evidence did not add up, which is the same
    failure arriving through the other door.

    Every row carries the refresh action that would settle it, so this list is
    a work queue rather than a complaint.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for state in list_states(owner=scope.owner, namespace=scope.namespace,
                             project_id=scope.project_id, limit=scope.capped(),
                             store=store, now=now):
        for name, held in sorted(state.fields.items()):
            unproved = (name == "proof_status"
                        and str(held.value or "") not in PROVEN_VERDICTS
                        and held.value is not None)
            if held.epistemic not in UNVERIFIED_EPISTEMICS and not unproved:
                continue
            key = (state.entity_id, name)
            if key in seen:
                continue
            seen.add(key)
            row = _meta(state, name, held)
            row["reason"] = "unproved" if unproved else held.epistemic
            row["refresh"] = _freshness.refresh_action(
                state.schema, name, source=held.source,
                entity_id=state.entity_id)
            row["revision"] = state.revision
            out.append(row)
    return out


def _pressure(state: MaterializedState, resource: str, used: Any, total: Any,
              limit: float, *, field: str) -> Optional[Dict[str, Any]]:
    """One resource row when it is over its threshold, else `None`.

    A ratio needs both halves and a total of zero is not a full disk: a device
    that reported `gpu_used_bytes` and not `gpu_total_bytes` has told us a
    number we cannot judge, and answering "100% full" to that would be an
    arithmetic error presented as an alarm.
    """
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return None
    ratio = float(used) / float(total)
    if ratio < limit:
        return None
    held = state.fields.get(field) or FieldState()
    row = _meta(state, field, held)
    row.update({"resource": resource, "used": used, "total": total,
                "fraction": round(ratio, 4), "threshold": limit,
                "revision": state.revision})
    return row


def resource_pressure(scope: Scope, *, store: Any = None,
                      now: Any = None) -> List[Dict[str, Any]]:
    """Where this machine is running out, by `PRESSURE_THRESHOLDS`.

    A row exists only when a threshold is crossed, and it names the number and
    the threshold it crossed so the judgement can be argued with. Freshness
    travels with each row and is not filtered on: `device_state.v1` is
    guaranteed for five seconds, so filtering to `fresh` would make this list
    empty on any machine nothing has swept in the last five, which reads as
    "everything is fine" rather than as "nobody has looked".
    """
    out: List[Dict[str, Any]] = []
    for state in _rows_for(scope, "device", store, now):
        cpu = _value(state, "cpu_percent")
        if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) \
                and float(cpu) >= PRESSURE_THRESHOLDS["cpu_percent"]:
            held = state.fields.get("cpu_percent") or FieldState()
            row = _meta(state, "cpu_percent", held)
            row.update({"resource": "cpu", "used": cpu, "total": 100.0,
                        "fraction": round(float(cpu) / 100.0, 4),
                        "threshold": PRESSURE_THRESHOLDS["cpu_percent"],
                        "revision": state.revision})
            out.append(row)

        ram = _pressure(state, "ram", _value(state, "ram_used_bytes"),
                        _value(state, "ram_total_bytes"),
                        PRESSURE_THRESHOLDS["ram_fraction"],
                        field="ram_used_bytes")
        if ram is not None:
            out.append(ram)

        gpu = _pressure(state, "gpu_memory", _value(state, "gpu_used_bytes"),
                        _value(state, "gpu_total_bytes"),
                        PRESSURE_THRESHOLDS["gpu_memory_fraction"],
                        field="gpu_used_bytes")
        if gpu is not None:
            out.append(gpu)

        utilisation = _value(state, "gpu_utilisation")
        if isinstance(utilisation, (int, float)) and not isinstance(utilisation, bool) \
                and float(utilisation) >= PRESSURE_THRESHOLDS["gpu_utilisation"]:
            held = state.fields.get("gpu_utilisation") or FieldState()
            row = _meta(state, "gpu_utilisation", held)
            row.update({"resource": "gpu_utilisation", "used": utilisation,
                        "total": 100.0,
                        "fraction": round(float(utilisation) / 100.0, 4),
                        "threshold": PRESSURE_THRESHOLDS["gpu_utilisation"],
                        "revision": state.revision})
            out.append(row)

        free = _value(state, "disk_free_bytes")
        if isinstance(free, (int, float)) and not isinstance(free, bool) \
                and float(free) <= PRESSURE_THRESHOLDS["disk_free_bytes"]:
            held = state.fields.get("disk_free_bytes") or FieldState()
            row = _meta(state, "disk_free_bytes", held)
            row.update({"resource": "disk", "used": None, "total": None,
                        "free": free,
                        "threshold": PRESSURE_THRESHOLDS["disk_free_bytes"],
                        "revision": state.revision})
            out.append(row)
    return out


#: Name -> function. `SITUATIONS` is rebuilt from this table below, so the
#: closed tuple a route offers and the functions that actually exist cannot
#: disagree: a query added here is offered without a second edit, and one
#: removed stops being offered.
_SITUATIONS = {
    "running_work": running_work,
    "blocked_work": blocked_work,
    "pending_approvals": pending_approvals,
    "available_capabilities": available_capabilities,
    "unverified_changes": unverified_changes,
    "resource_pressure": resource_pressure,
}

SITUATIONS = tuple(sorted(_SITUATIONS))


def situation(name: str, scope: Scope, *, store: Any = None,
              now: Any = None) -> List[Dict[str, Any]]:
    """Run one situation query by name, or answer `[]` for one that is not.

    A name outside `SITUATIONS` is logged with the list of what exists. It is
    not an exception: this is dispatched from a route parameter, and a typo in
    a URL should not be able to raise out of a read.
    """
    fn = _SITUATIONS.get(str(name or "").strip())
    if fn is None:
        logger.warning("state queries: %r is not a situation query; they are %s",
                       name, list(SITUATIONS))
        return []
    try:
        return fn(scope, store=store, now=now)
    except Exception:  # noqa: BLE001 - a read path
        logger.exception("state queries: the %s situation failed", name)
        return []
