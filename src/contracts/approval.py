"""
contracts/approval.py — a yes that is bound to exactly what was shown.

"Si cambian destinatario, coste, secreto, permisos o output, caduca la
aprobación anterior."  The way that goes wrong in practice is never a forged
approval; it is a plan that drifts one field after the card was signed — a new
recipient, one more secret, a model that turned out to be a cloud one — and an
approval object that still says `granted`.

So an approval stores the plan it approved, not just its hash.  When a later
plan does not match, `covers()` answers with **which fields moved**, because
"approval expired" sends the user to look for a bug and "the recipient changed
from a@x to b@y" sends them to look at the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    one_of, reject_unknown, semver, text, text_list, timestamp, whole,
)
from .skill import APPROVAL_TRIGGERS

APPROVAL_STATUSES = ("pending", "granted", "denied", "expired", "consumed", "revoked")

#: The five things the masterplan names, plus the two that decide *where* the
#: work runs.  A change in any of them is a different question to ask.
PLAN_FIELDS = ("action", "skill_id", "skill_version", "backend", "recipients",
               "cost_units", "secret_names", "permissions", "output_kinds", "detail")


@dataclass(frozen=True)
class ApprovalPlan:
    """What the card said.  Everything a human would have re-read before
    saying yes, and nothing that changes on its own between two identical
    requests — no timestamps, no run id, no attempt counter."""

    action: str
    skill_id: str = ""
    skill_version: str = ""
    backend: str = ""
    recipients: Tuple[str, ...] = ()
    cost_units: Optional[int] = None
    secret_names: Tuple[str, ...] = ()
    permissions: Mapping[str, Any] = field(default_factory=dict)
    output_kinds: Tuple[str, ...] = ()
    detail: str = ""

    @classmethod
    def parse(cls, raw: Any, path: str = "plan") -> "ApprovalPlan":
        data = as_mapping(raw, path)
        reject_unknown(data, PLAN_FIELDS, path)
        skill_id = ident(data, "skill_id", path, required=False)
        skill_version = semver(data, "skill_version", path, required=False)
        if skill_id and not skill_version:
            raise ContractError(
                f"{path}.skill_version",
                "is required alongside skill_id; approving 'the video skill' without a "
                "version approves whatever it becomes after the next update",
            )
        perms = data.get("permissions")
        if perms is not None and not isinstance(perms, Mapping):
            raise ContractError(f"{path}.permissions", "expected an object", got=perms)
        return cls(
            action=one_of(data, "action", path, choices=APPROVAL_TRIGGERS),
            skill_id=skill_id,
            skill_version=skill_version,
            backend=text(data, "backend", path, required=False, max_len=64),
            recipients=text_list(data, "recipients", path, max_items=200, max_len=320),
            cost_units=whole(data, "cost_units", path, minimum=0),
            secret_names=text_list(data, "secret_names", path, max_items=32, max_len=128),
            permissions=dict(perms or {}),
            output_kinds=text_list(data, "output_kinds", path, max_items=16, max_len=32),
            detail=text(data, "detail", path, required=False, max_len=2000),
        )


    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action, "skill_id": self.skill_id,
            "skill_version": self.skill_version, "backend": self.backend,
            "recipients": list(self.recipients), "cost_units": self.cost_units,
            "secret_names": list(self.secret_names),
            "permissions": dict(self.permissions),
            "output_kinds": list(self.output_kinds), "detail": self.detail,
        }

    def fingerprint(self) -> str:
        """`detail` is deliberately part of it: if the wording the user read
        changed, they did not read this plan."""
        return fingerprint([(name, self.to_dict()[name]) for name in PLAN_FIELDS])

    def differences(self, other: "ApprovalPlan") -> Tuple[Dict[str, Any], ...]:
        """Field-by-field, what moved between the approved plan and this one."""
        mine, theirs = self.to_dict(), other.to_dict()
        out = []
        for name in PLAN_FIELDS:
            a, b = mine[name], theirs[name]
            if a != b:
                out.append({"field": name, "approved": a, "now": b})
        return tuple(out)


@dataclass(frozen=True)
class Approval:
    """The signed card.  `uses_left` defaults to 1: an approval is for the
    action that was shown, and a standing yes is something the user has to
    ask for on purpose."""

    id: str
    plan: ApprovalPlan
    status: str = "pending"
    owner: str = ""
    requested_at: str = ""
    decided_at: Optional[str] = None
    decided_by: str = ""
    expires_at: Optional[str] = None
    uses_left: int = 1
    reason: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "plan", "status", "owner", "requested_at", "decided_at",
             "decided_by", "expires_at", "uses_left", "reason", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "approval") -> "Approval":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        status = one_of(data, "status", path, choices=APPROVAL_STATUSES,
                        required=False, default="pending")
        decided = timestamp(data, "decided_at", path)
        if status in ("granted", "denied") and not decided:
            raise ContractError(f"{path}.decided_at", f"is required once status is '{status}'")
        if status == "pending" and decided:
            raise ContractError(f"{path}.decided_at", "is set while the card is still pending")
        return cls(
            id=text(data, "id", path, max_len=64),
            plan=ApprovalPlan.parse(data.get("plan"), f"{path}.plan"),
            status=status,
            owner=text(data, "owner", path, required=False, max_len=128),
            requested_at=timestamp(data, "requested_at", path, default=now_iso()),
            decided_at=decided,
            decided_by=text(data, "decided_by", path, required=False, max_len=128),
            expires_at=timestamp(data, "expires_at", path),
            uses_left=whole(data, "uses_left", path, default=1, minimum=0, maximum=1000),
            reason=text(data, "reason", path, required=False, max_len=1000),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id, "plan": self.plan.to_dict(),
            "plan_fingerprint": self.plan.fingerprint(),
            "status": self.status, "owner": self.owner,
            "requested_at": self.requested_at, "decided_at": self.decided_at,
            "decided_by": self.decided_by, "expires_at": self.expires_at,
            "uses_left": self.uses_left, "reason": self.reason,
        }

    def covers(self, plan: ApprovalPlan, *, now: Optional[str] = None) -> Dict[str, Any]:
        """Does this approval authorise `plan` right now?

        Always answers with a machine-readable `reason` **and** the concrete
        differences, so the caller can tell the user what changed instead of
        showing them a second identical card and hoping they notice."""
        stamp = now or now_iso()
        if self.status != "granted":
            return {"ok": False, "reason": f"status_{self.status}", "changes": ()}
        if self.expires_at and stamp > self.expires_at:
            return {"ok": False, "reason": "expired", "changes": (),
                    "expired_at": self.expires_at}
        if self.uses_left <= 0:
            return {"ok": False, "reason": "used_up", "changes": ()}
        changes = self.plan.differences(plan)
        if changes:
            return {"ok": False, "reason": "plan_changed", "changes": changes}
        return {"ok": True, "reason": "granted", "changes": ()}

    def consumed(self, *, at: Optional[str] = None) -> "Approval":
        left = max(0, self.uses_left - 1)
        return Approval(
            id=self.id, plan=self.plan,
            status="consumed" if left == 0 else self.status,
            owner=self.owner, requested_at=self.requested_at,
            decided_at=self.decided_at or (at or now_iso()),
            decided_by=self.decided_by, expires_at=self.expires_at,
            uses_left=left, reason=self.reason, schema_version=self.schema_version,
        )
