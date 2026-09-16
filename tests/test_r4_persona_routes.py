"""tests/test_r4_persona_routes.py — R4: `/api/personas/*` HTTP surface.

Modeled on tests/test_l92_board_routes.py's FastAPI TestClient pattern:
AUTH_ENABLED=false so require_user passes through, and the personas
registry's DATA_DIR pointed at an isolated tmp dir.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import persona_routes  # noqa: E402
from src.personas import registry  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(registry, "DATA_DIR", str(tmp_path))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(persona_routes.setup_persona_routes())
    return TestClient(app)


def test_list_personas(client):
    resp = client.get("/api/personas")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["personas"]) == 16
    assert data["errors"] == []


def test_get_one_persona(client):
    resp = client.get("/api/personas/backend-python")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Backend Python Engineer"


def test_get_unknown_persona_404(client):
    resp = client.get("/api/personas/does-not-exist")
    assert resp.status_code == 404


def test_render_persona(client):
    resp = client.get("/api/personas/code-reviewer/render")
    assert resp.status_code == 200
    assert "Code Reviewer" in resp.text


def test_put_persona_creates_override(client):
    content = "---\nname: Custom Backend\ndivision: engineering\n---\n\nCustom identity text.\n"
    resp = client.put("/api/personas/backend-python", json={"content": content})
    assert resp.status_code == 200
    assert resp.json()["source"] == "user"

    resp2 = client.get("/api/personas/backend-python")
    assert resp2.json()["name"] == "Custom Backend"


def test_put_invalid_frontmatter_rejected(client):
    resp = client.put("/api/personas/broken", json={"content": "not frontmatter"})
    assert resp.status_code == 400


def test_delete_user_override(client):
    content = "---\nname: Custom\n---\n\nCustom identity text here.\n"
    client.put("/api/personas/backend-python", json={"content": content})
    resp = client.delete("/api/personas/backend-python")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    resp2 = client.get("/api/personas/backend-python")
    assert resp2.json()["source"] == "builtin"


def test_delete_nonexistent_override_404(client):
    resp = client.delete("/api/personas/never-overridden")
    assert resp.status_code == 404
