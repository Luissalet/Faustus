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
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, one_of,
    reject_unknown, text, text_list, timestamp, whole,
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
#
# INF-05 §11 "Inventario físico frente a nombres de conexión": a `GpuInfo`'s
# `index` is a runtime slot number, not identity — reconnecting an AORUS eGPU
# or letting the driver enumerate cards in a different order changes which
# card is "index 1" without changing which physical card it is. `link` and
# `transport` are optional, additive fields so a `GpuInfo` built before this
# lote (no `link`/`transport` in its dict) still parses — `parse()` treats a
# missing key exactly like an explicit `null`.

LINK_SOURCES = ("observed", "manual", "absent")
TRANSPORT_KINDS = ("pcie", "thunderbolt", "oculink", "usb4", "unknown")
TRANSPORT_SOURCES = ("observed", "manual", "heuristic")


@dataclass(frozen=True)
class LinkInfo:
    """PCIe link state as `nvidia-smi` reports it (`pcie.link.*`). Any field
    can be `None` — a `[N/A]`/`[Not Supported]` cell, never coerced to 0 —
    and `source` says whether the whole reading came from the driver
    (`observed`) or a person typed it in (`manual`; `apply_annotations`
    never lets `manual` win over `observed`, only over the default
    `absent`)."""

    gen_current: Optional[int] = None
    width_current: Optional[int] = None
    gen_max: Optional[int] = None
    width_max: Optional[int] = None
    source: str = "absent"

    _KEYS = ("gen_current", "width_current", "gen_max", "width_max", "source")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "LinkInfo":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            gen_current=whole(data, "gen_current", path, minimum=1),
            width_current=whole(data, "width_current", path, minimum=1),
            gen_max=whole(data, "gen_max", path, minimum=1),
            width_max=whole(data, "width_max", path, minimum=1),
            source=one_of(data, "source", path, choices=LINK_SOURCES, required=False, default="absent"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gen_current": self.gen_current, "width_current": self.width_current,
            "gen_max": self.gen_max, "width_max": self.width_max, "source": self.source,
        }


