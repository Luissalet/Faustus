"""attention.py — ADP-11: what actually needs YOU, out of everything running.

`studio/src/screens/Activity.tsx` already has a "Needs action" tray, but it
is flat: every run sitting on `status === 'waiting'` is equally urgent, a
run with no events in ten minutes reads as "still working" because nothing
ever re-checked it, and a run that finished five minutes ago simply vanishes
from the list (Activity's `conversationRuns` only shows running/awaiting/
queued sessions — see `studio/src/adapters/activity.ts`). This module is the
one place that turns the REAL state of a run (`src/agent_runs.py`'s own
status/queue/last-event clock, `src/tool_approvals.py`'s mid-turn approval
gate, `src/question_store.py`'s open `ask_user` questions, and
`src/agent_tools/subagent_tools.py`'s delegate-worker board) into one of
eight honest buckets, ranked, with a reason a person can read.

Two independent pieces live here, on purpose:

* `classify(RunInfo) -> Attention` is PURE — no I/O, nothing it reads is not
  already on the `RunInfo` it was handed. This is what makes "a run with no
  recent events is `disconnected`, never `working` by inertia" a fact you
  can pin down in a test with a fake, not something that only shows up by
  running the whole server.
* `attention_for_owner()` is the wiring: it goes and gets the REAL data (or,
  for tests, accepts every source as an injectable keyword so the whole
  pipeline can be exercised with fakes too) and calls `classify()` once per
  candidate session.

Read marks (`DATA_DIR/attention_reads.json`, `{owner: {session_id: ts}}`)
are the second half of ADP-11: a per-user "I've seen this" timestamp. It
never changes what `classify()` says about a run — an unread approval is
still exactly as pending as a read one; reading it only silences the
unread badge and, for the one kind that has nothing left to resolve
(`finished_unreviewed`), drops it off the list.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.atomic_io import atomic_write_json
from src import constants as _constants

# ---------------------------------------------------------------------------
# Priority — fixed by the ADP-11 ficha:
#   approval > question > finished_unreviewed > disconnected > queued_model
#   > dependency > working > none
# Lower number sorts first (more urgent). `working`/`none` never need a
# person, so `attention_for_owner()` filters them out of what it returns —
# but `classify()` still produces them: a caller checking "did this
# regress to working-by-inertia" needs the negative case to be a real,
# checkable value, not silently dropped inside the classifier.
# ---------------------------------------------------------------------------
_PRIORITY: Dict[str, int] = {
    "approval": 0,
    "question": 1,
    "finished_unreviewed": 2,
    "disconnected": 3,
    "queued_model": 4,
    "dependency": 5,
    "working": 6,
    "none": 7,
}

#: A `running` turn (`src/agent_runs.py::_Run.status`) counts as
#: `disconnected` once its OWN sign-of-life clock (`last_event_at`, bumped by
#: every SSE event the run publishes — `_observe_activity`, including the
#: periodic `queue_status` ticks a queued run gets while it waits for its
#: lane) has gone this many seconds without moving. There is no "how long is
#: normal for a local model" constant anywhere else in the codebase to
#: inherit — this exists purely to stop silence from being read as
#: progress, so it is set to something a genuinely healthy connection never
#: gets close to (the chat SSE layer's own heartbeat interval is far
#: shorter than this) rather than a tuned guess at model latency. A run that
#: crosses it is not declared dead — `stop()`/`request_pause()` are not
#: called — it is only no longer shown as quietly `working`.
STALE_AFTER_S = 120.0

#: How far back a finished chat turn (`core.database.Session.last_message_at`
#: — the SAME column the sidebar's own "recent" sort already reads, not a
#: new clock) is still worth surfacing as `finished_unreviewed`. Past this,
#: an unopened old conversation is not "attention", it is just history —
#: the sidebar's own recency list is where that lives.
FINISHED_LOOKBACK_S = 24 * 3600.0

#: Kinds `attention_for_owner()` actually returns — the ones a person can DO
#: something about. `working`/`none` are real, classifiable states but never
#: "need action": a run quietly making progress is what Activity's "In
#: progress" tab is for.
ACTIONABLE_KINDS = frozenset(
    {"approval", "question", "finished_unreviewed", "disconnected", "queued_model", "dependency"}
)

_REASONS: Dict[str, str] = {
    # English source strings — same convention as every other user-facing
    # string in this codebase (studio/src/i18n's `t()`): written in English
    # at the "call site" (here, the one place that produces them), Spanish
    # supplied by docs/ui/i18n/es.tsv. Deliberately carry NO baked-in numbers
    # (age, queue position) so the same literal string is always the same
    # translation-table key; the client formats `since`/`detail` itself
    # (e.g. via `relativeTime`) exactly the way the rest of Activity already
    # does for every other row.
    "approval": "Waiting for your approval",
    "question": "Waiting for your answer",
    "finished_unreviewed": "Finished — not reviewed yet",
    "disconnected": "No recent events — is it disconnected?",
    "queued_model": "Queued for its turn",
    "dependency": "Waiting on another run to finish",
    "working": "Working",
    "none": "",
}


@dataclass(frozen=True)
class RunInfo:
    """The subset of one run's REAL state `classify()` needs — every field is
    either a fact `src/agent_runs.py` (+ the approval/question/worker
    modules) already tracks, or an explicit `None`/`0`/`False` for "not
    observed". Never fabricated: a caller that does not know a field leaves
    it at its default rather than guessing.
    """

    session_id: str
    #: `src/agent_runs.py::_Run.status` verbatim ("running") when there is a
    #: live detached run; "done"/"error"/"stopped" for one still in the
    #: module's short post-completion grace window; "" when agent_runs has
    #: no run on record at all (e.g. it was evicted, or never ran through a
    #: detached run — see `finished_unreviewed` below for that case).
    status: str = ""
    has_pending_approval: bool = False
    has_pending_question: bool = False
    #: >0 while the run is admitted to `_RUNS` but still waiting for its
    #: lane (`agent_runs.queued_positions()`); 0 once it is admitted or was
    #: never queued.
    queued_position: int = 0
    #: A short label for whatever this run is waiting ON (a delegate worker
    #: name, today) — never a reason of its own; `classify()` only uses
    #: "is this set", the label rides along in `Attention.detail`.
    dependency: Optional[str] = None
    #: Epoch seconds of the run's last observed SSE event
    #: (`agent_runs`'s own `last_event_at`) — the actual sign-of-life clock.
    last_event_at: Optional[float] = None
    started_at: Optional[float] = None
    #: Set only for the `finished_unreviewed` path, where there is no live
    #: `_Run` left to ask — see `_finished_rows_from_db`.
    finished_at: Optional[float] = None
    label: str = ""


@dataclass(frozen=True)
class Attention:
    kind: str
    reason: str
    priority: int
    since: Optional[float]
    #: Extra context a client MAY show next to `reason` — a queue position, a
    #: dependency's label — never required to make sense of `reason` on its
    #: own (a client that ignores this still shows a true, complete state).
    detail: str = ""


def classify(run: RunInfo, *, now: Optional[float] = None) -> Attention:
    """The ONE honest classification of `run`, in the fixed priority order
    the ADP-11 ficha specifies. Pure: no clock but `now` (defaults to
    `time.time()`), no I/O, no import of `agent_runs` or anything else —
    every fact it looks at is already on `run`.
    """
    now = time.time() if now is None else now

    if run.has_pending_approval:
        since = run.last_event_at if run.last_event_at is not None else run.started_at
        return _attn("approval", since)

    if run.has_pending_question:
        since = run.last_event_at if run.last_event_at is not None else run.started_at
        return _attn("question", since)

    if run.status in ("done", "error", "stopped"):
        since = run.finished_at if run.finished_at is not None else run.last_event_at
        return _attn("finished_unreviewed", since)

    if run.status == "running":
        age = None if run.last_event_at is None else max(0.0, now - run.last_event_at)
        # STALE CHECK COMES BEFORE "queued": a queue position that stopped
        # ticking is itself the disconnection — see STALE_AFTER_S's docstring.
        if age is not None and age >= STALE_AFTER_S:
            mins = round(age / 60.0)
            return _attn("disconnected", run.last_event_at, detail=f"{mins} min")
        if run.queued_position > 0:
            return _attn("queued_model", run.started_at, detail=f"#{run.queued_position}")
        if run.dependency:
            return _attn("dependency", run.last_event_at, detail=run.dependency)
        return _attn("working", run.last_event_at)

    if run.queued_position > 0:
        return _attn("queued_model", run.started_at, detail=f"#{run.queued_position}")
    if run.dependency:
        return _attn("dependency", run.last_event_at, detail=run.dependency)
    return _attn("none", None)


def _attn(kind: str, since: Optional[float], *, detail: str = "") -> Attention:
    return Attention(kind=kind, reason=_REASONS[kind], priority=_PRIORITY[kind], since=since, detail=detail)


# ---------------------------------------------------------------------------
# Wiring: real sources by default, every one overridable for tests. None of
# the real-module imports happen at module scope — agent_runs/tool_approvals/
# question_store/subagent_tools/core.database all stay lazy, the same
# "no import-time coupling" shape `agent_runs.py` itself uses for its own
# optional lookups (see its `_setting()`).
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _naive_utc_to_epoch(dt: Any) -> Optional[float]:
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


def _default_agent_activity() -> Dict[str, Dict[str, Any]]:
    from src import agent_runs
    return agent_runs.activity_details()


def _default_pending_approvals(owner: str) -> Iterable[str]:
    from src.tool_approvals import tool_approval_store
    return tool_approval_store.pending_session_ids(owner=owner)


def _default_open_questions(owner: str) -> List[Dict[str, Any]]:
    from src import question_store
    try:
        return question_store.list_open(owner=owner)
    except Exception:  # noqa: BLE001 - never let a listing failure break the tray
        return []


def _default_worker_cards() -> Dict[str, Dict[str, Any]]:
    try:
        from src.agent_tools.subagent_tools import worker_board
        return worker_board()
    except Exception:  # noqa: BLE001 - the delegate-worker board is optional
        return {}


def _default_finished_rows(owner: str, *, cutoff: float, exclude: Iterable[str]) -> List[Tuple[str, str, Optional[float]]]:
    """`(session_id, label, last_activity_epoch)` for sessions of `owner`
    whose most recent activity is real DB state (`Session.last_message_at`,
    falling back to `updated_at` — the exact fallback `routes/session_routes.py`
    already uses for its own "recent" sort) newer than `cutoff`, excluding
    sessions already accounted for by a live run/approval/question above.

    This is the ONLY source `finished_unreviewed` has: `src/agent_runs.py`
    evicts a terminal run's in-memory record ~3 minutes after it goes quiet
    (`_EVICT_GRACE_S`), and this module does not own that file, so it cannot
    add a longer-lived marker there. Falling back to the session's own
    last-activity timestamp is real data, not a new clock — but it is coarser
    than agent_runs': it cannot tell "the agent just finished a turn" apart
    from "the human sent one more message a minute ago and hasn't gotten a
    reply yet". Documented as a known limit, not silently smoothed over.
    """
    from core.database import SessionLocal, Session as DbSession
    from src.auth_helpers import owner_filter

    exclude_set = set(exclude)
    out: List[Tuple[str, str, Optional[float]]] = []
    db = SessionLocal()
    try:
        q = db.query(DbSession.id, DbSession.name, DbSession.last_message_at, DbSession.updated_at).filter(
            DbSession.archived == False  # noqa: E712
        )
        q = owner_filter(q, DbSession, owner)
        for row in q.all():
            if row.id in exclude_set:
                continue
            ts = _naive_utc_to_epoch(row.last_message_at) or _naive_utc_to_epoch(row.updated_at)
            if ts is None or ts < cutoff:
                continue
            out.append((row.id, row.name or row.id, ts))
    finally:
        db.close()
    return out


def attention_for_owner(
    owner: str,
    *,
    limit: int = 50,
    now: Optional[float] = None,
    agent_activity: Optional[Dict[str, Dict[str, Any]]] = None,
    pending_approval_sessions: Optional[Iterable[str]] = None,
    open_questions: Optional[List[Dict[str, Any]]] = None,
    worker_cards: Optional[Dict[str, Dict[str, Any]]] = None,
    finished_rows: Optional[List[Tuple[str, str, Optional[float]]]] = None,
    reads: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Every session of `owner` that needs attention, classified and sorted.

    Every keyword lets a caller (a test, or a future caller with its own
    idea of "what's running") substitute a fake for the corresponding real
    source without touching anything else — `classify()` itself never
    changes. Returns plain dicts (the route's own JSON shape), most urgent
    first, `ACTIONABLE_KINDS` only.
    """
    now = time.time() if now is None else now
    limit = max(1, min(int(limit or 50), 200))

    activity = _default_agent_activity() if agent_activity is None else agent_activity
    approval_sessions = set(_default_pending_approvals(owner) if pending_approval_sessions is None else pending_approval_sessions)
    questions = _default_open_questions(owner) if open_questions is None else open_questions
    question_by_session: Dict[str, Dict[str, Any]] = {}
    for q in questions:
        sid = str(q.get("session_id") or q.get("session") or "")
        if sid:
            question_by_session.setdefault(sid, q)
    workers = _default_worker_cards() if worker_cards is None else worker_cards
    reads_map = get_reads(owner) if reads is None else reads

    infos: Dict[str, RunInfo] = {}
    for sid, snap in activity.items():
        infos[sid] = RunInfo(
            session_id=sid,
            status="running",
            has_pending_approval=sid in approval_sessions,
            has_pending_question=sid in question_by_session,
            queued_position=int(snap.get("queued_position") or 0),
            last_event_at=snap.get("last_event_at"),
            started_at=snap.get("started_at"),
            label=str(snap.get("label") or ""),
        )
    # A pending approval/question on a session `agent_activity` did not
    # report (e.g. the detached run already ended, the card is what is
    # keeping the chat "open") still needs a row — never dropped just
    # because the run itself is no longer live.
    for sid in approval_sessions:
        if sid not in infos:
            infos[sid] = RunInfo(session_id=sid, has_pending_approval=True)
    for sid in question_by_session:
        if sid not in infos:
            infos[sid] = RunInfo(session_id=sid, has_pending_question=True)
        elif not infos[sid].has_pending_question:
            infos[sid] = replace(infos[sid], has_pending_question=True)

    for _child_sid, card in workers.items():
        parent = str(card.get("parent") or "")
        if parent in infos and not infos[parent].dependency:
            infos[parent] = replace(infos[parent], dependency=str(card.get("name") or _child_sid))

    rows = (
        _default_finished_rows(owner, cutoff=now - FINISHED_LOOKBACK_S, exclude=infos.keys())
        if finished_rows is None
        else finished_rows
    )
    for sid, label, ts in rows:
        if sid in infos:
            continue
        read_ts = reads_map.get(sid)
        if read_ts is not None and ts is not None and read_ts >= ts:
            continue  # already reviewed — finished_unreviewed has nothing else to resolve
        infos[sid] = RunInfo(session_id=sid, status="done", finished_at=ts, label=label)

    out: List[Dict[str, Any]] = []
    for sid, info in infos.items():
        att = classify(info, now=now)
        if att.kind not in ACTIONABLE_KINDS:
            continue
        read_ts = reads_map.get(sid)
        unread = read_ts is None or (att.since is not None and read_ts < att.since)
        out.append({
            "session_id": sid,
            "kind": att.kind,
            "reason": att.reason,
            "priority": att.priority,
            "since": att.since,
            "detail": att.detail,
            "label": info.label,
            "unread": unread,
        })

    out.sort(key=lambda r: (r["priority"], -(r["since"] or 0)))
    return out[:limit]


