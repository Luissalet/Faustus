"""Faustus' side of the Hub's browser lanes (Agora #265): one app at a time per site.

Several family apps drive a web browser (Tantalus reads shops, Argus and Phileas read pages, Faustus has browser tools).
Nothing stopped two of them from hitting the same shop at once, which doubles the automation footprint and gets the machine
blocked. The Hub keeps one lane per registrable root domain; this module is how Faustus takes it, through the vendored Hub
client (``src/hoard_link/browser_lane.py``):

* ``read_lane(url)`` wraps one bounded read (``reach_browser`` read / capture, and the browser fallback of ``reach_read`` /
  ``reach_extract`` / ``reach_crawl``): the lane is held for the read and given back at once;
* ``guard_call(...)`` is the dispatch-side gate of the shared browser MCP (``browser_navigate`` and friends): a browser that
  stays on a page keeps the lane of that page's site until it navigates to another site, is closed or disconnected, or sits
  idle for ``IDLE_RELEASE_S``; every other browser call just counts as use. Links clicked inside the page that lead to another
  site are not followed: the lane stays the one of the site the browser was last *sent* to.

**Faustus never waits on itself.** All of Faustus is one holder to the Hub, so the lane of a site is shared inside the process:
a read of a site one of Faustus' browsers already holds, two browsers (two chats) on the same site, or two reads at once, all
use the one lane (a user count; it goes back to the Hub when the last user leaves). Only another *app* can make Faustus wait.

A lane another app holds past the wait answers *«ocupado por X desde HH:MM:SS»* (``BrowserLaneBusy`` / a blocked tool result),
so the model knows who has the site and does not retry in a loop. Everything is a no-op when the Hub is not configured or
does not answer (the read goes ahead, a warning is logged once): ``FAUSTUS_BROWSER_LANES=0`` switches it off, ``=1`` also
uses the default Hub address when none is configured. Nothing here is ever raised for a Hub problem.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

HOLDER = "faustus"
READ_WAIT_S = 20.0        # a bounded read waits this long for a lane another app holds
NAV_WAIT_S = 20.0         # so does a browser_navigate
LANE_TTL_S = 300.0        # the Hub frees a lane its holder stopped renewing after this (the client renews it meanwhile)
IDLE_RELEASE_S = 60.0     # the shared browser gives its lane back after this long without a browser call
HUB_DOWN_FOR_S = 60.0     # after the Hub failed to answer, do not ask again for this long (a refused connect costs ~2 s)
BUSY_MEMO_S = 5.0         # a second caller for a site that was just answered "busy" gets the same answer without waiting again
SWEEP_EVERY_S = 15.0

_lock = threading.RLock()
_down_until = 0.0
_sweeper: Optional[threading.Thread] = None
_ids = itertools.count(1)


class BrowserLaneBusy(ValueError):
    """Another app holds the site's lane. ``str()`` is the user-facing «ocupado por X ...» text."""

    def __init__(self, message: str, *, held_by: str = "", since: Optional[float] = None, domain: str = ""):
        super().__init__(message)
        self.held_by = held_by
        self.since = since
        self.domain = domain


@dataclass
class _Shared:
    """The one Hub lane Faustus holds for a site, and who inside Faustus is using it (user id -> last use, monotonic)."""

    lane: Any
    domain: str
    users: Dict[str, float] = field(default_factory=dict)


_shared: Dict[str, _Shared] = {}              # domain -> the lane Faustus holds
_conn_domain: Dict[str, str] = {}             # browser connection id -> the domain it holds
_gates: Dict[str, threading.Lock] = {}        # domain -> serialises taking the lane (so two callers never wait on each other)
_recent_busy: Dict[str, Tuple[float, BrowserLaneBusy]] = {}


# ── when and where ──────────────────────────────────────────────────────────

def _hub_location() -> Dict[str, Any]:
    try:
        from src import hoard_hub

        return dict(hoard_hub.hub_location() or {})
    except Exception:  # noqa: BLE001 - location is best-effort
        return {}


def _hub_url() -> str:
    """The Hub Faustus already talks to (the hoardhub connector, ``HOARD_HUB_URL``, the data folder); '' lets the client decide."""
    return str(_hub_location().get("url") or "")


def _token_file() -> str:
    """The family token Faustus talks to the Hub with (the lane is bound to whoever it belongs to)."""
    return str(_hub_location().get("token_file") or "")


