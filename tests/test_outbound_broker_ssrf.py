"""B-019: the outbound broker is the single destination policy for untrusted HTTP.

Before this, a caller validated a hostname with one of three disagreeing
classifiers and then handed the *name* to a fresh httpx client, which resolved
it a second time. Everything here is about closing that gap: one resolution per
hop, a connect pinned to the address that was judged, the same judgement re-run
on every redirect, and byte/MIME/ratio ceilings that fire before the bytes land.
"""
import gzip
import ipaddress

import httpx
import pytest

from src import outbound_fetch
from src.outbound_fetch import BodyTooLargeError, OutboundPolicyError


class _ChunkStream(httpx.SyncByteStream):
    """Streams a canned body in small pieces, like a real socket would."""

    def __init__(self, body: bytes, chunk: int = 512):
        self._body = body
        self._chunk = chunk

    def __iter__(self):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def close(self) -> None:
        pass


class _ExplodingStream(httpx.SyncByteStream):
    """Fails the test if anything tries to read the body."""

    def __iter__(self):
        raise AssertionError("body must not be read once the size is refused")

    def close(self) -> None:
        pass


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, script, log):
        self._script = script
        self._log = log

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self._log.append(
            {
                "url": url,
                "host": request.headers.get("host"),
                "accept_encoding": request.headers.get("accept-encoding"),
            }
        )
        if url not in self._script:
            raise AssertionError(f"unexpected request to {url}")
        status, headers, body = self._script[url]
        stream = body if isinstance(body, httpx.SyncByteStream) else _ChunkStream(body)
        return httpx.Response(status, headers=headers, stream=stream)


def _wire(script):
    """Return (transport_factory, pinned_ips, request_log) for a canned wire."""
    pins: list[str] = []
    log: list[dict] = []

    def factory(ip):
        pins.append(str(ip))
        return _ScriptedTransport(script, log)

    return factory, pins, log


def _resolver(mapping):
    def resolve(host):
        if host not in mapping:
            raise AssertionError(f"unexpected resolution of {host}")
        return mapping[host]

    return resolve


def _ok(body=b"hi", content_type="text/plain"):
    return (200, {"content-type": content_type, "content-length": str(len(body))}, body)


# ---------------------------------------------------------------------------
# DNS rebinding
# ---------------------------------------------------------------------------

def test_rebinding_after_the_check_cannot_move_the_socket():
    """The classic TOCTOU: the name answers public for the guard and metadata
    for the connect. The broker resolves once and connects to what it judged, so
    the second answer is never asked for on this hop."""
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])
    factory, pins, log = _wire({"https://rebind.example/img.png": _ok()})

    resp = outbound_fetch.fetch(
        "https://rebind.example/img.png",
        profile=outbound_fetch.PUBLIC_UNTRUSTED,
        resolver=lambda host: next(answers),
        transport_factory=factory,
    )

    assert resp.status_code == 200
    assert pins == ["93.184.216.34"]
    # Only the socket destination moved: Host (and therefore SNI and vhost
    # routing) still carries the original name.
    assert log[0]["host"] == "rebind.example"
    assert log[0]["accept_encoding"] == "identity"


def test_every_redirect_hop_is_resolved_and_judged_again():
    """A hop that rebinds between the first and second request is caught,
    because the second hop gets a full re-classification rather than inheriting
    the first hop's verdict."""
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])
    factory, pins, log = _wire(
        {
            "https://rebind.example/one": (302, {"location": "/two"}, b""),
            "https://rebind.example/two": _ok(),
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://rebind.example/one",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=lambda host: next(answers),
            transport_factory=factory,
        )

    assert "link-local" in str(exc.value)
    assert [entry["url"] for entry in log] == ["https://rebind.example/one"]


def test_cname_landing_on_a_private_address_is_blocked_before_connecting():
    """CNAME shape: a perfectly public-looking name whose resolution ends up
    inside RFC 1918. Nothing is dialled."""
    factory, pins, log = _wire({})

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://cdn.attacker.example/payload",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=_resolver({"cdn.attacker.example": ["10.0.0.7"]}),
            transport_factory=factory,
        )

    assert "private" in str(exc.value)
    assert pins == [] and log == []


