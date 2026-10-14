"""External inputs become ordinary durable tasks; delivery never reruns work."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import quote

from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError

from core.database import ExternalEvent, ExternalEventJob, ScheduledTask, SessionLocal, TaskRun, utcnow_naive


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def decode_input(body):
    """Accept normal envelopes and Gmail's PubSub notification envelope."""
    if not isinstance(body, dict):
        raise ValueError("Event envelope must be an object")
    source = str(body.get("source") or "webhook")
    payload = body.get("payload", {})
    event_key = str(body.get("event_id") or "")
    event_type = str(body.get("event_type") or "external_event")
    if source == "gmail_pubsub":
        message = body.get("message") or {}
        if not isinstance(message, dict):
            raise ValueError("Gmail message envelope must be an object")
        payload = json.loads(base64.b64decode(message.get("data") or "", validate=True))
        if not isinstance(payload, dict):
            raise ValueError("Gmail notification data must be an object")
        event_key = str(payload.get("emailAddress") or "") + ":" + str(payload.get("historyId") or "")
        if not payload.get("emailAddress") or not payload.get("historyId"):
            raise ValueError("Gmail notification needs emailAddress and historyId")
        event_type = "email_received"
    if not event_key or not source or not isinstance(payload, dict):
        raise ValueError("source, event_id and an object payload are required")
    return source, event_type, event_key, payload


def ingest(*, owner, source, event_type, event_key, payload, task_id=None, delivery=None, dispatch=True):
    """Receipt + per-event one-shot jobs in one transaction, including thresholds."""
    if not event_key or not source or not event_type or not isinstance(payload, dict):
        raise ValueError("A stable event key and an object payload are required")
    if delivery is not None and not isinstance(delivery, dict):
        raise ValueError("Delivery must be an object")
    if delivery and not (delivery.get("event_type") or (delivery.get("app") and delivery.get("tool"))):
        raise ValueError("Delivery needs a Hub event_type or app and tool")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    eid = _key(owner, source, event_type, event_key)
    db = SessionLocal()
    try:
        # Serialize receipt insertion and threshold updates on the local database.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        existing = db.get(ExternalEvent, eid)
        if existing:
            if existing.payload != encoded:
                raise ValueError("This event key already identifies a different payload")
            return {"event_id": eid, "duplicate": True, "jobs": _jobs(db, eid)}
        query = db.query(ScheduledTask).filter(ScheduledTask.owner == owner, ScheduledTask.status == "active")
        if task_id:
            tasks = query.filter(ScheduledTask.id == task_id).all()
            if not tasks:
                raise ValueError("Active task not found for this owner")
        else:
            tasks = query.filter(ScheduledTask.trigger_type == "event", ScheduledTask.trigger_event == event_type).with_for_update().all()
        db.add(ExternalEvent(id=eid, owner=owner, source=source, event_type=event_type, event_key=str(event_key), payload=encoded))
        now = utcnow_naive()
        for parent in tasks if dispatch else []:
            if not task_id:
                parent.trigger_counter = (parent.trigger_counter or 0) + 1
                if parent.trigger_counter < (parent.trigger_count or 1):
                    continue
                parent.trigger_counter = 0
            jid = "event-" + _key(eid, parent.id)
            from src.task_scheduler import get_task_policy, set_task_policy
            set_task_policy(jid, **get_task_policy(parent.id))
            copied = {k: getattr(parent, k) for k in (
                "owner", "name", "prompt", "task_type", "action", "output_target", "session_id",
                "model", "endpoint_url", "then_task_id", "crew_member_id", "character_id",
                "max_steps", "email_results", "notifications_enabled")}
            # Keep action script/config JSON byte-for-byte. LLM/research gets the event.
            if (parent.task_type or "llm") != "action":
                copied["prompt"] = (parent.prompt or "") + "\n\nIncoming event (source data):\n" + json.dumps(
                    {"source": source, "type": event_type, "event_id": event_key, "payload": payload}, ensure_ascii=False)
            db.add(ScheduledTask(id=jid, **copied, trigger_type="schedule", schedule="once", scheduled_date=now,
                                 next_run=now, timezone="UTC", status="active", run_count=0))
            db.add(ExternalEventJob(id=jid, event_id=eid, parent_task_id=parent.id, task_id=jid,
                                   delivery=json.dumps(delivery) if delivery else None,
                                   delivery_status="pending" if delivery else "none"))
        db.commit()
        return {"event_id": eid, "duplicate": False, "jobs": _jobs(db, eid)}
    except IntegrityError:
        db.rollback()
        # A simultaneous non-SQLite receipt insertion already committed the jobs.
        existing = db.get(ExternalEvent, eid)
        if existing and existing.payload == encoded:
            return {"event_id": eid, "duplicate": True, "jobs": _jobs(db, eid)}
        raise
    finally:
        db.close()


def _jobs(db, event_id):
    return [_view(db, j) for j in db.query(ExternalEventJob).filter(ExternalEventJob.event_id == event_id).all()]


