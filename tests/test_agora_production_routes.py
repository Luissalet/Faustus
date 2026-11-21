"""Studio links and previews for decisions that remain owned by the Hub."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN, SecurityHeadersMiddleware
from routes import agora_routes, hoard_hub_routes


@pytest.fixture
def studio(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(hoard_hub_routes.hoard_hub, "hub_location", lambda: {"url": "http://127.0.0.1:8958"})
    app = FastAPI()
    app.include_router(agora_routes.setup_agora_routes())
    app.include_router(hoard_hub_routes.setup_hoard_hub_routes())
    return TestClient(app)


@pytest.mark.parametrize("key", ["gate-16", "draft-16"])
def test_decision_link_opens_exact_gate_in_the_configured_hub(studio, key):
    result = studio.get("/api/hoard/agora/open", params={"space_id": "test-room", "decision": key}, follow_redirects=False)
    assert result.status_code == 307
    assert result.headers["Location"] == f"http://127.0.0.1:8958/?space_id=test-room&decision={key}#hub/orders"
    assert studio.get("/api/hoard/agora/open", params={"decision": "unknown-16"}).status_code == 400


def test_only_piece_images_are_proxied(studio, monkeypatch):
    answers = iter(["image/png", "text/html"])

    def read(method, path, **options):
        assert method == "GET" and path == "/api/agora/piece_file/16"
        assert options["query"] == {"preview": "0"} and options["raw"]
        return 200, b"example bytes", next(answers)

    monkeypatch.setattr(agora_routes, "hub_request", read)
    result = studio.get("/api/agora/piece-preview/16?preview=0")
    assert result.status_code == 200 and result.content == b"example bytes"
    assert result.headers["Content-Type"] == "image/png"
    assert studio.get("/api/agora/piece-preview/16?preview=0").status_code == 404


@pytest.mark.parametrize("operation", ["gate_decide", "draft_decide", "draft_decide/", "production_decide"])
def test_studio_refuses_non_person_decisions_before_contacting_hub(studio, monkeypatch, operation):
    def unexpected(*args, **kwargs):
        pytest.fail("The decision must be refused before reaching the Hub")

    monkeypatch.setattr(agora_routes, "hub_request", unexpected)
    for headers in (
        {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
        {"Authorization": "Bearer fixture"},
        {"Origin": "http://another.example"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        result = studio.post("/api/agora/x/" + operation, headers=headers, json={})
        assert result.status_code == 403
        assert result.json()["code"] == "studio_person_required"


@pytest.mark.parametrize(("auth_enabled", "signed_in", "state", "url", "remote", "order_base"), [
    ("false", False, "yes", "http://127.0.0.1:8958", True, ""),
    ("false", False, "yes", "http://127.0.0.1:8958", False, ""),
    ("true", False, "no", "http://testserver:8958", False, "http://testserver:8958"),
    ("true", True, "yes", "http://testserver:8958", False, "http://testserver:8958"),
])
def test_development_panel_keeps_assets_on_studio_and_frames_only_here(
        studio, monkeypatch, auth_enabled, signed_in, state, url, remote, order_base):
    html = (b'<html lang="es"><head><base href="/agora/">'
            b'<script src="/ui/browser-session.js"></script>'
            b'<script type="module" src="/agora/assets/index-QA.js"></script></head></html>')

    def get(method, path, **options):
        assert method == "GET" and options["raw"]
        if path == "/agora/":
            return 200, html, "text/html"
        assert path == "/agora/assets/index-QA.js"
        return 200, b"export {};", "text/javascript"

    monkeypatch.setattr(hoard_hub_routes, "hub_request", get)
    monkeypatch.setenv("AUTH_ENABLED", auth_enabled)
    monkeypatch.setattr(agora_routes.hub_link, "location", lambda: {"url": url, "remote": remote})
    from routes.auth_routes import SESSION_COOKIE
    studio.app.state.auth_manager = SimpleNamespace(
        is_configured=True, is_admin=lambda user: user == "fixture-admin",
        get_username_for_token=lambda token: "fixture-admin" if token == "valid-session" else None,
    )

    @studio.app.middleware("http")
    async def admin_state(request, call_next):
        request.state.current_user = "fixture-admin"
        return await call_next(request)

    if signed_in:
        studio.cookies.set(SESSION_COOKIE, "valid-session")
    studio.app.add_middleware(SecurityHeadersMiddleware)
    panel = "/api/hoard/agora/panel/"
    response = studio.get(panel + "?space_id=test-room&embedded=1")
    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert f'data-studio-write="{state}"' in response.text
    assert f'data-studio-order-base="{order_base}"' in response.text
    assert f'<base href="{panel}">' in response.text
    assert f'src="{panel}assets/index-QA.js"' in response.text
    assert "browser-session.js" not in response.text
    asset = studio.get(panel + "assets/index-QA.js")
    assert asset.text == "export {};"
    assert asset.headers["X-Frame-Options"] == "DENY"
    assert studio.get(panel + "assets/index.html").status_code == 404


@pytest.mark.parametrize(("lang", "message"), [
    ("es", "No se puede abrir Desarrollo"),
    ("en", "Development is unavailable"),
])
def test_development_panel_can_explain_a_hub_outage(studio, monkeypatch, lang, message):
    monkeypatch.setattr(hoard_hub_routes, "hub_request", lambda *a, **k: (None, None, ""))
    result = studio.get("/api/hoard/agora/panel/", params={"lang": lang})
    assert result.status_code == 503
    assert message in result.text and 'role="alert"' in result.text


@pytest.mark.parametrize("operation", ["console/message", "production_decide"])
@pytest.mark.parametrize("auth_enabled", ["false", "true"])
def test_development_writes_use_the_person_and_respect_sign_in(studio, monkeypatch, operation, auth_enabled):
    forwarded = []

    def post(method, path, **options):
        forwarded.append((method, path, options["body"]))
        return 200, {"ok": True}, "application/json"

    monkeypatch.setattr(agora_routes, "hub_request", post)
    path = "/api/agora/development/" + operation
    from routes.auth_routes import SESSION_COOKIE
    studio.app.state.auth_manager = SimpleNamespace(
        is_configured=True, is_admin=lambda user: user == "fixture-admin",
        get_username_for_token=lambda token: "fixture-admin" if token == "valid-session" else None,
    )

    @studio.app.middleware("http")
    async def admin_state(request, call_next):
        request.state.current_user = "fixture-admin"
        return await call_next(request)

    monkeypatch.setenv("AUTH_ENABLED", auth_enabled)
    if auth_enabled == "true":
        result = studio.post(path, json={})
        assert result.status_code == 403 and result.json()["code"] == "studio_session_required"
        studio.cookies.set(SESSION_COOKIE, "expired")
        result = studio.post(path, json={})
        assert result.status_code == 403 and result.json()["code"] == "studio_session_required"
        studio.cookies.set(SESSION_COOKIE, "valid-session")
    for headers in (
        {"Origin": "http://another.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Authorization": "Bearer fixture"},
        {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
    ):
        result = studio.post(path, headers=headers, json={})
        assert result.status_code == 403 and result.json()["code"] == "studio_person_required"
    assert not forwarded
    body = {"space_id": "test-room"}
    assert studio.post(path, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
                       json={**body, "_person": True}).json() == {"ok": True}
    assert forwarded == [("POST", "/api/agora/" + operation, body)]


@pytest.mark.parametrize(("url", "remote", "expected"), [
    ("http://testserver:8958", False, "http://testserver:8958"),
    ("http://127.0.0.1:8958", False, None),
    ("http://testserver:8958", True, None),
])
def test_studio_reads_shared_pending_decisions_from_hub(studio, monkeypatch, url, remote, expected):
    queue = {"ok": True, "rows": [{"key": "draft-2", "space": "test-room"}], "counts": {"test-room": 1}}

    def read(method, path, *, query):
        assert method == "GET"
        assert path == "/api/agora/pending_decisions"
        assert query == {"all_spaces": "1"}
        return 200, queue, "application/json"

    monkeypatch.setattr(agora_routes, "hub_request", read)
    monkeypatch.setattr(agora_routes.hub_link, "location", lambda: {"url": url, "remote": remote})
    result = studio.get("/api/agora/x/pending_decisions?all_spaces=1")
    assert result.status_code == 200
    assert result.json() == {**queue, "order_base": expected}


def test_studio_proxies_only_the_shared_card_assets(studio, monkeypatch):
    def read(method, path, **options):
        assert method == "GET"
        assert path == "/ui/production_decisions_card.js"
        assert options["raw"]
        return 200, b"export const renderDecisionCards = () => {};", "text/javascript"

    monkeypatch.setattr(agora_routes, "hub_request", read)
    result = studio.get("/api/agora/decision-assets/production_decisions_card.js")
    assert result.headers["Content-Type"].startswith("text/javascript")
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    assert studio.get("/api/agora/decision-assets/other.js").status_code == 404
