"""SSRF policy shared by every outbound request: which URLs may be fetched and which addresses may be reached.

Three profiles say how much of the machine's network a caller may touch:

``PUBLIC``          only globally routable unicast addresses (pages and APIs on the internet). The default.
``OPERATOR_LOCAL``  also loopback, private (RFC 1918, unique-local) and shared/CGNAT addresses: endpoints the
                    *operator* configured (a local SearXNG, a LAN printer, a tailnet host). Cloud metadata,
                    link-local, multicast, reserved and unspecified addresses stay refused.
``INTERNAL``        loopback only: the hub talking to apps on the same machine.

Every profile refuses non-``http(s)`` schemes (``file:``, ``data:``, ``javascript:`` ...), credentials in the URL,
control characters, backslashes and the cloud metadata host names. ``PUBLIC`` additionally refuses single-label
and local-looking names (``.local``, ``.lan``, ``.internal``, ``.localhost`` ...) and every IPv4 literal that is not
written as four plain decimal numbers (``2130706433``, ``0x7f.1``, ``017700000001``). An IPv4 address tunnelled in
IPv6 (``::ffff:127.0.0.1``, 6to4, NAT64) is judged by the IPv4 part; Teredo is refused.

Time of check versus time of use: resolving a name, checking the answer and then letting the HTTP client resolve it
again lets a hostile DNS server answer differently the second time. :func:`resolve_public` returns the checked
addresses and :func:`pinned_transport` connects to exactly one of them (TLS server name and ``Host`` header keep the
original name), which closes that gap.

The Node twin is ``js/hoard-commons/web.js`` (``checkUrl``, ``classifyIp``), checked against
``tests/vectors/web_safety.json``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from ..errors import HoardLinkError, missing_dependency

__all__ = [
    "PUBLIC", "OPERATOR_LOCAL", "INTERNAL", "PROFILES", "UNRESOLVABLE_PREFIX", "PolicyError", "Resolver",
    "default_resolver", "classify_ip", "check_url", "resolve_public", "pinned_transport", "parse_loose_ipv4",
]

PUBLIC = "public"
OPERATOR_LOCAL = "operator_local"
INTERNAL = "internal"
PROFILES = (PUBLIC, OPERATOR_LOCAL, INTERNAL)

# The reason text starts with this when the host simply does not resolve; a fetcher reports it as a DNS failure
# rather than as an unsafe URL.
UNRESOLVABLE_PREFIX = "unresolvable host"

Resolver = Callable[[str, int], Iterable[str]]

MAX_URL_LEN = 2048

METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                            "instance-data.ec2.internal", "metadata.azure.com"})
_METADATA_ADDRS = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "fd00:ec2::254"))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home.arpa", ".intranet", ".corp",
                   ".home", ".private")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_TEREDO = ipaddress.ip_network("2001::/32")
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.I)
_HOSTNAME = re.compile(r"^[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?)*$")


class PolicyError(HoardLinkError):
    """The URL (or the address it resolves to) is not allowed under the profile. ``reason`` says why and
    ``kind`` is ``"policy"`` or ``"dns"`` (the name does not resolve)."""

    def __init__(self, reason: str, url: str = "", kind: str = "policy"):
        self.reason = reason
        self.url = url
        self.kind = kind
        super().__init__(reason)


def default_resolver(host: str, port: int) -> list[str]:
    """Every address (all families) ``host`` resolves to."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


# ---- IP classification ------------------------------------------------------------------------

def parse_loose_ipv4(host: str) -> Optional[ipaddress.IPv4Address]:
    """What ``inet_aton`` makes of ``host`` (1 to 4 parts, each decimal, ``0``-octal or ``0x`` hex, the last part
    filling the remaining bytes): ``2130706433``, ``0x7f.1`` and ``017700000001`` are all 127.0.0.1. ``None`` when
    ``host`` is not such a literal."""
    h = host.strip().lower()
    if not _NUMERIC_HOST.match(h):
        return None
    nums: list[int] = []
    for part in h.split("."):
        try:
            if part.startswith("0x"):
                nums.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    last, head = nums[-1], nums[:-1]
    if any(n > 255 for n in head) or last >= 256 ** (4 - len(head)):
        return None
    value = last
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _nets(*specs: str) -> tuple[Any, ...]:
    return tuple(ipaddress.ip_network(s) for s in specs)


