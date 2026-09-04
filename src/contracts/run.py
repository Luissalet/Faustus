"""
contracts/run.py — one execution of a chat turn, a skill or a workflow node.

The state machine is written down rather than implied because three parts of
Faustus already disagree politely about it: `agent_runs` knows about queued and
stopped, `crash_recovery` invented `interrupted` for "the process died and
nobody can say whether it worked", and the dispatch worker distinguishes
cancelled from failed.  A run that cannot say which of those happened is
exactly the run whose summary lies.

`interrupted` is deliberately terminal and deliberately not a failure.  It
means the same as `prove`'s `unproved`: the work may well have completed and
nothing survives that could show it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    one_of, reject_unknown, semver, text, text_list, timestamp, whole,
)

RUN_KINDS = ("chat", "skill", "workflow", "tool")

STATUSES = ("created", "awaiting_approval", "running",
            "completed", "failed", "cancelled", "interrupted")

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})

#: What may follow what.  Anything absent is a bug in the caller, and saying so
#: is cheaper than discovering later that a cancelled run reported success.
TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "created": ("awaiting_approval", "running", "cancelled", "failed", "interrupted"),
    "awaiting_approval": ("running", "cancelled", "failed", "interrupted"),
    "running": ("completed", "failed", "cancelled", "interrupted", "awaiting_approval"),
    "completed": (),
    "failed": (),
    "cancelled": (),
    "interrupted": (),
}

#: The four-valued outcome from `src.tool_outcome`, so a run and a tool call
#: are summarised in the same vocabulary.
OUTCOME_OF = {
    "completed": "success",
    "failed": "panic",
    "cancelled": "cancelled",
    "interrupted": None,        # honestly unknown, not a failure
}


def check_transition(current: str, nxt: str, *, path: str = "run.status") -> None:
    """Raise unless `current → nxt` is a transition the machine allows."""
    if current not in TRANSITIONS:
        raise ContractError(path, f"unknown current status; expected one of {list(STATUSES)}", got=current)
    if nxt not in STATUSES:
        raise ContractError(path, f"unknown status; expected one of {list(STATUSES)}", got=nxt)
    if nxt == current:
        return
    allowed = TRANSITIONS[current]
    if nxt not in allowed:
        detail = f"cannot go {current} → {nxt}"
        if current in TERMINAL:
            detail += " (a terminal run does not change its mind; open a new run)"
        else:
            detail += f"; allowed from {current}: {list(allowed)}"
        raise ContractError(path, detail)


@dataclass(frozen=True)
class Run:
    """A run carries its own provenance: who asked, which skill and version,
    which spec, and which approval — if any — let it start."""

    id: str
    kind: str
    status: str = "created"
    owner: str = ""
    project_id: str = ""
    session_id: str = ""
    skill_id: str = ""
    skill_version: str = ""
    parent_run_id: str = ""
    execution_fingerprint: str = ""
    approval_id: str = ""
    label: str = ""
    created_at: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    cost_units: Optional[int] = None
    reason: str = ""                 # why it ended, when the status alone is thin
    artifact_ids: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "kind", "status", "owner", "project_id", "session_id", "skill_id",
             "skill_version", "parent_run_id", "execution_fingerprint", "approval_id",
             "label", "created_at", "started_at", "ended_at", "cost_units", "reason",
             "artifact_ids", "schema_version",
             # Derived, and accepted only so a `to_dict()` payload round-trips.
             # It is checked rather than ignored: a row that says `cancelled`
             # and `success` is a contradiction, and reading past it is how a
             # summary ends up reporting the wrong one.
             "outcome")


    @classmethod
    def parse(cls, raw: Any, path: str = "run") -> "Run":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        kind = one_of(data, "kind", path, choices=RUN_KINDS)
        status = one_of(data, "status", path, choices=STATUSES, required=False, default="created")
        skill_id = ident(data, "skill_id", path, required=False)
        skill_version = semver(data, "skill_version", path, required=False)
        if kind == "skill" and not skill_id:
            raise ContractError(f"{path}.skill_id", "is required for a run of kind 'skill'")
        if skill_id and not skill_version:
            raise ContractError(
                f"{path}.skill_version",
                "is required alongside skill_id; a run that cannot name the version it "
                "ran cannot be reproduced or audited",
            )
        started = timestamp(data, "started_at", path)
        ended = timestamp(data, "ended_at", path)
        if ended and not started:
            raise ContractError(f"{path}.started_at", "is missing while ended_at is set")
        if started and ended and ended < started:
            raise ContractError(f"{path}.ended_at", "is before started_at", got=ended)
        if status in TERMINAL and not ended:
            raise ContractError(f"{path}.ended_at", f"is required once status is '{status}'")
        if status not in TERMINAL and ended:
            raise ContractError(f"{path}.ended_at", f"is set while status is still '{status}'")
        if "outcome" in data:
            expected = OUTCOME_OF.get(status)
            if data["outcome"] != expected:
                raise ContractError(
                    f"{path}.outcome",
                    f"contradicts status '{status}', which means {expected!r}",
                    got=data["outcome"],
                )
        return cls(
            id=text(data, "id", path, max_len=64),
            kind=kind,
            status=status,
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            session_id=text(data, "session_id", path, required=False, max_len=128),
            skill_id=skill_id,
            skill_version=skill_version,
            parent_run_id=text(data, "parent_run_id", path, required=False, max_len=64),
            execution_fingerprint=text(data, "execution_fingerprint", path, required=False, max_len=64),
            approval_id=text(data, "approval_id", path, required=False, max_len=64),
            label=text(data, "label", path, required=False, max_len=200),
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            started_at=started,
            ended_at=ended,
            cost_units=whole(data, "cost_units", path, minimum=0),
            reason=text(data, "reason", path, required=False, max_len=500),
            artifact_ids=text_list(data, "artifact_ids", path, max_items=1000, max_len=64),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    @property
    def outcome(self) -> Optional[str]:
        """`None` for a run still going *and* for `interrupted` — the caller has
        to tell those apart by status, because collapsing "not finished" and
        "finished, unknowable" into one word is how a summary starts lying."""
        return OUTCOME_OF.get(self.status)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def advanced_to(self, status: str, *, reason: str = "", at: Optional[str] = None) -> "Run":
        """Return the next run.  Raises on an illegal transition rather than
        recording it."""
        check_transition(self.status, status)
        stamp = at or now_iso()
        started = self.started_at
        if status == "running" and not started:
            started = stamp
        ended = self.ended_at
        if status in TERMINAL:
            ended = ended or stamp
            started = started or stamp
        return Run(
            id=self.id, kind=self.kind, status=status, owner=self.owner,
            project_id=self.project_id, session_id=self.session_id,
            skill_id=self.skill_id, skill_version=self.skill_version,
            parent_run_id=self.parent_run_id,
            execution_fingerprint=self.execution_fingerprint,
            approval_id=self.approval_id, label=self.label,
            created_at=self.created_at, started_at=started, ended_at=ended,
            cost_units=self.cost_units, reason=reason or self.reason,
            artifact_ids=self.artifact_ids, schema_version=self.schema_version,
        )

    def with_artifact(self, artifact_id: str) -> "Run":
        if artifact_id in self.artifact_ids:
            return self
        return Run(**{**self.to_dict_internal(), "artifact_ids": self.artifact_ids + (artifact_id,)})

    def to_dict_internal(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "owner": self.owner,
            "project_id": self.project_id, "session_id": self.session_id,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "parent_run_id": self.parent_run_id,
            "execution_fingerprint": self.execution_fingerprint,
            "approval_id": self.approval_id, "label": self.label,
            "created_at": self.created_at, "started_at": self.started_at,
            "ended_at": self.ended_at, "cost_units": self.cost_units,
            "reason": self.reason, "artifact_ids": self.artifact_ids,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_dict_internal()
        d["artifact_ids"] = list(self.artifact_ids)
        d["outcome"] = self.outcome
        return d
