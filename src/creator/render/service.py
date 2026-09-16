"""service.py — WP14: plan/render/status/cancel over the timeline renderer.

Wires the pure compiler (``graph.py``) and the profile catalogue
(``profiles.py``) to the real world: the WP02 document store, the WP10
``ffmpeg`` adapter (``src/creator/adapters/ffmpeg.py``'s additive ``graph``
task), the WP10 staging helper, the render cache (``cache.py``) and
``src/creator/production_runs.py`` (the ONE place a non-ComfyUI adapter's
output becomes an artifact occurrence with full ``derived_from``
provenance — this module writes nothing to the artifact store itself).

**Two calls the ficha asks for:**

* :func:`plan` — PURE-ish: reads the document/profile/cache (no adapter
  call, no process spawned, nothing written) and returns a
  :class:`RenderPlan` with an estimated duration/size and — when the render
  cache already has an identical key — the CACHED occurrence, so a caller
  can skip rendering entirely without ever calling :func:`render`.
* :func:`render` — the EFFECT. Runs synchronously to completion in the
  calling thread by default (used directly by tests and by
  ``start_render``'s background thread); the caller (a route, or
  ``start_render``) decides whether to run it inline or off-thread.

**Async jobs (WP14's "progreso parseado y colas/cancelación por proceso
propio").** ``ffmpeg``'s own ``submit()`` is itself synchronous (blocks
until the process exits or is killed — see ``adapters/ffmpeg.py``'s module
docstring), so a caller that wants to POLL progress or CANCEL a render that
is still running needs a handle available BEFORE ``submit()`` returns.
``start_render()``/``get_job()``/``cancel_job()`` are that handle: a small,
in-process (never persisted — same discipline the ffmpeg adapter's own
``_JOBS`` ledger already documents for a local, single-process render) token
registry, keyed by a ``render_token`` minted immediately and resolved to the
adapter's real ``job_id`` via ``FfmpegAdapter.submit()``'s additive
``on_job_id`` callback the instant the process is spawned — so
``cancel_job()`` can reach and terminate the EXACT process this render
started, and nothing else.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import CreatorError, InvalidOperation
from . import cache as cache_mod
from . import graph as graph_mod
from .profiles import Profile, get_profile

__all__ = [
    "RenderPlan", "plan", "render", "start_render", "get_job", "cancel_job",
]


class RenderNotReady(CreatorError):
    """The document/engine is not in a state this module can render right
    now (wrong document kind, ffmpeg unavailable, an occurrence that does
    not exist/is not the caller's) — never silently degraded."""


@dataclass(frozen=True)
class RenderPlan:
    doc_id: str
    doc_revision: int
    profile: Profile
    graph: graph_mod.RenderGraph
    cache_key: str
    cached_occurrence_id: Optional[str]
    estimated_duration_seconds: float
    estimated_size_bytes: int
    engine_build: str
    engine_available: bool
    engine_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id, "doc_revision": self.doc_revision,
            "profile": self.profile.to_dict(), "graph": self.graph.to_dict(),
            "cache_key": self.cache_key, "cached_occurrence_id": self.cached_occurrence_id,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "estimated_size_bytes": self.estimated_size_bytes,
            "engine_build": self.engine_build, "engine_available": self.engine_available,
            "engine_reason": self.engine_reason,
        }


def _get_document(owner: str, doc_id: str):
    from ..store import get_store
    from ..errors import DocumentNotFound

    doc = get_store().get(owner, doc_id)
    if doc is None:
        raise DocumentNotFound(f"no such document: {doc_id!r}")
    if doc.kind != "timeline":
        raise InvalidOperation(f"document {doc_id!r} is a {doc.kind!r}, not a timeline")
    return doc


def _av_and_subtitle_ids(graph: graph_mod.RenderGraph) -> Tuple[List[str], List[str]]:
    """`(all_occurrence_ids_for_staging, av_occurrence_ids_for_ffmpeg_inputs)`."""
    av_ids = [i.occurrence_id for i in graph.inputs]
    all_ids = list(av_ids)
    if graph.subtitle_occurrence_id and graph.subtitle_occurrence_id not in all_ids:
        all_ids.append(graph.subtitle_occurrence_id)
    return all_ids, av_ids


