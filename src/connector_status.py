"""src/connector_status.py — F1.3: honest connector state.

Two independent signals, both real, never faked:

* ``app`` — did the domain app itself answer its health endpoint? A plain
  TCP-level check (``httpx`` GET, 2s timeout, no redirects, no token in the
  request) against the app's own port, nothing to do with MCP.
* ``adapter`` — what does `McpManager` say about the *stdio bridge* to that
  app? A `tools/list` that returns a catalogue only proves the bridge
  process started, not that the app behind it is alive (rule 3) — hence two
  separate checks instead of inferring one from the other.

``compute_status`` combines them into one of the seven states the contract
defines, in a fixed priority so the same inputs always produce the same
verdict: disabled > unconfigured > (never checked) unknown > app_off >
Writer's wrong-service error > connecting > available > error.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from src.connectors import ConnectorPreset, read_token_file, resolve_preset_values
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 2.0
#: How long a freshly forced (``check=1``) health check is reused before a
#: second ``check=1`` triggers a real network call again — a debounce, not an
#: auto-refresh: a plain read (no ``check=1``) always reuses whatever is
#: cached, however old, per F1.5 ("sin él devuelve el último health cacheado").
CACHE_DEBOUNCE_S = 15.0

# connector_id -> (checked_at monotonic, health dict)
_health_cache: Dict[str, tuple] = {}


def _port_of(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return str(parsed.port or "")
    except Exception:  # noqa: BLE001
        return ""


async def _fetch_health(app_url: str, health_path: str, token: Optional[str] = None) -> Dict[str, Any]:
    """One real GET against ``app_url + health_path``. Never raises: every
    failure — refused connection, timeout, DNS, TLS — comes back as
    ``reachable: False`` with the failure recorded in ``detail``; anything
    else that answers at all (including a non-200 status) is
    ``reachable: True`` (F1.1: old-build compatibility), whatever the body
    turned out to be. Sends the app's own bearer token when the connector
    has a TOKEN_FILE and the bridge asks for it (Writer's Hoard answers 401
    to an anonymous /api/health, 17-09): first anonymously, then once more
    with the token only on a 401."""
    import httpx

    url = app_url.rstrip("/") + health_path
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=HEALTH_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code == 401 and token:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException, httpx.TransportError) as exc:
        return {
            "reachable": False, "checked_at": now_iso(), "latency_ms": None,
            "status_code": None, "body": None,
            "detail": ("connection refused or timed out" + (f": {exc}" if str(exc).strip() else "")),
        }
    except Exception as exc:  # noqa: BLE001 - a probe must never raise into the caller
        return {
            "reachable": False, "checked_at": now_iso(), "latency_ms": None,
            "status_code": None, "body": None, "detail": f"health check failed: {exc}",
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    body: Optional[dict] = None
    try:
        parsed = resp.json()
        body = parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - a non-JSON body is not a failure to reach the app
        body = None
    detail = "" if resp.status_code == 200 else (
        f"server answers but has no {health_path} (old build)" if body is None
        else f"server answered {resp.status_code}"
    )
    return {
        "reachable": True, "checked_at": now_iso(), "latency_ms": latency_ms,
        "status_code": resp.status_code, "body": body, "detail": detail,
    }


async def get_health(connector_id: str, app_url: str, health_path: str, *, force: bool,
                     token: Optional[str] = None) -> Dict[str, Any]:
    """The cached or freshly-checked app health for one connector.

    Returns ``None`` (not a dict) when nothing has ever been checked and
    `force` is False — the caller renders that as the ``unknown`` state
    rather than inventing a health result nobody asked for.
    """
    cached = _health_cache.get(connector_id)
    if force:
        if cached is not None and (time.monotonic() - cached[0]) < CACHE_DEBOUNCE_S:
            return cached[1]
        health = await _fetch_health(app_url, health_path, token)
        _health_cache[connector_id] = (time.monotonic(), health)
        return health
    if cached is not None:
        return cached[1]
    return None  # never checked


def invalidate_health(connector_id: str) -> None:
    _health_cache.pop(connector_id, None)


def _adapter_summary(manager_status: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mcp_status": manager_status.get("status", "disconnected"),
        "tool_count": manager_status.get("tool_count"),
        "last_error": manager_status.get("error"),
    }


async def compute_status(
    preset: Optional[ConnectorPreset],
    *,
    values: Dict[str, Any],
    is_enabled: bool,
    connector_id: str,
    manager_status: Dict[str, Any],
    force_check: bool,
) -> Dict[str, Any]:
    """The full `ConnectorStatus` for one connector (F1.3).

    `manager_status` is whatever `McpManager.get_server_status(server_id)`
    returned — this function never talks to the manager itself, so it stays
    trivially testable with a fake dict. Async because the health GET must
    not block the event loop a route handler runs on."""
    reasons: list = []
    empty_app = {"reachable": None, "checked_at": None, "latency_ms": None, "detail": ""}
    adapter = _adapter_summary(manager_status)

    if not is_enabled:
        return {"state": "disabled", "app": empty_app, "adapter": adapter,
                "reasons": ["Connector is disabled"]}

    if preset is None:
        return {"state": "error", "app": empty_app, "adapter": adapter,
                "reasons": ["Unknown preset for this connector"]}

    resolved = resolve_preset_values(preset, values)
    if not resolved["ok"]:
        reasons.extend(resolved.get("reasons") or [])
        return {"state": "unconfigured", "app": empty_app, "adapter": adapter, "reasons": reasons}

    app_url = resolved["app_url"]
    token = read_token_file(resolved.get("token_file") or "")
    health = await get_health(connector_id, app_url, preset.health_path, force=force_check, token=token)
    return _finish_status(preset, app_url, health, adapter, reasons)


def _finish_status(preset: ConnectorPreset, app_url: str, health: Optional[Dict[str, Any]],
                    adapter: Dict[str, Any], reasons: list) -> Dict[str, Any]:
    if health is None:
        return {
            "state": "unknown",
            "app": {"reachable": None, "checked_at": None, "latency_ms": None, "detail": ""},
            "adapter": adapter,
            "reasons": reasons + ["Health not checked yet — use ?check=1 or POST .../check"],
        }

    app_out = {k: health[k] for k in ("reachable", "checked_at", "latency_ms", "detail")}

    if health["reachable"] is False:
        reasons.append(f"App is not reachable at {app_url}: {health['detail']}")
        return {"state": "app_off", "app": app_out, "adapter": adapter, "reasons": reasons}

    expect_ok = True
    body = health.get("body") or {}
    for key, want in preset.health_expect.items():
        got = body.get(key)
        if got != want:
            expect_ok = False
            if preset.id == "writer" and key == "service":
                port = _port_of(app_url)
                reasons.append(
                    f"Port {port} answers but it is not Writer's Hoard (service={got!r})"
                )
            else:
                reasons.append(f"health check did not report expected {key}={want!r} (got {got!r})")

    if not expect_ok:
        return {"state": "error", "app": app_out, "adapter": adapter, "reasons": reasons}

    if adapter["mcp_status"] in ("connecting", "probing", "needs_auth"):
        return {"state": "connecting", "app": app_out, "adapter": adapter, "reasons": reasons}

    if adapter["mcp_status"] == "connected" and (adapter.get("tool_count") or 0) > 0:
        return {"state": "available", "app": app_out, "adapter": adapter, "reasons": reasons}

    if adapter.get("last_error"):
        reasons.append(str(adapter["last_error"]))
    if adapter["mcp_status"] not in ("connected",):
        reasons.append("The app is reachable, but the MCP adapter is not connected")
    elif not (adapter.get("tool_count") or 0):
        reasons.append("The MCP adapter is connected but reports no tools")
    return {"state": "error", "app": app_out, "adapter": adapter, "reasons": reasons}
