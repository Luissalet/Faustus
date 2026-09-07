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
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from src import media_workflows as workflows
from src.contracts.base import now_iso
from src.media_backends import ComfyUIBackend, ComfyUIError
from src.media_workflows import TemplateError

logger = logging.getLogger(__name__)

#: Statuses a media run can be in. Three of them are about the SUBMIT rather
#: than the render, because that is the gap where a row and a GPU can
#: disagree: `submit_pending` is an intention written down before anything was
#: sent, `submitted` is an id we have but a queue position we have not asked
#: about yet, and `submit_unknown` is the honest answer when the call failed in
#: a way that cannot rule out the engine having taken the job. `unknown` is the
#: later cousin of that one: an engine that was restarted has forgotten a job
#: it certainly had, which is still not the same as a failure. `pending`
#: survives for rows written before the outbox existed.
STATUSES = ("submit_pending", "submitted", "submit_unknown", "pending",
            "queued", "running", "completed", "failed", "cancelled", "unknown")

#: Statuses meaning "we do not know whether a job of ours is on a GPU".
UNSETTLED_SUBMIT = ("submit_pending", "submit_unknown")
_POLL_LOCKS = tuple(threading.RLock() for _ in range(64))

#: Submit failures that PROVE the engine never took the job: it read the graph
#: and refused it. Anything else -- a socket that died, a gateway that timed
#: out, a reply we could not parse -- leaves the question open, and answering
#: it with `failed` is exactly how a real render ends up burning a GPU with
#: nothing pointing at it.
REFUSED_BEFORE_QUEUE = frozenset({"missing_requirements", "rejected_by_engine",
                                  "empty_graph"})

#: What we call ourselves to an engine when there is no run to name. A real
#: submit always overrides it: a constant client id correlates nothing.
DEFAULT_CLIENT_ID = "faustus"


def _backend(url: str = "", *, client_id: str = "") -> ComfyUIBackend:
    return ComfyUIBackend(url, client_id=client_id or DEFAULT_CLIENT_ID)