@dataclass(frozen=True)
class TransportInfo:
    """What physical interface carries the link. `heuristic` is a narrow,
    explicit guess (`gpu_topology.heuristic_transport`: a link observed
    unusually narrow) — never derived from a GPU's commercial name (§11:
    "no deducir ancho de banda útil de una etiqueta comercial"). `manual`
    is a person's annotation, kept distinguishable from both."""

    kind: str = "unknown"
    source: str = "observed"
    note: str = ""
    observed_at: Optional[str] = None

    _KEYS = ("kind", "source", "note", "observed_at")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "TransportInfo":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            kind=one_of(data, "kind", path, choices=TRANSPORT_KINDS, required=False, default="unknown"),
            source=one_of(data, "source", path, choices=TRANSPORT_SOURCES, required=False, default="observed"),
            note=text(data, "note", path, required=False, default="", allow_blank=True, max_len=500),
            observed_at=timestamp(data, "observed_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "note": self.note, "observed_at": self.observed_at}


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    uuid: Optional[str] = None
    bus_id: Optional[str] = None
    vram_bytes: Optional[int] = None
    provenance: str = ""
    driver: Optional[str] = None
    link: Optional[LinkInfo] = None
    transport: Optional[TransportInfo] = None

    _KEYS = ("index", "name", "uuid", "bus_id", "vram_bytes", "provenance",
              "driver", "link", "transport")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "GpuInfo":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        link_raw = data.get("link")
        transport_raw = data.get("transport")
        return cls(
            index=whole(data, "index", path, required=True, minimum=0),
            name=text(data, "name", path, max_len=256),
            uuid=text(data, "uuid", path, required=False, default=None, allow_blank=False) or None,
            bus_id=text(data, "bus_id", path, required=False, default=None, allow_blank=False) or None,
            vram_bytes=whole(data, "vram_bytes", path, minimum=0),
            provenance=text(data, "provenance", path, required=False, default="", allow_blank=True),
            driver=text(data, "driver", path, required=False, default=None, allow_blank=False) or None,
            link=LinkInfo.parse(link_raw, f"{path}.link") if link_raw is not None else None,
            transport=TransportInfo.parse(transport_raw, f"{path}.transport") if transport_raw is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "name": self.name, "uuid": self.uuid,
            "bus_id": self.bus_id, "vram_bytes": self.vram_bytes,
            "provenance": self.provenance, "driver": self.driver,
            "link": self.link.to_dict() if self.link is not None else None,
            "transport": self.transport.to_dict() if self.transport is not None else None,
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


def identity_key(gpu: GpuInfo) -> Optional[str]:
    """The physical identity `reconcile_indices`/`MemoryBudget` key on:
    `uuid` when the driver reports one, else `bus_id`, else `None` — an
    `index` is NEVER identity (§11 T15: "GPU 1" is not the same device after
    a reconnect). Prefixed (`uuid:...`/`bus:...`) so a uuid string and a bus
    id string can never collide in the same key space."""
    if gpu.uuid:
        return f"uuid:{gpu.uuid}"
    if gpu.bus_id:
        return f"bus:{gpu.bus_id}"
    return None


RECONCILIATION_STATES = ("same", "moved", "missing", "new", "unidentifiable")


@dataclass(frozen=True)
class IndexReconciliation:
    key: Optional[str]
    previous_index: Optional[int]
    current_index: Optional[int]
    state: str

    _KEYS = ("key", "previous_index", "current_index", "state")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "IndexReconciliation":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            key=text(data, "key", path, required=False, default=None, allow_blank=False) or None,
            previous_index=whole(data, "previous_index", path, minimum=0),
            current_index=whole(data, "current_index", path, minimum=0),
            state=one_of(data, "state", path, choices=RECONCILIATION_STATES),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "previous_index": self.previous_index,
            "current_index": self.current_index, "state": self.state,
        }


def reconcile_indices(
    previous: HardwareSnapshot, current: HardwareSnapshot,
) -> Tuple[IndexReconciliation, ...]:
    """Match `previous`'s GPUs to `current`'s by `identity_key`, never by
    `index` alone (§11 T15). A GPU with no uuid/bus_id on either side is
    always `unidentifiable` — it is never reported `same` just because its
    index did not change, since nothing here actually re-identified it."""
    prev_by_key: Dict[str, GpuInfo] = {}
    prev_unidentified: List[GpuInfo] = []
    for g in previous.gpus:
        key = identity_key(g)
        (prev_unidentified.append(g) if key is None else prev_by_key.__setitem__(key, g))
    cur_by_key: Dict[str, GpuInfo] = {}
    cur_unidentified: List[GpuInfo] = []
    for g in current.gpus:
        key = identity_key(g)
        (cur_unidentified.append(g) if key is None else cur_by_key.__setitem__(key, g))

    out: List[IndexReconciliation] = []
    for key, pg in prev_by_key.items():
        cg = cur_by_key.get(key)
        if cg is None:
            out.append(IndexReconciliation(key=key, previous_index=pg.index, current_index=None, state="missing"))
        elif cg.index == pg.index:
            out.append(IndexReconciliation(key=key, previous_index=pg.index, current_index=cg.index, state="same"))
        else:
            out.append(IndexReconciliation(key=key, previous_index=pg.index, current_index=cg.index, state="moved"))
    for key, cg in cur_by_key.items():
        if key not in prev_by_key:
            out.append(IndexReconciliation(key=key, previous_index=None, current_index=cg.index, state="new"))
    for g in prev_unidentified:
        out.append(IndexReconciliation(key=None, previous_index=g.index, current_index=None, state="unidentifiable"))
    for g in cur_unidentified:
        out.append(IndexReconciliation(key=None, previous_index=None, current_index=g.index, state="unidentifiable"))
    return tuple(out)


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


# ── InferenceProfile / BenchmarkCase / BenchmarkRun / Comparison (INF-04) ───
#
# §09 "Optimizar para mi equipo" and §10 (the benchmark bench). Four more
# shapes, kept in this module rather than a new one: they lean on
# EngineIdentity/ModelDescriptor/ExecutionMetrics directly (a profile IS an
# (engine, model) pair; a sample's metrics ARE an ExecutionMetrics), and
# CONTRATO_INF04 asks for "mismo estilo" — one place a reader already knows
# to look for the vocabulary a launch/benchmark speaks.

PROFILE_OBJECTIVES = ("interactive", "coding_agent", "long_documents")
PROFILE_EVALUATIONS = (
    "not_evaluated", "baseline", "evaluated", "inconclusive", "recommended", "regression",
)
PROFILE_SOURCES = ("manual", "current", "candidate", "imported")

CHECK_KINDS = (
    "contains", "regex", "json_valid", "json_has_keys", "max_words",
    "language_es", "no_tool_leak",
)

RUN_STATES = (
    "planned", "waiting_resources", "preparing", "running", "evaluating",
    "completed", "partial", "cancelled", "interrupted", "failed",
)

COMPARISON_VERDICTS = ("improvement", "no_change", "regression", "inconclusive")

#: States `plan()`/`start()` may write between `planned` and a terminal
#: state — used by the runner to validate transitions; kept here (not in
#: src/bench/runner.py) so the vocabulary of "what is still moving" lives
#: next to RUN_STATES itself.
RUN_STATES_IN_FLIGHT = ("waiting_resources", "preparing", "running", "evaluating")
RUN_STATES_TERMINAL = ("completed", "partial", "cancelled", "interrupted", "failed")


def compute_profile_fingerprint(
    *, model: ModelDescriptor, engine: EngineIdentity,
    options: Mapping[str, Any], hardware_id: Optional[str],
) -> str:
    """A1: "sha256 corto de model.artifact_id+revision + engine.implementation
    +version + options canonicas ordenadas + hardware_id — excluye secretos y
    URLs". Deliberately leaves out `engine.host`/`engine.port`/`session_id`
    (connection details, not what makes two launches the same configuration)
    and anything from `model.digest` (identity, not a tuning knob). Reuses
    `base.fingerprint()` — the same length-prefixed, key-sorted hash every
    other contract's identity uses — rather than hand-rolling string
    concatenation that could collide across a field boundary.

    Truncated to 16 hex chars (64 bits): a profile fingerprint is a
    de-duplication key a person reads in a list, not a security boundary —
    the spec asks for "corto", and 64 bits of a SHA-256 is exorbitantly more
    collision-resistant than the handful of profiles any one machine will
    ever hold.
    """
    full = fingerprint([
        ("model.artifact_id", model.artifact_id),
        ("model.revision", model.revision),
        ("engine.implementation", engine.implementation),
        ("engine.version", engine.version),
        ("options", dict(options or {})),
        ("hardware_id", hardware_id),
    ])
    return full[:16]


@dataclass(frozen=True)
class InferenceProfile:
    """A1: one named (model, engine, options) configuration under
    evaluation. `fingerprint` is what makes two profiles "the same
    configuration" for `Comparison.comparable` and for spotting that a saved
    profile has drifted from what is actually running now — recomputed by
    `compute_profile_fingerprint` whenever a caller does not supply one
    (parsing a profile back off disk always has one already)."""

    id: str
    label: str
    model: ModelDescriptor
    engine: EngineIdentity
    hardware_id: Optional[str]
    options: Dict[str, Any]
    objective: str
    evaluation: str
    fingerprint: str
    created_at: str
    source: str
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "label", "model", "engine", "hardware_id", "options",
              "objective", "evaluation", "fingerprint", "created_at",
              "source", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "profile") -> "InferenceProfile":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        model = ModelDescriptor.parse(data.get("model"), f"{path}.model")
        engine = EngineIdentity.parse(data.get("engine"), f"{path}.engine")
        hardware_id = text(data, "hardware_id", path, required=False, default=None, allow_blank=False) or None
        options = _dict_field(data, "options", path)
        fp = text(data, "fingerprint", path, required=False, default=None, allow_blank=False) or None
        if fp is None:
            fp = compute_profile_fingerprint(model=model, engine=engine, options=options, hardware_id=hardware_id)
        return cls(
            id=text(data, "id", path, max_len=128),
            label=text(data, "label", path, max_len=256),
            model=model,
            engine=engine,
            hardware_id=hardware_id,
            options=options,
            objective=one_of(data, "objective", path, choices=PROFILE_OBJECTIVES),
            evaluation=one_of(data, "evaluation", path, choices=PROFILE_EVALUATIONS,
                              required=False, default="not_evaluated"),
            fingerprint=fp,
            created_at=timestamp(data, "created_at", path, required=True),
            source=one_of(data, "source", path, choices=PROFILE_SOURCES, required=False, default="manual"),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "label": self.label,
            "model": self.model.to_dict(),
            "engine": self.engine.to_dict(),
            "hardware_id": self.hardware_id,
            "options": self.options,
            "objective": self.objective,
            "evaluation": self.evaluation,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "source": self.source,
        }


