"""
media_runs.py — a render, from an approved recipe to an artifact with a story.

This is where Phase 3 meets Phase 0. `media_workflows` decides what may be
asked for, `media_backends.comfyui` talks to the engine, and this module keeps
the row that makes a render survive the web process and turns its outputs into
artifacts that can be explained a year later.

Three things it refuses to do:

**Hold a socket open for twenty minutes.** `start()` queues and returns; the
row carries the engine's job id, and `poll()` reconciles. A restart mid-render
loses nothing, because the truth is on the engine and in the row rather than
in a coroutine that died.

**Write a status it did not check.** After a restart the row says `running`
and might be wrong — the engine may have finished, failed, or forgotten. So
`poll()` asks the engine about `engine_job_id` rather than trusting what was
written before the process went away.

**Save a picture without its story.** Every artifact carries the workflow id,
version and fingerprint, the seed, the resolved inputs, the models AND their
licences, and the engine it ran on. The licence is the one people forget and
the one that matters when a file has been handed to a client.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from typing import Any, Dict, List, Mapping, Optional

from src import media_workflows as workflows
from src.contracts.base import now_iso
from src.media_backends import ComfyUIBackend, ComfyUIError
from src.media_workflows import TemplateError

logger = logging.getLogger(__name__)

#: Statuses a media run can be in. `unknown` is real: an engine that was
#: restarted has forgotten the job, and that is not the same as a failure.
STATUSES = ("pending", "queued", "running", "completed", "failed",
            "cancelled", "unknown")


def _backend(url: str = "") -> ComfyUIBackend:
    return ComfyUIBackend(url)


# ── looking before leaping ────────────────────────────────────────────────

def plan(workflow_id: str, inputs: Optional[Mapping[str, Any]] = None, *,
         version: str = "", engine_url: str = "",
         check_engine: bool = True) -> Dict[str, Any]:
    """What this render would be, without queueing it.

    Pure except for the optional engine question, which is the point of the
    `check_engine` flag: a caller composing a request wants the resolved
    values and the refusals; a caller about to submit also wants to know the
    checkpoint is actually on the machine."""
    workflow = workflows.load(workflow_id, version)
    if workflow is None:
        catalogue = workflows.catalogue()
        known = sorted({w.id for w in catalogue["workflows"]})
        return {"ok": False, "reason": "no_such_workflow",
                "detail": f"no approved template called {workflow_id!r}"
                          + (f"; there is {', '.join(known)}" if known else
                             ", and none are installed"),
                "broken_templates": catalogue["broken"]}

    try:
        rendered = workflows.render(workflow, inputs)
    except TemplateError as e:
        return {"ok": False, "reason": "bad_inputs", "field": e.path,
                "detail": e.message, "workflow": workflow.to_dict()}

    out: Dict[str, Any] = {
        "ok": True, "workflow": workflow.id, "version": workflow.version,
        "fingerprint": rendered["fingerprint"],
        "values": rendered["values"], "models": rendered["models"],
        "outputs": dict(workflow.outputs),
    }
    if not check_engine:
        return out

    engine = _backend(engine_url)
    gate = engine.probe()
    out["engine"] = {"url": engine.base_url, **gate}
    if not gate["ok"]:
        out["ok"] = False
        out["reason"] = gate["reason"]
        out["detail"] = gate["detail"]
        return out
    try:
        gap = engine.missing(rendered, requires_nodes=list(workflow.requires_nodes))
    except ComfyUIError as e:
        out["ok"] = False
        out["reason"] = "catalogue_unreadable"
        out["detail"] = str(e)
        return out
    if not gap["ok"]:
        out["ok"] = False
        out["reason"] = "missing_requirements"
        out["detail"] = gap["detail"]
        out["missing"] = {"nodes": gap["missing_nodes"], "models": gap["missing_models"]}
    return out


# ── starting one ──────────────────────────────────────────────────────────

def start(workflow_id: str, inputs: Optional[Mapping[str, Any]] = None, *,
          version: str = "", engine_url: str = "", owner: str = "",
          project_id: str = "", session_id: str = "",
          approval_id: str = "") -> Dict[str, Any]:
    """Queue a render and write the row that will outlive this process.

    The row is written **before** the job is queued and updated after, the
    same ordering as a workflow node: a process that dies between the two
    leaves a row saying `pending` with no engine id, which is recoverable and
    honest. The other order leaves a job running that nothing remembers."""
    from core.database import MediaRunRow, SessionLocal

    workflow = workflows.load(workflow_id, version)
    if workflow is None:
        return {"ok": False, "reason": "no_such_workflow", "detail": workflow_id}
    try:
        rendered = workflows.render(workflow, inputs)
    except TemplateError as e:
        return {"ok": False, "reason": "bad_inputs", "field": e.path,
                "detail": e.message}

    engine = _backend(engine_url)
    run_id = f"mrun_{uuid.uuid4().hex[:20]}"

    db = SessionLocal()
    try:
        db.add(MediaRunRow(
            id=run_id, workflow_id=workflow.id, workflow_version=workflow.version,
            workflow_fingerprint=rendered["fingerprint"],
            engine="comfyui", engine_url=engine.base_url,
            status="pending", reason="",
            values_json=json.dumps(rendered["values"], ensure_ascii=False),
            models_json=json.dumps(rendered["models"], ensure_ascii=False),
            owner=owner or None, project_id=project_id or None,
            session_id=session_id or None, approval_id=approval_id or None,
            created_at_iso=now_iso(), schema_version=1))
        db.commit()
    finally:
        db.close()

    try:
        job = engine.submit(rendered, requires_nodes=list(workflow.requires_nodes))
    except ComfyUIError as e:
        _update(run_id, status="failed", reason=f"{e.reason}: {e.detail}",
                ended_at=now_iso())
        return {"ok": False, "run_id": run_id, "reason": e.reason,
                "detail": e.detail, "workflow": workflow.id}

    _update(run_id, status="queued", engine_job_id=job["prompt_id"],
            started_at=now_iso())
    return {"ok": True, "run_id": run_id, "status": "queued",
            "engine_job_id": job["prompt_id"], "position": job.get("position"),
            "workflow": workflow.id, "version": workflow.version,
            "values": rendered["values"]}


def _update(run_id: str, **fields: Any) -> bool:
    from core.database import MediaRunRow, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(MediaRunRow, run_id)
        if row is None:
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _row_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": row.id, "workflow": row.workflow_id, "version": row.workflow_version,
        "fingerprint": row.workflow_fingerprint,
        "engine": row.engine, "engine_url": row.engine_url,
        "engine_job_id": row.engine_job_id,
        "status": row.status, "reason": row.reason or "",
        "values": json.loads(row.values_json or "{}"),
        "models": json.loads(row.models_json or "[]"),
        "artifact_ids": [a for a in (row.artifact_ids or "").split(",") if a],
        "owner": row.owner or "", "project_id": row.project_id or "",
        "session_id": row.session_id or "", "approval_id": row.approval_id or "",
        "created_at": row.created_at_iso, "started_at": row.started_at,
        "ended_at": row.ended_at,
    }


def get(run_id: str) -> Optional[Dict[str, Any]]:
    from core.database import MediaRunRow, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(MediaRunRow, run_id)
        return _row_dict(row) if row else None
    finally:
        db.close()


def recent(*, owner: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    from core.database import MediaRunRow, SessionLocal
    db = SessionLocal()
    try:
        query = db.query(MediaRunRow)
        if owner:
            query = query.filter(MediaRunRow.owner == owner)
        rows = query.order_by(MediaRunRow.created_at_iso.desc()).limit(
            max(1, min(limit, 200))).all()
        return [_row_dict(r) for r in rows]
    finally:
        db.close()


# ── watching one ──────────────────────────────────────────────────────────

def poll(run_id: str, *, collect: bool = True) -> Dict[str, Any]:
    """Ask the engine what happened, write it down, and keep the outputs.

    Safe to call as often as anyone likes, and safe to call after a restart —
    which is the whole reason it asks the engine instead of reading the status
    it wrote earlier. A finished run is answered from the row without asking
    again: the artifacts are already in the store, and content-hash storage
    means collecting twice would be harmless but pointless."""
    record = get(run_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "run_id": run_id}
    if record["status"] in ("completed", "failed", "cancelled"):
        return {"ok": True, "run_id": run_id, **record, "checked": False}
    if not record["engine_job_id"]:
        return {"ok": True, "run_id": run_id, **record, "checked": False,
                "detail": "this run never reached the engine"}

    engine = _backend(record["engine_url"] or "")
    try:
        state = engine.status(record["engine_job_id"])
    except ComfyUIError as e:
        # The engine being down does NOT make the run failed. It makes what
        # the run is doing unknown, and a status written on a guess is how a
        # finished render gets reported as a failure.
        return {"ok": True, "run_id": run_id, **record, "checked": True,
                "engine_reachable": False, "detail": str(e)}

    if state["status"] in ("queued", "running"):
        _update(run_id, status=state["status"])
        return {"ok": True, "run_id": run_id, **{**record, "status": state["status"]},
                "checked": True, "ahead": state.get("ahead")}

    if state["status"] == "failed":
        _update(run_id, status="failed", reason=state.get("reason") or "the render failed",
                ended_at=now_iso())
        return {"ok": True, "run_id": run_id,
                **{**record, "status": "failed", "reason": state.get("reason", "")},
                "checked": True}

    if state["status"] == "unknown":
        _update(run_id, status="unknown", reason=state.get("reason") or "")
        return {"ok": True, "run_id": run_id,
                **{**record, "status": "unknown", "reason": state.get("reason", "")},
                "checked": True}

    outputs = engine.outputs(record["engine_job_id"])
    if not collect:
        return {"ok": True, "run_id": run_id, **{**record, "status": "completed"},
                "checked": True, "outputs": outputs}

    kept = _collect(record, outputs, engine)
    _update(run_id, status="completed", ended_at=now_iso(),
            artifact_ids=",".join(a["id"] for a in kept["artifacts"]))
    return {"ok": True, "run_id": run_id,
            **{**record, "status": "completed"},
            "checked": True, "artifacts": kept["artifacts"],
            "skipped": kept["skipped"]}


def _collect(record: Mapping[str, Any], outputs: List[Dict[str, Any]],
             engine: ComfyUIBackend) -> Dict[str, Any]:
    """Download what the engine made and put it in the artifact store, with
    the whole story attached."""
    from src import artifact_store
    from src.contracts import ExecutionResult

    scratch = artifact_store.run_dir(record["id"])
    names: List[str] = []
    for descriptor in outputs:
        try:
            written = engine.download(descriptor, into=scratch)
            names.append(os.path.basename(written))
        except ComfyUIError as e:
            logger.warning("media run %s: could not fetch %s: %s",
                           record["id"], descriptor.get("filename"), e)

    models = record.get("models") or []
    result = ExecutionResult.parse({
        "run_id": record["id"], "backend": "media_worker", "status": "completed",
        "exit_code": 0, "started_at": record.get("started_at") or record["created_at"],
        "ended_at": now_iso(), "artifact_filenames": names,
    })
    collected = artifact_store.collect(
        result, source_dir=scratch,
        owner=record.get("owner") or "", project_id=record.get("project_id") or "",
        skill_id=record["workflow"], skill_version=record["version"],
        provenance={
            "recipe": record["workflow"],
            "recipe_version": record["version"],
            "recipe_fingerprint": record.get("fingerprint") or None,
            "engine": record.get("engine") or "comfyui",
            "engine_job_id": record.get("engine_job_id") or "",
            "model": ", ".join(str(m.get("name")) for m in models),
            # The licence is the field people forget and the one that matters
            # once a file has been handed to a client.
            "model_license": ", ".join(str(m.get("license") or "unstated")
                                       for m in models),
            "seed": (record.get("values") or {}).get("seed"),
            # A DIGEST of the inputs, not the inputs. A prompt can carry a
            # client's name or an unreleased product, and an artifact row is
            # read by more people than a media run row is. The values
            # themselves stay on the run, which is one lookup away for anyone
            # who is allowed to see them.
            "inputs_digest": _inputs_digest(record.get("values") or {}),
            "note": f"rendered from the approved template {record['workflow']} "
                    f"{record['version']}; the graph was not written by a model. "
                    f"The exact inputs are on media run {record['id']}.",
        })
    artifact_store.persist(collected.artifacts,
                           session_id=record.get("session_id") or "")

    shutil.rmtree(scratch, ignore_errors=True)
    return {"artifacts": [a.to_dict() for a in collected.artifacts],
            "skipped": [dict(s) for s in collected.skipped]}


def _inputs_digest(values: Mapping[str, Any]) -> str:
    """The same length-prefixed rule the contracts use, over the resolved
    values in a fixed order. Two renders that used the same inputs get the
    same digest; one that changed a single word does not."""
    from src.contracts.base import fingerprint
    return fingerprint([(k, values[k]) for k in sorted(values)])


def cancel(run_id: str) -> Dict[str, Any]:
    """Stop a render and free the engine's queue.

    A cancel that leaves the job to start a moment later is worse than an
    error, so the backend does both halves. Here we only refuse to cancel
    something that already finished — undoing that is not a cancel."""
    record = get(run_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "run_id": run_id}
    if record["status"] in ("completed", "failed", "cancelled"):
        return {"ok": False, "reason": f"already_{record['status']}", "run_id": run_id}
    if not record["engine_job_id"]:
        _update(run_id, status="cancelled", reason="cancelled before it was queued",
                ended_at=now_iso())
        return {"ok": True, "run_id": run_id, "status": "cancelled",
                "detail": "it had not reached the engine"}

    engine = _backend(record["engine_url"] or "")
    try:
        stopped = engine.cancel(record["engine_job_id"])
    except ComfyUIError as e:
        return {"ok": False, "run_id": run_id, "reason": e.reason, "detail": e.detail}
    _update(run_id, status="cancelled", ended_at=now_iso(),
            reason=f"cancelled while {stopped.get('was')}")
    return {"ok": True, "run_id": run_id, "status": "cancelled", **stopped}
