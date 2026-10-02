"""farm_state.py - one document that says what is running right now.

`GET /api/farm/state` (routes/farm_routes.py) answers "what is Faustus doing
at this moment?" in a single call: the chat turns in flight, the sub-agents a
turn delegated to, the jobs dispatched from outside and their workers, a night
shift, the workflow runs and the scheduled tasks that are executing, and the
period budget (pace, GPU seconds, breaker, cooldowns). A watcher (Cassandra's
Hoard) polls it every few seconds, so the document is built at most once per
second per owner and carries an ETag: asking again while nothing changed is a
304 with no body.

The shape follows one idea: a sub-agent belongs to the run that started it.
Every item is `{id, kind, title, parent, state, started_at, last_event_at,
model, progress, link, children}`. `parent` is the id of another item in the
same document, or None. Top-level items carry their children nested, so a
reader never has to join anything.

Titles are names (a chat's name, a worker's name, a task's title), never the
words somebody typed: nothing in here reads a message body.

The sources are read lazily and each one is optional: a source that fails
leaves the others intact and is named in `errors`. Every source can be
replaced by a keyword of `build()`, which is how the tests exercise the merge
with fakes instead of a running server.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = 1
#: A build is shared for this long (per owner): a dozen watchers cost one build.
CACHE_TTL_S = 1.0
#: A run with no event for this long is `stalled`, never `running` by inertia.
STALE_AFTER_S = 120.0
#: At most this many rows of each source (the board is a glance, not a log).
MAX_PER_SOURCE = 40
TITLE_CHARS = 120

#: States an item can be in. `waiting` = parked on a person (approval, question,
#: an interactive turn it yields to); `paused` = a workflow held on purpose.
STATES = ("running", "queued", "waiting", "paused", "stalled", "verifying", "cancelling")

KINDS = ("chat_run", "worker", "dispatch_job", "night_shift", "workflow_run", "scheduled_task")


# -- small helpers ------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN is not a time


def _ts(value: Any) -> Optional[float]:
    v = _num(value)
    return round(v, 3) if v and v > 0 else None


def _epoch(dt: Any) -> Optional[float]:
    """A naive-UTC `datetime` (how the database stores them) as epoch seconds."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


def _iso_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _epoch(dt)


def _title(text: Any, fallback: str) -> str:
    out = " ".join(str(text or "").split())
    if not out:
        out = fallback
    return out if len(out) <= TITLE_CHARS else out[: TITLE_CHARS - 1] + "\u2026"


def _link(session_id: Optional[str]) -> Optional[str]:
    sid = str(session_id or "").strip()
    return f"/studio?s={sid}" if sid else None


def _item(id_: str, kind: str, title: str, state: str, *, parent: Optional[str] = None,
          started_at: Optional[float] = None, last_event_at: Optional[float] = None,
          model: Optional[str] = None, progress: Optional[Dict[str, Any]] = None,
          link: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": id_, "kind": kind, "title": title, "parent": parent, "state": state,
        "started_at": _ts(started_at), "last_event_at": _ts(last_event_at),
        "model": (str(model).strip() or None) if model else None,
        "progress": progress, "link": link,
    }
    for key, value in extra.items():
        if value is not None and value != "" and value != [] and value != {}:
            row[key] = value
    return row


def _progress_counts(done: int, total: int, unit: str) -> Optional[Dict[str, Any]]:
    if total <= 0:
        return None
    done = max(0, min(done, total))
    return {"done": done, "total": total, "unit": unit, "percent": round(100.0 * done / total, 1)}


def _visible(owner: str, row_owner: Optional[str]) -> bool:
    """Same rule as `auth_helpers.owner_filter`: no owner = single-user mode
    (everything), otherwise the owner's rows and the shared (ownerless) ones."""
    if not owner:
        return True
    return (row_owner or None) in (owner, None)


# -- the real sources (each lazy, each replaceable) ---------------------------

