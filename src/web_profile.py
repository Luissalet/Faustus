"""Passive observations from a single existing, guarded HTTP fetch.

Inspired by Web-Check's useful separation of observations and check coverage.
No scores, vulnerability verdicts, scans or additional network requests.
"""
HEADERS = (
    "content-type", "server", "x-powered-by", "strict-transport-security",
    "content-security-policy", "content-security-policy-report-only",
    "x-frame-options", "x-content-type-options", "referrer-policy",
    "permissions-policy", "cache-control", "last-modified", "etag",
)


def passive_http_profile(requested_url: str, result: dict) -> dict:
    recorded = isinstance(result.get("headers"), dict)
    headers = {str(k).lower(): str(v) for k, v in (result.get("headers") or {}).items()}
    observed = {name: {"present": (name in headers) if recorded else None, "value": headers.get(name, "")[:256],
                       "value_truncated": len(headers.get(name, "")) > 256}
                for name in HEADERS}
    return {
        "kind": "passive_http_profile", "requested_url": requested_url[:1024],
        "final_url": str(result["final_url"])[:1024] if result.get("final_url") else None,
        "urls_truncated": len(requested_url) > 1024 or len(str(result.get("final_url") or "")) > 1024,
        "http_status": result.get("http_status"), "headers_recorded": recorded,
        "captured_at": result.get("captured_at"), "headers": observed,
        "body_truncated": bool(result.get("truncated")),
        "not_checked": ["redirect_chain", "cookies", "dns", "tls_certificate",
                        "robots", "sitemap", "ports", "vulnerabilities"],
        "interpretation": "Header presence and values are observations, not a security verdict. "
                          "Server/technology claims are self-reported and may be inaccurate. "
                          "Null metadata was not recorded by the fetch backend.",
    }
