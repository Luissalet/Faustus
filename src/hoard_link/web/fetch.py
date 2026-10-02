"""The polite fetcher: one way to read a web page or an API from any Hoard app.

``Fetcher.get(url, ...)`` never raises for an expected failure; it returns a :class:`FetchResult` with ``ok``,
``error`` and ``error_kind`` filled in. What it does on every request:

* **Policy** (:mod:`.safety`) at the first URL *and every redirect hop*, with the connection pinned to the address that
  was checked (no DNS rebinding between check and connect). Profiles: ``public`` (default), ``operator_local``,
  ``internal``. Redirects are followed by hand (at most 5); credentials headers are dropped on a cross-host hop.
* **robots.txt** (:mod:`.robots`, wildcard rules, ``Crawl-delay`` honoured) for HTML requests.
* **Politeness**: one request at a time per host, a minimum interval per host (state in a :class:`HostStateStore`: in
  memory by default, :class:`JsonFileHostState` to share it between processes), a **block cooldown** after an anti-bot
  page, a login wall or HTTP 429 (30 minutes, or the server's ``Retry-After`` capped at one hour).
* **Conditional GET** (``etag`` / ``last_modified`` -> ``not_modified``), a **byte cap** while streaming (``truncated``),
  gzip/deflate bounded against decompression bombs, charset from BOM, header, ``<meta>`` / XML prolog, then UTF-8,
  then cp1252.
* **Retries** with backoff on network errors and 5xx (default 1), a **disk cache** with ``ttl`` and stale-if-error,
  an **offline** switch that answers from the cache only.
* **Block detection** (:mod:`.blocks`) and an optional **browser** rung (:mod:`.browser`) for blocked pages.

``error_kind`` is one of ``dns tls timeout refused reset network policy robots offline blocked http content
redirects``. :class:`JsonApiClient` wraps the same machinery for API clients (pacing, cache, retries, offline).

Everything that touches the world is injectable (``transport``, ``clock``, ``sleep``, ``resolver``, ``state``), so tests
never need the network. ``httpx`` is imported when the first request is made.

This module replaces: Tantalus ``fetch/__init__.py``, Links ``fetcher.js``/``watches.js::fetchText`` (Python side of the
design), Midas ``providers/base.py::HttpClient``, Cassandra ``classify_error``, the per-app ``httpx.get`` calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import ssl
import threading
import time
import zlib
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..errors import HoardLinkError, missing_dependency
from . import safety
from .blocks import BROWSER_RETRY_REASONS, HTTP_403, HTTP_429, LOGIN, apply_block, block_hint, detect_block
from .robots import DEFAULT_AGENT, RobotsCache

__all__ = [
    "DEFAULT_USER_AGENT", "FetchResult", "Fetcher", "HostStateStore", "MemoryHostState", "JsonFileHostState",
    "JsonApiClient", "ApiResponse", "ApiError", "decode_body", "classify_error", "ERROR_KINDS",
]

# One current desktop browser string for every app, so the family looks like one client and carries no app name.
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/148.0.0.0 Safari/537.36")
ACCEPT = {
    "html": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "json": "application/json, text/plain, */*",
    "any": "*/*",
}
ERROR_KINDS = ("dns", "tls", "timeout", "refused", "reset", "network", "policy", "robots", "offline", "blocked", "http",
               "content", "redirects")

MAX_BODY_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_MIN_INTERVAL_S = 1.0
MAX_WAIT_S = 30.0                  # one politeness sleep never exceeds this
BLOCK_COOLDOWN_S = 30 * 60
MAX_COOLDOWN_S = 3600
ROBOTS_TIMEOUT_S = 10.0
ROBOTS_MAX_BYTES = 512 * 1024
_REDIRECTS = (301, 302, 303, 307, 308)
_SENSITIVE = ("authorization", "cookie", "proxy-authorization", "x-api-key", "x-subscription-token")
_BINARY_TYPES = ("image/", "video/", "audio/", "font/", "application/octet-stream", "application/zip", "application/pdf",
                 "application/x-", "application/gzip", "application/vnd.")
_TEXTY = ("text/", "application/json", "application/xml", "application/xhtml", "application/rss", "application/atom",
          "application/javascript", "application/ld+json", "+json", "+xml")
_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.I | re.S)


# ---------------------------------------------------------------------------------------------- results
@dataclass
class FetchResult:
    """What every fetch returns. ``text`` is decoded; ``body`` holds the raw bytes only for ``accept="any"``."""

    url: str
    final_url: str = ""
    status: int = 0                    # HTTP status (0: no HTTP answer)
    headers: dict[str, str] = field(default_factory=dict)      # lowercase names
    content_type: str = ""
    text: str = ""
    body: Optional[bytes] = None
    etag: str = ""
    last_modified: str = ""
    ok: bool = False                   # a usable document was obtained
    not_modified: bool = False         # conditional GET answered 304
    truncated: bool = False            # the body hit max_bytes and was cut
    from_cache: bool = False
    stale: bool = False                # from_cache after a failure (stale-if-error)
    elapsed_ms: int = 0
    fetched_at: float = 0.0
    tier: str = "http"                 # http | browser | window | cache
    blocked: bool = False              # an anti-bot page, a login wall or 403/429
    block_reason: str = ""             # cloudflare | akamai | datadome | perimeterx | captcha | login | http_403 | http_429 | http_5xx
    error: str = ""
    error_kind: str = ""               # see ERROR_KINDS
    note: str = ""                     # non-fatal remark (robots unreachable, served stale ...)
    redirects: list[str] = field(default_factory=list)

    def to_dict(self, *, with_text: bool = False) -> dict[str, Any]:
        d = asdict(self)
        d.pop("body", None)
        text = d.pop("text", "")
        d["text_len"] = len(text)
        if with_text:
            d["text"] = text
        return d


# ---------------------------------------------------------------------------------------------- host state
class HostStateStore(Protocol):
    """Per-host politeness state. Keys: ``last_fetch_ts, min_interval_s, blocked_until_ts, block_reason,
    preferred_tier, ok_count, fail_count``. The hub can supply a SQLite-backed one."""

    def get(self, host: str) -> dict[str, Any]: ...
    def update(self, host: str, **cols: Any) -> None: ...
    def items(self) -> Iterable[tuple[str, dict[str, Any]]]: ...


_STATE_DEFAULT: dict[str, Any] = {"last_fetch_ts": 0.0, "min_interval_s": None, "blocked_until_ts": 0.0, "block_reason": "",
                                  "preferred_tier": "", "ok_count": 0, "fail_count": 0}


class MemoryHostState:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, host: str) -> dict[str, Any]:
        with self._lock:
            return {**_STATE_DEFAULT, **self._rows.get(host, {})}

    def update(self, host: str, **cols: Any) -> None:
        with self._lock:
            self._rows.setdefault(host, {}).update(cols)

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(h, {**_STATE_DEFAULT, **r}) for h, r in sorted(self._rows.items())]


class JsonFileHostState(MemoryHostState):
    """Host state kept in a JSON file, so several processes (and restarts) share cooldowns and throttles. The file
    is re-read when its modification time changes and replaced atomically (retrying on Windows sharing errors)."""

    def __init__(self, path: Any):
        super().__init__()
        self.path = Path(path)
        self._mtime = -1.0
        self._load()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._rows = {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
                self._mtime = mtime
        except (OSError, ValueError):
            pass

    def get(self, host: str) -> dict[str, Any]:
        self._load()
        return super().get(host)

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        self._load()
        return super().items()

    def update(self, host: str, **cols: Any) -> None:
        self._load()
        super().update(host, **cols)
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._rows, indent=0), encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            self._mtime = self.path.stat().st_mtime
        except OSError:
            with suppress(OSError):
                tmp.unlink()


# ---------------------------------------------------------------------------------------------- helpers
def _chain(error: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = error
    while cur is not None and id(cur) not in seen:
        out.append(cur)
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return out


def classify_error(error: BaseException, host: str = "", timeout: float = 0.0) -> tuple[str, str]:
    """An exception from httpx, ``ssl`` or ``socket`` as ``(kind, detail)`` with kind ``dns`` | ``tls`` | ``timeout`` |
    ``refused`` | ``reset`` | ``network``."""
    try:
        import httpx
    except ImportError:                                    # pragma: no cover
        httpx = None                                       # type: ignore[assignment]
    for item in _chain(error):
        if isinstance(item, socket.gaierror):
            return "dns", f"DNS failure: {host or 'the host'} does not resolve ({item.strerror or item})"
        if isinstance(item, ssl.SSLCertVerificationError):
            return "tls", f"TLS error: {getattr(item, 'verify_message', '') or getattr(item, 'reason', '') or item}"
        if isinstance(item, ssl.SSLError):
            return "tls", f"TLS error: {getattr(item, 'reason', '') or item}"
        if isinstance(item, (socket.timeout, TimeoutError)) or (httpx is not None and isinstance(item, httpx.TimeoutException)):
            return "timeout", f"Timeout: no answer within {timeout:.0f} s" if timeout else "Timeout: no answer"
        if isinstance(item, ConnectionRefusedError):
            return "refused", "Connection refused: nothing accepts connections on that port"
        if isinstance(item, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) or (
                httpx is not None and isinstance(item, (httpx.RemoteProtocolError, httpx.ReadError))):
            return "reset", "Connection reset by the server"
    return "network", f"{type(error).__name__}: {error}"[:300]


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_\-:.]+)["']""", re.I)


