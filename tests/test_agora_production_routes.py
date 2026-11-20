"""Studio links and previews for decisions that remain owned by the Hub."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
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


@pytest.mark.parametrize("operation", ["gate_decide", "draft_decide", "draft_decide/"])
def test_studio_refuses_agent_channel_before_contacting_hub(studio, monkeypatch, operation):
    def unexpected(*args, **kwargs):
        pytest.fail("The decision must be refused before reaching the Hub")

    monkeypatch.setattr(agora_routes, "hub_request", unexpected)
    result = studio.post("/api/agora/x/" + operation, headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}, json={})
    assert result.status_code == 403
