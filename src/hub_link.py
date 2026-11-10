"""Where the family hub is when Faustus does not run next to it.

On the person's PC the hub is found the usual way (:func:`src.hoard_hub.hub_location`). On a Spark there is no hub: the
hub of the PC keeps a tunnel open towards the Spark while Faustus is there and leaves a small file that says where it
answers — a loopback address and the path of a copy of the hub's token. The unit that starts Faustus on the Spark names
that file in ``FAUSTUS_HUB_LINK``.

Only what lives in the hub's own records goes through the link: the Agora (spaces, channels, tasks, threads) and
coordination (rooms and council). Services that hand over file paths do not: a path here means nothing on the PC, and
they already work on the Spark by themselves.

Nothing here raises. With the PC off the link file is still there and the address refuses the connection at once, which
callers report as "the hub is not answering".
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src import hoard_hub

ENV = "FAUSTUS_HUB_LINK"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def link() -> Optional[Dict[str, str]]:
    """``{"url", "token_file"}`` from the link file, or None when Faustus runs next to its hub (no file named, missing or
    not a loopback http address)."""
    path = (os.environ.get(ENV) or "").strip()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    url = str((data or {}).get("url") or "").strip().rstrip("/") if isinstance(data, dict) else ""
    try:
        parts = urllib.parse.urlsplit(url)
        local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and bool(parts.port) \
            and not parts.username and parts.path in ("", "/") and not parts.query
    except ValueError:
        local = False
    if not local:
        return None
    return {"url": f"http://{parts.hostname}:{parts.port}", "token_file": str(data.get("token_file") or "").strip()}


def location() -> Dict[str, Any]:
    """Where to call the hub: ``{"url", "token_file", "remote"}``. ``remote`` is True when the hub is on another machine,
    reached through the link."""
    linked = link()
    if linked:
        return {**linked, "remote": True}
    where = hoard_hub.hub_location()
    return {"url": where["url"], "token_file": where["token_file"], "remote": False}


def token(where: Optional[Dict[str, Any]] = None) -> str:
    where = where or location()
    return hoard_hub._read_text(where["token_file"]) if where.get("token_file") else ""


def post(path: str, body: dict, timeout: float = 30.0) -> Tuple[Optional[int], Any]:
    """``(status, json)`` of a POST to the hub through the link; ``(None, None)`` when nothing answered or there is no
    link. Same shape as the family client's own post."""
    linked = link()
    if not linked:
        return None, None
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "hoard-link"}
    bearer = token(linked)
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(linked["url"] + path, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001 - refused (the PC is off), timed out
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None
