"""Shared SSRF policy; configured local model endpoints retain operator-local access."""
import ipaddress
import socket
from src.hoard_link.web.safety import check_url, classify_ip, PUBLIC, OPERATOR_LOCAL

ALLOWED_SCHEMES = ("http", "https")

def _classify(ip, *, block_private=False):
    """Compatibility entry point for the broker's pinned-address checks."""
    reason = classify_ip(str(ip), PUBLIC if block_private else OPERATOR_LOCAL)
    if reason:
        reason = reason.replace("cloud metadata address", "link-local/cloud metadata address")
    return "blocked: " + reason if reason else None

def _default_resolver(host):
    return [info[4][0] for info in socket.getaddrinfo(host, None)]

def check_outbound_url(url, *, block_private=False, resolver=None):
    if not isinstance(url, str):
        return False, "URL must be a string"
    if not url.strip():
        return False, "URL is required"
    resolve = resolver or _default_resolver
    def addresses(host, port):
        valid = []
        for value in resolve(host):
            if not isinstance(value, str):
                continue
            try:
                valid.append(str(ipaddress.ip_address(value.split("%")[0])))
            except ValueError:
                continue
        if not valid:
            raise OSError("host does not resolve to an IP")
        return valid
    reason = check_url(url.strip(), PUBLIC if block_private else OPERATOR_LOCAL, addresses)
    if reason:
        reason = reason.replace("cloud metadata address", "link-local/cloud metadata address")
        if block_private and "local" in reason:
            reason = "private/local target: " + reason
    return (False, "blocked: " + reason) if reason else (True, "ok")
