"""
contracts/tool.py — a tool call in the spec v2 vocabulary (§34.5, ARCH-01/TOOL-01).

Faustus already has three fragments of "what is a tool" that do not know
about each other: `src/tool_capabilities.py` (effects and result integrity,
used for the approval gate), `src/tool_schemas.py` (native tool schemas for
the model), and per-tool ad hoc result dicts in `src/agent_tools/*.py` with no
`ToolResult` class at all (0 occurrences in the repo before this module).
`ToolDescriptor`/`ToolInvocation`/`ToolResult`/`EvidenceRef` give the spec's
four call-shaped schemas a typed home, following `contracts/base.py`'s rules
exactly the way `contracts/run.py` and `contracts/changeset.py` already do:
an unknown key is an error, nothing is coerced across a type boundary, and a
rejection names the field.

Two design notes that do not show up in the field lists:

`ToolResult.status` is the seven-valued outcome from spec §34.5 —
`succeeded`, `failed`, `cancelled`, `conflict`, `denied`, `outcome_unknown`,
`partial` — a finer instrument than `src/tool_outcome.py`'s four (which stays
exactly as it is; it answers "does this count against a scorecard", not
"what shape was this specific call's result"). `outcome_unknown` is the one
that matters most: it is `Run`'s `interrupted` and `prove`'s `unproved`
stated at the level of one tool call — the work may have happened and
nothing here can prove it either way — and it is the one status this module
refuses to leave unexplained: it must carry `uncertainty`.

`ErrorInfo` (from `.errors`) is reused as-is for `ToolResult.error` rather
than redefined here, with `require_known_category` left off — a tool's own
error vocabulary (`BASE_REVISION_MISMATCH` in the spec's own example) is
real data, not a violation of the closed OBS-03 taxonomy that categorises
Faustus's own exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, as_mapping, fingerprint, one_of, reject_unknown, text,
    text_list, timestamp, whole,
)
from .errors import ErrorInfo

#: The literal `schema_version` every spec v2 payload carries. A JSON Schema
#: `const`, not a compatibility field — refused outright rather than read as
#: "close enough" when it is missing or any other value (rule 3 of base.py:
#: nothing is coerced across a type boundary, and "1" or 1.0 are guesses
#: about what someone meant by this exact string).
SPEC_V2_SCHEMA_VERSION = "1.0"

#: The generic opaque-id shape the spec's schemas share for `call_id`,
#: `attempt_id`, `task_id`, `run_id`, `evidence_id`, `question_id`,
#: `request_id`, `event_id`, `stream_id`, and the id fields of plan steps,
#: acceptance criteria and decisions: 1-128 chars of `[A-Za-z0-9_.:-]`.
#: Deliberately not `base.ident` — that helper is lowercase-only and would
#: reject `task_demo`'s own capital-free but otherwise ordinary siblings the
#: moment a caller used a capital letter, which the spec's pattern allows.
_REF_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

#: Same shape as a SHA-256 hex digest (`base.sha256_hex`), duplicated here as
#: a private constant because `content_sha256` is nullable in a way
#: `base.sha256_hex` (required-string-or-default) cannot express: the schema
#: requires the KEY to be present but allows the VALUE to be `null`.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def spec_version(data: Mapping[str, Any], path: str) -> str:
    """Validate and return the literal `schema_version` const."""
    if "schema_version" not in data:
        raise ContractError(f"{path}.schema_version", "is required")
    raw = data["schema_version"]
    if raw != SPEC_V2_SCHEMA_VERSION:
        raise ContractError(
            f"{path}.schema_version",
            f"must be exactly {SPEC_V2_SCHEMA_VERSION!r}", got=raw)
    return raw


def ref_id(data: Mapping[str, Any], key: str, path: str, *,
           required: bool = True, max_len: int = 128, default: str = "") -> str:
    """An opaque id: `[A-Za-z0-9_.:-]{1,max_len}`. It becomes a filter key, a
    dict key and a URL segment three modules over, so the same narrowness
    `base.ident` applies to skill/backend ids applies here — just with the
    wider, case-sensitive charset the spec's own schemas declare."""
    value = text(data, key, path, required=required, default=default, max_len=max_len)
    if not value:
        return value
    if not _REF_ID_RE.fullmatch(value):
        raise ContractError(
            f"{path}.{key}", "must contain only letters, digits, and _ . : -", got=value)
    return value