# ── BenchmarkCase ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaseCheck:
    """One quality assertion `src/bench/suites.py::run_checks` evaluates
    against a sample's raw output. `arg` is whatever shape the `kind` needs
    (a substring, a regex, a list of JSON keys, a word limit) — never
    reinterpreted here, only carried."""

    kind: str
    arg: Any = None

    @classmethod
    def parse(cls, raw: Any, path: str) -> "CaseCheck":
        data = as_mapping(raw, path)
        reject_unknown(data, ("kind", "arg"), path)
        return cls(
            kind=one_of(data, "kind", path, choices=CHECK_KINDS),
            arg=_json_value(data, "arg", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "arg": self.arg}


@dataclass(frozen=True)
class BenchmarkCase:
    """A2: one deterministic test case from a suite file. Exactly one of
    `prompt`/`messages` is required — a case is either a single-turn prompt
    or a scripted multi-turn exchange, never neither (a case with no input
    measures nothing) nor both (ambiguous which one the runner should send)."""

    id: str
    suite: str
    prompt: Optional[str] = None
    messages: Optional[Tuple[Dict[str, Any], ...]] = None
    checks: Tuple[CaseCheck, ...] = ()
    max_tokens: Optional[int] = None
    tags: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "suite", "prompt", "messages", "checks", "max_tokens",
              "tags", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "case") -> "BenchmarkCase":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        # `long_documents` cases embed a whole ~3-4k token document IN the
        # prompt (A2: "sin ficheros externos") — the default 4096-char cap
        # every other free-text contract field uses would reject exactly
        # the suite this lote ships, so `prompt` gets its own, much larger
        # ceiling (a real safety cap, not effectively unbounded: 128 KiB is
        # already an order of magnitude past what any suite here needs).
        prompt = text(data, "prompt", path, required=False, default=None,
                      allow_blank=False, max_len=131_072) or None
        messages_raw = data.get("messages")
        messages: Optional[Tuple[Dict[str, Any], ...]] = None
        if messages_raw is not None:
            if not isinstance(messages_raw, (list, tuple)):
                raise ContractError(f"{path}.messages", "expected a list", got=messages_raw)
            messages = tuple(
                dict(as_mapping(m, f"{path}.messages[{i}]")) for i, m in enumerate(messages_raw)
            )
        if prompt is None and not messages:
            raise ContractError(path, "requires either prompt or messages")
        if prompt is not None and messages:
            raise ContractError(path, "requires prompt or messages, not both")
        checks_raw = data.get("checks") or []
        if not isinstance(checks_raw, (list, tuple)):
            raise ContractError(f"{path}.checks", "expected a list", got=checks_raw)
        return cls(
            id=text(data, "id", path, max_len=128),
            suite=text(data, "suite", path, max_len=128),
            prompt=prompt,
            messages=messages,
            checks=tuple(CaseCheck.parse(c, f"{path}.checks[{i}]") for i, c in enumerate(checks_raw)),
            max_tokens=whole(data, "max_tokens", path, minimum=1),
            tags=text_list(data, "tags", path, max_items=16, max_len=64),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "suite": self.suite,
            "prompt": self.prompt,
            "messages": [dict(m) for m in self.messages] if self.messages is not None else None,
            "checks": [c.to_dict() for c in self.checks],
            "max_tokens": self.max_tokens,
            "tags": list(self.tags),
        }


