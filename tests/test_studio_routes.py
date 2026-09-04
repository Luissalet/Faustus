"""The Studio deep-link routes, and the catch-all that must never exist (UI-020).

The Studio shell routes on the client, so a reload or a pasted link has to
come back with the SPA. The tempting way to do that is one wildcard route.
It is also the way to break the API: a catch-all answers every unmatched
/api/... path with HTML, and a caller that expected JSON gets a parse error
instead of a 404 it could handle.

So the routes are whitelisted, and the second test here is the one that
matters - it fails the moment somebody "simplifies" them into a wildcard.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app

STUDIO_ROUTES = [
    "/",
    "/studio",
    "/projects",
    "/projects/100d012d1ff3",
    "/library",
    "/automations",
    "/activity",
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("path", STUDIO_ROUTES)
def test_studio_route_serves_the_shell(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, f"{path} should serve the SPA"
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()


SHELL_MARKER = "faustus_studio_shell"


def test_unknown_api_path_does_not_get_the_shell(client: TestClient) -> None:
    """The whole reason the routes above are a whitelist.

    The status code is deliberately not asserted: with auth enabled the app's
    middleware redirects unknown paths to /login before routing sees them, so
    pinning 404 here would be pinning the auth configuration, not the routing.
    What must hold in every configuration is that an unmatched /api/... path
    never comes back as the SPA - which is exactly what a `{path:path}`
    catch-all would do.
    """
    response = client.get("/api/definitely-not-a-real-endpoint")
    assert SHELL_MARKER not in response.text


def test_unknown_page_does_not_get_the_shell(client: TestClient) -> None:
    """A path nobody declared is not silently the app."""
    response = client.get("/not-a-studio-route-either")
    assert SHELL_MARKER not in response.text


def test_studio_bootstrap_is_off_by_default() -> None:
    """The pilot must not load itself on a plain visit."""
    html = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "faustus_studio_shell" in html, "the pilot bootstrap is missing"
    # The bundle is appended by script, never with a plain <script src> tag
    # that would run for everybody.
    assert '<script type="module" src="/static/studio/studio.js"' not in html
