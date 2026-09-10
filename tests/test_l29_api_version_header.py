"""L29 (integrates L21's "necessary in app.py" note): `/api/version` now also
answers the negotiated ARCH-01 wire version via the `X-Faustus-Api-Version`
header (src/api_version.py), so a client can learn what the server speaks
from this cheap endpoint before ever opening the chat SSE stream.

Reverting the header stamp in app.py's `get_version()` makes the assertion
below fail with a missing header; the JSON body is untouched either way
(additive-only, COMUN.md rule 3).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import app
from src import api_version


def test_version_endpoint_reports_the_negotiated_api_version_header() -> None:
    client = TestClient(app)
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.headers.get(api_version.API_VERSION_HEADER) == api_version.API_VERSION
    # Body shape is unchanged — the header is additive, not a body field.
    body = response.json()
    assert "version" in body and "build" in body and "served_studio" in body
