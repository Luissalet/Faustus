"""Model Explorer — WP08 (UX11, MOD07, MOD10, MOD11, MOD12, MOD16, MOD17).

Makes the technical detail already captured elsewhere useful for actually
*choosing and configuring* a model. This module invents no new identity, no
new capability vocabulary and no new VRAM arithmetic: it is a read-only
aggregator over four authorities CONTRATO.md names as the ones to never
duplicate:

* `src/model_identity.py` (WP06) — `ModelSpec` (the weights) and
  `DeploymentManifest` (how they are being served right now), plus whatever
  `CapabilityEvidence` is on file for the deployment.
* `src/creator/capabilities.py` (WP07) — the fixed-axis multimodal
  capability profile (known/announced/unsupported/unknown) built from that
  same evidence.
* `src/creator/params.py` (WP07) — the typed `(engine, task)` parameter
  contract, so a "ficha de modelo" can show what a form would actually ask
  for, not a universal control set.
* `src/vram_fit.py` + `routes/local_models_routes.py::fit_verdict` — the
  SAME VRAM verdict the model picker (`studio/src/screens/ModelPalette.tsx`,
  `studio/src/adapters/fit.ts::fitOf`) already computes. This module imports
  `fit_verdict` rather than re-deriving fit/tight/over bands, per CONTRATO
  rule 1 ("no crear autoridades paralelas") — the one new thing it adds is
  *aggregation*: pulling a deployment's own weight-file size (via
  `src.gpu_policy.model_sizes`, the same cache `src/model_router.py` and
  `src/workflow_cost_estimate.py` already read) and current VRAM headroom
  (via `src.gpu_shared_memory.vram_snapshot`) together into one footprint
  block, keyed to this exact deployment's endpoint.

`src/model_calibration.py`'s pre-WP06 manifest (`ollama|digest:<digest>`, or
name-keyed for non-Ollama vendors) is surfaced too, read-only, under
``legacy_calibration`` — the same bridge `model_identity.py`'s own
`LEGACY_UNKNOWN_SCOPE` acknowledges: older evidence that predates a real
`deployment_id` is real evidence, just weaker-scoped, and is shown as such
rather than silently dropped or silently promoted.

**Nothing here is invented.** A field with no evidence is the literal string
``"unknown"``, never a zero, an empty string standing in for "no", or an
estimate presented as a measurement (MOD-11's two closing criteria at once:
"comparar dos rutas marca unknown en vez de convertir campos vacíos en cero
o no" and "comparación no equipara estimado con medido"). Every numeric or
capability cell this module emits carries an explicit ``basis`` alongside
its value — ``measured`` | ``estimated`` | ``known`` | ``announced`` |
``unsupported`` | ``unknown`` — so a caller (the route, the Studio table)
never has to guess which kind of "we don't know" it is looking at.

No network I/O beyond what `gpu_policy.model_sizes` and
`gpu_shared_memory.vram_snapshot` already do on their own (both are
metadata reads — `/api/tags` and `nvidia-smi` — cached, never a model load);
this module resolves nothing itself and never calls `model_identity.
resolve_deployment`. It reads whatever `ModelIdentityStore` already has on
file. A deployment nobody has resolved yet simply does not appear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src import model_calibration as calib
from src import model_capabilities as mc
from src import model_identity as mi
from src.creator import capabilities as cap
from src.creator import params as pr

# ── cell "basis" vocabulary — shared by the single-entry footprint and by
# compare()'s diff cells. Distinct from cap.STATUS_* (which is specifically
# about capability evidence): this also covers plain numeric facts (a
# context window, a file size) that are not capability evidence at all. ────

BASIS_MEASURED = "measured"
BASIS_ESTIMATED = "estimated"
BASIS_KNOWN = cap.STATUS_KNOWN
BASIS_ANNOUNCED = cap.STATUS_ANNOUNCED
BASIS_UNSUPPORTED = cap.STATUS_UNSUPPORTED
BASIS_UNKNOWN = "unknown"

BASES = frozenset({BASIS_MEASURED, BASIS_ESTIMATED, BASIS_KNOWN, BASIS_ANNOUNCED,
                    BASIS_UNSUPPORTED, BASIS_UNKNOWN})

# TASK_* (src/model_capabilities.py) -> the AXIS_* (src/creator/capabilities.py)
# it is asking about, for compare()'s "does this deployment support the task
# I'm comparing for" row. Deliberately partial: TASK_EMBEDDINGS_CREATE,
# TASK_RERANK, TASK_CLASSIFY and TASK_MODERATE have no multimodal axis
# counterpart in WP07's fixed axis list, and are left unmapped rather than
# guessed at — compare() reports BASIS_UNKNOWN for those, not a made-up axis.
_TASK_TO_AXIS: Dict[str, str] = {
    mc.TASK_CHAT_COMPLETIONS: cap.AXIS_TEXT,
    mc.TASK_IMAGE_GENERATE: cap.AXIS_IMAGE_OUT,
    mc.TASK_IMAGE_EDIT: cap.AXIS_IMAGE_EDIT,
    mc.TASK_IMAGE_INPAINT: cap.AXIS_REGION_EDIT,
    mc.TASK_IMAGE_UPSCALE: cap.AXIS_UPSCALE,
    mc.TASK_IMAGE_CONTROLNET: cap.AXIS_CONTROLNET,
    mc.TASK_VIDEO_GENERATE: cap.AXIS_VIDEO_OUT,
    mc.TASK_VIDEO_EDIT: cap.AXIS_VIDEO_OUT,
    mc.TASK_AUDIO_TRANSCRIBE: cap.AXIS_ASR,
    mc.TASK_AUDIO_SYNTHESIZE: cap.AXIS_TTS,
    mc.TASK_MUSIC_GENERATE: cap.AXIS_MUSIC,
}


def axis_for_task(task: str) -> Optional[str]:
    return _TASK_TO_AXIS.get(str(task or "").strip().lower())


# ── footprint: weight-file size + the SAME VRAM verdict the picker uses ────

@dataclass(frozen=True)
class FootprintCell:
    """One deployment's weight-on-disk size and VRAM verdict, or the honest
    absence of either. ``basis`` is `BASIS_ESTIMATED` whenever a verdict is
    present: `fit_verdict`'s own docstring is explicit that this is "the
    file on disk", not a measurement of the resident footprint (that finer
    "measured" reading, from `vram_fit.kv_bytes_per_token_measured` against
    an actually-loaded model, is `routes/model_routes.py`'s per-request
    Vitals surface, not something this offline aggregator can see without
    loading the model itself — which it never does)."""

    size_bytes: int = 0
    basis: str = BASIS_UNKNOWN
    vram_state: str = ""  # 'fits' | 'tight' | 'over' | '' (no verdict)
    vram_note: str = ""
    reason: str = ""  # why there is no verdict, when there isn't one

    def to_dict(self) -> Dict[str, Any]:
        return {
            "size_bytes": self.size_bytes,
            "basis": self.basis,
            "vram_state": self.vram_state,
            "vram_note": self.vram_note,
            "reason": self.reason,
        }


def footprint_for_deployment(
    deployment: Mapping[str, Any],
    model_spec: Optional[Mapping[str, Any]],
) -> FootprintCell:
    engine_kind = str(((deployment.get("engine") or {}).get("kind")) or "")
    endpoint_url = str(deployment.get("endpoint_url") or "")
    if engine_kind != "ollama" or not endpoint_url:
        return FootprintCell(basis=BASIS_UNKNOWN, reason="not a local Ollama deployment: no verdict without a network round trip to a card we cannot see")

    try:
        from src import gpu_policy
    except Exception:  # noqa: BLE001
        return FootprintCell(basis=BASIS_UNKNOWN, reason="gpu_policy unavailable")

    aliases = tuple((model_spec or {}).get("aliases") or ()) or (str((model_spec or {}).get("model_id") or ""),)
    try:
        sizes = gpu_policy.model_sizes(endpoint_url)
    except Exception:  # noqa: BLE001
        return FootprintCell(basis=BASIS_UNKNOWN, reason="could not read /api/tags for this endpoint")

    size_bytes = 0
    for alias in aliases:
        if alias and alias in sizes:
            size_bytes = int(sizes[alias])
            break
    if size_bytes <= 0:
        return FootprintCell(basis=BASIS_UNKNOWN, reason="model not found on this endpoint's /api/tags")

    try:
        from src import gpu_shared_memory
        from src import vram_fit
        from routes.local_models_routes import fit_verdict
    except Exception:  # noqa: BLE001
        return FootprintCell(size_bytes=size_bytes, basis=BASIS_ESTIMATED, reason="VRAM verdict module unavailable")

    try:
        vram = gpu_shared_memory.vram_snapshot()
        if not vram.get("supported"):
            return FootprintCell(size_bytes=size_bytes, basis=BASIS_ESTIMATED,
                                  reason=str(vram.get("reason") or "no GPU visible on this machine"))
        pool = vram_fit.pool_budgets(vram, held_by_runner_bytes=0, others_bytes=0)
        verdict = fit_verdict(size_bytes, pool, clean=True)
    except Exception as exc:  # noqa: BLE001 — a VRAM read must never break the explorer
        return FootprintCell(size_bytes=size_bytes, basis=BASIS_ESTIMATED, reason=f"VRAM read failed: {exc}"[:200])

    if not verdict:
        return FootprintCell(size_bytes=size_bytes, basis=BASIS_ESTIMATED, reason="no verdict for this size/card combination")
    return FootprintCell(
        size_bytes=size_bytes,
        basis=BASIS_ESTIMATED,
        vram_state=str(verdict.get("state") or ""),
        vram_note=str(verdict.get("note") or ""),
    )


# ── legacy calibration bridge (read-only) ───────────────────────────────────

def legacy_calibration_for(model_spec: Optional[Mapping[str, Any]], deployment: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whatever `src/model_calibration.py` has on file for this weights/
    endpoint pair, by the SAME `manifest_key` it has always used — never
    rewritten, never reinterpreted (CONTRATO rule 1: "nunca reinterpretes
    datos guardados"). `None` when there is genuinely nothing on file (an
    empty dict from `get_manifest` with no announced/tested entries), so a
    caller can tell "no legacy record" apart from "an empty one exists"."""
    if not model_spec:
        return None
    vendor = str(model_spec.get("vendor") or "")
    model_id = str(model_spec.get("model_id") or "")
    digest = str(model_spec.get("digest") or "")
    endpoint_id = str((deployment or {}).get("endpoint_id") or "")
    if not vendor or not model_id:
        return None
    key = calib.manifest_key(vendor=vendor, model_id=model_id, endpoint_id=endpoint_id, digest=digest)
    manifest = calib.get_manifest(key)
    if not manifest.get("announced") and not manifest.get("tested"):
        return None
    return {"manifest_key": key, **manifest}


