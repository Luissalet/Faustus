"""The app side of the hub's web service: ask the hub to read the web, instead of fetching on your own.

The hub (facet ``web``) owns ONE polite fetcher for the whole family: per-host spacing that holds across apps, one
robots.txt cache, block cooldowns that survive restarts, a shared response cache, one optional browser profile, web
search and link previews. An app calls :func:`fetch` / :func:`search` / :func:`preview` instead of carrying its own
HTTP code, and keeps that code only as the fallback for when the hub is not there::

    from hoard_link import family, fam_web

    family.configure("tantalus", DATA_DIR)                  # the app's token is how the hub knows who asks
    res = fam_web.fetch(url, extract="readable")             # {ok, status, text, extract: {title, text, ...}, from_cache, ...}
    if not res.get("ok") and res.get("error") == "hub unreachable":
        ...                                                  # the app's own fetcher

Most apps want both halves in one call: :func:`fetch_or_local` uses the hub when it answers and a process-local
:class:`hoard_link.web.fetch.Fetcher` (needs ``httpx``) when it does not; the answer says which one served it (``via``).

Every function returns a dict and never raises. The hub's answers are passed through as they are (see
``docs/commons/web.md``): a fetch that failed is ``{"ok": False, "status": <upstream status>, "error": ..., "error_kind": ...}``,
nothing answering is ``{"ok": False, "error": "hub unreachable"}``, a refused token adds ``status: 401``. A body fetched with
``accept="any"`` arrives decoded in ``body`` (bytes, up to 5 MB; bigger ones come with ``body_omitted``: use :func:`fetch_file`).

Standard library only (runs inside apps that vendor ``hoard_link/``); ``httpx`` is needed only by the local fallback.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Optional

from . import family as _f
from ._hubclient import fetch as _fetch

CACHE_S = 30.0
EXTRACTS = ("readable", "markdown", "meta", "jsonld", "feed")
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()
_local: dict[str, Any] = {"fetcher": None}
#: Seconds the hub may spend queueing behind other callers on top of the request's own ``timeout``.
QUEUE_GRACE_S = 35.0


def _get(path: str, timeout: float) -> tuple[Optional[int], Any]:
    return _fetch(_f._hub() + path, method="GET", timeout=timeout, headers=_f._headers())


def _post(path: str, body: dict[str, Any], timeout: float) -> tuple[Optional[int], Any]:
    return _f._post(path, body, timeout)


def _answer(status: Optional[int], body: Any) -> dict[str, Any]:
    if status is None:
        with _lock:
            _cache.pop(_f._hub(), None)
        return {"ok": False, "error": "hub unreachable"}
    if not isinstance(body, dict):
        return {"ok": 200 <= status < 300, "status": status, **({} if 200 <= status < 300 else {"error": f"HTTP {status}"})}
    body.setdefault("ok", 200 <= status < 300)
    if status == 401:
        body["error"] = f"the hub refused this app's token ({_f.status().get('token_file') or 'no token file'})"
        body.setdefault("status", 401)
    elif status >= 400 and not body.get("ok") and "tier" not in body:     # a hub-level refusal (400, 403, 503...), not a fetch result
        body.setdefault("status", status)
    return body


def available(timeout: float = 1.0) -> bool:
    """True when a hub with the web service answers (and accepts this app's token). Cached for 30 seconds."""
    base = _f._hub()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(base)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    status, data = _get("/api/web/status", timeout)
    ok = status == 200 and isinstance(data, dict) and data.get("ok") is True and data.get("enabled") is not False
    with _lock:
        _cache[base] = (now, ok)
    return ok


def _reset_cache() -> None:
    with _lock:
        _cache.clear()


def fetch(url: str, *, tier: str = "auto", accept: str = "html", etag: str = "", last_modified: str = "", respect_robots: bool = True,
          max_bytes: Optional[int] = None, timeout: float = 30, extract: Optional[str] = None, cache_ttl_s: float = 0,
          fresh: bool = False) -> dict[str, Any]:
    """Read one URL through the hub. ``tier``: ``auto`` (http, then the browser when the page is an anti-bot wall and the hub has
    one), ``http`` or ``browser``. ``accept``: ``html`` (text documents), ``json`` or ``any`` (binary: ``body`` bytes). ``etag`` /
    ``last_modified`` make it a conditional GET (``not_modified``). ``extract``: ``readable`` | ``markdown`` | ``meta`` | ``jsonld`` |
    ``feed`` adds an ``extract`` object. ``cache_ttl_s`` > 0 sets how old a cached copy may be (``0`` = the hub's own default, a few
    minutes); ``fresh=True`` skips the cache. ``respect_robots`` can only ask for MORE politeness: the hub's setting rules."""
    payload: dict[str, Any] = {"url": str(url or ""), "tier": tier, "accept": accept, "respect_robots": bool(respect_robots),
                               "timeout": timeout}
    for key, value in (("etag", etag), ("last_modified", last_modified), ("max_bytes", max_bytes), ("extract", extract)):
        if value:
            payload[key] = value
    if cache_ttl_s and cache_ttl_s > 0:
        payload["cache_ttl_s"] = float(cache_ttl_s)
    if fresh:
        payload["fresh"] = True
    status, data = _post("/api/web/fetch", payload, float(timeout) + QUEUE_GRACE_S)
    res = _answer(status, data)
    b64 = res.pop("body_b64", None)
    if b64:
        try:
            res["body"] = base64.b64decode(b64)
        except (ValueError, TypeError):
            res["body_error"] = "the body could not be decoded"
    return res


def fetch_file(url: str, *, dest_dir: Optional[str] = None, max_bytes: int = 50_000_000, timeout: float = 120) -> dict[str, Any]:
    """Download a file through the hub: ``{ok, path, sha256, content_type, size, filename}``. It is saved in the hub's
    ``data/web/files/<app>/`` unless ``dest_dir`` (an absolute folder the app chose) is given."""
    payload: dict[str, Any] = {"url": str(url or ""), "max_bytes": int(max_bytes), "timeout": timeout}
    if dest_dir:
        payload["dest_dir"] = str(dest_dir)
    status, data = _post("/api/web/fetch_file", payload, float(timeout) + QUEUE_GRACE_S)
    return _answer(status, data)


def search(query: str, *, limit: int = 10, freshness_days: Optional[int] = None, engines: Optional[list[str]] = None,
           news: bool = False, timeout: float = 90.0) -> dict[str, Any]:
    """Search the web: ``{ok, hits: [{url, title, snippet, engine, rank, published}], errors: {engine: why}, engines}``. One engine
    failing never hides the others."""
    payload: dict[str, Any] = {"query": str(query or ""), "limit": int(limit), "news": bool(news)}
    if freshness_days:
        payload["freshness_days"] = int(freshness_days)
    if engines:
        payload["engines"] = list(engines)
    status, data = _post("/api/web/search", payload, timeout)
    return _answer(status, data)


def preview(url: str, *, timeout: float = 40.0) -> dict[str, Any]:
    """A link card: ``{ok, title, description, image, favicon, favicons, site_name, canonical, ...}``; the hub keeps it 7 days."""
    status, data = _post("/api/web/preview", {"url": str(url or "")}, timeout)
    return _answer(status, data)


def host_status() -> dict[str, Any]:
    """The hub's per-host state: ``{ok, hosts: [{host, last_status, blocked_now, blocked_until_ts, block_reason, min_interval_s,
    preferred_tier, last_caller, ...}], blocked}``."""
    status, data = _get("/api/web/hosts", 5.0)
    return _answer(status, data)


def open_for_human(url: str) -> dict[str, Any]:
    """Ask the hub to open the page where a person can solve a challenge or log in (the family browser profile, or the default
    browser when the hub has no playwright). Returns at once: ``{ok, started, via}``."""
    status, data = _post("/api/web/open", {"url": str(url or "")}, 10.0)
    return _answer(status, data)


# ---------------------------------------------------------------------------------------------- extraction
def extract_payload(kind: str, text: str, base_url: str = "", *, cap: Optional[int] = None) -> dict[str, Any]:
    """The ``extract`` object of a fetch: ``readable`` (title, text, quality, excerpt), ``markdown`` (markdown, title, links,
    headings, tables), ``meta`` (page meta + feeds + favicons), ``jsonld`` (blocks, errors) or ``feed`` (format, title, items).
    The hub and the local fallback of :func:`fetch_or_local` build it with this one function. ``cap`` limits the text in bytes."""
    from .web import feeds, htmltext, meta

    def clip(value: str) -> tuple[str, bool]:
        if cap is None or len(value) * 4 <= cap or len(value.encode("utf-8")) <= cap:
            return value, False
        return value.encode("utf-8")[:cap].decode("utf-8", "ignore"), True

    try:
        if kind == "readable":
            title, body = htmltext.readable(text)
            body, cut = clip(body)
            return {"kind": kind, "title": title, "text": body, "text_truncated": cut, "quality": htmltext.quality(body),
                    "excerpt": htmltext.excerpt(body)}
        if kind == "markdown":
            md = htmltext.to_markdown(text, base_url)
            md["markdown"], cut = clip(md.get("markdown", ""))
            return {"kind": kind, "text_truncated": cut, **md}
        if kind == "meta":
            return {"kind": kind, **meta.page_meta(text, base_url), "feeds": meta.discover_feeds(text, base_url),
                    "favicons": meta.favicon_candidates(text, base_url)}
        if kind == "jsonld":
            blocks, errors = meta.jsonld_blocks(text)
            return {"kind": kind, "blocks": blocks, "errors": errors}
        if kind == "feed":
            feed = feeds.parse_feed(text, base_url)
            if feed is None:
                return {"kind": kind, "error": "the document is not an RSS, Atom or RDF feed"}
            return {"kind": kind, **feed}
    except Exception as exc:  # noqa: BLE001 - extraction must not lose the fetch
        return {"kind": kind, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"kind": kind, "error": "unknown extract"}


# ---------------------------------------------------------------------------------------------- hub first, local second
def local_fetcher() -> Any:
    """The process-local fallback :class:`~hoard_link.web.fetch.Fetcher` (built on first use; needs ``httpx``)."""
    with _lock:
        if _local["fetcher"] is None:
            from .web.fetch import Fetcher
            _local["fetcher"] = Fetcher(min_interval_s=2.0, cache_ttl_s=0.0)
        return _local["fetcher"]


def _hub_gone(res: dict[str, Any]) -> bool:
    """True when the hub itself could not serve the call (no hub, token refused, service off), not when the page failed."""
    if res.get("error") == "hub unreachable":
        return True
    return res.get("ok") is False and "tier" not in res and res.get("status") in (401, 503)


def fetch_or_local(url: str, *, local_fetcher: Any = None, **kw: Any) -> dict[str, Any]:
    """:func:`fetch` through the hub when it is there, else through a process-local Fetcher (``local_fetcher``, or a lazily built
    default). Same arguments as :func:`fetch`; the answer has the same shape plus ``via``: ``"hub"`` or ``"local"``. Where the app
    fetches from is then the hub's per-host state when it can be, and this process's own when it cannot."""
    if available():
        res = fetch(url, **kw)
        if not _hub_gone(res):
            res["via"] = "hub"
            return res
        with _lock:
            _cache.pop(_f._hub(), None)
    return _fetch_local(url, local_fetcher, **kw)


def _fetch_local(url: str, fetcher: Any, *, tier: str = "auto", accept: str = "html", etag: str = "", last_modified: str = "",
                 respect_robots: bool = True, max_bytes: Optional[int] = None, timeout: float = 30, extract: Optional[str] = None,
                 cache_ttl_s: float = 0, fresh: bool = False) -> dict[str, Any]:
    try:
        f = fetcher if fetcher is not None else local_fetcher()
        fr = f.get(str(url or ""), tier=tier, accept=accept, etag=etag, last_modified=last_modified, respect_robots=respect_robots,
                   max_bytes=max_bytes, timeout=timeout, cache_ttl_s=(None if not cache_ttl_s or fresh else float(cache_ttl_s)))
    except Exception as exc:  # noqa: BLE001 - never raises: no httpx, a broken fetcher...
        return {"ok": False, "url": str(url or ""), "error": f"{type(exc).__name__}: {exc}"[:300], "error_kind": "network", "via": "local"}
    res = fr.to_dict(with_text=True)
    res["text_truncated"] = False
    if fr.body is not None:
        res["body"] = fr.body
        res["body_size"] = len(fr.body)
    if extract in EXTRACTS and fr.ok and not fr.not_modified and fr.text:
        res["extract"] = extract_payload(extract, fr.text, fr.final_url or fr.url)
    res["via"] = "local"
    return res


__all__ = ["available", "fetch", "fetch_file", "search", "preview", "host_status", "open_for_human", "fetch_or_local",
           "extract_payload", "local_fetcher"]