def _blob_hashes(owner: str, occurrence_ids: Sequence[str]) -> List[str]:
    from src import artifact_identity as identity

    hashes = []
    for occ_id in occurrence_ids:
        occ = identity.for_owner(occ_id, owner=owner)  # raises ArtifactNotFound/NotTheOwner
        hashes.append(occ.blob_sha256)
    return hashes


def _estimate(graph: graph_mod.RenderGraph, profile: Profile) -> Tuple[float, int]:
    duration_seconds = float(graph.clock.seconds_of(graph.duration_ticks))
    video_bps = _parse_bitrate(profile.video_bitrate)
    audio_bps = _parse_bitrate(profile.audio_bitrate) if graph.audio_map else 0
    size_bytes = int((video_bps + audio_bps) * duration_seconds / 8.0)
    return duration_seconds, size_bytes


def _parse_bitrate(value: str) -> int:
    value = str(value or "").strip().lower()
    if not value:
        return 0
    mult = 1
    if value.endswith("k"):
        mult, value = 1_000, value[:-1]
    elif value.endswith("m"):
        mult, value = 1_000_000, value[:-1]
    try:
        return int(float(value) * mult)
    except ValueError:
        return 0


def _adapter():
    from src.creator import adapters
    return adapters.get("ffmpeg")


def plan(*, owner: str, project_id: str, doc_id: str, profile_id: str,
         subtitle_occurrence_id: Optional[str] = None) -> RenderPlan:
    """Pure read: no adapter call, no process, nothing written."""
    doc = _get_document(owner, doc_id)
    profile = get_profile(profile_id)
    render_graph = graph_mod.compile_graph(
        doc.content, profile=profile, document_revision=doc.revision,
        subtitle_occurrence_id=subtitle_occurrence_id,
    )
    all_ids, _av_ids = _av_and_subtitle_ids(render_graph)
    input_hashes_by_id = dict(zip(all_ids, _blob_hashes(owner, all_ids)))
    av_hashes = [input_hashes_by_id[i.occurrence_id] for i in render_graph.inputs]
    subtitle_hash = (
        input_hashes_by_id[render_graph.subtitle_occurrence_id]
        if render_graph.subtitle_occurrence_id else ""
    )

    manifest = _adapter().describe()
    engine_build = manifest.version or ""

    cache_key = cache_mod.compute_key(
        input_hashes=av_hashes, doc_revision=doc.revision,
        profile_fingerprint=profile.cache_fingerprint(), subtitle_hash=subtitle_hash,
        engine_build=engine_build,
    )

    cached_occurrence_id: Optional[str] = None
    cached_row = cache_mod.lookup(cache_key, owner=owner)
    if cached_row is not None:
        from src import artifact_identity as identity
        from src.artifact_identity import ArtifactNotFound, NotTheOwner
        try:
            identity.for_owner(cached_row["output_occurrence_id"], owner=owner)
            cached_occurrence_id = cached_row["output_occurrence_id"]
        except (ArtifactNotFound, NotTheOwner):
            cached_occurrence_id = None  # stale row: the occurrence was since removed

    duration_seconds, size_bytes = _estimate(render_graph, profile)
    return RenderPlan(
        doc_id=doc_id, doc_revision=doc.revision, profile=profile, graph=render_graph,
        cache_key=cache_key, cached_occurrence_id=cached_occurrence_id,
        estimated_duration_seconds=duration_seconds, estimated_size_bytes=size_bytes,
        engine_build=engine_build, engine_available=manifest.available, engine_reason=manifest.reason,
    )


