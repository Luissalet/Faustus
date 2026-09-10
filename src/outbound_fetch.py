"""SSRF-guarded synchronous HTTP fetching primitives.

This module owns outbound URL classification, one-resolution-per-hop DNS
pinning, redirects, and response-body budgets.  It deliberately has no search
or content-extraction dependencies so callers outside search can reuse the
same transport boundary.

fetch() at the bottom is the brokered entry point every untrusted caller should
use: it takes an explicit trust profile instead of an ad-hoc boolean, and it is
the only place where "which addresses may this request reach" is decided.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Iterable, cast
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from src import url_safety
from src.constants import WEB_FETCH_HARD_MAX_BYTES, WEB_FETCH_SOFT_MAX_BYTES


_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_address(addr: ipaddress._BaseAddress) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or any(addr in net for net in _PRIVATE_NETWORKS)
    )


def _resolve_hostname_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except Exception:
            continue
    return out


def _public_http_url(
    url: str,
    *,
    resolver: Callable[[str], list[ipaddress._BaseAddress]] | None = None,
) -> bool:
    resolver = resolver or _resolve_hostname_ips
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip()
        if not host:
            return False
        lower = host.lower()
        if lower in ("localhost", "metadata", "metadata.google.internal"):
            return False
        if lower.endswith((".local", ".localhost", ".internal", ".lan", ".intranet")):
            return False
        try:
            return not _is_private_address(ipaddress.ip_address(host))
        except ValueError:
            pass
        addrs = resolver(host)
        return bool(addrs) and not any(_is_private_address(a) for a in addrs)
    except Exception:
        return False


def _resolve_public_ips(
    url: str,
    *,
    resolver: Callable[[str], list[ipaddress._BaseAddress]] | None = None,
) -> list[ipaddress._BaseAddress]:
    resolver = resolver or _resolve_hostname_ips
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise httpx.RequestError(f"Blocked non-public URL: {url}")
    host = (parsed.hostname or "").strip().lower()
    if host in ("localhost", "metadata", "metadata.google.internal"):
        raise httpx.RequestError(f"Blocked non-public hostname: {host}")
    try:
        ip = ipaddress.ip_address(host)
        if _is_private_address(ip):
            raise httpx.RequestError(f"Blocked non-public IP literal: {host}")
        return [ip]
    except httpx.RequestError:
        raise
    except ValueError:
        pass
    addrs = resolver(host)
    if not addrs or any(_is_private_address(a) for a in addrs):
        raise httpx.RequestError(f"Blocked non-public URL: {url}")
    return addrs


class _PinnedBackend(httpcore.NetworkBackend):
    """Network backend that connects to a pre-resolved IP."""

    def __init__(self, ip: ipaddress._BaseAddress):
        self._ip = str(ip)
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        return self._real.connect_tcp(
            self._ip, port, timeout, local_address, socket_options
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._real.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        return self._real.sleep(seconds)


_HTTPCORE_TO_HTTPX_EXC = {
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.WriteError: httpx.WriteError,
    httpcore.WriteTimeout: httpx.WriteTimeout,
}


class _PinnedTransport(httpx.BaseTransport):
    """Transport that pins every TCP connect to a pre-resolved IP."""

    def __init__(self, ip: ipaddress._BaseAddress, *, http2: bool = False):
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            http1=True,
            http2=http2,
            network_backend=_PinnedBackend(ip),
        )

    def __enter__(self):
        self._pool.__enter__()
        return self

    def __exit__(self, exc_type=None, exc_value=None, traceback=None) -> None:
        self._pool.__exit__(exc_type, exc_value, traceback)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        httpcore_req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            httpcore_resp = self._pool.handle_request(httpcore_req)
            content = b"".join(cast(Iterable[bytes], httpcore_resp.stream))
        except Exception as exc:
            mapped = _HTTPCORE_TO_HTTPX_EXC.get(type(exc))
            if mapped is not None:
                raise mapped(str(exc)) from exc
            raise

        return httpx.Response(
            status_code=httpcore_resp.status,
            headers=httpcore_resp.headers,
            content=content,
            extensions=httpcore_resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


class BodyTooLargeError(Exception):
    """The server declared a body larger than the hard fetch ceiling."""

    def __init__(self, url: str, declared_bytes: int, cap: int | None = None):
        self.url = url
        self.declared_bytes = declared_bytes
        # The broker enforces a per-profile cap below the hard ceiling, so the
        # message has to name the limit that actually fired. Leaving `cap` unset
        # keeps the original wording for the web-fetch callers.
        self.cap = WEB_FETCH_HARD_MAX_BYTES if cap is None else cap
        super().__init__(
            f"response body is {declared_bytes:,} bytes, over the "
            f"{self.cap:,}-byte {'hard cap' if cap is None else 'cap'}"
        )


class _CappedFetch:
    """Result of a size-capped streaming GET."""

    __slots__ = (
        "status_code",
        "headers",
        "content",
        "truncated",
        "declared_bytes",
        "encoding",
        "url",
    )

    def __init__(
        self,
        status_code,
        headers,
        content,
        truncated,
        declared_bytes,
        encoding,
        url,
    ):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.truncated = truncated
        self.declared_bytes = declared_bytes
        self.encoding = encoding
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code} for {self.url}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


def _get_public_url(
    url: str,
    headers: dict,
    timeout: int,
    max_redirects: int = 5,
    max_bytes: int | None = None,
    *,
    resolve_public_ips: Callable[[str], list[ipaddress._BaseAddress]] | None = None,
    transport_factory: Callable[[ipaddress._BaseAddress], httpx.BaseTransport] | None = None,
) -> _CappedFetch:
    """Capped streaming GET with SSRF-guarded, DNS-pinned redirects."""
    resolve_public_ips = resolve_public_ips or _resolve_public_ips
    transport_factory = transport_factory or _PinnedTransport
    cap = min(max_bytes or WEB_FETCH_SOFT_MAX_BYTES, WEB_FETCH_HARD_MAX_BYTES)
    current = url
    for _ in range(max_redirects + 1):
        ips = resolve_public_ips(current)
        req_headers = dict(headers or {})
        req_headers["Accept-Encoding"] = "identity"

        with httpx.Client(
            headers=req_headers,
            timeout=timeout,
            follow_redirects=False,
            transport=transport_factory(ips[0]),
        ) as client:
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return _CappedFetch(
                            response.status_code,
                            response.headers,
                            b"",
                            False,
                            None,
                            response.encoding,
                            str(response.url),
                        )
                    current = urljoin(str(response.url), location)
                    continue

                enc = (response.headers.get("content-encoding") or "").strip().lower()
                if enc and enc != "identity":
                    raise httpx.RequestError(
                        f"Refusing compressed response (Content-Encoding: {enc}) after "
                        "requesting identity: cannot bound decoded body size",
                        request=httpx.Request("GET", current),
                    )

                declared = None
                raw_len = response.headers.get("content-length")
                if raw_len and raw_len.isdigit():
                    declared = int(raw_len)

                if declared is not None and declared > WEB_FETCH_HARD_MAX_BYTES:
                    raise BodyTooLargeError(current, declared)

                chunks = []
                read = 0
                truncated = False
                for chunk in response.iter_bytes():
                    read += len(chunk)
                    if read > cap:
                        keep = cap - (read - len(chunk))
                        if keep > 0:
                            chunks.append(chunk[:keep])
                        truncated = True
                        break
                    chunks.append(chunk)

                return _CappedFetch(
                    response.status_code,
                    response.headers,
                    b"".join(chunks),
                    truncated,
                    declared,
                    response.encoding,
                    str(response.url),
                )

    raise httpx.RequestError(
        "Too many redirects", request=httpx.Request("GET", current)
    )


# ---------------------------------------------------------------------------
# Outbound broker: one destination policy for every untrusted HTTP request
# ---------------------------------------------------------------------------
#
# Faustus grew three separate ideas of "safe destination" -- the private-network
# table above, url_safety._classify, and url_security._blocked_ip -- and they
# disagreed at the edges (shared address space, IPv4-mapped v6, .internal
# names). Worse, several callers validated a *hostname* and then handed that
# name to a fresh httpx client, which resolved it a second time. The gap
# between the two resolutions is a DNS-rebinding window: the name answers with
# a public address for the check and with 169.254.169.254 for the connect,
# while the request still carries whatever credentials the caller attached.
#
# Everything untrusted goes through fetch() below. It resolves once, connects
# to the address it validated, keeps Host/SNI on the original name so vhost
# routing and certificate validation are unaffected, and re-runs the whole
# judgement on every redirect hop.

PUBLIC_UNTRUSTED = "public_untrusted"
PROVIDER_RESULT = "provider_result"
OPERATOR_LOCAL = "operator_local"
INTERNAL_SERVICE = "internal_service"

TRUST_PROFILES = (PUBLIC_UNTRUSTED, PROVIDER_RESULT, OPERATOR_LOCAL, INTERNAL_SERVICE)

# The two profiles that may reach an address inside the deployment. Reaching a
# LAN box is a legitimate Faustus feature, so the answer is not to forbid it but
# to make it impossible to acquire by accident: these profiles are inert unless
# the caller also passes allow_local=True, and that flag must be derived from
# operator configuration -- never from a model, a tool argument, or a URL a
# provider handed back. Mixing the operator's trust into a path that only ever
# meant to reach the public internet is the actual defect B-019 describes.
_LOCAL_PROFILES = frozenset({OPERATOR_LOCAL, INTERNAL_SERVICE})

ZONE_PUBLIC = "public"
ZONE_LOCAL = "local"

# Names that cannot denote a public destination. A public-profile fetch is
# refused on the name alone; resolving first would hand the decision to a
# resolver that is free to answer differently on the next query.
_INTERNAL_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".intranet")

# 32 + MAX_WBITS: let zlib sniff a gzip or a zlib header rather than guessing
# from the Content-Encoding string, which servers spell inconsistently.
_ZLIB_AUTO_WBITS = 47
_DECODABLE_ENCODINGS = ("gzip", "x-gzip", "deflate")

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class OutboundPolicyError(httpx.RequestError):
    """A destination or response the outbound policy refuses to touch.

    Subclasses httpx.RequestError so callers that already funnel transport
    failures into a single handler keep behaving; catch it specifically where a
    policy refusal has to be reported differently from a network failure -- the
    image tools do, because "the provider handed us a metadata URL" and "the CDN
    timed out" deserve different answers.
    """


@dataclass(frozen=True)
class OutboundLimits:
    """Per-profile ceilings applied to every hop of a brokered fetch.

    Ceilings, not defaults: a caller may ask for less, never for more, so a
    profile is a bound on the damage a compromised caller can do rather than a
    suggestion it can talk its way past.
    """

    max_bytes: int
    timeout: float
    max_redirects: int
    max_decompression_ratio: float
    allowed_mime: tuple = ()


# provider_result and operator_local carry the image budget: a 1024x1536 render
# lands well above the 2 MB text budget, and unlike a web page it is useless
# truncated. internal_service takes no redirects at all -- a Faustus-internal
# service that answers 302 is misconfigured, not a hop to follow.
PROFILE_LIMITS = {
    PUBLIC_UNTRUSTED: OutboundLimits(WEB_FETCH_SOFT_MAX_BYTES, 20.0, 5, 20.0),
    PROVIDER_RESULT: OutboundLimits(8_000_000, 60.0, 3, 20.0),
    OPERATOR_LOCAL: OutboundLimits(8_000_000, 60.0, 3, 20.0),
    INTERNAL_SERVICE: OutboundLimits(WEB_FETCH_SOFT_MAX_BYTES, 20.0, 0, 20.0),
}


def _unwrap_mapped(addr: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_local_address(addr: ipaddress._BaseAddress) -> bool:
    """Zone test: inside the deployment rather than on the public internet.

    Link-local and the other always-refused ranges are deliberately not a zone;
    _classify_address rejects them before anything asks which side they are on.
    """
    addr = _unwrap_mapped(addr)
    return addr.is_private or addr.is_loopback


def _classify_address(addr: ipaddress._BaseAddress, *, profile: str) -> str | None:
    """Rejection reason for one resolved address under `profile`, or None.

    The private/link-local/metadata judgement is url_safety._classify's, reused
    rather than restated: three copies of that table drifting apart is the
    fragmentation this broker exists to end. It also unwraps IPv4-mapped IPv6,
    so ::ffff:169.254.169.254 cannot slip past a check that only reads v4.
    """
    local_ok = profile in _LOCAL_PROFILES
    reason = url_safety._classify(addr, block_private=not local_ok)
    if reason:
        return reason
    if profile == INTERNAL_SERVICE and not _unwrap_mapped(addr).is_loopback:
        return f"internal_service may only reach loopback, got {addr}"
    return None


def _hostname_rejection(host: str, *, profile: str) -> str | None:
    if not host:
        return "URL has no host"
    if profile in _LOCAL_PROFILES:
        # homeassistant.local is the normal spelling of an operator's own box.
        return None
    lower = host.lower()
    if lower in _INTERNAL_HOSTNAMES or lower.endswith(_INTERNAL_SUFFIXES):
        return f"internal hostname blocked: {host}"
    return None


def _coerce_addresses(raw) -> list[ipaddress._BaseAddress]:
    """Normalise resolver output to de-duplicated address objects.

    Accepts strings or address objects because the two resolvers in the codebase
    return different things, and drops duplicates because getaddrinfo(host, None)
    has no socktype filter -- a single-homed host arrives once per socktype.
    """
    out: list[ipaddress._BaseAddress] = []
    seen = set()
    for item in raw or []:
        if isinstance(item, str):
            try:
                item = ipaddress.ip_address(item.split("%")[0])  # strip zone id
            except ValueError:
                continue
        elif not isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _require_profile(profile: str, allow_local: bool) -> OutboundLimits:
    if profile not in TRUST_PROFILES:
        raise OutboundPolicyError(f"Unknown outbound trust profile: {profile!r}")
    if profile in _LOCAL_PROFILES and not allow_local:
        raise OutboundPolicyError(
            f"Trust profile {profile} reaches inside the deployment and stays "
            "closed until an operator-configured caller passes allow_local=True"
        )
    if allow_local and profile not in _LOCAL_PROFILES:
        # Fail loudly instead of silently ignoring it: a caller that believes it
        # unlocked the LAN and did not would otherwise ship broken either way.
        raise OutboundPolicyError(
            f"allow_local is meaningless for {profile}: public profiles never "
            "reach local addresses"
        )
    return PROFILE_LIMITS[profile]


def classify_destination(
    url: str,
    *,
    profile: str,
    allow_local: bool = False,
    resolver: Callable[[str], list] | None = None,
) -> tuple[str, list[ipaddress._BaseAddress]]:
    """Resolve `url` once and return the (zone, addresses) it may reach.

    The address list is not advisory: it is what the caller must connect to.
    Handing the hostname back to a client that resolves it again is precisely
    the rebinding bug this function exists to remove.
    """
    _require_profile(profile, allow_local)
    resolver = resolver or _resolve_hostname_ips
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise OutboundPolicyError(f"Blocked non-HTTP(S) URL: {url}")
    host = (parsed.hostname or "").strip()
    reason = _hostname_rejection(host, profile=profile)
    if reason:
        raise OutboundPolicyError(f"Blocked URL {url}: {reason}")

    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        addrs = _coerce_addresses(resolver(host))
    if not addrs:
        raise OutboundPolicyError(f"Blocked URL {url}: host does not resolve")

    for addr in addrs:
        reason = _classify_address(addr, profile=profile)
        if reason:
            raise OutboundPolicyError(f"Blocked URL {url}: {reason}")

    zones = {ZONE_LOCAL if _is_local_address(a) else ZONE_PUBLIC for a in addrs}
    if len(zones) > 1:
        # A name answering with both a public and a private record is a
        # rebinding setup that does not even need timing: pinning would pick one
        # arbitrarily, so refuse rather than let the resolver order decide.
        raise OutboundPolicyError(
            f"Blocked URL {url}: host straddles public and local addresses"
        )
    return zones.pop(), addrs


def _read_plain_body(response, url: str, *, cap: int) -> bytes:
    chunks = []
    read = 0
    for chunk in response.iter_bytes():
        read += len(chunk)
        if read > cap:
            # Refuse rather than truncate: the broker's callers save binaries,
            # and half a PNG on disk is a worse outcome than a failed download.
            raise BodyTooLargeError(url, read, cap)
        chunks.append(chunk)
    return b"".join(chunks)


def _read_decompressed_body(response, url: str, *, cap: int, max_ratio: float) -> bytes:
    """Inflate a body the server compressed anyway, bounded on both axes.

    Every hop asks for Accept-Encoding: identity, so arriving here means the
    server ignored that. Refusing outright would break those servers; inflating
    unbounded is a decompression bomb, because 20 MB of one repeated byte gzips
    to a few kilobytes and the size on the wire then says nothing about the size
    in memory. So the decoded body is held to both the byte cap and a ratio of
    what actually crossed the wire.
    """
    decompressor = zlib.decompressobj(_ZLIB_AUTO_WBITS)
    compressed = 0
    out = bytearray()
    # iter_raw, not iter_bytes: httpx would inflate the body itself, unbounded,
    # before this function ever saw a byte of it.
    for chunk in response.iter_raw():
        compressed += len(chunk)
        try:
            out += decompressor.decompress(chunk, cap + 1 - len(out))
        except zlib.error as exc:
            raise OutboundPolicyError(
                f"Blocked response from {url}: undecodable compressed body ({exc})"
            ) from exc
        if len(out) > cap:
            raise BodyTooLargeError(url, len(out), cap)
        if compressed and len(out) > compressed * max_ratio:
            raise OutboundPolicyError(
                f"Blocked response from {url}: decompression ratio "
                f"{len(out) / compressed:.0f}x exceeds the {max_ratio:g}x limit"
            )
    return bytes(out)


def _read_capped_body(response, url: str, *, cap: int, max_ratio: float, allowed_mime):
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    # An absent Content-Type is allowed through: plenty of object stores omit it
    # on presigned URLs, and refusing there would break real downloads without
    # closing anything -- the byte cap, not the label, is what bounds the risk.
    if allowed_mime and content_type and not content_type.startswith(tuple(allowed_mime)):
        raise OutboundPolicyError(
            f"Blocked response from {url}: content type {content_type!r} is "
            f"outside the allowed set {tuple(allowed_mime)}"
        )

    declared = None
    raw_len = (response.headers.get("content-length") or "").strip()
    if raw_len.isdigit():
        declared = int(raw_len)
    if declared is not None and declared > cap:
        raise BodyTooLargeError(url, declared, cap)

    encoding = (response.headers.get("content-encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        body = _read_plain_body(response, url, cap=cap)
    elif encoding in _DECODABLE_ENCODINGS:
        body = _read_decompressed_body(response, url, cap=cap, max_ratio=max_ratio)
    else:
        raise OutboundPolicyError(
            f"Blocked response from {url}: cannot bound the decoded size of "
            f"Content-Encoding {encoding!r}"
        )
    return _CappedFetch(
        response.status_code,
        response.headers,
        body,
        False,
        declared,
        response.encoding,
        str(response.url),
    )


class _CoreByteStream(httpx.SyncByteStream):
    """Adapts an httpcore response stream to httpx without buffering it.

    _PinnedTransport above joins the whole body inside handle_request, which the
    search fetcher can live with but the broker cannot, twice over: a byte cap
    checked only after the download finished is not a cap, and httpx inflates a
    compressed body while constructing the Response, well before the ratio guard
    could see it. Streaming puts both limits in front of the bytes.
    """

    def __init__(self, core_response):
        self._core_response = core_response

    def __iter__(self):
        try:
            for part in self._core_response.stream:
                yield part
        except Exception as exc:
            mapped = _HTTPCORE_TO_HTTPX_EXC.get(type(exc))
            if mapped is not None:
                raise mapped(str(exc)) from exc
            raise

    def close(self) -> None:
        self._core_response.close()


class _StreamingPinnedTransport(_PinnedTransport):
    """_PinnedTransport that hands the body back unread, as a stream."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            core_resp = self._pool.handle_request(core_req)
        except Exception as exc:
            mapped = _HTTPCORE_TO_HTTPX_EXC.get(type(exc))
            if mapped is not None:
                raise mapped(str(exc)) from exc
            raise
        return httpx.Response(
            status_code=core_resp.status,
            headers=core_resp.headers,
            stream=_CoreByteStream(core_resp),
            extensions=core_resp.extensions,
        )


