"""exec_ledger.py — a causal, append-only ledger of what a run did.

The replay buffer of a detached run (``src/agent_runs.py``) and the Studio
transcript are views: they compact, truncate, evict and are rebuilt from what
the UI chose to keep. This ledger is the other thing: one row per causal fact
about a run, written when the fact happens and never changed afterwards.

Kinds of rows (``kind``)::

    run_started      the run began                      payload: label, model
    run_state        a state transition of the run      payload: state, reason
    call_requested   the model asked for a tool call    call_id, payload: tool, args_sha256
    approval_requested  the call was parked on a card   approval_id, call_id, payload: tool, args_sha256, digest, expires_at
    approval_granted / approval_denied                  approval_id, call_id, payload: scope
    call_resumed     an approved call is executed       call_id (the call the card was made for), approval_id, payload: executed_in_run, executed_call_id, args_match
    attempt_started  a handler was entered              call_id, attempt_id
    attempt_result   what came back                     call_id, attempt_id, effect_id, payload: status, effect_certainty, result_sha256, result_bytes, exit_code, duration_ms
    steer_*          queued / drained / applied / dropped / redelivered steering messages (``ref`` = receipt id)
    model_call       one model call of the run          payload: phase, transport, model, usage fields, duration_ms, error
    child_result     a delegated worker finished        payload: child_session_id, worker_id, name, tokens, duration_ms, outcome

Properties:

* append-only: the table refuses UPDATE and DELETE (triggers); the only way a
  row leaves is ``purge_session`` when the session itself is deleted;
* independent of the UI: nothing here reads or writes the replay buffer;
* writing never blocks a tool call. A write that fails is counted in
  ``summary()["lost_writes"]`` and logged; the effect outbox
  (``src/effect_outbox.py``) is the gate, this is the record;
* ``replay(run_id)`` folds the rows back into the run's state, call by call.

Result *references* (a SHA-256 and a length), never the result body: the
ledger proves which result a call produced without keeping its content.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Mapping, Optional

logger = logging.getLogger(__name__)

#: Identifies this process. A row written under another boot id belongs to a
#: process that is gone.
BOOT_ID = uuid.uuid4().hex

KINDS = (
    "run_started", "run_state", "call_requested", "approval_requested", "approval_granted",
    "approval_denied", "approval_expired", "call_resumed", "attempt_started", "attempt_result",
    "steer_queued", "steer_drained", "steer_applied", "steer_dropped", "steer_redelivered",
    "model_call", "child_result",
)

_db_override: Optional[str] = None
_lost_writes = 0
_init_lock = threading.Lock()
_initialized: set = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    call_id TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    approval_id TEXT NOT NULL DEFAULT '',
    effect_id TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    boot_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS exec_events_run ON exec_events (run_id, seq);
CREATE INDEX IF NOT EXISTS exec_events_session ON exec_events (session_id, seq);
CREATE INDEX IF NOT EXISTS exec_events_approval ON exec_events (approval_id) WHERE approval_id != '';
CREATE INDEX IF NOT EXISTS exec_events_ref ON exec_events (kind, ref) WHERE ref != '';
CREATE TRIGGER IF NOT EXISTS exec_events_no_update BEFORE UPDATE ON exec_events
BEGIN SELECT RAISE(ABORT, 'exec_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS exec_events_no_delete BEFORE DELETE ON exec_events
BEGIN SELECT RAISE(ABORT, 'exec_events is append-only'); END;
"""


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def set_db_path(path: Optional[str]) -> None:
    global _db_override
    _db_override = path
    _initialized.discard(path or "")


def db_path() -> str:
    if _db_override:
        return _db_override
    from src import constants
    return os.path.join(str(getattr(constants, "DATA_DIR", "data")), "exec_ledger.sqlite3")


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_exec_ledger", True))
    except Exception:  # noqa: BLE001
        return True


