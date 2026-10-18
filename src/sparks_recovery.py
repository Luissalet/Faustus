"""Report a degenerate LAN model to its family service owner.

Prometheus owns recipe identity, confirmation, idle coordination and restart.
This client never probes, stops or launches a remote model itself, and a queued
report does not mean that the model has recovered.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def managed_lan_url(url: str) -> str:
    """A normalized LAN candidate; the owner must still match its managed recipe."""
    try:
        parsed = urlsplit(str(url or ""))
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return ""
        if parsed.query or parsed.fragment:
            return ""
        # Do not resolve arbitrary names or contact an external API from recovery.
        try:
            address = ipaddress.ip_address(host)
            if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
                return ""
        except ValueError:
            if "." in host and not host.lower().endswith(".local"):
                return ""
            if host.lower() == "localhost":
                return ""
        _ = parsed.port  # Reject a malformed port before scheduling a report.
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def _send(url: str, model: str, reason: str) -> dict[str, Any]:
    from src import family_services
    from src.hoard_link import _famsvc

    family_services.configure()
    return _famsvc.call_tool("prometheus", "deploy_report_issue", {
        "base_url": url, "model": model, "error_class": "degenerate_output", "reason": reason,
    }, timeout_s=15.0)


async def report(url: str, model: str, reason: str = "") -> dict[str, Any]:
    endpoint = managed_lan_url(url)
    base = {"url": endpoint or str(url or ""), "model": model}
    if not endpoint or not model:
        return {**base, "action": "skipped", "detail": "not an identified LAN model endpoint"}
    try:
        response = await asyncio.to_thread(_send, endpoint, model, reason)
    except Exception as exc:
        return {**base, "action": "unknown", "detail": f"recovery owner unavailable: {type(exc).__name__}"}
    if not response.get("ok"):
        return {**base, "action": "unknown", "detail": "recovery owner did not accept the report"}
    result = response.get("data") or {}
    # A report is asynchronous. Preserve the owner's actual state for callers;
    # do not say restarted or healthy merely because the transport succeeded.
    if not isinstance(result, dict):
        return {**base, "action": "unknown", "detail": "invalid recovery owner response"}
    scope = {key: result[key] for key in (
        "verified_scope", "algorithm_semantics_verified", "canaries_discriminatively_validated",
    ) if key in result}
    state = result.get("status")
    if state in ("pending", "waiting_coordination", "confirming", "waiting_idle", "restarting", "checking", "verifying"):
        return {**base, **scope, "action": "checking", "recovery_id": result.get("id"),
                "status": state, "deduplicated": bool(result.get("deduplicated"))}
    if state == "recovered":
        return {**base, **scope, "action": "restarted", "recovery_id": result.get("id"), "status": state}
    return {**base, **scope, "action": "unknown", "recovery_id": result.get("id"), "status": state,
            "detail": result.get("detail") or "recovery has not confirmed structural generation checks"}
