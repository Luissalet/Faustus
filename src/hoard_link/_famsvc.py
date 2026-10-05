"""Plumbing shared by the family service clients ``fam_media``, ``fam_docs`` and ``fam_embed``.

Not a public module: apps import the ``fam_*`` clients. What lives here is how a call to an owner app *through the hub*
is made, classified and polled, so the three clients agree on one vocabulary:

* :func:`call_tool` runs ``family.call(owner, tool, args)`` and folds the hub's proxy answer into
  ``{"ok", "data", "error", "kind", "status", "ms"}``. ``kind`` says what went wrong: ``hub_down`` (nothing answered at
  the hub), ``app_down`` (the hub is up, the owner app is not), ``app_missing`` (the hub does not know the owner),
  ``tool_missing`` (an older owner without the tool), ``timeout``, ``auth`` (the hub refused this app's token) or
  ``tool_error`` (the tool ran and refused or failed: a bad path, a corrupt file...). The first four are
  :data:`UNAVAILABLE`: nobody there to do the work, so a client with a local fallback may use it; the others are real
  answers and are reported as they are.
* :func:`run_job` is the long-work rule of :mod:`hoard_link.waiting`: the first call waits at most
  :data:`~hoard_link.waiting.MAX_WAIT_S` (150 s, under the 180 s an MCP client allows), and while the job is still running
  the status tool is polled with the same cap until the caller's own deadline.
* :func:`available` says whether the owner of a service is running (``GET /api/apps/<owner>``), cached for 30 seconds.

Standard library only. Nothing here raises.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional

from . import family as _f
from ._hubclient import fetch
from .waiting import DONE_STATES, MAX_WAIT_S, clamp_wait
from .service_contracts import OWNERS

#: service name -> the app that owns it (docs/commons/services.md)

#: the hub accepts at most this per call (``POST /api/apps/<id>/call``, ``timeout_s``)
HUB_MAX_S = 900.0
#: extra seconds the hub call gets over the wait it asks the tool for, so a tool that answers at its own deadline is heard
HUB_MARGIN_S = 20.0
CACHE_S = 30.0

#: nobody there to do the work: a client with a local fallback may use it
UNAVAILABLE = frozenset({"hub_down", "app_down", "app_missing", "tool_missing"})

_cache: dict[tuple[str, str, str], tuple[float, bool]] = {}
_lock = threading.Lock()


def owner_of(service: str) -> str:
    return OWNERS.get(str(service or "").strip().lower(), str(service or "").strip())


def hub_timeout(seconds: Any) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 120.0
    if value != value:
        value = 120.0
    return max(1.0, min(value, HUB_MAX_S))


def fail(kind: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "error": error, "data": None, "status": extra.pop("status", None), **extra}


def is_unavailable(res: Mapping[str, Any]) -> bool:
    return not res.get("ok") and res.get("kind") in UNAVAILABLE


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("error", "message", "detail"):
            inner = value.get(key)
            if inner:
                return _text(inner)
    return ""


def _unknown_tool(low: str) -> bool:
    """Is a 404's message "this app has no such tool" (rather than the tool's own "nothing found")? Same reading as the hub's proxy."""
    return ("tool" in low and any(w in low for w in ("unknown", "not found", "no existe", "desconoc", "no such"))) or low.strip(" .") in ("not found", "http 404", "")


