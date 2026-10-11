"""Browser lanes: one app at a time per site, so two apps never automate the same shop together.

Several family apps drive a web browser (Tantalus reads shops, Faustus has browser tools, Argus and Phileas read pages).
Nothing stopped two of them from opening the same shop at once, or from using the same Edge profile concurrently, which
doubles the automation footprint and gets the machine blocked. The hub keeps one *lane* per registrable root domain
(``shop.example.co.uk`` -> ``example.co.uk``) and, optionally, one per browser profile folder. An app takes the lane before
it opens a browser against the site and gives it back when it is done::

    from hoard_link import lane, LaneBusy

    with lane("https://shop.example.com/p/1", holder="tantalus", ttl_s=300, wait_s=60) as l:
        ...                         # the lane is renewed in the background and released on exit

    try:
        with lane("example.com", holder="faustus", wait_s=20, profile=r"C:\\edge-profile"):
            ...
    except LaneBusy as busy:
        print(busy.held_by, busy.since)     # who has it, since when

    with lane("example.com", holder="argus", wait_s=0, raise_on_busy=False) as l:
        if not l.acquired:
            return f"busy: {l.held_by}"

Entering waits up to ``wait_s`` for the lane (the hub queues waiters in arrival order) and then raises :class:`LaneBusy`
(or, with ``raise_on_busy=False``, enters with ``acquired == False`` and ``busy`` filled in). A holder that crashes frees the
lane when its ``ttl_s`` runs out (or at once when its process is gone); a long session keeps it by being renewed in the
background. Nesting inside the same thread (``lane`` for a site that thread already holds) just counts.

Apps must keep working without the hub: when no hub answers (or the hub is too old to know lanes) the context manager logs
once and lets the caller proceed (``l.via == "local"``). Unlike the GPU lease, this client never starts a hub on its own.

Standard library only (it is vendored into every app).
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from . import _hubclient
from .errors import HoardLinkError

__all__ = ["lane", "Lane", "LaneBusy", "LaneError", "registrable_domain", "profile_key", "lane_status"]

log = logging.getLogger("hoard_link.browser_lane")

DEFAULT_TTL_S = 300.0
DEFAULT_WAIT_S = 30.0
POLL_WAIT_S = 25.0
RELEASE_TRIES = 3
_warned: set[str] = set()


class LaneError(HoardLinkError, ValueError):
    """The hub refused the request itself (no domain and no profile, a bad number...)."""


class LaneBusy(HoardLinkError, RuntimeError):
    """The lane was still taken when ``wait_s`` ran out. ``held_by`` / ``since`` say by whom and since when."""

    def __init__(self, message: str, *, domain: str = "", held_by: str = "", since: Optional[float] = None,
                 expires_at: Optional[float] = None, purpose: str = "", waited_s: float = 0.0, waiting: int = 0):
        self.domain = domain
        self.held_by = held_by
        self.since = since
        self.expires_at = expires_at
        self.purpose = purpose
        self.waited_s = waited_s
        self.waiting = waiting
        super().__init__(message)

    @property
    def since_text(self) -> str:
        return _clock_text(self.since)


def _clock_text(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------- the lane key
#: Two-label public suffixes (the registrable domain is one label more). A small list on purpose: no new dependency.
_SECOND_LEVEL = frozenset("""
co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk
com.es org.es nom.es gob.es edu.es
com.au net.au org.au edu.au gov.au asn.au id.au
co.nz net.nz org.nz govt.nz ac.nz
co.jp ne.jp or.jp ac.jp go.jp
com.br net.br org.br gov.br edu.br
com.mx org.mx gob.mx edu.mx net.mx
com.ar gob.ar org.ar net.ar edu.ar
com.co org.co net.co edu.co gov.co
com.pe com.ve com.uy com.ec com.bo com.py com.cl
co.za org.za net.za gov.za ac.za
co.in net.in org.in gov.in ac.in firm.in gen.in ind.in
co.kr or.kr ne.kr go.kr ac.kr
com.cn net.cn org.cn gov.cn edu.cn
com.tw org.tw net.tw edu.tw gov.tw
com.hk org.hk net.hk edu.hk gov.hk
com.sg org.sg net.sg edu.sg gov.sg
com.my org.my net.my edu.my gov.my
com.ph org.ph net.ph edu.ph gov.ph
com.tr org.tr net.tr edu.tr gov.tr
com.pt org.pt edu.pt gov.pt
com.pl net.pl org.pl
com.ua org.ua net.ua gov.ua
co.il org.il net.il ac.il gov.il
co.id or.id web.id ac.id go.id
co.th or.th ac.th go.th in.th
com.vn net.vn org.vn edu.vn gov.vn
com.eg org.eg edu.eg gov.eg
com.ng org.ng gov.ng edu.ng
com.sa org.sa edu.sa gov.sa
com.pk org.pk net.pk edu.pk gov.pk
co.ke or.ke ac.ke go.ke
asso.fr gouv.fr com.fr
""".split())
#: Suffixes under which every customer is a different site (and a different lane): hosting platforms and the "rented" ones.
_PRIVATE_SUFFIX = frozenset("""
github.io gitlab.io blogspot.com wordpress.com herokuapp.com vercel.app netlify.app pages.dev workers.dev
myshopify.com azurewebsites.net web.app firebaseapp.com onrender.com fly.dev
uk.com gb.com eu.org us.com de.com za.com br.com cn.com ru.com sa.com co.com
duckdns.org ngrok-free.app trycloudflare.com nip.io sslip.io
""".split())
#: Second labels that, under a two-letter country code, mean "this is a suffix" (co.xx, com.xx, gov.xx ...).
_GENERIC_SLD = frozenset("co com org net gov edu ac or ne go gob nom mil ltd plc sch".split())


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _split_host(value: str) -> tuple[str, Optional[int]]:
    """``(host, explicit port)`` of a URL, ``host:port``, a bare host or a bare IPv6 literal (the scheme's default port is dropped)."""
    text = (value or "").strip()
    if not text:
        return "", None
    try:
        ipaddress.ip_address(text.strip("[]"))
        return text.strip("[]").lower(), None
    except ValueError:
        pass
    has_scheme = "://" in text
    try:
        parts = urlsplit(text if has_scheme or text.startswith("//") else "//" + text)
        host = parts.hostname or ""
    except ValueError:
        return "", None
    try:
        port = parts.port
    except ValueError:
        port = None
    if has_scheme and port is not None and port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        port = None
    return host.strip(".").lower(), port


def registrable_domain(value: str) -> str:
    """The lane key for a URL or host name: its registrable root domain, lowercase.

    ``https://shop.example.co.uk/x`` -> ``example.co.uk``; ``www.tienda.com`` -> ``tienda.com``; an international name and its
    punycode form are the same lane (``bücher.de`` == ``xn--bcher-kva.de``); hosting and rented suffixes (``myshopify.com``,
    ``github.io``, ``uk.com``, ``eu.org``...) keep the customer's label (``acme.myshopify.com``). An IP address, ``localhost``
    or any single-label host is its own site and, with an explicit port, one lane per port (``localhost:8080``,
    ``[::1]:9000``): two dev servers on one machine are different sites. Empty when ``value`` has no host.
    """
    host, port = _split_host(value)
    if not host:
        return ""
    try:
        ip = ipaddress.ip_address(host)
        shown = f"[{host}]" if ip.version == 6 and port else host
        return f"{shown}:{port}" if port else shown
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    labels = [p for p in host.split(".") if p]
    if len(labels) == 1:
        return labels[0] + (f":{port}" if port else "")
    if len(labels) <= 2:
        return ".".join(labels)
    last2 = ".".join(labels[-2:])
    if last2 in _PRIVATE_SUFFIX:
        return ".".join(labels[-3:])
    if last2 in _SECOND_LEVEL or (len(labels[-1]) == 2 and labels[-2] in _GENERIC_SLD):
        return ".".join(labels[-3:])
    return last2


def profile_key(path: str) -> str:
    """A browser profile folder as a comparable key (absolute, case-folded on Windows, no trailing separator)."""
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(text))).rstrip("\\/") or text