# ── BenchmarkRun ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunBudget:
    max_cases: Optional[int] = None
    max_seconds: Optional[float] = None
    max_generated_tokens: Optional[int] = None
    repeats: int = 1

    _KEYS = ("max_cases", "max_seconds", "max_generated_tokens", "repeats")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunBudget":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            max_cases=whole(data, "max_cases", path, minimum=1),
            max_seconds=_number(data, "max_seconds", path),
            max_generated_tokens=whole(data, "max_generated_tokens", path, minimum=1),
            repeats=whole(data, "repeats", path, default=1, minimum=1) or 1,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_cases": self.max_cases, "max_seconds": self.max_seconds,
            "max_generated_tokens": self.max_generated_tokens, "repeats": self.repeats,
        }


@dataclass(frozen=True)
class RunConditions:
    """§10 "condiciones reproducibles" — `cold_start`/`resident` stay `None`
    (unknown) unless the runner actually observed which one happened; a
    caller must never guess "cold" just because it did not check."""

    cold_start: Optional[bool] = None
    resident: Optional[bool] = None
    prefix_cache: Optional[str] = None
    seed: Optional[int] = None
    temperature: Optional[float] = None
    sampling: Dict[str, Any] = field(default_factory=dict)

    _KEYS = ("cold_start", "resident", "prefix_cache", "seed", "temperature", "sampling")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunConditions":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        cold_start = data.get("cold_start")
        if cold_start is not None and not isinstance(cold_start, bool):
            raise ContractError(f"{path}.cold_start", "expected true, false or null", got=cold_start)
        resident = data.get("resident")
        if resident is not None and not isinstance(resident, bool):
            raise ContractError(f"{path}.resident", "expected true, false or null", got=resident)
        return cls(
            cold_start=cold_start,
            resident=resident,
            prefix_cache=text(data, "prefix_cache", path, required=False, default=None, allow_blank=False) or None,
            seed=whole(data, "seed", path),
            temperature=_number(data, "temperature", path),
            sampling=_dict_field(data, "sampling", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cold_start": self.cold_start, "resident": self.resident,
            "prefix_cache": self.prefix_cache, "seed": self.seed,
            "temperature": self.temperature, "sampling": self.sampling,
        }


@dataclass(frozen=True)
class SampleQuality:
    #: `None` means "not evaluated yet" (a case still queued), never a
    #: silent pass — `run_checks` always resolves this to True/False before
    #: a sample is recorded as `completed`.
    passed: Optional[bool] = None
    failed_checks: Tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Any, path: str) -> "SampleQuality":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("passed", "failed_checks"), path)
        passed = data.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise ContractError(f"{path}.passed", "expected true, false or null", got=passed)
        return cls(
            passed=passed,
            failed_checks=text_list(data, "failed_checks", path, max_items=32, max_len=200, unique=False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "failed_checks": list(self.failed_checks)}


@dataclass(frozen=True)
class RunSample:
    case_id: str
    repeat: int
    metrics: Optional[ExecutionMetrics] = None
    quality: SampleQuality = field(default_factory=SampleQuality)
    output_chars: Optional[int] = None
    error: Optional[str] = None

    _KEYS = ("case_id", "repeat", "metrics", "quality", "output_chars", "error")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunSample":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        metrics_raw = data.get("metrics")
        return cls(
            case_id=text(data, "case_id", path, max_len=128),
            repeat=whole(data, "repeat", path, required=True, minimum=1),
            metrics=ExecutionMetrics.parse(metrics_raw, f"{path}.metrics") if metrics_raw is not None else None,
            quality=SampleQuality.parse(data.get("quality"), f"{path}.quality"),
            output_chars=whole(data, "output_chars", path, minimum=0),
            error=text(data, "error", path, required=False, default=None, allow_blank=False) or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "repeat": self.repeat,
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
            "quality": self.quality.to_dict(),
            "output_chars": self.output_chars,
            "error": self.error,
        }


@dataclass(frozen=True)
class RunStat:
    median: Optional[float] = None
    p95: Optional[float] = None
    n: int = 0

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunStat":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("median", "p95", "n"), path)
        return cls(
            median=_number(data, "median", path),
            p95=_number(data, "p95", path),
            n=whole(data, "n", path, default=0, minimum=0) or 0,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"median": self.median, "p95": self.p95, "n": self.n}


@dataclass(frozen=True)
class RunSummary:
    cases_run: int = 0
    cases_planned: int = 0
    quality_pass_rate: Optional[float] = None
    gen_tps: RunStat = field(default_factory=RunStat)
    ttft_ms: RunStat = field(default_factory=RunStat)
    total_ms: Optional[float] = None
    #: A3's `plan()` duration estimate, from `llm_core.local_speed(model)` —
    #: `None` when no speed has been learned for this model yet, NEVER a
    #: guessed number. Not in CONTRATO_INF04 A1's literal field list for
    #: `summary`, added here (rather than as a new top-level `BenchmarkRun`
    #: field, or a second key next to `{"run": ...}` in A4's
    #: `POST /api/bench/plan` response, neither of which the contract
    #: declares either) because it is a fact about "how much of `summary` do
    #: we expect", which is exactly what the rest of this shape already
    #: describes — see CONTRATO_INF04.md's Lote A report for this deviation.
    estimate_seconds: Optional[float] = None

    _KEYS = ("cases_run", "cases_planned", "quality_pass_rate", "gen_tps",
              "ttft_ms", "total_ms", "estimate_seconds")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunSummary":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            cases_run=whole(data, "cases_run", path, default=0, minimum=0) or 0,
            cases_planned=whole(data, "cases_planned", path, default=0, minimum=0) or 0,
            quality_pass_rate=_number(data, "quality_pass_rate", path),
            gen_tps=RunStat.parse(data.get("gen_tps"), f"{path}.gen_tps"),
            ttft_ms=RunStat.parse(data.get("ttft_ms"), f"{path}.ttft_ms"),
            total_ms=_number(data, "total_ms", path),
            estimate_seconds=_number(data, "estimate_seconds", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cases_run": self.cases_run, "cases_planned": self.cases_planned,
            "quality_pass_rate": self.quality_pass_rate,
            "gen_tps": self.gen_tps.to_dict(), "ttft_ms": self.ttft_ms.to_dict(),
            "total_ms": self.total_ms, "estimate_seconds": self.estimate_seconds,
        }


@dataclass(frozen=True)
class RunInterruption:
    at: Optional[str]
    reason: str

    @classmethod
    def parse(cls, raw: Any, path: str) -> "RunInterruption":
        data = as_mapping(raw, path)
        reject_unknown(data, ("at", "reason"), path)
        return cls(
            at=timestamp(data, "at", path),
            reason=text(data, "reason", path, max_len=200),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"at": self.at, "reason": self.reason}


@dataclass(frozen=True)
class BenchmarkRun:
    """A3: one benchmark execution — the plan, the budget, every sample
    collected so far, and the state machine `src/bench/runner.py` drives.
    `state` is never written outside `RUN_STATES`, and the runner's own
    transitions (`RUN_STATES_IN_FLIGHT` -> a member of `RUN_STATES_TERMINAL`)
    are enforced there, not re-derived from this shape."""

    id: str
    suite_id: str
    suite_version: str
    profile: InferenceProfile
    baseline_run_id: Optional[str]
    state: str
    budget: RunBudget
    conditions: RunConditions
    samples: Tuple[RunSample, ...]
    summary: RunSummary
    interruptions: Tuple[RunInterruption, ...]
    started_at: Optional[str]
    finished_at: Optional[str]
    notes: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "suite_id", "suite_version", "profile", "baseline_run_id",
              "state", "budget", "conditions", "samples", "summary",
              "interruptions", "started_at", "finished_at", "notes",
              "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "run") -> "BenchmarkRun":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        samples_raw = data.get("samples") or []
        if not isinstance(samples_raw, (list, tuple)):
            raise ContractError(f"{path}.samples", "expected a list", got=samples_raw)
        interruptions_raw = data.get("interruptions") or []
        if not isinstance(interruptions_raw, (list, tuple)):
            raise ContractError(f"{path}.interruptions", "expected a list", got=interruptions_raw)
        return cls(
            id=text(data, "id", path, max_len=128),
            suite_id=text(data, "suite_id", path, max_len=128),
            suite_version=text(data, "suite_version", path, max_len=64),
            profile=InferenceProfile.parse(data.get("profile"), f"{path}.profile"),
            baseline_run_id=text(data, "baseline_run_id", path, required=False, default=None, allow_blank=False) or None,
            state=one_of(data, "state", path, choices=RUN_STATES),
            budget=RunBudget.parse(data.get("budget"), f"{path}.budget"),
            conditions=RunConditions.parse(data.get("conditions"), f"{path}.conditions"),
            samples=tuple(RunSample.parse(s, f"{path}.samples[{i}]") for i, s in enumerate(samples_raw)),
            summary=RunSummary.parse(data.get("summary"), f"{path}.summary"),
            interruptions=tuple(
                RunInterruption.parse(x, f"{path}.interruptions[{i}]") for i, x in enumerate(interruptions_raw)
            ),
            started_at=timestamp(data, "started_at", path),
            finished_at=timestamp(data, "finished_at", path),
            notes=text_list(data, "notes", path, max_items=32, max_len=500, unique=False),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "profile": self.profile.to_dict(),
            "baseline_run_id": self.baseline_run_id,
            "state": self.state,
            "budget": self.budget.to_dict(),
            "conditions": self.conditions.to_dict(),
            "samples": [s.to_dict() for s in self.samples],
            "summary": self.summary.to_dict(),
            "interruptions": [x.to_dict() for x in self.interruptions],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "notes": list(self.notes),
        }


# ── Comparison ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComparisonDeltas:
    gen_tps_median_pct: Optional[float] = None
    ttft_median_pct: Optional[float] = None
    quality_pass_rate_delta: Optional[float] = None

    @classmethod
    def parse(cls, raw: Any, path: str) -> "ComparisonDeltas":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("gen_tps_median_pct", "ttft_median_pct", "quality_pass_rate_delta"), path)
        return cls(
            gen_tps_median_pct=_number(data, "gen_tps_median_pct", path),
            ttft_median_pct=_number(data, "ttft_median_pct", path),
            quality_pass_rate_delta=_number(data, "quality_pass_rate_delta", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gen_tps_median_pct": self.gen_tps_median_pct,
            "ttft_median_pct": self.ttft_median_pct,
            "quality_pass_rate_delta": self.quality_pass_rate_delta,
        }


@dataclass(frozen=True)
class SampleSizes:
    baseline: int = 0
    candidate: int = 0

    @classmethod
    def parse(cls, raw: Any, path: str) -> "SampleSizes":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, ("baseline", "candidate"), path)
        return cls(
            baseline=whole(data, "baseline", path, default=0, minimum=0) or 0,
            candidate=whole(data, "candidate", path, default=0, minimum=0) or 0,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"baseline": self.baseline, "candidate": self.candidate}