def call_tool(owner: str, tool: str, args: Optional[Mapping[str, Any]] = None, *, timeout_s: float = 120.0) -> dict[str, Any]:
    """One tool of ``owner`` through the hub, classified (see the module docstring). ``data`` is the tool's own result when
    ``ok``. ``timeout_s`` is how long the hub may take (1 to 900)."""
    timeout = hub_timeout(timeout_s)
    t0 = time.monotonic()
    try:
        raw = _f.call(owner, tool, dict(args or {}), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the contract: never raise
        return fail("hub_down", "hub unreachable", detail=f"{type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - t0
    if not isinstance(raw, Mapping):
        return fail("tool_error", "unexpected answer from the hub")
    status = raw.get("status")
    ms = raw.get("ms")
    error = _text(raw.get("error"))
    if status is None and not raw.get("ok"):
        if elapsed >= timeout * 0.95:
            return fail("timeout", f"timeout after {timeout:.0f}s", ms=ms)
        if "hub not reachable" in error or "contract" not in raw:
            return fail("hub_down", "hub unreachable", detail=error)
        return fail("app_down", f"{owner} unreachable", detail=error, ms=ms)
    if not raw.get("ok"):
        low = error.lower()
        if status == 401:
            return fail("auth", error or "the hub refused this app's token", status=401, ms=ms)
        if status == 404 and "unknown app" in low:
            return fail("app_missing", f"{owner} is not registered in the hub", status=404, ms=ms)
        if status == 404 and _unknown_tool(low):
            return fail("tool_missing", f"{owner} has no tool {tool} (update the app)", status=404, detail=error, ms=ms)
        return fail("tool_error", error or f"HTTP {status}", status=status, ms=ms)
    data = raw.get("result")
    if isinstance(data, Mapping) and data.get("ok") is False and "status" not in data:      # a job view says ok: false while it runs
        return fail("tool_error", _text(data) or "the tool failed", status=status, data=dict(data), ms=ms)
    return {"ok": True, "kind": "", "error": "", "data": data, "status": status, "ms": ms}


def public_error(res: Mapping[str, Any], via: str) -> dict[str, Any]:
    """The failure shape callers see: ``{"ok": False, "error", "via", "kind"}`` (+ ``detail`` / ``data`` when there is one)."""
    out: dict[str, Any] = {"ok": False, "error": str(res.get("error") or "failed"), "via": via, "kind": str(res.get("kind") or "tool_error")}
    if res.get("detail") and res.get("kind") not in ("hub_down", "app_down"):
        out["detail"] = res["detail"]
    if isinstance(res.get("data"), Mapping):
        out["result"] = dict(res["data"])
    return out


def never_raises(via: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Decorator: an exception escaping a client function becomes ``{"ok": False, "error": ..., "via": via}``."""
    def wrap(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "via": via, "kind": "client_error"}
        return inner
    return wrap


def as_path(value: Any) -> str:
    """An absolute, string path from a ``str`` / ``PathLike`` (the owners need absolute paths on the shared disk)."""
    text = os.fspath(value) if not isinstance(value, str) else value
    return os.path.abspath(text) if text else ""


def clean_args(**kw: Any) -> dict[str, Any]:
    """The arguments of a tool call without the ``None``, empty-string and empty-list ones (the owner's defaults apply)."""
    return {k: v for k, v in kw.items() if v is not None and v != "" and v != []}


def default_done(data: Any) -> bool:
    status = data.get("status") if isinstance(data, Mapping) else None
    return status is None or str(status).lower() in DONE_STATES


def fraction(value: Any) -> Optional[float]:
    """A progress value as 0..1 (a percentage above 1 is divided by 100); ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return max(0.0, min(1.0, float(value) / 100.0 if value > 1 else float(value)))


def run_job(owner: str, start_tool: str, start_args: Mapping[str, Any], status_tool: str, *, timeout_s: float,
            id_field: str = "job_id", id_arg: str = "job_id", wait_arg: str = "wait_s", status_wait_arg: str = "",
            min_wait: float = 0.0,
            is_done: Callable[[Any], bool] = default_done, on_data: Optional[Callable[[Mapping[str, Any]], None]] = None,
            ) -> dict[str, Any]:
    """Start a long job and follow it. Returns the final :func:`call_tool` answer; when the caller's ``timeout_s`` runs out
    first, ``{"ok": False, "kind": "timeout", "data": <the job as last seen>, "job_id": ...}`` (the job keeps running).
    ``wait_arg`` / ``status_wait_arg`` name the argument that makes the start / status tool wait (default ``wait_s`` for both);
    ``min_wait`` is the shortest wait the owner accepts (Links' ``media_download`` takes 5 s at least)."""
    t0 = time.monotonic()
    deadline = t0 + max(1.0, float(timeout_s))
    chunk = max(clamp_wait(min(MAX_WAIT_S, timeout_s)), min_wait)
    res = call_tool(owner, start_tool, {**dict(start_args), wait_arg: chunk},
                    timeout_s=chunk + HUB_MARGIN_S)
    while True:
        if not res["ok"]:
            return res
        data = res["data"]
        if isinstance(data, Mapping) and on_data is not None:
            try:
                on_data(data)
            except Exception:  # noqa: BLE001 - a progress callback must not break the job
                pass
        if is_done(data):
            return res
        job_id = data.get(id_field) if isinstance(data, Mapping) else None
        remaining = deadline - time.monotonic()
        if not job_id or remaining <= 0.5:
            return {**fail("timeout", f"still running after {timeout_s:.0f}s", status=res.get("status")),
                    "data": dict(data) if isinstance(data, Mapping) else None, "job_id": job_id}
        chunk = max(clamp_wait(min(MAX_WAIT_S, remaining)), min_wait)
        polled = time.monotonic()
        res = call_tool(owner, status_tool, {id_arg: job_id, (status_wait_arg or wait_arg): chunk}, timeout_s=chunk + HUB_MARGIN_S)
        if res["ok"] and not is_done(res["data"]) and time.monotonic() - polled < 0.2:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))      # an owner that ignores wait_s must not be hammered


# ---------------------------------------------------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------------------------------------------------

def available(service: str, timeout: float = 1.0) -> bool:
    """True when the hub answers and the app that owns ``service`` (``media``, ``stt``, ``tts``, ``docs``, ``embed``, or an
    app id) is running. Cached for 30 seconds; a failed probe is cached too, so a loop does not retry every call."""
    owner = owner_of(service)
    key = (_f._hub(), owner, _f._token())
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    status, body = fetch(_f._hub() + "/api/apps/" + owner, timeout=timeout, headers=_f._headers())
    ok = status == 200 and isinstance(body, dict) and body.get("state") == "running"
    with _lock:
        _cache[key] = (now, ok)
    return ok


def forget_availability() -> None:
    """Drop the 30-second cache (after changing the hub, or in tests)."""
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------------------------------------------------
# the local fallback through hoard_link.Link (needs httpx; imported only when a fallback runs)
# ---------------------------------------------------------------------------------------------------------------------

_link_cache: dict[str, Any] = {}


def default_link() -> Any:
    """A :class:`hoard_link.Link` for this app, built once (raises when ``httpx`` is not installed)."""
    with _lock:
        link = _link_cache.get("link")
        if link is None:
            try:
                from . import Link, LinkConfig       # lazy: needs httpx
            except ImportError:
                from .errors import missing_dependency
                raise missing_dependency("httpx", "the local model fallback") from None
            link = _link_cache["link"] = Link(LinkConfig.load(None, app=str(_f.status().get("app") or "app")))
        return link


def run_link(link: Any, method: str, *args: Any) -> Any:
    """``link.sync.<method>(...)`` (a :class:`~hoard_link.Link`), or ``link.<method>`` run to completion when it is a coroutine."""
    sync = getattr(link, "sync", None)
    fn = getattr(sync, method, None) if sync is not None else None
    if fn is not None:
        return fn(*args)
    result = getattr(link, method)(*args)
    if hasattr(result, "__await__"):
        import asyncio
        return asyncio.run(result)
    return result
