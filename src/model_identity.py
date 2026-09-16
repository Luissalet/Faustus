"""Model identity vs. deployment identity (MOD-01, MOD-02, MOD-19).

`docs/spec/creator/plan/docs/04_MODELOS_Y_RECURSOS.md` describes three levels
that must never be blended into one row: **weights** (a digest — the same
weights under two tags share this), **deployment** (that digest served by a
specific engine build, at a specific endpoint, with a specific effective
configuration — two servers of the "same" model can disagree on tool calling,
context, or memory), and **capability evidence** (what is actually known
about a given deployment: declared, inferred, probed or measured, dated and
sourced, never silently promoted to a stronger claim than it earned).

This module is purely additive next to the four authorities `docs/spec/
creator/00_WP00_INVENTARIO.md` §3 confirms are still current: it does not
replace `src/model_capabilities.py` (vocabulary), `src/model_calibration.py`
(the existing `ollama|digest:<digest>` manifest store — untouched, still
read the same way), `src/model_load_options.py` (saved num_ctx/num_gpu/
keep_alive) or `src/vram_fit.py` (resource arithmetic). It reads all four and
adds the missing seam: an identity for *this exact deployment*, separate from
the identity of the weights it happens to be serving right now.

``resolve_deployment(endpoint_url, model)`` performs the only network I/O in
this module (Ollama's own ``/api/tags`` and ``/api/show``, through the
existing ``src/model_capability_readers/ollama.py`` normalizer — no new
payload parsing invented here). Tests never hit a real Ollama: they
monkeypatch the two module-level fetch functions, ``_fetch_tags`` and
``_fetch_show``.

Persistence (`ModelIdentityStore`, `DATA_DIR/creator/model_identity.db`) is a
sqlite CAS store shaped like `src/harness_evolution/store.py`: one file under
`DATA_DIR`, a connection opened and closed per call, WAL for concurrent
readers, plain `INSERT OR REPLACE`/`INSERT` (no cross-deployment CAS is
needed here — a deployment's identity is a pure function of its inputs, so
two callers computing the same `deployment_id` write the same row; only
capability evidence appends).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

import httpx

from src import model_capabilities as mc
from src.model_capability_readers import base as mcr_base
from src.model_capability_readers import ollama as ollama_reader

# ── evidence levels ──────────────────────────────────────────────────────
#
# Resolution order when several sources disagree about the same capability
# on the same deployment (04_MODELOS_Y_RECURSOS.md, "Fuentes de metadatos y
# resolución de conflictos"): measured beats probed beats inferred beats
# declared. Nothing here discards a weaker source — `list_evidence` keeps
# every entry; only `resolve_capability` picks the strongest by this order,
# and ties break on the newer `observed_at`.

LEVEL_DECLARED = "declared"
LEVEL_INFERRED = "inferred"
LEVEL_PROBED = "probed"
LEVEL_MEASURED = "measured"

LEVELS: Sequence[str] = (LEVEL_DECLARED, LEVEL_INFERRED, LEVEL_PROBED, LEVEL_MEASURED)
_LEVEL_RANK: Dict[str, int] = {level: rank for rank, level in enumerate(LEVELS)}


def level_rank(level: str) -> int:
    """Higher wins. An unknown level ranks below `declared` (rank -1): a
    typo or a future level this build does not know about must never
    outrank a source this module actually understands the strength of."""
    return _LEVEL_RANK.get(str(level or "").strip().lower(), -1)


# Old evidence recorded before a deployment could be resolved (or with no
# endpoint/engine/config to resolve one from at all — see WP06's "migrar
# probes viejos a scope legacy_unknown") never gets a real deployment_id
# invented for it. It is filed under this fixed pseudo-scope instead, capped
# at LEVEL_INFERRED regardless of how the probe itself was run: an old result
# with insufficient identity is NOT promoted to `probed`/`measured` just
# because a probe suite produced it (MOD-19's closing criterion).
LEGACY_UNKNOWN_SCOPE = "legacy_unknown"
_MAX_LEVEL_WITHOUT_DEPLOYMENT = LEVEL_INFERRED


def legacy_deployment_id(manifest_key: str) -> str:
    """A stable pseudo-deployment id for evidence that only ever had the
    legacy `src/model_calibration.py` manifest key to go on — never a
    fabricated identity, always namespaced so it can never collide with (and
    never be confused for) a real `deployment_id` computed by
    `deployment_id_for`."""
    return f"{LEGACY_UNKNOWN_SCOPE}:{_sha256_hex(str(manifest_key or ''))}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── ModelSpec: identity of the weights/components, not of how they run ──────

@dataclass(frozen=True)
class ModelSpec:
    """Identity of pesos y componentes — MOD-01. Two deployments that serve
    the same ``digest`` share one ``model_spec_id`` even on different
    endpoints, different engines, or under different tags; a re-pulled blob
    (new digest) is a different ModelSpec, deliberately, the same rule
    `src/model_calibration.py::manifest_key` already applies to Ollama."""

    model_spec_id: str
    vendor: str
    family: str
    model_id: str
    aliases: tuple[str, ...] = ()
    digest: str = ""
    parameter_size: str = ""
    quantization: str = ""
    architecture: str = ""
    license: str = ""
    modalities_in: tuple[str, ...] = ()
    modalities_out: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    context_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_spec_id": self.model_spec_id,
            "vendor": self.vendor,
            "family": self.family,
            "model_id": self.model_id,
            "aliases": list(self.aliases),
            "digest": self.digest,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "architecture": self.architecture,
            "license": self.license,
            "modalities": {"in": list(self.modalities_in), "out": list(self.modalities_out)},
            "capabilities": list(self.capabilities),
            "context_tokens": self.context_tokens,
        }


def model_spec_id_for(*, vendor: str, digest: str = "", family: str = "", model_id: str = "") -> str:
    """A digest (the actual weights bytes) is the strongest identity there
    is: any tag pointing at it is the same ModelSpec. Without one (a vendor
    that never hands out a digest) the id falls back to vendor+family+name —
    weaker, but still stable across repeated calls for the same input."""
    vendor = str(vendor or "").strip().lower()
    digest = str(digest or "").strip()
    if digest:
        return f"{vendor}:digest:{digest}"
    fallback = _sha256_hex(_canonical_json([vendor, str(family or ""), str(model_id or "")]))[:32]
    return f"{vendor}:name:{fallback}"


# ── DeploymentManifest: how it is being served right now ────────────────────

@dataclass(frozen=True)
class EngineInfo:
    kind: str
    version: str = ""
    build_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "version": self.version, "build_digest": self.build_digest or None}


@dataclass(frozen=True)
class DeploymentManifest:
    """Identidad de ejecución — MOD-02. `deployment_id` is a pure function
    of (engine kind, engine version, the weights digest/revision actually
    loaded, and the effective configuration): change the engine, its build,
    or a config knob that affects behaviour (num_ctx, num_gpu, dtype,
    keep_alive, chat template…) and this is a *different* deployment,
    on purpose — old evidence must not silently carry over."""

    deployment_id: str
    model_spec_id: str
    weights_revision: str
    engine: EngineInfo
    endpoint_id: str
    endpoint_url: str
    configuration: Dict[str, Any] = field(default_factory=dict)
    configuration_fingerprint: str = ""
    resources: Dict[str, Any] = field(default_factory=dict)
    availability: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "model_spec_id": self.model_spec_id,
            "weights_revision": self.weights_revision,
            "engine": self.engine.to_dict(),
            "endpoint_id": self.endpoint_id,
            "endpoint_url": self.endpoint_url,
            "configuration": dict(self.configuration),
            "configuration_fingerprint": self.configuration_fingerprint,
            "resources": dict(self.resources),
            "availability": self.availability,
        }


def configuration_fingerprint(configuration: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical_json(dict(configuration or {})))


def deployment_id_for(
    *, engine_kind: str, engine_version: str, model_digest: str, configuration: Mapping[str, Any],
) -> str:
    """Same digest, same engine, same effective config → same id, every
    time (an idempotent CAS key, not a random uuid). Any of the three inputs
    changing changes the id — MOD-02's acceptance criterion: "cambiar el
    motor o template invalida pruebas incompatibles sin perder la ficha de
    pesos" (the ModelSpec survives; only the deployment row is a new one)."""
    fp = configuration_fingerprint(configuration)
    payload = "|".join([
        str(engine_kind or "").strip().lower(),
        str(engine_version or "").strip(),
        str(model_digest or "").strip(),
        fp,
    ])
    return _sha256_hex(payload)


