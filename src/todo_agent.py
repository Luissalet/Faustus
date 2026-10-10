"""src/todo_agent.py -- hand a todo (a note of type todo/checklist) to an agent.

``assign(...)`` creates a fresh agent conversation whose first message is the
todo (title, body, pending items, due date, links), starts the turn in the
background and links conversation and todo both ways: the note stores
``agent_session_id`` and an ``agent_run`` record the Studio shows as a status
chip ("en curso por agente"), and the session name carries the todo title.

Rules that do not bend:

* **Autonomy is never widened.** The baseline is the project's saved
  ``autonomy_preset`` (``supervised`` when there is none). A request can ask
  for the same or a stricter posture, never a looser one: the effective preset
  is the stricter of the two. ``read_only < supervised < bounded_autonomous``.
* The text goes in as untrusted (``chat_bridges.turn.run_turn``), so a
  state-changing tool still stops at an exact-approval card; an unattended
  run can never approve itself.
* A todo is never reported ``done`` without a completed run: ``done`` needs an
  answer, no error, no pending approval and no timeout. A run that stopped on
  an approval or produced nothing is ``needs_review``; a crash is ``failed``.
* Idempotent: assigning again while the run is live returns that run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Strictest first. The effective preset is the lower rank of the two.
AUTONOMY_RANK = {"read_only": 0, "supervised": 1, "bounded_autonomous": 2}
DEFAULT_AUTONOMY = "supervised"
SUMMARY_LIMIT = 1800
RUNNING, DONE, NEEDS_REVIEW, FAILED = "running", "done", "needs_review", "failed"
_URL = re.compile(r"https?://[^\s<>\")\]]+")

#: note id -> live asyncio task. A record that says "running" with no task
#: here belongs to a process that died; it is repaired, not trusted.
_LIVE: Dict[str, "asyncio.Task[Any]"] = {}


class AssignError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def clamp_autonomy(requested: Optional[str], baseline: Optional[str]) -> Dict[str, Any]:
    """The preset a run gets: never looser than the baseline."""
    base = baseline if baseline in AUTONOMY_RANK else DEFAULT_AUTONOMY
    asked = (requested or "").strip().lower()
    if asked and asked not in AUTONOMY_RANK:
        raise AssignError(400, f"Unknown autonomy preset: {requested}")
    wanted = asked or base
    effective = wanted if AUTONOMY_RANK[wanted] <= AUTONOMY_RANK[base] else base
    return {"requested": asked or None, "baseline": base, "effective": effective,
            "clamped": bool(asked) and effective != asked}


def build_prompt(note: Dict[str, Any]) -> str:
    title = (note.get("title") or "").strip()
    body = (note.get("content") or "").strip()
    items = note.get("items") if isinstance(note.get("items"), list) else []
    pending = [str(i.get("text") or "").strip() for i in items
               if isinstance(i, dict) and not i.get("done") and str(i.get("text") or "").strip()]
    lines = ["Complete this todo from the user's list and report what you did.", ""]
    lines.append(f"Todo: {title or '(untitled)'}")
    if body:
        lines += ["", "Notes:", body]
    if pending:
        lines += ["", "Pending items:"] + [f"- {p}" for p in pending]
    if note.get("due_date"):
        lines += ["", f"Due: {note['due_date']}"]
    links = []
    for chunk in [title, body, *pending]:
        for url in _URL.findall(chunk):
            if url not in links:
                links.append(url)
    if links:
        lines += ["", "Links:"] + [f"- {u}" for u in links[:12]]
    lines += ["", "Finish with a short summary: what was done, what was not, and anything "
                  "that needs the user's decision."]
    return "\n".join(lines)


def classify(result: Any, cancelled: bool = False) -> str:
    """done only for a completed run; everything doubtful is needs_review."""
    if cancelled:
        return NEEDS_REVIEW
    error = getattr(result, "error", "") or ""
    approvals = getattr(result, "approvals", None) or []
    stop = getattr(result, "stop_reason", "") or ""
    text = (getattr(result, "text", "") or "").strip()
    if error and not text:
        return FAILED
    if error or approvals or stop in ("approval", "timeout", "cancelled") or not text:
        return NEEDS_REVIEW
    return DONE


def parse_record(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _save(note_id: str, record: Dict[str, Any]) -> None:
    from core.database import Note, SessionLocal
    db = SessionLocal()
    try:
        note = db.query(Note).filter(Note.id == note_id).first()
        if note is not None:
            note.agent_run = json.dumps(record, ensure_ascii=False)
            note.agent_session_id = record.get("session_id") or note.agent_session_id
            db.commit()
    finally:
        db.close()


def current(note_id: str, raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stored run record, with a dead "running" repaired to failed."""
    record = parse_record(raw)
    if record and record.get("status") == RUNNING and note_id not in _LIVE:
        record = {**record, "status": FAILED, "finished_at": time.time(),
                  "error": "The run was interrupted (Faustus restarted before it finished)."}
        _save(note_id, record)
    return record