@dataclass(frozen=True)
class Comparison:
    """A3 `compare()`'s output. `comparable=False` always carries a `reasons`
    entry explaining why (different suite/version/model/objective) — a
    reader must never have to infer incomparability from an absent field."""

    baseline_run_id: str
    candidate_run_id: str
    verdict: str
    reasons: Tuple[str, ...]
    deltas: ComparisonDeltas
    sample_sizes: SampleSizes
    comparable: bool
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("baseline_run_id", "candidate_run_id", "verdict", "reasons",
              "deltas", "sample_sizes", "comparable", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "comparison") -> "Comparison":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        comparable_raw = data.get("comparable")
        if not isinstance(comparable_raw, bool):
            raise ContractError(f"{path}.comparable", "expected true or false", got=comparable_raw)
        return cls(
            baseline_run_id=text(data, "baseline_run_id", path, max_len=128),
            candidate_run_id=text(data, "candidate_run_id", path, max_len=128),
            verdict=one_of(data, "verdict", path, choices=COMPARISON_VERDICTS),
            reasons=text_list(data, "reasons", path, max_items=16, max_len=500, unique=False),
            deltas=ComparisonDeltas.parse(data.get("deltas"), f"{path}.deltas"),
            sample_sizes=SampleSizes.parse(data.get("sample_sizes"), f"{path}.sample_sizes"),
            comparable=comparable_raw,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline_run_id": self.baseline_run_id,
            "candidate_run_id": self.candidate_run_id,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "deltas": self.deltas.to_dict(),
            "sample_sizes": self.sample_sizes.to_dict(),
            "comparable": self.comparable,
        }


