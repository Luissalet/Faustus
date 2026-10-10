"""Per-turn latency timeline of a voice conversation.

A voice turn passes through six moments. Each one is a wall-clock instant in
milliseconds since the epoch, recorded where it happens:

* ``speech_end``       the person stopped (the VAD closed the turn, or the
                       push-to-talk key was released)   - client
* ``transcript_ready`` speech-to-text finished (and the optional dictation
                       clean-up)                         - client
* ``request_sent``     the message left for the model   - client
* ``first_token``      the first token of the answer reached the screen - client
* ``tts_chunk``        the first spoken sentence came back from the speech
                       provider, ready to play          - client
* ``first_audio``      that audio began to play         - client

The server contributes what only it can measure: how long the speech-to-text
and the text-to-speech calls took on its side (``stt_server_ms``,
``tts_server_ms``) and the model's own time to first token
(``server_ttft_ms``, from the turn's metrics). Client marks all come from one
clock, so the gaps between them are exact; the server durations are separate
numbers and are never subtracted from a client instant.

The store is a small SQLite file under ``DATA_DIR`` so the separate MCP
process (``mcp_servers/harness_server.py``) reads the same rows as the REST
routes. Rows hold only timestamps and ids, never any text of the
conversation. Everything here is best-effort: a failing timeline must never
cost a voice turn.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

#: The marks, in the order a healthy turn produces them.
MARKS = ("speech_end", "transcript_ready", "request_sent", "first_token", "tts_chunk", "first_audio")
#: Server-side durations (milliseconds) that ride along with the marks.
SERVER_FIELDS = ("stt_server_ms", "tts_server_ms", "server_ttft_ms")
#: Gap name -> (from mark, to mark). The first three are the headline ones.
GAPS = {
    "speech_to_text": ("speech_end", "transcript_ready"),
    "text_to_first_token": ("transcript_ready", "first_token"),
    "first_token_to_audio": ("first_token", "first_audio"),
    "text_to_request": ("transcript_ready", "request_sent"),
    "request_to_first_token": ("request_sent", "first_token"),
    "first_token_to_tts_chunk": ("first_token", "tts_chunk"),
    "tts_chunk_to_playback": ("tts_chunk", "first_audio"),
    "total": ("speech_end", "first_audio"),
}
HEADLINE = ("speech_to_text", "text_to_first_token", "first_token_to_audio")

MAX_ROWS = 2000
#: A mark further from "now" than this is a bad clock, not a measurement.
MAX_SKEW_MS = 7 * 24 * 3600 * 1000
#: A gap longer than this (10 minutes) is not a voice turn's latency.
MAX_GAP_MS = 10 * 60 * 1000

_lock = threading.Lock()


def db_path() -> str:
    return os.path.join(DATA_DIR, "voice_timeline.db")


@contextlib.contextmanager
def _connect():
    conn = _raw_connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _raw_connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voice_turns ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, turn_id TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',"
        " recorded_at REAL NOT NULL, data TEXT NOT NULL,"
        " UNIQUE(owner, session_id, turn_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS voice_turns_session ON voice_turns(session_id, id)")
    return conn


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def clean_marks(raw: Dict[str, Any], now_ms: Optional[float] = None) -> Dict[str, Any]:
    """Keep only the known fields with sane numbers; unknown or bad ones are dropped."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    out: Dict[str, Any] = {}
    for key in MARKS:
        value = _num((raw or {}).get(key))
        if value is not None and value > 0 and abs(now_ms - value) <= MAX_SKEW_MS:
            out[key] = int(round(value))
    for key in SERVER_FIELDS:
        value = _num((raw or {}).get(key))
        if value is not None and 0 <= value <= MAX_GAP_MS:
            out[key] = int(round(value))
    return out