def _src_runs() -> Dict[str, Dict[str, Any]]:
    from src import agent_runs
    return agent_runs.activity_details()


def _src_pending_approvals(owner: str) -> Iterable[str]:
    from src.tool_approvals import tool_approval_store
    return tool_approval_store.pending_session_ids(owner=owner or None)


def _src_workers() -> Dict[str, Dict[str, Any]]:
    from src.agent_tools.subagent_tools import worker_board
    return worker_board()


def _src_jobs() -> List[Any]:
    from src import dispatch
    return [j for j in list(dispatch._jobs.values()) if j.status in dispatch._LIVE]


def _src_shifts(owner: str) -> List[Dict[str, Any]]:
    from src import night_shift
    live = set(night_shift._RUNNING)
    if not live:
        return []
    return [s for s in night_shift.list_for(owner or None, limit=20)
            if s.get("id") in live and s.get("state") in ("queued", "running")]


def _src_session_rows(session_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = sorted({str(s) for s in session_ids if str(s or "").strip()})
    if not ids:
        return {}
    from core.database import SessionLocal, Session as DbSession
    db = SessionLocal()
    try:
        rows = db.query(DbSession.id, DbSession.name, DbSession.model, DbSession.owner) \
                 .filter(DbSession.id.in_(ids)).all()
        return {r.id: {"name": r.name, "model": r.model, "owner": r.owner} for r in rows}
    finally:
        db.close()


def _src_workflows(owner: str) -> List[Dict[str, Any]]:
    """Workflow runs that are running or paused, with how many of their nodes
    have reached a terminal state."""
    from core.database import NodeRunRow, SessionLocal, WorkflowRunRow
    from src.auth_helpers import owner_filter
    db = SessionLocal()
    try:
        q = db.query(WorkflowRunRow).filter(WorkflowRunRow.status.in_(("running", "paused")),
                                            WorkflowRunRow.ended_at.is_(None))
        q = owner_filter(q, WorkflowRunRow, owner)
        runs = q.order_by(WorkflowRunRow.created_at.desc()).limit(MAX_PER_SOURCE).all()
        if not runs:
            return []
        nodes: Dict[str, List[Any]] = {}
        for n in db.query(NodeRunRow.workflow_run_id, NodeRunRow.node_id, NodeRunRow.status,
                          NodeRunRow.started_at, NodeRunRow.ended_at) \
                   .filter(NodeRunRow.workflow_run_id.in_([r.id for r in runs])).all():
            nodes.setdefault(n.workflow_run_id, []).append(n)
        out: List[Dict[str, Any]] = []
        terminal = {"completed", "failed", "skipped", "cancelled"}
        for r in runs:
            try:
                definition = json.loads(r.definition_json or "{}")
            except ValueError:
                definition = {}
            if not isinstance(definition, dict):
                definition = {}
            rows = nodes.get(r.id, [])
            seen = [t for n in rows for t in (_iso_epoch(n.started_at), _iso_epoch(n.ended_at)) if t]
            out.append({
                "id": r.id, "title": definition.get("title"), "status": r.status,
                "owner": r.owner, "trigger": r.trigger,
                "started_at": _iso_epoch(r.started_at) or _iso_epoch(r.created_at_iso),
                "last_event_at": max(seen) if seen else None,
                "done": len({n.node_id for n in rows if n.status in terminal}),
                "total": len(definition.get("nodes") or []),
            })
        return out
    finally:
        db.close()


def _src_tasks(owner: str) -> List[Dict[str, Any]]:
    """Scheduled tasks whose lease is held right now = a run in progress."""
    from core.database import ScheduledTask, SessionLocal
    from src.auth_helpers import owner_filter
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        q = db.query(ScheduledTask).filter(ScheduledTask.status == "active",
                                           ScheduledTask.lease_owner.isnot(None),
                                           ScheduledTask.lease_expires_at.isnot(None),
                                           ScheduledTask.lease_expires_at > now)
        q = owner_filter(q, ScheduledTask, owner)
        return [{"id": t.id, "name": t.name, "model": t.model, "session_id": t.session_id,
                 "task_type": t.task_type, "attempt": int(t.lease_attempt or 0),
                 "last_event_at": _epoch(t.lease_heartbeat_at)}
                for t in q.limit(MAX_PER_SOURCE).all()]
    finally:
        db.close()


def _src_budget() -> Dict[str, Any]:
    from src import period_budget
    return period_budget.state()

# -- the budget block ---------------------------------------------------------

def summarize_budget(state: Dict[str, Any]) -> Dict[str, Any]:
    """The period budget in the few numbers a glance needs. Pace fractions are
    rounded to a thousandth so the ETag moves when the picture moves, not every
    second the clock ticks."""
    def row(r: Dict[str, Any]) -> Dict[str, Any]:
        pace = r.get("pace") or {}
        target = _num(r.get("target"))
        used = _num(r.get("used")) or 0.0
        return {
            "provider": r.get("provider"), "metric": r.get("metric"), "window": r.get("window"),
            "used": used, "target": target,
            "used_fraction": round(used / target, 3) if target else None,
            "pace_fraction": round(_num(pace.get("allowed_fraction")) or 0.0, 3),
            "pace_allowed": _num(pace.get("allowed")),
            "paused": bool(r.get("paused")), "paused_until": _ts(r.get("paused_until")),
            "reason": r.get("reason") or None,
            "resets_at": _ts(r.get("window_resets_at")),
        }

    breaker = state.get("breaker") or {}
    last = breaker.get("last_failure") if isinstance(breaker.get("last_failure"), dict) else {}
    gpu = state.get("gpu")
    return {
        "enabled": bool(state.get("enabled")),
        "window": state.get("window"),
        "window_resets_at": _ts(state.get("window_resets_at")),
        "interactive_active": bool(state.get("interactive_active")),
        "providers": [row(r) for r in (state.get("providers") or []) if isinstance(r, dict)],
        "gpu": row(gpu) if isinstance(gpu, dict) else None,
        "breaker": {
            "enabled": bool(breaker.get("enabled")), "open": bool(breaker.get("open")),
            "consecutive_failures": int(breaker.get("consecutive_failures") or 0),
            "threshold": int(breaker.get("threshold") or 0),
            "reopens_at": _ts(breaker.get("pause_until")),
            "last_failure_kind": (last or {}).get("kind") or None,
        },
        "cooldowns": [
            {"endpoint": c.get("endpoint"), "status": c.get("status"),
             "until": _ts(c.get("until") or c.get("pause_until")), "hits": c.get("hits")}
            for c in (state.get("cooldowns") or []) if isinstance(c, dict)
        ],
    }


# -- the merge ----------------------------------------------------------------

def _run_state(sid: str, snap: Dict[str, Any], pending: set, now: float) -> str:
    if sid in pending:
        return "waiting"
    try:
        if int(snap.get("queued_position") or 0) > 0:
            return "queued"
    except (TypeError, ValueError):
        pass
    if snap.get("phase") == "awaiting_user" or snap.get("phase_canonical") == "awaiting_user":
        return "waiting"
    last = _num(snap.get("last_event_at"))
    if last and now - last > STALE_AFTER_S:
        return "stalled"
    if snap.get("phase_canonical") == "verifying":
        return "verifying"
    return "running"


def _job_progress(job: Any) -> Optional[Dict[str, Any]]:
    tasks = list((getattr(job, "args", None) or {}).get("tasks") or [])
    total = len(tasks)
    if not total:
        return None
    try:
        from src import dispatch
        latest = dispatch.compact(job).get("progress") or {}
    except Exception:  # noqa: BLE001 - a missing hint is not a failed build
        latest = {}
    done = sum(1 for p in latest.values() if isinstance(p, dict) and p.get("last_event") in ("done", "error"))
    return _progress_counts(done, total, "tasks")


def _job_last_event(job: Any) -> Optional[float]:
    stamps = [_num(ev.get("ts")) for ev in list(getattr(job, "events", None) or []) if isinstance(ev, dict)]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def _assemble(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flat items -> the forest: each item under its parent when that parent is
    in the document, otherwise a root. A parent cycle is cut at the item that
    closes it."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if it["id"] not in by_id:
            by_id[it["id"]] = it
    for it in by_id.values():
        it["children"] = []

    def cyclic(it: Dict[str, Any]) -> bool:
        seen = {it["id"]}
        cur = by_id.get(it.get("parent") or "")
        while cur is not None:
            if cur["id"] in seen:
                return True
            seen.add(cur["id"])
            cur = by_id.get(cur.get("parent") or "")
        return False

    roots: List[Dict[str, Any]] = []
    for it in by_id.values():
        parent = by_id.get(it.get("parent") or "")
        if parent is None or parent is it or cyclic(it):
            it["parent"] = None
            roots.append(it)
        else:
            parent["children"].append(it)

    def newest_first(it: Dict[str, Any]) -> Tuple[int, float]:
        return (0 if it["started_at"] else 1, -(it["started_at"] or 0.0))

    def oldest_first(it: Dict[str, Any]) -> Tuple[int, float]:
        return (0 if it["started_at"] else 1, it["started_at"] or 0.0)

    def sort_tree(nodes: List[Dict[str, Any]], key: Callable) -> None:
        nodes.sort(key=key)
        for n in nodes:
            sort_tree(n["children"], oldest_first)

    sort_tree(roots, newest_first)
    return roots


def _count(roots: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    by_state: Dict[str, int] = {}
    total = 0

    def walk(nodes: List[Dict[str, Any]]) -> None:
        nonlocal total
        for n in nodes:
            total += 1
            by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1
            by_state[n["state"]] = by_state.get(n["state"], 0) + 1
            walk(n["children"])

    walk(roots)
    return {"total": total, "roots": len(roots), "by_kind": by_kind, "by_state": by_state}


def _job_visible(job: Any, owner: str) -> bool:
    try:
        from src import dispatch
        return bool(dispatch.visible_to(job, owner or None))
    except Exception:  # noqa: BLE001
        return not owner or getattr(job, "owner", None) in (owner, None)

def build(owner: str = "", *, now: Optional[float] = None,
          runs: Optional[Dict[str, Dict[str, Any]]] = None,
          pending_approvals: Optional[Iterable[str]] = None,
          workers: Optional[Dict[str, Dict[str, Any]]] = None,
          jobs: Optional[List[Any]] = None,
          shifts: Optional[List[Dict[str, Any]]] = None,
          workflows: Optional[List[Dict[str, Any]]] = None,
          tasks: Optional[List[Dict[str, Any]]] = None,
          session_rows: Optional[Callable[[Iterable[str]], Dict[str, Dict[str, Any]]]] = None,
          budget: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole document for `owner` ("" = single-user mode: everything)."""
    owner = owner or ""
    t = time.time() if now is None else now
    errors: Dict[str, str] = {}

    def read(name: str, supplied: Any, fetch: Callable[[], Any], empty: Any) -> Any:
        if supplied is not None:
            return supplied
        try:
            return fetch()
        except Exception as exc:  # noqa: BLE001 - one source down never takes the board down
            logger.debug("[farm] source %s failed", name, exc_info=True)
            errors[name] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return empty

    run_map = read("runs", runs, _src_runs, {})
    pending = set(read("approvals", pending_approvals, lambda: _src_pending_approvals(owner), []))
    worker_map = read("workers", workers, _src_workers, {})
    job_list = read("dispatch", jobs, _src_jobs, [])
    shift_list = read("night_shift", shifts, lambda: _src_shifts(owner), [])
    workflow_list = read("workflows", workflows, lambda: _src_workflows(owner), [])
    task_list = read("tasks", tasks, lambda: _src_tasks(owner), [])

    # One query for every session name/model/owner we are about to need.
    wanted: List[str] = list(run_map) + list(worker_map) \
        + [str((c or {}).get("parent") or "") for c in worker_map.values()] \
        + [str(getattr(j, "session_id", "") or "") for j in job_list]
    rows = read("sessions", None, lambda: (session_rows or _src_session_rows)(wanted), {})

    def may_see(*session_ids: str) -> bool:
        """Whether the first session we know a row for belongs to `owner`."""
        if not owner:
            return True
        for sid in session_ids:
            r = rows.get(sid)
            if r is not None:
                return _visible(owner, r.get("owner"))
        return False

    items: List[Dict[str, Any]] = []

    # A job IS the session its workers hang from.
    job_by_session: Dict[str, str] = {}
    visible_jobs = [j for j in job_list if _job_visible(j, owner)][:MAX_PER_SOURCE]
    for j in visible_jobs:
        if getattr(j, "session_id", None):
            job_by_session[str(j.session_id)] = f"job:{j.id}"

    live_shifts = [s for s in shift_list if isinstance(s, dict)][:MAX_PER_SOURCE]
    shifts_of: Dict[str, List[str]] = {}
    shift_started: Dict[str, float] = {}
    for s in live_shifts:
        sid_ = f"shift:{s.get('id')}"
        shifts_of.setdefault(str(s.get("owner") or "_"), []).append(sid_)
        shift_started[sid_] = _num(s.get("started")) or _num(s.get("created")) or 0.0

    for j in visible_jobs:
        parent = None
        # A night shift runs its tasks as unattended jobs. They are not linked
        # by id, so they are attached only when it cannot be anyone else's:
        # one live shift for that owner, started before the job.
        mine = shifts_of.get(str(getattr(j, "owner", None) or "_")) or []
        if getattr(j, "unattended", False) and len(mine) == 1:
            if (_num(getattr(j, "created", None)) or 0.0) >= shift_started.get(mine[0], 0.0) - 5.0:
                parent = mine[0]
        status = str(getattr(j, "status", "running"))
        items.append(_item(
            f"job:{j.id}", "dispatch_job", _title(getattr(j, "title", ""), "Dispatched job"),
            status if status in STATES else "running", parent=parent,
            started_at=getattr(j, "started", None) or getattr(j, "created", None),
            last_event_at=_job_last_event(j) or getattr(j, "started", None),
            model=getattr(j, "model", None), progress=_job_progress(j),
            link=_link(getattr(j, "session_id", None)),
            unattended=True if getattr(j, "unattended", False) else None))

    for s in live_shifts:
        total = len(s.get("tasks") or [])
        waiting = s.get("waiting_for")
        items.append(_item(
            f"shift:{s.get('id')}", "night_shift", f"Night shift ({total} tasks)",
            "waiting" if waiting else ("running" if s.get("state") == "running" else "queued"),
            started_at=s.get("started") or s.get("created"), last_event_at=s.get("started") or s.get("created"),
            model=s.get("model"), progress=_progress_counts(len(s.get("results") or []), total, "tasks"),
            waiting_for=str(waiting) if waiting else None))

    for sid, snap in list(run_map.items())[:MAX_PER_SOURCE]:
        if sid in job_by_session or not isinstance(snap, dict) or not may_see(sid):
            continue
        r = rows.get(sid) or {}
        pct = _num(snap.get("percent"))
        items.append(_item(
            f"run:{sid}", "chat_run", _title(r.get("name"), "Untitled chat"),
            _run_state(sid, snap, pending, t),
            started_at=snap.get("started_at"), last_event_at=snap.get("last_event_at"),
            model=r.get("model"), progress={"percent": pct, "unit": "plan"} if pct is not None else None,
            link=_link(sid), phase=snap.get("phase_canonical") or snap.get("phase"),
            tool=snap.get("tool"), round=snap.get("round") or None,
            queue_position=int(snap.get("queued_position") or 0) or None))

    run_ids = {it["id"] for it in items}
    for sid, card in list(worker_map.items())[: MAX_PER_SOURCE * 2]:
        if not isinstance(card, dict):
            continue
        parent_sid = str(card.get("parent") or "")
        if not may_see(sid, parent_sid):
            continue
        parent_id = job_by_session.get(parent_sid) \
            or (f"run:{parent_sid}" if f"run:{parent_sid}" in run_ids else None) \
            or (f"worker:{parent_sid}" if parent_sid in worker_map else None)
        r = rows.get(sid) or {}
        items.append(_item(
            f"worker:{sid}", "worker", _title(card.get("name") or r.get("name"), "Worker"),
            "stalled" if card.get("stalled") else "running", parent=parent_id,
            started_at=card.get("started_at"), last_event_at=card.get("last_event_at"),
            model=r.get("model"), link=_link(sid), role=card.get("role") or None,
            round=card.get("round") or None, tool_calls=card.get("tool_calls") or None))
    for w in workflow_list[:MAX_PER_SOURCE]:
        if not isinstance(w, dict) or not _visible(owner, w.get("owner")):
            continue
        items.append(_item(
            f"workflow:{w.get('id')}", "workflow_run", _title(w.get("title"), "Workflow run"),
            "paused" if w.get("status") == "paused" else "running",
            started_at=w.get("started_at"), last_event_at=w.get("last_event_at") or w.get("started_at"),
            progress=_progress_counts(int(w.get("done") or 0), int(w.get("total") or 0), "steps"),
            trigger=w.get("trigger") or None))

    for task in task_list[:MAX_PER_SOURCE]:
        if not isinstance(task, dict):
            continue
        attempt = int(task.get("attempt") or 0)
        items.append(_item(
            f"task:{task.get('id')}", "scheduled_task", _title(task.get("name"), "Scheduled task"), "running",
            last_event_at=task.get("last_event_at"), model=task.get("model"),
            link=_link(task.get("session_id")), task_type=task.get("task_type") or None,
            attempt=attempt if attempt > 1 else None))

    roots = _assemble(items)
    budget_state = read("budget", budget, _src_budget, {})
    body: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": round(t, 3),
        "counts": _count(roots),
        "items": roots,
        "budget": summarize_budget(budget_state) if budget_state else None,
    }
    if errors:
        body["errors"] = errors
    return body


# -- cache + ETag -------------------------------------------------------------

def etag_of(body: Dict[str, Any]) -> str:
    """A weak validator over everything except the clock: two builds one second
    apart that describe the same picture have the same ETag."""
    stable = {k: v for k, v in body.items() if k != "generated_at"}
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return 'W/"farm-' + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] + '"'


def etag_matches(header: Optional[str], etag: str) -> bool:
    """`If-None-Match` handling: `*`, a list, weak or strong forms."""
    if not header:
        return False
    want = etag[2:] if etag.startswith("W/") else etag
    for part in header.split(","):
        part = part.strip()
        if part == "*":
            return True
        if (part[2:] if part.startswith("W/") else part) == want:
            return True
    return False


_cache: Dict[str, Tuple[float, Dict[str, Any], str]] = {}
_lock = threading.Lock()


def cached(owner: str = "", *, ttl: float = CACHE_TTL_S) -> Tuple[Dict[str, Any], str]:
    """`(body, etag)` rebuilt at most once per `ttl` per owner. Concurrent
    callers wait for the one build in flight and share it."""
    key = owner or ""
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1], hit[2]
        body = build(owner)
        tag = etag_of(body)
        _cache[key] = (time.monotonic(), body, tag)
        if len(_cache) > 64:
            for stale in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
                _cache.pop(stale, None)
        return body, tag


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()