def _iso_ago(seconds: int) -> str:
    """`now_iso()` as of N seconds ago, for comparing against `created_at`.

    The run timestamps are ISO-8601 UTC strings and are compared as strings,
    so a cutoff has to be minted in exactly the same shape.
    """
    moment = (datetime.now(timezone.utc).replace(microsecond=0)
              - timedelta(seconds=max(0, int(seconds))))
    return moment.isoformat().replace("+00:00", "Z")


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

    from src.media_backends import pool

    picked = pool.choose(rendered, requires_nodes=list(workflow.requires_nodes),
                         prefer=engine_url)
    out["engines"] = picked["why"]
    if not picked["ok"]:
        out["ok"] = False
        out["reason"] = picked["reason"]
        out["detail"] = picked["detail"]
        # The models nobody has, gathered from what each engine said, so the
        # answer to "why not" is a file name rather than a count.
        missing = sorted({m for w in picked["why"]
                          for m in (w.get("missing_models") or ())})
        if missing:
            out["reason"] = "missing_requirements"
            out["missing"] = {"nodes": [], "models": missing}
        return out

    engine = _backend(picked["url"])
    out["engine"] = {"url": engine.base_url, "ok": True,
                     "gpu": (picked.get("engine") or {}).get("gpu", ""),
                     "chosen_because": picked.get("chosen_because", "")}
    try:
        gap = engine.missing(rendered, requires_nodes=list(workflow.requires_nodes))
    except ComfyUIError as e:
        out["ok"] = False
        out["reason"] = "catalogue_unreadable"
        out["detail"] = str(e)
        return out
    if not gap["ok"]:
        # The pool only checks checkpoints; a missing NODE is caught here.
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
    same ordering as a workflow node. On its own that ordering is not enough:
    a process killed between the two used to leave a row claiming the render
    never reached the engine while the engine was already rendering it, and
    nothing could ever find that job again -- not to poll it, not to cancel
    it, not to collect its output.

    So the first write is an OUTBOX entry rather than a hopeful `pending`. It
    says `submit_pending` and carries a client id derived from this run, which
    ComfyUI echoes back on both /queue and /history. That id is the thread
    `reconcile()` pulls on afterwards, and it is what makes the difference
    between a lost render and a slow one.

    The failure classification matters as much as the ordering: only a refusal
    the engine actually spoke (a missing model, a rejected graph) is written
    down as `failed`. A socket that died mid-POST is `submit_unknown`, because
    it cannot rule out a job now sitting on a GPU."""
    from core.database import MediaRunRow, SessionLocal

    workflow = workflows.load(workflow_id, version)
    if workflow is None:
        return {"ok": False, "reason": "no_such_workflow", "detail": workflow_id}
    try:
        rendered = workflows.render(workflow, inputs)
    except TemplateError as e:
        return {"ok": False, "reason": "bad_inputs", "field": e.path,
                "detail": e.message}

    # Which engine, and why not the others. With one ComfyUI this picks it and
    # says so; with two it fills the smaller card first, which is throughput
    # rather than politeness — see media_backends/pool.py.
    from src.media_backends import pool

    picked = pool.choose(rendered, requires_nodes=list(workflow.requires_nodes),
                         prefer=engine_url)
    if not picked["ok"]:
        return {"ok": False, "reason": picked["reason"],
                "detail": picked["detail"], "why": picked["why"]}
    run_id = f"mrun_{uuid.uuid4().hex[:20]}"
    client_id = client_id_for(run_id)
    engine = _backend(picked["url"], client_id=client_id)

    db = SessionLocal()
    try:
        db.add(MediaRunRow(
            id=run_id, workflow_id=workflow.id, workflow_version=workflow.version,
            workflow_fingerprint=rendered["fingerprint"],
            engine="comfyui", engine_url=engine.base_url,
            client_id=client_id,
            status="submit_pending", reason="",
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
        if e.reason in REFUSED_BEFORE_QUEUE or e.reason.startswith("http_4"):
            # The engine spoke and said no. Nothing is queued, so this is a
            # finished story rather than an open question.
            _update(run_id, status="failed", reason=f"{e.reason}: {e.detail}",
                    ended_at=now_iso())
            return {"ok": False, "run_id": run_id, "status": "failed",
                    "reason": e.reason, "detail": e.detail, "workflow": workflow.id}
        # Everything else is unresolved on purpose. `reconcile()` asks the
        # engine whether it is holding a job with this run's client id, which
        # is a question that can actually be answered -- unlike "did the POST
        # arrive before the socket closed?".
        _update(run_id, status="submit_unknown", reason=f"{e.reason}: {e.detail}")
        logger.warning("media run %s: the submit outcome is unknown (%s); it will "
                       "be reconciled by client id %s", run_id, e.reason, client_id)
        return {"ok": False, "run_id": run_id, "status": "submit_unknown",
                "reason": e.reason, "detail": e.detail, "workflow": workflow.id,
                "client_id": client_id, "recoverable": True}

    _update(run_id, status="queued", engine_job_id=job["prompt_id"],
            started_at=now_iso())
    return {"ok": True, "run_id": run_id, "status": "queued",
            "engine_job_id": job["prompt_id"], "position": job.get("position"),
            "workflow": workflow.id, "version": workflow.version,
            "values": rendered["values"],
            "engine_url": engine.base_url, "client_id": client_id,
            "chosen_because": picked.get("chosen_because", ""),
            "engine_gpu": (picked.get("engine") or {}).get("gpu", "")}


def client_id_for(run_id: str) -> str:
    """What this run calls itself to the engine.

    Deterministic, so a reconciliation pass can compute it from the row rather
    than having to have stored it -- and so a job found on the engine names
    the run that owns it instead of merely proving Faustus submitted it.
    """
    return f"{DEFAULT_CLIENT_ID}:{run_id}"


# ── closing the outbox ────────────────────────────────────────────────────

def reconcile_run(run_id: str, *, grace_seconds: int = 60,
                  record: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Settle one run whose submit outcome was never written down.

    Three outcomes, and they are genuinely different. The engine is holding a
    job with our client id: adopt its prompt id and the run is alive again --
    pollable, cancellable, collectable. The engine is reachable and has no such
    job: it never got queued, which is a real failure and is recorded as one.
    The engine cannot be reached: nothing is decided, because a status written
    on a guess is the bug one level up.

    `grace_seconds` keeps this off a submit that is merely still in flight; a
    caller that knows the sender is gone (a startup sweep, a poll) passes 0.
    """
    record = dict(record) if record is not None else get(run_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "run_id": run_id, "changed": False}
    if record["engine_job_id"]:
        return {"ok": True, "run_id": run_id, "reason": "already_known",
                "changed": False}
    if record["status"] not in UNSETTLED_SUBMIT:
        return {"ok": True, "run_id": run_id, "reason": "nothing_to_reconcile",
                "changed": False}
    if record["created_at"] > _iso_ago(grace_seconds):
        return {"ok": True, "run_id": run_id, "reason": "too_soon", "changed": False}

    client_id = record.get("client_id") or ""
    if not client_id:
        # A row from before per-run client ids. There is nothing to correlate
        # on, and guessing would adopt somebody else's job.
        changed = _update(run_id, _only_unsubmitted=True, status="failed", ended_at=now_iso(),
                reason="this run carries no client id, so a job of its on the "
                       "engine cannot be told from anyone else's")
        return {"ok": True, "run_id": run_id, "reason": "no_correlation",
                "changed": changed}

    engine = _backend(record["engine_url"] or "", client_id=client_id)
    try:
        found = engine.find_by_client_id(client_id)
    except ComfyUIError as e:
        return {"ok": True, "run_id": run_id, "reason": "engine_unreachable",
                "detail": str(e), "changed": False}

    if found["found"]:
        changed = _update(run_id, _only_unsubmitted=True, status="submitted", engine_job_id=found["prompt_id"],
                started_at=record.get("started_at") or now_iso(),
                reason=f"adopted from the engine's {found['where']} by client id")
        logger.info("media run %s adopted engine job %s from %s", run_id,
                    found["prompt_id"], found["where"])
        return {"ok": True, "run_id": run_id, "reason": "adopted",
                "engine_job_id": found["prompt_id"], "where": found["where"],
                "changed": changed}

    changed = _update(run_id, _only_unsubmitted=True, status="failed", ended_at=now_iso(),
            reason="the engine is reachable and holds no job carrying this run's "
                   "client id, so the prompt never reached the queue")
    return {"ok": True, "run_id": run_id, "reason": "never_queued" if changed else "already_settled", "changed": changed}