# ── MemoryBudget / CandidateEstimate / ContextLimits (INF-05 §11/§14) ───────
#
# CONTRATO_INF05 Lote A: a per-GPU memory budget split into named components
# (never one opaque "used" number), a candidate-load estimate that says
# explicitly when it is incomplete rather than guessing, and the three
# separately-tracked context limits §14 insists on (native / configured /
# evaluated — a RoPE/YaRN extension is never reported as if it were native).
# `src/memory_budget.py` is the only producer; this module only defines and
# validates the shape, same division of labor as every other contract here.

MEMORY_COMPONENT_SOURCES = ("observed", "reported_engine", "estimated", "manual", "absent")


@dataclass(frozen=True)
class MemoryComponent:
    """One named slice of a `MemoryBudget`/`CandidateEstimate` — never a
    bare number. `bytes=None` is "not observed", and it is never read as
    zero by a caller (§11: "lo que no se observa es absent/unknown, nunca
    0")."""

    bytes: Optional[int] = None
    source: str = "absent"
    note: str = ""

    _KEYS = ("bytes", "source", "note")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "MemoryComponent":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            bytes=whole(data, "bytes", path, minimum=0),
            source=one_of(data, "source", path, choices=MEMORY_COMPONENT_SOURCES, required=False, default="absent"),
            note=text(data, "note", path, required=False, default="", allow_blank=True, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"bytes": self.bytes, "source": self.source, "note": self.note}


_MEMORY_COMPONENT_NAMES = (
    "weights_resident", "kv_state", "buffers_runtime", "auxiliary_models",
    "other_processes", "system_margin", "free",
)


@dataclass(frozen=True)
class MemoryComponents:
    weights_resident: MemoryComponent = field(default_factory=MemoryComponent)
    kv_state: MemoryComponent = field(default_factory=MemoryComponent)
    buffers_runtime: MemoryComponent = field(default_factory=MemoryComponent)
    auxiliary_models: MemoryComponent = field(default_factory=MemoryComponent)
    other_processes: MemoryComponent = field(default_factory=MemoryComponent)
    system_margin: MemoryComponent = field(default_factory=MemoryComponent)
    free: MemoryComponent = field(default_factory=MemoryComponent)

    @classmethod
    def parse(cls, raw: Any, path: str) -> "MemoryComponents":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, _MEMORY_COMPONENT_NAMES, path)
        return cls(**{
            key: MemoryComponent.parse(data.get(key), f"{path}.{key}") for key in _MEMORY_COMPONENT_NAMES
        })

    def to_dict(self) -> Dict[str, Any]:
        return {key: getattr(self, key).to_dict() for key in _MEMORY_COMPONENT_NAMES}


