from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.middleware import SecurityHeadersMiddleware
import pytest


def test_agora_can_embed_the_configured_hub_only(monkeypatch):
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
        assert "frame-src 'self' http://127.0.0.1:8810;" in csp
        assert "connect-src 'self';" in csp
        assert "frame-ancestors 'none'" in csp
        assert 'prefix' not in csp and 'x=y' not in csp
    assert "frame-src 'self';" in client.get('/studio').headers['content-security-policy']
    assert "frame-src 'self';" in client.get('/api/health').headers['content-security-policy']


def test_agora_frame_follows_configured_https_origin(monkeypatch):
    from src import hoard_hub
    monkeypatch.setattr(hoard_hub, 'hub_location', lambda: {'url': 'https://hub.internal:9443/hub/'})
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/agora')
    def shell():
        return {'ok': True}

    csp = TestClient(app).get('/agora').headers['content-security-policy']
    assert "frame-src 'self' https://hub.internal:9443;" in csp
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
    assert "frame-src 'self';" in response.headers['content-security-policy']
