"""
contracts/inference.py — INF-02 §05: the evidence a local-inference launch
produces, kept as three separate axes instead of one boolean.

The spec (`ESPEC_INFERENCIA_LOCAL.md` §05) is explicit that **support** (the
implementation accepts an option), **effective application** (the running
process actually has it set, observed) and **measured benefit** (a benchmark
showed a gain) must never collapse into a single flag. A model can be denied
vision, or have a KV-cache quantization flag accepted by the binary yet
unconfirmed at runtime because the probe that would show it timed out. None of
that is representable as `True`/`False`/`0` without lying about what is known.

The same discipline that governs every other contract in this package governs
this one (see `base.py`'s module docstring): an unknown key is an error, a
vocabulary violation is a `ContractError` naming the field and the value, and
nothing is coerced across a type boundary — in particular, an absent boolean
or number stays `None`, it is never read as `False` or `0`.

Six shapes, in the order the spec lists them:
  EngineIdentity      — which process, which build, which port, whose it is.
  ModelDescriptor     — which weights, provisional until a digest is known.
  HardwareSnapshot     — what physical/topology evidence backs a claim.
  CapabilityAssessment — one option's support/effective/benefit/evidence.
  LaunchReceipt        — the whole of one serve attempt: requested → observed.
  ExecutionMetrics     — INF-03's shape, defined now so the wire format does
                         not change out from under whoever builds it next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, one_of, reject_unknown, text,
    text_list, timestamp, whole,
)

# ── vocabularies ─────────────────────────────────────────────────────────────

IMPLEMENTATIONS = (
    "llama-server", "llama_cpp.server", "vllm", "sglang", "ollama", "mlx",
    "unknown",
)
MANAGED_KINDS = ("faustus", "external", "remote")

MODEL_KINDS = ("dense", "moe", "unknown")
IDENTITY_STATES = ("confirmed", "provisional")

TOPOLOGY_STATES = ("known", "partial", "unknown")

SUPPORT_STATES = ("supported", "unsupported", "unknown")
ASSESSMENT_SCOPES = ("server_start", "request", "global")
EFFECTIVE_STATES = ("confirmed", "mismatch", "unconfirmed", "not_applicable")
BENEFIT_STATES = ("not_evaluated", "observed_gain", "no_gain", "regression")
EVIDENCE_KINDS = ("versioned_manifest", "engine_probe", "none")

CHECK_STATES = ("passed", "failed", "skipped")
VERIFY_STATES = ("pending", "verified", "failed", "stale")

METRIC_SOURCES = ("observed_client", "reported_engine", "computed", "inferred", "absent")
METRIC_SCOPES = ("request", "instance")

#: `observed` on a `LaunchReceipt` is a probe's raw payload, kept for
#: debugging — but a probe answers with whatever the engine sends, and this
#: is not a log store. 32 KB (the number the contract names) is enough to
#: keep every field a reader currently parses and small enough that a
#: misbehaving engine cannot turn a receipt file into a multi-megabyte blob.
MAX_OBSERVED_BYTES = 32 * 1024
#: `requested`/`effective.value` are single option values, not payloads —
#: capped far tighter so one giant string in a request can't bloat a receipt.
MAX_SCALAR_JSON_BYTES = 8 * 1024


def _json_value(data: Mapping[str, Any], key: str, path: str, *,
                 required: bool = False, max_bytes: int = MAX_SCALAR_JSON_BYTES) -> Any:
    """Read `Any`-typed field: a JSON scalar/list/dict, passed through
    unchanged (rule 3 in `base.py` — nothing is coerced), rejected only if it
    cannot be serialized at all or is implausibly large for a single option
    value. `None` is a legitimate value here, not a sentinel for "absent"."""
    _UNSET = object()
    raw = data.get(key, _UNSET)
    if raw is _UNSET:
        if required:
            raise ContractError(f"{path}.{key}", "is required")
        return None
    try:
        encoded = json.dumps(raw)
    except (TypeError, ValueError):
        raise ContractError(f"{path}.{key}", "must be JSON-representable", got=raw)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ContractError(f"{path}.{key}", f"is larger than {max_bytes} bytes once encoded")
    return raw


def _number(data: Mapping[str, Any], key: str, path: str, *,
            required: bool = False) -> Optional[float]:
    """A `number|null` field (int or float, never bool) — `whole()` in
    base.py is int-only, and phase/token metrics are allowed fractional
    milliseconds (e.g. computed averages), so this is its own reader."""
    _UNSET = object()
    raw = data.get(key, _UNSET)
    if raw is _UNSET or raw is None:
        if required:
            raise ContractError(f"{path}.{key}", "is required")
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ContractError(f"{path}.{key}", "expected a number", got=raw)
    return float(raw)


def _dict_field(data: Mapping[str, Any], key: str, path: str, *,
                 max_bytes: Optional[int] = None) -> Dict[str, Any]:
    raw = data.get(key)
    if raw is None:
        return {}
    mapping = as_mapping(raw, f"{path}.{key}")
    if max_bytes is not None:
        try:
            encoded = json.dumps(mapping)
        except (TypeError, ValueError):
            raise ContractError(f"{path}.{key}", "must be JSON-representable", got=raw)
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ContractError(f"{path}.{key}", f"is larger than {max_bytes} bytes once encoded")
    return dict(mapping)


# ── EngineIdentity ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EngineIdentity:
    """Which process answered, not just which API shape it speaks. Two
    `llama-server` instances a version apart, or a `llama-server` and a
    `python -m llama_cpp.server` that both happen to expose the same
    OpenAI-compatible routes, are not interchangeable evidence (§06 H04)."""

    implementation: str
    version: Optional[str] = None
    build: Optional[str] = None
    platform: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    managed: str = "faustus"
    generation: int = 1
    session_id: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("implementation", "version", "build", "platform", "host", "port",
              "managed", "generation", "session_id", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "engine") -> "EngineIdentity":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            implementation=one_of(data, "implementation", path, choices=IMPLEMENTATIONS),
            version=text(data, "version", path, required=False, default=None, allow_blank=False) or None,
            build=text(data, "build", path, required=False, default=None, allow_blank=False) or None,
            platform=text(data, "platform", path, required=False, default=None, allow_blank=False) or None,
            host=text(data, "host", path, required=False, default=None, allow_blank=False) or None,
            port=whole(data, "port", path, minimum=1, maximum=65535),
            managed=one_of(data, "managed", path, choices=MANAGED_KINDS, required=False, default="faustus"),
            generation=whole(data, "generation", path, required=False, default=1, minimum=1) or 1,
            session_id=text(data, "session_id", path, required=False, default=None, allow_blank=False) or None,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "implementation": self.implementation,
            "version": self.version,
            "build": self.build,
            "platform": self.platform,
            "host": self.host,
            "port": self.port,
            "managed": self.managed,
            "generation": self.generation,
            "session_id": self.session_id,
        }


# ── ModelDescriptor ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelDescriptor:
    """Identity of the weights being served. `identity_state` is
    `"provisional"` whenever no `digest` backs the descriptor — §05 forbids
    hashing hundreds of gigabytes just to open a picker, so most descriptors
    stay provisional until something (a cached digest, a registry manifest)
    confirms them; nothing here promotes a descriptor to `"confirmed"` on its
    own."""

    artifact_id: str
    revision: Optional[str] = None
    digest: Optional[str] = None
    architecture: Optional[str] = None
    kind: str = "unknown"
    quantization: Optional[str] = None
    total_params: Optional[int] = None
    active_params: Optional[int] = None
    mtp: Optional[bool] = None
    identity_state: str = "provisional"
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("artifact_id", "revision", "digest", "architecture", "kind",
              "quantization", "total_params", "active_params", "mtp",
              "identity_state", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "model") -> "ModelDescriptor":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        digest = text(data, "digest", path, required=False, default=None, allow_blank=False) or None
        mtp_raw = data.get("mtp")
        if mtp_raw is not None and not isinstance(mtp_raw, bool):
            raise ContractError(f"{path}.mtp", "expected true, false or null", got=mtp_raw)
        # `identity_state` is derived from `digest` when the caller does not
        # state one explicitly — a caller MAY still assert "confirmed"
        # without a digest (e.g. a registry-signed manifest), but silence
        # defaults to the honest, unhashed case.
        identity_state = one_of(
            data, "identity_state", path, choices=IDENTITY_STATES, required=False, default=None,
        )
        if identity_state is None:
            identity_state = "confirmed" if digest else "provisional"
        return cls(
            artifact_id=text(data, "artifact_id", path, max_len=512),
            revision=text(data, "revision", path, required=False, default=None, allow_blank=False) or None,
            digest=digest,
            architecture=text(data, "architecture", path, required=False, default=None, allow_blank=False) or None,
            kind=one_of(data, "kind", path, choices=MODEL_KINDS, required=False, default="unknown"),
            quantization=text(data, "quantization", path, required=False, default=None, allow_blank=False) or None,
            total_params=whole(data, "total_params", path, minimum=0),
            active_params=whole(data, "active_params", path, minimum=0),
            mtp=mtp_raw,
            identity_state=identity_state,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "digest": self.digest,
            "architecture": self.architecture,
            "kind": self.kind,
            "quantization": self.quantization,
            "total_params": self.total_params,
            "active_params": self.active_params,
            "mtp": self.mtp,
            "identity_state": self.identity_state,
        }


# ── HardwareSnapshot ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    uuid: Optional[str] = None
    bus_id: Optional[str] = None
    vram_bytes: Optional[int] = None
    provenance: str = ""

    _KEYS = ("index", "name", "uuid", "bus_id", "vram_bytes", "provenance")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "GpuInfo":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            index=whole(data, "index", path, required=True, minimum=0),
            name=text(data, "name", path, max_len=256),
            uuid=text(data, "uuid", path, required=False, default=None, allow_blank=False) or None,
            bus_id=text(data, "bus_id", path, required=False, default=None, allow_blank=False) or None,
            vram_bytes=whole(data, "vram_bytes", path, minimum=0),
            provenance=text(data, "provenance", path, required=False, default="", allow_blank=True),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "name": self.name, "uuid": self.uuid,
            "bus_id": self.bus_id, "vram_bytes": self.vram_bytes,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class HardwareSnapshot:
    """What is physically known about the box the engine runs on, and how
    sure of it we are (`topology`). A GPU list built from `nvidia-smi` output
    is `known`; one inferred from a driver count with no per-device detail is
    `partial`; nothing read at all is `unknown` — never an empty list
    standing in for "no GPUs", which is indistinguishable from "didn't look"."""

    host: str
    gpus: Tuple[GpuInfo, ...] = ()
    ram_bytes: Optional[int] = None
    observed_at: Optional[str] = None
    topology: str = "unknown"
    provenance: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("host", "gpus", "ram_bytes", "observed_at", "topology",
              "provenance", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "hardware") -> "HardwareSnapshot":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        gpus_raw = data.get("gpus")
        if gpus_raw is None:
            gpus: Tuple[GpuInfo, ...] = ()
        else:
            if not isinstance(gpus_raw, (list, tuple)):
                raise ContractError(f"{path}.gpus", "expected a list", got=gpus_raw)
            gpus = tuple(
                GpuInfo.parse(item, f"{path}.gpus[{i}]") for i, item in enumerate(gpus_raw)
            )
        return cls(
            host=text(data, "host", path, max_len=256),
            gpus=gpus,
            ram_bytes=whole(data, "ram_bytes", path, minimum=0),
            observed_at=timestamp(data, "observed_at", path),
            topology=one_of(data, "topology", path, choices=TOPOLOGY_STATES, required=False, default="unknown"),
            provenance=text(data, "provenance", path, required=False, default="", allow_blank=True),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "host": self.host,
            "gpus": [g.to_dict() for g in self.gpus],
            "ram_bytes": self.ram_bytes,
            "observed_at": self.observed_at,
            "topology": self.topology,
            "provenance": self.provenance,
        }


# ── CapabilityAssessment ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Effective:
    value: Any = None
    state: str = "unconfirmed"

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Effective":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("value", "state"), path)
        return cls(
            value=_json_value(data, "value", path),
            state=one_of(data, "state", path, choices=EFFECTIVE_STATES, required=False, default="unconfirmed"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "state": self.state}


@dataclass(frozen=True)
class Benefit:
    state: str = "not_evaluated"
    benchmark_id: Optional[str] = None

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Benefit":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("state", "benchmark_id"), path)
        return cls(
            state=one_of(data, "state", path, choices=BENEFIT_STATES, required=False, default="not_evaluated"),
            benchmark_id=text(data, "benchmark_id", path, required=False, default=None, allow_blank=False) or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "benchmark_id": self.benchmark_id}


@dataclass(frozen=True)
class Evidence:
    kind: str = "none"
    observed_at: Optional[str] = None

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Evidence":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("kind", "observed_at"), path)
        return cls(
            kind=one_of(data, "kind", path, choices=EVIDENCE_KINDS, required=False, default="none"),
            observed_at=timestamp(data, "observed_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "observed_at": self.observed_at}


@dataclass(frozen=True)
class CapabilityAssessment:
    """One option, three axes (module docstring). `support` is what the
    manifest/engine says is possible; `effective` is what a probe actually
    observed running (defaults to `unconfirmed` — nothing here runs a probe);
    `benefit` is whether it was shown to help (defaults to `not_evaluated` —
    nothing here runs a benchmark). `reasons` is where a human-readable
    "why" for support/effective goes; it is never inferred back out of the
    enum values by a caller."""

    option: str
    requested: Any
    support: str
    scope: str
    requirements: Tuple[str, ...] = ()
    effective: Effective = field(default_factory=Effective)
    benefit: Benefit = field(default_factory=Benefit)
    evidence: Evidence = field(default_factory=Evidence)
    reasons: Tuple[str, ...] = ()

    _KEYS = ("option", "requested", "support", "scope", "requirements",
              "effective", "benefit", "evidence", "reasons")

    @classmethod
    def parse(cls, raw: Any, path: str = "assessment") -> "CapabilityAssessment":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            option=text(data, "option", path, max_len=128),
            requested=_json_value(data, "requested", path),
            support=one_of(data, "support", path, choices=SUPPORT_STATES),
            scope=one_of(data, "scope", path, choices=ASSESSMENT_SCOPES),
            requirements=text_list(data, "requirements", path, max_items=16, max_len=200, unique=False),
            effective=Effective.parse(data.get("effective"), f"{path}.effective"),
            benefit=Benefit.parse(data.get("benefit"), f"{path}.benefit"),
            evidence=Evidence.parse(data.get("evidence"), f"{path}.evidence"),
            reasons=text_list(data, "reasons", path, max_items=16, max_len=500, unique=False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "option": self.option,
            "requested": self.requested,
            "support": self.support,
            "scope": self.scope,
            "requirements": list(self.requirements),
            "effective": self.effective.to_dict(),
            "benefit": self.benefit.to_dict(),
            "evidence": self.evidence.to_dict(),
            "reasons": list(self.reasons),
        }


# ── LaunchReceipt ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RewriteStep:
    """One entry of INF-01's `rewrites` ledger — a step name and the command
    text before/after it ran, so a client never has to guess whether (or
    how) the server altered what it asked for."""

    step: str
    before: str
    after: str

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RewriteStep":
        data = as_mapping(raw, path)
        reject_unknown(data, ("step", "before", "after"), path)
        return cls(
            step=text(data, "step", path, max_len=128),
            before=text(data, "before", path, required=False, default="", allow_blank=True, max_len=8192),
            after=text(data, "after", path, required=False, default="", allow_blank=True, max_len=8192),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"step": self.step, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class Difference:
    option: str
    requested: Any
    observed: Any
    state: str

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Difference":
        data = as_mapping(raw, path)
        reject_unknown(data, ("option", "requested", "observed", "state"), path)
        return cls(
            option=text(data, "option", path, max_len=128),
            requested=_json_value(data, "requested", path),
            observed=_json_value(data, "observed", path),
            state=one_of(data, "state", path, choices=EFFECTIVE_STATES),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "option": self.option, "requested": self.requested,
            "observed": self.observed, "state": self.state,
        }


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str = ""

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Check":
        data = as_mapping(raw, path)
        reject_unknown(data, ("name", "state", "detail"), path)
        return cls(
            name=text(data, "name", path, max_len=128),
            state=one_of(data, "state", path, choices=CHECK_STATES),
            detail=text(data, "detail", path, required=False, default="", allow_blank=True, max_len=2000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class LaunchReceipt:
    """§07: "a plan authorized, not a surprising command", carried through to
    what actually ran. Distinguishes requested → translated (`plan`) →
    accepted by the process (`final_cmd`, `rewrites`) → observed in
    execution (`observed`, `differences`, `checks`). `verify_state` starts
    `"pending"`; nothing here calls a probe."""

    session_id: str
    engine: EngineIdentity
    model: Optional[ModelDescriptor]
    requested_cmd: str
    final_cmd: str
    rewrites: Tuple[RewriteStep, ...]
    plan: Dict[str, Any]
    assessments: Tuple[CapabilityAssessment, ...]
    observed: Dict[str, Any]
    differences: Tuple[Difference, ...]
    checks: Tuple[Check, ...]
    created_at: str
    verified_at: Optional[str] = None
    verify_state: str = "pending"
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("session_id", "engine", "model", "requested_cmd", "final_cmd",
              "rewrites", "plan", "assessments", "observed", "differences",
              "checks", "created_at", "verified_at", "verify_state",
              "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "receipt") -> "LaunchReceipt":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        rewrites_raw = data.get("rewrites") or []
        if not isinstance(rewrites_raw, (list, tuple)):
            raise ContractError(f"{path}.rewrites", "expected a list", got=rewrites_raw)
        assessments_raw = data.get("assessments") or []
        if not isinstance(assessments_raw, (list, tuple)):
            raise ContractError(f"{path}.assessments", "expected a list", got=assessments_raw)
        differences_raw = data.get("differences") or []
        if not isinstance(differences_raw, (list, tuple)):
            raise ContractError(f"{path}.differences", "expected a list", got=differences_raw)
        checks_raw = data.get("checks") or []
        if not isinstance(checks_raw, (list, tuple)):
            raise ContractError(f"{path}.checks", "expected a list", got=checks_raw)
        model_raw = data.get("model")
        return cls(
            session_id=text(data, "session_id", path, max_len=128),
            engine=EngineIdentity.parse(data.get("engine"), f"{path}.engine"),
            model=ModelDescriptor.parse(model_raw, f"{path}.model") if model_raw is not None else None,
            requested_cmd=text(data, "requested_cmd", path, required=False, default="", allow_blank=True, max_len=16384),
            final_cmd=text(data, "final_cmd", path, required=False, default="", allow_blank=True, max_len=16384),
            rewrites=tuple(RewriteStep.parse(item, f"{path}.rewrites[{i}]") for i, item in enumerate(rewrites_raw)),
            plan=_dict_field(data, "plan", path),
            assessments=tuple(
                CapabilityAssessment.parse(item, f"{path}.assessments[{i}]")
                for i, item in enumerate(assessments_raw)
            ),
            observed=_dict_field(data, "observed", path, max_bytes=MAX_OBSERVED_BYTES),
            differences=tuple(Difference.parse(item, f"{path}.differences[{i}]") for i, item in enumerate(differences_raw)),
            checks=tuple(Check.parse(item, f"{path}.checks[{i}]") for i, item in enumerate(checks_raw)),
            created_at=timestamp(data, "created_at", path, required=True),
            verified_at=timestamp(data, "verified_at", path),
            verify_state=one_of(data, "verify_state", path, choices=VERIFY_STATES, required=False, default="pending"),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "engine": self.engine.to_dict(),
            "model": self.model.to_dict() if self.model is not None else None,
            "requested_cmd": self.requested_cmd,
            "final_cmd": self.final_cmd,
            "rewrites": [r.to_dict() for r in self.rewrites],
            "plan": self.plan,
            "assessments": [a.to_dict() for a in self.assessments],
            "observed": self.observed,
            "differences": [d.to_dict() for d in self.differences],
            "checks": [c.to_dict() for c in self.checks],
            "created_at": self.created_at,
            "verified_at": self.verified_at,
            "verify_state": self.verify_state,
        }


# ── ExecutionMetrics (INF-03 will populate it; the shape is fixed now) ──────

@dataclass(frozen=True)
class MetricValue:
    value: Optional[float] = None
    source: str = "absent"

    @classmethod
    def parse(cls, raw: Any, path: str) -> "MetricValue":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("value", "source"), path)
        return cls(
            value=_number(data, "value", path),
            source=one_of(data, "source", path, choices=METRIC_SOURCES, required=False, default="absent"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source}


_PHASE_KEYS = ("queue_wait_ms", "load_ms", "prefill_ms", "generation_ms", "tools_ms", "total_ms")
_TOKEN_KEYS = ("prompt", "generated")


@dataclass(frozen=True)
class Phases:
    queue_wait_ms: MetricValue = field(default_factory=MetricValue)
    load_ms: MetricValue = field(default_factory=MetricValue)
    prefill_ms: MetricValue = field(default_factory=MetricValue)
    generation_ms: MetricValue = field(default_factory=MetricValue)
    tools_ms: MetricValue = field(default_factory=MetricValue)
    total_ms: MetricValue = field(default_factory=MetricValue)

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Phases":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, _PHASE_KEYS, path)
        return cls(**{key: MetricValue.parse(data.get(key), f"{path}.{key}") for key in _PHASE_KEYS})

    def to_dict(self) -> Dict[str, Any]:
        return {key: getattr(self, key).to_dict() for key in _PHASE_KEYS}


@dataclass(frozen=True)
class Tokens:
    prompt: MetricValue = field(default_factory=MetricValue)
    generated: MetricValue = field(default_factory=MetricValue)

    @classmethod
    def parse(cls, raw: Any, path: str) -> "Tokens":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, _TOKEN_KEYS, path)
        return cls(**{key: MetricValue.parse(data.get(key), f"{path}.{key}") for key in _TOKEN_KEYS})

    def to_dict(self) -> Dict[str, Any]:
        return {key: getattr(self, key).to_dict() for key in _TOKEN_KEYS}


@dataclass(frozen=True)
class ExecutionMetrics:
    """INF-03's per-turn timing/token account, defined here so the wire
    shape is fixed before that milestone starts writing to it. Every phase
    and token count is a `MetricValue`, never a bare number — a missing
    `tools_ms` (no tool call happened) and an unmeasured `prefill_ms` (the
    engine doesn't report it) are different facts and must stay different
    facts on the wire."""

    phases: Phases
    tokens: Tokens
    scope: str
    engine: Optional[EngineIdentity] = None
    observed_at: Optional[str] = None
    #: Free-text caveats a caller could not express as a `MetricValue.source`
    #: — e.g. "phases overlap: engine and client clocks are not additive"
    #: when prefill+generation+tools exceeds total (never silently corrected),
    #: or why a phase was marked `inferred` rather than `computed`. Empty by
    #: far the common case; never inferred back out of the phase values by a
    #: reader — if a caller has a reason, it belongs here explicitly.
    notes: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("phases", "tokens", "scope", "engine", "observed_at", "notes", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "metrics") -> "ExecutionMetrics":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        engine_raw = data.get("engine")
        return cls(
            phases=Phases.parse(data.get("phases"), f"{path}.phases"),
            tokens=Tokens.parse(data.get("tokens"), f"{path}.tokens"),
            scope=one_of(data, "scope", path, choices=METRIC_SCOPES),
            engine=EngineIdentity.parse(engine_raw, f"{path}.engine") if engine_raw is not None else None,
            observed_at=timestamp(data, "observed_at", path),
            notes=text_list(data, "notes", path, max_items=16, max_len=500, unique=False),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "phases": self.phases.to_dict(),
            "tokens": self.tokens.to_dict(),
            "scope": self.scope,
            "engine": self.engine.to_dict() if self.engine is not None else None,
            "observed_at": self.observed_at,
            "notes": list(self.notes),
        }
