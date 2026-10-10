"""Faustus' side of the Hub's browser lanes (Agora #265): one app at a time per site.

Several family apps drive a web browser (Tantalus reads shops, Argus and Phileas read pages, Faustus has browser tools).
Nothing stopped two of them from hitting the same shop at once, which doubles the automation footprint and gets the machine
blocked. The Hub keeps one lane per registrable root domain; this module is how Faustus takes it, through the vendored Hub
client (``src/hoard_link/browser_lane.py``):

* ``read_lane(url)`` wraps one bounded read (``reach_browser`` read / capture, and the browser fallback of ``reach_read`` /
  ``reach_extract`` / ``reach_crawl``): the lane is held for the read and given back at once;
* ``guard_call(...)`` is the dispatch-side gate of the shared browser MCP (``browser_navigate`` and friends): a browser that
  stays on a page keeps the lane of that page's site until it navigates to another site, is closed, or sits idle for
  ``IDLE_RELEASE_S``; every other browser call just counts as use.

A lane another app holds past the wait answers *«ocupado por X desde HH:MM:SS»* (``BrowserLaneBusy`` / a blocked tool result),
so the model knows who has the site and does not retry in a loop. Everything is a no-op when the Hub is not configured or
does not answer (the read goes ahead, a warning is logged once): ``FAUSTUS_BROWSER_LANES=0`` switches it off, ``=1`` also
uses the default Hub address when none is configured. Nothing here is ever raised for a Hub problem.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

HOLDER = "faustus"
READ_WAIT_S = 20.0        # a bounded read waits this long for a lane another app holds
NAV_WAIT_S = 20.0         # so does a browser_navigate
LANE_TTL_S = 300.0        # the Hub frees a lane its holder stopped renewing after this
IDLE_RELEASE_S = 180.0    # the shared browser gives its lane back after this long without a browser call
HUB_DOWN_FOR_S = 60.0     # after the Hub failed to answer, do not ask again for this long (a refused connect costs ~2 s)
SWEEP_EVERY_S = 15.0

_lock = threading.Lock()
_down_until = 0.0
_sweeper: Optional[threading.Thread] = None


class BrowserLaneBusy(ValueError):
    """Another app holds the site's lane. ``str()`` is the user-facing «ocupado por X ...» text."""

    def __init__(self, message: str, *, held_by: str = "", since: Optional[float] = None, domain: str = ""):
        super().__init__(message)
        self.held_by = held_by
        self.since = since
        self.domain = domain


@dataclass
class _Held:
    lane: Any
    domain: str
    last_used: float


_held: Dict[str, _Held] = {}


# ── when and where ──────────────────────────────────────────────────────────

def _hub_url() -> str:
    """The Hub Faustus already talks to (the hoardhub connector, ``HOARD_HUB_URL``, the data folder); '' lets the client decide."""
    try:
        from src import hoard_hub

        return str(hoard_hub.hub_location().get("url") or "")
    except Exception:  # noqa: BLE001 - location is best-effort
        return ""