CONSUMER_KINDS = ("ollama", "faustus_serve", "other_process")


@dataclass(frozen=True)
class MemoryConsumer:
    """One process/model attributed to a `MemoryBudget` — §11 T14: two
    endpoints on the same physical GPU are two consumers of ONE budget, not
    two budgets."""

    kind: str
    label: str
    pid: Optional[int] = None
    bytes: Optional[int] = None
    source: str = "absent"

    _KEYS = ("kind", "label", "pid", "bytes", "source")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "MemoryConsumer":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            kind=one_of(data, "kind", path, choices=CONSUMER_KINDS),
            label=text(data, "label", path, max_len=256),
            pid=whole(data, "pid", path, minimum=0),
            bytes=whole(data, "bytes", path, minimum=0),
            source=one_of(data, "source", path, choices=MEMORY_COMPONENT_SOURCES, required=False, default="absent"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "pid": self.pid,
                "bytes": self.bytes, "source": self.source}


@dataclass(frozen=True)
class MemoryBudget:
    """The desegregated memory picture for ONE physical GPU (`gpu_key` from
    `identity_key`, never an index). `stale=True` means `observed_at` is
    older than the caller's freshness window — a stale budget is never a
    reason to say a load fits (§11: "una lectura obsoleta no autoriza una
    carga arriesgada"; enforced by `memory_budget.admissible`, not here)."""

    gpu_key: Optional[str]
    gpu_index: Optional[int]
    gpu_name: str
    total_bytes: Optional[int]
    observed_at: Optional[str]
    components: MemoryComponents
    consumers: Tuple[MemoryConsumer, ...]
    shared_spill: MemoryComponent
    stale: bool = False
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("gpu_key", "gpu_index", "gpu_name", "total_bytes", "observed_at",
              "components", "consumers", "shared_spill", "stale", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "budget") -> "MemoryBudget":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        consumers_raw = data.get("consumers") or []
        if not isinstance(consumers_raw, (list, tuple)):
            raise ContractError(f"{path}.consumers", "expected a list", got=consumers_raw)
        stale_raw = data.get("stale")
        if stale_raw is not None and not isinstance(stale_raw, bool):
            raise ContractError(f"{path}.stale", "expected true or false", got=stale_raw)
        return cls(
            gpu_key=text(data, "gpu_key", path, required=False, default=None, allow_blank=False) or None,
            gpu_index=whole(data, "gpu_index", path, minimum=0),
            gpu_name=text(data, "gpu_name", path, required=False, default="", allow_blank=True, max_len=256),
            total_bytes=whole(data, "total_bytes", path, minimum=0),
            observed_at=timestamp(data, "observed_at", path),
            components=MemoryComponents.parse(data.get("components"), f"{path}.components"),
            consumers=tuple(MemoryConsumer.parse(c, f"{path}.consumers[{i}]") for i, c in enumerate(consumers_raw)),
            shared_spill=MemoryComponent.parse(data.get("shared_spill"), f"{path}.shared_spill"),
            stale=bool(stale_raw) if stale_raw is not None else False,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "gpu_key": self.gpu_key,
            "gpu_index": self.gpu_index,
            "gpu_name": self.gpu_name,
            "total_bytes": self.total_bytes,
            "observed_at": self.observed_at,
            "components": self.components.to_dict(),
            "consumers": [c.to_dict() for c in self.consumers],
            "shared_spill": self.shared_spill.to_dict(),
            "stale": self.stale,
        }


# ── CandidateEstimate (§11 "presupuesto de memoria desglosado") ────────────

CANDIDATE_BASES = ("single_observation", "fitted", "metadata", "incomplete")


@dataclass(frozen=True)
class EstimateValidity:
    """The domain a `CandidateEstimate` is actually valid over — a
    `single_observation`'s `ctx_min == ctx_max` is the one context it was
    measured at; a `fitted` estimate's wider range is the span of the
    observations the fit came from. `None` on the estimate itself means
    "no stated domain" (e.g. `basis: incomplete`)."""

    ctx_min: Optional[int] = None
    ctx_max: Optional[int] = None
    slots: Optional[int] = None

    _KEYS = ("ctx_min", "ctx_max", "slots")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "EstimateValidity":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            ctx_min=whole(data, "ctx_min", path, minimum=0),
            ctx_max=whole(data, "ctx_max", path, minimum=0),
            slots=whole(data, "slots", path, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"ctx_min": self.ctx_min, "ctx_max": self.ctx_max, "slots": self.slots}


