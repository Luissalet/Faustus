"""`GET /api/creator/availability`: the Creator flag as an answer, not a 404.

Studio asks on every load whether to show the Creator. Asking `/capabilities`
returned 404 while `creator_enabled` was off, which left a red error in the
console each time; `/availability` answers 200 in both states and everything
else stays 404 while the flag is off (CONTRATO rule 5).
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.creator_routes import setup_creator_routes
from src import settings as settings_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client(monkeypatch, enabled: bool) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: enabled if key == "creator_enabled" else default)
    app = FastAPI()
    app.include_router(setup_creator_routes())
    return TestClient(app)


def test_availability_is_200_and_false_while_the_flag_is_off(monkeypatch):
    client = _client(monkeypatch, enabled=False)
    resp = client.get("/api/creator/availability")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_availability_is_true_once_the_flag_is_on(monkeypatch):
    client = _client(monkeypatch, enabled=True)
    resp = client.get("/api/creator/availability")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}


def test_the_other_creator_routes_stay_404_while_the_flag_is_off(monkeypatch):
    client = _client(monkeypatch, enabled=False)
    assert client.get("/api/creator/capabilities").status_code == 404
    assert client.get("/api/creator/documents?project_id=p").status_code == 404


def test_availability_reveals_nothing_beyond_the_flag(monkeypatch):
    client = _client(monkeypatch, enabled=True)
    assert set(client.get("/api/creator/availability").json()) == {"enabled"}


@pytest.mark.parametrize("path", [
    "studio/src/adapters/creator.ts",
])
def test_the_studio_probe_uses_availability_not_capabilities(path):
    """The client half of the fix: the nav probe must not be able to 404."""
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        src = fh.read()
    hook = src[src.index("export function useCreatorAvailable"):]
    assert "getAvailability()" in hook
    assert "getCapabilities(" not in hook