def decode_body(body: bytes, content_type: str = "") -> str:
    """Bytes to text, never raising: a BOM wins, then the ``charset`` of the ``Content-Type``, then ``<meta charset>``
    or the XML prolog, then strict UTF-8, finally cp1252 (the usual mislabelled legacy page)."""
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        with suppress(UnicodeError):
            return body.decode("utf-16")
    ctype = (content_type or "").lower()
    names: list[str] = []
    m = re.search(r"charset\s*=\s*['\"]?([\w\-:.]+)", ctype)
    if m:
        names.append(m.group(1))
    if "json" not in ctype:
        sniff = _META_CHARSET.search(body[:4096]) if ("html" in ctype or not ctype) else None
        sniff = sniff or _XML_ENCODING.search(body[:200])
        if sniff:
            names.append(sniff.group(1).decode("ascii", "ignore"))
    for name in names:
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError):
            with suppress(LookupError):
                if name.lower() in ("utf-8", "utf8"):
                    return body.decode("utf-8", "replace")
            continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")


def _with_params(url: str, params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(k, str(v)) for k, v in params.items() if v is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _retry_after(value: str, now: float) -> Optional[float]:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, IndexError):
        return None


class _Unsupported(Exception):
    pass


def _read_body(response: Any, cap: int) -> tuple[bytes, bool]:
    """Stream the body, decoding gzip/deflate with a hard output bound (a compression bomb never inflates past
    ``cap``). Returns ``(bytes, truncated)``."""
    if getattr(response, "is_stream_consumed", False):          # already read (a test transport): httpx decoded it
        data = response.content
        return data[:cap], len(data) > cap
    enc = (response.headers.get("content-encoding") or "").strip().lower()
    inflater: Any = None
    if enc in ("gzip", "x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        inflater = zlib.decompressobj(zlib.MAX_WBITS)
    elif enc not in ("", "identity"):
        raise _Unsupported(enc)
    out = bytearray()
    first = True
    for chunk in response.iter_raw():
        if inflater is None:
            out += chunk
        else:
            buf = chunk
            if first and enc == "deflate" and buf[:1] and (buf[0] & 0x0F) != 8:
                inflater = zlib.decompressobj(-zlib.MAX_WBITS)       # raw deflate without the zlib header
            first = False
            try:
                while buf and len(out) <= cap:
                    piece = inflater.decompress(buf, cap + 1 - len(out))
                    out += piece
                    buf = inflater.unconsumed_tail
                    if not piece and not buf:
                        break
            except zlib.error:
                break
        if len(out) > cap:
            return bytes(out[:cap]), True
    return bytes(out), False


# ---------------------------------------------------------------------------------------------- the fetcher
class Fetcher:
    """See the module docstring. All arguments are keyword-only.

    ``profile``          default policy profile (``public`` | ``operator_local`` | ``internal``), overridable per call.
    ``min_interval_s``   default minimum gap between two requests to one host (per-host overrides in the state store).
    ``state``            a :class:`HostStateStore` (default: in memory).
    ``cache_dir`` / ``cache_ttl_s``   enable the disk cache; ``stale_if_error`` serves an old copy when a fetch fails.
    ``offline``          answer from the cache only.
    ``pin``              connect to the checked address (default ``None`` = yes, unless a proxy is configured in the
                         environment, which cannot be combined with pinning).
    ``transport``        an ``httpx`` transport used for every request (tests, ``httpx.MockTransport``); disables pinning.
    ``browser``          a browser rung (:class:`~.browser.BrowserRung`), ``False`` to disable, ``None`` to build one on
                         demand when ``browser_profile_dir`` is given.
    """

    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT, profile: str = safety.PUBLIC,
                 state: Optional[HostStateStore] = None, transport: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, resolver: Optional[safety.Resolver] = None, browser: Any = None,
                 browser_profile_dir: Any = None, offline: bool = False, cache_dir: Any = None, cache_ttl_s: float = 0.0,
                 stale_if_error: bool = True, min_interval_s: float = DEFAULT_MIN_INTERVAL_S, timeout_s: float = 20.0,
                 max_bytes: int = MAX_BODY_BYTES, max_redirects: int = MAX_REDIRECTS, retries: int = 1,
                 backoff_s: float = 0.6, accept_language: str = "en-US,en;q=0.8", block_cooldown_s: float = BLOCK_COOLDOWN_S,
                 robots_agent: str = DEFAULT_AGENT, robots_store: Any = None, pin: Optional[bool] = None):
        safety._profile(profile)
        self.user_agent = user_agent
        self.profile = profile
        self.state: HostStateStore = state if state is not None else MemoryHostState()
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.resolver = resolver
        self.offline = offline
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl_s = float(cache_ttl_s)
        self.stale_if_error = stale_if_error
        self.min_interval_s = float(min_interval_s)
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self.retries = int(retries)
        self.backoff_s = float(backoff_s)
        self.accept_language = accept_language
        self.block_cooldown_s = float(block_cooldown_s)
        self._browser = browser
        self._browser_profile_dir = browser_profile_dir
        self._browser_lock = threading.Lock()
        self._browser_cooldown: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._client: Any = None
        self._client_lock = threading.Lock()
        if pin is None:
            import urllib.request
            pin = not any(k in urllib.request.getproxies() for k in ("http", "https"))
        self.pin = bool(pin) and transport is None
        self.robots = RobotsCache(self._fetch_robots, store=robots_store, clock=clock, agent=robots_agent)
        self.hits = 0
        self.misses = 0

    # ================================================================== public
    def get(self, url: str, *, tier: str = "auto", headers: Optional[Mapping[str, str]] = None,
            params: Optional[Mapping[str, Any]] = None, accept: str = "html", etag: str = "", last_modified: str = "",
            respect_robots: bool = True, min_interval_s: Optional[float] = None, timeout: Optional[float] = None,
            max_bytes: Optional[int] = None, retries: Optional[int] = None, cache_ttl_s: Optional[float] = None,
            profile: Optional[str] = None, allowed_mime: Optional[Iterable[str]] = None, cache_html: bool = True) -> FetchResult:
        """Fetch one URL. ``tier``: ``auto`` (http, then the browser when blocked), ``http``, ``browser``, ``window``.
        ``accept``: ``html``, ``json`` or ``any`` (raw ``body`` bytes, binary allowed). ``allowed_mime``: content-type
        prefixes to accept (others come back as ``error_kind="content"``)."""
        started = self.clock()
        full_url = _with_params(url, params)
        accept = accept if accept in ACCEPT else "html"
        fr = FetchResult(url=full_url, fetched_at=started)
        profile = profile or self.profile
        ttl = self.cache_ttl_s if cache_ttl_s is None else float(cache_ttl_s)
        use_cache = bool(self.cache_dir) and accept != "any" and ttl > 0
        key = self._cache_key(full_url, accept) if self.cache_dir and accept != "any" else ""
        entry: Optional[dict[str, Any]] = None

        if use_cache:
            entry = self._cache_read(key)
            if entry and started - float(entry.get("ts", 0)) <= ttl:
                self.hits += 1
                return self._from_cache(fr, entry, started)
        if self.offline:
            entry = self._cache_read(key) if key else None
            if entry:
                self.hits += 1
                return self._from_cache(fr, entry, started)
            return self._fail(fr, "offline mode: the network is disabled and nothing is cached for this request", "offline", started)
        self.misses += 1

        host = (urlsplit(full_url).hostname or "").lower()
        memo: dict[tuple[str, int], list[str]] = {}
        try:
            self._ips(full_url, profile, memo)
        except safety.PolicyError as error:
            return self._fail(fr, error.reason, error.kind, started)
        try:
            tier = tier if tier in ("auto", "http", "browser", "window") else "auto"
            with self._host_lock(host):
                result = self._get_locked(full_url, host, tier, dict(headers or {}), accept, etag, last_modified, respect_robots,
                                          min_interval_s, float(timeout or self.timeout_s),
                                          int(max_bytes or self.max_bytes), self.retries if retries is None else int(retries),
                                          profile, tuple(allowed_mime or ()), fr, memo, stale_entry=entry if use_cache else None)
        except Exception as error:                                         # noqa: BLE001 - get() never raises
            kind, detail = classify_error(error, host, float(timeout or self.timeout_s))
            result = self._fail(fr, detail, kind, started)
        if result.ok and not result.from_cache and use_cache and not result.blocked and not result.truncated and not result.not_modified \
                and (cache_html or "html" not in result.content_type.lower()):
            self._cache_write(key, result)
        elif not result.ok and use_cache and self.stale_if_error and result.error_kind not in ("policy", "robots", "offline"):
            old = entry or self._cache_read(key)
            if old:
                served = self._from_cache(FetchResult(url=full_url, fetched_at=started), old, started)
                served.stale = True
                served.note = f"served from cache after a failure: {result.error}"
                return served
        result.elapsed_ms = result.elapsed_ms or int((self.clock() - started) * 1000)
        return result

    def get_json(self, url: str, **kwargs: Any) -> tuple[FetchResult, Any]:
        """``get`` with ``Accept: application/json``; returns ``(result, parsed)`` where ``parsed`` is ``None`` unless
        the result is ok and the body is valid JSON (``result.error`` then says ``invalid JSON``)."""
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Accept", ACCEPT["json"])
        kwargs.setdefault("accept", "json")
        fr = self.get(url, headers=headers, **kwargs)
        if not fr.ok or fr.not_modified:
            return fr, None
        text = fr.text
        if fr.tier in ("browser", "window"):
            m = _PRE.search(text)
            if m:
                import html as _html
                text = _html.unescape(m.group(1))
        try:
            return fr, json.loads(text)
        except ValueError as error:
            fr.ok = False
            fr.error = f"invalid JSON: {error}"
            fr.error_kind = "content"
            return fr, None

    def host_status(self) -> list[dict[str, Any]]:
        now = self.clock()
        out = []
        for host, row in self.state.items():
            until = float(row.get("blocked_until_ts") or 0)
            out.append({"host": host, **row, "blocked_now": until > now})
        return out

    def clear_block(self, host: str) -> None:
        self.state.update(host, blocked_until_ts=0.0, block_reason="")
        self._browser_cooldown.pop(host, None)

    def reset_host(self, host: str) -> None:
        self.clear_block(host)
        self.state.update(host, preferred_tier="")

    def set_min_interval(self, host: str, seconds: Optional[float]) -> None:
        self.state.update(host, min_interval_s=None if seconds is None else max(0.0, float(seconds)))

    def open_for_human(self, url: str, **kw: Any) -> dict[str, Any]:
        """Open a visible browser window on the shared profile so a person can solve a CAPTCHA or log in; returns when it
        is closed and clears the host's block."""
        rung = self._rung()
        if rung is None:
            raise HoardLinkError("the browser rung is disabled or unavailable")
        result = rung.open_for_human(url, **kw)
        host = (urlsplit(url).hostname or "").lower()
        if host:
            self.clear_block(host)
        return result

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                with suppress(Exception):
                    self._client.close()
                self._client = None
        if self._browser not in (None, False) and getattr(self, "_browser_owned", False):
            with suppress(Exception):
                self._browser.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ================================================================== orchestration
    def _fail(self, fr: FetchResult, message: str, kind: str, started: float) -> FetchResult:
        fr.error = message
        fr.error_kind = kind
        fr.elapsed_ms = int((self.clock() - started) * 1000)
        return fr

    def _get_locked(self, url: str, host: str, tier: str, headers: dict[str, str], accept: str, etag: str, last_modified: str,
                    respect_robots: bool, min_interval_s: Optional[float], timeout_s: float, cap: int, retries: int,
                    profile: str, allowed_mime: tuple[str, ...], fr: FetchResult, memo: dict,
                    stale_entry: Optional[dict] = None) -> FetchResult:
        note = ""
        if respect_robots and accept == "html":
            allowed, note = self.robots.check(url)
            if not allowed:
                fr.error = f"robots.txt of {host} disallows this path"
                fr.error_kind = "robots"
                return fr
        revalidate = stale_entry if (stale_entry and not (etag or last_modified)) else None
        if revalidate:                                         # an expired cache entry: ask the server if it changed
            etag, last_modified = revalidate.get("etag", ""), revalidate.get("last_modified", "")
        interval = self._interval(host, min_interval_s, url if respect_robots and accept == "html" else "")
        result = self._route(url, host, tier, headers, accept, etag, last_modified, interval, timeout_s, cap, retries, profile,
                             allowed_mime, fr, memo)
        if revalidate and result.not_modified:
            cached = self._from_cache(FetchResult(url=url, fetched_at=result.fetched_at), revalidate, self.clock())
            cached.note = "revalidated (304)"
            cached.headers = result.headers or cached.headers
            self._cache_write(self._cache_key(url, accept), cached)          # refresh the timestamp
            return cached
        if note:
            result.note = (result.note + "; " if result.note else "") + note
        return result

    def _interval(self, host: str, override: Optional[float], robots_url: str) -> float:
        row = self.state.get(host)
        if override is not None:
            interval = float(override)
        elif row.get("min_interval_s") is not None:
            interval = float(row["min_interval_s"])
        else:
            interval = self.min_interval_s
        if robots_url:
            delay = self.robots.crawl_delay(robots_url)
            if delay:
                interval = max(interval, min(float(delay), MAX_WAIT_S))
        return interval

    def _route(self, url: str, host: str, tier: str, headers: dict[str, str], accept: str, etag: str, last_modified: str,
               interval: float, timeout_s: float, cap: int, retries: int, profile: str, allowed_mime: tuple[str, ...],
               fr: FetchResult, memo: dict) -> FetchResult:
        row = self.state.get(host)
        now = self.clock()
        cooling = float(row.get("blocked_until_ts") or 0) > now
        prefers_browser = row.get("preferred_tier") == "browser"
        rung_ok = accept == "html" and self._rung_available(profile)

        if tier in ("browser", "window"):
            if not rung_ok:
                fr.tier = tier
                return self._fail(fr, self._rung_unavailable_text(profile), "network", self.clock())
            if cooling and tier != "window":
                return self._cooldown_result(fr, host, row, tier)
            self._wait_turn(host, interval)
            result = self._attempt_browser(url, timeout_s, fr, window=(tier == "window"))
            self._record(host, result)
            return result

        if tier == "http":
            if cooling:
                return self._cooldown_result(fr, host, row, "http")
            order = ["http"]
        else:
            if cooling and (not rung_ok or not prefers_browser or self._browser_cooldown.get(host, 0) > now):
                return self._cooldown_result(fr, host, row, "browser" if prefers_browser else "http")
            if prefers_browser and rung_ok:
                order = ["browser"]
            else:
                order = ["http", "browser"] if rung_ok else ["http"]

        self._wait_turn(host, interval)
        if order[0] == "browser":
            result = self._attempt_browser(url, timeout_s, fr, window=False)
        else:
            result = self._attempt_http(url, headers, accept, etag, last_modified, timeout_s, cap, retries, profile, allowed_mime, fr, memo)
        self._record(host, result)
        if order[0] == "http" and len(order) > 1 and result.blocked and result.block_reason in BROWSER_RETRY_REASONS:
            second = FetchResult(url=url, fetched_at=self.clock())
            browser_result = self._attempt_browser(url, timeout_s, second, window=False)
            self._record(host, browser_result)
            if browser_result.ok or browser_result.blocked:
                return browser_result
            result.error = f"{result.error}; browser fallback failed: {browser_result.error}"
        return result

    # ------------------------------------------------------------------ http rung
    def _attempt_http(self, url: str, headers: dict[str, str], accept: str, etag: str, last_modified: str, timeout_s: float, cap: int,
                      retries: int, profile: str, allowed_mime: tuple[str, ...], fr: FetchResult, memo: dict) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent, "Accept": ACCEPT[accept], "Accept-Language": self.accept_language,
                           "Accept-Encoding": "gzip, deflate"}
        request_headers.update(headers)
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        began = self.clock()
        attempt = 0
        while True:
            fresh = FetchResult(url=fr.url, fetched_at=fr.fetched_at)
            self._fetch_raw(url, request_headers, timeout_s, cap, fresh, profile, memo, accept, allowed_mime)
            transient = (not fresh.status and fresh.error_kind in ("timeout", "refused", "reset", "network")) or \
                        (fresh.status >= 500 and not fresh.headers.get("retry-after"))
            if transient and attempt < retries:
                attempt += 1
                self.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            fr = fresh
            break
        fr.elapsed_ms = int((self.clock() - began) * 1000)
        if fr.error and not fr.status:
            return fr
        if fr.status == 304:
            fr.not_modified = True
            fr.ok = True
            fr.error = fr.error_kind = ""
            return fr
        if fr.error_kind == "content":
            return fr
        reason = detect_block(fr.status, fr.text, fr.headers, fr.final_url or fr.url) if fr.text or fr.status else ""
        if accept == "json" and reason in (HTTP_403, LOGIN):
            reason = ""                                    # an API refusing a key is an error, not an anti-bot wall
        apply_block(fr, reason)
        if accept == "any" and 200 <= fr.status < 400 and not reason and fr.body:
            fr.ok, fr.error, fr.error_kind = True, "", ""
        return fr

    def _fetch_raw(self, url: str, headers: dict[str, str], timeout_s: float, cap: int, fr: FetchResult, profile: str, memo: dict,
                   accept: str = "html", allowed_mime: tuple[str, ...] = ()) -> FetchResult:
        """One logical GET with manual, policy-checked, pinned redirects and a body cap. Fills ``fr``; never raises."""
        current = url
        headers = dict(headers)
        try:
            for _hop in range(self.max_redirects + 1):
                try:
                    ips = self._ips(current, profile, memo)
                except safety.PolicyError as error:
                    fr.error = ("redirect refused: " if _hop else "") + error.reason
                    fr.error_kind = error.kind
                    fr.final_url = current
                    return fr
                response, closer = self._open(current, headers, timeout_s, ips)
                try:
                    status = response.status_code
                    location = response.headers.get("location")
                    if status in _REDIRECTS and location:
                        target = urljoin(current, location.strip())
                        fr.redirects.append(target)
                        if urlsplit(target).hostname != urlsplit(current).hostname:
                            for name in list(headers):
                                if name.lower() in _SENSITIVE:
                                    del headers[name]
                        current = target
                        continue
                    return self._finish(response, current, cap, fr, accept, allowed_mime)
                finally:
                    closer()
            fr.error = f"too many redirects (> {self.max_redirects})"
            fr.error_kind = "redirects"
        except _Unsupported as error:
            fr.error = f"unsupported content-encoding {error}"
            fr.error_kind = "content"
        except Exception as error:                                          # noqa: BLE001 - classified, not raised
            kind, detail = classify_error(error, urlsplit(current).hostname or "", timeout_s)
            fr.error, fr.error_kind = detail, kind
        return fr

    def _finish(self, response: Any, current: str, cap: int, fr: FetchResult, accept: str, allowed_mime: tuple[str, ...]) -> FetchResult:
        fr.final_url = current
        fr.status = response.status_code
        fr.headers = {k.lower(): v for k, v in response.headers.items()}
        fr.content_type = fr.headers.get("content-type", "")
        fr.etag = fr.headers.get("etag", "")
        fr.last_modified = fr.headers.get("last-modified", "")
        if fr.status == 304:
            return fr
        ctype = fr.content_type.lower().split(";")[0].strip()
        if allowed_mime and ctype and not any(ctype.startswith(p.lower()) for p in allowed_mime):
            fr.error, fr.error_kind = f"unexpected content type {ctype}", "content"
            return fr
        if accept != "any" and ctype and ctype.startswith(_BINARY_TYPES) and not ctype.endswith(("+json", "+xml")):
            fr.error, fr.error_kind = f"not a text document (content-type {ctype})", "content"
            return fr
        body, truncated = _read_body(response, cap)
        fr.truncated = truncated
        if accept == "any":
            fr.body = body
            if not ctype or ctype.startswith(_TEXTY) or ctype.endswith(("+json", "+xml")):
                fr.text = decode_body(body, fr.content_type)
        else:
            fr.text = decode_body(body, fr.content_type)
        return fr

    # ------------------------------------------------------------------ connections
    def _ips(self, url: str, profile: str, memo: dict) -> list[str]:
        parts = urlsplit(url)
        key = ((parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80))
        if key in memo:
            problem = safety.check_url(url, profile, lambda h, p: memo[key])    # re-validate the URL shape, reuse the answer
            if problem:
                raise safety.PolicyError(problem, url)
            return memo[key]
        ips = safety.resolve_public(url, profile, self.resolver)
        memo[key] = ips
        return ips

    def _shared_client(self) -> Any:
        import httpx
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(transport=self.transport, follow_redirects=False)
            return self._client

    def _open(self, url: str, headers: dict[str, str], timeout_s: float, ips: list[str]) -> tuple[Any, Callable[[], None]]:
        """Send a GET and return ``(streaming response, closer)``. Tries each checked address on connect failures."""
        try:
            import httpx
        except ImportError as error:                                       # pragma: no cover
            raise missing_dependency("httpx", "fetching web pages") from error
        timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))
        if self.transport is not None or not self.pin:
            client = self._shared_client() if self.transport is not None else httpx.Client(follow_redirects=False)
            response = client.send(client.build_request("GET", url, headers=headers, timeout=timeout), stream=True)

            def close() -> None:
                response.close()
                if self.transport is None:
                    client.close()
            return response, close
        last: Optional[BaseException] = None
        for ip in ips[:3]:
            client = httpx.Client(transport=safety.pinned_transport(ip), follow_redirects=False, trust_env=False)
            try:
                response = client.send(client.build_request("GET", url, headers=headers, timeout=timeout), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                last = error
                client.close()
                continue
            except BaseException:
                client.close()
                raise

            def close(response: Any = response, client: Any = client) -> None:
                response.close()
                client.close()
            return response, close
        raise last or httpx.ConnectError("no address to connect to")

    def _fetch_robots(self, url: str) -> tuple[int, str, str]:
        probe = FetchResult(url=url)
        try:
            memo: dict = {}
            self._fetch_raw(url, {"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.5", "Accept-Encoding": "gzip, deflate"},
                            ROBOTS_TIMEOUT_S, ROBOTS_MAX_BYTES, probe, self.profile, memo, "any")
        except Exception as error:                                          # noqa: BLE001
            return 0, "", str(error)
        if probe.error and not probe.status:
            return 0, "", probe.error
        return probe.status, probe.text, ""

    # ------------------------------------------------------------------ browser rung
    def _rung(self) -> Any:
        if self._browser is False:
            return None
        if self._browser is None:
            with self._browser_lock:
                if self._browser is None:
                    if self._browser_profile_dir is None:
                        self._browser = False
                    else:
                        from .browser import BrowserRung
                        self._browser = BrowserRung(self._browser_profile_dir)
                        self._browser_owned = True
        return self._browser or None

    def _rung_available(self, profile: str) -> bool:
        if self.offline:
            return False
        rung = self._rung()
        try:
            return bool(rung is not None and rung.available())
        except Exception:                                                   # noqa: BLE001
            return False

    def _rung_unavailable_text(self, profile: str) -> str:
        rung = self._rung()
        if rung is None:
            return "the browser rung is not configured (give the Fetcher a browser or a browser_profile_dir)"
        try:
            return getattr(rung, "unavailable_reason", lambda: "")() or "the browser rung is unavailable"
        except Exception as error:                                          # noqa: BLE001
            return f"the browser rung is unavailable: {error}"

    def _attempt_browser(self, url: str, timeout_s: float, fr: FetchResult, *, window: bool) -> FetchResult:
        rung = self._rung()
        if rung is None:
            fr.tier = "window" if window else "browser"
            return self._fail(fr, self._rung_unavailable_text(self.profile), "network", self.clock())
        result = rung.fetch_window(url, timeout_s=timeout_s) if window else rung.fetch(url, timeout_s=timeout_s)
        result.tier = "window" if window else "browser"
        return result

    # ------------------------------------------------------------------ host state
    def _host_lock(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def _wait_turn(self, host: str, interval: float) -> None:
        last = float(self.state.get(host).get("last_fetch_ts") or 0)
        if last and interval > 0:
            wait = last + interval - self.clock()
            if wait > 0:
                self.sleep(min(wait, MAX_WAIT_S))
        self.state.update(host, last_fetch_ts=self.clock())

    def _record(self, host: str, fr: FetchResult) -> None:
        now = self.clock()
        row = self.state.get(host)
        cols: dict[str, Any] = {"last_fetch_ts": now}
        if fr.ok:
            cols.update(ok_count=int(row.get("ok_count") or 0) + 1, blocked_until_ts=0.0, block_reason="")
            if fr.tier == "browser":
                cols["preferred_tier"] = "browser"               # the browser got through: use it first next time
            self._browser_cooldown.pop(host, None)
            self.state.update(host, **cols)
            return
        cols["fail_count"] = int(row.get("fail_count") or 0) + 1
        retry_after = _retry_after(fr.headers.get("retry-after", ""), now)
        if fr.blocked or (fr.status in (429, 503) and retry_after is not None):
            cooldown = self.block_cooldown_s
            if fr.status in (429, 503) and retry_after is not None:
                cooldown = retry_after
            cooldown = min(max(cooldown, 1.0), MAX_COOLDOWN_S)
            cols.update(blocked_until_ts=now + cooldown, block_reason=fr.block_reason or "http_5xx")
            if fr.tier == "browser":
                self._browser_cooldown[host] = now + cooldown
        self.state.update(host, **cols)

    def _cooldown_result(self, fr: FetchResult, host: str, row: dict[str, Any], rung: str) -> FetchResult:
        remaining = max(0, int(float(row.get("blocked_until_ts") or 0) - self.clock()))
        reason = row.get("block_reason") or "http_403"
        fr.tier = rung
        fr.blocked = True
        fr.block_reason = reason
        fr.error = (f"blocked: {block_hint(reason)}; not retrying for {max(1, remaining // 60)} more minute(s) "
                    "(wait, or open the site in a browser to resolve it)")
        fr.error_kind = "blocked"
        return fr

    # ------------------------------------------------------------------ cache
    def _cache_key(self, url: str, accept: str) -> str:
        return hashlib.sha256(f"{accept}\n{url}".encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_read(self, key: str) -> Optional[dict[str, Any]]:
        if not key or not self.cache_dir:
            return None
        try:
            entry = json.loads(self._cache_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return entry if isinstance(entry, dict) and "text" in entry else None

    def _cache_write(self, key: str, fr: FetchResult) -> None:
        if not key or not self.cache_dir:
            return
        entry = {"url": fr.url, "final_url": fr.final_url, "status": fr.status, "content_type": fr.content_type, "etag": fr.etag,
                 "last_modified": fr.last_modified, "text": fr.text, "ts": self.clock()}
        path = self._cache_path(key)
        with suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(tmp, path)

    def _from_cache(self, fr: FetchResult, entry: dict[str, Any], now: float) -> FetchResult:
        fr.final_url = entry.get("final_url") or fr.url
        fr.status = int(entry.get("status") or 200)
        fr.content_type = entry.get("content_type", "")
        fr.text = entry.get("text", "")
        fr.etag = entry.get("etag", "")
        fr.last_modified = entry.get("last_modified", "")
        fr.headers = {"content-type": fr.content_type} if fr.content_type else {}
        fr.ok = True
        fr.from_cache = True
        fr.tier = "cache"
        fr.fetched_at = float(entry.get("ts", now))
        return fr

    def clear_cache(self) -> int:
        removed = 0
        if self.cache_dir and self.cache_dir.is_dir():
            for f in self.cache_dir.glob("*/*.json"):
                with suppress(OSError):
                    f.unlink()
                    removed += 1
        return removed

    def cache_stats(self) -> dict[str, Any]:
        files = list(self.cache_dir.glob("*/*.json")) if self.cache_dir and self.cache_dir.is_dir() else []
        size = 0
        for f in files:
            with suppress(OSError):
                size += f.stat().st_size
        return {"entries": len(files), "bytes": size, "ttl_s": self.cache_ttl_s, "hits": self.hits, "misses": self.misses,
                "dir": str(self.cache_dir or "")}


# ---------------------------------------------------------------------------------------------- JSON API client
class ApiError(HoardLinkError):
    """A JSON API call failed. ``kind``: ``offline`` | ``rate_limited`` | ``unreachable`` | ``server_error`` | ``policy``
    | ``blocked``; ``retry_after`` is the server's ``Retry-After`` text when it sent one."""

    def __init__(self, kind: str, message: str, *, status: int = 0, retry_after: Optional[str] = None):
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


@dataclass
class ApiResponse:
    status: int
    text: str
    cached: bool
    fetched_at: float
    url: str
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def json(self) -> Any:
        return json.loads(self.text)


class JsonApiClient:
    """A client for JSON APIs: an identifying User-Agent, a minimum interval between calls, a small on-disk cache,
    stale-if-error, an offline switch, one retry on network errors and 5xx, ``429`` reported with its ``Retry-After``.

    ``get`` returns an :class:`ApiResponse` (any 2xx-4xx status except 429) or raises :class:`ApiError`. Only successful
    non-HTML responses are cached (a challenge page is never cached). Not for fetching arbitrary URLs: it applies the
    ``public`` policy but is meant for a handful of fixed API hosts; use :class:`Fetcher` for pages."""

    def __init__(self, base_url: str = "", user_agent: str = "", min_interval_s: float = 0.0, ttl: float = 6 * 3600,
                 offline: bool = False, retries: int = 1, cache_dir: Any = None, *, timeout_s: float = 20.0,
                 profile: str = safety.PUBLIC, stale_if_error: bool = True, max_bytes: int = 10 * 1024 * 1024,
                 transport: Any = None, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 resolver: Optional[safety.Resolver] = None):
        self.base_url = base_url.rstrip("/")
        self.ttl = float(ttl)
        self.offline = offline
        self._fetcher = Fetcher(user_agent=user_agent or DEFAULT_USER_AGENT, profile=profile, transport=transport, clock=clock,
                                sleep=sleep, resolver=resolver, offline=offline, cache_dir=Path(cache_dir) / "http" if cache_dir else None,
                                cache_ttl_s=ttl, stale_if_error=stale_if_error, min_interval_s=min_interval_s,
                                timeout_s=timeout_s, max_bytes=max_bytes, retries=retries)

    @property
    def hits(self) -> int:
        return self._fetcher.hits

    @property
    def misses(self) -> int:
        return self._fetcher.misses

    def cache_stats(self) -> dict[str, Any]:
        return self._fetcher.cache_stats()

    def clear_cache(self) -> int:
        return self._fetcher.clear_cache()

    def close(self) -> None:
        self._fetcher.close()

    def get(self, url: str, params: Optional[Mapping[str, Any]] = None, *, ttl: Optional[float] = None, use_cache: bool = True,
            headers: Optional[Mapping[str, str]] = None, label: str = "") -> ApiResponse:
        full = url if "://" in url else f"{self.base_url}/{url.lstrip('/')}"
        who = label or urlsplit(full).hostname or "the API"
        fr = self._fetcher.get(full, params=params, accept="json", headers=headers, respect_robots=False, tier="http",
                               cache_ttl_s=(self.ttl if ttl is None else ttl) if use_cache else 0, cache_html=False)
        retry_after = fr.headers.get("retry-after") or None
        if fr.error_kind == "offline":
            raise ApiError("offline", f"{who}: offline mode, nothing cached for this request.")
        if fr.status == 429 or fr.block_reason == HTTP_429:
            raise ApiError("rate_limited", f"{who} rate limit reached.", status=429, retry_after=retry_after)
        if fr.error_kind in ("policy",):
            raise ApiError("policy", f"{who}: {fr.error}")
        if fr.error_kind == "blocked":
            raise ApiError("blocked", f"{who}: {fr.error}", status=fr.status, retry_after=retry_after)
        if not fr.status:
            raise ApiError("unreachable", f"{who} could not be reached ({fr.error}).")
        if fr.status >= 500:
            raise ApiError("server_error", f"{who} answered HTTP {fr.status}.", status=fr.status, retry_after=retry_after)
        return ApiResponse(fr.status, fr.text, fr.from_cache, fr.fetched_at, full, fr.content_type, fr.headers)

    def get_json(self, url: str, params: Optional[Mapping[str, Any]] = None, **kw: Any) -> Any:
        response = self.get(url, params, **kw)
        try:
            return response.json()
        except ValueError as error:
            raise ApiError("server_error", f"{url}: the answer is not JSON ({error})", status=response.status) from error
