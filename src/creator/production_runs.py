"""production_runs.py — WP10: adapter submits, projected onto MediaRun.

ADR-02 ("un único propietario del lifecycle"): `src/media_runs.py` and its
`core.database.MediaRunRow` table are the one authority for a render's
lifecycle. This module does NOT create a second `creator_jobs` table with an
independent status — every write below targets that SAME `MediaRunRow`
table, using the SAME status vocabulary `src/media_runs.py::STATUSES`
already defines.

For the ComfyUI adapter, the row already exists: `media_runs.start()` wrote
it, and the adapter's `job_id` IS the MediaRun `run_id`. `record_submit()`
for that adapter is a pure read-through — nothing here duplicates what
`media_runs.start()`/`.poll()` already did.

For an adapter with no MediaRun authority of its own (ffmpeg today, and any
adapter added later that is not ComfyUI), THIS module is the row's only
writer — it inserts through the same `core.database.MediaRunRow` model
`src/media_runs.py` itself imports, so a UI reading `media_runs.recent()`
sees an ffmpeg composition next to a ComfyUI render with no adapter-specific
branch. This does not require editing `src/media_runs.py` (CONTRATO.md: this
lot may not touch it, WP30 already did): the row SCHEMA is the authority,
not one module's ComfyUI-shaped helper functions, and every column written
below is one `src/media_runs.py` already defines and already reads back
through `_row_dict()`.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.contracts.base import now_iso
from src.creator.adapter_port import CollectResult

#: Adapters that already have a MediaRun row written by `media_runs.start()`
#: — this module reads those back rather than writing a second copy.
_PROJECTED_BY_MEDIA_RUNS = frozenset({"comfyui"})

#: `SubmitResult.state` -> the `media_runs.STATUSES` vocabulary.
_SUBMIT_STATE_TO_STATUS = {
    "accepted": "queued",
    "rejected_before_queue": "failed",
    "accepted_uncertain": "submit_unknown",
}


def _row_dict_for(row) -> Dict[str, Any]:
    from src import media_runs
    return media_runs._row_dict(row)  # the SAME projection media_runs.get()/.recent() return


def record_submit(*, adapter_name: str, engine: str, op: str, job_id: str,
                   submit_state: str, owner: str, project_id: str = "",
                   session_id: str = "", approval_id: str = "",
                   values: Optional[Mapping[str, Any]] = None,
                   reason: str = "") -> Dict[str, Any]:
    """The MediaRun projection of one adapter submit — see module docstring
    for why this does, and does not, write for every adapter."""
    if not job_id:
        return {"id": "", "status": "failed", "engine": engine, "reason": reason,
                "workflow": f"{adapter_name}:{op}"}

    if adapter_name in _PROJECTED_BY_MEDIA_RUNS:
        from src import media_runs
        record = media_runs.get(job_id)
        return record if record is not None else {
            "id": job_id, "status": "unknown", "engine": engine,
            "reason": "media_runs has no row for this job id",
        }

    from core.database import MediaRunRow, SessionLocal

    status = _SUBMIT_STATE_TO_STATUS.get(submit_state, "unknown")
    db = SessionLocal()
    try:
        row = db.get(MediaRunRow, job_id)
        if row is None:
            row = MediaRunRow(
                id=job_id, workflow_id=f"{adapter_name}:{op}", workflow_version="1",
                engine=engine, engine_url="", client_id="",
                status=status, reason=reason or "",
                values_json=json.dumps(dict(values or {}), ensure_ascii=False),
                models_json="[]",
                owner=owner or None, project_id=project_id or None,
                session_id=session_id or None, approval_id=approval_id or None,
                created_at_iso=now_iso(), schema_version=1,
                started_at=now_iso() if status in ("queued", "running") else None,
                ended_at=now_iso() if status == "failed" else None,
            )
            db.add(row)
        else:
            row.status = status
            row.reason = reason or row.reason
            if status == "failed" and not row.ended_at:
                row.ended_at = now_iso()
        db.commit()
        db.refresh(row)
        return _row_dict_for(row)
    finally:
        db.close()


def mark_terminal(job_id: str, *, adapter_name: str, status: str, reason: str = "",
                   artifact_ids: Sequence[str] = ()) -> Dict[str, Any]:
    """Move a non-`media_runs`-projected run to a terminal status
    (`completed`, `failed`, `cancelled`) with whichever occurrence ids were
    registered for it. A no-op for ComfyUI — `media_runs.poll()`/`.cancel()`
    already own that transition for it."""
    if adapter_name in _PROJECTED_BY_MEDIA_RUNS:
        from src import media_runs
        return media_runs.get(job_id) or {"id": job_id, "status": "unknown"}

    from core.database import MediaRunRow, SessionLocal

    db = SessionLocal()
    try:
        row = db.get(MediaRunRow, job_id)
        if row is None:
            return {"id": job_id, "status": "unknown", "reason": "no such run"}
        if row.status in ("completed", "failed", "cancelled"):
            return _row_dict_for(row)  # already terminal — never overwrite a settled outcome
        row.status = status
        row.reason = reason or row.reason
        row.ended_at = now_iso()
        if artifact_ids:
            row.artifact_ids = ",".join(artifact_ids)
        db.commit()
        db.refresh(row)
        return _row_dict_for(row)
    finally:
        db.close()


def link_outputs(job_id: str, *, adapter_name: str, collected: CollectResult,
                  owner: str, project_id: str, op: str,
                  input_occurrence_ids: Sequence[str] = (),
                  session_id: str = "") -> Dict[str, Any]:
    """Register a validated `CollectResult`'s outputs as artifact
    occurrences with provenance, and settle the run.

    The ComfyUI adapter's `collect()` already returns outputs that
    `media_runs.poll()` registered itself — calling this for it would
    double-register, so it is a read-through here exactly like
    `record_submit()`. For every other adapter this is the ONLY place their
    outputs become artifacts, using the same `artifact_store.collect()` +
    `.persist()` pipeline `media_runs._collect_into()` uses, so a Creator
    render and a legacy one are indistinguishable once they land in the
    Library (WP03) — same hashing, same `validate_artifact_bytes`, same
    `derived_from` relation via `source_artifact_ids`.
    """
    if adapter_name in _PROJECTED_BY_MEDIA_RUNS:
        from src import media_runs
        record = media_runs.get(job_id)
        return record or {"id": job_id, "status": "unknown"}

    if not collected.ok or not collected.outputs:
        return mark_terminal(job_id, adapter_name=adapter_name, status="failed",
                              reason=collected.detail or "collect produced no valid output")

    from src import artifact_store
    from src import artifact_identity as identity
    from src.contracts import ExecutionResult
    from src.contracts.blob import ArtifactOccurrence

    source_dir = os.path.dirname(collected.outputs[0].path) or "."
    filenames = [os.path.basename(o.path) for o in collected.outputs]
    if len({os.path.dirname(o.path) for o in collected.outputs}) != 1:
        return mark_terminal(job_id, adapter_name=adapter_name, status="failed",
                              reason="collected outputs are not in one bounded directory")

    result = ExecutionResult.parse({
        "run_id": job_id, "backend": f"creator_adapter.{adapter_name}", "status": "completed",
        "exit_code": 0, "started_at": now_iso(), "ended_at": now_iso(),
        "artifact_filenames": filenames,
    })
    # `artifact_store.collect()` moves the bytes into the store and hashes
    # them (dedup by content); it does NOT persist an occurrence — that is
    # done below, occurrence by occurrence, so `relations.derived_from` can
    # be set the same way `src/creator/library.py`'s proxy path already
    # does (WP03), which `artifact_store.persist()` does not expose.
    skill_id = f"creator.adapter.{adapter_name}.{op}".replace("_", "-")
    collected_batch = artifact_store.collect(
        result, source_dir=source_dir, owner=owner or "", project_id=project_id or "",
        skill_id=skill_id, skill_version="1.0.0",
        provenance={
            "recipe": f"{adapter_name}:{op}", "recipe_version": "1.0.0",
            "engine": adapter_name, "engine_job_id": job_id,
            "source_artifact_ids": list(input_occurrence_ids),
            "note": f"produced by the Creator {adapter_name} adapter ({op}); "
                    f"job {job_id}.",
        })
    if collected_batch.skipped or len(collected_batch.artifacts) != len(filenames):
        return mark_terminal(job_id, adapter_name=adapter_name, status="failed",
                              reason="not every collected output could be registered")

    artifact_ids: List[str] = []
    artifact_dicts: List[Dict[str, Any]] = []
    for art in collected_batch.artifacts:
        identity.ensure_blob(sha256=art.sha256, byte_size=art.byte_size,
                              filename=art.filename, media_type=art.media_type)
        occurrence = ArtifactOccurrence.parse({
            "id": art.id, "kind": art.kind, "blob_sha256": art.sha256,
            "label": art.label, "owner": art.owner, "project_id": art.project_id,
            "run_id": art.run_id, "session_id": session_id,
            "skill_id": art.skill_id, "skill_version": art.skill_version,
            "created_at": art.created_at, "partial": art.partial,
            "provenance": art.provenance.to_dict(), "retention": art.retention.to_dict(),
            "relations": {"derived_from": list(input_occurrence_ids)} if input_occurrence_ids else {},
        })
        recorded, _made = identity.ensure_occurrence(occurrence)
        artifact_ids.append(recorded.id)
        artifact_dicts.append(recorded.to_dict())

    settled = mark_terminal(job_id, adapter_name=adapter_name, status="completed",
                             artifact_ids=artifact_ids)
    settled["artifacts"] = artifact_dicts
    return settled
