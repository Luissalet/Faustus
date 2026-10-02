"""Tell the person through the hub — from inside an app.

The hub (facet ``notify``) decides how the person is told: Windows toast, ntfy, Telegram or mail,
by the priority and the sphere, with quiet hours, duplicate and rate limits. An app calls
:func:`notify` instead of carrying its own channel code, and keeps that code only as the fallback
for when the hub is unreachable (``notify`` then answers ``{"ok": False, "error": "hub unreachable"}``)::

    from .hoard_link import fam_notify
    res = fam_notify.notify("Payment failed", "Netflix 12.99 EUR", priority="high", url=url,
                            group="payment", dedupe_key=f"pay:{tx_id}")
    if not res.get("ok") and res.get("error") == "hub unreachable":
        own_toast(...)          # the app's own channel

Most apps want the whole decision, not the two halves: :class:`Router` is the ``auto | hub | own`` switch every app used to
re-implement (a ``notify.via`` setting), with the app's old channel code as ``own_send``::

    router = fam_notify.Router(lambda: settings.get("notify.via", "auto"), own_send, app_name="Tantalus")
    res = router.send("Price dropped", "Kindle 79 EUR", priority="high", url=url, group="price", dedupe_key=f"p:{wid}")
    # {"via": "hub" | "own", "ok": True, "why": "" | "held: quiet" | "<error>"}

Standard library only; configure the app first (``family.configure`` or ``family.install_fastapi``)
so the request carries the app's own token.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from . import family as _f
from ._hubclient import fetch

CACHE_S = 30.0
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def notify(title: str, body: str = "", *, priority: str = "normal", url: str = "", group: str = "",
           dedupe_key: str = "", sphere: Optional[str] = None, timeout: float = 5.0) -> dict[str, Any]:
    """Ask the hub to notify the person (blocking POST). Returns the hub's answer
    (``{ok, id, held, channels, delivered, ...}``; ``held`` says why nothing was pushed), or
    ``{"ok": False, "error": "hub unreachable"}`` when nothing answered."""
    payload: dict[str, Any] = {"title": str(title or ""), "body": str(body or ""), "priority": str(priority or "normal")}
    for key, value in (("url", url), ("group", group), ("dedupe_key", dedupe_key), ("sphere", sphere)):
        if value:
            payload[key] = str(value)
    status, data = _f._post("/api/notify", payload, timeout)
    if status is None:
        with _lock:
            _cache.pop(_f._hub(), None)
        return {"ok": False, "error": "hub unreachable"}
    if isinstance(data, dict):
        data.setdefault("ok", 200 <= status < 300)
        if status == 401:
            data["error"] = "the hub refused this app's token (" + (_f.status().get("token_file") or "no token file") + ")"
        data.setdefault("status", status)
        return data
    return {"ok": 200 <= status < 300, "status": status, **({} if 200 <= status < 300 else {"error": f"HTTP {status}"})}


def hub_available(timeout: float = 1.0) -> bool:
    """True when a hub with the notification facet answers. Cached for 30 seconds."""
    base = _f._hub()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(base)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    status, data = fetch(base + "/api/notify?limit=1", timeout=timeout)
    ok = status == 200 and isinstance(data, dict) and data.get("ok") is True
    with _lock:
        _cache[base] = (now, ok)
    return ok


def _reset_cache() -> None:
    with _lock:
        _cache.clear()


VIA_MODES = ("auto", "hub", "own")


class Router:
    """The ``auto | hub | own`` switch in front of the hub's notification facet.

    ``via_getter()`` returns the person's setting (``auto``, ``hub`` or ``own``; anything else is ``auto``; a getter that raises
    is ``auto``). ``own_send(title, body, *, priority, url, group, dedupe_key, sphere)`` is the app's own delivery (its toast, ntfy,
    Telegram or mail code, built on :mod:`hoard_link.notify_channels`); it may return a dict with ``ok`` / ``error``, a bool, an
    error string, or nothing (= done).

    * ``own``  : only ``own_send``; the hub is not called.
    * ``hub``  : only the hub; when it does not answer the failure is reported, not hidden.
    * ``auto`` : the hub when it answers, ``own_send`` when it does not (or refuses the call). A message the hub *held* on purpose
      (quiet hours, a duplicate, the digest, a rate limit, notifications switched off) counts as delivered and is NOT repeated
      through the app's own channels: that would defeat the person's rules.

    ``hub`` is the object with ``notify()`` and ``hub_available()`` (default: this module; tests inject a fake).
    """

    def __init__(self, via_getter: Optional[Callable[[], Any]] = None, own_send: Optional[Callable[..., Any]] = None,
                 app_name: str = "", *, hub: Any = None):
        self.via_getter = via_getter
        self.own_send = own_send
        self.app_name = str(app_name or "")
        self._hub = hub

    @property
    def hub(self) -> Any:
        if self._hub is None:
            import sys
            return sys.modules[__name__]
        return self._hub

    def via_setting(self) -> str:
        try:
            value = str(self.via_getter() if self.via_getter else "auto").strip().lower()
        except Exception:  # noqa: BLE001
            return "auto"
        return value if value in VIA_MODES else "auto"

    def hub_up(self) -> bool:
        try:
            return bool(self.hub.hub_available())
        except Exception:  # noqa: BLE001
            return False

    def via_status(self) -> dict[str, Any]:
        """What the next message would use: ``{setting, effective: "hub" | "own", hub_available}``."""
        setting = self.via_setting()
        up = self.hub_up() if setting != "own" else False
        return {"setting": setting, "effective": "hub" if (setting == "hub" or (setting == "auto" and up)) else "own", "hub_available": up}

    def send(self, title: str, body: str = "", *, priority: str = "normal", url: Optional[str] = None, group: Optional[str] = None,
             dedupe_key: Optional[str] = None, sphere: Optional[str] = None) -> dict[str, Any]:
        """Deliver one notification. Never raises. ``{"via": "hub" | "own", "ok": bool, "why": str}``; with the hub also ``held``
        (why it kept the message back, if it did), ``id`` and ``hub`` (the hub's whole answer)."""
        via = self.via_setting()
        if via != "own" and (via == "hub" or self.hub_up()):
            res = self._via_hub(title, body, priority, url, group, dedupe_key, sphere)
            if res["ok"] or via == "hub":
                return res
        return self._via_own(title, body, priority, url, group, dedupe_key, sphere)

    def _via_hub(self, title, body, priority, url, group, dedupe_key, sphere) -> dict[str, Any]:
        try:
            answer = self.hub.notify(title, body, priority=priority or "normal", url=url or "", group=group or "",
                                     dedupe_key=dedupe_key or "", sphere=sphere or None)
        except Exception as exc:  # noqa: BLE001
            return {"via": "hub", "ok": False, "why": f"hub notify: {type(exc).__name__}"}
        if not isinstance(answer, dict):
            return {"via": "hub", "ok": False, "why": "hub notify: unexpected answer"}
        if answer.get("ok"):
            held = str(answer.get("held") or "")
            return {"via": "hub", "ok": True, "why": f"held: {held}" if held else "", "held": held, "id": answer.get("id"), "hub": answer}
        return {"via": "hub", "ok": False, "held": "", "hub": answer,
                "why": str(answer.get("error") or f"hub notify failed ({answer.get('status')})")[:200]}

    def _via_own(self, title, body, priority, url, group, dedupe_key, sphere) -> dict[str, Any]:
        if self.own_send is None:
            return {"via": "own", "ok": False, "why": "no own channel configured"}
        try:
            raw = self.own_send(title, body, priority=priority or "normal", url=url, group=group, dedupe_key=dedupe_key, sphere=sphere)
        except Exception as exc:  # noqa: BLE001 - a notification must never raise out of the app's engine
            return {"via": "own", "ok": False, "why": type(exc).__name__}
        if isinstance(raw, dict):
            ok = bool(raw.get("ok", not raw.get("error")))
            return {"via": "own", "ok": ok, "why": "" if ok else str(raw.get("error") or "failed")[:200], "own": raw}
        if isinstance(raw, str):
            return {"via": "own", "ok": not raw, "why": raw[:200]}
        if raw is False:
            return {"via": "own", "ok": False, "why": "failed"}
        return {"via": "own", "ok": True, "why": ""}