def enabled() -> bool:
    """On when a Hub is configured for Faustus (or ``FAUSTUS_BROWSER_LANES=1``); ``FAUSTUS_BROWSER_LANES=0`` turns it off."""
    flag = (os.environ.get("FAUSTUS_BROWSER_LANES") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if time.monotonic() < _down_until:
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    try:
        from src import hoard_hub

        return bool(hoard_hub.hub_location().get("configured"))
    except Exception:  # noqa: BLE001
        return False


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
    """The registrable domain of an http(s) URL, '' for anything else (about:blank, data:, a relative path)."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    from src.hoard_link.browser_lane import registrable_domain

    return registrable_domain(parts.hostname)


def _new_lane(url: str, *, purpose: str, wait_s: float, ttl_s: float, track: bool) -> Any:
    from src.hoard_link.browser_lane import Lane

    return Lane(url, HOLDER, ttl_s=ttl_s, wait_s=wait_s, purpose=purpose, hub_url=_hub_url() or None,
                raise_on_busy=False, autostart=False, track=track)


# ── one bounded read ────────────────────────────────────────────────────────

@contextmanager
def read_lane(url: str, *, purpose: str = "reach_browser", wait_s: float = READ_WAIT_S) -> Iterator[None]:
    """Hold the site's lane for the duration of one read. Raises :class:`BrowserLaneBusy` when another app keeps it past
    ``wait_s``; does nothing when lanes are off, the URL has no site, or the Hub is away."""
    site = _site(url)
    if not site or not enabled():
        yield
        return
    try:
        held = _new_lane(url, purpose=purpose, wait_s=wait_s, ttl_s=LANE_TTL_S, track=True)
        held.acquire()
    except Exception as exc:  # noqa: BLE001 - a lane problem never fails a read
        logger.warning("browser lane for %s could not be taken: %s", site, exc)
        yield
        return
    try:
        if held.via == "local":
            _mark_hub_down()
        elif not held.acquired:
            raise BrowserLaneBusy(busy_text(held.domain, held.held_by, held.since), held_by=held.held_by,
                                  since=held.since, domain=held.domain)
        yield
    finally:
        try:
            held.release()
        except Exception:  # noqa: BLE001
            logger.debug("browser lane release failed", exc_info=True)


# ── the shared browser MCP (a page that stays open) ─────────────────────────

def _release_locked(conn_id: str) -> None:
    h = _held.pop(conn_id, None)
    if h is not None:
        try:
            h.lane.release()
        except Exception:  # noqa: BLE001
            logger.debug("browser lane release failed", exc_info=True)


def release(conn_id: str) -> None:
    """Give back the lane the browser connection ``conn_id`` holds (browser closed, idle, or moved to another site)."""
    with _lock:
        _release_locked(conn_id)


def release_all() -> None:
    with _lock:
        for conn_id in list(_held):
            _release_locked(conn_id)


def held_lanes() -> Dict[str, Dict[str, Any]]:
    """What the browser connections hold now, for status views and tests."""
    with _lock:
        now = time.monotonic()
        return {c: {"domain": h.domain, "idle_s": round(now - h.last_used, 1), "lease_id": h.lane.lease_id} for c, h in _held.items()}


def sweep(now: Optional[float] = None, idle_s: float = IDLE_RELEASE_S) -> int:
    """Release the lanes of browsers that have not been used for ``idle_s``; returns how many."""
    now = time.monotonic() if now is None else now
    with _lock:
        stale = [c for c, h in _held.items() if now - h.last_used >= idle_s]
        for conn_id in stale:
            _release_locked(conn_id)
    return len(stale)


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is not None and _sweeper.is_alive():
        return

    def loop() -> None:
        while True:
            time.sleep(SWEEP_EVERY_S)
            try:
                sweep()
            except Exception:  # noqa: BLE001
                logger.debug("browser lane sweep failed", exc_info=True)
            with _lock:
                if not _held:
                    return

    _sweeper = threading.Thread(target=loop, name="faustus-browser-lane-sweeper", daemon=True)
    _sweeper.start()


def touch(conn_id: str) -> None:
    with _lock:
        h = _held.get(conn_id)
        if h is not None:
            h.last_used = time.monotonic()


def guard_navigate(conn_id: str, url: str, *, wait_s: float = NAV_WAIT_S) -> Optional[Dict[str, Any]]:
    """Take the lane of ``url``'s site for the browser connection ``conn_id`` (blocking up to ``wait_s``). ``None`` when
    the navigation may go ahead; a blocked tool result saying who holds the site when it may not."""
    site = _site(url)
    if not site:
        touch(conn_id)
        return None
    if not enabled():
        return None
    with _lock:
        h = _held.get(conn_id)
        if h is not None and h.domain == site:
            h.last_used = time.monotonic()
            return None
        _release_locked(conn_id)          # leaving the old site: give it back before asking for the next one
    try:
        lane = _new_lane(url, purpose="browser_navigate", wait_s=wait_s, ttl_s=LANE_TTL_S, track=False)
        lane.acquire()
    except Exception as exc:  # noqa: BLE001 - never block a navigation over a Hub problem
        logger.warning("browser lane for %s could not be taken: %s", site, exc)
        return None
    if lane.via == "local":
        _mark_hub_down()
        return None
    if not lane.acquired:
        text = busy_text(lane.domain, lane.held_by, lane.since)
        return {"error": f"browser_navigate: {text}. Espera a que acabe, usa otra fuente o prueba más tarde; no insistas en bucle.",
                "exit_code": 1, "blocked": True, "policy": "browser_lane", "busy": True, "held_by": lane.held_by,
                "since": lane.since, "domain": lane.domain}
    with _lock:
        _release_locked(conn_id)          # a concurrent navigate may have stored one meanwhile
        _held[conn_id] = _Held(lane=lane, domain=site, last_used=time.monotonic())
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
