"""Chat sessions, and whether anything is happening in them.

Two modules answer half the question each and neither knows the other exists.
`core/session_manager.py` knows which sessions there ARE and what project each
belongs to; `src/agent_runs.py` knows which of them are busy, what their run is
doing and where it sits in the lane queue. The sidebar joins them by hand every
few seconds and nothing else can.

`active` is the field this adapter exists for, and it is the one that must not
be guessed. It is set ONLY when the read of `agent_runs` succeeded: a session
that is not in `active_session_ids()` is idle, but a session we could not ask
about is unknown, and `contracts._flag` is a tri-state precisely so those two
never collapse into `False`. A sidebar that says "idle" about a chat whose
worker is still writing is how somebody sends a second instruction into a
running turn.

`queued_position` lands here rather than on the run, which is where it looks
like it belongs: `run_state.v1` declares no field for a queue position, and
`session_state.v1` does. It is 1-based and only present while a run waits for
its lane; `agent_runs.queued_positions` omits everything else, so absence is
"not waiting" and not "position zero".

Entity ids: `session://<owner>/<namespace>/<session_id>`. `source_refs` carry
`session:<session_id>`, the scheme `context_engine/adapters/sessions.py`
already mints for the same rows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

SCHEMA = "session_state.v1"

__all__ = ["SessionsAdapter", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


class SessionsAdapter(ThreadedAdapter):
    """`core/session_manager.py` plus `src/agent_runs.py` -- what is live."""

    name = "sessions"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        try:
            import core.models                                 # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("sessions adapter: core.models unavailable: %s", exc)
            return False
        return True

    def _sessions(self, scope: Scope) -> List[Any]:
        """The sessions this owner may see, newest first is not available here.

        The shared manager, never a fresh `SessionManager()`: constructing one
        loads every session from the database, and a sweep that did that on a
        timer would be a second full read of the chat history every minute.
        A process that has not installed one yet answers nothing, which is the
        truth about a sweep running before the app booted.
        """
        from core.models import get_session_manager_instance

        manager = get_session_manager_instance()
        if manager is None:
            return []
        # `get_sessions_for_user(None)` means every owner; `""` is a real owner
        # on a no-login install and must not be passed as a filter that nothing
        # matches.
        found = manager.get_sessions_for_user(scope.owner or None)
        rows = list((found or {}).values() if isinstance(found, Mapping) else [])
        return rows[:scope.capped(500)]

    def _runs(self) -> Optional[Tuple[frozenset, Dict[str, int], Any]]:
        """`(active ids, queue positions, the module)`, or `None`.

        `None` when `agent_runs` could not be read at all, and that is what
        keeps `active` absent instead of `False` for the whole sweep. Returning
        empty collections here would be indistinguishable from "nothing is
        running", which is the one answer this adapter must not invent.
        """
        try:
            from src import agent_runs
        except Exception as exc:                               # noqa: BLE001
            logger.debug("sessions adapter: src.agent_runs unavailable: %s", exc)
            return None
        ids = self._safe(agent_runs.active_session_ids)
        if ids is None:
            return None
        positions = self._safe(agent_runs.queued_positions, default={}) or {}
        clean = {_word(k, 128): v for k, v in dict(positions).items()
                 if isinstance(v, int) and not isinstance(v, bool) and v > 0} \
            if isinstance(positions, Mapping) else {}
        return frozenset(_word(x, 128) for x in list(ids)), clean, agent_runs

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for row in (self._safe(self._sessions, scope, default=[]) or []):
            ident = _word(getattr(row, "id", ""), 128)
            if not ident:
                continue
            mode = _word(getattr(row, "mode", ""), 32)
            made = entity("session", ident, scope=scope, schema=SCHEMA,
                          display_name=_word(getattr(row, "name", ""), 300),
                          labels=(mode,) if mode else (),
                          source_refs=(f"session:{ident}",))
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        rows = self._safe(self._sessions, scope, default=[]) or []
        live = self._safe(self._runs)
        stamp = now_iso()
        out: List[StateObservation] = []
        for row in rows:
            ident = _word(getattr(row, "id", ""), 128)
            if not ident:
                continue
            target = self._safe(entity_id, "session", scope.owner, ident,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            body: Dict[str, Any] = {
                "project_id": _word(getattr(row, "project_id", ""), 256) or None,
            }
            if live is not None:
                active, positions, agent_runs = live
                body["active"] = ident in active
                body["turn_state"] = _word(
                    self._safe(agent_runs.get_status, ident), 32) or None
                body["queued_position"] = positions.get(ident)
            # Everything above is read out of the registry that owns it: the
            # session manager for the project binding, `agent_runs` for the
            # run. Nothing here is computed, so one observation is enough.
            made = observation(target, self.name, body, scope=scope,
                               schema=SCHEMA, epistemic="observed",
                               observed_at=stamp,
                               evidence_refs=(f"session:{ident}",))
            if made is not None:
                out.append(made)
        return out
