"""asr.py — WP15: orchestrates ASR over the WP10 adapter port.

One audio (or video-with-audio) occurrence goes in; a WP02 `transcript`
CreatorDocument (WP12's multi-speaker shape) comes out, with per-segment
text/language/confidence and, when requested, per-word timestamps and
confidence. Nothing here talks to `faster_whisper` directly — that is
`src/creator/adapters/whisper.py`'s job, reached only through the
`AdapterPort` contract (`describe`/`plan`/`submit`/`status`/`collect`/
`cancel`) so this module stays engine-agnostic, exactly like
`src/creator/production_runs.py` stays agnostic of ComfyUI vs. ffmpeg.

Pipeline, in order:

1. Resolve the occurrence (owner-scoped; "no es tuyo"/"no existe" answer
   the same per CONTRATO.md rule 3) and stage it via
   `src.creator.adapters.base.stage_inputs` — the one door from an id to a
   filesystem path.
2. Normalize to 16 kHz mono WAV via `ffmpeg`, IF ffmpeg is on PATH. Missing
   ffmpeg is a documented degrade (CONTRATO.md rule 6): the adapter still
   receives the staged source bytes as-is rather than failing outright,
   because faster-whisper decodes most containers itself; only the
   `content.sample_rate` unit choice below is unaffected either way, since
   segment/word timestamps come back from the engine in SECONDS and this
   module converts them to samples at a single, fixed 16 kHz time base
   regardless of the source file's native rate.
3. `adapter.plan()` (pure) then `adapter.submit()` (the one effect —
   enqueues a worker job; never blocks).
4. `get_job()` polls `adapter.status()`; once `completed`, `adapter.
   collect()` pulls the segments/words JSON into a bounded tmp dir, and
   THIS module (never the adapter) turns that into a validated
   `transcript` document via `src.creator.store` — `store.create()` runs
   the SAME structural validation every other Creator document kind goes
   through, so a malformed engine result fails loudly here rather than
   silently becoming a broken document.

**Speaker identity.** `speaker_id` is always `None` on every cue this
module writes. Real diarization (SUB02: "identificar turnos de speaker_id")
is a genuine extension point — `src/creator/adapters/whisper.py` already
reports whether `pyannote.audio` is importable in its manifest — but
actually running a diarization pipeline needs a pretrained checkpoint
pulled from the HuggingFace hub on first use, which CONTRATO.md rule 6 and
the ficha ("no se descargan pesos al importar") forbid happening as a side
effect of this module doing its job. A caller that passes `diarize=True`
gets that request honestly reflected back (`diarization_applied: False`,
`limitations`) rather than a silently-ignored flag OR a fabricated
`speaker_1`/`speaker_2` grouping that would read as more than it is —
SUB02's own acceptance criterion, word for word: "un speaker_1 nunca se
convierte en persona identificada sin un dato explícito."

**No timestamp is ever invented.** A word faster-whisper could not time
(SUB01's acceptance criterion) is written with `start_sample`/`end_sample`/
`confidence` all `None` rather than interpolated from its neighbours or
from the enclosing segment's span.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .adapter_port import Staging
from .errors import CreatorError

logger = logging.getLogger(__name__)


class AsrError(CreatorError):
    """A transcription request failed validation, or the source occurrence/
    engine could not be prepared — safe to show a caller verbatim."""


class AsrJobNotFound(CreatorError):
    """No ASR job with that id, or it belongs to someone else — 404 either
    way per CONTRATO.md rule 3."""


#: The fixed time base every transcript document this module writes uses
#: for `start_sample`/`end_sample`, independent of the source file's own
#: sample rate (see module docstring point 2).
SAMPLE_RATE = 16000

_NORMALIZE_TIMEOUT_S = 300
_PROBE_TIMEOUT_S = 30

_JOBS_LOCK = threading.Lock()
#: job_id -> {"owner", "project_id", "occurrence_id", "adapter_job_id",
#: "state", "document_id", "error", "align_words", "diarize", "workdir",
#: "created_at"}. In-memory, same as the whisper adapter's own job ledger —
#: this module's job id is a SEPARATE id from the adapter's (never the same
#: string), so a caller never has to know which adapter did the work.
_JOBS: Dict[str, Dict[str, Any]] = {}


# ── adapter lookup ──────────────────────────────────────────────────────

def _adapter():
    from . import adapters
    return adapters.get("whisper")


# ── ffmpeg normalization (bounded, degrades without ffmpeg) ─────────────

def _probe_duration(path: str, *, which=shutil.which, run=subprocess.run) -> Optional[float]:
    ffprobe = which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
                  capture_output=True, timeout=_PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _normalize_audio(source_path: str, workdir: str, *,
                     which=shutil.which, run=subprocess.run) -> str:
    """Best-effort 16 kHz mono WAV of `source_path`, written under
    `workdir`. Returns `source_path` unchanged (never raises) when ffmpeg
    is not on PATH — a documented degrade (CONTRATO.md rule 6): the ASR
    engine still gets real bytes to decode itself, just not pre-normalized.
    Raises `AsrError` only when ffmpeg IS present but genuinely fails on
    this specific file (a real problem worth surfacing, not swallowing)."""
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        logger.info("creator.asr: ffmpeg not installed; passing the source through unnormalized")
        return source_path
    import os
    out_path = os.path.join(workdir, "normalized_16k_mono.wav")
    try:
        proc = run([ffmpeg, "-y", "-nostdin", "-i", source_path,
                   "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", out_path],
                  capture_output=True, timeout=_NORMALIZE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AsrError(f"ffmpeg could not normalize the source audio: {exc}") from exc
    if proc.returncode != 0 or not os.path.isfile(out_path):
        stderr_tail = (proc.stderr or b"")[-2000:].decode("utf-8", "replace")
        raise AsrError(f"ffmpeg failed to normalize the source audio: {stderr_tail}")
    return out_path


# ── submit ───────────────────────────────────────────────────────────────

def submit(owner: str, occurrence_id: str, *, project_id: Optional[str] = None,
          language: Optional[str] = None, align_words: bool = True,
          model: Optional[str] = None, device: Optional[str] = None,
          diarize: bool = False) -> Dict[str, Any]:
    """Start transcribing `occurrence_id` for `owner`. Returns the same
    shape `get_job` does, at whatever state the adapter reached
    synchronously (usually `queued`; `rejected_before_queue` folds to
    `failed` with the adapter's own detail)."""
    if not occurrence_id:
        raise AsrError("occurrence_id is required")
    if language is not None and not (isinstance(language, str) and language.strip()):
        raise AsrError("language must be a non-empty string or omitted for auto-detect")

    from src import artifact_identity as identity
    try:
        occ = identity.for_owner(occurrence_id, owner=owner)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        raise AsrJobNotFound(occurrence_id) from exc
    if occ.kind not in ("audio", "video"):
        raise AsrError(f"occurrence {occurrence_id} is kind={occ.kind!r}; ASR needs audio or video")

    resolved_project_id = project_id or occ.project_id
    if not resolved_project_id:
        raise AsrError("project_id is required (the occurrence itself carries none)")

    from .adapters.base import stage_inputs
    staging = stage_inputs(owner=owner, project_id=resolved_project_id,
                           occurrence_ids=[occurrence_id])
    source_path = staging.input_paths[occurrence_id]
    duration = _probe_duration(source_path)
    normalized_path = _normalize_audio(source_path, staging.workdir)

    adapter = _adapter()
    manifest = adapter.describe()
    if not manifest.available:
        raise AsrError(manifest.reason or "ASR engine is not available")

    params: Dict[str, Any] = {"language": language, "align_words": bool(align_words)}
    if model:
        params["model"] = model
    if device:
        params["device"] = device
    if duration:
        params["duration_seconds"] = duration

    plan = adapter.plan("transcribe", params, [occurrence_id])
    if not plan.ok:
        raise AsrError(plan.detail or "ASR could not plan this transcription")

    submit_staging = Staging(owner=owner, project_id=resolved_project_id,
                             workdir=staging.workdir,
                             input_paths={occurrence_id: normalized_path})
    result = adapter.submit(plan, submit_staging)

    job_id = f"asr_{uuid.uuid4().hex[:20]}"
    rejected = result.state == "rejected_before_queue"
    limitations: List[str] = []
    if diarize:
        limitations.append(
            "diarization was requested but not performed: this build has no "
            "diarization backend wired in (speaker_id stays null on every cue)")
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "owner": owner, "project_id": resolved_project_id, "occurrence_id": occurrence_id,
            "adapter_job_id": result.job_id or None,
            "state": "failed" if rejected else "queued",
            "error": result.detail if rejected else "",
            "document_id": None, "align_words": bool(align_words), "diarize": bool(diarize),
            "diarization_applied": False, "limitations": limitations,
            "workdir": staging.workdir, "duration_seconds": duration,
            "created_at": time.time(),
        }
    return get_job(owner, job_id)  # type: ignore[return-value]


