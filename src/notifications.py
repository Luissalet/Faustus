"""src/notifications.py — in-process event bus for the mobile app (lot M-A).

Faustus already has a client-driven notification store (`routes/notifications_
routes.py`, ACT-02): the web Studio *observes* a pending item through its own
polling and calls `POST /api/notifications/emit` to record it, with dedupe,
per-type channel prefs and quiet hours. That system was deliberately scoped to
never touch `chat_routes`/`agent_loop`/`task_scheduler` — it could only react
to what the browser already saw.

This module is the other half: a small **server-side** bus that the handful of
places where a turn/approval/task/reminder actually *finishes* push into
directly, so a phone that is not polling anything can still get a WebSocket
push the moment something happens. It does not replace the ACT-02 store, read
its file, or share its dedupe semantics — different consumer (a background
Android service holding one WS connection), different shape (a flat ring
instead of per-owner unread state), different name (`notifications.jsonl`
here vs `notifications.json` there) on purpose, so the two never collide.

Design: a bounded in-memory ring (last 200 events, oldest dropped first),
mirrored to `data/notifications.jsonl` so a server restart does not lose the
last few events a client reconnecting with `since_id` would otherwise miss.
Each `emit()` also wakes any `asyncio.Queue` a live WebSocket handler is
reading from (`subscribe`/`unsubscribe`). Every write path is best-effort:
`emit()` never raises into its caller — a notification is a courtesy, not
something a chat turn, an approval decision or a scheduled task should ever
fail over.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

NOTIFICATIONS_FILE = os.path.join(DATA_DIR, "notifications.jsonl")

#: Last-200 ring, mirrored to disk. 200 is generous for "what did I miss while
#: my phone was asleep" without the file or the in-memory list growing
#: unbounded on a chatty install.
RING_SIZE = 200

#: The only kinds this bus emits. Not enforced strictly (a caller typo should
#: not crash the turn that triggered it) but documented so a new kind is a
#: deliberate choice, not a drift.
KINDS = (
    "turn_finished", "turn_error", "approval_pending", "approval_resolved",
    "task_finished", "reminder",
)

_lock = threading.RLock()
_ring: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
_next_id = 1
#: owner -> list of live subscriber queues (each an `asyncio.Queue`, typed as
#: `Any` here since importing `asyncio` at module scope only to spell the
#: type would be the only reason to). The empty string key is used for
#: legacy/ownerless events (single-user installs, or a caller that could not
#: resolve an owner) — `list()`/`subscribe()` treat it as "everyone's own".
_subscribers: Dict[str, List[Any]] = {}


def _owner_key(owner: Optional[str]) -> str:
    return (owner or "").strip()


def _load_from_disk() -> None:
    """Best-effort replay of the last `RING_SIZE` lines on import, so a
    restarted process still answers `since_id` queries for events it did not
    itself emit this run. Corrupt/partial lines (a kill -9 mid-write) are
    skipped rather than aborting the whole load."""
    global _next_id
    if not os.path.exists(NOTIFICATIONS_FILE):
        return
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        logger.debug("notifications: could not read %s", NOTIFICATIONS_FILE, exc_info=True)
        return
    for line in lines[-RING_SIZE:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or "id" not in row:
            continue
        _ring.append(row)
        try:
            if int(row["id"]) >= _next_id:
                _next_id = int(row["id"]) + 1
        except (TypeError, ValueError):
            pass


_load_from_disk()


def _persist_ring() -> None:
    """Rewrite `notifications.jsonl` from the current ring. The ring is
    already bounded to `RING_SIZE`, so this is a small, cheap, atomic write —
    simpler and safer than an ever-appending log this module would then have
    to separately truncate."""
    try:
        from core.atomic_io import atomic_write_text
        with _lock:
            text = "\n".join(json.dumps(e, ensure_ascii=False) for e in _ring)
        if text:
            text += "\n"
        atomic_write_text(NOTIFICATIONS_FILE, text)
    except Exception:
        logger.debug("notifications: failed to persist ring", exc_info=True)


def emit(
    kind: str,
    *,
    owner: Optional[str],
    title: str = "",
    body: str = "",
    data: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    approval_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Append an event to the ring, persist it, and wake subscribers.

    Fire-and-forget by contract: any failure here (disk full, a bad `data`
    value, a subscriber queue that turns out to be closed) is logged and
    swallowed, never raised into the caller — a chat turn finishing or an
    approval being decided must never fail because a notification could not
    be recorded. Returns the stored event dict, or None if emission failed
    before an id could even be minted.
    """
    try:
        owner_key = _owner_key(owner)
        with _lock:
            global _next_id
            event = {
                "id": _next_id,
                "kind": str(kind or "").strip(),
                "owner": owner_key,
                "title": (title or "").strip()[:200],
                "body": (body or "").strip()[:200],
                "data": data if isinstance(data, dict) else {},
                "session_id": session_id or None,
                "approval_id": approval_id or None,
                "ts": time.time(),
            }
            _next_id += 1
            _ring.append(event)
            # Deliver to this owner's subscribers, plus anyone subscribed to
            # the ownerless/"all" key (single-user installs where nothing
            # stamped an owner on the request that triggered this event).
            targets = [*_subscribers.get(owner_key, ())]
            if owner_key:
                targets += [*_subscribers.get("", ())]
    except Exception:
        logger.warning("notifications.emit failed for kind=%r", kind, exc_info=True)
        return None

    try:
        _persist_ring()
    except Exception:
        # `_persist_ring` already swallows its own errors; this is a second
        # line of defense for a monkeypatched/replaced implementation (a
        # test double, or a future refactor) so the "emit never raises"
        # contract holds regardless of what persistence does internally.
        logger.debug("notifications: _persist_ring raised", exc_info=True)

    for q in targets:
        try:
            q.put_nowait(event)
        except Exception:
            # A full or closed queue is the subscriber's problem to notice
            # (it still has `list(since_id=...)` to catch up) — never the
            # emitter's.
            logger.debug("notifications: subscriber queue rejected event", exc_info=True)

    return event


