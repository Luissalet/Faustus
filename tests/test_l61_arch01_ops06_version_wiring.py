"""Lote 61 — ARCH-01/OPS-06: `src/api_version.py`'s negotiation used to cover
only the chat SSE wire (and, since L29, the standalone `/api/version` probe).
This closes the rest of ARCH-01's literal acceptance ("un cliente antiguo
falla de forma comprensible ANTE UN EVENTO NUEVO OBLIGATORIO" -- generalized
here to "ante cualquier endpoint /api/*", per this lote's brief) and OPS-06's
two previously-unwired functions:

* every `/api/*` response carries `X-Faustus-Api-Version`, via
  `core/middleware.py::SecurityHeadersMiddleware` -- not just chat/version;
* a client that IDENTIFIES as older than `MIN_CLIENT_VERSION` gets a
  comprehensible 426 on ANY `/api/*` endpoint, not only chat_stream/resume;
* `api_version.openapi_version_extension()` is merged into `app.openapi()`;
* `api_version.client_adaptation_notice()` is surfaced on `/api/version`.

Uses a real FastAPI app + TestClient (not the production `app.py`, which
pulls in the whole server) so the full middleware stack — the actual thing
a client talks to — is what's under test, the same pattern
tests/test_security_headers_middleware.py already uses for this middleware.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware
from src import api_version


def _build_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/whatever")
    def whatever():
        return {"ok": True}

    @app.get("/api/version")
    def version():
        return {"version": "test"}

    @app.get("/api/health")
    def health():
        return {"status": "healthy"}

    @app.get("/not-api")
    def not_api():
        return {"ok": True}

    return app


def _client():
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# Header on every /api/* response
# ---------------------------------------------------------------------------

def test_every_api_response_carries_the_negotiated_version_header():
    response = _client().get("/api/whatever")
    assert response.status_code == 200
    assert response.headers.get(api_version.API_VERSION_HEADER) == api_version.API_VERSION


def test_non_api_routes_are_left_alone():
    """The header is additive scoped to /api/* -- nothing about this lote's
    change should touch a route outside that prefix."""
    response = _client().get("/not-api")
    assert response.status_code == 200
    assert api_version.API_VERSION_HEADER not in response.headers


def test_a_client_with_no_version_header_at_all_is_unaffected():
    """Every request before this scheme existed, and every existing test:
    treated as compatible, exactly as api_version.is_supported(None) says."""
    response = _client().get("/api/whatever")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 426 on any /api/* endpoint, not just chat
# ---------------------------------------------------------------------------

def test_an_old_client_gets_426_on_a_non_chat_endpoint():
    """This is the literal gap this lote closes: before, only
    routes/chat_routes.py's own two call sites (chat_stream, resume) checked
    this at all -- every other /api/* route ignored the header completely."""
    response = _client().get(
        "/api/whatever", headers={api_version.CLIENT_VERSION_HEADER: "0.1"},
    )
    assert response.status_code == 426
    body = response.json()
    assert "0.1" in body["detail"]
    assert api_version.MIN_CLIENT_VERSION in body["detail"]
    assert "error_class" in body
    assert response.headers.get(api_version.API_VERSION_HEADER) == api_version.API_VERSION


def test_a_current_client_is_never_rejected():
    response = _client().get(
        "/api/whatever", headers={api_version.CLIENT_VERSION_HEADER: api_version.API_VERSION},
    )
    assert response.status_code == 200


def test_version_and_health_stay_reachable_even_for_a_client_below_the_floor():
    """A client has to be ABLE to ask "what do you speak" (and confirm the
    process is even up) before it can decide whether it's compatible --
    gating the discovery endpoints themselves would be a lockout, not a
    comprehensible error."""
    for path in ("/api/version", "/api/health"):
        response = _client().get(path, headers={api_version.CLIENT_VERSION_HEADER: "0.1"})
        assert response.status_code == 200, path


def test_cors_preflight_is_never_426d():
    """A genuine preflight (OPTIONS + Access-Control-Request-Method) carries
    no credentials and must reach the browser's real request unmolested --
    matching `is_cors_preflight`'s existing use elsewhere in this file."""
    response = _client().options(
        "/api/whatever",
        headers={
            "Access-Control-Request-Method": "GET",
            api_version.CLIENT_VERSION_HEADER: "0.1",
        },
    )
    assert response.status_code != 426


# ---------------------------------------------------------------------------
# OPS-06: app.openapi() carries the extension
# ---------------------------------------------------------------------------

def test_app_openapi_is_wired_to_the_version_extension(monkeypatch):
    """app.py overrides `app.openapi` with a function that merges
    `api_version.openapi_version_extension()` into `info` -- exercised here
    against the real production app (importing it is the only way to prove
    the actual wiring, not a reimplementation of it)."""
    import app as app_module

    app_module.app.openapi_schema = None  # bust FastAPI's own cache
    schema = app_module.app.openapi()
    info = schema.get("info", {})
    assert info.get("x-api-version") == api_version.API_VERSION
    assert info.get("x-min-client-version") == api_version.MIN_CLIENT_VERSION
    assert info.get("x-api-version-header") == api_version.API_VERSION_HEADER
    assert info.get("x-client-version-header") == api_version.CLIENT_VERSION_HEADER
    assert info.get("x-deprecations") == []
    app_module.app.openapi_schema = None  # leave no cached schema behind for other tests


def test_openapi_schema_is_cached_like_fastapis_own():
    import app as app_module

    app_module.app.openapi_schema = None
    first = app_module.app.openapi()
    assert app_module.app.openapi() is first
    app_module.app.openapi_schema = None


# ---------------------------------------------------------------------------
# OPS-06: /api/version surfaces client_adaptation_notice()
# ---------------------------------------------------------------------------

def test_version_endpoint_carries_a_notice_for_a_supported_but_older_client(monkeypatch):
    import app as app_module

    monkeypatch.setattr(api_version, "API_VERSION", "2.5")
    monkeypatch.setattr(api_version, "MIN_CLIENT_VERSION", "2.0")
    client = TestClient(app_module.app)

    response = client.get("/api/version", headers={api_version.CLIENT_VERSION_HEADER: "2.1"})
    assert response.status_code == 200
    body = response.json()
    assert body["client_adaptation_notice"] is not None
    assert "2.1" in body["client_adaptation_notice"]
    assert "2.5" in body["client_adaptation_notice"]


def test_version_endpoint_notice_is_null_with_no_client_version_header():
    import app as app_module

    client = TestClient(app_module.app)
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.json()["client_adaptation_notice"] is None