# Explicit tables (not ``ipaddress.is_private``/``is_global``, whose answers moved between Python releases), so the
# verdict is the same on every supported Python and in the Node twin (which generates its tables from these).
V4_LINK_LOCAL = _nets("169.254.0.0/16")
V4_MULTICAST = _nets("224.0.0.0/4")
V4_PRIVATE = _nets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
V4_RESERVED = _nets("0.0.0.0/8", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24",
                    "203.0.113.0/24", "240.0.0.0/4")
V6_LINK_LOCAL = _nets("fe80::/10")
V6_MULTICAST = _nets("ff00::/8")
V6_PRIVATE = _nets("fc00::/7")
V6_RESERVED = _nets("64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16", "fec0::/10")
V6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")        # nothing else is allocated; the rest is refused


def _within(ip: Any, nets: tuple[Any, ...]) -> bool:
    return any(ip in n for n in nets)


def _unwrap(ip: Any) -> Any:
    """The IPv4 address hidden in an IPv6 one (mapped, compatible, 6to4, NAT64), else the address itself."""
    if isinstance(ip, ipaddress.IPv6Address):
        n = int(ip)
        if n >> 32 == 0xFFFF:                            # ::ffff:a.b.c.d
            return ipaddress.IPv4Address(n & 0xFFFFFFFF)
        if n >> 112 == 0x2002:                           # 6to4: 2002:AABB:CCDD::
            return ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF)
        if ip in _NAT64:
            return ipaddress.IPv4Address(n & 0xFFFFFFFF)
        if n >> 32 == 0 and n > 1:                       # ::a.b.c.d, the deprecated IPv4-compatible form
            return ipaddress.IPv4Address(n & 0xFFFFFFFF)
    return ip


def _category(ip: Any) -> str:
    if isinstance(ip, ipaddress.IPv6Address) and ip in _TEREDO:
        return "reserved"
    ip = _unwrap(ip)
    if ip in _METADATA_ADDRS:
        return "metadata"
    n = int(ip)
    v4 = isinstance(ip, ipaddress.IPv4Address)
    if n == 0:
        return "unspecified"
    if (v4 and n >> 24 == 127) or (not v4 and n == 1):
        return "loopback"
    if _within(ip, V4_LINK_LOCAL if v4 else V6_LINK_LOCAL):
        return "link-local"
    if v4 and ip in _CGNAT:
        return "cgnat"
    if _within(ip, V4_MULTICAST if v4 else V6_MULTICAST):
        return "multicast"
    if _within(ip, V4_PRIVATE if v4 else V6_PRIVATE):
        return "private"
    if _within(ip, V4_RESERVED if v4 else V6_RESERVED) or (not v4 and ip not in V6_GLOBAL_UNICAST):
        return "reserved"
    return "public"


_REASONS = {
    "metadata": "a cloud metadata address",
    "unspecified": "the unspecified address",
    "loopback": "a loopback address",
    "link-local": "a link-local address",
    "cgnat": "a shared (CGNAT 100.64.0.0/10) address",
    "multicast": "a multicast address",
    "private": "a private address",
    "reserved": "a reserved address",
}

_ALLOWED = {
    PUBLIC: frozenset({"public"}),
    OPERATOR_LOCAL: frozenset({"public", "loopback", "private", "cgnat"}),
    INTERNAL: frozenset({"loopback"}),
}


def _profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} (use one of {', '.join(PROFILES)})")
    return profile


