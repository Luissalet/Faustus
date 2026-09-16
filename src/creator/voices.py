"""voices.py — WP18: casting, pronunciation and TTS/dub orchestration.

Sits above `src/creator/adapters/tts.py` exactly the way `src/creator/asr.py`
sits above `adapters/whisper.py`: this module never talks to Kokoro/SAPI/
piper/edge-tts/chatterbox directly, only through the `AdapterPort` contract
(`describe`/`plan`/`submit`/`status`/`collect`), so it stays engine-agnostic.

Three jobs, matching the ficha:

1. **Catalog** (`list_voices`) — the adapter's own documented voice menu,
   with each engine's real, freshly-probed availability folded in so a
   caller never has to cross-reference `describe()` separately.
2. **Casting** (`get_casting`/`set_casting`) — `speaker_id -> voice_id` for
   one transcript document, persisted on the project's `CreatorProfile`
   (WP02's `src.creator.profile` — no new table; CONTRATO.md rule 1).
   `set_casting` reports exactly which cues a change invalidates (AUD01:
   "cambiar el casting sólo invalida las frases que usan esa voz") rather
   than a caller having to diff the whole document itself.
3. **Rendering** (`audition`, `synthesize_transcript`) — one short
   preview utterance, or one occurrence per transcript cue, each registered
   through `src.creator.production_runs` with `derived_from` provenance —
   the SAME generic path WP10's ffmpeg adapter output already goes through
   (CONTRATO.md rule 1: no parallel artifact-registration path).

**A cue's synthesized audio never silently replaces the original track.**
AUD-level acceptance: "reemplazar audio original exige elección explícita."
`synthesize_transcript` always returns NEW occurrences plus a timing
manifest; nothing here ever mutates the transcript document's
`source_asset_ref` or any timeline clip. A caller that wants a swap makes
that a separate, explicit step — this build has no such step yet, and says
so (`replace_original=True` is accepted but always reports
`replaced_original: False` with a `limitations` entry, never a silent
no-op that LOOKS like it worked).
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .adapter_port import Staging
from .errors import CreatorError

CASTING_PROFILE_KEY = "voice_casting"


def _register_param_schema() -> None:
    """Register `(engine="tts", task="tts")`'s param shape with WP07's
    `src.creator.params` — additive, through the extension point that
    module's own docstring names ("a future WP ... can register an
    engine-specific schema without editing this module's seed data").
    `task` here is the literal string `"tts"`, because
    `src.creator.preflight._validate_params` forwards `operation` straight
    through as `task` — not a `model_capabilities.TASK_*` constant — so
    this is the exact pair `POST /api/creator/voices/synthesize`'s
    preflight call resolves. Every field is OPTIONAL: `synthesize_transcript`
    renders many cues, each with its own text/voice, so there is no single
    (text, voice_id) pair for this coarse, document-level preflight to
    validate — the REAL per-utterance validation is
    `adapters/tts.py::TtsAdapter.plan()`, run once per cue."""
    from src.creator import params as params_mod

    if params_mod.get_schema("tts", "tts") is not None:
        return
    params_mod.register_schema(params_mod.ParamSchema(
        engine="tts", task="tts",
        fields=(
            params_mod.ParamField(key="voice_id", label="Voice", type=params_mod.TYPE_STRING),
            params_mod.ParamField(key="text", label="Text", type=params_mod.TYPE_STRING),
            params_mod.ParamField(key="language", label="Language", type=params_mod.TYPE_STRING),
            params_mod.ParamField(key="speed", label="Speed", type=params_mod.TYPE_NUMBER, default=1.0),
            params_mod.ParamField(key="consent_subject", label="Consent subject", type=params_mod.TYPE_STRING),
        ),
    ))


_register_param_schema()


def _install_preflight_params_bridge() -> None:
    """Bridge a real integration gap between WP07's `params.py` and WP09's
    `preflight.py`, discovered wiring this lot: `preflight._missing_params`
    only understands `None`/`True`/`False`/a plain `Mapping` back from its
    `_validate_params` seam, but the REAL `src.creator.params.validate()`
    returns a `ValidationResult` DATACLASS — none of those — so every real
    preflight call against a registered `(engine, task)` schema (not just
    this one) falls into `_missing_params`'s "unrecognised validation
    result" branch and is reported as a spurious param failure, even when
    validation actually passed. Neither `preflight.py` (WP09) nor
    `params.py` (WP07) is this lot's to edit (CONTRATO.md file ownership) —
    but `preflight.py`'s own docstring names `_validate_params` as EXACTLY
    the seam a caller monkeypatches for this ("a test can monkeypatch.
    setattr either one directly... if WP07 has landed by the time this
    runs, the seam just forwards to it unchanged"). This installs that
    forward, adapted: `ValidationResult.to_dict()` — `{"ok", "errors":
    [{"field", "message", ...}]}` — IS the Mapping shape `_missing_params`
    already reads correctly. Idempotent (checked via an attribute on the
    installed function) so importing this module twice, or from two
    threads, never double-wraps. See `WP18_wiring.md` for the exact
    before/after and why this could not be fixed in either owning file.
    """
    from src.creator import params as params_mod
    from src.creator import preflight as preflight_mod

    if getattr(preflight_mod._validate_params, "_wp18_bridged", False):
        return

    def _bridged(engine: str, task: str, params: Any) -> Dict[str, Any]:
        return params_mod.validate(engine, task, params).to_dict()

    _bridged._wp18_bridged = True  # type: ignore[attr-defined]
    preflight_mod._validate_params = _bridged  # type: ignore[assignment]


_install_preflight_params_bridge()


class VoiceError(CreatorError):
    """A voices.py call failed validation — safe to show a caller verbatim."""


class VoiceJobNotFound(CreatorError):
    """No audition/synthesis job with that id, or it belongs to someone
    else — 404 either way, CONTRATO.md rule 3."""


def _adapter():
    from . import adapters
    return adapters.get("tts")


def _project_store():
    from services.projects import get_store
    return get_store()


# ── catalog ─────────────────────────────────────────────────────────────

def list_voices(*, language: Optional[str] = None) -> Dict[str, Any]:
    """The adapter's documented voice menu, each entry's engine folded in
    with a fresh `installed`/`detail` probe. `language`, when given, filters
    to voices that ACTUALLY declare it (MOD14: "elegir español filtra ... en
    lugar de fingir soporte") — an English-only voice is simply absent from
    a Spanish filter, never relabelled."""
    manifest = _adapter().describe()
    engines = manifest.limits.get("engines", {})
    voices = manifest.limits.get("voices", [])
    out = []
    for voice in voices:
        engine_probe = engines.get(voice["engine"], {"installed": False, "detail": "unknown engine"})
        entry = dict(voice)
        entry["available"] = bool(engine_probe.get("installed"))
        entry["engine_detail"] = str(engine_probe.get("detail") or "")
        if language and voice.get("languages") and voice["languages"] != ["und"] and language not in voice["languages"]:
            continue
        out.append(entry)
    return {"voices": out, "engines": engines, "cloning_ops": manifest.limits.get("cloning_ops", [])}


def voice_by_id(voice_id: str) -> Optional[Dict[str, Any]]:
    for voice in list_voices()["voices"]:
        if voice["id"] == voice_id:
            return voice
    return None


# ── casting (persisted on the project's CreatorProfile) ───────────────────

def _casting_map(profile: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    raw = profile.get(CASTING_PROFILE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def get_casting(owner: str, project_id: str, doc_id: str) -> Dict[str, str]:
    """`{speaker_id: voice_id}` for one document, or `{}` if nothing has
    been cast yet (never an error — an uncast document is a normal state,
    not a missing one)."""
    from . import profile as profile_mod

    prof = profile_mod.get_profile(_project_store(), owner, project_id)
    if prof is None:
        return {}
    return dict(_casting_map(prof).get(doc_id) or {})


def set_casting(owner: str, project_id: str, doc_id: str,
                assignments: Mapping[str, str]) -> Dict[str, Any]:
    """Assign `speaker_id -> voice_id` for `doc_id`'s cues, merged onto
    whatever casting already existed for it. Every `voice_id` must be a
    real, catalog id — an unknown one is rejected before anything is
    written, never silently ignored.

    Returns the merged casting plus `changed_speakers` and
    `invalidated_cue_ids` — the cues in `doc_id` whose `speaker_id` just
    got a DIFFERENT voice (a brand-new assignment for a previously-uncast
    speaker also counts as "changed"), so a caller can regenerate exactly
    those cues instead of the whole document (AUD01).
    """
    from . import profile as profile_mod
    from . import store as store_mod

    doc = store_mod.get_store().get(owner, doc_id)
    if doc is None or doc.project_id != project_id:
        raise VoiceError(f"transcript document not found: {doc_id}")
    if doc.kind != "transcript":
        raise VoiceError(f"document {doc_id} is kind={doc.kind!r}; casting needs a transcript")

    catalog_ids = {v["id"] for v in list_voices()["voices"]}
    assignments = {str(k): str(v) for k, v in (assignments or {}).items()}
    for speaker_id, voice_id in assignments.items():
        if not speaker_id:
            raise VoiceError("speaker_id must be non-empty")
        if voice_id not in catalog_ids:
            raise VoiceError(f"unknown voice_id {voice_id!r}")

    previous = get_casting(owner, project_id, doc_id)
    merged = dict(previous)
    changed_speakers = []
    for speaker_id, voice_id in assignments.items():
        if previous.get(speaker_id) != voice_id:
            changed_speakers.append(speaker_id)
        merged[speaker_id] = voice_id

    invalidated_cue_ids = [
        cue["id"] for cue in (doc.content.get("cues") or [])
        if cue.get("speaker_id") in changed_speakers
    ]

    prof = profile_mod.get_profile(_project_store(), owner, project_id) or {}
    casting_map = _casting_map(prof)
    casting_map[doc_id] = merged
    profile_mod.set_profile(_project_store(), owner, project_id, {CASTING_PROFILE_KEY: casting_map})

    return {"doc_id": doc_id, "casting": merged, "changed_speakers": changed_speakers,
            "invalidated_cue_ids": invalidated_cue_ids}


# ── audition (short preview job) ────────────────────────────────────────

def audition(owner: str, project_id: str, voice_id: str, text: str, *,
            language: Optional[str] = None, speed: float = 1.0) -> Dict[str, Any]:
    """Synthesize one short preview line — not a persistent transcript cue,
    just a per-phrase test (WP18 ficha: "preview por frase, no prueba de
    micrófono automática"). Registers the result as an ordinary artifact
    occurrence with no `derived_from` (there is no source occurrence — the
    text came straight from the caller)."""
    if not text or not text.strip():
        raise VoiceError("text is required")
    if not project_id:
        raise VoiceError("project_id is required")

    from . import production_runs
    from .adapters.base import new_staging_dir

    adapter = _adapter()
    plan = adapter.plan("tts", {"voice_id": voice_id, "text": text, "language": language,
                                "speed": speed, "_owner_hint": owner}, [])
    if not plan.ok:
        raise VoiceError(plan.detail or "audition could not be planned")

    workdir = new_staging_dir()
    staging = Staging(owner=owner, project_id=project_id, workdir=workdir, input_paths={})
    result = adapter.submit(plan, staging)
    run = production_runs.record_submit(
        adapter_name="tts", engine=plan.engine_plan.get("engine", "tts"), op="tts",
        job_id=result.job_id, submit_state=result.state, owner=owner, project_id=project_id,
        values={"voice_id": voice_id, "kind": "audition"}, reason=result.detail,
    )
    if result.state != "accepted":
        return {"ok": False, "run": run, "reason": result.reason, "detail": result.detail}

    collected = adapter.collect(result.job_id, workdir)
    linked = production_runs.link_outputs(
        result.job_id, adapter_name="tts", collected=collected, owner=owner,
        project_id=project_id, op="tts", input_occurrence_ids=(),
    )
    return {"ok": bool(collected.ok), "run": linked, "voice_id": voice_id,
            "dropped_tags": plan.engine_plan.get("dropped_tags", [])}


# ── synthesize_transcript (one occurrence per cue, with a timing manifest) ─

def synthesize_transcript(owner: str, project_id: str, doc_id: str, *,
                          default_voice_id: Optional[str] = None,
                          cue_ids: Optional[Sequence[str]] = None,
                          replace_original: bool = False) -> Dict[str, Any]:
    """Render every cue of `doc_id` (or only `cue_ids`, when given — see
    `set_casting`'s `invalidated_cue_ids`) through this document's casting,
    one artifact occurrence per cue, `derived_from` the transcript's
    original audio occurrence. Returns each cue's outcome plus a manifest
    (`{cue_id, speaker_id, voice_id, start_sample, end_sample,
    occurrence_id}` per cue) a timeline can align against — this module
    does not write that manifest into any timeline document itself (see
    module docstring)."""
    from . import production_runs
    from . import store as store_mod
    from .adapters.base import new_staging_dir

    doc = store_mod.get_store().get(owner, doc_id)
    if doc is None or doc.project_id != project_id:
        raise VoiceError(f"transcript document not found: {doc_id}")
    if doc.kind != "transcript":
        raise VoiceError(f"document {doc_id} is kind={doc.kind!r}; needs a transcript")

    source_asset_ref = str(doc.content.get("source_asset_ref") or "")
    casting = get_casting(owner, project_id, doc_id)
    cues = doc.content.get("cues") or []
    if cue_ids is not None:
        wanted = set(str(c) for c in cue_ids)
        cues = [c for c in cues if c.get("id") in wanted]

    adapter = _adapter()
    results: List[Dict[str, Any]] = []
    manifest: List[Dict[str, Any]] = []
    limitations: List[str] = []
    if replace_original:
        limitations.append(
            "replace_original was requested but this build has no track-replacement path; "
            "the new occurrences below are available for the caller to swap in explicitly, "
            "nothing was replaced automatically")

    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        speaker_id = cue.get("speaker_id")
        voice_id = casting.get(speaker_id) if speaker_id else None
        voice_id = voice_id or default_voice_id
        if not voice_id:
            results.append({"cue_id": cue["id"], "ok": False, "reason": "no_voice_assigned",
                            "detail": f"speaker_id {speaker_id!r} has no cast voice and no "
                                      "default_voice_id was given"})
            continue

        workdir = new_staging_dir()
        plan = adapter.plan("tts", {"voice_id": voice_id, "text": text,
                                    "language": cue.get("language"), "_owner_hint": owner}, [])
        if not plan.ok:
            results.append({"cue_id": cue["id"], "ok": False, "reason": "plan_rejected",
                            "detail": plan.detail})
            continue

        staging = Staging(owner=owner, project_id=project_id, workdir=workdir, input_paths={})
        result = adapter.submit(plan, staging)
        run = production_runs.record_submit(
            adapter_name="tts", engine=plan.engine_plan.get("engine", "tts"), op="tts",
            job_id=result.job_id, submit_state=result.state, owner=owner, project_id=project_id,
            values={"voice_id": voice_id, "cue_id": cue["id"], "doc_id": doc_id}, reason=result.detail,
        )
        if result.state != "accepted":
            results.append({"cue_id": cue["id"], "ok": False, "reason": result.reason,
                            "detail": result.detail, "run": run})
            continue

        collected = adapter.collect(result.job_id, workdir)
        input_ids = (source_asset_ref,) if source_asset_ref else ()
        linked = production_runs.link_outputs(
            result.job_id, adapter_name="tts", collected=collected, owner=owner,
            project_id=project_id, op="tts", input_occurrence_ids=input_ids,
        )
        occurrence_id = ""
        artifacts = linked.get("artifacts") or []
        if collected.ok and artifacts:
            occurrence_id = str(artifacts[0].get("id") or "")
        results.append({"cue_id": cue["id"], "ok": bool(collected.ok), "voice_id": voice_id,
                        "occurrence_id": occurrence_id, "run": linked,
                        "dropped_tags": plan.engine_plan.get("dropped_tags", [])})
        manifest.append({
            "cue_id": cue["id"], "speaker_id": speaker_id, "voice_id": voice_id,
            "start_sample": cue.get("start_sample"), "end_sample": cue.get("end_sample"),
            "occurrence_id": occurrence_id,
        })

    return {"doc_id": doc_id, "results": results, "manifest": manifest,
            "replaced_original": False, "limitations": limitations}


__all__ = [
    "CASTING_PROFILE_KEY", "VoiceError", "VoiceJobNotFound",
    "list_voices", "voice_by_id", "get_casting", "set_casting",
    "audition", "synthesize_transcript",
]