def nullable_ref_id(data: Mapping[str, Any], key: str, path: str, *,
                     max_len: int = 128) -> Optional[str]:
    """The spec's `anyOf [ref-id-pattern, null]` fields: the KEY must be
    present (there is no "it just wasn't sent" reading of `active_run_id`),
    but the value itself may honestly be nothing yet."""
    if key not in data:
        raise ContractError(f"{path}.{key}", "is required (use null if there is none)")
    raw = data[key]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ContractError(f"{path}.{key}", "expected a string or null", got=raw)
    value = raw.strip()
    if not value or len(value) > max_len:
        raise ContractError(
            f"{path}.{key}", f"must be 1-{max_len} chars when not null", got=raw)
    if not _REF_ID_RE.fullmatch(value):
        raise ContractError(
            f"{path}.{key}", "must contain only letters, digits, and _ . : -", got=value)
    return value


def ref_id_list(data: Mapping[str, Any], key: str, path: str, *,
                 max_items: int = 1000, max_len: int = 128) -> Tuple[str, ...]:
    """A list of opaque ids (`evidence_refs`, `depends_on`, `derived_from`…).
    Order is kept and duplicates are NOT rejected — the spec's own schemas
    never say a list like this must be a set, and enforcing one here would
    refuse payloads the JSON Schema itself accepts."""
    values = text_list(data, key, path, max_items=max_items, max_len=max_len, unique=False)
    for i, item in enumerate(values):
        if not _REF_ID_RE.fullmatch(item):
            raise ContractError(
                f"{path}.{key}[{i}]", "must contain only letters, digits, and _ . : -", got=item)
    return values


def _nullable_sha256(data: Mapping[str, Any], key: str, path: str) -> Optional[str]:
    if key not in data:
        raise ContractError(f"{path}.{key}", "is required (use null if there is none yet)")
    raw = data[key]
    if raw is None:
        return None
    if not isinstance(raw, str) or not _HEX64_RE.fullmatch(raw.lower()):
        raise ContractError(
            f"{path}.{key}", "must be 64 lowercase hex chars (a SHA-256) or null", got=raw)
    return raw.lower()


def _require_object(data: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    if key not in data:
        raise ContractError(f"{path}.{key}", "is required")
    return as_mapping(data[key], f"{path}.{key}")


def as_list(data: Mapping[str, Any], key: str, path: str) -> Tuple[Any, ...]:
    """A required array-of-objects field, defended against the case a string
    would otherwise pass silently through: `for x in "abc"` iterates
    characters, and a caller who sent a string where a list belonged deserves
    "expected a list", not three single-character objects failing for an
    unrelated reason three frames later. Public (not `_as_list`) because
    `contracts.task` reuses it for the same reason on its own object lists."""
    raw = data.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ContractError(f"{path}.{key}", "expected a list", got=raw)
    return tuple(raw)


# ── tool_descriptor ──────────────────────────────────────────────────────────

EFFECT_CLASSES = ("read", "write", "execute", "external", "sensitive", "control")
CANCELLATION_MODES = ("cooperative", "best_effort", "not_supported")
IDEMPOTENCY_MODES = ("safe_read", "local_transaction", "provider_key",
                      "reconcile_before_retry", "not_supported")

#: `fs.apply_patch`, `mail.send` — lowercase, dotted, at least one segment
#: past the first. Narrower than `base.ident` (which allows `-`/`_` at the
#: top and no required dot) because this is the spec's own pattern, not
#: Faustus's skill-id one, and the two are not interchangeable.
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    initial_delay_ms: int = 0
    retry_on: Tuple[str, ...] = ()

    _KEYS = ("max_attempts", "initial_delay_ms", "retry_on")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "retry_policy") -> "RetryPolicy":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            max_attempts=whole(data, "max_attempts", path, required=True, minimum=1, maximum=10),
            initial_delay_ms=whole(data, "initial_delay_ms", path, required=True,
                                    minimum=0, maximum=600000),
            retry_on=text_list(data, "retry_on", path, max_items=32, max_len=512, unique=False),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"max_attempts": self.max_attempts, "initial_delay_ms": self.initial_delay_ms,
                "retry_on": list(self.retry_on)}


