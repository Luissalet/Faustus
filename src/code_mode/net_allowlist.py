"""Per-run network allowlist for Code Mode (#255).

``run_code`` may carry ``allowed_hosts``: the only hosts the program may reach.
This module is the one place that decides what that list means:

* :func:`parse_allowed_hosts` validates and normalises the list. An entry is a
  host name (``pypi.org``), a subdomain wildcard with a LEADING ``*.`` only
  (``*.github.com``, which does not match the apex), or an IP literal written
  on purpose (``203.0.113.7``, ``[2001:db8::1]``); each may carry ``:port``.
  URLs, paths, ``localhost``, bare TLDs and wildcards anywhere else are refused.
  An empty list means no network; ``None`` means the caller did not ask for a
  list (today's behaviour).
* :class:`AllowlistProxy` is a small HTTP/1.1 forward proxy (plain requests with
  an absolute URI, and ``CONNECT`` for TLS) that only reaches those hosts. A name
  that resolves to a loopback / private / link-local address is refused (DNS
  rebinding); only an explicit IP entry can reach such an address. Every request
  is answered with ``Connection: close`` so a kept-alive connection cannot be
  reused for a second, different host.
* :func:`start_host_egress` / :class:`HostEgress` wire the proxy into the HOST
  runtime: the guest gets ``HTTP_PROXY`` & co. pointing at it, plus an in-guest
  socket guard (``guest.py``) that refuses direct connections. That is NOT a
  security boundary (native code or a child process can still open a socket); the
  result says so. The CONFINED runtime uses the same proxy as a sidecar container
  on an ``--internal`` docker network (``confined.py``), where the guest has no
  route anywhere else: that one is a real boundary.

The module is stdlib-only and importable on its own, because the container
sidecar executes this very file (``python3 -I -c <bootstrap> --serve``).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

MAX_HOSTS = 32
MAX_ENTRY_CHARS = 255
HEAD_LIMIT = 64 * 1024
CONNECT_TIMEOUT = 10.0
HEAD_TIMEOUT = 15.0
IDLE_TIMEOUT = 120.0
MAX_CONNECTIONS = 64
SIDECAR_PORT = 3128

# Enforcement kinds reported in the run result (see :func:`build_report`).
KIND_CONTAINER_PROXY = "container_internal_network_allowlist_proxy"
KIND_CONTAINER_NONE = "container_no_network"
KIND_HOST_PROXY = "host_proxy_env_and_socket_guard"
KIND_HOST_NONE = "host_socket_guard_no_network"

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOP_BY_HOP = frozenset({"proxy-connection", "proxy-authorization", "connection", "keep-alive",
                         "upgrade", "te", "trailer"})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class HostRule:
    """One validated allowlist entry."""

    __slots__ = ("kind", "value", "port", "text")

    def __init__(self, kind: str, value: str, port: Optional[int], text: str):
        self.kind = kind      # "name" | "suffix" | "ip"
        self.value = value    # lower-case name, suffix without "*.", or canonical IP
        self.port = port      # None = any port
        self.text = text      # the normalised entry shown to the human

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        if self.kind == "ip":
            return host == self.value
        if self.kind == "name":
            return host == self.value
        return host.endswith("." + self.value) and host != self.value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HostRule({self.text!r})"


def _parse_port(raw: str) -> int:
    if not raw.isdigit():
        raise ValueError(f"'{raw}' is not a port number")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is outside 1-65535")
    return port


def _check_name(name: str, *, wildcard: bool) -> str:
    try:
        ascii_name = name.encode("idna").decode("ascii") if not name.isascii() else name
    except UnicodeError as exc:
        raise ValueError("is not a valid host name") from exc
    labels = ascii_name.split(".")
    if len(ascii_name) > 253 or any(not _LABEL.match(label) for label in labels):
        raise ValueError("is not a valid host name")
    if labels[-1].isdigit():
        raise ValueError("looks like a number, not a host name; write an IP address in full (a.b.c.d)")
    if len(labels) < 2:
        raise ValueError("must be a fully qualified host name such as example.com")
    if wildcard and len(labels) < 2:
        raise ValueError("is too broad")
    return ascii_name


def parse_entry(raw: Any) -> HostRule:
    """Validate one entry; ``ValueError`` carries a message fit for the model."""
    if not isinstance(raw, str):
        raise ValueError("must be a string")
    text = raw.strip().lower()
    if not text:
        raise ValueError("is empty")
    if len(text) > MAX_ENTRY_CHARS:
        raise ValueError("is too long")
    if any(ch in text for ch in "/\\?#@ \t\r\n\0,;'\"`<>"):
        raise ValueError("must be only a host name, optionally followed by :port "
                         "(no scheme, path, credentials or spaces)")
    port: Optional[int] = None
    host = text
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("has an unclosed '[' in an IPv6 address")
        host, rest = text[1:end], text[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError("has text after the closing ']' that is not :port")
            port = _parse_port(rest[1:])
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("is not a valid IPv6 address") from exc
        if ip.version != 6:
            raise ValueError("uses [] around an IPv4 address; write it without brackets")
        return HostRule("ip", str(ip), port, f"[{ip}]" + (f":{port}" if port else ""))
    if text.count(":") > 1:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError as exc:
            raise ValueError("has several ':'; wrap an IPv6 address in [] when giving a port") from exc
        return HostRule("ip", str(ip), None, f"[{ip}]")
    if ":" in text:
        host, port_text = text.rsplit(":", 1)
        port = _parse_port(port_text)
    host = host.rstrip(".")
    if not host:
        raise ValueError("has no host")
    port_suffix = f":{port}" if port else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 6:
            return HostRule("ip", str(ip), port, f"[{ip}]{port_suffix}")
        return HostRule("ip", str(ip), port, f"{ip}{port_suffix}")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("is a loopback name; to allow this computer write the IP address "
                         "explicitly (127.0.0.1)")
    if "*" in host:
        if not host.startswith("*.") or "*" in host[2:]:
            raise ValueError("uses '*' somewhere other than a leading '*.'")
        suffix = _check_name(host[2:], wildcard=True)
        return HostRule("suffix", suffix, port, f"*.{suffix}{port_suffix}")
    name = _check_name(host, wildcard=False)
    return HostRule("name", name, port, f"{name}{port_suffix}")


def parse_allowed_hosts(value: Any) -> Tuple[Optional[List[HostRule]], List[str]]:
    """``(rules, errors)``. ``rules`` is ``None`` when no list was given and ``[]``
    for an empty list (no network). Any error means the run must not start."""
    if value is None:
        return None, []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    if not isinstance(value, (list, tuple)):
        return None, ["allowed_hosts must be a list of host names (an empty list means no network)"]
    if len(value) > MAX_HOSTS:
        return None, [f"allowed_hosts has {len(value)} entries; the limit is {MAX_HOSTS}"]
    rules: List[HostRule] = []
    errors: List[str] = []
    seen = set()
    for raw in value:
        try:
            rule = parse_entry(raw)
        except ValueError as exc:
            errors.append(f"allowed_hosts entry {str(raw)[:80]!r} {exc}")
            continue
        if rule.text not in seen:
            seen.add(rule.text)
            rules.append(rule)
    if errors:
        return None, errors
    return rules, []


def rule_texts(rules: Optional[List[HostRule]]) -> Optional[List[str]]:
    return None if rules is None else [r.text for r in rules]


# ---------------------------------------------------------------------------
# Approval-card wording
# ---------------------------------------------------------------------------

def approval_note(content: Any, runtime: str = "confined") -> str:
    """One sentence for the approval card about a ``run_code`` call's network, or
    ``''`` when the call asks for no list (the card is then unchanged)."""
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("allowed_hosts") is None:
        return ""
    rules, errors = parse_allowed_hosts(data.get("allowed_hosts"))
    if errors:
        return "Network: the allowed_hosts list is invalid, so the run will be refused (" + "; ".join(errors) + ")."
    how = ("a container on an internal network that can only reach a filtering proxy (a hard boundary)"
           if runtime != "host" else
           "a filtering proxy plus an in-process socket guard (best effort, not a security boundary)")
    if not rules:
        return f"Network: none (allowed_hosts is empty), enforced by {'the container having no network' if runtime != 'host' else 'an in-process socket guard (best effort)'}."
    return f"Network: only {', '.join(r.text for r in rules)}; every other host is refused, enforced by {how}."


# ---------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------

Resolver = Callable[[str, int], Awaitable[List[str]]]


async def default_resolver(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: List[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out[:8]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global and not ip.is_multicast)


def _canon_host(host: str) -> str:
    """An IP literal in its canonical spelling (so 0:0:0:0:0:0:0:1 equals ::1)."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def split_authority(authority: str, default_port: int) -> Tuple[str, int]:
    authority = authority.strip()
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError("bad authority")
        host, rest = authority[1:end], authority[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host.lower(), port
    if authority.count(":") == 1:
        host, port_text = authority.split(":")
        if not port_text.isdigit():
            raise ValueError("bad port")
        return host.lower().rstrip("."), int(port_text)
    return authority.lower().rstrip("."), default_port


class AllowlistProxy:
    """Allow-only forward proxy. ``hosts`` are :class:`HostRule` objects."""

    def __init__(self, hosts: List[HostRule], *, bind: str = "127.0.0.1", port: int = 0,
                 resolver: Optional[Resolver] = None, public_only: bool = True,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.rules = list(hosts)
        self.bind = bind
        self.requested_port = port
        self.resolver = resolver or default_resolver
        self.public_only = public_only
        self.on_event = on_event
        self.allowed = 0
        self.denied = 0
        self.allowed_targets: List[str] = []
        self.denied_targets: List[str] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._sem = asyncio.Semaphore(MAX_CONNECTIONS)
        self._tasks: set = set()
        self.port = 0

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.bind, self.requested_port,
                                                  limit=HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                pass
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stats(self) -> Dict[str, Any]:
        return {"requests_allowed": self.allowed, "requests_denied": self.denied,
                "allowed_targets": list(self.allowed_targets), "denied_targets": list(self.denied_targets)}

    # -- policy ------------------------------------------------------------
    def _note(self, decision: str, host: str, port: int, why: str = "") -> None:
        target = f"{host}:{port}"
        if decision == "allow":
            self.allowed += 1
            if target not in self.allowed_targets and len(self.allowed_targets) < 20:
                self.allowed_targets.append(target)
        else:
            self.denied += 1
            if target not in self.denied_targets and len(self.denied_targets) < 20:
                self.denied_targets.append(target)
        if self.on_event is not None:
            try:
                self.on_event({"t": decision, "host": host, "port": port, "why": why})
            except Exception:  # noqa: BLE001 - reporting never breaks a request
                pass

    def match(self, host: str, port: int) -> Optional[HostRule]:
        for rule in self.rules:
            if rule.matches(host, port):
                return rule
        return None

    async def _targets(self, host: str, port: int) -> Tuple[Optional[List[str]], str]:
        """Addresses to connect to for an allowed request, or ``(None, why)``."""
        rule = self.match(host, port)
        if rule is None:
            return None, "host is not in allowed_hosts"
        if rule.kind == "ip":
            return [host], ""
        try:
            addresses = await asyncio.wait_for(self.resolver(host, port), timeout=CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return None, f"could not resolve the host ({type(exc).__name__})"
        if not addresses:
            return None, "the host did not resolve"
        if self.public_only and not all(_is_public(a) for a in addresses):
            return None, "the host resolves to a loopback/private address; allow it by its IP address if that is intended"
        return addresses, ""

    # -- connection handling -----------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            async with self._sem:
                await self._serve_one(reader, writer)
        except (asyncio.CancelledError, ConnectionError, OSError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - a bad client never takes the proxy down
            pass
        finally:
            _close(writer)
            if task is not None:
                self._tasks.discard(task)

    async def _deny(self, writer, status: int, reason: str, host: str = "", port: int = 0) -> None:
        body = f"Faustus Code Mode: {host + ':' + str(port) + ' - ' if host else ''}{reason}\n".encode()
        phrase = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(status, "Error")
        writer.write(f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                     f"X-Faustus-Egress: denied\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    async def _serve_one(self, reader, writer) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].upper().startswith("HTTP/"):
            await self._deny(writer, 400, "malformed request")
            return
        method, target, version = parts[0].upper(), parts[1], parts[2]
        try:
            if method == "CONNECT":
                host, port = split_authority(target, 443)
                path = ""
            else:
                url = urlsplit(target)
                if url.scheme.lower() != "http" or not url.hostname:
                    await self._deny(writer, 400, "this is a forward proxy: send an absolute http:// URI or CONNECT")
                    return
                host, port = url.hostname.lower().rstrip("."), url.port or 80
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        except ValueError:
            await self._deny(writer, 400, "malformed target")
            return
        addresses, why = await self._targets(host, port)
        if addresses is None:
            self._note("deny", host, port, why)
            await self._deny(writer, 403, f"refused, {why}. Allowed: {', '.join(r.text for r in self.rules) or 'nothing'}",
                             host, port)
            return
        upstream = None
        last_error = "no address answered"
        for address in addresses:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}"
        if upstream is None:
            self._note("deny", host, port, f"connect failed: {last_error}")
            await self._deny(writer, 502, f"could not connect ({last_error})", host, port)
            return
        up_reader, up_writer = upstream
        self._note("allow", host, port)
        try:
            if method == "CONNECT":
                writer.write(f"{version} 200 Connection established\r\n\r\n".encode())
                await writer.drain()
            else:
                headers = []
                for line in lines[1:]:
                    if not line:
                        continue
                    name = line.split(":", 1)[0].strip().lower()
                    if name in _HOP_BY_HOP or name == "host":
                        continue
                    headers.append(line)
                authority = host if port == 80 else f"{host}:{port}"
                if ":" in host:
                    authority = f"[{host}]" + ("" if port == 80 else f":{port}")
                out = [f"{method} {path} {version}", f"Host: {authority}", *headers, "Connection: close", "", ""]
                up_writer.write("\r\n".join(out).encode("latin-1"))
                await up_writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            _close(up_writer)


def _close(writer) -> None:
    try:
        writer.close()
    except Exception:  # noqa: BLE001
        pass


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(source.read(65536), timeout=IDLE_TIMEOUT)
            if not data:
                break
            sink.write(data)
            await sink.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            if sink.can_write_eof():
                sink.write_eof()
            else:
                sink.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Host runtime wiring
# ---------------------------------------------------------------------------

# Overridable by tests (resolver / public_only); production keeps the defaults.
PROXY_OPTIONS: Dict[str, Any] = {"resolver": None, "public_only": True}


def proxy_env(url: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = url
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def build_report(kind: str, rules: List[HostRule], *, hard: bool, stats: Optional[Dict[str, Any]] = None,
                 note: str = "") -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "allowed_hosts": [r.text for r in rules],
        "enforcement": kind,
        "hard_boundary": hard,
    }
    if kind == KIND_CONTAINER_PROXY:
        report["how"] = ("the program runs in a container attached only to an internal docker network; "
                         "the one thing it can reach there is a filtering proxy that connects only to the allowed hosts")
    elif kind == KIND_CONTAINER_NONE:
        report["how"] = "the container has no network at all"
    elif kind == KIND_HOST_PROXY:
        report["how"] = ("HTTP_PROXY/HTTPS_PROXY point at a filtering proxy and an in-process socket guard refuses "
                         "direct connections, child processes and native libraries")
        report["bypassable_by"] = ["code that opens a socket from native code or from a process the guard does not see",
                                   "any other way of running a program outside the Python interpreter that hosts the code"]
    elif kind == KIND_HOST_NONE:
        report["how"] = "an in-process socket guard refuses every network connection"
        report["bypassable_by"] = ["native code or a child process the guard does not see"]
    if stats is not None:
        report["proxy"] = stats
    if note:
        report["note"] = note
    return report


def wire_summary(report: Any) -> Optional[Dict[str, Any]]:
    """The small form of a ``network`` report that rides on the chat stream's
    ``tool_output`` event (and is persisted with it) so the transcript can say
    what network a run_code call had. ``None`` for anything that is not one."""
    if not isinstance(report, dict) or "allowed_hosts" not in report or "enforcement" not in report:
        return None
    hosts = report.get("allowed_hosts")
    proxy = report.get("proxy") if isinstance(report.get("proxy"), dict) else {}
    return {
        "allowed_hosts": [str(h) for h in hosts][:32] if isinstance(hosts, list) else None,
        "enforcement": str(report.get("enforcement")),
        "hard_boundary": bool(report.get("hard_boundary")),
        "requests_allowed": int(proxy.get("requests_allowed") or 0),
        "requests_denied": int(proxy.get("requests_denied") or 0),
        "denied_targets": [str(x) for x in (proxy.get("denied_targets") or [])][:5],
    }


class HostEgress:
    """The per-run network state of the host runtime."""

    def __init__(self, rules: List[HostRule], proxy: Optional[AllowlistProxy]):
        self.rules = rules
        self.proxy = proxy

    @property
    def env(self) -> Dict[str, str]:
        if self.proxy is None:
            return {}
        return proxy_env(f"http://127.0.0.1:{self.proxy.port}")

    @property
    def guard(self) -> Dict[str, Any]:
        return {"proxy_port": self.proxy.port if self.proxy is not None else None}

    def report(self) -> Dict[str, Any]:
        if self.proxy is None:
            return build_report(KIND_HOST_NONE, self.rules, hard=False)
        return build_report(KIND_HOST_PROXY, self.rules, hard=False, stats=self.proxy.stats())

    async def stop(self) -> None:
        if self.proxy is not None:
            await self.proxy.stop()


async def start_host_egress(rules: List[HostRule]) -> HostEgress:
    if not rules:
        return HostEgress(rules, None)
    proxy = AllowlistProxy(rules, bind="127.0.0.1", port=0, resolver=PROXY_OPTIONS.get("resolver"),
                           public_only=bool(PROXY_OPTIONS.get("public_only", True)))
    await proxy.start()
    return HostEgress(rules, proxy)


# ---------------------------------------------------------------------------
# Sidecar entry point (container runtime)
# ---------------------------------------------------------------------------

def encode_hosts(rules: List[HostRule]) -> str:
    return base64.b64encode(json.dumps([r.text for r in rules]).encode("utf-8")).decode("ascii")


def _serve(argv: List[str]) -> int:
    port = int(argv[0])
    texts = json.loads(base64.b64decode(argv[1]).decode("utf-8"))
    rules, errors = parse_allowed_hosts(texts)
    if errors or rules is None:
        sys.stderr.write("egress sidecar: invalid hosts\n")
        return 2

    def emit(event: Dict[str, Any]) -> None:
        sys.stdout.write("EGRESS " + json.dumps(event) + "\n")
        sys.stdout.flush()

    async def main() -> None:
        proxy = AllowlistProxy(rules, bind="0.0.0.0", port=port, on_event=emit)
        await proxy.start()
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        await asyncio.Event().wait()

    asyncio.run(main())
    return 0


if __name__ == "__main__" and len(sys.argv) >= 4 and sys.argv[1] == "--serve":
    sys.exit(_serve(sys.argv[2:4]))