def reconcile(*, grace_seconds: int = 60, limit: int = 50) -> Dict[str, Any]:
    """Settle every run left in an unsettled submit state.

    Meant for startup, which is exactly when the population of these is
    largest: everything this process was submitting when it was killed. It is
    also the orphan collector the audit asked for -- a render nobody knows
    about is found by the metadata the engine carries for us, and once its
    prompt id is back on the row the ordinary `poll()` collects its outputs
    like any other run.
    """
    from core.database import MediaRunRow, SessionLocal

    db = SessionLocal()
    try:
        rows = (db.query(MediaRunRow)
                .filter(MediaRunRow.status.in_(UNSETTLED_SUBMIT),
                        MediaRunRow.engine_job_id.is_(None))
                .order_by(MediaRunRow.created_at_iso.asc())
                .limit(max(1, min(limit, 500))).all())
        pending = [_row_dict(r) for r in rows]
    finally:
        db.close()

    settled = [reconcile_run(r["id"], grace_seconds=grace_seconds, record=r)
               for r in pending]
    return {"ok": True, "checked": len(settled),
            "adopted": [s["run_id"] for s in settled if s.get("reason") == "adopted"],
            "never_queued": [s["run_id"] for s in settled
                             if s.get("reason") == "never_queued"],
            "undecided": [s["run_id"] for s in settled if not s.get("changed")],
            "runs": settled}


def _update(run_id: str, *, _only_unsubmitted: bool = False, **fields: Any) -> bool:
    from core.database import MediaRunRow, SessionLocal
    db = SessionLocal()
    try:
        query = db.query(MediaRunRow).filter(MediaRunRow.id == run_id,
            MediaRunRow.status.notin_(['completed', 'failed', 'cancelled']))
        if _only_unsubmitted:
            from sqlalchemy import or_
            query = query.filter(MediaRunRow.status.in_(UNSETTLED_SUBMIT),
                                 or_(MediaRunRow.engine_job_id.is_(None), MediaRunRow.engine_job_id == ''))
        changed = query.update(fields, synchronize_session=False)
        db.commit()
        return bool(changed)
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
        "client_id": getattr(row, "client_id", None) or "",
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