def related_runs(db, task_id):
    children = db.query(ExternalEventJob.task_id).filter(ExternalEventJob.parent_task_id == task_id)
    return db.query(TaskRun).filter(or_(TaskRun.task_id == task_id, TaskRun.task_id.in_(children)))


def _view(db, job):
    task = db.get(ScheduledTask, job.task_id)
    run = db.query(TaskRun).filter(TaskRun.task_id == job.task_id).order_by(TaskRun.started_at.desc()).first()
    return {"id": job.id, "event_id": job.event_id, "parent_task_id": job.parent_task_id, "task_id": job.task_id,
            "session_id": task.session_id if task else None, "execution": run.status if run else "pending",
            "run_id": run.id if run else None, "model": run.model if run else None, "result": run.result if run else None,
            "execution_error": run.error if run else None, "delivery": job.delivery_status, "delivery_error": job.delivery_error}


def list_events(owner, task_id=None):
    db = SessionLocal()
    try:
        query = db.query(ExternalEvent).filter(ExternalEvent.owner == owner).order_by(ExternalEvent.created_at.desc())
        if task_id:
            query = query.join(ExternalEventJob, ExternalEventJob.event_id == ExternalEvent.id).filter(ExternalEventJob.parent_task_id == task_id)
        return [{"id": e.id, "source": e.source, "event_type": e.event_type, "event_key": e.event_key,
                 "payload": json.loads(e.payload), "jobs": _jobs(db, e.id)} for e in query.limit(100).all()]
    finally:
        db.close()


def retry_delivery(job_id, owner):
    db = SessionLocal()
    try:
        row = db.query(ExternalEventJob).join(ExternalEvent, ExternalEventJob.event_id == ExternalEvent.id).filter(
            ExternalEventJob.id == job_id, ExternalEvent.owner == owner).first()
        if not row or not row.delivery:
            raise ValueError("Event delivery not found")
        # A retry never supersedes an active sender. Conditional update also
        # closes the race between reading failed and another sender claiming it.
        db.query(ExternalEventJob).filter(ExternalEventJob.id == job_id,
            ExternalEventJob.delivery_status.in_(["failed", "unknown"])).update(
                {"delivery_status": "pending", "delivery_error": None}, synchronize_session=False)
        db.commit()
        db.refresh(row)
        return _view(db, row)
    finally:
        db.close()


def _deliver(job_id):
    from src.hoard_hub import _call
    db = SessionLocal()
    try:
        job = db.get(ExternalEventJob, job_id)
        run = db.query(TaskRun).filter(TaskRun.task_id == job.task_id, TaskRun.status == "success").order_by(TaskRun.started_at.desc()).first()
        if not run:
            return
        claimed = db.query(ExternalEventJob).filter(ExternalEventJob.id == job_id, ExternalEventJob.delivery_status == "pending").update(
            {"delivery_status": "sending", "updated_at": utcnow_naive()}, synchronize_session=False)
        db.commit()
        if not claimed:
            return
        spec = json.loads(job.delivery)
        from core.constants import internal_api_base
        context = {"event_id": job.event_id, "job_id": job.id, "task_id": job.task_id, "run_id": run.id,
                   "result_url": internal_api_base() + "/api/tasks/events?task_id=" + quote(job.parent_task_id, safe="")}
        if spec.get("event_type"):
            path, body = "/api/events", {"type": spec["event_type"], "source": "faustus", "data": context}
        else:
            args = dict(spec.get("arguments") or {})
            args[spec.get("result_field") or "result"] = run.result or ""
            args[spec.get("key_field") or "event_id"] = job.id
            path, body = "/api/apps/" + quote(spec["app"], safe="") + "/call", {"tool": spec["tool"], "arguments": args}
        status, answer, _ = _call("POST", path, body, timeout=30)
        if status is None or (200 <= status < 300 and not isinstance(answer, dict)):
            outcome, error = "unknown", "Delivery response unavailable; work was not repeated"
        elif 200 <= status < 300 and isinstance(answer, dict) and answer.get("ok") is not False:
            outcome, error = "delivered", None
        else:
            outcome, error = "failed", f"Hub delivery returned HTTP{status}"
        db.query(ExternalEventJob).filter(ExternalEventJob.id == job_id, ExternalEventJob.delivery_status == "sending").update(
            {"delivery_status": outcome, "delivery_error": error, "updated_at": utcnow_naive()}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


async def deliver_pending():
    """One scheduler tick, independent of native execution and retained results."""
    db = SessionLocal()
    try:
        # Interrupted sends are uncertain; never replay them on reconnect.
        from datetime import timedelta
        db.query(ExternalEventJob).filter(ExternalEventJob.delivery_status == "sending",
            ExternalEventJob.updated_at < utcnow_naive() - timedelta(minutes=2)).update(
                {"delivery_status": "unknown", "delivery_error": "Delivery interrupted; result remains available"})
        db.commit()
        ids = [j.id for j in db.query(ExternalEventJob).filter(ExternalEventJob.delivery_status == "pending").all()]
    finally:
        db.close()
    for jid in ids:
        await asyncio.to_thread(_deliver, jid)
