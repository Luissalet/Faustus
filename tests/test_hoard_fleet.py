import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI

from src import hoard_hub
from routes.hoard_hub_routes import setup_hoard_hub_routes


@pytest.mark.asyncio
async def test_fleet_proxy_uses_configured_hub_and_returns_only_published_metadata(monkeypatch):
    payload = {"ok": True, "generated_ts": 100, "items": [{"id": "qa", "label": "QA", "state": "silent"}],
               "brief": {"blocked": [{"id": "qa", "label": "QA", "state": "silent"}]},
               "counts": {"silent": 1}, "coverage": {"errors": {}}, "private_extra": "QA private marker"}
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(hoard_hub, "hub_location", lambda: {"url": f"http://127.0.0.1:{server.server_port}", "token_file": "", "configured": True})
    from types import SimpleNamespace
    import core.middleware as auth
    monkeypatch.setattr(auth, 'auth_disabled', lambda: False)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user == 'qa-admin')
    @app.middleware('http')
    async def fixture_admin(request, call_next):
        request.state.current_user = 'qa-admin'
        return await call_next(request)
    app.include_router(setup_hoard_hub_routes())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/hoard/fleet")
        assert response.status_code == 200
        assert response.json()["counts"]["silent"] == 1
        assert "QA private marker" not in response.text
        assert calls == ["/api/fleet"]
    finally: server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize("status,data", [(None, None), (404, {}), (200, {"ok": False}), (200, {"ok": True, "items": []})])
def test_fleet_absent_or_malformed_does_not_claim_healthy(monkeypatch, status, data):
    monkeypatch.setattr(hoard_hub, "_call", lambda *args, **kwargs: (status, data, {"url": "http://127.0.0.1:1"}))
    result = hoard_hub.get_fleet()
    assert result["ok"] is False
    assert result["error"] == "fleet unavailable"


@pytest.mark.asyncio
async def test_machine_wide_task_labels_are_not_proxied_to_a_non_admin(monkeypatch):
    from types import SimpleNamespace
    import core.middleware as auth
    calls = []
    monkeypatch.setattr(auth, 'auth_disabled', lambda: False)
    monkeypatch.setattr(hoard_hub, 'get_fleet', lambda: calls.append('read') or {'ok': True, 'items': []})
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user == 'admin')
    @app.middleware('http')
    async def fixture_user(request, call_next):
        request.state.current_user = request.headers.get('X-Fixture-User')
        return await call_next(request)
    app.include_router(setup_hoard_hub_routes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        denied = await client.get('/api/hoard/fleet', headers={'X-Fixture-User': 'guest'})
        assert denied.status_code == 403 and not calls
        allowed = await client.get('/api/hoard/fleet', headers={'X-Fixture-User': 'admin'})
        assert allowed.status_code == 200 and calls == ['read']