# ── CapabilityEvidence: what is known about ONE deployment ──────────────────

@dataclass(frozen=True)
class CapabilityEvidence:
    evidence_id: str
    deployment_id: str
    capability: str
    level: str
    source: str
    observed_at: str
    conditions: Dict[str, Any] = field(default_factory=dict)
    method: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "deployment_id": self.deployment_id,
            "capability": self.capability,
            "level": self.level,
            "source": self.source,
            "observed_at": self.observed_at,
            "conditions": dict(self.conditions),
            "method": self.method,
        }


# ── network seam: the only I/O in this module, monkeypatched in tests ───────

_FETCH_TIMEOUT_S = 10.0


def _fetch_tags(root: str) -> Dict[str, Any]:
    with httpx.Client(timeout=_FETCH_TIMEOUT_S) as client:
        r = client.get(root.rstrip("/") + "/api/tags")
        r.raise_for_status()
        return r.json() or {}


def _fetch_show(root: str, model: str) -> Dict[str, Any]:
    with httpx.Client(timeout=_FETCH_TIMEOUT_S) as client:
        r = client.post(root.rstrip("/") + "/api/show", json={"model": model, "name": model})
        r.raise_for_status()
        return r.json() or {}


def _fetch_version(root: str) -> str:
    """Best-effort: not every Ollama build answers `/api/version`, and this
    is never allowed to fail `resolve_deployment` — an unknown engine
    version is a fact (`"unknown"`), not a 502."""
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_S) as client:
            r = client.get(root.rstrip("/") + "/api/version")
            r.raise_for_status()
            data = r.json() or {}
        return str(data.get("version") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _find_tag(tags_payload: Mapping[str, Any], model: str) -> Dict[str, Any]:
    from src.model_load_options import _model_matches  # local: avoids an import cycle at module load

    for item in (tags_payload or {}).get("models") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("model") or item.get("name") or "")
        if name and _model_matches(name, model):
            return dict(item)
    return {}