# ---------------------------------------------------------------------------
# WEB-02: signals on a read result — duplicate content, stale content
# ---------------------------------------------------------------------------
# The SSRF/redirect/size guarding above answers "was this safe to fetch".
# These two are the other half the backlog asks for: "was this worth
# fetching" — a page that repeats content the run already has under a
# different URL, or a page whose own HTTP headers say it has not changed in
# years. Both are pure functions of data the caller already has (a body's
# text, a response's headers) so a UI or a research loop can call them
# without this module knowing anything about sessions or research runs.

#: Past this, a page is flagged stale even with no other signal to weigh it
#: against — two years is long enough that "still true" needs re-checking
#: for anything time-sensitive, short enough that a reference page (a spec,
#: an RFC) legitimately this old is still just one flag among many a caller
#: can choose to ignore.
STALE_AFTER_DAYS = 730

_HTTP_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %Z",   # RFC 7231 (the only one a compliant server sends)
    "%A, %d-%b-%y %H:%M:%S %Z",   # RFC 850 (obsolete but still seen)
    "%a %b %d %H:%M:%S %Y",       # asctime (ditto)
)


def _parse_http_date(value: "str | None"):
    if not value:
        return None
    from datetime import datetime, timezone

    raw = str(value).strip()
    for fmt in _HTTP_DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def staleness_from_headers(
    headers,
    *,
    now=None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> "dict | None":
    """A staleness signal from one response's headers, or `None` when the
    response carries nothing to judge it by.

    Prefers `Last-Modified` (when the server states it, it is telling the
    truth about the resource, not about a cache) and falls back to `Date`
    minus `Age` (a CDN's own "how long have I been holding this" number) when
    `Last-Modified` is absent. Returns
    `{"stale": bool, "age_days": float, "basis": "last-modified"|"age"}` —
    never raises, and an unparseable/missing date is `None`, not a false
    "fresh".
    """
    try:
        get = headers.get if hasattr(headers, "get") else (lambda k: None)
        from datetime import datetime, timedelta, timezone

        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)

        last_modified = _parse_http_date(get("last-modified") or get("Last-Modified"))
        if last_modified is not None:
            age_days = max(0.0, (clock - last_modified).total_seconds() / 86400.0)
            return {"stale": age_days > stale_after_days, "age_days": round(age_days, 1),
                    "basis": "last-modified"}

        date_header = _parse_http_date(get("date") or get("Date"))
        age_header = get("age") or get("Age")
        if date_header is not None and age_header is not None:
            try:
                age_seconds = float(str(age_header).strip())
            except (TypeError, ValueError):
                return None
            effective = date_header - timedelta(seconds=age_seconds)
            age_days = max(0.0, (clock - effective).total_seconds() / 86400.0)
            return {"stale": age_days > stale_after_days, "age_days": round(age_days, 1),
                    "basis": "age"}
    except Exception:  # noqa: BLE001 - a staleness signal is never load-bearing
        return None
    return None