def classify_ip(addr: str, profile: str = PUBLIC) -> Optional[str]:
    """``None`` when ``addr`` may be reached under ``profile``, otherwise the reason it may not."""
    _profile(profile)
    text = str(addr or "").strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return f"{addr!r} is not an IP address"
    cat = _category(ip)
    if cat in _ALLOWED[profile]:
        return None
    return f"address {text} is {_REASONS.get(cat, cat)}" + ("" if profile == PUBLIC else f" (not allowed for {profile})")


# ---- URL checks -------------------------------------------------------------------------------

def _host_reason(host: str, profile: str, original: str) -> Optional[str]:
    if host in METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        return f"host {original} is a cloud metadata name"
    if profile == PUBLIC:            # other profiles accept names; the addresses they resolve to decide
        if "." not in host:
            return f"host {original} is a local (single-label) name"
        if host.endswith(_LOCAL_SUFFIXES):
            return f"host {original} is a local name"
    return None


def _evaluate(url: Any, profile: str, resolver: Optional[Resolver], max_len: int) -> tuple[Optional[str], list[str], str]:
    """``(reason, addresses, kind)`` — ``reason`` is ``None`` when the URL is fine."""
    _profile(profile)
    raw = "" if url is None else str(url).strip()
    if not raw:
        return "empty URL", [], "policy"
    if max_len and len(raw) > max_len:
        return f"URL longer than {max_len} characters", [], "policy"
    if _CONTROL.search(raw):
        return "URL contains control characters or spaces", [], "policy"
    if "\\" in raw.split("?", 1)[0].split("#", 1)[0]:
        return "URL contains a backslash", [], "policy"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "malformed URL", [], "policy"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return f"unsupported scheme {scheme or '(none)'!r}: only http and https are allowed", [], "policy"
    if not parts.netloc or not parts.hostname:
        return "URL has no host", [], "policy"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "URLs with embedded credentials are not allowed", [], "policy"
    try:
        port = parts.port
    except ValueError:
        return "invalid port", [], "policy"
    if port is None:
        port = 443 if scheme == "https" else 80
    if port == 0:
        return "invalid port", [], "policy"
    original = parts.hostname
    host = unicodedata.normalize("NFKC", original).strip().lower().rstrip(".")
    if not host:
        return "URL has no host", [], "policy"

    # literal addresses (IPv6 in brackets, IPv4 as four decimal numbers)
    literal = None
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    if literal is not None:
        reason = classify_ip(str(literal), profile)
        return reason, ([] if reason else [str(literal)]), "policy"
    if _NUMERIC_HOST.match(host):
        loose = parse_loose_ipv4(host)
        return f"host {original} is an obfuscated IP address" + (f" ({loose})" if loose else ""), [], "policy"
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return f"host {original} is not a valid name", [], "policy"
    if not _HOSTNAME.match(host) or len(host) > 253:
        return f"host {original} is not a valid name", [], "policy"
    reason = _host_reason(host, profile, original)
    if reason:
        return reason, [], "policy"
    if profile != PUBLIC and (host == "localhost" or host.endswith(".localhost")):
        addrs = ["127.0.0.1", "::1"]                     # by definition; no DNS needed
        return None, addrs, "policy"
    try:
        found = [str(a) for a in (resolver or default_resolver)(host, port)]
    except (OSError, UnicodeError) as error:
        return f"{UNRESOLVABLE_PREFIX} {original}: {error}", [], "dns"
    if not found:
        return f"{UNRESOLVABLE_PREFIX} {original}", [], "dns"
    for addr in found:
        problem = classify_ip(addr, profile)
        if problem:
            return f"host {original} resolves to a non-allowed address ({addr}: {problem.split(' is ', 1)[-1]})", [], "policy"
    return None, found, "policy"


def check_url(url: Any, profile: str = PUBLIC, resolver: Optional[Resolver] = None, *, max_len: int = MAX_URL_LEN) -> Optional[str]:
    """``None`` when ``url`` may be fetched under ``profile``, otherwise the reason it may not. A name is resolved
    (with ``resolver(host, port) -> [ip, ...]`` or the system resolver) and *every* address must be allowed."""
    return _evaluate(url, profile, resolver, max_len)[0]


