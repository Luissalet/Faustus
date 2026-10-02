"""Local ports and "is this app already running": what ``port.py`` (16 copies) and ``scripts/launch.py`` (15 copies) did.

Standard library only (HTTP probes use ``urllib`` with proxies disabled, so ``HTTP_PROXY`` never hijacks a loopback
call). The Node twin is ``validPort`` / ``canListen`` / ``findAvailablePort`` / ``alreadyRunning`` in
``js/hoard-commons/server.js``.

* :func:`can_listen`, :func:`find_available_port`, :func:`free_port`.
* :func:`already_running` — decide *before touching the data folder* whether a second start should just exit: the
  port is busy **and** ``GET /api/health`` answers ``{"service": <name>}``.
* :func:`wait_healthy`, :func:`open_in_browser`.

On Windows ``SO_REUSEADDR`` lets a socket bind a port that another one is already listening on, so the old probe
said "free" for a port in use; here Windows uses ``SO_EXCLUSIVEADDRUSE`` and POSIX keeps ``SO_REUSEADDR`` (so a
port in ``TIME_WAIT`` still counts as free).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

__all__ = ["can_listen", "find_available_port", "free_port", "already_running", "wait_healthy", "open_in_browser", "health_url", "fetch_health"]

DEFAULT_HOST = "127.0.0.1"


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def can_listen(port: int, host: str = DEFAULT_HOST) -> bool:
    """True when a TCP socket can bind ``host:port`` right now (nothing else is listening there)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(_family(host), socket.SOCK_STREAM) as probe:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
    except OSError:
        return False


def find_available_port(preferred: int, *, span: int = 20, host: str = DEFAULT_HOST) -> int:
    """The first bindable port in ``preferred .. preferred + span``. ``RuntimeError`` when none is free,
    ``ValueError`` for a ``preferred`` outside 1..65535."""
    try:
        first = int(preferred)
    except (TypeError, ValueError):
        raise ValueError(f"not a port: {preferred!r}") from None
    if not 1 <= first <= 65535:
        raise ValueError(f"not a port: {preferred!r}")
    last = min(65535, first + max(0, int(span)))
    for port in range(first, last + 1):
        if can_listen(port, host):
            return port
    raise RuntimeError(f"No free port between {first} and {last}.")


def free_port(host: str = DEFAULT_HOST) -> int:
    """Any free port (the OS picks it); for tests and one-off servers."""
    with socket.socket(_family(host), socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def health_url(base: str) -> str:
    """``base`` with ``/api/health`` appended unless it already points at a path."""
    b = base.strip()
    rest = b.split("://", 1)[-1]
    if "/" not in rest.rstrip("/"):
        return b.rstrip("/") + "/api/health"
    return b


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_health(url: str, *, timeout: float = 1.0) -> Optional[dict[str, Any]]:
    """GET ``url`` (a ``/api/health`` URL) and return the JSON object, or ``None`` on any failure or non-200."""
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def already_running(service: str, port: int, *, host: str = DEFAULT_HOST, timeout: float = 1.0) -> bool:
    """True when ``port`` is taken **and** ``GET /api/health`` there answers ``{"service": service}``: another
    instance of this very app is serving. Anything else (port free, another program, another Hoard) is False."""
    if can_listen(port, host):
        return False
    probe_host = f"[{host}]" if ":" in host else host
    data = fetch_health(f"http://{probe_host}:{int(port)}/api/health", timeout=timeout)
    return bool(data) and data.get("service") == service


def wait_healthy(url: str, service: Optional[str] = None, timeout: float = 30.0, *, poll: float = 0.2,
                 sleep: Callable[[float], Any] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """Poll ``url`` (a base URL or a ``/api/health`` URL) until it answers 200 (with ``{"service": service}`` when
    ``service`` is given) or ``timeout`` seconds pass. Returns whether it came up."""
    target = health_url(url)
    deadline = clock() + max(0.0, timeout)
    while True:
        data = fetch_health(target, timeout=min(1.0, max(0.1, timeout)))
        if data is not None and (service is None or data.get("service") == service):
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the default browser (``os.startfile`` on Windows, ``webbrowser`` elsewhere). Only http(s)
    and file URLs are opened. Returns whether something was launched; never raises."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://", "file://")):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - a launcher must never fail because no browser is installed
        return False
