"""music.py — WP24: Music Studio orchestration over the WP10 adapter port.

A `song` `CreatorDocument` (WP02/WP12's typed doc: sections with per-section
lyrics, style tags, target BPM/key, seed) goes in; a generated audio
occurrence — with an honest receipt of what was actually sent to the
engine — comes out, recorded on the SAME document as a new `take` (never a
second, parallel record of what was generated). Nothing here talks to
`acestep`/`audiocraft` directly — that is
`src/creator/adapters/music.py`'s job, reached only through the
`AdapterPort` contract, exactly like `src/creator/asr.py` stays
engine-agnostic of `faster_whisper`.

Pipeline, per call to :func:`generate`:

1. Load the `song` document (owner-scoped; CONTRATO.md rule 3) and build
   the structured prompt from its CURRENT content via
   `adapters.music.build_structured_prompt` — section markers, style tags,
   BPM/key hints. Deterministic: the same document content always builds
   the same prompt (WP24's own test requirement).
2. `adapter.plan()` (pure) validates the request — an impossible duration,
   an unsupported engine, no prompt/lyrics/tags at all — and is where an
   impossible input is rejected, PER VARIANT (see :func:`variants`), before
   anything reaches a queue (WP24 closing criterion).
3. `adapter.submit()` (the one effect) enqueues a worker job; `generate`
   returns immediately at whatever state the adapter reached synchronously
   (usually `queued`).
4. `get_job()` polls `adapter.status()`; once `completed`,
   `adapter.collect()` pulls the WAV + metadata JSON into a bounded tmp
   dir, and THIS module turns the WAV into a real, owner-scoped
   `ArtifactOccurrence` (kind `audio`, `relations.derived_from=(doc_id,)` —
   MUS01's "la canción como asset enlazado, no sólo un mensaje") and the
   engine's reported params into a `song.record_take` command on the SAME
   document (dedup-safe: `command_id` is derived from the job id, so a
   crashed-and-retried poll never double-records one job's take).

**No timestamp is ever invented.** `lyrics_timing` on a recorded take is the
engine's own reported per-line/word timestamps when it gave any, else
`None` — never interpolated from section boundaries or a guessed tempo
(MUS02/asr.py's own "no se inventan timestamps" discipline, reused
verbatim for music).

**A reference audio input is never sent to the engine without consent and
an output policy already checked** — `generate()`'s `reference_occurrence_id`
parameter is staged (`adapters.base.stage_inputs`, so "not yours"/"does not
exist" answer identically) but this module does NOT itself decide whether
sending it onward is allowed: that is `src.creator.preflight`'s job, and
`routes/creator_music_routes.py` requires an approved preflight digest
before calling `generate()` at all (MUS04's acceptance criterion) — this
module trusts the route already gated that, exactly like `asr.py` trusts
its own route's flag check.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from .adapter_port import Staging
from .errors import CreatorError

DEFAULT_DURATION_S = 60.0


class MusicError(CreatorError):
    """A generation request failed validation, or the document/engine could
    not be prepared — safe to show a caller verbatim."""


class MusicJobNotFound(CreatorError):
    """No music job with that id, or it belongs to someone else — 404
    either way per CONTRATO.md rule 3."""


_JOBS_LOCK = threading.Lock()
#: job_id -> {"owner", "project_id", "doc_id", "adapter_job_id", "state",
#: "occurrence_id", "error", "workdir", "created_at", "params"}. In-memory
#: only, same discipline as `asr.py`'s own job ledger.
_JOBS: Dict[str, Dict[str, Any]] = {}


def _adapter():
    from . import adapters
    return adapters.get("music")


# ── prompt building (delegates to the adapter's deterministic builder) ──

def build_prompt_for_document(content: Dict[str, Any]) -> Dict[str, Any]:
    """The structured prompt `generate()` would send RIGHT NOW for a `song`
    document's current `content` — exposed standalone (no adapter, no I/O)
    so a route/test can preview it, and so `variants()`'s "same prompt,
    different seed" claim is checkable without submitting anything."""
    from .adapters.music import build_structured_prompt

    return build_structured_prompt(
        lyrics="", style_tags=content.get("style_tags") or [],
        bpm=content.get("bpm_target"), key=content.get("key_target"),
        sections=content.get("sections") or [],
    )


# ── generate / variants ─────────────────────────────────────────────────

