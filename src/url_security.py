"""URL validation helpers for server-side outbound requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


from src.hoard_link.web.safety import check_url, PUBLIC

def _resolve_hostname_ips(hostname):
    return [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(hostname, None)]

def is_public_http_url(url):
    try:
        return check_url((url or "").strip(), PUBLIC,
                         lambda host, port: [str(ip) for ip in _resolve_hostname_ips(host)]) is None
    except (TypeError, ValueError):
        return False

def validate_public_http_url(url: str, *, max_length: int = 2048) -> str:
    """Validate a user/API-token supplied server-side HTTP(S) endpoint.

    This is for untrusted outbound URLs, not admin-created model endpoints
    that are intentionally allowed to point at private model providers. DNS
    failures fail closed, and DNS checks reduce obvious private-network
    targets but do not eliminate every DNS rebinding race by themselves.
    """
    cleaned = (url or "").strip()
    if len(cleaned) > max_length:
        raise ValueError("URL is too long")
    if not is_public_http_url(cleaned):
        raise ValueError("URL must point to a public HTTP(S) endpoint")
    return cleaned
