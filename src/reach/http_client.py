"""Shared httpx client factory for Reach backends.

A single seam (`make_client`) so tests can monkeypatch it to return a client
wired to `httpx.MockTransport` instead of touching the network (contract
rule 6: no network in tests). Short timeouts everywhere -- a slow mirror
must not stall the whole channel fallback chain.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=4.0)

REACH_USER_AGENT = "Mozilla/5.0 (compatible; FaustusReach/1.0; +https://github.com/anomalyco/opencode)"


def make_client(*, timeout: Optional[httpx.Timeout] = None, headers: Optional[dict] = None, **kwargs: Any) -> httpx.AsyncClient:
    hdrs = {"User-Agent": REACH_USER_AGENT}
    if headers:
        hdrs.update(headers)
    return httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT, headers=hdrs, follow_redirects=True, **kwargs)