def content_fingerprint(text: "str | bytes") -> str:
    """A stable identity for page content, independent of the URL it came
    from — two different URLs serving the same bytes fingerprint the same.

    Whitespace is collapsed first: a syndicated copy that differs only by
    reflow (different line wrapping, trailing spaces) must still match —
    that is precisely the "duplicate page under a different URL" case this
    exists to catch, not only byte-identical mirrors.
    """
    import hashlib
    import re as _re

    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    normalized = _re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def find_duplicate(text: "str | bytes", seen: "dict[str, str]") -> "str | None":
    """The URL already recorded under this content's fingerprint in `seen`,
    or `None` when it is new content. Never mutates `seen` — recording a new
    fingerprint is the caller's decision (it knows the current URL to store),
    this only ever answers the lookup."""
    if not text:
        return None
    return seen.get(content_fingerprint(text))


def fetch(
    url: str,
    *,
    profile: str,
    allow_local: bool = False,
    method: str = "GET",
    headers: dict | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    allowed_mime: tuple | None = None,
    resolver: Callable[[str], list] | None = None,
    transport_factory: Callable[[ipaddress._BaseAddress], httpx.BaseTransport] | None = None,
) -> _CappedFetch:
    """Fetch `url` under `profile`, resolving once per hop and pinning the connect.

    Redirects are followed by hand so each hop is re-classified instead of
    inheriting the first hop's verdict, and a hop that crosses zones is refused:
    a public URL that redirects into the LAN is the shape of every SSRF report,
    and a local URL that redirects out to the internet leaks whatever the LAN
    service returned. The whole call shares one time budget, so a chain of slow
    hops cannot multiply the caller's timeout by the redirect limit.
    """
    limits = _require_profile(profile, allow_local)
    transport_factory = transport_factory or _StreamingPinnedTransport
    cap = min(max_bytes or limits.max_bytes, limits.max_bytes, WEB_FETCH_HARD_MAX_BYTES)
    budget = min(timeout or limits.timeout, limits.timeout)
    hops = limits.max_redirects if max_redirects is None else min(max_redirects, limits.max_redirects)
    mime = allowed_mime if allowed_mime is not None else limits.allowed_mime

    deadline = time.monotonic() + budget
    zone: str | None = None
    current = url
    visited: set[str] = set()

    for _ in range(hops + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundPolicyError(
                f"Outbound time budget of {budget:g}s exhausted fetching {url}"
            )
        hop_zone, addrs = classify_destination(
            current, profile=profile, allow_local=allow_local, resolver=resolver
        )
        if zone is None:
            zone = hop_zone
        elif hop_zone != zone:
            raise OutboundPolicyError(
                f"Blocked mixed-zone redirect: {url} started in the {zone} zone "
                f"and {current} is {hop_zone}"
            )
        if current in visited:
            raise OutboundPolicyError(f"Blocked redirect loop at {current}")
        visited.add(current)

        req_headers = dict(headers or {})
        req_headers["Accept-Encoding"] = "identity"
        with httpx.Client(
            headers=req_headers,
            timeout=remaining,
            follow_redirects=False,
            transport=transport_factory(addrs[0]),
        ) as client:
            with client.stream(method, current) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return _CappedFetch(
                            response.status_code,
                            response.headers,
                            b"",
                            False,
                            None,
                            response.encoding,
                            str(response.url),
                        )
                    current = urljoin(str(response.url), location)
                    continue
                return _read_capped_body(
                    response,
                    current,
                    cap=cap,
                    max_ratio=limits.max_decompression_ratio,
                    allowed_mime=mime,
                )

    raise OutboundPolicyError(f"Too many redirects (limit {hops}) starting at {url}")


def profile_for_configured_endpoint(*, block_private: bool) -> tuple[str, bool]:
    """Trust profile for a URL an operator configured, as (profile, allow_local).

    The single place that turns a deployment's "may this box talk to the LAN?"
    switch into a profile, so integrations, embeddings and image endpoints stop
    each spelling the same decision differently. It takes a boolean the caller
    read from configuration, which is what keeps the local profile unreachable
    from model output: no string a model emits can reach this argument.
    """
    if block_private:
        return PUBLIC_UNTRUSTED, False
    return OPERATOR_LOCAL, True
