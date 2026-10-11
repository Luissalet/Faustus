"""Ask the hub for GPU memory before loading a model: :func:`lease`.

::

    from hoard_link import lease

    with lease(vram_mb=6000, purpose="whisper large-v3", owner="scribe") as l:
        model = load_on(l.gpu)        # l.gpu: the GPU index to use, or None
        ...                           # the lease is renewed in the background
                                      # and released on exit

    async with lease(vram_mb=20000, purpose="video render", owner="daguerre", priority=1) as l:
        ...

    with lease(vram_mb=12000, purpose="render", gpu=[2, 3]) as l:   # any of GPUs 2 or 3
        ...

The hub (``python -m hoard_link.hub``, port 8810) keeps one queue for the
whole machine, counting both what ``nvidia-smi`` reports and what it has
already promised to others, so two apps can no longer see the same free
gigabytes at the same moment and both load. Entering the context manager
waits until the hub grants the request (``timeout_s`` bounds the wait and
raises :class:`LeaseTimeout`; ``None`` waits as long as it takes).

Apps must keep working without the hub. When no hub answers and none can
be started (auto-start is the same headless start the MCP bridge uses,
off with ``HOARD_HUB_AUTOSTART=0``), the lease falls back to the old local
check: it reads ``gpu_free_mb()``, picks a GPU that has the room when
there is one, logs a warning, and lets the caller proceed. ``l.via`` says
which path was taken (``"hub"`` or ``"local"``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

from . import _hubclient
from .errors import HoardLinkError
from .gpu import gpu_free_mb

log = logging.getLogger("hoard_link.lease")

DEFAULT_TTL_S = 1800
POLL_WAIT_S = 25.0
_warned: set[str] = set()


class LeaseError(HoardLinkError, ValueError):
    """The hub refused the request itself (e.g. more VRAM than any GPU has)."""


class LeaseTimeout(HoardLinkError, TimeoutError):
    """The lease was still queued when ``timeout_s`` ran out."""

    def __init__(self, message: str, position: Optional[int] = None):
        self.position = position
        super().__init__(message)


def _wanted_gpus(request: Any) -> Optional[list[int]]:
    """The GPU indices a request restricts the lease to (``gpu=2``, ``gpu=[2, 3]``,
    ``"2,3"``), or None for "any"."""
    if request is None or isinstance(request, bool):
        return None
    if isinstance(request, str):
        text = request.strip().lower()
        if text in ("", "any", "auto"):
            return None
        request = [p for p in text.replace(";", ",").split(",")]
    elif isinstance(request, int):
        request = [request]
    out: list[int] = []
    for item in request if isinstance(request, (list, tuple, set, frozenset)) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out or None


def _default_owner() -> str:
    env = os.environ.get("HOARD_APP") or ""
    if env:
        return env
    base = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    return os.path.splitext(base)[0] or "app"


class Lease:
    """A GPU memory lease, usable as ``with`` or ``async with``.

    After entering: ``gpu`` (index or None), ``lease_id`` (hub path only),
    ``via`` (``"hub"`` | ``"local"``), ``state`` (``granted`` | ``local`` |
    ``released``), ``info`` (the hub's last answer) and ``warning`` (why
    the local fallback was used)."""

    def __init__(
        self,
        vram_mb: int,
        purpose: str = "",
        owner: Optional[str] = None,
        gpu: Any = None,
        priority: int = 0,
        timeout_s: Optional[float] = None,
        hub_url: Optional[str] = None,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        autostart: Optional[bool] = None,
        client: Any = None,
    ):
        self.vram_mb = int(vram_mb)
        self.purpose = purpose
        self.owner = owner or _default_owner()
        self.gpu_request = gpu
        self.priority = int(priority)
        self.timeout_s = timeout_s
        self.hub_url = _hubclient.hub_url(hub_url)
        self.ttl_s = int(ttl_s)
        self.autostart = _hubclient.autostart_enabled() if autostart is None else bool(autostart)
        self._client = client              # an httpx.AsyncClient for the async path (optional)
        self._own_client = False
        self.lease_id: Optional[str] = None
        self.gpu: Optional[int] = None
        self.via: Optional[str] = None
        self.state = "new"
        self.info: dict[str, Any] = {}
        self.warning: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None

    def __repr__(self) -> str:
        return f"<Lease {self.lease_id or '-'} {self.state} via={self.via} gpu={self.gpu} {self.vram_mb}MiB>"

    # -- shared -----------------------------------------------------------
    def _body(self) -> dict[str, Any]:
        # The first request never long-polls: the client learns its lease_id
        # at once, so it can always leave the queue (timeout, cancellation).
        return {"owner": self.owner, "purpose": self.purpose, "vram_mb": self.vram_mb,
                "gpu": "any" if self.gpu_request is None else self.gpu_request, "priority": self.priority,
                "ttl_s": self.ttl_s, "wait": False, "pid": os.getpid()}

    def _renew_every(self) -> float:
        return max(2.0, min(self.ttl_s / 3.0, 300.0))

    def _remaining(self, started: float) -> Optional[float]:
        if self.timeout_s is None:
            return None
        return float(self.timeout_s) - (time.monotonic() - started)

    def _apply(self, body: dict[str, Any]) -> None:
        self.info = body
        self.lease_id = body.get("lease_id") or self.lease_id
        self.state = body.get("state") or self.state
        self.gpu = body.get("gpu")

    def _fallback(self, reason: str) -> None:
        """No hub: the pre-hub behaviour — look at free VRAM, warn, go on."""
        self.via = "local"
        self.state = "local"
        self.lease_id = None
        gpus = gpu_free_mb()
        wanted = _wanted_gpus(self.gpu_request)
        pool = gpus if wanted is None else [g for g in gpus if g.index in wanted]
        # A single named GPU is used as asked even when it is not listed; with several, the roomiest wins.
        named = wanted[0] if wanted is not None and len(wanted) == 1 else None
        fits = [g for g in pool if g.free_mb >= self.vram_mb]
        if fits:
            self.gpu = max(fits, key=lambda g: g.free_mb).index
            detail = f"GPU {self.gpu} has {max(g.free_mb for g in fits)} MiB free"
        elif pool:
            self.gpu = named if named is not None else (
                max(pool, key=lambda g: g.free_mb).index if wanted is not None else None)
            detail = f"no GPU has {self.vram_mb} MiB free (best: {max(g.free_mb for g in pool)} MiB); proceeding anyway"
        else:
            self.gpu = named
            detail = "no GPU inventory (nvidia-smi not found)"
        self.warning = f"GPU lease hub not reachable at {self.hub_url} ({reason}); local check only: {detail}"
        key = self.hub_url + "|" + reason
        if key not in _warned:
            _warned.add(key)
            log.warning("%s [%s, %s]", self.warning, self.owner, self.purpose or "no purpose")
        else:
            log.debug("%s", self.warning)

    def _check(self, status: Optional[int], body: Any) -> Optional[str]:
        """None when ``body`` is a usable lease answer; else why not.
        Raises :class:`LeaseError` for a request the hub rejected."""
        if status is None:
            return "no answer"
        if status == 400 and isinstance(body, dict):
            raise LeaseError(f"hub refused the lease: {body.get('error') or body}")
        if status == 404 and isinstance(body, dict) and body.get("lease_id"):
            return "lost"
        if status >= 400 or not isinstance(body, dict) or not body.get("lease_id"):
            return f"unexpected answer (HTTP {status})"
        return None

    # -- sync ---------------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any], timeout: float = 10.0) -> tuple[Optional[int], Any]:
        return _hubclient.fetch(self.hub_url + path, body, timeout=timeout)

    def acquire(self) -> "Lease":
        up = _hubclient.hub_up(self.hub_url)
        if not up and self.autostart:
            up = _hubclient.ensure_hub(self.hub_url)
        if not up:
            self._fallback("no hub")
            return self
        started = time.monotonic()
        body = self._body()
        while True:
            left = self._remaining(started)
            if left is not None and body.get("wait"):
                body["wait_s"] = max(0.0, min(POLL_WAIT_S, left))
            status, reply = self._post("/api/lease/request", body, timeout=POLL_WAIT_S + 10)
            problem = self._check(status, reply)
            if problem == "lost":
                body = self._body()          # reaped while queued: ask again from scratch
                continue
            if problem:
                self._fallback(problem)
                return self
            self._apply(reply)
            if self.state == "granted":
                self.via = "hub"
                self._start_renewer_thread()
                return self
            left = self._remaining(started)
            if left is not None and left <= 0:
                position = reply.get("position")
                self.release()
                raise LeaseTimeout(f"GPU lease for {self.vram_mb} MiB still queued (position {position}) "
                                   f"after {self.timeout_s}s", position)
            body = {"lease_id": self.lease_id, "wait": True}

    def _start_renewer_thread(self) -> None:
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self._renew_every()):
                status, reply = self._post("/api/lease/renew", {"lease_id": self.lease_id, "ttl_s": self.ttl_s})
                if status == 404:
                    log.warning("GPU lease %s was reaped by the hub while in use (%s)", self.lease_id, self.purpose)
                    return
                if isinstance(reply, dict) and reply.get("ok"):
                    self.info = reply

        self._thread = threading.Thread(target=loop, name=f"hoard-lease-{self.lease_id}", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self.lease_id and self.via != "local":
            self._post("/api/lease/release", {"lease_id": self.lease_id}, timeout=5.0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.state = "released"

    def __enter__(self) -> "Lease":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()

    # -- async --------------------------------------------------------------
    async def _apost(self, path: str, body: Optional[dict[str, Any]], timeout: float = 10.0) -> tuple[Optional[int], Any]:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(trust_env=False)
            self._own_client = True
        try:
            if body is None:
                resp = await self._client.get(self.hub_url + path, timeout=timeout)
            else:
                resp = await self._client.post(self.hub_url + path, json=body, timeout=timeout)
        except Exception:  # noqa: BLE001  (refused, timed out, mock not registered...)
            return None, None
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    async def _ahub_up(self) -> bool:
        status, body = await self._apost("/api/health", None, timeout=1.5)
        return status == 200 and _hubclient.is_hub(body)

    async def aacquire(self) -> "Lease":
        up = await self._ahub_up()
        if not up and self.autostart:
            up = await asyncio.to_thread(_hubclient.ensure_hub, self.hub_url) and await self._ahub_up()
        if not up:
            await asyncio.to_thread(self._fallback, "no hub")   # nvidia-smi is a blocking subprocess
            return self
        started = time.monotonic()
        body = self._body()
        pending: Optional["asyncio.Future[Any]"] = None
        try:
            while True:
                left = self._remaining(started)
                if left is not None and body.get("wait"):
                    body["wait_s"] = max(0.0, min(POLL_WAIT_S, left))
                pending = asyncio.ensure_future(self._apost("/api/lease/request", body, timeout=POLL_WAIT_S + 10))
                status, reply = await asyncio.shield(pending)
                problem = self._check(status, reply)
                if problem == "lost":
                    body = self._body()
                    continue
                if problem:
                    await asyncio.to_thread(self._fallback, problem)
                    return self
                self._apply(reply)
                if self.state == "granted":
                    self.via = "hub"
                    self._task = asyncio.ensure_future(self._arenew_loop())
                    return self
                left = self._remaining(started)
                if left is not None and left <= 0:
                    position = reply.get("position")
                    await self.arelease()
                    raise LeaseTimeout(f"GPU lease for {self.vram_mb} MiB still queued (position {position}) "
                                       f"after {self.timeout_s}s", position)
                body = {"lease_id": self.lease_id, "wait": True}
        except asyncio.CancelledError:
            # Cancelled while queued: leave the queue (fire and forget, the
            # loop may be going away). If the cancellation landed while the
            # first request was still in flight, the hub may already have
            # queued us without the reply having reached this client yet:
            # let that reply arrive (briefly) so the lease can be released.
            if not self.lease_id and pending is not None and not pending.done():
                try:
                    status, reply = await asyncio.wait_for(pending, timeout=5.0)
                    if isinstance(reply, dict) and reply.get("lease_id"):
                        self.lease_id = reply["lease_id"]
                except BaseException:  # noqa: BLE001  (second cancel, timeout, transport error)
                    pass
            if self.lease_id:
                lid = self.lease_id
                threading.Thread(target=self._post, args=("/api/lease/release", {"lease_id": lid}),
                                 daemon=True).start()
            raise

    async def _arenew_loop(self) -> None:
        while True:
            await asyncio.sleep(self._renew_every())
            status, reply = await self._apost("/api/lease/renew", {"lease_id": self.lease_id, "ttl_s": self.ttl_s})
            if status == 404:
                log.warning("GPU lease %s was reaped by the hub while in use (%s)", self.lease_id, self.purpose)
                return
            if isinstance(reply, dict) and reply.get("ok"):
                self.info = reply

    async def arelease(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.lease_id and self.via != "local":
            await self._apost("/api/lease/release", {"lease_id": self.lease_id}, timeout=5.0)
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._own_client = False
        self.state = "released"

    async def __aenter__(self) -> "Lease":
        return await self.aacquire()

    async def __aexit__(self, *exc: Any) -> None:
        await self.arelease()


def lease(
    vram_mb: int,
    purpose: str = "",
    owner: Optional[str] = None,
    gpu: Any = None,
    priority: int = 0,
    timeout_s: Optional[float] = None,
    hub_url: Optional[str] = None,
    **kwargs: Any,
) -> Lease:
    """A :class:`Lease` to use with ``with`` or ``async with``."""
    return Lease(vram_mb, purpose, owner, gpu, priority, timeout_s, hub_url, **kwargs)