def generate(owner: str, doc_id: str, *, project_id: Optional[str] = None,
            duration_s: Optional[float] = None, engine: Optional[str] = None,
            steps: Optional[int] = None, seed: Optional[int] = None,
            device: Optional[str] = None,
            reference_occurrence_id: Optional[str] = None) -> Dict[str, Any]:
    """Start one generation for `doc_id`'s current content. Returns the
    same shape `get_job` does. `seed`, when omitted, falls back to the
    document's own `content.seed` (set via `song.set_seed`); still omitted
    means "let the engine pick", honestly reported as `seed: None` on the
    resulting take rather than a fabricated 0."""
    from . import store as store_mod
    from .documents import DOCUMENT_KINDS  # noqa: F401 - documents kind is validated by store.get itself

    store = store_mod.get_store()
    doc = store.get(owner, doc_id)
    if doc is None:
        raise MusicJobNotFound(doc_id)
    if doc.kind != "song":
        raise MusicError(f"document {doc_id} is kind={doc.kind!r}; music generation needs a 'song' document")

    resolved_project_id = project_id or doc.project_id
    content = doc.content
    resolved_seed = seed if seed is not None else content.get("seed")

    inputs: List[str] = []
    staging: Optional[Staging] = None
    workdir = ""
    if reference_occurrence_id:
        from .adapters.base import stage_inputs
        staging = stage_inputs(owner=owner, project_id=resolved_project_id,
                               occurrence_ids=[reference_occurrence_id])
        inputs = [reference_occurrence_id]
        workdir = staging.workdir
    else:
        from .adapter_port import new_staging_dir
        workdir = new_staging_dir()

    adapter = _adapter()
    manifest = adapter.describe()
    if not manifest.available:
        raise MusicError(manifest.reason or "no music engine is available")

    params: Dict[str, Any] = {
        "duration_s": float(duration_s if duration_s is not None else DEFAULT_DURATION_S),
        "sections": content.get("sections") or [],
        "style_tags": content.get("style_tags") or [],
        "bpm": content.get("bpm_target"),
        "key": content.get("key_target"),
        "seed": resolved_seed,
    }
    if engine:
        params["engine"] = engine
    if steps is not None:
        params["steps"] = steps
    if device:
        params["device"] = device

    plan = adapter.plan("music.generate", params, inputs)
    if not plan.ok:
        raise MusicError(plan.detail or "music adapter could not plan this generation")

    submit_staging = Staging(owner=owner, project_id=resolved_project_id, workdir=workdir,
                             input_paths=dict(staging.input_paths) if staging else {})
    result = adapter.submit(plan, submit_staging)

    job_id = f"music_{uuid.uuid4().hex[:20]}"
    rejected = result.state == "rejected_before_queue"
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "owner": owner, "project_id": resolved_project_id, "doc_id": doc_id,
            "adapter_job_id": result.job_id or None,
            "state": "failed" if rejected else "queued",
            "error": result.detail if rejected else "",
            "occurrence_id": None, "take_id": None,
            "workdir": workdir, "created_at": time.time(),
            "engine_plan": dict(plan.engine_plan or {}),
        }
    return get_job(owner, job_id)  # type: ignore[return-value]