def resolve_public(url: Any, profile: str = PUBLIC, resolver: Optional[Resolver] = None, *, max_len: int = MAX_URL_LEN) -> list[str]:
    """The addresses ``url`` may be reached at (already checked, in resolver order). Raises :class:`PolicyError`
    when the URL is not allowed or does not resolve. Connect to one of them with :func:`pinned_transport`."""
    reason, addrs, kind = _evaluate(url, profile, resolver, max_len)
    if reason:
        raise PolicyError(reason, str(url), kind)
    return addrs


# ---- connection pinning -----------------------------------------------------------------------

def pinned_transport(ip: str, *, verify: Any = True, http2: bool = False) -> Any:
    """An ``httpx`` transport that opens every TCP connection to ``ip`` whatever the URL's host says. The URL keeps
    its host name for the ``Host`` header and for TLS (server name indication and certificate check), so the page
    still validates as its own site while the connection goes where :func:`resolve_public` said it may.

    The body is handed back as a stream (a byte cap in front of the download works). ``httpx`` is imported here."""
    try:
        import httpcore
        import httpx
    except ImportError as error:                           # pragma: no cover
        raise missing_dependency("httpx", "pinned connections") from error
    import ssl

    class _Backend(httpcore.NetworkBackend):
        def __init__(self) -> None:
            self._real = httpcore.SyncBackend()

        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return self._real.connect_tcp(ip, port, timeout, local_address, socket_options)

        def connect_unix_socket(self, path, timeout=None, socket_options=None):   # pragma: no cover
            raise httpcore.ConnectError("unix sockets are not allowed")

        def sleep(self, seconds: float) -> None:
            self._real.sleep(seconds)

    exc_map = {
        httpcore.ConnectError: httpx.ConnectError, httpcore.ConnectTimeout: httpx.ConnectTimeout,
        httpcore.LocalProtocolError: httpx.LocalProtocolError, httpcore.NetworkError: httpx.NetworkError,
        httpcore.PoolTimeout: httpx.PoolTimeout, httpcore.ProtocolError: httpx.ProtocolError,
        httpcore.ProxyError: httpx.ProxyError, httpcore.ReadError: httpx.ReadError,
        httpcore.ReadTimeout: httpx.ReadTimeout, httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
        httpcore.TimeoutException: httpx.TimeoutException, httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
        httpcore.WriteError: httpx.WriteError, httpcore.WriteTimeout: httpx.WriteTimeout,
    }

    def _map(exc: BaseException) -> BaseException:
        for core_type, httpx_type in exc_map.items():
            if type(exc) is core_type:
                return httpx_type(str(exc))
        for core_type, httpx_type in exc_map.items():
            if isinstance(exc, core_type):
                return httpx_type(str(exc))
        return exc

    class _Stream(httpx.SyncByteStream):
        def __init__(self, response: Any) -> None:
            self._response = response

        def __iter__(self):
            try:
                yield from self._response.stream
            except BaseException as exc:
                mapped = _map(exc)
                if mapped is exc:
                    raise
                raise mapped from exc

        def close(self) -> None:
            self._response.close()

    class _Pinned(httpx.BaseTransport):
        def __init__(self) -> None:
            context = verify if isinstance(verify, ssl.SSLContext) else (httpx.create_ssl_context(verify=verify))
            self._pool = httpcore.ConnectionPool(ssl_context=context, http1=True, http2=http2, network_backend=_Backend())

        def handle_request(self, request: "httpx.Request") -> "httpx.Response":
            core_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port,
                                 target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream, extensions=request.extensions)
            try:
                response = self._pool.handle_request(core_request)
            except BaseException as exc:
                mapped = _map(exc)
                if mapped is exc:
                    raise
                raise mapped from exc
            return httpx.Response(status_code=response.status, headers=response.headers, stream=_Stream(response),
                                  extensions=response.extensions)

        def close(self) -> None:
            self._pool.close()

    return _Pinned()
