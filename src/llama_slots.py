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
llama.cpp is never sent the field.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Dict, Optional

_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {}
_ASSIGNED: Dict[str, "OrderedDict[str, int]"] = {}

MIN_SLOTS = 3


def server_base(url: str) -> str:
    """`http://host:port` for a chat-completions or /v1 URL."""
    url = str(url or "").strip().rstrip("/")
    if "/v1" in url:
        return url.split("/v1")[0]
    return url


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
        if _COUNTS.get(base) != count:
            _COUNTS[base] = count
            # A server restarted with fewer slots: forget assignments that
            # would now name a slot it does not have.
            assigned = _ASSIGNED.get(base)
            if assigned:
                for sid in [s for s, slot in assigned.items() if slot >= count]:
                    del assigned[sid]


def slot_count(url: str) -> Optional[int]:
    with _LOCK:
        return _COUNTS.get(server_base(url))


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
        count = _COUNTS.get(base) or 0
        if count < MIN_SLOTS:
            return None
        assigned = _ASSIGNED.setdefault(base, OrderedDict())
        if sid in assigned:
            assigned.move_to_end(sid)
            return assigned[sid]
        taken = set(assigned.values())
        free = [s for s in range(1, count) if s not in taken]
        if free:
            slot = free[0]
        else:
            # Every slot has a chat: the one used longest ago gives it up.
            _oldest, slot = assigned.popitem(last=False)
        assigned[sid] = slot
        return slot


def reset() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _COUNTS.clear()
        _ASSIGNED.clear()