@dataclass(frozen=True)
class ToolDescriptor:
    """What a tool is, published once and read by every caller that wants to
    invoke it — the catalogue entry `TOOL-01` is missing today (fragmented
    across `tool_capabilities.py`/`tool_schemas.py`/`agent_tools/__init__.py`,
    none of them versioned)."""

    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    effect_class: str = "read"
    required_scopes: Tuple[str, ...] = ()
    timeout_ms: int = 1
    cancellation: str = "not_supported"
    idempotency: str = "not_supported"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    max_output_bytes: int = 1
    executor: str = ""
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "name", "version", "description", "input_schema",
              "output_schema", "effect_class", "required_scopes", "timeout_ms",
              "cancellation", "idempotency", "retry_policy", "max_output_bytes", "executor")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "tool_descriptor") -> "ToolDescriptor":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        name = text(data, "name", path, max_len=512)
        if not _TOOL_NAME_RE.fullmatch(name):
            raise ContractError(
                f"{path}.name",
                "must be a lowercase dotted tool name like 'fs.apply_patch' "
                "(letters, digits, underscores; at least one dot)", got=name)
        input_schema = _require_object(data, "input_schema", path)
        output_schema = _require_object(data, "output_schema", path)
        return cls(
            schema_version=version,
            name=name,
            version=text(data, "version", path, max_len=512),
            description=text(data, "description", path, max_len=4000),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            effect_class=one_of(data, "effect_class", path, choices=EFFECT_CLASSES),
            required_scopes=text_list(data, "required_scopes", path,
                                       max_items=32, max_len=512, unique=False),
            timeout_ms=whole(data, "timeout_ms", path, required=True, minimum=1, maximum=86400000),
            cancellation=one_of(data, "cancellation", path, choices=CANCELLATION_MODES),
            idempotency=one_of(data, "idempotency", path, choices=IDEMPOTENCY_MODES),
            retry_policy=RetryPolicy.from_mapping(data.get("retry_policy"), f"{path}.retry_policy"),
            max_output_bytes=whole(data, "max_output_bytes", path, required=True,
                                    minimum=1, maximum=10000000),
            executor=text(data, "executor", path, max_len=512),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "name": self.name, "version": self.version,
            "description": self.description, "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema), "effect_class": self.effect_class,
            "required_scopes": list(self.required_scopes), "timeout_ms": self.timeout_ms,
            "cancellation": self.cancellation, "idempotency": self.idempotency,
            "retry_policy": self.retry_policy.to_mapping(),
            "max_output_bytes": self.max_output_bytes, "executor": self.executor,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


# ── tool_invocation ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolRef:
    name: str
    version: str

    _KEYS = ("name", "version")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "tool") -> "ToolRef":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(name=text(data, "name", path, max_len=512),
                    version=text(data, "version", path, max_len=512))

    def to_mapping(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class ToolInvocation:
    """One attempt at one call. `attempt_id` is separate from `call_id` on
    purpose — a retried call keeps its identity across attempts, which is
    what lets `idempotency_key` do its job."""

    call_id: str
    attempt_id: str
    task_id: str
    run_id: str
    tool: ToolRef
    arguments: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    authorization_ref: Optional[str] = None
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "call_id", "attempt_id", "task_id", "run_id",
              "tool", "arguments", "idempotency_key", "authorization_ref")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "tool_invocation") -> "ToolInvocation":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        arguments = _require_object(data, "arguments", path)
        return cls(
            schema_version=version,
            call_id=ref_id(data, "call_id", path),
            attempt_id=ref_id(data, "attempt_id", path),
            task_id=ref_id(data, "task_id", path),
            run_id=ref_id(data, "run_id", path),
            tool=ToolRef.from_mapping(data.get("tool"), f"{path}.tool"),
            arguments=dict(arguments),
            idempotency_key=text(data, "idempotency_key", path, max_len=512),
            authorization_ref=nullable_ref_id(data, "authorization_ref", path),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "call_id": self.call_id,
            "attempt_id": self.attempt_id, "task_id": self.task_id, "run_id": self.run_id,
            "tool": self.tool.to_mapping(), "arguments": dict(self.arguments),
            "idempotency_key": self.idempotency_key, "authorization_ref": self.authorization_ref,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


# ── tool_result ──────────────────────────────────────────────────────────────

#: The seven-valued result of one call (spec §34.5). Finer than
#: `tool_outcome.Outcome` (four values, for a failure tally) — this is the
#: shape of the result itself, not a verdict about whether it counts against
#: a scorecard.
TOOL_RESULT_STATUSES = ("succeeded", "failed", "cancelled", "conflict", "denied",
                         "outcome_unknown", "partial")
EFFECT_STATES = ("confirmed", "partial", "unknown")

#: Statuses that a `ToolResult` cannot report without also carrying an
#: `error` — the same rule `ExecutionResult` already applies to `refused`
#: (`execution.py`): a refusal or a failure nobody can explain reads as a
#: broken run.
_STATUSES_REQUIRING_ERROR = ("failed", "conflict", "denied")


@dataclass(frozen=True)
class ToolEffect:
    resource_ref: str
    operation: str
    state: str

    _KEYS = ("resource_ref", "operation", "state")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "effect") -> "ToolEffect":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            resource_ref=text(data, "resource_ref", path, max_len=512),
            operation=text(data, "operation", path, max_len=512),
            state=one_of(data, "state", path, choices=EFFECT_STATES),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {"resource_ref": self.resource_ref, "operation": self.operation, "state": self.state}


