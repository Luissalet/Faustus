"""UX-10: the prompt library — typed variables, versions, and the one
acceptance line that matters: importing a template whose project reference is
stale must ask to resolve it rather than silently writing into whatever
project now holds that old id.
"""
from __future__ import annotations

import shutil

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core import middleware
from routes.prompts_routes import setup_prompt_routes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.prompts_routes.PROMPTS_FILE", str(tmp_path / "prompts.json"))
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User", "alice")
        return await call_next(request)

    app.include_router(setup_prompt_routes())
    return TestClient(app)


def _mk(client, **overrides):
    body = {"name": "Bug report", "description": "", "body": "Fix {{issue}} in {{area}}",
            "variables": [{"name": "issue", "type": "string", "required": True},
                          {"name": "area", "type": "string", "required": False, "default": "backend"}],
            "scope": "personal", "tags": ["dev"]}
    body.update(overrides)
    r = client.post("/api/prompts", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_and_render(client):
    created = _mk(client)
    assert created["version"] == 1
    listed = client.get("/api/prompts").json()["templates"]
    assert any(t["id"] == created["id"] for t in listed)

    rendered = client.post(f"/api/prompts/{created['id']}/render", json={"values": {"issue": "login crash"}})
    assert rendered.status_code == 200
    assert rendered.json()["text"] == "Fix login crash in backend"


def test_render_missing_required_variable_is_rejected(client):
    created = _mk(client)
    missing = client.post(f"/api/prompts/{created['id']}/render", json={"values": {}})
    assert missing.status_code == 400
    assert "issue" in missing.json()["detail"]


def test_create_rejects_undeclared_variable_in_body(client):
    r = client.post("/api/prompts", json={"name": "x", "body": "Hello {{name}}", "variables": [], "scope": "personal"})
    assert r.status_code == 400
    assert "name" in r.json()["detail"]


def test_personal_template_is_not_visible_to_another_owner(client):
    _mk(client)
    other = client.get("/api/prompts", headers={"X-Test-User": "bob"}).json()["templates"]
    assert other == []


def test_import_with_stale_project_path_requests_resolution_instead_of_writing_to_it(client):
    """The acceptance line: a template that names a project workspace nobody
    currently has must come back as needs_resolution, and nothing gets
    written under a guessed/foreign project id."""
    export_payload = {
        "name": "Deploy checklist", "description": "", "body": "Deploy {{service}}",
        "variables": [{"name": "service", "type": "string", "required": True}],
        "scope": "project", "tags": [],
        "source_project_workspace": "/old/machine/path/that-project",
    }
    imported = client.post("/api/prompts/import", json={"templates": [export_payload]})
    assert imported.status_code == 200
    body = imported.json()
    assert imported.json()["imported"] == []
    assert len(body["needs_resolution"]) == 1
    assert body["needs_resolution"][0]["source_project_workspace"] == "/old/machine/path/that-project"

    # Nothing was written: the template must not be in the list at all.
    assert client.get("/api/prompts", params={"scope": "project"}).json()["templates"] == []


def test_import_with_resolve_mapping_attaches_to_the_chosen_project(client, monkeypatch):
    class FakeStore:
        def list(self, owner=None):
            return [{"id": "proj-42", "workspace": "/new/machine/path"}]

    monkeypatch.setattr("services.projects.get_store", lambda: FakeStore())
    export_payload = {
        "name": "Deploy checklist", "body": "Deploy {{service}}",
        "variables": [{"name": "service", "type": "string", "required": True}],
        "scope": "project", "source_project_workspace": "/old/machine/path/that-project",
    }
    imported = client.post("/api/prompts/import", json={
        "templates": [export_payload],
        "resolve": {"/old/machine/path/that-project": "proj-42"},
    })
    assert imported.status_code == 200
    body = imported.json()
    assert body["needs_resolution"] == []
    assert len(body["imported"]) == 1
    row = client.get(f"/api/prompts/{body['imported'][0]}").json()
    assert row["project_id"] == "proj-42"


