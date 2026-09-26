"""The runner a chat's runnable html block loads: its own policy, sandboxed."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.sandbox_routes import RUNNER_CSP, setup_sandbox_routes
from tests.test_document_render_pdf_iframe import _dispatch


def test_runner_page_is_served_and_waits_for_its_parent():
    app = FastAPI()
    app.include_router(setup_sandbox_routes())
    r = TestClient(app).get("/api/sandbox/app")
    assert r.status_code == 200 and "faustusAppReady" in r.text and "window.parent === window" in r.text


def test_runner_gets_its_own_sandboxed_offline_policy():
    resp = asyncio.run(_dispatch("/api/sandbox/app"))
    csp = resp.headers["Content-Security-Policy"]
    assert csp == RUNNER_CSP
    assert csp.startswith("sandbox allow-scripts") and "default-src 'none'" in csp
    assert "allow-same-origin" not in csp and "connect-src" not in csp
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    other = asyncio.run(_dispatch("/api/sandbox/app2"))
    assert other.headers["X-Frame-Options"] == "DENY"
