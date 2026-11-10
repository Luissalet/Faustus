"""What waits for the person in the Agora, told to their phone.

The Agora lives in the family hub. A Faustus page that is open sees for itself what waits (the notification tray asks
once a minute); a phone with nothing open does not. This asks the hub the same question on the person's behalf, without
moving any read mark, and puts each new mention and each new question on the notification bus (``src/notifications``),
from where Web Push takes it to the devices that asked for notifications (``src/push``).

Told once: what was already told is remembered in ``<data>/agora_watch.json``. The first look only takes note of what
is there, so starting this for the first time does not ring for a week of old mentions; neither does anything older
than a day. After a long silence (Faustus was off, the PC was off) only the latest few are told one by one and the
rest in a single line. A question asked again in other words is a new question. A hub that is not answering is simply
asked again later.

The person chooses what is told. A server they silenced is left out by the hub itself (``skip_muted``), and so is, once
it is heard again, what was said in it meanwhile. Mentions and questions each have their own switch in the notification
preferences (``routes/notifications_routes.py``); what a switch kept quiet is still taken as seen, so turning it back
on does not ring for what happened meanwhile either.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

POLL_S = 60.0
FRESH_S = 86_400
KEEP = 600
#: How many are told one by one in a single look; beyond that, one line says how many more wait.
MAX_PER_LOOK = 3
STATE_FILE = os.path.join(DATA_DIR, "agora_watch.json")
PERSON = "luis"
_SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKS = re.compile(r"\*\*|__|`+")


def _line(text: Any, limit: int = 180) -> str:
    """One line fit for a lock screen: no line breaks, no Markdown marks, nothing shaped like a token."""
    text = _MARKS.sub("", _LINK.sub(r"\1", str(text or "")))
    text = _SECRET_LIKE.sub("[redacted]", " ".join(text.split()))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _place(space: str, thread: Any) -> str:
    from urllib.parse import quote
    return "/agora?space_id=" + quote(str(space), safe="") + "&c=" + quote(f"t:{thread}", safe="")


def waiting(inbox: Dict[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """What the hub's inbox of the person holds, as things to tell: ``{key, kind, title, body, url, fresh}``."""
    now = time.time() if now is None else now
    out: List[Dict[str, Any]] = []
    for row in inbox.get("escalated") or []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        space = str(row.get("space_id") or "ecosystem")
        who = str(row.get("escalated_by") or "").strip()
        asked_how = zlib.crc32(str(row.get("question") or "").encode("utf-8")) & 0xFFFFFFFF
        out.append({"key": f"agora:q:{space}:{row['id']}:{asked_how:x}", "kind": "agora_question", "who": who,
                    "when": float(row.get("updated") or 0),
                    "title": _line("Ágora · " + (who + " · " if who else "") + str(row.get("title") or ""), 120),
                    "body": _line(row.get("question") or ""), "url": _place(space, row["id"]),
                    "fresh": now - float(row.get("updated") or 0) <= FRESH_S,
                    "data": {"space_id": space, "thread_id": row["id"]}})
    asked = {(item["data"]["space_id"], item["data"]["thread_id"]) for item in out}
    for row in inbox.get("pending") or []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        space = str(row.get("space_id") or "ecosystem")
        if (space, row.get("thread_id")) in asked:      # the question itself names the person: told once, as the question
            continue
        out.append({"key": f"agora:m:{space}:{row['id']}", "kind": "agora_mention", "who": str(row.get("author") or ""),
                    "when": float(row.get("created") or 0),
                    "title": _line("Ágora · " + str(row.get("author") or "") + " · " + str(row.get("thread_title") or ""), 120),
                    "body": _line(row.get("body") or ""), "url": _place(space, row.get("thread_id")),
                    "fresh": now - float(row.get("created") or 0) <= FRESH_S,
                    "data": {"space_id": space, "thread_id": row.get("thread_id"), "message_id": row["id"]}})
    return out


def _load() -> Optional[List[str]]:
    """The keys already told, oldest first; None when this has never looked."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            seen = json.load(fh).get("seen")
        return [str(k) for k in seen] if isinstance(seen, list) else None
    except (OSError, ValueError, AttributeError):
        return None


def _save(seen: List[str]) -> None:
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(STATE_FILE, {"seen": seen[-KEEP:]}, indent=0)
    except Exception:  # noqa: BLE001 - told twice at worst
        logger.debug("agora_watch: could not save what was told", exc_info=True)


def _owners() -> List[str]:
    """Whose devices asked for notifications; the ownerless bucket when nobody did (the bus still keeps the event)."""
    try:
        from src import push
        owners = sorted({str(row.get("owner") or "") for row in push._load_subscriptions() if isinstance(row, dict)})
    except Exception:  # noqa: BLE001
        owners = []
    return owners or [""]


def _ask() -> Optional[Dict[str, Any]]:
    from routes.agora_routes import hub_request
    status, payload, _ = hub_request("GET", "/api/agora/inbox", timeout=20.0, query={
        "agent": PERSON, "all_spaces": "1", "peek": "1", "mine_only": "1", "skip_muted": "1", "limit": "60"})
    return payload if status == 200 and isinstance(payload, dict) and payload.get("ok") is not False else None


def _wants(owner: str) -> Dict[str, bool]:
    """Which kinds this owner asked to be told; everything when the preferences cannot be read."""
    try:
        from routes.notifications_routes import agora_wants
        chosen = agora_wants(owner)
    except Exception:  # noqa: BLE001
        return {"agora_mention": True, "agora_question": True}
    return {"agora_mention": bool(chosen.get("mentions", True)), "agora_question": bool(chosen.get("questions", True))}


def look(now: Optional[float] = None) -> Tuple[int, int]:
    """One look at the hub: tells what is new. ``(told, waiting)``; ``(0, -1)`` when the hub did not answer."""
    inbox = _ask()
    if inbox is None:
        return 0, -1
    items = waiting(inbox, now)
    seen = _load()
    first = seen is None
    seen = seen or []
    known = set(seen)
    changed = first
    news: List[Dict[str, Any]] = []
    for item in items:
        if item["key"] in known:
            continue
        known.add(item["key"])
        seen.append(item["key"])
        changed = True
        if not first and item["fresh"]:
            news.append(item)
    told = 0
    if news:
        from src import notifications
        news.sort(key=lambda item: item["when"], reverse=True)
        for owner in _owners():
            wants = _wants(owner)
            mine = [item for item in news if wants.get(item["kind"], True)]
            told = max(told, len(mine))
            one_by_one, rest = mine[:MAX_PER_LOOK], mine[MAX_PER_LOOK:]
            for item in reversed(one_by_one):              # oldest first, as they happened
                notifications.emit(item["kind"], owner=owner or None, title=item["title"], body=item["body"],
                                   data={**item["data"], "url": item["url"]})
            if rest:
                names = list(dict.fromkeys(item["who"] for item in rest if item["who"]))
                notifications.emit("agora_mention", owner=owner or None, title=f"Ágora · +{len(rest)}",
                                   body=_line(", ".join(names)), data={"url": "/agora?c=v%3Ainbox", "more": len(rest)})
    if changed:
        _save(seen)
    return told, len(items)


async def run_watch(poll_s: float = POLL_S) -> None:
    """Look once a minute for as long as Faustus runs."""
    await asyncio.sleep(min(20.0, poll_s))
    while True:
        try:
            await asyncio.to_thread(look)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a notification is a courtesy
            logger.debug("agora_watch: a look failed", exc_info=True)
        await asyncio.sleep(poll_s)