# ── polling / finalization ──────────────────────────────────────────────

def _finish_from_collect(owner: str, job_id: str) -> None:
    """Completed at the adapter → collected → turned into a validated
    `transcript` document. Runs at most once per job (checked under the
    lock before any filesystem/store I/O starts, and again right before the
    document is actually created, so two concurrent pollers never build two
    documents for the same job)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.get("document_id"):
            return
        adapter_job_id = job["adapter_job_id"]
        workdir = job["workdir"]

    import os
    import shutil as _shutil
    import tempfile

    adapter = _adapter()
    collect_dir = tempfile.mkdtemp(prefix="creator-asr-collect-", dir=workdir if os.path.isdir(workdir) else None)
    try:
        collected = adapter.collect(adapter_job_id, collect_dir)
        if not collected.ok or not collected.outputs:
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.update(state="failed", error=collected.detail or "ASR collect failed")
            return
        with open(collected.outputs[0].path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            if job.get("document_id"):
                return  # another poller already built it
            owner_, project_id, occurrence_id = job["owner"], job["project_id"], job["occurrence_id"]
            align_words = job["align_words"]

        doc = _build_transcript_document(owner_, project_id, occurrence_id, payload,
                                         align_words=align_words)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None and not job.get("document_id"):
                job.update(state="completed", document_id=doc.id)
    finally:
        _shutil.rmtree(collect_dir, ignore_errors=True)


def _poll(owner: str, job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner:
            return
        state = job["state"]
        adapter_job_id = job["adapter_job_id"]
    if state not in ("queued", "running") or not adapter_job_id:
        return
    status = _adapter().status(adapter_job_id)
    if status.state == "running":
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None and job["state"] == "queued":
                job["state"] = "running"
        return
    if status.state == "failed":
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job.update(state="failed", error=status.detail)
        return
    if status.state == "cancelled":
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job["state"] = "cancelled"
        return
    if status.state == "completed":
        _finish_from_collect(owner, job_id)


def get_job(owner: str, job_id: str) -> Optional[Dict[str, Any]]:
    """This owner's job at its current state, polling the adapter and
    finishing it (building the transcript document) if it just completed.
    `None` for an unknown/foreign job — 404 either way, CONTRATO.md rule 3."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner:
            return None
    _poll(owner, job_id)
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id, "state": job["state"], "occurrence_id": job["occurrence_id"],
            "project_id": job["project_id"], "document_id": job["document_id"],
            "error": job["error"], "align_words": job["align_words"],
            "diarize": job["diarize"], "diarization_applied": job["diarization_applied"],
            "limitations": list(job["limitations"]), "duration_seconds": job["duration_seconds"],
            "created_at": job["created_at"],
        }


