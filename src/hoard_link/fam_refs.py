"""References between apps, from inside an app — the client of the hub's ``refs`` facet.

An app that put ``hoard://<app>/<kind>/<id>`` in a record (see :mod:`hoard_link.artifacts`) can tell
the hub "this record is the same thing as that one" and later ask "what is connected to this?"::

    from .hoard_link import fam_refs
    fam_refs.link("hoard://ledger/tx/12", "hoard://kafka/document/7", "purchase",
                  from_label="Amazon 23.90 EUR", to_label="Invoice 2026-114")
    fam_refs.around("hoard://ledger/tx/12")     # {"ok": True, "nodes": [...], "edges": [...]}

Both calls are blocking and short (5 s) and never raise: when the hub is not there they answer
``{"ok": False, "error": "hub unreachable"}`` and the app carries on — links are hints, the app's own
database is the truth. The hub only accepts a link when one end is a record of the calling app (its own
bearer token says who it is), so :func:`hoard_link.family.configure` must have run. Standard library only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import family as _f
from ._hubclient import fetch

UNREACHABLE = {"ok": False, "error": "hub unreachable"}


def _answer(status: Any, body: Any) -> dict[str, Any]:
    if status is None:
        return dict(UNREACHABLE)
    if isinstance(body, dict):
        body.setdefault("ok", 200 <= status < 300)
        return body
    return {"ok": 200 <= status < 300, "status": status}


def link(from_uri: str, to_uri: str, rel: str = "related", *, from_label: str = "", to_label: str = "",
         note: str = "", timeout: float = 5.0) -> dict[str, Any]:
    """Record ``from_uri --rel--> to_uri`` in the hub. Idempotent. Returns the hub's answer
    (``{ok, created, edge}``) or ``{"ok": False, "error": ...}``."""
    try:
        status, body = _f._post("/api/refs", {"from": from_uri, "to": to_uri, "rel": rel, "from_label": from_label,
                                              "to_label": to_label, "note": note}, timeout)
    except Exception as exc:  # noqa: BLE001 - never raise into the app
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return _answer(status, body)


def around(uri: str, depth: int = 1, timeout: float = 5.0) -> dict[str, Any]:
    """The records linked to ``uri`` within ``depth`` hops: ``{ok, nodes: [{uri, app, kind, id, label, app_url}],
    edges: [{from, to, rel, ...}]}``."""
    try:
        status, body = fetch(f"{_f._hub()}/api/refs?uri={quote(str(uri), safe='')}&depth={int(depth or 1)}",
                             timeout=timeout, headers=_f._headers())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return _answer(status, body)


def unlink(from_uri: str, to_uri: str, rel: str = "", timeout: float = 5.0) -> dict[str, Any]:
    """Remove a link this app owns (one end is its own record)."""
    try:
        status, body = _f._post("/api/refs/remove", {"from": from_uri, "to": to_uri, **({"rel": rel} if rel else {})}, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return _answer(status, body)