# ── param schemas relevant to this deployment's engine ─────────────────────

def param_schemas_for_engine(engine_kind: str) -> List[Dict[str, Any]]:
    """Every registered `(engine, task)` schema whose `engine` matches this
    deployment's engine kind — e.g. an `invoke`/`ace_step`/`wan` media
    deployment gets its real control set; an `ollama` chat deployment
    correctly gets none (params.py's registry is media-engine schemas only
    right now; chat has no registered contract to show here, and this
    module does not invent one)."""
    engine_kind = str(engine_kind or "").strip().lower()
    return [s.to_dict() for s in pr.list_schemas() if s.engine == engine_kind]


# ── the full "ficha de modelo" for one deployment ───────────────────────────

@dataclass(frozen=True)
class ModelExplorerEntry:
    deployment_id: str
    model_spec: Dict[str, Any] = field(default_factory=dict)
    deployment: Dict[str, Any] = field(default_factory=dict)
    capability_profile: Dict[str, Any] = field(default_factory=dict)
    param_schemas: List[Dict[str, Any]] = field(default_factory=list)
    footprint: Dict[str, Any] = field(default_factory=dict)
    legacy_calibration: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "model_spec": self.model_spec,
            "deployment": self.deployment,
            "capability_profile": self.capability_profile,
            "param_schemas": self.param_schemas,
            "footprint": self.footprint,
            "legacy_calibration": self.legacy_calibration,
        }