def render(*, owner: str, project_id: str, doc_id: str, profile_id: str,
           subtitle_occurrence_id: Optional[str] = None, session_id: str = "",
           on_job_id: Optional[Any] = None) -> Dict[str, Any]:
    """The effect. Reuses a cache hit when one exists; otherwise stages
    inputs, submits the `graph` task to the `ffmpeg` adapter, collects and
    registers the output via `production_runs.link_outputs` (full
    `derived_from` over EVERY input occurrence + the burned subtitle, when
    one was used) and stores the new cache entry.

    Raises :class:`RenderNotReady` when ffmpeg is not available — never
    silently produces "another quality" output (VID12's acceptance
    criterion): the caller decides whether that is a 400/409/blocked
    response.
    """
    render_plan = plan(owner=owner, project_id=project_id, doc_id=doc_id,
                        profile_id=profile_id, subtitle_occurrence_id=subtitle_occurrence_id)

    if render_plan.cached_occurrence_id:
        return {
            "cache_hit": True, "status": "completed", "job_id": None,
            "occurrence_id": render_plan.cached_occurrence_id,
            "cache_key": render_plan.cache_key, "artifacts": [],
        }

    if not render_plan.engine_available:
        raise RenderNotReady(render_plan.engine_reason or "ffmpeg is not available")

    from src.creator.adapters.base import stage_inputs
    from src.creator import production_runs

    render_graph = render_plan.graph
    all_ids, av_ids = _av_and_subtitle_ids(render_graph)
    staging = stage_inputs(owner=owner, project_id=project_id, occurrence_ids=all_ids)

    params = {
        "output_ext": render_plan.profile.container_ext,
        "filter_complex": render_graph.filter_complex,
        "video_map": render_graph.video_map,
        "audio_map": render_graph.audio_map,
        "output_args": list(render_graph.output_args),
        "subtitle_occurrence_id": render_graph.subtitle_occurrence_id,
        "total_duration_seconds": render_plan.estimated_duration_seconds,
    }
    adapter = _adapter()
    adapter_plan = adapter.plan("graph", params, av_ids)
    if not adapter_plan.ok:
        raise RenderNotReady(adapter_plan.detail or "ffmpeg could not plan this render")

    submit_kwargs: Dict[str, Any] = {}
    if on_job_id is not None:
        submit_kwargs["on_job_id"] = on_job_id
    submit_result = adapter.submit(adapter_plan, staging, **submit_kwargs)

    row = production_runs.record_submit(
        adapter_name="ffmpeg", engine="ffmpeg", op="render_timeline",
        job_id=submit_result.job_id, submit_state=submit_result.state,
        owner=owner, project_id=project_id, session_id=session_id,
        values={"doc_id": doc_id, "profile_id": render_plan.profile.id,
                "cache_key": render_plan.cache_key},
        reason=submit_result.detail,
    )

    if submit_result.state != "accepted":
        return {
            "cache_hit": False, "status": row.get("status", "failed"),
            "job_id": submit_result.job_id, "occurrence_id": None,
            "cache_key": render_plan.cache_key, "reason": submit_result.detail,
            "artifacts": [],
        }

    collect_dir = os.path.join(staging.workdir, "collected")
    collected = adapter.collect(submit_result.job_id, collect_dir)
    settled = production_runs.link_outputs(
        submit_result.job_id, adapter_name="ffmpeg", collected=collected,
        owner=owner, project_id=project_id, op="render_timeline",
        input_occurrence_ids=all_ids, session_id=session_id,
    )

    artifacts = settled.get("artifacts") or []
    output_occurrence_id = artifacts[0]["id"] if artifacts else None
    if output_occurrence_id:
        cache_mod.store(
            render_plan.cache_key, owner=owner, project_id=project_id, doc_id=doc_id,
            output_occurrence_id=output_occurrence_id, engine_build=render_plan.engine_build,
            profile_id=render_plan.profile.id,
        )

    return {
        "cache_hit": False, "status": settled.get("status", "unknown"),
        "job_id": submit_result.job_id, "occurrence_id": output_occurrence_id,
        "cache_key": render_plan.cache_key, "artifacts": artifacts,
        "reason": "" if collected.ok else collected.detail,
    }


