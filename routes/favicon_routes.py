"""routes/favicon_routes.py — ``GET /api/favicon?domain=<host>``.

Studio's activity rail shows a small favicon next to each web source the
agent read while answering (see ``docs/ui/i18n``'s "Reading {sites}" string
and ``studio/src/screens/studio/Transcript.tsx``'s ``FaviconStrip``). Serving
those icons same-origin, rather than pointing an `<img>` straight at
``https://<domain>/favicon.ico``, avoids leaking the viewer's IP/UA to every
site a search touched and lets the response be cached and capped.

Gating is ``require_user`` (read-only, same class as ``alternatives_routes``'s
``list_experiments``/``get_experiment``): this never mutates anything, it
only proxies a tiny public image through the same SSRF-guarded transport
``src/outbound_fetch.py`` already uses for ``web_fetch``.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from src.auth_helpers import require_user
from src.constants import DATA_DIR
from src.outbound_fetch import PUBLIC_UNTRUSTED, OutboundPolicyError, fetch

logger = logging.getLogger(__name__)

FAVICON_CACHE_DIR = Path(DATA_DIR) / "favicons"
_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_FETCH_TIMEOUT = 5.0
_MAX_BYTES = 64 * 1024

# A neutral globe/page placeholder shown whenever a real favicon cannot be
# fetched (unknown host, 404 everywhere, refused as private/loopback, or any
# other failure) -- Studio always gets *an* image back, never a broken <img>.
_PLACEHOLDER_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16">
<circle cx="8" cy="8" r="7" fill="none" stroke="#8a8a8a" stroke-width="1"/>
<path d="M1 8h14M8 1c2 2 2 12 0 14M8 1c-2 2-2 12 0 14" fill="none" stroke="#8a8a8a" stroke-width="1"/>
</svg>"""

# Only a bare hostname/domain -- letters, digits, dots and hyphens. Rejects
# anything that looks like a scheme, path, query, or credential ("@") so a
# caller cannot smuggle a full URL (and therefore a redirect target of its
# choosing) through what is documented as "just the host".
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}\.)+[a-zA-Z]{2,24}$")

_EXT_BY_CONTENT_TYPE = {
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_CONTENT_TYPE_BY_EXT = {v: k for k, v in _EXT_BY_CONTENT_TYPE.items()}


def _safe_domain(raw: str) -> Optional[str]:
    domain = (raw or "").strip().lower()
    if not domain or len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None
    return domain


def _cache_path_for(domain: str) -> Optional[Path]:
    """Return the cached favicon file for ``domain`` if it exists and is
    still fresh (mtime within ``_CACHE_TTL_SECONDS``); else ``None``."""
    try:
        FAVICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for ext in _EXT_BY_CONTENT_TYPE.values():
        candidate = FAVICON_CACHE_DIR / f"{domain}.{ext}"
        if candidate.exists():
            try:
                age = time.time() - candidate.stat().st_mtime
            except OSError:
                continue
            if age < _CACHE_TTL_SECONDS:
                return candidate
    return None


def _store_cache(domain: str, content_type: str, content: bytes) -> None:
    ext = _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower(), "ico")
    try:
        FAVICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (FAVICON_CACHE_DIR / f"{domain}.{ext}").write_bytes(content)
    except OSError as e:
        logger.debug("[favicon] cache write skipped for %s: %s", domain, e)


def _extract_icon_link(html: str) -> Optional[str]:
    """Pull the first ``<link rel="icon"...>`` (or "shortcut icon") href out
    of a homepage's HTML. Best-effort text scan, not a full HTML parser --
    this only ever feeds into the same SSRF-guarded `fetch()`, so a bad match
    here costs one wasted request, not a vulnerability."""
    m = re.search(
        r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]*href=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\'](?:shortcut )?icon["\']',
            html,
            re.IGNORECASE,
        )
    return m.group(1) if m else None


def _fetch_favicon_bytes(domain: str) -> Optional[tuple[str, bytes]]:
    """Try ``https://<domain>/favicon.ico`` first, then the homepage's own
    ``<link rel=icon>`` on a 404. Returns (content_type, bytes) or None.
    Every request goes through ``src.outbound_fetch.fetch`` under the
    ``public_untrusted`` profile -- the same SSRF guard (private/loopback
    hosts refused, DNS re-pinned per redirect hop) ``web_fetch`` uses."""
    try:
        result = fetch(
            f"https://{domain}/favicon.ico",
            profile=PUBLIC_UNTRUSTED,
            timeout=_FETCH_TIMEOUT,
            max_bytes=_MAX_BYTES,
            allowed_mime=("image/",),
        )
        if result.status_code == 200 and result.content:
            content_type = (result.headers.get("content-type") or "image/x-icon").split(";")[0].strip()
            return content_type, result.content
    except Exception as e:  # noqa: BLE001 - fall through to homepage probe
        logger.debug("[favicon] favicon.ico fetch failed for %s: %s", domain, e)

    try:
        home = fetch(
            f"https://{domain}/",
            profile=PUBLIC_UNTRUSTED,
            timeout=_FETCH_TIMEOUT,
            max_bytes=_MAX_BYTES,
            allowed_mime=("text/html", "text/"),
        )
        if home.status_code == 200 and home.content:
            href = _extract_icon_link(home.text)
            if href:
                if href.startswith("//"):
                    href = f"https:{href}"
                elif href.startswith("/"):
                    href = f"https://{domain}{href}"
                elif not href.startswith("http"):
                    href = f"https://{domain}/{href.lstrip('/')}"
                icon = fetch(
                    href,
                    profile=PUBLIC_UNTRUSTED,
                    timeout=_FETCH_TIMEOUT,
                    max_bytes=_MAX_BYTES,
                    allowed_mime=("image/",),
                )
                if icon.status_code == 200 and icon.content:
                    content_type = (icon.headers.get("content-type") or "image/x-icon").split(";")[0].strip()
                    return content_type, icon.content
    except Exception as e:  # noqa: BLE001
        logger.debug("[favicon] homepage icon probe failed for %s: %s", domain, e)

    return None


def _placeholder_response() -> Response:
    return Response(
        content=_PLACEHOLDER_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def setup_favicon_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["favicon"])

    @router.get("/favicon")
    def get_favicon(request: Request, domain: str = Query(..., max_length=253),
                     _u: str = Depends(require_user)) -> Response:
        safe = _safe_domain(domain)
        if not safe:
            return _placeholder_response()

        cached = _cache_path_for(safe)
        if cached is not None:
            content_type = _CONTENT_TYPE_BY_EXT.get(cached.suffix.lstrip("."), "image/x-icon")
            try:
                return Response(
                    content=cached.read_bytes(),
                    media_type=content_type,
                    headers={"Cache-Control": "public, max-age=604800"},
                )
            except OSError:
                pass  # fall through and refetch

        fetched = _fetch_favicon_bytes(safe)
        if fetched is None:
            return _placeholder_response()
        content_type, content = fetched
        _store_cache(safe, content_type, content)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=604800"},
        )

    return router
