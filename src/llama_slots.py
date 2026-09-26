"""One llama-server slot per agent chat.

llama-server keeps one prompt cache per slot. A request that names no slot
goes to the idle slot whose cache shares the most of its prompt, but only
past `--slot-prompt-similarity` (0.5 here); below that it takes the least
recently used slot, often an empty one. So when an early part of a long
prompt changes (old tool images folded, context dropped on a retry) the
round did not reuse the part before the change: it moved to another slot
and read the whole prompt again. Live on exam 32 (26-09-2026) that happened
three times in 25 minutes, 68k-75k tokens and about 110 s each.

Naming the slot (`id_slot`) keeps a chat's rounds on the same cache, which
then reuses everything up to the first changed token. Each agent chat gets a
slot of its own from 1..n-1; slot 0 stays free for requests that pick their
own. With fewer than three slots nothing is pinned, since two chats would
end up waiting on each other. A slot count is only known once
`model_context` has read the server's /slots, so a server that is not
llama.cpp is never sent the field; a count older than `COUNT_TTL` seconds is
not trusted (a slot the server no longer has would make it hold the request
forever).

Helper requests (titles, reviews, one-off completions) are sent to the
slots no chat holds, in turn (`helper_slot`). Left to the server they went
by similarity or to the least recently used slot, which on the 3B helper
server (similarity 0.1) was the chat's own: live, the second turn of a chat
on its pinned slot still read its whole prompt again.
"""
from __future__ import annotations

import logging
import threading
import time
import zlib
from collections import OrderedDict
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {}
_SEEN_AT: Dict[str, float] = {}
_ASSIGNED: Dict[str, "OrderedDict[str, int]"] = {}
_HELPER_TURN: Dict[str, int] = {}
_BUSY: Dict[str, frozenset] = {}

MIN_SLOTS = 3
COUNT_TTL = 600.0


def server_base(url: str) -> str:
    """`http://host:port` for a chat-completions or /v1 URL."""
    url = str(url or "").strip().rstrip("/")
    if "/v1" in url:
        return url.split("/v1")[0]
    return url


def note_slots(url: str, slots) -> None:
    """Record a llama-server's /slots answer: how many slots, and which are
    working right now (another Faustus on the same server may hold those)."""
    if not isinstance(slots, list):
        return
    note_slot_count(url, len(slots))
    busy = set()
    for i, slot in enumerate(slots):
        if isinstance(slot, dict) and slot.get("is_processing"):
            try:
                busy.add(int(slot.get("id", i)))
            except (TypeError, ValueError):
                busy.add(i)
    with _LOCK:
        _BUSY[server_base(url)] = frozenset(busy)


def note_slot_count(url: str, count: int) -> None:
    """Record how many slots the llama-server at `url` has (from /slots)."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return
    if count <= 0:
        return
    base = server_base(url)
    with _LOCK:
        _SEEN_AT[base] = time.monotonic()
        if _COUNTS.get(base) != count:
            _COUNTS[base] = count
            # A server restarted with fewer slots: forget assignments that
            # would now name a slot it does not have.
            assigned = _ASSIGNED.get(base)
            if assigned:
                for sid in [s for s, slot in assigned.items() if slot >= count]:
                    del assigned[sid]


def _fresh_count(base: str) -> int:
    """The recorded slot count for `base` (lock held), 0 when unknown or stale."""
    seen = _SEEN_AT.get(base)
    if seen is None or time.monotonic() - seen > COUNT_TTL:
        return 0
    return _COUNTS.get(base) or 0


def slot_count(url: str) -> Optional[int]:
    with _LOCK:
        return _fresh_count(server_base(url)) or None


def _enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("llamacpp_pin_session_slot", True))
    except Exception:  # noqa: BLE001 - settings unavailable: keep the default
        return True


def slot_for(url: str, session_id: Optional[str]) -> Optional[int]:
    """The slot this chat's rounds should use on `url`, or None to let the
    server choose (unknown server, too few slots, no chat, or turned off)."""
    if not session_id or not _enabled():
        return None
    base = server_base(url)
    sid = str(session_id)
    with _LOCK:
        count = _fresh_count(base)
        if count < MIN_SLOTS:
            return None
        assigned = _ASSIGNED.setdefault(base, OrderedDict())
        if sid in assigned:
            assigned.move_to_end(sid)
            return assigned[sid]
        taken = set(assigned.values())
        free = [s for s in range(1, count) if s not in taken]
        # Prefer a slot the server is not working on (another Faustus on the
        # same server may be holding it), and spread chats by their id so two
        # instances do not both start from slot 1.
        idle = [s for s in free if s not in _BUSY.get(base, frozenset())]
        pool = idle or free
        if pool:
            slot = pool[zlib.crc32(sid.encode("utf-8")) % len(pool)]
        else:
            # Every slot has a chat: the one used longest ago gives it up.
            _oldest, slot = assigned.popitem(last=False)
        assigned[sid] = slot
    logger.info("[engine] chat %s keeps llama-server slot %s on %s (%s slots)",
                sid[:8], slot, base, count)
    return slot


def helper_slot(url: str) -> Optional[int]:
    """A slot no chat holds on `url`, taken in turn so parallel helper
    requests still run side by side; None when nothing is pinned there."""
    if not _enabled():
        return None
    base = server_base(url)
    with _LOCK:
        count = _fresh_count(base)
        if count < MIN_SLOTS:
            return None
        taken = set((_ASSIGNED.get(base) or {}).values())
        free = [s for s in range(count) if s not in taken] or [0]
        turn = _HELPER_TURN.get(base, 0)
        _HELPER_TURN[base] = turn + 1
        return free[turn % len(free)]


def reset() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _COUNTS.clear()
        _SEEN_AT.clear()
        _ASSIGNED.clear()
        _HELPER_TURN.clear()
        _BUSY.clear()