@dataclass(frozen=True)
class ToolUncertainty:
    reason: str
    reconcile_action: str

    _KEYS = ("reason", "reconcile_action")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "uncertainty") -> "ToolUncertainty":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(reason=text(data, "reason", path, max_len=512),
                    reconcile_action=text(data, "reconcile_action", path, max_len=512))

    def to_mapping(self) -> Dict[str, Any]:
        return {"reason": self.reason, "reconcile_action": self.reconcile_action}


@dataclass(frozen=True)
class ToolResult:
    """What came back from one attempt. `outcome_unknown` is the status that
    exists so a caller never has to guess: it is refused unless it carries
    `uncertainty` (reason + what would reconcile it), the same way `succeeded`
    is refused if it carries an `error` — a success with an attached error is
    a contradiction, not extra detail."""

    call_id: str
    attempt_id: str
    status: str
    output: Any = None
    evidence_refs: Tuple[str, ...] = ()
    effects: Tuple[ToolEffect, ...] = ()
    error: Optional[ErrorInfo] = None
    uncertainty: Optional[ToolUncertainty] = None
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "call_id", "attempt_id", "status", "output",
              "evidence_refs", "effects", "error", "uncertainty")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "tool_result") -> "ToolResult":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        status = one_of(data, "status", path, choices=TOOL_RESULT_STATUSES)

        if "output" not in data:
            raise ContractError(f"{path}.output", "is required (use null if there is none)")
        if "error" not in data:
            raise ContractError(f"{path}.error", "is required (use null if there is none)")
        if "uncertainty" not in data:
            raise ContractError(f"{path}.uncertainty", "is required (use null if there is none)")

        error_raw = data["error"]
        error = (ErrorInfo.from_mapping(error_raw, f"{path}.error")
                 if error_raw is not None else None)
        uncertainty_raw = data["uncertainty"]
        uncertainty = (ToolUncertainty.from_mapping(uncertainty_raw, f"{path}.uncertainty")
                       if uncertainty_raw is not None else None)

        if status == "succeeded":
            if error is not None:
                raise ContractError(
                    f"{path}.error",
                    "must be null when status is 'succeeded' — a success with an "
                    "attached error is a contradiction, not extra detail", got=error_raw)
            if uncertainty is not None:
                raise ContractError(
                    f"{path}.uncertainty",
                    "must be null when status is 'succeeded'", got=uncertainty_raw)
        if status == "outcome_unknown" and uncertainty is None:
            raise ContractError(
                f"{path}.uncertainty",
                "is required when status is 'outcome_unknown' — an unknown outcome has "
                "to say why it could not be confirmed and what would reconcile it")
        if status in _STATUSES_REQUIRING_ERROR and error is None:
            raise ContractError(f"{path}.error", f"is required when status is {status!r}")

        effects = tuple(ToolEffect.from_mapping(e, f"{path}.effects[{i}]")
                        for i, e in enumerate(as_list(data, "effects", path)))

        return cls(
            schema_version=version,
            call_id=ref_id(data, "call_id", path),
            attempt_id=ref_id(data, "attempt_id", path),
            status=status,
            output=data["output"],
            evidence_refs=ref_id_list(data, "evidence_refs", path, max_items=1000),
            effects=effects,
            error=error,
            uncertainty=uncertainty,
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "call_id": self.call_id,
            "attempt_id": self.attempt_id, "status": self.status, "output": self.output,
            "evidence_refs": list(self.evidence_refs),
            "effects": [e.to_mapping() for e in self.effects],
            "error": self.error.to_mapping() if self.error else None,
            "uncertainty": self.uncertainty.to_mapping() if self.uncertainty else None,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])