@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if path not in _initialized:
            with _init_lock:
                conn.executescript(_SCHEMA)
                _initialized.add(path)
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def sha256_of(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif isinstance(value, str):
        data = value.encode("utf-8", "replace")
    else:
        try:
            data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError):
            data = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def run_key(run_id: Any, session_id: Any) -> str:
    return str(run_id or "") or (f"session:{session_id}" if session_id else "")


def record(kind: str, *, run_id: Any = "", session_id: Any = "", owner: Any = "", call_id: Any = "",
           attempt_id: Any = "", approval_id: Any = "", effect_id: Any = "", ref: Any = "",
           payload: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Append one row. Never raises; returns the sequence number, or ``None``
    when the ledger is disabled or the write was lost (counted)."""
    global _lost_writes
    if kind not in KINDS or not enabled():
        return None
    key = run_key(run_id, session_id)
    if not key:
        return None
    try:
        body = json.dumps(dict(payload or {}), ensure_ascii=False, default=str)
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO exec_events (ts, run_id, session_id, owner, kind, call_id, attempt_id, approval_id,"
                " effect_id, ref, boot_id, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), key, str(session_id or ""), str(owner or ""), kind, str(call_id or ""),
                 str(attempt_id or ""), str(approval_id or ""), str(effect_id or ""), str(ref or ""),
                 BOOT_ID, body))
            return int(cur.lastrowid)
    except Exception:  # noqa: BLE001 - a record that cannot be written is counted, never raised
        _lost_writes += 1
        logger.warning("exec ledger write lost (%s)", kind, exc_info=True)
        return None


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    item = {k: r[k] for k in r.keys()}
    try:
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
    except (TypeError, ValueError):
        item["payload"] = {}
    return item


def events(run_id: str, *, after_seq: int = 0, limit: int = 5000, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM exec_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (str(run_id), int(after_seq), max(1, min(int(limit), 20000)))).fetchall()
    except sqlite3.Error:
        return []
    out = [_row(r) for r in rows]
    if owner is not None:
        out = [e for e in out if not e["owner"] or e["owner"] == str(owner)]
    return out


def run_ids_for_session(session_id: str, *, limit: int = 50) -> List[str]:
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT run_id, MAX(seq) AS last FROM exec_events WHERE session_id = ? GROUP BY run_id "
                "ORDER BY last DESC LIMIT ?", (str(session_id), int(limit))).fetchall()
    except sqlite3.Error:
        return []
    return [r["run_id"] for r in rows]


def purge_session(session_id: str) -> int:
    """The only way rows leave: the session they belong to was deleted."""
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DROP TRIGGER IF EXISTS exec_events_no_delete")
                cur = conn.execute("DELETE FROM exec_events WHERE session_id = ?", (str(session_id),))
                n = cur.rowcount
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS exec_events_no_delete BEFORE DELETE ON exec_events "
                    "BEGIN SELECT RAISE(ABORT, 'exec_events is append-only'); END")
                conn.execute("COMMIT")
                return int(n)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.Error:
        logger.warning("exec ledger purge failed for %s", session_id, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# typed writers
# ---------------------------------------------------------------------------

def run_started(run_id: Any, session_id: Any, *, label: str = "", model: str = "", owner: Any = "") -> Optional[int]:
    return record("run_started", run_id=run_id, session_id=session_id, owner=owner,
                  payload={"label": str(label or "")[:200], "model": str(model or "")[:200]})


def run_state(run_id: Any, session_id: Any, state: str, *, reason: str = "", owner: Any = "") -> Optional[int]:
    return record("run_state", run_id=run_id, session_id=session_id, owner=owner,
                  payload={"state": str(state), "reason": str(reason or "")[:300]})


def call_requested(run_id: Any, session_id: Any, call_id: Any, *, tool: str, args_sha256: str,
                   owner: Any = "", effect_class: str = "") -> Optional[int]:
    return record("call_requested", run_id=run_id, session_id=session_id, owner=owner, call_id=call_id,
                  payload={"tool": str(tool)[:120], "args_sha256": args_sha256, "effect_class": effect_class})


def approval_requested(run_id: Any, session_id: Any, call_id: Any, approval_id: Any, *, tool: str,
                       args_sha256: str, digest: str = "", expires_at: float = 0.0, owner: Any = "") -> Optional[int]:
    return record("approval_requested", run_id=run_id, session_id=session_id, owner=owner, call_id=call_id,
                  approval_id=approval_id,
                  payload={"tool": str(tool)[:120], "args_sha256": args_sha256, "digest": digest,
                           "expires_at": expires_at})


def approval_decided(run_id: Any, session_id: Any, call_id: Any, approval_id: Any, *, granted: bool,
                     scope: str = "", owner: Any = "", reason: str = "") -> Optional[int]:
    return record("approval_granted" if granted else "approval_denied", run_id=run_id, session_id=session_id,
                  owner=owner, call_id=call_id, approval_id=approval_id,
                  payload={"scope": str(scope or ""), "reason": str(reason or "")[:200]})


def call_resumed(run_id: Any, session_id: Any, call_id: Any, *, approval_id: Any, executed_in_run: Any,
                 executed_call_id: Any, args_match: Optional[bool], owner: Any = "") -> Optional[int]:
    """The approved call is being executed. Recorded under the run and call id
    the card was created for, so the call keeps one history across a restart;
    ``executed_in_run`` names the run that actually executes it."""
    return record("call_resumed", run_id=run_id, session_id=session_id, owner=owner, call_id=call_id,
                  approval_id=approval_id,
                  payload={"executed_in_run": str(executed_in_run or ""), "executed_call_id": str(executed_call_id or ""),
                           "args_match": args_match})


def attempt_started(run_id: Any, session_id: Any, call_id: Any, attempt_id: Any, *, owner: Any = "") -> Optional[int]:
    return record("attempt_started", run_id=run_id, session_id=session_id, owner=owner, call_id=call_id,
                  attempt_id=attempt_id)


def attempt_result(run_id: Any, session_id: Any, call_id: Any, attempt_id: Any, *, status: str,
                   effect_certainty: str, result: Any = None, exit_code: Any = None,
                   duration_ms: Optional[int] = None, effect_id: Any = "", owner: Any = "",
                   error: str = "") -> Optional[int]:
    if isinstance(result, (str, bytes)):
        body = result
    else:
        body = json.dumps(result, default=str, sort_keys=True) if result is not None else ""
    return record("attempt_result", run_id=run_id, session_id=session_id, owner=owner, call_id=call_id,
                  attempt_id=attempt_id, effect_id=effect_id,
                  payload={"status": str(status), "effect_certainty": str(effect_certainty),
                           "result_sha256": sha256_of(body) if body else "", "result_bytes": len(body or ""),
                           "exit_code": exit_code, "duration_ms": duration_ms, "error": str(error or "")[:300]})


def current_identity(session_id: Any = None) -> tuple:
    """(run_id, session_id) of the detached run the calling context belongs to,
    or ("", "") outside one. Read from the server-owned run binding, never from
    anything a model or client supplied."""
    try:
        from src.run_causality import _ORIGIN
        origin = _ORIGIN.get()
        if origin is not None and (not session_id or origin.session_id == str(session_id)):
            return origin.run_id or "", origin.session_id or ""
    except Exception:  # noqa: BLE001
        pass
    return "", ""


_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
               "cached_tokens", "reasoning_tokens", "cost_usd")


def model_call(run_id: Any, session_id: Any, *, phase: str = "", transport: str = "", model: str = "",
               usage: Optional[Mapping[str, Any]] = None, duration_ms: Optional[float] = None, error: str = "",
               source: str = "", owner: Any = "") -> Optional[int]:
    """One model call of a run, with only what was observed: a usage field the
    call did not report is absent here, never zero."""
    observed: Dict[str, Any] = {}
    for key in _USAGE_KEYS:
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            observed[key] = value
    for key in ("cost_state", "usage_source"):
        if (usage or {}).get(key):
            observed[key] = str((usage or {})[key])
    return record("model_call", run_id=run_id, session_id=session_id, owner=owner,
                  payload={"phase": phase, "transport": transport, "model": str(model or "")[:200], "usage": observed,
                           "duration_ms": round(duration_ms, 1) if isinstance(duration_ms, (int, float)) else None,
                           "error": str(error or "")[:200], "source": source})


def child_result(run_id: Any, session_id: Any, *, child_session_id: str, worker_id: str, name: str = "",
                 role: str = "", outcome: str = "", stop_reason: str = "", input_tokens: Any = None,
                 output_tokens: Any = None, duration_ms: Optional[float] = None, tool_calls: Any = None,
                 parent_call_id: Any = None, owner: Any = "") -> Optional[int]:
    return record("child_result", run_id=run_id, session_id=session_id, owner=owner, call_id=parent_call_id or "",
                  payload={"child_session_id": child_session_id, "worker_id": worker_id, "name": name[:120],
                           "role": role[:60], "outcome": outcome, "stop_reason": stop_reason,
                           "input_tokens": input_tokens, "output_tokens": output_tokens, "tool_calls": tool_calls,
                           "duration_ms": round(duration_ms, 1) if isinstance(duration_ms, (int, float)) else None})


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

_TERMINAL_RUN_STATES = frozenset({"done", "stopped", "error", "interrupted"})
_STATUS_TO_CALL_STATE = {
    "succeeded": "succeeded", "failed": "failed", "outcome_unknown": "outcome_unknown",
    "partial": "partial", "cancelled": "cancelled", "interrupted": "interrupted",
    "blocked": "failed", "denied": "denied", "timeout": "failed",
}


def _new_call(call_id: str) -> Dict[str, Any]:
    return {"call_id": call_id, "tool": "", "args_sha256": "", "state": "requested", "attempts": [],
            "approval": None, "resumed": None, "effect_id": "",
            "effect_certainty": "none", "requested_seq": 0, "last_seq": 0}


def replay(run_id: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Fold a run's rows back into its state.

    The result never depends on the UI: a call is ``awaiting_approval`` while
    its card is pending, ``approved`` after a grant, ``running`` between
    ``attempt_started`` and ``attempt_result``, and ends in the state its
    result reported. An approved call executed after a restart keeps its own
    history: ``resumed`` names the run that executed it and whether the
    arguments matched what was requested.
    """
    rows = events(run_id, owner=owner)
    calls: Dict[str, Dict[str, Any]] = {}
    run: Dict[str, Any] = {"run_id": run_id, "session_id": "", "state": "unknown", "reason": "", "label": "",
                           "started_at": "", "last_event_at": "", "boots": []}
    for ev in rows:
        kind, p, cid = ev["kind"], ev["payload"], ev["call_id"]
        run["session_id"] = run["session_id"] or ev["session_id"]
        run["last_event_at"] = ev["ts"]
        if ev["boot_id"] and ev["boot_id"] not in run["boots"]:
            run["boots"].append(ev["boot_id"])
        if kind == "run_started":
            run.update(state="running", started_at=ev["ts"], label=p.get("label", ""), model=p.get("model", ""))
        elif kind == "run_state":
            run["state"], run["reason"] = p.get("state", run["state"]), p.get("reason", "")
        if not cid:
            continue
        call = calls.setdefault(cid, _new_call(cid))
        call["last_seq"] = ev["seq"]
        if kind == "call_requested":
            call.update(tool=p.get("tool", call["tool"]), args_sha256=p.get("args_sha256", call["args_sha256"]),
                        requested_seq=call["requested_seq"] or ev["seq"])
        elif kind == "approval_requested":
            call.update(tool=p.get("tool", call["tool"]), args_sha256=p.get("args_sha256", call["args_sha256"]),
                        requested_seq=call["requested_seq"] or ev["seq"])
            if call["state"] == "requested":
                call["state"] = "awaiting_approval"
            call["approval"] = {"approval_id": ev["approval_id"], "state": "pending", "requested_at": ev["ts"],
                                "digest": p.get("digest", ""), "expires_at": p.get("expires_at", 0)}
        elif kind in ("approval_granted", "approval_denied"):
            if call["approval"] is None:
                call["approval"] = {"approval_id": ev["approval_id"], "state": "pending"}
            granted = kind == "approval_granted"
            call["approval"].update(state="granted" if granted else "denied", decided_at=ev["ts"],
                                    scope=p.get("scope", ""), decided_boot=ev["boot_id"])
            call["state"] = "approved" if granted else "denied"
        elif kind == "approval_expired":
            if call["approval"]:
                call["approval"]["state"] = "expired"
            call["state"] = "expired"
        elif kind == "call_resumed":
            call["resumed"] = {"approval_id": ev["approval_id"], "executed_in_run": p.get("executed_in_run", ""),
                               "executed_call_id": p.get("executed_call_id", ""), "args_match": p.get("args_match"),
                               "resumed_boot": ev["boot_id"]}
            if call["approval"] is not None and call["approval"].get("state") == "granted":
                call["approval"]["state"] = "consumed"
        elif kind == "attempt_started":
            call["attempts"].append({"attempt_id": ev["attempt_id"], "started_at": ev["ts"], "state": "running"})
            call["state"] = "running"
        elif kind == "attempt_result":
            attempt = next((a for a in call["attempts"] if a["attempt_id"] == ev["attempt_id"]), None)
            if attempt is None:
                attempt = {"attempt_id": ev["attempt_id"], "started_at": ev["ts"]}
                call["attempts"].append(attempt)
            attempt.update(state=_STATUS_TO_CALL_STATE.get(p.get("status", ""), p.get("status", "failed")),
                           status=p.get("status", ""), effect_certainty=p.get("effect_certainty", "none"),
                           result_sha256=p.get("result_sha256", ""), result_bytes=p.get("result_bytes", 0),
                           finished_at=ev["ts"], exit_code=p.get("exit_code"), duration_ms=p.get("duration_ms"))
            call["state"] = attempt["state"]
            call["effect_certainty"] = p.get("effect_certainty", call["effect_certainty"])
            if ev["effect_id"]:
                call["effect_id"] = ev["effect_id"]
    ordered = sorted(calls.values(), key=lambda c: (c["requested_seq"] or c["last_seq"], c["call_id"]))
    return {
        **run,
        "event_count": len(rows),
        "last_seq": rows[-1]["seq"] if rows else 0,
        "calls": ordered,
        "pending_approvals": [c["approval"]["approval_id"] for c in ordered
                              if c["approval"] and c["approval"]["state"] == "pending"],
        "unresolved": [c["call_id"] for c in ordered if c["state"] in ("running", "outcome_unknown", "partial")],
    }


def pending_calls(owner: Optional[str] = None, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Calls that still wait for an approval, from the ledger alone."""
    try:
        with _db() as conn:
            sql = ("SELECT run_id, MAX(seq) AS last FROM exec_events WHERE kind = 'approval_requested'"
                   + (" AND session_id = ?" if session_id else "") + " GROUP BY run_id ORDER BY last DESC LIMIT 200")
            runs = [r["run_id"] for r in conn.execute(sql, ((str(session_id),) if session_id else ())).fetchall()]
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for rid in runs:
        state = replay(rid, owner=owner)
        for call in state["calls"]:
            if call["approval"] and call["approval"]["state"] == "pending":
                out.append({**call, "run_id": rid, "session_id": state["session_id"]})
    return out


def resume_target(approval_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The exact call an approval belongs to, as the ledger recorded it when
    the card was created: which run, which call id, which tool, which argument
    digest. Used to prove that an approval granted after a restart resumes that
    call and not a new one."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM exec_events WHERE kind = 'approval_requested' AND approval_id = ? ORDER BY seq LIMIT 1",
                (str(approval_id),)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    ev = _row(row)
    if owner is not None and ev["owner"] and ev["owner"] != str(owner):
        return None
    return {"run_id": ev["run_id"], "session_id": ev["session_id"], "call_id": ev["call_id"],
            "approval_id": ev["approval_id"], "tool": ev["payload"].get("tool", ""),
            "args_sha256": ev["payload"].get("args_sha256", ""), "digest": ev["payload"].get("digest", "")}


# ---------------------------------------------------------------------------
# listing, recovery, summary
# ---------------------------------------------------------------------------

def list_runs(*, owner: Optional[str] = None, session_id: Optional[str] = None,
              limit: int = 30) -> List[Dict[str, Any]]:
    try:
        with _db() as conn:
            where: List[str] = []
            args: List[Any] = []
            if session_id:
                where.append("session_id = ?")
                args.append(str(session_id))
            if owner is not None:
                where.append("(owner = '' OR owner = ?)")
                args.append(str(owner))
            sql = ("SELECT run_id, session_id, MAX(seq) AS last_seq, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                   "COUNT(*) AS n FROM exec_events" + (" WHERE " + " AND ".join(where) if where else "")
                   + " GROUP BY run_id ORDER BY last_seq DESC LIMIT ?")
            rows = conn.execute(sql, (*args, max(1, min(int(limit), 200)))).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        state = replay(r["run_id"])
        out.append({"run_id": r["run_id"], "session_id": r["session_id"], "events": r["n"], "first_at": r["first_ts"],
                    "last_at": r["last_ts"], "state": state["state"], "calls": len(state["calls"]),
                    "pending_approvals": len(state["pending_approvals"]), "unresolved": len(state["unresolved"])})
    return out


def recover_after_restart() -> Dict[str, Any]:
    """Close what a previous process left open, by appending rows (never by
    rewriting): a run still ``running`` becomes ``interrupted``; a call between
    ``attempt_started`` and ``attempt_result`` gets an ``interrupted`` result
    whose effect certainty is ``unknown``. Approvals still pending stay pending:
    they survive a restart and can still be granted."""
    closed_runs: List[str] = []
    closed_calls: List[Dict[str, str]] = []
    try:
        with _db() as conn:
            run_rows = conn.execute(
                "SELECT DISTINCT run_id FROM exec_events WHERE boot_id != ?", (BOOT_ID,)).fetchall()
    except sqlite3.Error:
        return {"runs": [], "calls": []}
    for r in run_rows:
        state = replay(r["run_id"])
        if state["state"] in _TERMINAL_RUN_STATES or state["state"] == "waiting_user":
            continue
        for call in state["calls"]:
            for attempt in call["attempts"]:
                if attempt.get("state") == "running":
                    attempt_result(state["run_id"], state["session_id"], call["call_id"], attempt["attempt_id"],
                                   status="interrupted", effect_certainty="unknown",
                                   error="the process ended before a result was recorded")
                    closed_calls.append({"run_id": state["run_id"], "call_id": call["call_id"]})
        if state["state"] in ("running", "unknown") and state["started_at"]:
            run_state(state["run_id"], state["session_id"], "interrupted", reason="process_restart")
            closed_runs.append(state["run_id"])
    return {"runs": closed_runs, "calls": closed_calls}


def summary() -> Dict[str, Any]:
    try:
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM exec_events").fetchone()["n"]
            runs = conn.execute("SELECT COUNT(DISTINCT run_id) AS n FROM exec_events").fetchone()["n"]
    except sqlite3.Error:
        n, runs = -1, -1
    return {"enabled": enabled(), "events": n, "runs": runs, "lost_writes": _lost_writes, "boot_id": BOOT_ID,
            "path": db_path()}


__all__ = [
    "BOOT_ID", "KINDS", "approval_decided", "approval_requested", "attempt_result", "attempt_started",
    "call_requested", "call_resumed", "child_result", "current_identity", "db_path", "enabled", "events", "list_runs", "pending_calls",
    "model_call", "purge_session", "record", "recover_after_restart", "replay", "resume_target", "run_ids_for_session",
    "run_started", "run_state", "set_db_path", "sha256_of", "summary",
]