@pytest.mark.parametrize(
    "mapped",
    ["::ffff:169.254.169.254", "::ffff:10.0.0.1", "::ffff:127.0.0.1"],
)
def test_ipv4_mapped_ipv6_is_judged_by_the_embedded_v4(mapped):
    factory, pins, _ = _wire({})

    with pytest.raises(OutboundPolicyError):
        outbound_fetch.fetch(
            "https://mapped.example/x",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=_resolver({"mapped.example": [mapped]}),
            transport_factory=factory,
        )
    assert pins == []


def test_ipv4_mapped_literal_in_the_url_is_blocked_too():
    factory, pins, _ = _wire({})

    with pytest.raises(OutboundPolicyError):
        outbound_fetch.fetch(
            "http://[::ffff:169.254.169.254]/latest/meta-data",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            transport_factory=factory,
        )
    assert pins == []


def test_internal_hostnames_are_refused_without_asking_a_resolver():
    """Resolving first would let a resolver that answers differently per query
    decide the outcome, so these names lose on the name alone."""

    def _never(host):
        raise AssertionError("internal hostname must not reach the resolver")

    for url in (
        "http://localhost/x",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://printer.lan/x",
        "http://svc.internal/x",
    ):
        with pytest.raises(OutboundPolicyError):
            outbound_fetch.fetch(
                url, profile=outbound_fetch.PUBLIC_UNTRUSTED, resolver=_never
            )


def test_a_host_straddling_both_zones_is_refused():
    """One public and one private record: pinning would pick by resolver order,
    which is a rebinding setup that does not even need timing."""
    factory, pins, _ = _wire({})

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://split.example/x",
            profile=outbound_fetch.OPERATOR_LOCAL,
            allow_local=True,
            resolver=_resolver({"split.example": ["93.184.216.34", "10.0.0.7"]}),
            transport_factory=factory,
        )

    assert "straddles" in str(exc.value)
    assert pins == []


# ---------------------------------------------------------------------------
# Mixed destinations across redirects
# ---------------------------------------------------------------------------

