"""harness_evolution/models.py — the versioned-transaction shape (dictamen §7).

`HarnessRevision` is a snapshot of the active harness: which instructions,
which specialists, which skill versions, which memory refs it runs on. A
`CandidatePatch` is a proposed, typed change against one parent revision,
carrying its own evaluation evidence and the exact revision to fall back to
if it ever needs to be undone.

Both are plain dataclasses with `to_dict`/`from_row` — `store.py` owns the
only sqlite table each lives in; nothing here opens a connection.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from src.contracts.base import now_iso

#: A revision is the currently running harness (`active`), a proposed one not
#: yet running (`candidate`), or one superseded/reverted (`retired`).
REVISION_STATUSES = ("active", "candidate", "retired")

#: The three typed changes a `CandidatePatch` may carry (dictamen §7). A
#: patch always names exactly one — composing several is a sequence of
#: patches, not one patch with mixed intent, so a promotion never has to
#: guess which half of a mixed change caused a regression.
CHANGE_TYPES = ("add_skill", "update_instruction", "update_specialist")

#: The lifecycle a `CandidatePatch` walks through. `proposed` -> `validated`
#: (schema/refs/permissions ok) or `rejected` (A26) -> `evaluated` (source +
#: held-out both passed) or `evaluated_failed` (A27 — held-out failed, no
#: promotion) -> `promoted` (A28/A30 gate) -> `reverted`/`disabled` if a
#: recheck fails or a human rolls it back.
PATCH_STATUSES = (
    "proposed", "rejected", "validated", "evaluated", "evaluated_failed",
    "promoted", "reverted", "disabled",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@dataclass
class HarnessRevision:
    revision_id: str
    parent_id: Optional[str]
    created_at: str
    refs: Dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    #: Optimistic-concurrency counter. `store.set_active` (the CAS step A30
    #: depends on) only succeeds when the caller's expected version still
    #: matches the row's current one.
    version: int = 1

    @classmethod
    def bootstrap(cls, *, refs: Optional[Mapping[str, Any]] = None) -> "HarnessRevision":
        """The very first revision a fresh store starts from — no parent,
        already `active`, so `propose()` always has something to branch
        from."""
        return cls(revision_id=new_id("rev"), parent_id=None, created_at=now_iso(),
                   refs=dict(refs or {}), status="active", version=1)

    def to_dict(self) -> Dict[str, Any]:
        return {"revision_id": self.revision_id, "parent_id": self.parent_id,
                "created_at": self.created_at, "refs": dict(self.refs),
                "status": self.status, "version": self.version}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "HarnessRevision":
        return cls(revision_id=row["revision_id"], parent_id=row["parent_id"],
                   created_at=row["created_at"], refs=json.loads(row["refs_json"] or "{}"),
                   status=row["status"], version=int(row["version"]))


@dataclass
class CandidatePatch:
    patch_id: str
    parent_revision: str
    changes: Dict[str, Any]
    source_trace_ids: List[str] = field(default_factory=list)
    scope: str = ""
    required_capabilities: Dict[str, str] = field(default_factory=dict)
    evaluation: Dict[str, Any] = field(default_factory=dict)
    rollback_target: Optional[str] = None
    status: str = "proposed"
    trace: str = ""
    created_at: str = ""
    #: Set once `promotion.promote` succeeds — the revision this patch
    #: became.
    promoted_revision_id: Optional[str] = None

    @property
    def change_type(self) -> str:
        return str((self.changes or {}).get("type") or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patch_id": self.patch_id, "parent_revision": self.parent_revision,
            "changes": dict(self.changes or {}),
            "source_trace_ids": list(self.source_trace_ids),
            "scope": self.scope, "required_capabilities": dict(self.required_capabilities),
            "evaluation": dict(self.evaluation or {}), "rollback_target": self.rollback_target,
            "status": self.status, "trace": self.trace, "created_at": self.created_at,
            "promoted_revision_id": self.promoted_revision_id,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CandidatePatch":
        return cls(
            patch_id=row["patch_id"], parent_revision=row["parent_revision"],
            changes=json.loads(row["changes_json"] or "{}"),
            source_trace_ids=json.loads(row["source_trace_ids_json"] or "[]"),
            scope=row["scope"] or "",
            required_capabilities=json.loads(row["required_capabilities_json"] or "{}"),
            evaluation=json.loads(row["evaluation_json"] or "{}"),
            rollback_target=row["rollback_target"], status=row["status"],
            trace=row["trace"] or "", created_at=row["created_at"],
            promoted_revision_id=row["promoted_revision_id"],
        )
