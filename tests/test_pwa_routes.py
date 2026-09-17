"""routes/pwa_routes.py (lot P-B): `/sw.js` and `/manifest.webmanifest`,
served at the origin root, unauthenticated, with the headers a service
worker registration and an install prompt need.

Uses the real `app` (`from app import app`), the same "prove it against
app.py itself" shape `tests/test_l55_app_wiring.py` already uses, rather
than a bare FastAPI() with only this router mounted — that would prove the
routes exist but nothing about whether `AUTH_EXEMPT_EXACT` actually lets an
anonymous request through. No auth headers are sent at all: that is the
whole point of the test.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_sw_js_is_public_and_correctly_typed():
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert resp.headers["Service-Worker-Allowed"] == "/"
    assert b"addEventListener('push'" in resp.content


def test_manifest_webmanifest_is_public_and_correctly_typed():
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    body = resp.json()
    assert body["display"] == "standalone"
    assert body["start_url"] == "/?source=pwa"
