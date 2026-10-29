"""Build httpx's default TLS context once per process, not once per request.

`httpx.get(...)` (and every `httpx.Client()` created for one call) builds a new
`ssl.SSLContext` and parses the whole CA bundle into it. On a loaded Windows PC
that took 3 s inside the server's event loop (stack captured by
src/loop_watchdog.py in a long turn: `ssl.create_default_context` called from the
context-window probe), and the server has many such one-shot calls. The default
context is read-only once built and safe to share between clients and threads,
so the one for `verify=True` with no client certificate is built the first time
and handed out afterwards. Any other `verify=` (a custom context, a CA file, off)
and any client certificate still build their own, so trust is never widened.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: Dict[Tuple[Any, ...], Any] = {}
_INSTALLED = False


def _key(trust_env: bool) -> Tuple[Any, ...]:
    return (bool(trust_env), os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))


def install() -> bool:
    """Patch httpx's transport to share the default TLS context. Idempotent; False when httpx has another layout."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        try:
            from httpx._transports import default as transport
            original = transport.create_ssl_context
        except Exception:  # noqa: BLE001 - another httpx: leave it alone
            logger.debug("httpx layout not recognised; TLS contexts stay per request", exc_info=True)
            return False

        def create_ssl_context(verify=True, cert=None, trust_env=True):  # same signature as httpx's
            if verify is not True or cert is not None:
                return original(verify=verify, cert=cert, trust_env=trust_env)
            key = _key(trust_env)
            ctx = _CACHE.get(key)
            if ctx is None:
                ctx = original(verify=True, cert=None, trust_env=trust_env)
                _CACHE[key] = ctx
            return ctx

        create_ssl_context.__wrapped__ = original  # type: ignore[attr-defined]
        transport.create_ssl_context = create_ssl_context
        _INSTALLED = True
        return True


def uninstall() -> None:
    """Restore httpx's own function (tests)."""
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            return
        from httpx._transports import default as transport
        transport.create_ssl_context = getattr(transport.create_ssl_context, "__wrapped__", transport.create_ssl_context)
        _CACHE.clear()
        _INSTALLED = False
