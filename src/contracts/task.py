"""
contracts/task.py — the task-level record spec v2 organises everything else
under (§04, ARCH-01): its state machine, the plan it is executing, what it is
waiting on, and the event stream it reports through.

`TaskState` is one level above `contracts.run.Run`. A `Run` is one execution
attempt (a chat turn, a skill, a workflow node); a `TaskState` is the request
that may span several of them (`active_run_id` names the current one), with
its own broader machine — `draft → ready → running → waiting_* → … →
succeeded/partial/failed/cancelled` — because "the model is waiting on the
user" and "the process died mid-run" are different facts a coordinator needs
to branch on, and `Run.STATUSES` does not have room for either.

`QuestionRequest` and `ApprovalRequest` are the two shapes a task raises when
it stops to ask for something — a choice, or a yes — while it is in
`waiting_user`/`waiting_approval`. `ApprovalRequest` does NOT reuse
`contracts.approval.Approval`: that object approves a *skill plan*
(action/skill_id/backend/recipients/…, a card shown once before a run
starts); this one approves a *tool call already inside a running task*
(call_id/tool_name/payload_digest/resource_refs). The two answer different
questions and this module does not touch `approval.py` to force them
together — see the lot report for exactly what a bridge to the closest
existing authority, `src.tool_approvals.ToolApprovalStore`, would need.

`EventEnvelope` is the spec v2 wire shape for `contracts.event.Event`, built
as a wrapper with an explicit, incomplete `from_event()`/`to_event()` adapter
rather than pretended to be a superset: `Event.EVENT_NAMES` and this module's
`EVENT_ENVELOPE_TYPES` are two different vocabularies today (`EVENT_NAMES` has
no member equal to any of the 13 envelope types), and `Event` has no
`task_id`. Nothing here invents a mapping across that gap where one is not
already unambiguous — see the report for the two-line change to `event.py`
that would close it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, as_mapping, fingerprint, flag, one_of, reject_unknown,
    sha256_hex, text, text_list, timestamp, whole,
)
from .event import Event
from .tool import (
    SPEC_V2_SCHEMA_VERSION, as_list, nullable_ref_id, ref_id, ref_id_list, spec_version,
)

# ── the task state machine (§04) ────────────────────────────────────────────

PLAN_STEP_STATES = ("pending", "active", "done", "blocked", "skipped")
ACCEPTANCE_STATES = ("pending", "proven", "unmet", "not_checked", "not_applicable")

TASK_STATES = ("draft", "ready", "running", "waiting_user", "waiting_approval",
               "waiting_resource", "paused", "interrupted", "blocked", "verifying",
               "succeeded", "partial", "failed", "cancelled")

TASK_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})

#: What may follow what. Same shape as `run.TRANSITIONS`: anything absent is
#: a bug in the caller to catch now rather than a task that silently jumps
#: from `draft` straight to `succeeded`.
TASK_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "draft": ("ready", "cancelled"),
    "ready": ("running", "cancelled"),
    "running": ("waiting_user", "waiting_approval", "waiting_resource", "paused",
                "interrupted", "blocked", "verifying",
                "succeeded", "partial", "failed", "cancelled"),
    "waiting_user": ("running", "interrupted", "cancelled"),
    "waiting_approval": ("running", "interrupted", "cancelled", "failed"),
    "waiting_resource": ("running", "interrupted", "cancelled", "failed"),
    "paused": ("running", "interrupted", "cancelled"),
    "interrupted": ("running", "cancelled"),
    "blocked": ("running", "cancelled", "failed"),
    "verifying": ("running", "succeeded", "partial", "failed", "cancelled"),
    "succeeded": (),
    "partial": (),
    "failed": (),
    "cancelled": (),
}


def assert_transition(old: str, new: str, *, path: str = "task_state.state") -> None:
    """Raise unless `old → new` is a move the §04 task machine allows.

    Same shape as `run.check_transition`: an unrecognised state name is a bug
    in the caller, staying put (`old == new`) is always allowed so a
    coordinator resending the state it already had does not have to
    special-case that as a transition, and a terminal task says so instead of
    listing an empty "allowed from" set."""
    if old not in TASK_TRANSITIONS:
        raise ContractError(
            path, f"unknown current state; expected one of {list(TASK_STATES)}", got=old)
    if new not in TASK_STATES:
        raise ContractError(path, f"unknown state; expected one of {list(TASK_STATES)}", got=new)
    if new == old:
        return
    allowed = TASK_TRANSITIONS[old]
    if new not in allowed:
        detail = f"cannot go {old} → {new}"
        if old in TASK_TERMINAL:
            detail += " (a terminal task does not change its mind; open a new task)"
        else:
            detail += f"; allowed from {old}: {list(allowed)}"
        raise ContractError(path, detail)


# ── the plan a task is executing ────────────────────────────────────────────

@dataclass(frozen=True)
class PlanStep:
    id: str
    title: str
    state: str = "pending"
    depends_on: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()

    _KEYS = ("id", "title", "state", "depends_on", "evidence_refs")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "plan_step") -> "PlanStep":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=ref_id(data, "id", path),
            title=text(data, "title", path, max_len=512),
            state=one_of(data, "state", path, choices=PLAN_STEP_STATES),
            depends_on=ref_id_list(data, "depends_on", path, max_items=100),
            evidence_refs=ref_id_list(data, "evidence_refs", path, max_items=100),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "state": self.state,
                "depends_on": list(self.depends_on), "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str
    state: str = "pending"
    evidence_refs: Tuple[str, ...] = ()

    _KEYS = ("id", "description", "state", "evidence_refs")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "acceptance_criterion") -> "AcceptanceCriterion":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=ref_id(data, "id", path),
            description=text(data, "description", path, max_len=4000),
            state=one_of(data, "state", path, choices=ACCEPTANCE_STATES),
            evidence_refs=ref_id_list(data, "evidence_refs", path, max_items=100),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"id": self.id, "description": self.description, "state": self.state,
                "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class Decision:
    id: str
    summary: str
    source_ref: str

    _KEYS = ("id", "summary", "source_ref")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "decision") -> "Decision":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            id=ref_id(data, "id", path),
            summary=text(data, "summary", path, max_len=512),
            source_ref=text(data, "source_ref", path, max_len=512),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"id": self.id, "summary": self.summary, "source_ref": self.source_ref}


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int = 0
    used_tool_calls: int = 0
    max_output_tokens: int = 0
    remote_cost_limit: Optional[float] = None

    _KEYS = ("max_tool_calls", "used_tool_calls", "max_output_tokens", "remote_cost_limit")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "budget") -> "Budget":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        if "remote_cost_limit" not in data:
            raise ContractError(f"{path}.remote_cost_limit", "is required (use null for no limit)")
        raw_limit = data["remote_cost_limit"]
        if raw_limit is not None:
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)):
                raise ContractError(f"{path}.remote_cost_limit", "expected a number or null", got=raw_limit)
            if raw_limit < 0:
                raise ContractError(f"{path}.remote_cost_limit", "must be >= 0", got=raw_limit)
        return cls(
            max_tool_calls=whole(data, "max_tool_calls", path, required=True, minimum=0),
            used_tool_calls=whole(data, "used_tool_calls", path, required=True, minimum=0),
            max_output_tokens=whole(data, "max_output_tokens", path, required=True, minimum=0),
            remote_cost_limit=float(raw_limit) if raw_limit is not None else None,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"max_tool_calls": self.max_tool_calls, "used_tool_calls": self.used_tool_calls,
                "max_output_tokens": self.max_output_tokens,
                "remote_cost_limit": self.remote_cost_limit}


@dataclass(frozen=True)
class TaskState:
    """The whole of what a task is doing, in one snapshot: its plan, what it
    has decided, what would prove it done, and its budget. `revision` is the
    optimistic-concurrency counter `PlanRevision.advance()` enforces below."""

    task_id: str
    owner_id: str
    project_id: Optional[str]
    request_ref: str
    revision: int
    state: str
    constraints: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[AcceptanceCriterion, ...] = ()
    plan_steps: Tuple[PlanStep, ...] = ()
    decisions: Tuple[Decision, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    active_run_id: Optional[str] = None
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "task_id", "owner_id", "project_id", "request_ref",
              "revision", "state", "constraints", "acceptance_criteria", "plan_steps",
              "decisions", "evidence_refs", "budget", "active_run_id")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "task_state") -> "TaskState":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        state = one_of(data, "state", path, choices=TASK_STATES)
        acceptance = tuple(
            AcceptanceCriterion.from_mapping(c, f"{path}.acceptance_criteria[{i}]")
            for i, c in enumerate(as_list(data, "acceptance_criteria", path)))
        steps = tuple(
            PlanStep.from_mapping(s, f"{path}.plan_steps[{i}]")
            for i, s in enumerate(as_list(data, "plan_steps", path)))
        decisions = tuple(
            Decision.from_mapping(d, f"{path}.decisions[{i}]")
            for i, d in enumerate(as_list(data, "decisions", path)))
        return cls(
            schema_version=version,
            task_id=ref_id(data, "task_id", path),
            owner_id=ref_id(data, "owner_id", path),
            project_id=nullable_ref_id(data, "project_id", path),
            request_ref=text(data, "request_ref", path, max_len=512),
            revision=whole(data, "revision", path, required=True, minimum=1),
            state=state,
            constraints=text_list(data, "constraints", path, max_items=500, max_len=512, unique=False),
            acceptance_criteria=acceptance,
            plan_steps=steps,
            decisions=decisions,
            evidence_refs=ref_id_list(data, "evidence_refs", path, max_items=1000),
            budget=Budget.from_mapping(data.get("budget"), f"{path}.budget"),
            active_run_id=nullable_ref_id(data, "active_run_id", path),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "task_id": self.task_id,
            "owner_id": self.owner_id, "project_id": self.project_id,
            "request_ref": self.request_ref, "revision": self.revision, "state": self.state,
            "constraints": list(self.constraints),
            "acceptance_criteria": [c.to_mapping() for c in self.acceptance_criteria],
            "plan_steps": [s.to_mapping() for s in self.plan_steps],
            "decisions": [d.to_mapping() for d in self.decisions],
            "evidence_refs": list(self.evidence_refs),
            "budget": self.budget.to_mapping(), "active_run_id": self.active_run_id,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


@dataclass(frozen=True)
class PlanRevision:
    """One numbered `TaskState` snapshot, paired with the two checks a
    coordinator applying an incoming update needs before it trusts it: the
    state transition is one §04 allows, AND the revision counter moved
    strictly forward. Neither check alone is enough — a replayed old
    snapshot can carry a transition that is legal from *some* earlier state,
    and a higher revision number proves nothing about the state it claims."""

    task: TaskState

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "task_state") -> "PlanRevision":
        return cls(TaskState.from_mapping(raw, path))

    def advance(self, raw: Any, path: str = "task_state") -> "PlanRevision":
        nxt = TaskState.from_mapping(raw, path)
        if nxt.task_id != self.task.task_id:
            raise ContractError(
                f"{path}.task_id",
                f"does not match the task this revision belongs to ({self.task.task_id!r})",
                got=nxt.task_id)
        if nxt.revision <= self.task.revision:
            raise ContractError(
                f"{path}.revision",
                f"must be greater than the current revision ({self.task.revision})",
                got=nxt.revision)
        assert_transition(self.task.state, nxt.state, path=f"{path}.state")
        return PlanRevision(nxt)


# ── question_request ─────────────────────────────────────────────────────────

QUESTION_EXPIRY_POLICIES = ("wait", "block", "use_preapproved_non_sensitive_default")


@dataclass(frozen=True)
class QuestionOption:
    id: str
    label: str
    description: str = ""

    _KEYS = ("id", "label", "description")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "option") -> "QuestionOption":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        if "description" not in data:
            raise ContractError(
                f"{path}.description", "is required (use an empty string if there is none)")
        return cls(
            id=ref_id(data, "id", path),
            label=text(data, "label", path, max_len=200),
            description=text(data, "description", path, required=True,
                              allow_blank=True, max_len=1000),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description}


@dataclass(frozen=True)
class QuestionRequest:
    """A task stopping to ask something while `waiting_user`. `options` is
    bounded 2-6 the same way the JSON Schema bounds it — a "question" with
    one option is an instruction wearing a question's clothes, and the model
    should have just said so."""

    question_id: str
    task_id: str
    run_id: str
    revision: int
    question: str
    options: Tuple[QuestionOption, ...]
    multi: bool = False
    allow_free_text: bool = False
    recommended_option_id: Optional[str] = None
    expiry_policy: str = "wait"
    expires_at: Optional[str] = None
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "question_id", "task_id", "run_id", "revision",
              "question", "options", "multi", "allow_free_text",
              "recommended_option_id", "expiry_policy", "expires_at")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "question_request") -> "QuestionRequest":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)

        raw_options = data.get("options")
        if not isinstance(raw_options, (list, tuple)):
            raise ContractError(f"{path}.options", "expected a list", got=raw_options)
        if not (2 <= len(raw_options) <= 6):
            raise ContractError(
                f"{path}.options",
                f"must offer between 2 and 6 options, got {len(raw_options)}", got=raw_options)
        options = tuple(QuestionOption.from_mapping(o, f"{path}.options[{i}]")
                        for i, o in enumerate(raw_options))
        option_ids = {o.id for o in options}
        if len(option_ids) != len(options):
            raise ContractError(f"{path}.options", "option ids must be unique", got=raw_options)

        if "multi" not in data:
            raise ContractError(f"{path}.multi", "is required")
        multi = flag(data, "multi", path, default=False)
        if "allow_free_text" not in data:
            raise ContractError(f"{path}.allow_free_text", "is required")
        allow_free_text = flag(data, "allow_free_text", path, default=False)

        recommended = nullable_ref_id(data, "recommended_option_id", path)
        if recommended is not None and recommended not in option_ids:
            raise ContractError(
                f"{path}.recommended_option_id",
                "must be the id of one of the options offered", got=recommended)

        return cls(
            schema_version=version,
            question_id=ref_id(data, "question_id", path),
            task_id=ref_id(data, "task_id", path),
            run_id=ref_id(data, "run_id", path),
            revision=whole(data, "revision", path, required=True, minimum=1),
            question=text(data, "question", path, max_len=4000),
            options=options,
            multi=multi,
            allow_free_text=allow_free_text,
            recommended_option_id=recommended,
            expiry_policy=one_of(data, "expiry_policy", path, choices=QUESTION_EXPIRY_POLICIES),
            expires_at=timestamp(data, "expires_at", path, required=False),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "question_id": self.question_id,
            "task_id": self.task_id, "run_id": self.run_id, "revision": self.revision,
            "question": self.question, "options": [o.to_mapping() for o in self.options],
            "multi": self.multi, "allow_free_text": self.allow_free_text,
            "recommended_option_id": self.recommended_option_id,
            "expiry_policy": self.expiry_policy, "expires_at": self.expires_at,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


# ── approval_request ─────────────────────────────────────────────────────────

APPROVAL_REQUEST_STATES = ("pending", "approved", "denied", "expired", "invalidated")
APPROVAL_DECISION_SCOPES = ("once", "task_bounded", "policy_bounded")


@dataclass(frozen=True)
class ApprovalRequest:
    """The spec v2 shape for one tool call gated on a yes — bound to exactly
    what was shown the same way `contracts.approval.Approval` binds a skill
    plan to exactly what a human read (`payload_digest` is that binding,
    checked with `base.sha256_hex`'s own 64-hex-char rule).

    Deliberately NOT an adapter onto `contracts.approval.Approval`: see this
    module's docstring for why the two do not coincide, and the lot report
    for what bridging this to Faustus's live per-call gate
    (`src.tool_approvals.ToolApprovalStore`) would need."""

    request_id: str
    task_id: str
    run_id: str
    revision: int
    call_id: str
    tool_name: str
    payload_digest: str
    resource_refs: Tuple[str, ...]
    effect_preview: str
    expires_at: str
    state: str = "pending"
    decision_scope: str = "once"
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "request_id", "task_id", "run_id", "revision",
              "call_id", "tool_name", "payload_digest", "resource_refs",
              "effect_preview", "expires_at", "state", "decision_scope")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "approval_request") -> "ApprovalRequest":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        return cls(
            schema_version=version,
            request_id=ref_id(data, "request_id", path),
            task_id=ref_id(data, "task_id", path),
            run_id=ref_id(data, "run_id", path),
            revision=whole(data, "revision", path, required=True, minimum=1),
            call_id=ref_id(data, "call_id", path),
            tool_name=text(data, "tool_name", path, max_len=512),
            payload_digest=sha256_hex(data, "payload_digest", path, required=True),
            resource_refs=text_list(data, "resource_refs", path,
                                     max_items=100, max_len=512, unique=False),
            effect_preview=text(data, "effect_preview", path, max_len=8000),
            expires_at=timestamp(data, "expires_at", path, required=True),
            state=one_of(data, "state", path, choices=APPROVAL_REQUEST_STATES,
                        required=False, default="pending"),
            decision_scope=one_of(data, "decision_scope", path, choices=APPROVAL_DECISION_SCOPES,
                                  required=False, default="once"),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "request_id": self.request_id,
            "task_id": self.task_id, "run_id": self.run_id, "revision": self.revision,
            "call_id": self.call_id, "tool_name": self.tool_name,
            "payload_digest": self.payload_digest, "resource_refs": list(self.resource_refs),
            "effect_preview": self.effect_preview, "expires_at": self.expires_at,
            "state": self.state, "decision_scope": self.decision_scope,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


# ── event_envelope ───────────────────────────────────────────────────────────

EVENT_ENVELOPE_TYPES = (
    "task.state_changed", "run.phase", "model.delta", "tool.started", "tool.result",
    "question.opened", "question.resolved", "approval.opened", "approval.resolved",
    "artifact.ready", "checkpoint.created", "warning", "run.terminal",
)
EVENT_ENVELOPE_VISIBILITY = ("user", "diagnostic")

#: Best-effort correspondence from today's `contracts.event.EVENT_NAMES` to
#: the spec v2 envelope's smaller `type` vocabulary — only the pairs where
#: the two names mean the same thing without guessing. This is NOT full
#: coverage: `EVENT_NAMES` has no member textually equal to any of the 13
#: `EVENT_ENVELOPE_TYPES`, so `EventEnvelope.from_event()` needs either a hit
#: here or an explicit `type_override` for anything not listed, rather than
#: this module picking a plausible bucket on its own (base.py rule 3:
#: nothing is coerced across a type boundary, and a guessed event type is
#: exactly that).
_ENVELOPE_TYPE_OF_EVENT_NAME: Dict[str, str] = {
    "run.created": "run.phase",
    "backend.started": "run.phase",
    "backend.finished": "run.phase",
    "run.completed": "run.terminal",
    "run.failed": "run.terminal",
    "run.cancelled": "run.terminal",
    "run.interrupted": "run.terminal",
    "approval.requested": "approval.opened",
    "approval.granted": "approval.resolved",
    "approval.denied": "approval.resolved",
    "approval.expired": "approval.resolved",
    "artifact.created": "artifact.ready",
}

#: The reverse, but only where exactly one Faustus event name maps to that
#: envelope type — `run.phase`, `run.terminal` and `approval.resolved` are
#: many-to-one above and have no honest default; `to_event()` requires
#: `name=` explicitly for those rather than guessing which of the several
#: candidates was meant.
_EVENT_NAME_OF_ENVELOPE_TYPE_DEFAULT: Dict[str, str] = {"artifact.ready": "artifact.created"}


@dataclass(frozen=True)
class EventEnvelope:
    """The spec v2 wire shape for one entry in a task's event stream —
    `contracts.event.Event` plus the `sequence`/`stream_id`/`visibility` a
    replayable, multi-consumer stream needs and `Event` does not carry today
    (`schema_version` and `seq`/`sequence` it already has, under a different
    name for the latter). `from_event()`/`to_event()` are the adapter; see
    the module docstring and the lot report for the gap they cannot close
    without touching `event.py`."""

    event_id: str
    stream_id: str
    sequence: int
    task_id: str
    run_id: str
    type: str
    timestamp: str
    visibility: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "event_id", "stream_id", "sequence", "task_id",
              "run_id", "type", "timestamp", "visibility", "payload")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "event_envelope") -> "EventEnvelope":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        if "payload" not in data:
            raise ContractError(f"{path}.payload", "is required")
        payload = as_mapping(data["payload"], f"{path}.payload")
        return cls(
            schema_version=version,
            event_id=ref_id(data, "event_id", path),
            stream_id=ref_id(data, "stream_id", path),
            sequence=whole(data, "sequence", path, required=True, minimum=1),
            task_id=ref_id(data, "task_id", path),
            run_id=ref_id(data, "run_id", path),
            type=one_of(data, "type", path, choices=EVENT_ENVELOPE_TYPES),
            timestamp=timestamp(data, "timestamp", path, required=True),
            visibility=one_of(data, "visibility", path, choices=EVENT_ENVELOPE_VISIBILITY),
            payload=dict(payload),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "event_id": self.event_id,
            "stream_id": self.stream_id, "sequence": self.sequence,
            "task_id": self.task_id, "run_id": self.run_id, "type": self.type,
            "timestamp": self.timestamp, "visibility": self.visibility,
            "payload": dict(self.payload),
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])

    @classmethod
    def from_event(cls, event: Event, *, event_id: str, stream_id: str, sequence: int,
                   task_id: str, run_id: Optional[str] = None, visibility: str = "user",
                   type_override: Optional[str] = None) -> "EventEnvelope":
        """Wrap a live `Event` for a v2 stream consumer. Raises rather than
        guessing when `event.name` has no declared envelope type — pass
        `type_override` when the caller knows which of the 13 types this
        particular event means."""
        envelope_type = type_override or _ENVELOPE_TYPE_OF_EVENT_NAME.get(event.name)
        if not envelope_type:
            raise ContractError(
                "type",
                f"event {event.name!r} has no declared spec v2 envelope type; pass "
                "type_override= or extend _ENVELOPE_TYPE_OF_EVENT_NAME once it is clear "
                "which of the 13 types it means",
                got=event.name,
            )
        return cls.from_mapping({
            "schema_version": SPEC_V2_SCHEMA_VERSION,
            "event_id": event_id, "stream_id": stream_id, "sequence": sequence,
            "task_id": task_id, "run_id": run_id or event.run_id,
            "type": envelope_type, "timestamp": event.at, "visibility": visibility,
            "payload": dict(event.data),
        })

    def to_event(self, *, name: Optional[str] = None, owner: str = "", project_id: str = "",
                 session_id: str = "", seq: Optional[int] = None,
                 secrets: Tuple[str, ...] = ()) -> Event:
        """Unwrap back into an `Event` for the existing SSE/hook machinery.
        `task_id` is dropped in this direction — `Event` has nowhere to put
        it today (see the module docstring)."""
        resolved = name or _EVENT_NAME_OF_ENVELOPE_TYPE_DEFAULT.get(self.type)
        if not resolved:
            raise ContractError(
                "type",
                f"envelope type {self.type!r} maps back to more than one Faustus event "
                "name; pass name= explicitly",
                got=self.type,
            )
        return Event.parse({
            "name": resolved, "run_id": self.run_id,
            "seq": self.sequence if seq is None else seq, "at": self.timestamp,
            "owner": owner, "project_id": project_id, "session_id": session_id,
            "data": dict(self.payload),
        }).redact(secrets)