def list_events(
    owner: Optional[str] = None,
    since_id: int = 0,
    limit: int = 50,
) -> Tuple[List[Dict[str, Any]], int]:
    """Events for `owner` (plus ownerless/legacy rows) newer than `since_id`,
    oldest-first, capped to `limit`. Returns `(rows, last_id)` where
    `last_id` is the id the caller should pass back next time — the highest
    id considered, even when `rows` came back empty because everything up to
    it was already seen.
    """
    owner_key = _owner_key(owner)
    since_id = max(0, int(since_id or 0))
    limit = max(1, min(int(limit or 50), RING_SIZE))
    with _lock:
        rows = [
            dict(e) for e in _ring
            if e["id"] > since_id and (not owner_key or not e.get("owner") or e.get("owner") == owner_key)
        ]
        newest_in_ring = _ring[-1]["id"] if _ring else since_id
    rows = rows[-limit:]
    # The caller's next `since_id` is the newest id actually returned, or —
    # if nothing new matched (including "nothing new for this owner" while
    # the ring itself moved on) — the newest id in the ring, so a caught-up
    # client never re-requests the same empty range forever.
    last_id = rows[-1]["id"] if rows else max(since_id, newest_in_ring)
    return rows, last_id


# Kept as a module-level alias so `from src import notifications as N; N.list(...)`
# reads naturally in the doc/tests without shadowing the builtin inside this
# module's own body (which uses `list_events` internally and never calls the
# builtin on a matching name).
list = list_events  # noqa: A001 - intentional, see docstring above


def latest_id() -> int:
    """The highest event id currently in the ring, or 0 if it is empty. What
    a fresh WS connection's `hello` reports before anything new has been
    emitted to it — the id a reconnecting client should pass as `since_id`."""
    with _lock:
        return _ring[-1]["id"] if _ring else 0


def subscribe(owner: Optional[str]):
    """Return a fresh asyncio.Queue that will receive every event emitted for
    `owner` (or ownerless events, if `owner` is falsy) from this point on.
    Must be paired with `unsubscribe` when the caller (a WebSocket handler)
    disconnects, or the queue leaks for the life of the process."""
    import asyncio
    q: "asyncio.Queue" = asyncio.Queue(maxsize=200)
    with _lock:
        _subscribers.setdefault(_owner_key(owner), []).append(q)
    return q


def unsubscribe(owner: Optional[str], queue) -> None:
    with _lock:
        bucket = _subscribers.get(_owner_key(owner))
        if bucket and queue in bucket:
            bucket.remove(queue)
            if not bucket:
                _subscribers.pop(_owner_key(owner), None)


def subscriber_count(owner: Optional[str] = None) -> int:
    """Test/diagnostic helper: how many live subscriber queues are there."""
    with _lock:
        if owner is None:
            return sum(len(v) for v in _subscribers.values())
        return len(_subscribers.get(_owner_key(owner), ()))