def _project_baseline(owner: Optional[str], project_id: str) -> Dict[str, Any]:
    if not project_id:
        return {"preset": None, "workspace": "", "folder": None, "id": None}
    from services.projects import get_store
    project = get_store().get(project_id, owner)
    if not project:
        raise AssignError(404, "Project not found")
    if not project.get("enabled", True):
        raise AssignError(400, "Project is disabled")
    defaults = project.get("execution_defaults") or {}
    return {"preset": defaults.get("autonomy_preset"), "workspace": project.get("workspace") or "",
            "folder": project.get("folder"), "id": project["id"]}


def _create_session(owner: Optional[str], title: str, model: str, project: Dict[str, Any]) -> str:
    from src.chat_bridges.turn import _default_route, _manager, TurnUnavailable
    try:
        url, chosen, headers = _default_route(owner or "", model)
        sm = _manager()
    except TurnUnavailable as exc:
        raise AssignError(503, str(exc))
    sid = str(uuid.uuid4())
    session = sm.create_session(session_id=sid, name=(f"Todo: {title}" if title else "Todo")[:120],
                                endpoint_url=url, model=chosen, rag=False, owner=owner or None,
                                project_id=project.get("id"), folder=project.get("folder"))
    if headers:
        try:
            session.headers = dict(headers)
            from routes.session_routes import _persist_session_headers
            _persist_session_headers(sid, session.headers)
        except Exception:  # noqa: BLE001 - re-resolved per turn
            logger.debug("todo agent: could not persist endpoint headers", exc_info=True)
    return sid


async def _run_turn(request):  # indirection so tests never reach a model
    from src.chat_bridges.turn import run_turn
    return await run_turn(request)


async def _drive(note_id: str, record: Dict[str, Any], turn_request) -> None:
    final = dict(record)
    try:
        result = await _run_turn(turn_request)
        final["status"] = classify(result)
        text = (getattr(result, "text", "") or "").strip()
        final["summary"] = text[:SUMMARY_LIMIT]
        final["error"] = getattr(result, "error", "") or ""
        final["approvals_pending"] = len(getattr(result, "approvals", None) or [])
        final["tool_calls"] = getattr(result, "tool_calls", 0)
    except asyncio.CancelledError:
        final.update(status=NEEDS_REVIEW, error="The run was cancelled.")
        raise
    except Exception as exc:  # noqa: BLE001 - a failed run is a status
        logger.warning("todo agent run failed: %s", exc)
        final.update(status=FAILED, error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        final["finished_at"] = time.time()
        if final.get("status") == RUNNING:
            final["status"] = FAILED
        try:
            _save(note_id, final)
        except Exception:  # noqa: BLE001
            logger.warning("todo agent: could not save the result", exc_info=True)
        _LIVE.pop(note_id, None)


def assign(note: Dict[str, Any], *, owner: Optional[str], mode: str = "agent",
           project_id: str = "", autonomy_preset: Optional[str] = None,
           model: str = "") -> Dict[str, Any]:
    """Start (or return) the agent run for a todo. Must run inside the loop."""
    if (mode or "agent") != "agent":
        raise AssignError(400, "Only mode 'agent' is supported")
    note_id = note["id"]
    existing = current(note_id, note.get("agent_run_raw"))
    if existing and existing.get("status") == RUNNING and note_id in _LIVE:
        return {**existing, "already_running": True}
    if not ((note.get("title") or "").strip() or (note.get("content") or "").strip()
            or note.get("items")):
        raise AssignError(400, "The todo is empty")
    project = _project_baseline(owner, (project_id or "").strip())
    autonomy = clamp_autonomy(autonomy_preset, project["preset"])
    sid = _create_session(owner, (note.get("title") or "").strip(), model, project)
    from src.chat_bridges.turn import TurnRequest
    turn_request = TurnRequest(owner=owner or "", session_id=sid, text=build_prompt(note),
                               mode="agent", model=model or "", source="todo",
                               autonomy_preset=autonomy["effective"], workspace=project["workspace"])
    record = {"run_id": uuid.uuid4().hex[:12], "status": RUNNING, "session_id": sid,
              "started_at": time.time(), "finished_at": None, "summary": "", "error": "",
              "project_id": project["id"], "autonomy_preset": autonomy["effective"],
              "autonomy_requested": autonomy["requested"], "autonomy_clamped": autonomy["clamped"]}
    _save(note_id, record)
    _LIVE[note_id] = asyncio.get_running_loop().create_task(_drive(note_id, record, turn_request))
    return {**record, "already_running": False}
