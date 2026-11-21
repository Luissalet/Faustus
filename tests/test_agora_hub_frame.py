from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.middleware import SecurityHeadersMiddleware
import pytest


DOCK_FRAME_SOURCES = {"'self'", "http://127.0.0.1:*", "http://localhost:*"}


@pytest.fixture(autouse=True)
def no_external_dock_origins(monkeypatch):
    from src import app_dock

    monkeypatch.setattr(app_dock, "framable_origins", lambda: ())


def frame_sources(csp):
    directive = next(part for part in csp.split(";") if part.strip().startswith("frame-src "))
    return set(directive.split()[1:])


def test_agora_includes_the_configured_hub_origin(monkeypatch):
    from src import hoard_hub
    monkeypatch.setattr(hoard_hub, 'hub_location', lambda: {'url': 'http://127.0.0.1:8810/prefix?x=y'})
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/{path:path}')
    def shell(path):
        return {'ok': True}

    client = TestClient(app)
    for path in ('/agora', '/agora/'):
        csp = client.get(path).headers['content-security-policy']
        assert frame_sources(csp) == DOCK_FRAME_SOURCES | {"http://127.0.0.1:8810"}
        assert "connect-src 'self';" in csp
        assert "frame-ancestors 'none'" in csp
        assert 'prefix' not in csp and 'x=y' not in csp
    for path in ("/studio", "/api/health"):
        assert frame_sources(client.get(path).headers["content-security-policy"]) == DOCK_FRAME_SOURCES


def test_agora_frame_follows_configured_https_origin(monkeypatch):
    from src import hoard_hub
    monkeypatch.setattr(hoard_hub, 'hub_location', lambda: {'url': 'https://hub.example.test:9443/hub/'})
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/agora')
    def shell():
        return {'ok': True}

    csp = TestClient(app).get('/agora').headers['content-security-policy']
    assert frame_sources(csp) == DOCK_FRAME_SOURCES | {"https://hub.example.test:*", "https://hub.example.test:9443"}
    assert "script-src 'self' 'nonce-" in csp


@pytest.mark.parametrize('url', ['http://localhost:bad', 'http://[broken'])
def test_bad_hub_location_keeps_agora_available(monkeypatch, url):
    from src import hoard_hub
    monkeypatch.setattr(hoard_hub, 'hub_location', lambda: {'url': url})
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/agora')
    def shell():
        return {'ok': True}

    response = TestClient(app, raise_server_exceptions=False).get('/agora')
    assert response.status_code == 200
    assert frame_sources(response.headers["content-security-policy"]) == DOCK_FRAME_SOURCES