def entry_for_deployment(deployment_id: str, *, store: Optional[mi.ModelIdentityStore] = None) -> Optional[ModelExplorerEntry]:
    """The full ficha for one deployment, or `None` when this install has
    never resolved/recorded it — a 404 upstream at the route, never a
    fabricated empty entry."""
    store = store or mi.default_store()
    deployment_id = str(deployment_id or "").strip()
    if not deployment_id:
        return None
    deployment = store.get_deployment(deployment_id)
    if deployment is None:
        return None
    model_spec_id = str(deployment.get("model_spec_id") or "")
    model_spec = store.get_model_spec(model_spec_id) if model_spec_id else None

    profile = cap.capability_profile_for_deployment(deployment_id, store=store)
    engine_kind = str(((deployment.get("engine") or {}).get("kind")) or "")

    return ModelExplorerEntry(
        deployment_id=deployment_id,
        model_spec=dict(model_spec or {}),
        deployment=dict(deployment),
        capability_profile=profile.to_dict(),
        param_schemas=param_schemas_for_engine(engine_kind),
        footprint=footprint_for_deployment(deployment, model_spec).to_dict(),
        legacy_calibration=legacy_calibration_for(model_spec, deployment),
    )


def list_entries(*, store: Optional[mi.ModelIdentityStore] = None) -> List[Dict[str, Any]]:
    """One compact row per known deployment (no per-row footprint/legacy
    lookup — those are per-deployment reads, done lazily by
    `entry_for_deployment` when a row is actually opened), for the
    Explorer's list/filter view. Mirrors
    `cap.known_deployments_summary` (same no-network, no-model-load
    guarantee) but also carries the ModelSpec's own filterable fields
    (vendor, family, modalities, license) so the Studio can filter by task/
    capability/language without a second round trip per row."""
    store = store or mi.default_store()
    rows: List[Dict[str, Any]] = []
    for dep in store.list_deployments():
        deployment_id = str(dep.get("deployment_id") or "")
        model_spec_id = str(dep.get("model_spec_id") or "")
        model_spec = store.get_model_spec(model_spec_id) if model_spec_id else None
        profile = cap.capability_profile_for_deployment(deployment_id, store=store)
        rows.append({
            "deployment_id": deployment_id,
            "model_spec_id": model_spec_id,
            "vendor": (model_spec or {}).get("vendor", ""),
            "family": (model_spec or {}).get("family", ""),
            "model_id": (model_spec or {}).get("model_id", ""),
            "license": (model_spec or {}).get("license", ""),
            "context_tokens": (model_spec or {}).get("context_tokens", 0),
            "engine": dep.get("engine") or {},
            "endpoint_id": dep.get("endpoint_id", ""),
            "known_axes": list(profile.known_axes()),
            "announced_axes": list(profile.announced_axes()),
        })
    return rows