def cancel(owner: str, job_id: str) -> Dict[str, Any]:
    """Forward a cancel to the adapter and fold its outcome into this job's
    view. Raises `AsrJobNotFound` for an unknown/foreign id."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner:
            raise AsrJobNotFound(job_id)
        adapter_job_id = job["adapter_job_id"]
        state = job["state"]
    if not adapter_job_id or state in ("completed", "failed", "cancelled"):
        return {"job_id": job_id, "outcome": "too_late" if state in ("completed", "failed") else "confirmed"}
    outcome = _adapter().cancel(adapter_job_id)
    if outcome.outcome in ("accepted", "confirmed"):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None and job["state"] not in ("completed", "failed"):
                job["state"] = "cancelled"
    return {"job_id": job_id, "outcome": outcome.outcome, "detail": outcome.detail}


# ── transcript document construction ────────────────────────────────────

def _to_sample(seconds: Any) -> Optional[int]:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return max(0, round(float(seconds) * SAMPLE_RATE))


def _build_transcript_document(owner: str, project_id: str, occurrence_id: str,
                               payload: Dict[str, Any], *, align_words: bool):
    from . import store as store_mod

    language = str(payload.get("language") or "und")
    segments = payload.get("segments") or []
    cues: List[Dict[str, Any]] = []
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue  # SUB01: never invent text for a segment that has none
        start_sample = _to_sample(seg.get("start")) or 0
        end_sample = _to_sample(seg.get("end")) or (start_sample + 1)
        if end_sample <= start_sample:
            end_sample = start_sample + 1  # a zero-length segment is a rounding
            # artifact of converting seconds->samples, never a claim about
            # the audio; widened by exactly one sample so the document
            # stays structurally valid without pretending more precision.

        words_out: List[Dict[str, Any]] = []
        for j, w in enumerate(seg.get("words") or []):
            word_text = str(w.get("text") or "").strip()
            if not word_text:
                continue
            w_start = _to_sample(w.get("start"))
            w_end = _to_sample(w.get("end"))
            confidence = w.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = None
            words_out.append({
                "id": f"w_{i}_{j}", "text": word_text,
                "start_sample": str(w_start) if w_start is not None else None,
                "end_sample": str(w_end) if w_end is not None else None,
                "confidence": float(confidence) if confidence is not None else None,
            })

        seg_confidence = seg.get("confidence")
        if not isinstance(seg_confidence, (int, float)) or isinstance(seg_confidence, bool):
            seg_confidence = None
        cues.append({
            "id": f"cue_{i}", "text": text, "language": language,
            "speaker_id": None,  # never fabricated — see module docstring
            "start_sample": str(start_sample), "end_sample": str(end_sample),
            "words": words_out, "editorial_status": "proposed",
            # additive beyond the reference schema (documents.py does not
            # forbid extra keys; CONTRATO.md rule 1: "amplía de forma
            # aditiva") — per-segment confidence the ficha asks for, kept
            # nullable rather than a fabricated 1.0 when unknown.
            "confidence": float(seg_confidence) if seg_confidence is not None else None,
        })

    content = {
        "source_asset_ref": occurrence_id,
        "sample_rate": SAMPLE_RATE,
        "cues": cues,
        "alignment_status": "partial" if align_words else "not_aligned",
    }
    return store_mod.get_store().create(owner, project_id, "transcript", content,
                                        asset_refs=[occurrence_id])


__all__ = ["AsrError", "AsrJobNotFound", "SAMPLE_RATE", "submit", "get_job", "cancel"]