def test_public_redirect_into_the_lan_is_blocked():
    factory, pins, log = _wire(
        {
            "https://images.example/a": (
                302,
                {"location": "http://192.168.1.10/admin"},
                b"",
            ),
        }
    )

    with pytest.raises(OutboundPolicyError):
        outbound_fetch.fetch(
            "https://images.example/a",
            profile=outbound_fetch.PROVIDER_RESULT,
            resolver=_resolver({"images.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert [entry["url"] for entry in log] == ["https://images.example/a"]
    assert pins == ["93.184.216.34"]


def test_operator_local_still_refuses_a_public_to_private_hop():
    """Even where the LAN is permitted, a request that started on the public
    internet may not end inside it: the operator's permission covers the
    endpoint they configured, not wherever it forwards to."""
    factory, _, log = _wire(
        {
            "https://provider.example/a": (
                302,
                {"location": "http://10.1.2.3/secret"},
                b"",
            ),
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://provider.example/a",
            profile=outbound_fetch.OPERATOR_LOCAL,
            allow_local=True,
            resolver=_resolver({"provider.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "mixed-zone" in str(exc.value)
    assert [entry["url"] for entry in log] == ["https://provider.example/a"]


def test_local_redirect_out_to_the_internet_is_blocked():
    """The other direction leaks whatever the LAN service put in the URL."""
    factory, _, _ = _wire(
        {
            "http://10.1.2.3/render": (
                302,
                {"location": "https://exfil.example/?data=secret"},
                b"",
            ),
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "http://10.1.2.3/render",
            profile=outbound_fetch.OPERATOR_LOCAL,
            allow_local=True,
            resolver=_resolver({"exfil.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "mixed-zone" in str(exc.value)


def test_same_zone_redirect_is_followed():
    factory, pins, log = _wire(
        {
            "https://a.example/one": (302, {"location": "https://b.example/two"}, b""),
            "https://b.example/two": _ok(b"payload"),
        }
    )

    resp = outbound_fetch.fetch(
        "https://a.example/one",
        profile=outbound_fetch.PUBLIC_UNTRUSTED,
        resolver=_resolver(
            {"a.example": ["93.184.216.34"], "b.example": ["151.101.1.140"]}
        ),
        transport_factory=factory,
    )

    assert resp.content == b"payload"
    assert pins == ["93.184.216.34", "151.101.1.140"]


def test_redirect_budget_is_enforced():
    factory, _, log = _wire(
        {
            "https://a.example/1": (302, {"location": "https://a.example/2"}, b""),
            "https://a.example/2": (302, {"location": "https://a.example/3"}, b""),
            "https://a.example/3": (302, {"location": "https://a.example/4"}, b""),
            "https://a.example/4": _ok(),
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://a.example/1",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            max_redirects=2,
            resolver=_resolver({"a.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "Too many redirects" in str(exc.value)
    assert len(log) == 3


def test_redirect_loop_is_refused():
    factory, _, _ = _wire(
        {
            "https://a.example/1": (302, {"location": "https://a.example/2"}, b""),
            "https://a.example/2": (302, {"location": "https://a.example/1"}, b""),
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://a.example/1",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=_resolver({"a.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "loop" in str(exc.value)


# ---------------------------------------------------------------------------
# Oversized and over-expanding responses
# ---------------------------------------------------------------------------

def test_declared_length_over_the_cap_is_refused_without_reading_the_body():
    factory, _, _ = _wire(
        {
            "https://big.example/f": (
                200,
                {"content-type": "image/png", "content-length": "999999999"},
                _ExplodingStream(),
            )
        }
    )

    with pytest.raises(BodyTooLargeError) as exc:
        outbound_fetch.fetch(
            "https://big.example/f",
            profile=outbound_fetch.PROVIDER_RESULT,
            max_bytes=1000,
            resolver=_resolver({"big.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert exc.value.cap == 1000


def test_a_body_that_outgrows_its_declared_length_is_refused_mid_stream():
    """A lying Content-Length must not buy the sender an unbounded download."""
    factory, _, _ = _wire(
        {
            "https://big.example/f": (
                200,
                {"content-type": "image/png", "content-length": "10"},
                b"x" * 40_000,
            )
        }
    )

    with pytest.raises(BodyTooLargeError):
        outbound_fetch.fetch(
            "https://big.example/f",
            profile=outbound_fetch.PROVIDER_RESULT,
            max_bytes=2048,
            resolver=_resolver({"big.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )


def test_decompression_ratio_is_bounded():
    """Identity was requested; a server that compresses anyway does not get to
    decide how much memory the decoded body takes."""
    body = gzip.compress(b"A" * 200_000)
    factory, _, _ = _wire(
        {
            "https://bomb.example/f": (
                200,
                {"content-type": "text/plain", "content-encoding": "gzip"},
                body,
            )
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://bomb.example/f",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=_resolver({"bomb.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "decompression ratio" in str(exc.value)


def test_a_compressed_body_within_both_bounds_is_decoded():
    payload = b"hello world " * 4
    factory, _, _ = _wire(
        {
            "https://ok.example/f": (
                200,
                {"content-type": "text/plain", "content-encoding": "gzip"},
                gzip.compress(payload),
            )
        }
    )

    resp = outbound_fetch.fetch(
        "https://ok.example/f",
        profile=outbound_fetch.PUBLIC_UNTRUSTED,
        resolver=_resolver({"ok.example": ["93.184.216.34"]}),
        transport_factory=factory,
    )

    assert resp.content == payload


def test_an_encoding_we_cannot_bound_is_refused():
    factory, _, _ = _wire(
        {
            "https://br.example/f": (
                200,
                {"content-type": "text/plain", "content-encoding": "br"},
                b"\x00" * 10,
            )
        }
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://br.example/f",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            resolver=_resolver({"br.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "cannot bound" in str(exc.value)


def test_content_type_outside_the_allowed_set_is_refused():
    factory, _, _ = _wire(
        {"https://cdn.example/f": _ok(b"<html>", content_type="text/html")}
    )

    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.fetch(
            "https://cdn.example/f",
            profile=outbound_fetch.PROVIDER_RESULT,
            allowed_mime=("image/",),
            resolver=_resolver({"cdn.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert "content type" in str(exc.value)


def test_a_caller_cannot_raise_a_profile_ceiling():
    """max_bytes is a request for less, never a request for more."""
    limits = outbound_fetch.PROFILE_LIMITS[outbound_fetch.PUBLIC_UNTRUSTED]
    factory, _, _ = _wire(
        {
            "https://big.example/f": (
                200,
                {"content-length": str(limits.max_bytes + 1)},
                _ExplodingStream(),
            )
        }
    )

    with pytest.raises(BodyTooLargeError) as exc:
        outbound_fetch.fetch(
            "https://big.example/f",
            profile=outbound_fetch.PUBLIC_UNTRUSTED,
            max_bytes=limits.max_bytes * 100,
            resolver=_resolver({"big.example": ["93.184.216.34"]}),
            transport_factory=factory,
        )

    assert exc.value.cap == limits.max_bytes


# ---------------------------------------------------------------------------
# Trust profiles
# ---------------------------------------------------------------------------

def test_local_profiles_are_inert_without_an_explicit_unlock():
    """The LAN is reachable only when a caller says so in code the operator's
    configuration drives -- never as a side effect of a URL or a model string."""
    for profile in (outbound_fetch.OPERATOR_LOCAL, outbound_fetch.INTERNAL_SERVICE):
        with pytest.raises(OutboundPolicyError) as exc:
            outbound_fetch.classify_destination(
                "http://192.168.1.10/x", profile=profile
            )
        assert "allow_local=True" in str(exc.value)


def test_allow_local_on_a_public_profile_is_an_error_not_a_no_op():
    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.classify_destination(
            "https://a.example/x",
            profile=outbound_fetch.PROVIDER_RESULT,
            allow_local=True,
        )
    assert "meaningless" in str(exc.value)


def test_unknown_profile_is_refused():
    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.classify_destination("https://a.example/x", profile="trusted")
    assert "Unknown outbound trust profile" in str(exc.value)


@pytest.mark.parametrize("ip", ["192.168.1.10", "127.0.0.1", "100.64.0.1", "0.0.0.0"])
def test_provider_result_never_reaches_inside_the_deployment(ip):
    with pytest.raises(OutboundPolicyError):
        outbound_fetch.classify_destination(
            f"http://{ip}/x", profile=outbound_fetch.PROVIDER_RESULT
        )


def test_operator_local_reaches_the_lan_once_unlocked():
    zone, addrs = outbound_fetch.classify_destination(
        "http://192.168.1.10/x",
        profile=outbound_fetch.OPERATOR_LOCAL,
        allow_local=True,
    )
    assert zone == outbound_fetch.ZONE_LOCAL
    assert addrs == [ipaddress.ip_address("192.168.1.10")]


def test_link_local_stays_blocked_even_for_the_local_profile():
    """Permitting the LAN never permits cloud instance metadata."""
    with pytest.raises(OutboundPolicyError) as exc:
        outbound_fetch.classify_destination(
            "http://169.254.169.254/latest/meta-data",
            profile=outbound_fetch.OPERATOR_LOCAL,
            allow_local=True,
        )
    assert "link-local" in str(exc.value)


def test_internal_service_is_loopback_only():
    zone, _ = outbound_fetch.classify_destination(
        "http://127.0.0.1:8080/health",
        profile=outbound_fetch.INTERNAL_SERVICE,
        allow_local=True,
    )
    assert zone == outbound_fetch.ZONE_LOCAL

    for url in ("http://192.168.1.10/x", "https://a.example/x"):
        with pytest.raises(OutboundPolicyError) as exc:
            outbound_fetch.classify_destination(
                url,
                profile=outbound_fetch.INTERNAL_SERVICE,
                allow_local=True,
                resolver=_resolver({"a.example": ["93.184.216.34"]}),
            )
        assert "loopback" in str(exc.value)


def test_configured_endpoint_profile_follows_the_deployment_switch():
    assert outbound_fetch.profile_for_configured_endpoint(block_private=False) == (
        outbound_fetch.OPERATOR_LOCAL,
        True,
    )
    assert outbound_fetch.profile_for_configured_endpoint(block_private=True) == (
        outbound_fetch.PUBLIC_UNTRUSTED,
        False,
    )


def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "gopher://a.example/", "ftp://a.example/x"):
        with pytest.raises(OutboundPolicyError):
            outbound_fetch.classify_destination(
                url, profile=outbound_fetch.PUBLIC_UNTRUSTED
            )


# ---------------------------------------------------------------------------
# The image tools, which were the fragmented callers B-019 names
# ---------------------------------------------------------------------------

def test_image_download_treats_a_foreign_result_host_as_untrusted(monkeypatch):
    from src import ai_interaction

    monkeypatch.delenv("IMAGE_BLOCK_PRIVATE_IPS", raising=False)
    assert ai_interaction._provider_image_profile(
        "https://cdn.evil.example/x.png",
        "https://api.openai.example/v1/chat/completions",
    ) == (outbound_fetch.PROVIDER_RESULT, False)


def test_image_download_inherits_permission_only_from_the_configured_host(monkeypatch):
    """A local diffusion server answers with a URL on its own host, which is the
    host the operator registered and is already sending the API key to. That is
    where the local permission comes from -- not from the response body."""
    from src import ai_interaction

    monkeypatch.delenv("IMAGE_BLOCK_PRIVATE_IPS", raising=False)
    assert ai_interaction._provider_image_profile(
        "http://192.168.1.50:7860/outputs/x.png",
        "http://192.168.1.50:7860/v1/chat/completions",
    ) == (outbound_fetch.OPERATOR_LOCAL, True)


def test_image_block_private_ips_forces_the_public_profile(monkeypatch):
    from src import ai_interaction

    monkeypatch.setenv("IMAGE_BLOCK_PRIVATE_IPS", "true")
    assert ai_interaction._provider_image_profile(
        "http://192.168.1.50:7860/outputs/x.png",
        "http://192.168.1.50:7860/v1/chat/completions",
    ) == (outbound_fetch.PUBLIC_UNTRUSTED, False)


def test_image_download_goes_through_the_broker(monkeypatch):
    """Regression for the actual defect: the download must not be a bare
    httpx.get, which re-resolves the host the guard just approved."""
    from src import ai_interaction

    monkeypatch.delenv("IMAGE_BLOCK_PRIVATE_IPS", raising=False)
    seen = {}

    def _fetch(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return outbound_fetch._CappedFetch(
            200, httpx.Headers({}), b"PNGDATA", False, 7, None, url
        )

    def _no_get(*args, **kwargs):
        raise AssertionError("image download must not use an unpinned httpx.get")

    monkeypatch.setattr(outbound_fetch, "fetch", _fetch)
    monkeypatch.setattr(httpx, "get", _no_get)

    body = ai_interaction._download_provider_image(
        "https://cdn.example/x.png", "https://api.openai.example/v1/chat/completions"
    )

    assert body == b"PNGDATA"
    assert seen["url"] == "https://cdn.example/x.png"
    assert seen["profile"] == outbound_fetch.PROVIDER_RESULT
    assert seen["allow_local"] is False
    assert seen["max_bytes"] == ai_interaction.IMAGE_DOWNLOAD_MAX_BYTES
    assert seen["allowed_mime"] == ai_interaction.IMAGE_DOWNLOAD_MIME


def test_image_download_surfaces_policy_refusals_distinctly(monkeypatch):
    from src import ai_interaction

    monkeypatch.delenv("IMAGE_BLOCK_PRIVATE_IPS", raising=False)

    with pytest.raises(OutboundPolicyError):
        ai_interaction._download_provider_image(
            "http://169.254.169.254/latest/meta-data",
            "https://api.openai.example/v1/chat/completions",
        )


def test_integrations_reads_its_trust_profile_from_the_broker():
    """execute_api_call keeps its own async pinned transport, but the decision
    of what the deployment's LAN switch means now has one home."""
    import inspect

    from src import integrations

    source = inspect.getsource(integrations.execute_api_call)
    assert "profile_for_configured_endpoint" in source


def test_over_a_real_socket_only_the_destination_moves():
    """End to end through the actual pinned transport, not the scripted one:
    the name resolves to loopback, the socket goes there, and the Host header
    still carries the name -- so vhost routing and certificate validation see
    the hostname the caller asked for, which is what makes pinning safe to do.
    """
    import socket
    import threading

    captured = {}
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            request = conn.recv(8192)
            for line in request.split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.lower() in (b"host", b"accept-encoding"):
                    captured[name.lower().decode()] = value.strip().decode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 5\r\nConnection: close\r\n\r\nhello"
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        resp = outbound_fetch.fetch(
            f"http://pinned.example:{port}/health",
            profile=outbound_fetch.INTERNAL_SERVICE,
            allow_local=True,
            resolver=_resolver({"pinned.example": ["127.0.0.1"]}),
        )
    finally:
        thread.join(timeout=5)
        server.close()

    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert captured["host"] == f"pinned.example:{port}"
    assert captured["accept-encoding"] == "identity"