def enabled() -> bool:
    """On when a Hub is configured for Faustus (or ``FAUSTUS_BROWSER_LANES=1``); ``FAUSTUS_BROWSER_LANES=0`` turns it off."""
    flag = (os.environ.get("FAUSTUS_BROWSER_LANES") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if time.monotonic() < _down_until:
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(_hub_location().get("configured"))


def _mark_hub_down() -> None:
    global _down_until
    _down_until = time.monotonic() + HUB_DOWN_FOR_S


def busy_text(domain: str, held_by: str = "", since: Optional[float] = None, purpose: str = "") -> str:
    """«ocupado por X desde HH:MM:SS»: what a tool answers when another app holds the site's browser lane."""
    when = ""
    if since:
        try:
            from datetime import datetime

            when = datetime.fromtimestamp(float(since)).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            when = ""
    text = f"ocupado por {held_by or 'otra app'}" + (f" desde las {when}" if when else "")
    if domain:
        text += f": el carril de navegador de {domain} lo tiene esa app del Hub (la está leyendo ahora mismo)"
    if purpose:
        text += f" [{purpose}]"
    return text


def _site(url: str) -> str:
    """The lane key of an http(s) URL (registrable domain; a local server keeps its port), '' for anything else."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    from src.hoard_link.browser_lane import registrable_domain

    return registrable_domain(str(url).strip())


def _new_lane(url: str, *, purpose: str, wait_s: float) -> Any:
    from src.hoard_link.browser_lane import Lane

    return Lane(url, HOLDER, ttl_s=LANE_TTL_S, wait_s=wait_s, purpose=purpose, hub_url=_hub_url() or None,
                raise_on_busy=False, autostart=False, track=False, token_file=_token_file() or None)


# ── the lane of a site, shared inside Faustus ───────────────────────────────

def _join(domain: str, url: str, user: str, purpose: str, wait_s: float) -> Tuple[Optional[BrowserLaneBusy], bool]:
    """Make ``user`` a user of the lane of ``domain``, taking it from the Hub when Faustus does not hold it yet (blocking up
    to ``wait_s``). ``(None, True)``: it may go ahead and holds a share; ``(None, False)``: it may go ahead without a lane (no
    Hub); ``(busy, False)``: another app holds the site."""
    with _lock:
        held = _shared.get(domain)
        if held is not None:
            held.users[user] = time.monotonic()
            return None, True
        gate = _gates.setdefault(domain, threading.Lock())
    with gate:                                       # one caller at a time asks the Hub for this site
        with _lock:
            held = _shared.get(domain)
            if held is not None:                     # the caller before us took it
                held.users[user] = time.monotonic()
                return None, True
            memo = _recent_busy.get(domain)
            if memo is not None and time.monotonic() - memo[0] < BUSY_MEMO_S:
                return memo[1], False
        try:
            lane = _new_lane(url, purpose=purpose, wait_s=wait_s)
            lane.acquire()
        except Exception as exc:  # noqa: BLE001 - a lane problem never fails a read
            logger.warning("browser lane for %s could not be taken: %s", domain, exc)
            return None, False
        if lane.via == "local":
            _mark_hub_down()
            return None, False
        if not lane.acquired:
            busy = BrowserLaneBusy(busy_text(lane.domain, lane.held_by, lane.since), held_by=lane.held_by,
                                   since=lane.since, domain=lane.domain)
            with _lock:
                _recent_busy[domain] = (time.monotonic(), busy)
            return busy, False
        with _lock:
            _recent_busy.pop(domain, None)
            _shared[domain] = _Shared(lane=lane, domain=domain, users={user: time.monotonic()})
        return None, True


def _leave(domain: str, user: str) -> None:
    """``user`` is done with the lane of ``domain``; the last one out gives it back to the Hub."""
    lane = None
    with _lock:
        held = _shared.get(domain)
        if held is None:
            return
        held.users.pop(user, None)
        if not held.users:
            _shared.pop(domain, None)
            lane = held.lane
    if lane is not None:
        try:
            lane.release()
        except Exception:  # noqa: BLE001
            logger.debug("browser lane release failed", exc_info=True)


# ── one bounded read ────────────────────────────────────────────────────────

@contextmanager
def read_lane(url: str, *, purpose: str = "reach_browser", wait_s: Optional[float] = None) -> Iterator[None]:
    """Hold the site's lane for the duration of one read. Raises :class:`BrowserLaneBusy` when another app keeps it past
    ``wait_s``; does nothing when lanes are off, the URL has no site, or the Hub is away. A lane Faustus already holds (a
    browser sitting on that site, another read) is used as it is."""
    site = _site(url)
    if not site or not enabled():
        yield
        return
    user = f"read:{next(_ids)}"
    busy, joined = _join(site, url, user, purpose, READ_WAIT_S if wait_s is None else wait_s)
    if busy is not None:
        raise busy
    try:
        yield
    finally:
        if joined:
            _leave(site, user)


# ── the shared browser MCP (a page that stays open) ─────────────────────────

def release(conn_id: str) -> None:
    """Give back the share the browser connection ``conn_id`` holds (browser closed, disconnected, idle, or on another site)."""
    with _lock:
        domain = _conn_domain.pop(conn_id, None)
    if domain:
        _leave(domain, f"conn:{conn_id}")


def release_all() -> None:
    """Give every lane back to the Hub (shutdown)."""
    with _lock:
        lanes = [h.lane for h in _shared.values()]
        _shared.clear()
        _conn_domain.clear()
    for lane in lanes:
        try:
            lane.release()
        except Exception:  # noqa: BLE001
            logger.debug("browser lane release failed", exc_info=True)


def held_lanes() -> Dict[str, Dict[str, Any]]:
    """What the browser connections hold now, for status views and tests."""
    with _lock:
        now = time.monotonic()
        out: Dict[str, Dict[str, Any]] = {}
        for conn, domain in _conn_domain.items():
            held = _shared.get(domain)
            if held is not None:
                out[conn] = {"domain": domain, "idle_s": round(now - held.users.get(f"conn:{conn}", now), 1),
                             "lease_id": held.lane.lease_id, "users": len(held.users)}
        return out


def sweep(now: Optional[float] = None, idle_s: Optional[float] = None) -> int:
    """Release the lanes of browsers that have not been used for ``idle_s``; returns how many connections were let go."""
    now = time.monotonic() if now is None else now
    idle_s = IDLE_RELEASE_S if idle_s is None else idle_s
    with _lock:
        stale = []
        for c, d in _conn_domain.items():
            held = _shared.get(d)
            if held is None or now - held.users.get(f"conn:{c}", now) >= idle_s:
                stale.append(c)
    for conn_id in stale:
        release(conn_id)
    return len(stale)


def _ensure_sweeper() -> None:
    """Start the idle sweeper if it is not running. Checked and started under the lock the sweeper uses to decide to stop,
    so a connection registered while it winds down is never left without one."""
    global _sweeper
    with _lock:
        if _sweeper is not None and _sweeper.is_alive():
            return

        def loop() -> None:
            global _sweeper
            while True:
                time.sleep(SWEEP_EVERY_S)
                try:
                    sweep()
                except Exception:  # noqa: BLE001
                    logger.debug("browser lane sweep failed", exc_info=True)
                with _lock:
                    if not _conn_domain:
                        _sweeper = None
                        return

        _sweeper = threading.Thread(target=loop, name="faustus-browser-lane-sweeper", daemon=True)
        _sweeper.start()


def touch(conn_id: str) -> None:
    with _lock:
        domain = _conn_domain.get(conn_id)
        held = _shared.get(domain) if domain else None
        if held is not None:
            held.users[f"conn:{conn_id}"] = time.monotonic()


def guard_navigate(conn_id: str, url: str, *, wait_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Take the lane of ``url``'s site for the browser connection ``conn_id`` (blocking up to ``wait_s``). ``None`` when
    the navigation may go ahead; a blocked tool result saying who holds the site when it may not."""
    site = _site(url)
    if not site:
        touch(conn_id)
        return None
    if not enabled():
        return None
    with _lock:
        current = _conn_domain.get(conn_id)
    if current == site:
        touch(conn_id)
        return None
    release(conn_id)                      # leaving the old site: give it back before asking for the next one
    busy, joined = _join(site, url, f"conn:{conn_id}", "browser_navigate", NAV_WAIT_S if wait_s is None else wait_s)
    if busy is not None:
        return {"error": f"browser_navigate: {busy}. Espera a que acabe, usa otra fuente o prueba más tarde; no insistas en bucle.",
                "exit_code": 1, "blocked": True, "policy": "browser_lane", "busy": True, "held_by": busy.held_by,
                "since": busy.since, "domain": busy.domain}
    if joined:
        with _lock:
            _conn_domain[conn_id] = site
        _ensure_sweeper()
    return None


async def guard_call(conn_id: str, tool_name: str, arguments: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The dispatch-side gate for a browser MCP call: ``None`` to go ahead, a tool-result dict to answer instead."""
    base = str(tool_name or "")
    if base == "browser_navigate":
        url = str((arguments or {}).get("url") or "")
        return await asyncio.to_thread(guard_navigate, conn_id, url)
    if base == "browser_close":
        await asyncio.to_thread(release, conn_id)
        return None
    touch(conn_id)
    return None