# ── evidence_ref ─────────────────────────────────────────────────────────────

EVIDENCE_SOURCE_TYPES = (
    "file", "web", "tool", "artifact", "test", "receipt", "memory", "media",
    # L18/CTX: evidence that lives only in the turn's own transcript, not a
    # tool call's output — src/context_compactor.py::compact_with_integrity
    # points at a folded conversation span with this.
    "conversation",
)
EVIDENCE_LOCATOR_KINDS = ("lines", "page", "cells", "time", "byte_range", "whole")
EVIDENCE_RETENTION = ("ephemeral", "task", "project", "pinned")


@dataclass(frozen=True)
class EvidenceLocator:
    kind: str
    value: str

    _KEYS = ("kind", "value")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "locator") -> "EvidenceLocator":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(kind=one_of(data, "kind", path, choices=EVIDENCE_LOCATOR_KINDS),
                    value=text(data, "value", path, max_len=512))

    def to_mapping(self) -> Dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer at what would convince somebody a claim is true — the same
    idea `changeset.Verification`/`prove` already act on, given the typed
    shape `ART-01`/`CALL-05` need to attach it to a `ToolResult` or a
    `TaskState` acceptance criterion by id instead of inline text."""

    evidence_id: str
    owner_id: str
    project_id: Optional[str]
    source_type: str
    source_ref: str
    source_revision: str
    content_sha256: Optional[str]
    captured_at: str
    locator: EvidenceLocator
    derived_from: Tuple[str, ...] = ()
    retention: str = "ephemeral"
    schema_version: str = SPEC_V2_SCHEMA_VERSION

    _KEYS = ("schema_version", "evidence_id", "owner_id", "project_id", "source_type",
              "source_ref", "source_revision", "content_sha256", "captured_at",
              "locator", "derived_from", "retention")

    @classmethod
    def from_mapping(cls, raw: Any, path: str = "evidence_ref") -> "EvidenceRef":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        version = spec_version(data, path)
        return cls(
            schema_version=version,
            evidence_id=ref_id(data, "evidence_id", path),
            owner_id=ref_id(data, "owner_id", path),
            project_id=nullable_ref_id(data, "project_id", path),
            source_type=one_of(data, "source_type", path, choices=EVIDENCE_SOURCE_TYPES),
            source_ref=text(data, "source_ref", path, max_len=512),
            source_revision=text(data, "source_revision", path, max_len=512),
            content_sha256=_nullable_sha256(data, "content_sha256", path),
            captured_at=timestamp(data, "captured_at", path, required=True),
            locator=EvidenceLocator.from_mapping(data.get("locator"), f"{path}.locator"),
            derived_from=ref_id_list(data, "derived_from", path, max_items=100),
            retention=one_of(data, "retention", path, choices=EVIDENCE_RETENTION),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "evidence_id": self.evidence_id,
            "owner_id": self.owner_id, "project_id": self.project_id,
            "source_type": self.source_type, "source_ref": self.source_ref,
            "source_revision": self.source_revision, "content_sha256": self.content_sha256,
            "captured_at": self.captured_at, "locator": self.locator.to_mapping(),
            "derived_from": list(self.derived_from), "retention": self.retention,
        }

    def fingerprint(self) -> str:
        m = self.to_mapping()
        return fingerprint([(k, m[k]) for k in self._KEYS])
