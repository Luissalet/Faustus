"""The permission card's "Always for this workspace folder" answers can be
listed and taken back over HTTP (Settings › Security), and the model's own
loopback token cannot take them back for the user."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.approvals_routes import setup_approvals_routes
from src import tool_approval_grants

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_approval_grants, "_path", lambda: str(tmp_path / "grants.json"))
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_approvals_routes())
    return TestClient(app)


def test_list_and_revoke_a_folder(client, tmp_path):
    folder = tmp_path / "proyecto"
    folder.mkdir()
    tool_approval_grants.grant("", str(folder), tool="bash")
    listed = client.get("/api/approvals/folder-grants").json()["grants"]
    assert len(listed) == 1 and listed[0]["workspace"] == str(folder) and listed[0]["tool"] == "bash"

    gone = client.post("/api/approvals/folder-grants/revoke", json={"workspace": str(folder)})
    assert gone.status_code == 200 and gone.json()["ok"] is True
    assert client.get("/api/approvals/folder-grants").json()["grants"] == []
    again = client.post("/api/approvals/folder-grants/revoke", json={"workspace": str(folder)})
    assert again.json() == {"ok": False, "reason": "not_found"}


def test_revoke_needs_a_workspace(client):
    assert client.post("/api/approvals/folder-grants/revoke", json={}).status_code == 400


def test_the_tool_token_cannot_revoke_for_the_user(client, tmp_path):
    tool_approval_grants.grant("", str(tmp_path))
    r = client.post("/api/approvals/folder-grants/revoke", json={"workspace": str(tmp_path)},
                    headers=TOOL_HEADERS)
    assert r.status_code in (401, 403)
    assert tool_approval_grants.is_granted("", str(tmp_path))