def _read_token(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _family_token_file() -> str:
    try:
        from . import family

        return str(family.status().get("token_file") or "")
    except Exception:  # noqa: BLE001 - the family module is optional in a vendored copy
        return ""


def _default_holder() -> str:
    env = os.environ.get("HOARD_APP") or ""
    if env:
        return env
    base = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    return os.path.splitext(base)[0] or "app"


# ---------------------------------------------------------------------------- the client
#: Lane keys the current thread / asyncio task already holds -> the Lane (nesting just counts). A context variable, not a
#: thread-local: tasks share their loop's thread but must not see each other's lanes. It holds an immutable dict.
_held_var: contextvars.ContextVar[dict] = contextvars.ContextVar("hoard_link_held_lanes", default={})


class Lane:
    """A browser lane, usable as ``with`` or ``async with``.

    After entering: ``acquired`` (False only when it stayed busy and ``raise_on_busy`` is off), ``via`` (``"hub"`` |
    ``"local"`` | ``"nested"``), ``domain`` (the registrable domain), ``lease_id``, ``waited_s``, and when busy ``held_by`` /
    ``since`` / ``busy`` (the hub's last answer). ``warning`` says why the local path was taken."""

    def __init__(self, domain: str = "", holder: Optional[str] = None, *, ttl_s: float = DEFAULT_TTL_S,
                 wait_s: float = DEFAULT_WAIT_S, profile: str = "", purpose: str = "", hub_url: Optional[str] = None,
                 raise_on_busy: bool = True, autostart: bool = False, track: bool = True, token_file: Optional[str] = None):
        self.domain = registrable_domain(domain)
        self.profile = profile_key(profile)
        if not self.domain and not self.profile:
            raise LaneError("a lane needs a domain (or URL) or a browser profile folder")
        self.holder = holder or _default_holder()
        self.ttl_s = max(1.0, float(ttl_s))
        self.wait_s = max(0.0, float(wait_s))
        self.purpose = purpose
        self.hub_url = _hubclient.hub_url(hub_url)
        #: The family token that says who this app is (the hub binds the lane to it): this file, else the app's own token
        #: (``hoard_link.family.configure``, ``HOARD_TOKEN_FILE``), else the hub's (``HOARD_HUB_TOKEN_FILE``).
        self.token_file = token_file
        self.raise_on_busy = bool(raise_on_busy)
        self.autostart = bool(autostart)
        #: False for a lane kept beyond one ``with`` block (a long-lived browser): it is not recorded for nesting detection.
        self.track = bool(track)
        self.lease_id: Optional[str] = None
        self.via: Optional[str] = None
        self.acquired = False
        self.busy: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        self.warning: Optional[str] = None
        self.waited_s = 0.0
        self.lost = False
        self.state = "new"
        self._keys: list[str] = self._lane_keys()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._nested = 0
        self._outer: Optional["Lane"] = None

    def __repr__(self) -> str:
        return f"<Lane {self.domain or self.profile} {self.state} via={self.via} holder={self.holder}>"

    @property
    def held_by(self) -> str:
        return str(self.busy.get("held_by") or "")

    @property
    def since(self) -> Optional[float]:
        return self.busy.get("since")

    # -- helpers ------------------------------------------------------------
    def _lane_keys(self) -> list[str]:
        keys = []
        if self.domain:
            keys.append("domain:" + self.domain)
        if self.profile:
            keys.append("profile:" + self.profile)
        return keys

    def _body(self, wait: float, ticket: Optional[str], final: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"domain": self.domain, "profile": self.profile, "holder": self.holder,
                                "purpose": self.purpose, "ttl_s": self.ttl_s, "wait_s": wait, "pid": os.getpid(),
                                "final": final}
        if ticket:
            body["ticket"] = ticket
        return body

    def _auth(self) -> dict[str, str]:
        for path in (self.token_file, _family_token_file(), os.environ.get("HOARD_TOKEN_FILE"),
                     os.environ.get("HOARD_HUB_TOKEN_FILE")):
            token = _read_token(path)
            if token:
                return {"Authorization": "Bearer " + token}
        return {}

    def _post(self, path: str, body: dict[str, Any], timeout: float = 10.0) -> tuple[Optional[int], Any]:
        return _hubclient.fetch(self.hub_url + path, body, timeout=timeout, headers=self._auth())

    def _local(self, reason: str) -> None:
        """No usable hub: proceed (the apps worked before there were lanes), say so once."""
        self.via = "local"
        self.acquired = True
        self.lease_id = None
        self.warning = f"browser lane hub not reachable at {self.hub_url} ({reason}); proceeding without a lane"
        key = self.hub_url + "|" + reason
        if key not in _warned:
            _warned.add(key)
            log.warning("%s [%s, %s]", self.warning, self.holder, self.domain or self.profile)
        else:
            log.debug("%s", self.warning)

    def _busy_error(self) -> LaneBusy:
        b = self.busy
        who = b.get("held_by") or "someone"
        since = _clock_text(b.get("since"))
        text = f"lane {self.domain or self.profile} busy: held by {who}" + (f" since {since}" if since else "")
        if b.get("purpose"):
            text += f" ({b['purpose']})"
        return LaneBusy(text, domain=self.domain, held_by=str(who), since=b.get("since"), expires_at=b.get("expires_at"),
                        purpose=str(b.get("purpose") or ""), waited_s=self.waited_s, waiting=int(b.get("waiting") or 0))

    # -- nesting (the calling thread / task already holds this lane) ----------------
    def _enter_nested(self) -> bool:
        held = _held_var.get()
        mine = [held[k] for k in self._keys if k in held]
        if mine and len(mine) == len(self._keys):          # this thread already holds every key: just count
            outer = mine[0]
            outer._nested += 1
            self._outer = outer
            self.via, self.acquired, self.state, self.lease_id = "nested", True, "granted", outer.lease_id
            return True
        if mine:
            raise LaneError("this thread already holds part of these lane keys; release it first or ask for the same keys")
        return False

    def _leave_nested(self) -> bool:
        outer = self._outer
        if outer is None:
            return False
        outer._nested = max(0, outer._nested - 1)
        self._outer = None
        self.state = "released"
        return True

    def _register(self) -> None:
        if self.track and self.via == "hub" and self.acquired:
            held = dict(_held_var.get())
            for k in self._keys:
                held[k] = self
            _held_var.set(held)

    def _unregister(self) -> None:
        held = _held_var.get()
        if any(held.get(k) is self for k in self._keys):
            _held_var.set({k: v for k, v in held.items() if v is not self})

    # -- the hub conversation (safe to run on a worker thread) -------------------
    def _acquire_remote(self) -> None:
        if self.autostart and not _hubclient.hub_up(self.hub_url):
            _hubclient.ensure_hub(self.hub_url)
        started = time.monotonic()
        ticket: Optional[str] = None
        while True:
            left = self.wait_s - (time.monotonic() - started)
            wait = max(0.0, min(POLL_WAIT_S, left))
            final = left <= POLL_WAIT_S + 0.05             # this poll covers the rest of the wait: leave the queue after it
            status, reply = self._post("/api/lane/acquire", self._body(wait, ticket, final), timeout=wait + 10.0)
            if status is None:
                return self._local("no answer")
            if status == 400 and isinstance(reply, dict):
                raise LaneError(f"hub refused the lane: {reply.get('error') or reply}")
            if status in (401, 403):
                return self._local(f"the hub refused this app's token (HTTP {status}): set HOARD_TOKEN_FILE or HOARD_HUB_TOKEN_FILE")
            if status >= 400 or not isinstance(reply, dict) or "granted" not in reply:
                return self._local(f"unexpected answer (HTTP {status})")
            self.waited_s = time.monotonic() - started
            if reply.get("granted"):
                self.info = reply
                self.lease_id = reply.get("lease_id")
                self.via, self.acquired, self.state = "hub", True, "granted"
                self._start_renewer()
                return
            self.busy = reply
            ticket = reply.get("ticket") or ticket
            if self.wait_s - (time.monotonic() - started) <= 0.05:
                self.via, self.acquired, self.state = "hub", False, "busy"
                if self.raise_on_busy:
                    raise self._busy_error()
                return

    def _start_renewer(self) -> None:
        self._stop.clear()
        every = max(1.0, min(self.ttl_s / 3.0, 120.0))

        def loop() -> None:
            while not self._stop.wait(every):
                status, reply = self._post("/api/lane/renew", {"lease_id": self.lease_id, "holder": self.holder,
                                                                "ttl_s": self.ttl_s}, timeout=5.0)
                if status in (401, 403, 404):
                    self.lost = True
                    log.warning("browser lane %s was lost while in use (%s)", self.domain or self.profile, self.holder)
                    return
                if isinstance(reply, dict) and reply.get("ok"):
                    self.info = reply

        self._thread = threading.Thread(target=loop, name=f"hoard-lane-{self.lease_id}", daemon=True)
        self._thread.start()

    def _release_remote(self) -> None:
        self._stop.set()
        if self.via == "hub" and self.acquired and self.lease_id:
            if self._nested:
                log.debug("lane %s released with %s nested use(s) still open", self.domain, self._nested)
            for attempt in range(RELEASE_TRIES):          # a lane left behind blocks the site until its ttl runs out
                status, _ = self._post("/api/lane/release", {"lease_id": self.lease_id, "holder": self.holder}, timeout=5.0)
                if status is not None and status < 500:
                    break
                if attempt + 1 < RELEASE_TRIES:
                    time.sleep(0.3 * (attempt + 1))
            else:
                log.warning("browser lane %s could not be released (%s); it frees itself when its ttl runs out",
                            self.domain or self.profile, self.holder)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.state = "released"

    # -- sync ---------------------------------------------------------------
    def acquire(self) -> "Lane":
        if self._enter_nested():
            return self
        self._acquire_remote()
        self._register()
        return self

    def release(self) -> None:
        if self._leave_nested() or self.state == "released":
            return
        self._unregister()
        self._release_remote()

    def __enter__(self) -> "Lane":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()

    # -- async: the hub conversation on a worker thread, the bookkeeping on the caller's task ----------
    async def __aenter__(self) -> "Lane":
        if self._enter_nested():
            return self
        await asyncio.to_thread(self._acquire_remote)
        self._register()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._leave_nested() or self.state == "released":
            return
        self._unregister()
        await asyncio.to_thread(self._release_remote)


def lane(domain: str = "", holder: Optional[str] = None, *, ttl_s: float = DEFAULT_TTL_S, wait_s: float = DEFAULT_WAIT_S,
         profile: str = "", purpose: str = "", hub_url: Optional[str] = None, raise_on_busy: bool = True,
         autostart: bool = False, track: bool = True, token_file: Optional[str] = None) -> Lane:
    """A :class:`Lane` to use with ``with`` or ``async with`` (see the module docstring). ``track=False`` is for a lane
    that outlives the block that took it (call ``acquire()`` / ``release()`` yourself)."""
    return Lane(domain, holder, ttl_s=ttl_s, wait_s=wait_s, profile=profile, purpose=purpose, hub_url=hub_url,
                raise_on_busy=raise_on_busy, autostart=autostart, track=track, token_file=token_file)


def lane_status(hub_url: Optional[str] = None, timeout: float = 3.0) -> Optional[dict[str, Any]]:
    """The hub's list of active lanes, or None when no hub answers."""
    status, body = _hubclient.fetch(_hubclient.hub_url(hub_url) + "/api/lane", timeout=timeout)
    return body if status == 200 and isinstance(body, dict) and "lanes" in body else None
