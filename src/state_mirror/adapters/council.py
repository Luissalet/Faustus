"""Council rooms: what each one is, and what it still owes.

A room of models produces prose faster than anyone can audit it, which is why
`src/council/ledger.py` exists and why the closing summary is built from the
ledger rather than from the transcript. The same argument applies one level up:
"is a room waiting on an unanswered objection?" is a question about the ledger,
and until now it could only be answered by opening the room. Three of the five
numbers here are ledger counts for exactly that reason.

Everything is read through `src/council/service.py::service()` rather than
through `src/council/persistence.py::store()` directly. The service is what
`store()` sits behind, and it is also what applies `_visible` -- the ownership
check that keeps one person's room out of another person's answer. Going
around it to save one call would mean this adapter had its own idea of who may
see a room, and two ideas about that eventually disagree.

Counts, not lists. `open_objections`, `held_claims` and `running_tasks` are
numbers because the ledger owns those rows and a copy of them in the mirror
would be a second place they can be wrong. What the mirror is for is knowing
that there ARE three unanswered objections without opening the room; which
three is a question for the room.

**Namespace.** A council room lives in `real` as an entity of kind `council`.
The `council:<id>` NAMESPACE is a separate world for a room's own isolated
scratch state, which nothing in this build writes; using it for the rooms
themselves would put every room in a namespace of its own, where the reducer
would refuse to fold anything from `real` into it and no query over the machine
would ever see one.

Entity ids: `council://<owner>/<namespace>/<session_id>`. `source_refs` carry
`council:<session_id>`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

SCHEMA = "council_state.v1"

#: The task status that means a participant is working on it right now.
#: `council/contracts.py::TASK_STATUSES` owns the list; this names the one rung
#: of it that `running_tasks` counts.
RUNNING_TASK_STATUS = "running"

#: What `turn_state` may say. Two words, because that is what the service can
#: honestly answer: a turn is in flight or it is not.
TURN_RUNNING = "running"
TURN_IDLE = "idle"
TURN_STATES: Tuple[str, ...] = (TURN_RUNNING, TURN_IDLE)

__all__ = ["CouncilAdapter", "RUNNING_TASK_STATUS", "TURN_RUNNING",
           "TURN_IDLE", "TURN_STATES", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _count(value: Any) -> int:
    """How many rows a ledger key holds. Zero for anything that is not a list.

    Zero rather than `None` on purpose here: `ledger()` returns every one of
    these keys on a room it could read, and a missing key means the ledger said
    "unreadable" -- which the caller has already handled by not asking. What it
    must never do is let a malformed key read as "some".
    """
    return len(value) if isinstance(value, (list, tuple)) else 0


def _running_tasks(book: Mapping[str, Any]) -> int:
    tasks = book.get("tasks")
    if not isinstance(tasks, (list, tuple)):
        return 0
    return sum(1 for t in tasks if isinstance(t, Mapping)
               and _word(t.get("status"), 32) == RUNNING_TASK_STATUS)


class CouncilAdapter(ThreadedAdapter):
    """`src/council/service.py` -- the rooms, their policy and their ledger."""

    name = "council"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        try:
            import src.council.service                         # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("council adapter: src.council.service unavailable: %s", exc)
            return False
        return True

    def _rooms(self, scope: Scope) -> List[Any]:
        from src.council.service import service

        return list(service().list(owner=scope.owner,
                                   limit=scope.capped(200)) or [])

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for room in (self._safe(self._rooms, scope, default=[]) or []):
            ident = _word(getattr(room, "id", ""), 128)
            if not ident:
                continue
            policy = _word(getattr(room, "policy", ""), 64)
            made = entity("council", ident, scope=scope, schema=SCHEMA,
                          display_name=_word(getattr(room, "title", ""), 300),
                          labels=(policy,) if policy else (),
                          source_refs=(f"council:{ident}",),
                          created_at=getattr(room, "created_at", ""),
                          updated_at=getattr(room, "updated_at", ""))
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        stamp = now_iso()
        out: List[StateObservation] = []
        for room in (self._safe(self._rooms, scope, default=[]) or []):
            ident = _word(getattr(room, "id", ""), 128)
            if not ident:
                continue
            target = self._safe(entity_id, "council", scope.owner, ident,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            participants = [p for p in (_word(x, 128) for x
                                        in list(getattr(room, "participants", ()) or ())) if p]
            observed: Dict[str, Any] = {
                "status": _word(getattr(room, "status", ""), 32) or None,
                "policy": _word(getattr(room, "policy", ""), 64) or None,
                "participants": participants or None,
            }
            derived = self._ledger_counts(scope, ident)
            for epistemic, body in (("observed", observed), ("derived", derived)):
                made = observation(target, self.name, body, scope=scope,
                                   schema=SCHEMA, epistemic=epistemic,
                                   observed_at=stamp,
                                   source_revision=_word(getattr(room, "revision", ""), 32),
                                   evidence_refs=(f"council:{ident}",))
                if made is not None:
                    out.append(made)
        return out

    def _ledger_counts(self, scope: Scope, room_id: str) -> Dict[str, Any]:
        """The three counts and the turn state, all computed here.

        A ledger that could not be read answers `{"unreadable": True}` rather
        than raising, and every count below would then be zero -- which would
        say "this room owes nothing" about a room nobody could look at. So an
        unreadable ledger contributes no fields at all, and the reducer keeps
        whatever the last readable sweep found.
        """
        from src.council.service import service

        svc = self._safe(service)
        if svc is None:
            return {}
        out: Dict[str, Any] = {}

        # Called through a lambda so the attribute lookup happens inside the
        # guard too: a service object from a newer build that dropped one of
        # these methods must cost the field, not the sweep.
        live = self._safe(lambda: svc.state(room_id, owner=scope.owner),
                          default={}) or {}
        if isinstance(live, Mapping) and "running" in live:
            out["turn_state"] = TURN_RUNNING if live.get("running") else TURN_IDLE

        book = self._safe(lambda: svc.ledger(room_id, owner=scope.owner),
                          default={}) or {}
        if isinstance(book, Mapping) and book and not book.get("unreadable"):
            out["open_objections"] = _count(book.get("open_objections"))
            out["held_claims"] = _count(book.get("held_claims"))
            out["running_tasks"] = _running_tasks(book)
        return out
