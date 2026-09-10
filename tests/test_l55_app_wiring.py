"""Lote 55 (item 1) — `routes/media_edit_routes.py` (MEDIA-03) and
`routes/integrations_routes.py` (CONN-01) existed as real, individually
tested routers (see `tests/test_p1_media_edit_routes.py`,
`tests/test_p1_conn_01_connector_registry.py`) but neither was ever
registered on the real `app.py`, so neither path answered on the server a
client actually talks to. This lote adds the two `include_router` calls next
to their neighbours (`setup_media_routes`/`setup_local_video_routes`); this
file proves it against `app` itself — `from app import app`, the same
"real app, not a rebuilt one" shape `tests/test_l29_api_version_header.py`
already uses — rather than rebuilding a bare FastAPI() with only these two
routers mounted, which would pass even if app.py never wired them.

`AUTH_ENABLED` is read once at `app.py` import time (see
`tests/test_localhost_bypass_acts_as_admin.py`'s note on that), so a fresh
test process here still boots with auth ON and no admin configured yet —
every unauthenticated `/api/...` call, matched route or not, comes back 401
"Setup required" and would prove nothing about wiring either way. The
existing internal-tool loopback bypass (`core/middleware.py`'s
`INTERNAL_TOOL_HEADER`/`INTERNAL_TOOL_TOKEN`, the same path the agent's own
in-process tool calls use to reach admin-gated routes) is the real,
already-there way past that — not a test-only shortcut — so it is what these
tests use, with `TestClient(app, client=("127.0.0.1", ...))` to satisfy the
"direct loopback, no proxy headers" check the bypass requires.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import routes.media_edit_routes as media_edit_routes
from app import app
from core import middleware


@pytest.fixture()
def client():
    return TestClient(app, client=("127.0.0.1", 51234))


@pytest.fixture()
def internal_headers():
    return {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


def test_media_edit_routes_are_registered_on_the_real_app(client, internal_headers, monkeypatch):
    # Owner resolution is orthogonal to routing (it is what MEDIA-03's own
    # route tests in tests/test_p1_media_edit_routes.py already cover in
    # depth); stub it so this test isolates the one thing item 1 is about —
    # does app.py's router table actually contain this path.
    monkeypatch.setattr(media_edit_routes, "_edit_owner", lambda request: "l55-tester")
    response = client.get("/api/media/edit/projects/does-not-exist", headers=internal_headers)
    # 404 "not_found" from media_edit_projects.get, reached through the real
    # router — not FastAPI's generic "Not Found" for an unmounted path (see
    # the contrast test below for what that looks like instead).
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "not_found"


def test_integrations_routes_are_registered_on_the_real_app(client, internal_headers):
    response = client.get("/api/connectors", headers=internal_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["connectors"] == []


def test_unmounted_sibling_path_still_answers_a_generic_404_for_contrast(client, internal_headers):
    """Sanity check for the two tests above: a path nothing registers comes
    back as the app's generic not-found handler, with no `reason` field —
    confirming the `reason` asserted above came from the mounted router's own
    error handling, not from a lucky match against the framework default."""
    response = client.get("/api/this-route-truly-does-not-exist-anywhere", headers=internal_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "Not Found"
    assert "reason" not in body