def _effective_configuration(endpoint_id: str, endpoint_url: str, model: str, show_summary: Mapping[str, Any]) -> Dict[str, Any]:
    """The knobs that actually change what a deployment can do: saved
    per-model options (num_ctx/num_gpu/keep_alive/main_gpu — MOD-04's own
    settings-only reader, no network) layered under whatever `/api/show`
    reports as the model's own default context, so a deployment with no
    saved override still gets a real `num_ctx` in its fingerprint instead of
    a hole that would make two genuinely different context windows collide
    on the same `deployment_id`."""
    configuration: Dict[str, Any] = {}
    try:
        from src import model_load_options as mlo
        saved = mlo.resolve_for_request(endpoint_url, model)
        configuration.update({k: v for k, v in dict(saved or {}).items() if v not in (None, "")})
    except Exception:  # noqa: BLE001 — settings I/O, never worth failing identity resolution over
        pass
    if "num_ctx" not in configuration:
        ctx = show_summary.get("context_length")
        if ctx:
            configuration["num_ctx"] = int(ctx)
    return configuration


def resolve_deployment(endpoint_url: str, model: str, *, endpoint_id: str = "") -> "Resolution":
    """ModelSpec + DeploymentManifest for `model` on the Ollama at
    `endpoint_url`, from what `/api/tags` and `/api/show` say right now — no
    model load, no mutation, no store write, no evidence write (the caller
    decides whether/when to persist via `ModelIdentityStore`). Uses
    `src/model_capability_readers/ollama.py` to normalize both payloads
    instead of re-parsing Ollama's wire format here.

    Returns a `Resolution(model_spec, deployment)` pair rather than the bare
    `DeploymentManifest` the ficha's arrow notation suggests: a route
    answering "what is this deployment" needs the ModelSpec too (MOD-01),
    and `DeploymentManifest` itself only carries `model_spec_id` — by design,
    matching `contracts/deployment-manifest.schema.json`, which has no room
    for the full spec inline."""
    endpoint_url = str(endpoint_url or "").rstrip("/")
    model = str(model or "").strip()
    if not endpoint_url or not model:
        raise ValueError("resolve_deployment needs both endpoint_url and model")

    try:
        tags_payload = _fetch_tags(endpoint_url)
    except Exception:  # noqa: BLE001 — an unreachable endpoint still resolves an identity, minus the digest
        tags_payload = {}
    tag = _find_tag(tags_payload, model)
    digest = str(tag.get("digest") or "")

    try:
        show_payload = _fetch_show(endpoint_url, model)
    except Exception:  # noqa: BLE001
        show_payload = {}

    record = ollama_reader.record_from_show_payload(
        model, show_payload, endpoint_id=endpoint_id, base_url=endpoint_url,
    )
    if record is None:
        record = mcr_base.ModelCapabilityRecord(
            vendor=mcr_base.VENDOR_OLLAMA, model_id=model,
            capability=mc.unknown_capability(source=mc.SOURCE_UNKNOWN),
        )
    capability = record.capability.to_dict()
    details = dict(show_payload.get("details") or {})
    limits = dict(capability.get("limits") or {})
    context_tokens = int(limits.get("context_tokens") or 0)
    modalities = capability.get("modalities") if isinstance(capability.get("modalities"), Mapping) else {}

    spec_id = model_spec_id_for(vendor="ollama", digest=digest, family=capability.get("family") or "", model_id=model)
    spec = ModelSpec(
        model_spec_id=spec_id,
        vendor="ollama",
        family=str(capability.get("family") or ""),
        model_id=model,
        aliases=(model,),
        digest=digest,
        parameter_size=str(details.get("parameter_size") or ""),
        quantization=str(details.get("quantization_level") or ""),
        architecture=str(details.get("family") or ""),
        license=str(show_payload.get("license") or "").strip()[:400],
        modalities_in=tuple(modalities.get("input") or ()),
        modalities_out=tuple(modalities.get("output") or ()),
        capabilities=tuple(capability.get("capabilities") or ()),
        context_tokens=context_tokens,
    )

    show_summary = {"context_length": context_tokens}
    configuration = _effective_configuration(endpoint_id, endpoint_url, model, show_summary)
    engine_version = _fetch_version(endpoint_url) or "unknown"
    dep_id = deployment_id_for(
        engine_kind="ollama", engine_version=engine_version, model_digest=digest or spec_id,
        configuration=configuration,
    )
    manifest = DeploymentManifest(
        deployment_id=dep_id,
        model_spec_id=spec_id,
        weights_revision=digest or "unknown",
        engine=EngineInfo(kind="ollama", version=engine_version),
        endpoint_id=endpoint_id,
        endpoint_url=endpoint_url,
        configuration=configuration,
        configuration_fingerprint=configuration_fingerprint(configuration),
        resources={},
        availability="discovered" if (tags_payload or show_payload) else "unknown",
    )
    return Resolution(spec, manifest)