def compute_gaps(marks: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Milliseconds between marks; None when either end is missing or the order is impossible."""
    gaps: Dict[str, Optional[int]] = {}
    for name, (a, b) in GAPS.items():
        start, end = marks.get(a), marks.get(b)
        if start is None or end is None:
            gaps[name] = None
            continue
        delta = end - start
        gaps[name] = int(delta) if 0 <= delta <= MAX_GAP_MS else None
    return gaps


def _record(row: sqlite3.Row) -> Dict[str, Any]:
    data = json.loads(row["data"])
    return {
        "session_id": row["session_id"], "turn_id": row["turn_id"], "owner": row["owner"],
        "recorded_at": row["recorded_at"], "marks": data.get("marks", {}),
        "server": data.get("server", {}), "gaps": compute_gaps(data.get("marks", {})),
        "complete": all(m in data.get("marks", {}) for m in ("speech_end", "transcript_ready", "first_token", "first_audio")),
    }


def record_turn(session_id: str, turn_id: str, raw: Dict[str, Any], owner: str = "",
                now_ms: Optional[float] = None) -> Dict[str, Any]:
    """Insert or merge one turn. A second call for the same turn adds the marks that were missing.

    A turn is keyed by (owner, session, turn): two people posting the same ids get two
    separate rows, so one can never read or change the other's marks.
    """
    session_id, turn_id = str(session_id or "")[:200], str(turn_id or "")[:200]
    if not session_id or not turn_id:
        raise ValueError("session_id and turn_id are required")
    cleaned = clean_marks(raw, now_ms)
    marks = {k: v for k, v in cleaned.items() if k in MARKS}
    server = {k: v for k, v in cleaned.items() if k in SERVER_FIELDS}
    with _lock, _connect() as conn:
        owner = owner or ""
        row = conn.execute("SELECT * FROM voice_turns WHERE owner=? AND session_id=? AND turn_id=?",
                           (owner, session_id, turn_id)).fetchone()
        if row is not None:
            old = json.loads(row["data"])
            marks = {**old.get("marks", {}), **marks}
            server = {**old.get("server", {}), **server}
            conn.execute("UPDATE voice_turns SET data=? WHERE id=?",
                         (json.dumps({"marks": marks, "server": server}), row["id"]))
        else:
            conn.execute("INSERT INTO voice_turns(session_id, turn_id, owner, recorded_at, data) VALUES (?,?,?,?,?)",
                         (session_id, turn_id, owner, time.time(), json.dumps({"marks": marks, "server": server})))
            conn.execute("DELETE FROM voice_turns WHERE id <= (SELECT MAX(id) FROM voice_turns) - ?", (MAX_ROWS,))
        saved = conn.execute("SELECT * FROM voice_turns WHERE owner=? AND session_id=? AND turn_id=?",
                             (owner, session_id, turn_id)).fetchone()
        return _record(saved)


def list_turns(session_id: Optional[str] = None, limit: int = 50, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first. ``owner`` None means every owner (the local single-user case)."""
    limit = max(1, min(int(limit or 50), 500))
    sql, args = "SELECT * FROM voice_turns", []
    where = []
    if session_id:
        where.append("session_id=?"); args.append(session_id)
    if owner:
        where.append("(owner='' OR owner=?)"); args.append(owner)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    if not os.path.exists(db_path()):
        return []
    with _lock, _connect() as conn:
        return [_record(r) for r in conn.execute(sql, args).fetchall()]


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in 0..100); None for no values."""
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q / 100.0
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (pos - low)


def aggregate(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """p50/p90 (ms) and sample count of every gap over the given turns."""
    out: Dict[str, Any] = {"turns": len(turns), "gaps": {}}
    for name in GAPS:
        values = [t["gaps"][name] for t in turns if t["gaps"].get(name) is not None]
        p50, p90 = percentile(values, 50), percentile(values, 90)
        out["gaps"][name] = {
            "n": len(values),
            "p50_ms": None if p50 is None else int(round(p50)),
            "p90_ms": None if p90 is None else int(round(p90)),
        }
    return out


def session_report(session_id: Optional[str] = None, limit: int = 50, owner: Optional[str] = None) -> Dict[str, Any]:
    turns = list_turns(session_id, limit, owner)
    return {"session_id": session_id or None, "turns": turns, "aggregate": aggregate(turns)}