# ── compare(): a cell-by-cell diff table, never inventing a value ──────────

@dataclass(frozen=True)
class CompareCell:
    value: Any
    basis: str

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "basis": self.basis}


@dataclass(frozen=True)
class CompareRow:
    field: str
    label: str
    cells: Dict[str, CompareCell]

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "label": self.label, "cells": {k: v.to_dict() for k, v in self.cells.items()}}


@dataclass(frozen=True)
class CompareTable:
    task: str
    deployment_ids: Tuple[str, ...]
    rows: Tuple[CompareRow, ...]
    unknown_deployment_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "deployment_ids": list(self.deployment_ids),
            "rows": [r.to_dict() for r in self.rows],
            "unknown_deployment_ids": list(self.unknown_deployment_ids),
        }


_ROW_LABELS: Dict[str, str] = {
    "task_support": "Task support",
    "context_tokens": "Context window (tokens)",
    "license": "License",
    "parameter_size": "Parameter size",
    "quantization": "Quantization",
    "footprint_bytes": "Weights on disk",
    "vram_verdict": "VRAM verdict",
    "param_contract": "Parameter contract registered",
}


def compare(deployment_ids: Sequence[str], task: str, *, store: Optional[mi.ModelIdentityStore] = None) -> CompareTable:
    """A cell-by-cell diff table across 2-3 deployments for one task.

    Every cell is `{value, basis}`. `basis` is one of `BASES`; an unresolved
    field is ALWAYS `{"value": None, "basis": "unknown"}` — never a zero, an
    empty string standing in for "unsupported", or an estimate relabeled as
    a measurement (MOD-11's two closing criteria). A `deployment_id` this
    install has no record of at all gets `unknown` in every cell and is
    additionally listed in `unknown_deployment_ids`, so a caller can tell
    "we know this deployment but not this field" apart from "we have never
    heard of this deployment"."""
    store = store or mi.default_store()
    ids = tuple(str(d or "").strip() for d in deployment_ids if str(d or "").strip())
    task = str(task or "").strip()
    axis = axis_for_task(task)

    entries: Dict[str, Optional[ModelExplorerEntry]] = {d: entry_for_deployment(d, store=store) for d in ids}
    unknown_ids = tuple(d for d, e in entries.items() if e is None)

    def _cell_from_axis(entry: Optional[ModelExplorerEntry]) -> CompareCell:
        if entry is None or axis is None:
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        axis_ev = (entry.capability_profile.get("axes") or {}).get(axis)
        if not axis_ev:
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        status = axis_ev.get("status", BASIS_UNKNOWN)
        return CompareCell(value=status, basis=status)

    def _cell_from_spec(entry: Optional[ModelExplorerEntry], key: str) -> CompareCell:
        if entry is None:
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        value = entry.model_spec.get(key)
        if value in (None, "", 0, ()):
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        return CompareCell(value=value, basis=BASIS_ANNOUNCED)

    def _cell_from_footprint(entry: Optional[ModelExplorerEntry], key: str) -> CompareCell:
        if entry is None:
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        fp = entry.footprint or {}
        basis = fp.get("basis", BASIS_UNKNOWN)
        if key == "footprint_bytes":
            value = fp.get("size_bytes") or None
            return CompareCell(value=value, basis=basis if value else BASIS_UNKNOWN)
        if key == "vram_verdict":
            value = fp.get("vram_state") or None
            return CompareCell(value=value, basis=basis if value else BASIS_UNKNOWN)
        return CompareCell(value=None, basis=BASIS_UNKNOWN)

    def _cell_param_contract(entry: Optional[ModelExplorerEntry]) -> CompareCell:
        if entry is None:
            return CompareCell(value=None, basis=BASIS_UNKNOWN)
        has = any(s.get("task") == task for s in entry.param_schemas)
        return CompareCell(value=bool(has), basis=BASIS_KNOWN if has else BASIS_UNKNOWN)

    rows: List[CompareRow] = []
    rows.append(CompareRow(field="task_support", label=_ROW_LABELS["task_support"],
                            cells={d: _cell_from_axis(entries[d]) for d in ids}))
    rows.append(CompareRow(field="context_tokens", label=_ROW_LABELS["context_tokens"],
                            cells={d: _cell_from_spec(entries[d], "context_tokens") for d in ids}))
    rows.append(CompareRow(field="license", label=_ROW_LABELS["license"],
                            cells={d: _cell_from_spec(entries[d], "license") for d in ids}))
    rows.append(CompareRow(field="parameter_size", label=_ROW_LABELS["parameter_size"],
                            cells={d: _cell_from_spec(entries[d], "parameter_size") for d in ids}))
    rows.append(CompareRow(field="quantization", label=_ROW_LABELS["quantization"],
                            cells={d: _cell_from_spec(entries[d], "quantization") for d in ids}))
    rows.append(CompareRow(field="footprint_bytes", label=_ROW_LABELS["footprint_bytes"],
                            cells={d: _cell_from_footprint(entries[d], "footprint_bytes") for d in ids}))
    rows.append(CompareRow(field="vram_verdict", label=_ROW_LABELS["vram_verdict"],
                            cells={d: _cell_from_footprint(entries[d], "vram_verdict") for d in ids}))
    rows.append(CompareRow(field="param_contract", label=_ROW_LABELS["param_contract"],
                            cells={d: _cell_param_contract(entries[d]) for d in ids}))

    return CompareTable(task=task, deployment_ids=ids, rows=tuple(rows), unknown_deployment_ids=unknown_ids)


__all__ = [
    "BASIS_MEASURED", "BASIS_ESTIMATED", "BASIS_KNOWN", "BASIS_ANNOUNCED",
    "BASIS_UNSUPPORTED", "BASIS_UNKNOWN", "BASES",
    "axis_for_task",
    "FootprintCell", "footprint_for_deployment",
    "legacy_calibration_for",
    "param_schemas_for_engine",
    "ModelExplorerEntry", "entry_for_deployment", "list_entries",
    "CompareCell", "CompareRow", "CompareTable", "compare",
]
