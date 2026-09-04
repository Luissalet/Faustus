"""
contracts/memory.py — knowledge with a scope, and the selection a run used.

Two objects, and the second is the one that matters.  `MemoryEntry` is a fact
with a scope, a source and a trust class.  `MemoryView` is *what a particular
run was actually shown*: which entries went in, which were dropped, and why.

Without the view, "the model knew about the brand voice" is unfalsifiable.
With it, a wrong answer can be traced to an entry that was included, or to one
that was dropped for budget — and those two bugs have different fixes.

`dropped` is therefore not an optimisation detail.  A view that lists what it
kept and stays quiet about what it cut is a view that hides the reason for
half of the model's behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    one_of, reject_unknown, text, text_list, timestamp, whole,
)
from .skill import MEMORY_SCOPES

#: `anti_pattern` is a first-class trust class, not a negative confidence:
#: the curator inverts a rule that keeps being marked harmful, and the model
#: is told to avoid it, which reads nothing like a weak positive.
TRUST_CLASSES = ("candidate", "proven", "anti_pattern", "retired")

MEMORY_SOURCES = ("user", "agent", "tool", "import", "curator", "skill")

DROP_REASONS = ("budget", "scope", "stale", "duplicate", "conflict",
                "low_trust", "retired", "not_relevant")


@dataclass(frozen=True)
class MemoryEntry:
    """One piece of knowledge, and the scope it is allowed to travel in."""

    id: str
    scope: str
    body: str
    source: str = "agent"
    trust: str = "candidate"
    owner: str = ""
    project_id: str = ""
    skill_id: str = ""
    run_id: str = ""
    created_at: str = ""
    updated_at: Optional[str] = None
    evidence: Tuple[str, ...] = ()      # spans/ids that support it
    inverted_from: str = ""             # the rule this anti-pattern came from
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "scope", "body", "source", "trust", "owner", "project_id",
             "skill_id", "run_id", "created_at", "updated_at", "evidence",
             "inverted_from", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "memory") -> "MemoryEntry":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        scope = one_of(data, "scope", path, choices=MEMORY_SCOPES)
        trust = one_of(data, "trust", path, choices=TRUST_CLASSES,
                       required=False, default="candidate")
        project_id = text(data, "project_id", path, required=False, max_len=128)
        skill_id = ident(data, "skill_id", path, required=False)
        if scope == "project" and not project_id:
            raise ContractError(f"{path}.project_id", "is required for a project-scoped entry")
        if scope == "skill" and not skill_id:
            raise ContractError(f"{path}.skill_id", "is required for a skill-scoped entry")
        inverted = text(data, "inverted_from", path, required=False, max_len=64)
        if trust == "anti_pattern" and not inverted:
            raise ContractError(
                f"{path}.inverted_from",
                "is required for an anti-pattern; an avoid-rule with no rule behind it "
                "cannot be explained to the user or undone if the inversion was wrong",
            )
        return cls(
            id=text(data, "id", path, max_len=64),
            scope=scope,
            body=text(data, "body", path, max_len=8000),
            source=one_of(data, "source", path, choices=MEMORY_SOURCES,
                          required=False, default="agent"),
            trust=trust,
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=project_id,
            skill_id=skill_id,
            run_id=text(data, "run_id", path, required=False, max_len=64),
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            updated_at=timestamp(data, "updated_at", path),
            evidence=text_list(data, "evidence", path, max_items=64, max_len=256),
            inverted_from=inverted,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id, "scope": self.scope, "body": self.body,
            "source": self.source, "trust": self.trust, "owner": self.owner,
            "project_id": self.project_id, "skill_id": self.skill_id,
            "run_id": self.run_id, "created_at": self.created_at,
            "updated_at": self.updated_at, "evidence": list(self.evidence),
            "inverted_from": self.inverted_from,
        }

    def readable_by(self, read_scopes: Tuple[str, ...], *,
                    project_id: str = "", skill_id: str = "") -> bool:
        """Scope is not a label, it is a wall.  A project entry never reaches a
        run in another project even if that run declared `project` readable."""
        if self.scope not in read_scopes:
            return False
        if self.scope == "project" and self.project_id != project_id:
            return False
        if self.scope == "skill" and self.skill_id != skill_id:
            return False
        return True


@dataclass(frozen=True)
class MemoryView:
    """The selection a single run was shown, and what it left behind."""

    run_id: str = ""
    scopes: Tuple[str, ...] = ()
    entry_ids: Tuple[str, ...] = ()
    dropped: Tuple[Mapping[str, str], ...] = ()   # {"id", "reason"}
    budget_chars: Optional[int] = None
    used_chars: Optional[int] = None
    degraded: bool = False
    degraded_reason: str = ""
    built_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("run_id", "scopes", "entry_ids", "dropped", "budget_chars",
             "used_chars", "degraded", "degraded_reason", "built_at", "schema_version")


    @classmethod
    def parse(cls, raw: Any, path: str = "memory_view") -> "MemoryView":
        from .base import flag
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        dropped = []
        raw_dropped = data.get("dropped") or ()
        if not isinstance(raw_dropped, (list, tuple)):
            raise ContractError(f"{path}.dropped", "expected a list", got=raw_dropped)
        for i, item in enumerate(raw_dropped):
            entry = as_mapping(item, f"{path}.dropped[{i}]")
            reject_unknown(entry, ("id", "reason", "detail"), f"{path}.dropped[{i}]")
            dropped.append({
                "id": text(entry, "id", f"{path}.dropped[{i}]", max_len=64),
                "reason": one_of(entry, "reason", f"{path}.dropped[{i}]", choices=DROP_REASONS),
                "detail": text(entry, "detail", f"{path}.dropped[{i}]", required=False, max_len=300),
            })
        degraded = flag(data, "degraded", path, default=False)
        reason = text(data, "degraded_reason", path, required=False, max_len=300)
        if degraded and not reason:
            raise ContractError(
                f"{path}.degraded_reason",
                "is required when degraded is true; a degraded view that will not say "
                "what it lost is worse than no flag at all",
            )
        return cls(
            run_id=text(data, "run_id", path, required=False, max_len=64),
            scopes=text_list(data, "scopes", path, choices=MEMORY_SCOPES, max_items=4),
            entry_ids=text_list(data, "entry_ids", path, max_items=2000, max_len=64),
            dropped=tuple(dropped),
            budget_chars=whole(data, "budget_chars", path, minimum=0),
            used_chars=whole(data, "used_chars", path, minimum=0),
            degraded=degraded,
            degraded_reason=reason,
            built_at=timestamp(data, "built_at", path, default=now_iso()),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "scopes": list(self.scopes), "entry_ids": list(self.entry_ids),
            "dropped": [dict(d) for d in self.dropped],
            "budget_chars": self.budget_chars, "used_chars": self.used_chars,
            "degraded": self.degraded, "degraded_reason": self.degraded_reason,
            "built_at": self.built_at,
        }

    def fingerprint(self) -> str:
        """Two runs with the same fingerprint saw the same memory — which is
        what makes "it worked yesterday" a question with an answer."""
        return fingerprint([
            ("scopes", list(self.scopes)),
            ("entries", list(self.entry_ids)),
            ("degraded", self.degraded),
        ])
