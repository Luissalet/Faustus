"""Exercise real HTTPX client/transport initialization without opening a socket."""
import ssl
from types import SimpleNamespace

import httpx
import pytest

from src import model_backend as mb


@pytest.fixture
def wire(monkeypatch):
    """Keep HTTPX's real SSL setup; replace only the transport's wire operation."""
    state = SimpleNamespace(loads=[], requests=[], clients=[], contexts=[],
                            reply=lambda request: httpx.Response(200, content=b"{}"))
    load_locations = ssl.SSLContext.load_verify_locations
    load_defaults = ssl.SSLContext.load_default_certs
    enter = httpx.Client.__enter__

    def locations(context, *args, **kwargs):
        state.loads.append("load_verify_locations")
        return load_locations(context, *args, **kwargs)

    def defaults(context, *args, **kwargs):
        state.loads.append("load_default_certs")
        return load_defaults(context, *args, **kwargs)

    def entered(client):
        state.clients.append(client)
        state.contexts.append(client._transport._pool._ssl_context)
        return enter(client)

    def handle_request(transport, request):
        state.requests.append(request)
        response = state.reply(request)
        response.request = request
        return response

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", locations)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", defaults)
    monkeypatch.setattr(httpx.Client, "__enter__", entered)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    mb.clear_cache()
    yield state
    mb.clear_cache()


@pytest.mark.parametrize("root", ["http://127.0.0.1:8090", "HTTP://192.168.1.8:8000"])
def test_http_probe_does_not_load_certificate_store(wire, root, record_property):
    result = mb._probe_local_backend(root)
    record_property("CAloader_calls", len(wire.loads))
    record_property("actual_backend", result or "None")
    assert result == "ollama"
    assert wire.loads == [], "plain HTTP must not initialize a CA trust store"
    assert len(wire.requests) == 1
    assert wire.requests[0].url.scheme == "http"
    assert wire.contexts[0].cert_store_stats()["x509_ca"] == 0


def test_http_probe_survives_unavailable_certificate_store(wire, monkeypatch, record_property):
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append("CA")
        raise RuntimeError("CA store initialization must not run for HTTP")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", unavailable)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", unavailable)
    result = mb._probe_local_backend("http://127.0.0.1:8090")
    record_property("CAloader_calls", len(attempts))
    record_property("actual_backend", result or "None")
    assert result == "ollama"
    assert len(wire.requests) == 1


def test_https_keeps_real_verified_context(wire, monkeypatch):
    # trust_env=False must ignore these without changing the HTTPS trust path.
    monkeypatch.setenv("SSL_CERT_FILE", "missing-certificate-file-886")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-used.invalid:886")
    assert mb._probe_local_backend("https://127.0.0.1:8090") == "ollama"
    assert wire.loads
    context = wire.contexts[0]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0
    assert wire.clients[0].trust_env is False


def test_https_trust_store_failure_stays_best_effort_and_cached(wire, monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append("CA")
        raise RuntimeError("CA store unavailable")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", unavailable)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", unavailable)
    root = "https://127.0.0.1:8090"
    assert mb._probe_local_backend(root) is None
    assert len(calls) == 1 and wire.requests == []
    assert mb._probe_local_backend(root) is None
    assert len(calls) == 1, "constructor failures keep the existing negative TTL cache"


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirects_are_not_followed_even_from_http_to_https(wire, scheme, code):
    wire.reply = lambda request: httpx.Response(code, headers={"Location": "https://other.invalid/private"})
    assert mb._probe_local_backend(f"{scheme}://127.0.0.1:8090") is None
    assert [request.url.path for request in wire.requests] == ["/api/version", "/props"]
    assert all(request.url.host == "127.0.0.1" for request in wire.requests)
    assert all(client.follow_redirects is False for client in wire.clients)
    if scheme == "http":
        assert wire.loads == [], "a redirect cannot move an unverified HTTP client onto HTTPS"
    else:
        assert wire.loads and wire.contexts[0].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("version,props,expected", [(200, 200, "ollama"),
    (404, 200, "llamacpp"), (500, 404, None)])
def test_probe_order_statuses_and_client_contract_unchanged(wire, version, props, expected):
    wire.reply = lambda request: httpx.Response(version if request.url.path == "/api/version" else props)
    assert mb._probe_local_backend("http://127.0.0.1:8090/") == expected
    assert [r.url.path for r in wire.requests] == (["/api/version"] if version == 200 else ["/api/version", "/props"])
    client = wire.clients[0]
    assert client.trust_env is False and client.follow_redirects is False
    for request in wire.requests:
        assert request.method == "GET"
        assert request.extensions["timeout"] == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}


@pytest.mark.parametrize("fallback", [True, False])
def test_probe_exceptions_still_try_props_and_cache(wire, monkeypatch, fallback):
    clock = [100.0]
    monkeypatch.setattr(mb.time, "time", lambda: clock[0])

    def reply(request):
        if request.url.path == "/api/version" or not fallback:
            raise httpx.ReadTimeout("synthetic wire timeout", request=request)
        return httpx.Response(200)

    wire.reply = reply
    root = "http://127.0.0.1:8090"
    expected = "llamacpp" if fallback else None
    assert mb._probe_local_backend(root) == expected
    assert len(wire.requests) == 2
    clock[0] = 129.999
    assert mb._probe_local_backend(root) == expected and len(wire.requests) == 2
    clock[0] = 130.0
    assert mb._probe_local_backend(root) == expected and len(wire.requests) == 4
    assert len(wire.clients) == 2 and wire.contexts[0] is not wire.contexts[1]


def test_http_and_https_keep_separate_roots_and_cached_results(wire):
    assert mb._probe_local_backend("http://127.0.0.1:8090") == "ollama"
    assert mb._probe_local_backend("https://127.0.0.1:8090") == "ollama"
    assert len(wire.clients) == 2
    loads = len(wire.loads)
    assert loads > 0
    assert mb._probe_local_backend("http://127.0.0.1:8090") == "ollama"
    assert mb._probe_local_backend("https://127.0.0.1:8090") == "ollama"
    assert len(wire.clients) == 2 and len(wire.loads) == loads