def variants(owner: str, doc_id: str, n: int, *, project_id: Optional[str] = None,
            duration_s: Optional[float] = None, engine: Optional[str] = None,
            steps: Optional[int] = None, device: Optional[str] = None,
            base_seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """Submits `n` independent generations of the SAME document, each with
    its own seed (`base_seed + i`, or a fresh random seed per variant when
    `base_seed` is omitted) — MUS05's "generar alternativas". Each variant
    is planned and, if invalid, rejected ON ITS OWN (WP24 closing criterion:
    "inputs imposibles se rechazan por variante") — one bad variant never
    stops the others in the same call from being submitted; a caller reads
    each returned job's own `state`/`error` to see which succeeded."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise MusicError("n must be a positive integer")
    if n > 8:
        raise MusicError("n must be at most 8 per call — submit a second batch instead of one huge one")

    import random
    jobs: List[Dict[str, Any]] = []
    for i in range(n):
        this_seed = (base_seed + i) if base_seed is not None else random.randint(0, 2**31 - 1)
        try:
            job = generate(owner, doc_id, project_id=project_id, duration_s=duration_s,
                           engine=engine, steps=steps, seed=this_seed, device=device)
        except MusicError as exc:
            job = {"job_id": "", "state": "failed", "error": str(exc), "seed": this_seed}
        jobs.append(job)
    return jobs


# ── polling / finalization ──────────────────────────────────────────────

def _command_id_for_job(job_id: str) -> str:
    """Deterministic from the job id alone, so a completed-but-not-yet-
    recorded job that gets polled by two threads (or retried after a
    crash) can never record its take twice: the SECOND `apply_command`
    call with this `command_id` dedupes at the store layer (WP02), not
    here."""
    return f"music_take_{job_id}"


def _finish_from_collect(owner: str, job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.get("occurrence_id"):
            return
        adapter_job_id = job["adapter_job_id"]
        workdir = job["workdir"]
        doc_id = job["doc_id"]
        project_id = job["project_id"]
        engine_plan = job["engine_plan"]

    import os
    import shutil as _shutil
    import tempfile

    adapter = _adapter()
    collect_dir = tempfile.mkdtemp(prefix="creator-music-collect-",
                                   dir=workdir if os.path.isdir(workdir) else None)
    try:
        collected = adapter.collect(adapter_job_id, collect_dir)
        if not collected.ok or not collected.outputs:
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.update(state="failed", error=collected.detail or "music collect failed")
            return
        audio_output = next((o for o in collected.outputs if o.media_type == "audio/wav"), None)
        meta_output = next((o for o in collected.outputs if o.media_type == "application/json"), None)
        if audio_output is None or not audio_output.valid:
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.update(state="failed", error="music collect returned no valid audio")
            return
        meta: Dict[str, Any] = {}
        if meta_output is not None and meta_output.valid:
            with open(meta_output.path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None or job.get("occurrence_id"):
                return  # another poller already finished it

        occurrence = _register_occurrence(
            owner=owner, project_id=project_id, doc_id=doc_id,
            audio_path=audio_output.path, sha256=audio_output.sha256,
            byte_size=audio_output.byte_size, meta=meta,
        )
        take = _take_from_meta(job_id=job_id, occurrence_id=occurrence["id"], meta=meta)

        from . import store as store_mod
        from .errors import RevisionConflict

        store = store_mod.get_store()
        # The document may have been edited (lyrics, style...) while this
        # job was running — retry on a revision conflict with whatever the
        # CURRENT revision is; `command_id` dedupe still protects against
        # ever recording the same job's take twice.
        for _attempt in range(5):
            doc = store.get(owner, doc_id)
            if doc is None:
                break
            try:
                store.apply_command(
                    owner, doc_id, _command_id_for_job(job_id), doc.revision,
                    {"type": "song.record_take", "take": take},
                )
                break
            except RevisionConflict:
                continue

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None and not job.get("occurrence_id"):
                job.update(state="completed", occurrence_id=occurrence["id"], take_id=take["id"])
    finally:
        _shutil.rmtree(collect_dir, ignore_errors=True)


def _register_occurrence(*, owner: str, project_id: str, doc_id: str, audio_path: str,
                         sha256: str, byte_size: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Publishes the collected WAV into the durable artifact store and
    records it as an owner-scoped occurrence, `relations.derived_from`
    pointing at the `song` document it came from (MUS01: "asset enlazado,
    no sólo un mensaje"). Reuses `src.artifact_identity`/`src.artifact_store`
    exactly as `library.py::generate_proxy` does — no second store."""
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence

    digest, size, filename, _created = artifact_store.publish_copy(
        audio_path, ARTIFACT_STORE_DIR, f"{doc_id}.wav")
    identity.ensure_blob(sha256=digest, byte_size=size, filename=filename, media_type="audio/wav")

    occurrence_id = "occ_" + hashlib.sha1(  # noqa: S324 - id, not a security digest
        f"{owner}:{doc_id}:{digest}".encode("utf-8")).hexdigest()[:24]
    occurrence = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "audio", "blob_sha256": digest,
        "label": f"song take of {doc_id}",
        "owner": owner, "project_id": project_id,
        "skill_id": "creator.music", "skill_version": "1.0.0",
        "provenance": {
            "backend": meta.get("engine") or "unknown",
            "recipe": "creator.music.generate",
            "recipe_version": "1",
            "recipe_fingerprint": hashlib.sha256(
                json.dumps(meta.get("structured_prompt") or {}, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "source_artifact_ids": [],
        },
        "retention": {"policy": "keep"},
        "relations": {"derived_from": [doc_id]},
    })
    recorded, _made = identity.ensure_occurrence(occurrence)
    return {"id": recorded.id}


def _take_from_meta(*, job_id: str, occurrence_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    take_id = f"take_{job_id}"
    return {
        "id": take_id,
        "occurrence_id": occurrence_id,
        "engine": meta.get("engine") or "unknown",
        "engine_version": meta.get("engine_version") or "",
        "seed": meta.get("seed"),
        "bpm": meta.get("bpm"),
        "key": meta.get("key"),
        "duration_s": meta.get("duration_s"),
        "steps": meta.get("steps"),
        "created_at": time.time(),
        # `lyrics_timing` is the engine's own reported timing, or an
        # explicit `None` — never fabricated (module docstring).
        "lyrics_timing": meta.get("timing"),
        "warnings": list(meta.get("warnings") or []),
        "favorite": False,
    }


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
    finishing it (registering the occurrence + recording the take) if it
    just completed. `None` for an unknown/foreign job — 404 either way."""
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
            "job_id": job_id, "state": job["state"], "doc_id": job["doc_id"],
            "project_id": job["project_id"], "occurrence_id": job["occurrence_id"],
            "take_id": job["take_id"], "error": job["error"], "created_at": job["created_at"],
        }


def cancel(owner: str, job_id: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["owner"] != owner:
            raise MusicJobNotFound(job_id)
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


def reset_for_tests() -> None:
    with _JOBS_LOCK:
        _JOBS.clear()


__all__ = [
    "MusicError", "MusicJobNotFound", "DEFAULT_DURATION_S",
    "build_prompt_for_document", "generate", "variants", "get_job", "cancel",
    "reset_for_tests",
]