@dataclass(frozen=True)
class CandidateEstimate:
    """§11: "una arquitectura desconocida produce una estimación INCOMPLETA,
    no falsa precisión" — `complete=False` + `basis="incomplete"` is the
    honest answer for an unknown architecture with no measured KV
    observation, not a number dressed up as a measurement."""

    weights: MemoryComponent
    kv_state: MemoryComponent
    buffers: MemoryComponent
    margin: MemoryComponent
    total_lower: Optional[int]
    total_upper: Optional[int]
    complete: bool
    basis: str
    validity: Optional[EstimateValidity] = None
    notes: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("weights", "kv_state", "buffers", "margin", "total_lower", "total_upper",
              "complete", "basis", "validity", "notes", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "estimate") -> "CandidateEstimate":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        complete_raw = data.get("complete")
        if not isinstance(complete_raw, bool):
            raise ContractError(f"{path}.complete", "expected true or false", got=complete_raw)
        validity_raw = data.get("validity")
        return cls(
            weights=MemoryComponent.parse(data.get("weights"), f"{path}.weights"),
            kv_state=MemoryComponent.parse(data.get("kv_state"), f"{path}.kv_state"),
            buffers=MemoryComponent.parse(data.get("buffers"), f"{path}.buffers"),
            margin=MemoryComponent.parse(data.get("margin"), f"{path}.margin"),
            total_lower=whole(data, "total_lower", path, minimum=0),
            total_upper=whole(data, "total_upper", path, minimum=0),
            complete=complete_raw,
            basis=one_of(data, "basis", path, choices=CANDIDATE_BASES),
            validity=EstimateValidity.parse(validity_raw, f"{path}.validity") if validity_raw is not None else None,
            notes=text_list(data, "notes", path, max_items=16, max_len=500, unique=False),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "weights": self.weights.to_dict(),
            "kv_state": self.kv_state.to_dict(),
            "buffers": self.buffers.to_dict(),
            "margin": self.margin.to_dict(),
            "total_lower": self.total_lower,
            "total_upper": self.total_upper,
            "complete": self.complete,
            "basis": self.basis,
            "validity": self.validity.to_dict() if self.validity is not None else None,
            "notes": list(self.notes),
        }


# ── ContextLimits (§14 "tres límites de contexto") ──────────────────────────
#
# Three numbers that must never collapse into one: what the model was
# TRAINED with (`native` — the ORIGINAL max_position_embeddings when RoPE/
# YaRN extends it further; extension is annotated, never presented as
# native), what the running engine is currently SET to (`configured`), and
# the largest prompt a benchmark run has actually EXERCISED (`evaluated`).

CONTEXT_NATIVE_SOURCES = ("hf_config", "ollama_show", "absent")
CONTEXT_CONFIGURED_SOURCES = ("receipt", "load_options", "absent")
CONTEXT_EVALUATED_SOURCES = ("bench_runs", "absent")


@dataclass(frozen=True)
class NativeContextLimit:
    value: Optional[int] = None
    source: str = "absent"
    note: str = ""

    _KEYS = ("value", "source", "note")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "NativeContextLimit":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            value=whole(data, "value", path, minimum=0),
            source=one_of(data, "source", path, choices=CONTEXT_NATIVE_SOURCES, required=False, default="absent"),
            note=text(data, "note", path, required=False, default="", allow_blank=True, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source, "note": self.note}


@dataclass(frozen=True)
class ConfiguredContextLimit:
    value: Optional[int] = None
    source: str = "absent"
    note: str = ""

    _KEYS = ("value", "source", "note")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "ConfiguredContextLimit":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            value=whole(data, "value", path, minimum=0),
            source=one_of(data, "source", path, choices=CONTEXT_CONFIGURED_SOURCES, required=False, default="absent"),
            note=text(data, "note", path, required=False, default="", allow_blank=True, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "source": self.source, "note": self.note}


@dataclass(frozen=True)
class EvaluatedContextLimit:
    min: Optional[int] = None
    max: Optional[int] = None
    source: str = "absent"
    note: str = ""

    _KEYS = ("min", "max", "source", "note")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "EvaluatedContextLimit":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            min=whole(data, "min", path, minimum=0),
            max=whole(data, "max", path, minimum=0),
            source=one_of(data, "source", path, choices=CONTEXT_EVALUATED_SOURCES, required=False, default="absent"),
            note=text(data, "note", path, required=False, default="", allow_blank=True, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"min": self.min, "max": self.max, "source": self.source, "note": self.note}


@dataclass(frozen=True)
class ContextLimits:
    native: NativeContextLimit = field(default_factory=NativeContextLimit)
    configured: ConfiguredContextLimit = field(default_factory=ConfiguredContextLimit)
    evaluated: EvaluatedContextLimit = field(default_factory=EvaluatedContextLimit)

    _KEYS = ("native", "configured", "evaluated")

    @classmethod
    def parse(cls, raw: Any, path: str = "context_limits") -> "ContextLimits":
        data = as_mapping(raw if raw is not None else {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            native=NativeContextLimit.parse(data.get("native"), f"{path}.native"),
            configured=ConfiguredContextLimit.parse(data.get("configured"), f"{path}.configured"),
            evaluated=EvaluatedContextLimit.parse(data.get("evaluated"), f"{path}.evaluated"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "native": self.native.to_dict(),
            "configured": self.configured.to_dict(),
            "evaluated": self.evaluated.to_dict(),
        }