@dataclass(frozen=True)
class Resolution:
    """`resolve_deployment`'s return shape: both halves, since a caller
    building a manifest needs the ModelSpec it points at too."""

    model_spec: ModelSpec
    deployment: DeploymentManifest

    def __iter__(self):
        yield self.model_spec
        yield self.deployment


# ── sqlite store: DATA_DIR/creator/model_identity.db ─────────────────────────

def _default_db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "model_identity.db")


_BUSY_TIMEOUT_S = 30


class ModelIdentityStore:
    """One sqlite file under `DATA_DIR/creator/`, opened and closed per call
    (`src/harness_evolution/store.py`'s pattern, CONTRATO rule 2): no
    long-lived handle to leak across a Windows process boundary, WAL so a
    reader (the routes) never blocks a writer (a calibration run recording
    evidence) for long."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_specs (
                    model_spec_id TEXT PRIMARY KEY,
                    vendor TEXT NOT NULL,
                    family TEXT,
                    model_id TEXT,
                    digest TEXT,
                    spec_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deployments (
                    deployment_id TEXT PRIMARY KEY,
                    model_spec_id TEXT NOT NULL,
                    endpoint_id TEXT,
                    endpoint_url TEXT,
                    engine_kind TEXT,
                    engine_version TEXT,
                    configuration_fingerprint TEXT,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    method TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_deployments_model_spec ON deployments(model_spec_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_evidence_deployment ON capability_evidence(deployment_id, capability)")

    # ── model specs / deployments (upsert: a pure function of its inputs) ──

    def upsert_model_spec(self, spec: ModelSpec) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM model_specs WHERE model_spec_id = ?", (spec.model_spec_id,)
            ).fetchone()
            created_at = row["created_at"] if row else now
            conn.execute(
                "INSERT OR REPLACE INTO model_specs "
                "(model_spec_id, vendor, family, model_id, digest, spec_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spec.model_spec_id, spec.vendor, spec.family, spec.model_id, spec.digest,
                 _canonical_json(spec.to_dict()), created_at, now),
            )

    def upsert_deployment(self, manifest: DeploymentManifest) -> None:
        now = _utcnow_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM deployments WHERE deployment_id = ?", (manifest.deployment_id,)
            ).fetchone()
            created_at = row["created_at"] if row else now
            conn.execute(
                "INSERT OR REPLACE INTO deployments "
                "(deployment_id, model_spec_id, endpoint_id, endpoint_url, engine_kind, engine_version, "
                " configuration_fingerprint, manifest_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (manifest.deployment_id, manifest.model_spec_id, manifest.endpoint_id, manifest.endpoint_url,
                 manifest.engine.kind, manifest.engine.version, manifest.configuration_fingerprint,
                 _canonical_json(manifest.to_dict()), created_at, now),
            )

    def get_model_spec(self, model_spec_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT spec_json FROM model_specs WHERE model_spec_id = ?", (model_spec_id,)
            ).fetchone()
            return json.loads(row["spec_json"]) if row else None

    def get_deployment(self, deployment_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
            return json.loads(row["manifest_json"]) if row else None

    def list_deployments(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT manifest_json FROM deployments ORDER BY updated_at DESC"
            ).fetchall()
            return [json.loads(r["manifest_json"]) for r in rows]

    # ── capability evidence (append-only) ───────────────────────────────────

    def add_evidence(
        self, *, deployment_id: str, capability: str, level: str, source: str,
        observed_at: Optional[str] = None, conditions: Optional[Mapping[str, Any]] = None, method: str = "",
    ) -> CapabilityEvidence:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS!r}, got {level!r}")
        if str(deployment_id or "").startswith(f"{LEGACY_UNKNOWN_SCOPE}:") and level_rank(level) > level_rank(_MAX_LEVEL_WITHOUT_DEPLOYMENT):
            level = _MAX_LEVEL_WITHOUT_DEPLOYMENT
        evidence = CapabilityEvidence(
            evidence_id=f"ev_{deployment_id[:12]}_{capability}_{int(time.time() * 1000)}",
            deployment_id=deployment_id,
            capability=str(capability),
            level=level,
            source=str(source or "unknown"),
            observed_at=observed_at or _utcnow_iso(),
            conditions=dict(conditions or {}),
            method=method,
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO capability_evidence "
                "(evidence_id, deployment_id, capability, level, source, observed_at, conditions_json, method, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence.evidence_id, evidence.deployment_id, evidence.capability, evidence.level,
                 evidence.source, evidence.observed_at, _canonical_json(evidence.conditions),
                 evidence.method, _utcnow_iso()),
            )
        return evidence

    def list_evidence(self, deployment_id: str, *, capability: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every entry, oldest first within a capability — callers that want
        the strongest current answer use `resolve_capability`, not this."""
        with self._conn() as conn:
            if capability:
                rows = conn.execute(
                    "SELECT * FROM capability_evidence WHERE deployment_id = ? AND capability = ? "
                    "ORDER BY observed_at ASC",
                    (deployment_id, capability),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM capability_evidence WHERE deployment_id = ? ORDER BY capability, observed_at ASC",
                    (deployment_id,),
                ).fetchall()
            return [_evidence_row_to_dict(r) for r in rows]

    def resolve_capability(self, deployment_id: str, capability: str) -> Optional[Dict[str, Any]]:
        """The strongest evidence on record for `capability` on this
        deployment: measured > probed > inferred > declared (04_MODELOS_Y_
        RECURSOS.md), the newest one winning a tie within the same level.
        `None` means no evidence at all — a genuinely unknown capability,
        never guessed as unsupported."""
        entries = self.list_evidence(deployment_id, capability=capability)
        if not entries:
            return None
        return max(entries, key=lambda e: (level_rank(e["level"]), e["observed_at"]))


def _evidence_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "evidence_id": row["evidence_id"],
        "deployment_id": row["deployment_id"],
        "capability": row["capability"],
        "level": row["level"],
        "source": row["source"],
        "observed_at": row["observed_at"],
        "conditions": json.loads(row["conditions_json"] or "{}"),
        "method": row["method"] or "",
    }


_default_store: Optional[ModelIdentityStore] = None
_default_store_lock = threading.Lock()


def default_store() -> ModelIdentityStore:
    """Process-wide singleton over the real `DATA_DIR` path — tests pass
    their own `ModelIdentityStore(db_path=...)` instead of calling this."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = ModelIdentityStore()
        return _default_store
