"""Cards waiting for a person, from `src/approval_store.py`.

An approval is the one row in this mirror whose whole value is that it is
STILL waiting. `src/contracts/approval.py` exists because the way an approval
goes wrong in practice is a plan that drifted after the card was signed, and
the way a WAITING approval goes wrong is nobody noticing it: a run parked on a
question nobody was shown looks exactly like a run that is being slow. Putting
pending cards in the mirror is what lets a projection say which is which.

Only `pending()` is read. A granted or denied card is settled and its state is
the decision, not something that ages; a mirror that re-listed every decided
card would be a copy of the approvals table with worse guarantees. `expire_stale`
is not called either: this module reads and a sweep must not decide anything.

`requires_human` is derived from the plan's action rather than stored, because
nothing stores it -- see `_requires_human` for why every action in this build
answers True and why that is worth writing down anyway.

Entity ids: `approval://<owner>/<namespace>/<approval_id>`. `source_refs` carry
`approval:<approval_id>`, which `approval_store.get()` reopens.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

SCHEMA = "approval_state.v1"

__all__ = ["ApprovalsAdapter", "SCHEMA"]


def _word(value: Any, limit: int = 128) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _requires_human(action: Any) -> Optional[bool]:
    """Whether this action needs a person, or `None` when we cannot say.

    `contracts/skill.py::APPROVAL_TRIGGERS` is described by its own comment as
    "what can force a human into the loop", and `ApprovalPlan.parse` will not
    accept an action outside it -- so in this build every stored plan answers
    True. Computing it anyway, instead of writing the constant, is the point:
    the day a trigger is added that a policy may grant on its own, this line is
    where the field stops being constant, rather than where it quietly starts
    being wrong.

    `None` for an unreadable action, which `observation()` drops. A card whose
    action we cannot read is not a card that needs nobody.
    """
    from src.contracts.skill import APPROVAL_TRIGGERS

    name = _word(action, 64)
    if not name:
        return None
    return name in APPROVAL_TRIGGERS


class ApprovalsAdapter(ThreadedAdapter):
    """`src/approval_store.py` -- what is still waiting for a person."""

    name = "approvals"
    schemas = (SCHEMA,)

    def available(self) -> bool:
        try:
            import src.approval_store                          # noqa: F401
        except Exception as exc:                               # noqa: BLE001
            logger.debug("approvals adapter: src.approval_store unavailable: %s", exc)
            return False
        return True

    def _cards(self, scope: Scope) -> List[Any]:
        from src import approval_store

        return list(approval_store.pending(owner=scope.owner,
                                           limit=scope.capped(200)) or [])

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for card in (self._safe(self._cards, scope, default=[]) or []):
            ident = _word(getattr(card, "id", ""), 128)
            if not ident:
                continue
            action = _word(getattr(getattr(card, "plan", None), "action", ""), 64)
            made = entity("approval", ident, scope=scope, schema=SCHEMA,
                          display_name=action, labels=(action,) if action else (),
                          source_refs=(f"approval:{ident}",),
                          created_at=getattr(card, "requested_at", ""))
            if made is not None:
                out.append(made)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        stamp = now_iso()
        out: List[StateObservation] = []
        for card in (self._safe(self._cards, scope, default=[]) or []):
            ident = _word(getattr(card, "id", ""), 128)
            if not ident:
                continue
            target = self._safe(entity_id, "approval", scope.owner, ident,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            action = _word(getattr(getattr(card, "plan", None), "action", ""), 64)
            uses = getattr(card, "uses_left", None)
            # The store is the authority on its own rows, so everything read
            # off the card is `observed`.
            observed: Dict[str, Any] = {
                "status": _word(getattr(card, "status", ""), 32) or None,
                "action": action or None,
                "requested_at": _word(getattr(card, "requested_at", ""), 64) or None,
                "expires_at": _word(getattr(card, "expires_at", ""), 64) or None,
                "uses_left": uses if isinstance(uses, int)
                             and not isinstance(uses, bool) else None,
            }
            derived: Dict[str, Any] = {"requires_human": _requires_human(action)}
            for epistemic, body in (("observed", observed), ("derived", derived)):
                made = observation(target, self.name, body, scope=scope,
                                   schema=SCHEMA, epistemic=epistemic,
                                   observed_at=stamp,
                                   evidence_refs=(f"approval:{ident}",))
                if made is not None:
                    out.append(made)
        return out