# ── async job registry (in-process; not persisted — see module docstring) ─

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _run_in_thread(token: str, *, owner: str, project_id: str, doc_id: str,
                    profile_id: str, subtitle_occurrence_id: Optional[str],
                    session_id: str) -> None:
    def _on_job_id(job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(token)
            if job is not None:
                job["ffmpeg_job_id"] = job_id
                if job.get("cancel_requested"):
                    # A cancel arrived after start_render() returned but
                    # before ffmpeg's own job_id was known — apply it now,
                    # the instant it becomes reachable.
                    try:
                        _adapter().cancel(job_id)
                    except Exception:  # noqa: BLE001
                        pass

    try:
        result = render(owner=owner, project_id=project_id, doc_id=doc_id,
                         profile_id=profile_id, subtitle_occurrence_id=subtitle_occurrence_id,
                         session_id=session_id, on_job_id=_on_job_id)
        with _JOBS_LOCK:
            job = _JOBS.get(token)
            if job is not None:
                job["state"] = "done"
                job["result"] = result
    except Exception as exc:  # noqa: BLE001 — a background render must report, never crash silently
        with _JOBS_LOCK:
            job = _JOBS.get(token)
            if job is not None:
                job["state"] = "error"
                job["error"] = str(exc)


def start_render(*, owner: str, project_id: str, doc_id: str, profile_id: str,
                  subtitle_occurrence_id: Optional[str] = None,
                  session_id: str = "") -> Dict[str, Any]:
    """Mint a `render_token`, start rendering on a background thread, and
    return immediately — the token is resolvable via `get_job()` to live
    ffmpeg progress the instant the process is spawned (see module
    docstring's `on_job_id` explanation)."""
    token = f"render_{uuid.uuid4().hex[:24]}"
    with _JOBS_LOCK:
        _JOBS[token] = {
            "state": "starting", "owner": owner, "ffmpeg_job_id": None,
            "cancel_requested": False, "result": None, "error": None,
            "started_at": time.time(),
        }
    thread = threading.Thread(
        target=_run_in_thread, name=f"creator-render-{token}", daemon=True,
        kwargs=dict(token=token, owner=owner, project_id=project_id, doc_id=doc_id,
                    profile_id=profile_id, subtitle_occurrence_id=subtitle_occurrence_id,
                    session_id=session_id),
    )
    thread.start()
    return {"render_token": token, "state": "starting"}


def get_job(token: str, *, owner: str) -> Optional[Dict[str, Any]]:
    """Owner-scoped (CONTRATO.md rule 3): a token minted for another owner
    reads back as `None`, same as one that never existed."""
    with _JOBS_LOCK:
        job = _JOBS.get(token)
        if job is None or job.get("owner") != owner:
            return None
        snapshot = dict(job)

    live_progress = None
    live_state = snapshot["state"]
    ffmpeg_job_id = snapshot.get("ffmpeg_job_id")
    if ffmpeg_job_id:
        status = _adapter().status(ffmpeg_job_id)
        live_progress = status.progress
        if snapshot["state"] not in ("done", "error"):
            live_state = status.state

    return {
        "render_token": token, "state": snapshot["state"], "engine_state": live_state,
        "progress": live_progress, "ffmpeg_job_id": ffmpeg_job_id,
        "result": snapshot.get("result"), "error": snapshot.get("error"),
    }


def cancel_job(token: str, *, owner: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(token)
        if job is None or job.get("owner") != owner:
            return {"outcome": "unknown", "detail": "no such render job"}
        if job["state"] in ("done", "error"):
            return {"outcome": "too_late", "detail": job["state"]}
        job["cancel_requested"] = True
        ffmpeg_job_id = job.get("ffmpeg_job_id")

    if not ffmpeg_job_id:
        # `_on_job_id` will apply the cancel the instant the process is
        # spawned (see `_run_in_thread`) — the render never gets a chance
        # to run to completion once `cancel_requested` is set.
        return {"outcome": "requested", "detail": "will be cancelled once the process starts"}
    result = _adapter().cancel(ffmpeg_job_id)
    return {"outcome": result.outcome, "detail": result.detail}
