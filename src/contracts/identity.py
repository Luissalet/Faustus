"""
contracts/identity.py — who is on the other end of a channel or a node.

Faustus will eventually be reachable from Telegram, a phone, a paired PC and a
CLI.  The thing that must not happen is that reaching it from somewhere new
quietly counts as being the owner.  So an external identity is a *binding*: a
provider, an id issued by that provider, the local user it was paired to, when,
by whom, and what it may do — plus a revocation that is a fact on the record
rather than the absence of a row.

`revoked_at` is why this is a contract and not a dict.  A binding that is
deleted on revocation leaves an audit trail that cannot answer "who had access
last March", and that is exactly the question asked after something goes wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    reject_unknown, text, text_list, timestamp, whole,
)

#: Kept open-ended by design — a provider is an id, not an enum, because the
#: point of the gateway is that new channels arrive without a core change.
#: What is *not* open-ended is what a binding may do.
CAPABILITIES = ("chat", "read_artifacts", "start_run", "approve", "upload", "notify")


@dataclass(frozen=True)
class ExternalIdentity:
    """One paired way in.  Deny by default: a fresh binding can do nothing
    until someone lists a capability on it."""

    id: str
    provider: str
    external_id: str
    owner: str = ""
    display_name: str = ""
    capabilities: Tuple[str, ...] = ()
    project_id: str = ""
    node_id: str = ""
    paired_at: str = ""
    paired_by: str = ""
    last_seen_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_reason: str = ""
    rate_limit_per_hour: Optional[int] = None
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "provider", "external_id", "owner", "display_name", "capabilities",
             "project_id", "node_id", "paired_at", "paired_by", "last_seen_at",
             "revoked_at", "revoked_reason", "rate_limit_per_hour", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "identity") -> "ExternalIdentity":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        owner = text(data, "owner", path, required=False, max_len=128)
        caps = text_list(data, "capabilities", path, choices=CAPABILITIES,
                         max_items=len(CAPABILITIES))
        revoked = timestamp(data, "revoked_at", path)
        reason = text(data, "revoked_reason", path, required=False, max_len=300)
        if revoked and not reason:
            raise ContractError(
                f"{path}.revoked_reason",
                "is required when revoked_at is set; a revocation nobody can explain "
                "later gets undone by the next person who wonders why it happened",
            )
        if caps and not owner:
            raise ContractError(
                f"{path}.owner",
                "is required once the binding can do anything; a capability that maps "
                "to no local user is a capability with no policy behind it",
            )
        return cls(
            id=text(data, "id", path, max_len=64),
            provider=ident(data, "provider", path),
            external_id=text(data, "external_id", path, max_len=256),
            owner=owner,
            display_name=text(data, "display_name", path, required=False, max_len=200),
            capabilities=caps,
            project_id=text(data, "project_id", path, required=False, max_len=128),
            node_id=text(data, "node_id", path, required=False, max_len=64),
            paired_at=timestamp(data, "paired_at", path, default=now_iso()),
            paired_by=text(data, "paired_by", path, required=False, max_len=128),
            last_seen_at=timestamp(data, "last_seen_at", path),
            revoked_at=revoked,
            revoked_reason=reason,
            rate_limit_per_hour=whole(data, "rate_limit_per_hour", path, minimum=0),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    @property
    def active(self) -> bool:
        return self.revoked_at is None and bool(self.capabilities)

    def may(self, capability: str) -> Dict[str, Any]:
        """Answer with the reason, not just the boolean — a channel that says
        "no" without saying "revoked on 3 March" makes the user retry."""
        if self.revoked_at:
            return {"ok": False, "reason": "revoked",
                    "at": self.revoked_at, "detail": self.revoked_reason}
        if capability not in CAPABILITIES:
            return {"ok": False, "reason": "unknown_capability", "detail": capability}
        if capability not in self.capabilities:
            return {"ok": False, "reason": "not_granted",
                    "detail": f"this binding has {list(self.capabilities)}"}
        return {"ok": True, "reason": "granted"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.id,
            "provider": self.provider, "external_id": self.external_id,
            "owner": self.owner, "display_name": self.display_name,
            "capabilities": list(self.capabilities), "project_id": self.project_id,
            "node_id": self.node_id, "paired_at": self.paired_at,
            "paired_by": self.paired_by, "last_seen_at": self.last_seen_at,
            "revoked_at": self.revoked_at, "revoked_reason": self.revoked_reason,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "active": self.active,
        }

    def fingerprint(self) -> str:
        return fingerprint([
            ("provider", self.provider),
            ("external_id", self.external_id),
            ("owner", self.owner),
            ("capabilities", list(self.capabilities)),
        ])