def artifact_details(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Reload outputs collected by another worker, without an engine call.

    Stale ids cannot expose another owner's result or resurrect removed data.
    """
    from src import artifact_catalog
    from src.contracts.artifact import Provenance
    from core.database import SessionLocal
    found = []
    with SessionLocal() as db:
        for artifact_id in record.get('artifact_ids') or []:
            row = artifact_catalog.get(db, artifact_id)
            if row is None or (row.owner or '') != record.get('owner') or row.run_id != record.get('id'):
                continue
            provenance = {key: getattr(row, key, None) for key in Provenance._KEYS
                          if key not in ('note', 'source_artifact_ids')}
            provenance['note'] = row.provenance_note or ''
            provenance['source_artifact_ids'] = json.loads(getattr(row, 'source_artifact_ids', None) or '[]')
            found.append({'id': row.id, 'label': row.label,
                          'kind': row.kind, 'filename': row.filename,
                          'sha256': row.sha256, 'owner': row.owner or '',
                          'project_id': row.project_id or '', 'run_id': row.run_id or '',
                          'skill_id': row.skill_id or '', 'skill_version': row.skill_version or '',
                          'created_at': str(row.created_at).replace(' ', 'T'),
                          'partial': bool(row.partial),
                          'retention': {'policy': row.retention_policy or 'keep',
                                        'days': row.retention_days, 'reason': row.retention_reason or ''},
                          'media_type': row.media_type, 'byte_size': row.byte_size,
                          'provenance': provenance})
    return found


def poll(run_id: str, *, collect: bool = True) -> Dict[str, Any]:
    # HTTP, workflow and media continuation can ask at once. Bound the lock
    # inventory; isolated collection directories also protect multi-process use.
    with _POLL_LOCKS[hash(run_id) % len(_POLL_LOCKS)]:
        result = _poll(run_id, collect=collect)
        current = get(run_id)
        if current and collect:
            result.update(current)
            if current['status'] == 'completed' and not result.get('artifacts'):
                result['artifacts'] = artifact_details(current)
        return result


def _poll(run_id: str, *, collect: bool = True) -> Dict[str, Any]:
    """Ask the engine what happened, write it down, and keep the outputs.

    Safe to call as often as anyone likes, and safe to call after a restart —
    which is the whole reason it asks the engine instead of reading the status
    it wrote earlier. A finished run is answered from the row without asking
    again: the artifacts are already in the store, and content-hash storage
    means collecting twice would be harmless but pointless.

    A run whose submit was never settled is reconciled here first. Polling is
    the moment somebody is actually asking about a render, so it is also the
    right moment to find out whether the job we lost track of is on a GPU."""
    record = get(run_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "run_id": run_id}
    if record["status"] in UNSETTLED_SUBMIT and not record["engine_job_id"]:
        # The outbox for this run is still open. Whoever was sending it is not
        # here any more -- we are -- so ask the engine, rather than report that
        # a run "never reached" an engine that may be rendering it right now.
        reconcile_run(run_id, grace_seconds=60 if record['status'] == 'submit_pending' else 0, record=record)
        record = get(run_id) or record
    if record["status"] in ("completed", "failed", "cancelled"):
        return {"ok": True, "run_id": run_id, **record, "checked": False}
    if not record["engine_job_id"]:
        return {"ok": True, "run_id": run_id, **record, "checked": False,
                "detail": "this run never reached the engine"}

    engine = _backend(record["engine_url"] or "",
                      client_id=record.get("client_id") or "")
    try:
        state = engine.status(record["engine_job_id"])
    except ComfyUIError as e:
        # The engine being down does NOT make the run failed. It makes what
        # the run is doing unknown, and a status written on a guess is how a
        # finished render gets reported as a failure.
        _update(run_id, reason=f'Engine unavailable; retrying: {e}')
        return {"ok": True, "run_id": run_id, **record, "checked": True,
                "engine_reachable": False, "detail": str(e)}

    if state["status"] in ("queued", "running"):
        _update(run_id, status=state["status"], reason='')
        return {"ok": True, "run_id": run_id, **{**record, "status": state["status"]},
                "checked": True, "ahead": state.get("ahead")}

    if state["status"] in ("failed", "cancelled"):
        # `cancelled` travels as itself. ComfyUI reports an interruption in the
        # same shape as a failure, and telling somebody who stopped a render
        # that it broke is a small lie that costs a real minute of worry.
        _update(run_id, status=state["status"],
                reason=state.get("reason") or f"the render {state['status']}",
                ended_at=now_iso())
        return {"ok": True, "run_id": run_id,
                **{**record, "status": state["status"],
                   "reason": state.get("reason", "")},
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

    if record["artifact_ids"]:
        # A previous pass already downloaded and stored these and died before
        # writing the status. Collecting again would be harmless -- the store
        # is content-hashed -- and pointless; the second pass only has to
        # finish the sentence the first one started.
        _update(run_id, status="completed",
                ended_at=record.get("ended_at") or now_iso())
        return {"ok": True, "run_id": run_id,
                **{**record, "status": "completed"},
                "checked": True, "artifacts": [], "skipped": [],
                "detail": "the outputs of this run were already collected"}

    if not outputs:
        _update(run_id, status='failed', ended_at=now_iso(),
                reason='the engine finished without reporting any output files')
        return {'ok': True, 'run_id': run_id, 'checked': True}
    try:
        kept = _collect(record, outputs, engine)
    except (ComfyUIError, OSError, ValueError) as exc:
        # The render exists; a temporarily inaccessible /view or full disk is
        # not a completed result. Keep it retryable and name the retrieval failure.
        _update(run_id, status='running', reason=f'Could not collect render outputs: {exc}')
        return {'ok': True, 'run_id': run_id, 'checked': True, 'collection_pending': True}
    _update(run_id, status="completed", ended_at=now_iso(),
            reason='',
            artifact_ids=",".join(a["id"] for a in kept["artifacts"]))
    return {"ok": True, "run_id": run_id,
            **{**record, "status": "completed"},
            "checked": True, "artifacts": kept["artifacts"],
            "skipped": kept["skipped"]}


def _collect(record: Mapping[str, Any], outputs: List[Dict[str, Any]],
             engine: ComfyUIBackend) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='faustus-render-') as scratch:
        return _collect_into(record, outputs, engine, scratch)


def _collect_into(record: Mapping[str, Any], outputs: List[Dict[str, Any]],
                  engine: ComfyUIBackend, scratch: str) -> Dict[str, Any]:
    """Download what the engine made and put it in the artifact store, with
    the whole story attached."""
    from src import artifact_store
    from src.contracts import ExecutionResult

    names: List[str] = []
    for descriptor in outputs:
        try:
            written = engine.download(descriptor, into=scratch)
            names.append(os.path.basename(written))
        except ComfyUIError as e:
            logger.warning("media run %s: could not fetch %s: %s",
                           record["id"], descriptor.get("filename"), e)
            raise

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
    if collected.skipped or len(collected.artifacts) != len(outputs):
        raise ValueError('not every render output could be collected')
    artifact_store.persist(collected.artifacts,
                           session_id=record.get("session_id") or "")

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
    something that already finished — undoing that is not a cancel.

    Cancelling an already-cancelled run is not a refusal, though: the caller
    asked for a state and the run is in it, so a retried click, a retried
    request and a cleanup pass all get the same answer. And a run whose submit
    was never settled is reconciled before anything is written down —
    cancelling the ROW while the engine holds the job is precisely the orphan
    this module exists to stop making."""
    record = get(run_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "run_id": run_id}
    if record["status"] == "cancelled":
        return {"ok": True, "run_id": run_id, "status": "cancelled",
                "reason": "already_cancelled", "idempotent": True}
    if record["status"] in ("completed", "failed"):
        return {"ok": False, "reason": f"already_{record['status']}", "run_id": run_id}
    if record["status"] in UNSETTLED_SUBMIT and not record["engine_job_id"]:
        settled = reconcile_run(run_id, grace_seconds=60 if record['status'] == 'submit_pending' else 0, record=record)
        record = get(run_id) or record
        if settled.get('reason') in ('too_soon', 'engine_unreachable'):
            return {'ok': False, 'run_id': run_id, 'status': record['status'],
                    'reason': 'submission_not_settled',
                    'detail': 'The submission is still in progress or the engine cannot be reached. Retry cancellation shortly.'}
        if record['status'] in ('completed', 'failed', 'cancelled'):
            return {'ok': record['status'] == 'cancelled', 'run_id': run_id,
                    'status': record['status'], 'reason': 'already_' + record['status']}
    if not record["engine_job_id"]:
        if not _update(run_id, status="cancelled", reason="cancelled before it was queued",
                       ended_at=now_iso()):
            current = get(run_id) or {}
            return {'ok': current.get('status') == 'cancelled', 'run_id': run_id,
                    'status': current.get('status'), 'reason': 'already_' + str(current.get('status'))}
        return {"ok": True, "run_id": run_id, "status": "cancelled",
                "detail": "it had not reached the engine"}

    engine = _backend(record["engine_url"] or "",
                      client_id=record.get("client_id") or "")
    try:
        stopped = engine.cancel(record["engine_job_id"])
    except ComfyUIError as e:
        return {"ok": False, "run_id": run_id, "reason": e.reason, "detail": e.detail}
    if not stopped.get('ok'):
        # A finished render or an engine that refused the stop is not a
        # cancellation. Leave the durable row for normal collection/retry.
        return {**stopped, 'ok': False, 'run_id': run_id,
                'status': (get(run_id) or record)['status']}
    if not _update(run_id, status="cancelled", ended_at=now_iso(),
                   reason=f"cancelled while {stopped.get('was')}"):
        current = get(run_id) or {}
        return {'ok': current.get('status') == 'cancelled', 'run_id': run_id,
                'status': current.get('status'), 'reason': 'already_' + str(current.get('status'))}
    return {"ok": True, "run_id": run_id, "status": "cancelled", **stopped}
