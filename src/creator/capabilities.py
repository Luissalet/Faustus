"""Multimodal capabilities per deployment — WP07 (MOD-03, MOD-04, MOD-05,
MOD-08, MOD-20, QA06).

This module answers one question per deployment: "what multimodal
operations does THIS deployment actually support, and how well is that
known?" — never "what does a model family usually do."

It is a pure reader/composer over two authorities that already exist and
that CONTRATO.md forbids re-implementing:

* `src/model_identity.py` — `ModelIdentityStore`, whose `CapabilityEvidence`
  rows are keyed by `deployment_id` (not by a bare model name) and carry an
  evidence `level`: `declared` < `inferred` < `probed` < `measured`
  (weakest to strongest). This module reuses those four levels verbatim; it
  does not invent a second evidence scale.
* `src/model_capabilities.py` — the canonical `CAP_*` vocabulary (vision,
  image generation/editing/inpainting, audio in/out, video generation, TTS,
  transcription, plus WP07's own additive `CAP_VIDEO_INPUT`,
  `CAP_MUSIC_GENERATION`, `CAP_REGION_EDITING`, `CAP_CONTROLNET_CONDITIONING`,
  `CAP_UPSCALING`) and `normalize_capability` for aliases.

**The one rule this module exists to enforce** (WP07's closing criterion:
"no se deriva edición o generación del mero soporte de visión"): a
capability profile reports exactly the capabilities it has evidence for —
`CAP_VISION` verified never implies `CAP_IMAGE_GENERATION` or
`CAP_IMAGE_EDITING`, `CAP_IMAGE_GENERATION` never implies
`CAP_REGION_EDITING` or `CAP_UPSCALING`. Every entry in
`MultimodalCapabilityProfile.axes` is independent; nothing here derives one
axis from another. `unknown` is a first-class state (see `STATUS_UNKNOWN`
below), never silently treated as `unsupported` or as a pass.

No network I/O, no model load. Building a profile is a handful of sqlite
reads through `ModelIdentityStore` (already open/close-per-call, WAL) plus
in-memory composition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src import model_capabilities as mc
from src import model_identity as mi

# ── the multimodal capability axes this module reports on ───────────────────
#
# One entry per axis named in the WP07 ficha: "texto, visión-in, imagen-out,
# audio-in/out, vídeo-in/out, música, TTS, ASR, edición por regiones,
# control-net/conditioning, upscaling". Each maps to exactly one `CAP_*`
# token from `src/model_capabilities.py` (existing or WP07-additive) — no
# parallel vocabulary is created here, per CONTRATO.md.

AXIS_TEXT = "text"
AXIS_VISION_IN = "vision_in"
AXIS_IMAGE_OUT = "image_out"
AXIS_IMAGE_EDIT = "image_edit"
AXIS_REGION_EDIT = "region_edit"
AXIS_CONTROLNET = "controlnet"
AXIS_UPSCALE = "upscale"
AXIS_AUDIO_IN = "audio_in"
AXIS_AUDIO_OUT = "audio_out"
AXIS_VIDEO_IN = "video_in"
AXIS_VIDEO_OUT = "video_out"
AXIS_MUSIC = "music"
AXIS_TTS = "tts"
AXIS_ASR = "asr"

# `AXIS_TEXT` has no `CAP_*` counterpart (plain chat is the family default,
# not a capability flag) — it is reported from `ModelSpec.modalities_in`
# directly, see `_text_axis_evidence` below.
_AXIS_TO_CAPABILITY: Dict[str, str] = {
    AXIS_VISION_IN: mc.CAP_VISION,
    AXIS_IMAGE_OUT: mc.CAP_IMAGE_GENERATION,
    AXIS_IMAGE_EDIT: mc.CAP_IMAGE_EDITING,
    AXIS_REGION_EDIT: mc.CAP_REGION_EDITING,
    AXIS_CONTROLNET: mc.CAP_CONTROLNET_CONDITIONING,
    AXIS_UPSCALE: mc.CAP_UPSCALING,
    AXIS_AUDIO_IN: mc.CAP_AUDIO_INPUT,
    AXIS_AUDIO_OUT: mc.CAP_AUDIO_OUTPUT,
    AXIS_VIDEO_IN: mc.CAP_VIDEO_INPUT,
    AXIS_VIDEO_OUT: mc.CAP_VIDEO_GENERATION,
    AXIS_MUSIC: mc.CAP_MUSIC_GENERATION,
    AXIS_TTS: mc.CAP_TTS,
    AXIS_ASR: mc.CAP_TRANSCRIPTION,
}

AXES: Tuple[str, ...] = (
    AXIS_TEXT,
    AXIS_VISION_IN,
    AXIS_IMAGE_OUT,
    AXIS_IMAGE_EDIT,
    AXIS_REGION_EDIT,
    AXIS_CONTROLNET,
    AXIS_UPSCALE,
    AXIS_AUDIO_IN,
    AXIS_AUDIO_OUT,
    AXIS_VIDEO_IN,
    AXIS_VIDEO_OUT,
    AXIS_MUSIC,
    AXIS_TTS,
    AXIS_ASR,
)

# ── "known vs announced" — WP07's own status vocabulary, over model_identity's
# evidence levels (declared/inferred/probed/measured) rather than a rename of
# them: a route or the Studio wants a 3-way read (known/announced/unknown),
# `model_identity` keeps its own 4-way strength scale for anyone finer-
# grained. `STATUS_UNSUPPORTED` is reserved for a future explicit negative
# evidence row (`level=measured`/`probed` recorded to say "tried, failed") —
# `resolve_capability` never returns that guess on its own; absence of
# evidence is always `STATUS_UNKNOWN`, never demoted to unsupported.

STATUS_KNOWN = "known"          # measured or probed: this deployment was actually tested.
STATUS_ANNOUNCED = "announced"  # declared or inferred: claimed, not verified here.
STATUS_UNSUPPORTED = "unsupported"  # explicit negative evidence on record.
STATUS_UNKNOWN = "unknown"      # no evidence at all.

STATUSES = frozenset({STATUS_KNOWN, STATUS_ANNOUNCED, STATUS_UNSUPPORTED, STATUS_UNKNOWN})

# A negative observation is recorded the same way as a positive one (an
# evidence row with this capability, at whatever level backs the check), the
# condition dict carries `{"supported": false}`. `_status_for_evidence` reads
# that flag; its absence (or True) means a positive observation.
CONDITION_SUPPORTED_KEY = "supported"

_LEVEL_TO_STATUS: Dict[str, str] = {
    mi.LEVEL_MEASURED: STATUS_KNOWN,
    mi.LEVEL_PROBED: STATUS_KNOWN,
    mi.LEVEL_INFERRED: STATUS_ANNOUNCED,
    mi.LEVEL_DECLARED: STATUS_ANNOUNCED,
}


@dataclass(frozen=True)
class CapabilityAxisEvidence:
    """What is known about ONE axis of ONE deployment — the atomic unit
    `MultimodalCapabilityProfile` is built from. `status` is the 3-way
    known/announced/unknown/unsupported read; `level`/`source`/`observed_at`
    carry the underlying `model_identity` evidence verbatim so a caller that
    wants the finer 4-way scale (or an audit trail) does not have to re-query
    the store."""

    axis: str
    capability: str
    status: str = STATUS_UNKNOWN
    level: str = ""
    source: str = ""
    observed_at: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)
    method: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "capability": self.capability,
            "status": self.status,
            "level": self.level,
            "source": self.source,
            "observed_at": self.observed_at,
            "conditions": dict(self.conditions),
            "method": self.method,
        }


@dataclass(frozen=True)
class MultimodalCapabilityProfile:
    """Every axis for one deployment — the shape `routes/creator_capability_
    routes.py` serializes for `GET /api/creator/capabilities/{deployment_id}`.
    `axes` always has one entry per `AXES` member, even when evidence is
    entirely absent (`STATUS_UNKNOWN`) — a caller (or the Studio) can always
    iterate the full, fixed axis list without checking for missing keys."""

    deployment_id: str
    model_spec_id: str = ""
    axes: Dict[str, CapabilityAxisEvidence] = field(default_factory=dict)

    def known_axes(self) -> Tuple[str, ...]:
        return tuple(axis for axis, ev in self.axes.items() if ev.status == STATUS_KNOWN)

    def announced_axes(self) -> Tuple[str, ...]:
        return tuple(axis for axis, ev in self.axes.items() if ev.status == STATUS_ANNOUNCED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "model_spec_id": self.model_spec_id,
            "axes": {axis: ev.to_dict() for axis, ev in self.axes.items()},
        }


def _status_for_evidence(entry: Optional[Mapping[str, Any]]) -> Tuple[str, str, str, str, Dict[str, Any], str]:
    """`(status, level, source, observed_at, conditions, method)` for one
    `ModelIdentityStore.resolve_capability` result — `None` means no evidence
    at all, i.e. `STATUS_UNKNOWN`, never guessed as unsupported."""
    if not entry:
        return STATUS_UNKNOWN, "", "", "", {}, ""
    level = str(entry.get("level") or "")
    conditions = entry.get("conditions") if isinstance(entry.get("conditions"), Mapping) else {}
    if conditions.get(CONDITION_SUPPORTED_KEY) is False:
        status = STATUS_UNSUPPORTED
    else:
        status = _LEVEL_TO_STATUS.get(level, STATUS_UNKNOWN)
    return (
        status,
        level,
        str(entry.get("source") or ""),
        str(entry.get("observed_at") or ""),
        dict(conditions),
        str(entry.get("method") or ""),
    )


def _text_axis_evidence(model_spec: Optional[Mapping[str, Any]]) -> CapabilityAxisEvidence:
    """Text is not a `CAP_*` flag (every chat-family model has it by
    default), so it is read straight from `ModelSpec.modalities_in` — the
    same identity evidence WP06 already resolved, not a fresh guess."""
    modalities = (model_spec or {}).get("modalities") if isinstance(model_spec, Mapping) else None
    has_text = isinstance(modalities, Mapping) and "text" in (modalities.get("in") or ())
    status = STATUS_KNOWN if has_text else STATUS_UNKNOWN
    return CapabilityAxisEvidence(axis=AXIS_TEXT, capability="", status=status, source="model_identity")


def capability_profile_for_deployment(
    deployment_id: str,
    *,
    store: Optional[mi.ModelIdentityStore] = None,
) -> MultimodalCapabilityProfile:
    """Build the full, fixed-axis profile for `deployment_id` from whatever
    `ModelIdentityStore` evidence exists right now. Reads only — never
    resolves a deployment over the network and never loads a model
    (MOD-03/MOD-04's acceptance: a model that reads images does not
    automatically receive image-generation or editing; this function simply
    has no code path that would let it)."""
    store = store or mi.default_store()
    deployment_id = str(deployment_id or "").strip()
    if not deployment_id:
        raise ValueError("deployment_id is required")

    deployment = store.get_deployment(deployment_id)
    model_spec_id = str((deployment or {}).get("model_spec_id") or "")
    model_spec = store.get_model_spec(model_spec_id) if model_spec_id else None

    axes: Dict[str, CapabilityAxisEvidence] = {AXIS_TEXT: _text_axis_evidence(model_spec)}
    for axis, capability in _AXIS_TO_CAPABILITY.items():
        entry = store.resolve_capability(deployment_id, capability)
        status, level, source, observed_at, conditions, method = _status_for_evidence(entry)
        axes[axis] = CapabilityAxisEvidence(
            axis=axis,
            capability=capability,
            status=status,
            level=level,
            source=source,
            observed_at=observed_at,
            conditions=conditions,
            method=method,
        )
    return MultimodalCapabilityProfile(deployment_id=deployment_id, model_spec_id=model_spec_id, axes=axes)


def record_evidence(
    *,
    deployment_id: str,
    axis: str,
    level: str,
    source: str,
    supported: bool = True,
    observed_at: Optional[str] = None,
    conditions: Optional[Mapping[str, Any]] = None,
    method: str = "",
    store: Optional[mi.ModelIdentityStore] = None,
) -> mi.CapabilityEvidence:
    """Write one evidence row for `axis` on `deployment_id`, through
    `ModelIdentityStore.add_evidence` (append-only, the sole write path —
    this module never opens its own table). `axis` must be one of `AXES`
    that has a `CAP_*` counterpart (`AXIS_TEXT` has none and is derived from
    `ModelSpec`, not recorded here). `supported=False` records a proven
    negative (`STATUS_UNSUPPORTED` on read), never silently dropped."""
    store = store or mi.default_store()
    capability = _AXIS_TO_CAPABILITY.get(axis)
    if not capability:
        raise ValueError(f"axis {axis!r} has no recordable capability (AXIS_TEXT is derived, not recorded)")
    merged_conditions = dict(conditions or {})
    merged_conditions[CONDITION_SUPPORTED_KEY] = bool(supported)
    return store.add_evidence(
        deployment_id=deployment_id,
        capability=capability,
        level=level,
        source=source,
        observed_at=observed_at,
        conditions=merged_conditions,
        method=method,
    )


def known_deployments_summary(store: Optional[mi.ModelIdentityStore] = None) -> Sequence[Dict[str, Any]]:
    """One row per deployment `ModelIdentityStore` already knows about — no
    resolution, no network, no model load (the ficha's `GET
    /api/creator/capabilities/models`: "por deployment conocido, sin
    cargar"). Each row is the stored `DeploymentManifest` dict plus how many
    axes currently have ANY evidence, cheap enough to compute per row
    without building a full profile for each."""
    store = store or mi.default_store()
    out = []
    for dep in store.list_deployments():
        deployment_id = str(dep.get("deployment_id") or "")
        evidenced_axes = 0
        for capability in set(_AXIS_TO_CAPABILITY.values()):
            if store.resolve_capability(deployment_id, capability) is not None:
                evidenced_axes += 1
        out.append({**dep, "evidenced_axes": evidenced_axes, "total_axes": len(_AXIS_TO_CAPABILITY)})
    return out


__all__ = [
    "AXES",
    "AXIS_TEXT", "AXIS_VISION_IN", "AXIS_IMAGE_OUT", "AXIS_IMAGE_EDIT",
    "AXIS_REGION_EDIT", "AXIS_CONTROLNET", "AXIS_UPSCALE", "AXIS_AUDIO_IN",
    "AXIS_AUDIO_OUT", "AXIS_VIDEO_IN", "AXIS_VIDEO_OUT", "AXIS_MUSIC",
    "AXIS_TTS", "AXIS_ASR",
    "STATUS_KNOWN", "STATUS_ANNOUNCED", "STATUS_UNSUPPORTED", "STATUS_UNKNOWN", "STATUSES",
    "CapabilityAxisEvidence",
    "MultimodalCapabilityProfile",
    "capability_profile_for_deployment",
    "record_evidence",
    "known_deployments_summary",
]