# ---------------------------------------------------------------------------
# Read marks — DATA_DIR/attention_reads.json, {owner: {session_id: ts}}.
# Small enough (one float per session a user has ever opened from the tray)
# that a plain atomically-written JSON file is the right amount of
# machinery — the same choice `core/atomic_io.py`'s own module docstring
# recommends for exactly this shape of file, and a level below the sqlite
# stores (`question_store.py`, `tool_approvals.py`) that need real
# transactions.
# ---------------------------------------------------------------------------

_READS_LOCK = threading.Lock()


def _reads_path() -> str:
    # Read `constants.DATA_DIR` at call time (not bound at import time) so a
    # test's `monkeypatch.setattr(src.constants, "DATA_DIR", tmp_path)` is
    # honoured the same way `src/agent_runs.py`'s own `_runs_dir()` does.
    return os.path.join(_constants.DATA_DIR, "attention_reads.json")


def _load_reads_file() -> Dict[str, Dict[str, float]]:
    path = _reads_path()
    if not os.path.isfile(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_reads(owner: str) -> Dict[str, float]:
    """This owner's `{session_id: read_at}` — never another owner's, since
    the top-level key of the file IS the owner."""
    data = _load_reads_file()
    owner_map = data.get(_owner_key(owner))
    if not isinstance(owner_map, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in owner_map.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def mark_read(owner: str, session_ids: Iterable[str], *, now: Optional[float] = None) -> int:
    """Record `now` as the read time for each of `session_ids`, for `owner`
    only — another owner's map is read, kept, and rewritten untouched (this
    is a read-modify-write of the WHOLE file, guarded by `_READS_LOCK` so two
    concurrent readers of this process never race each other's write; it is
    not a substitute for a real per-owner store if this ever needs to scale
    past "one JSON file", which a tray of read marks does not).
    """
    now = time.time() if now is None else now
    ids = [str(s) for s in session_ids if str(s or "").strip()]
    if not ids:
        return 0
    key = _owner_key(owner)
    with _READS_LOCK:
        data = _load_reads_file()
        owner_map = data.get(key)
        if not isinstance(owner_map, dict):
            owner_map = {}
        for sid in ids:
            owner_map[sid] = now
        data[key] = owner_map
        atomic_write_json(_reads_path(), data)
    return len(ids)


def _owner_key(owner: str) -> str:
    # The JSON object needs a real string key even in single-user mode
    # (`effective_user` can be "" / None there) — every other per-owner
    # store in this codebase treats that case as "the local user", not as
    # "no owner", so this does too rather than inventing a second meaning
    # for an empty key.
    return str(owner or "") or "_local"
