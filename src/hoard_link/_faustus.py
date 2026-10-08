"""Talking to a (possibly absent) Faustus instance.

Faustus is the local AI workspace, reachable on loopback with an
optional bearer token. Every helper here returns ``(status, json)`` with
``status is None`` meaning "could not connect at all" (refused, timed out,
DNS) — that is distinct from a real 401/403, which the caller needs to
tell apart to produce an accurate reason string. Nothing here raises, and
every request is bounded by a wall-clock ``TIMEOUT_S``.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

TIMEOUT_S = 1.5

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
# Names that only resolve inside the person's own network (mDNS and the home/LAN conventions).
_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[Optional[int], Any]:
    try:
        resp = await asyncio.wait_for(
            client.request(method, url, json=json_body, headers=headers, timeout=TIMEOUT_S),
            TIMEOUT_S,
        )
    except Exception:
        return None, None
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


async def get(
    client: httpx.AsyncClient, url: str, headers: Optional[dict[str, str]] = None
) -> tuple[Optional[int], Any]:
    return await _request(client, "GET", url, headers=headers)


async def post(
    client: httpx.AsyncClient,
    url: str,
    json_body: Any,
    headers: Optional[dict[str, str]] = None,
) -> tuple[Optional[int], Any]:
    return await _request(client, "POST", url, json_body=json_body, headers=headers)


def auth_headers(token: Optional[str]) -> Optional[dict[str, str]]:
    return {"Authorization": f"Bearer {token}"} if token else None


async def find_reachable(
    client: httpx.AsyncClient, candidate_urls: tuple[str, ...]
) -> Optional[str]:
    """Return the first candidate whose /api/health reports healthy."""
    for url in candidate_urls:
        url = url.rstrip("/")
        status, data = await get(client, f"{url}/api/health")
        if status == 200 and isinstance(data, dict) and data.get("status") == "healthy":
            return url
    return None


def host_scope(url: Any) -> str:
    """``"loopback"`` (this machine), ``"lan"`` (a private address or a local-network name: the person's own
    machines, such as a DGX Spark cluster next to the PC) or ``"remote"`` (anything else, cloud included)."""
    if not isinstance(url, str) or not url:
        return "remote"
    host = (urlsplit(url).hostname or "").lower().strip("[]")
    if host in _LOOPBACK_HOSTS or host == "::1":
        return "loopback"
    if host.endswith(_LAN_SUFFIXES):
        return "lan"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "remote"
    if ip.is_loopback:
        return "loopback"
    if (ip.is_private or ip.is_link_local) and not ip.is_unspecified and not ip.is_multicast:
        return "lan"
    return "remote"


def is_local_item(item: dict) -> bool:
    """Only use registry entries that keep data on the person's own machines.

    Faustus may also list cloud endpoints; a plugin must never send the
    user's data out of the home network just because Faustus knows an API key.
    Servers on this machine and on the local network (private addresses,
    ``.local`` names) that Faustus files as ``local`` are the person's own.
    """
    category = item.get("category")
    if category is not None and category != "local":
        return False
    return host_scope(item.get("url")) in ("loopback", "lan")


def is_lan_item(item: dict) -> bool:
    """A registry entry served by another of the person's machines (not this one)."""
    return host_scope(item.get("url")) == "lan"


def model_items(data: Any) -> list[dict]:
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def find_model_items(models: list[dict], capability: str) -> list[dict]:
    return [item for item in models if item.get("model_type") == capability]


def api_for_backend(backend: Optional[str]) -> str:
    if backend == "ollama":
        return "ollama"
    return "openai"
