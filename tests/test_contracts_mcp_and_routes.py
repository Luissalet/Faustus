"""The contracts surface an outside coordinator sees: the two routes and the
two MCP tools built on them.

The routes are pure, and the test says so out loud — validating a manifest
must not create a skill, a run or a file. What the MCP renderers are checked
for is the one line that would be easy to drop: a manifest that asks for the
network but forgets to declare the approval has to SAY so in the text the
coordinator reads, not only in the JSON nobody prints.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_servers import workers_server as ws
from routes.contracts_routes import setup_contracts_routes


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("routes.contracts_routes.require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_contracts_routes())
    return TestClient(app)


GOOD = {
    "id": "document.report", "version": "1.0.0", "title": "Write a report",
    "outputs": {"report": "artifact:document"},
    "permissions": {"backends": ["docker_workspace"]},
}

SNEAKY = {
    "id": "media.publish", "version": "2.0.0", "title": "Publish a clip",
    "outputs": {"clip": "artifact:video"},
    "permissions": {"network": True, "secrets": ["youtube"], "backends": ["media_worker"]},
    "approval": {"required_when": ["publish"]},
}


def test_the_catalogue_keeps_intent_and_observation_apart(client):
    body = client.get("/api/contracts/backends").json()
    rows = {r["declared"]["id"]: r for r in body["backends"]}
    assert rows["local"]["observed"]["state"] == "available"
    assert rows["docker_workspace"]["observed"]["state"] == "unavailable"
    assert body["docker"]["means"] == "a CLI on PATH does not prove a daemon is running"


def test_a_rejection_arrives_as_an_answer_not_an_http_error(client):
    r = client.post("/api/contracts/skill/validate", json={"manifest": {**GOOD, "permisions": {}}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["path"] == "skill"
    assert "did you mean 'permissions'" in body["error"]["message"]


def test_a_body_that_is_not_json_is_the_only_4xx(client):
    assert client.post("/api/contracts/skill/validate", content=b"not json").status_code == 400
    assert client.post("/api/contracts/skill/validate", json=[1, 2]).status_code == 400


def test_validating_a_manifest_installs_nothing(client, tmp_path, monkeypatch):
    """Pure means pure: the route may not reach a skills manager or the disk."""
    import services.memory.skills as skills_mod

    def explode(*a, **k):                      # pragma: no cover - must not run
        raise AssertionError("validation touched the skills store")

    monkeypatch.setattr(skills_mod.SkillsManager, "__init__", explode)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert client.post("/api/contracts/skill/validate", json={"manifest": GOOD}).json()["ok"]
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ── the MCP surface ────────────────────────────────────────────────────────

def test_both_tools_are_offered_and_described_without_promising_a_side_effect():
    names = {t.name for t in ws.TOOLS}
    assert {"contracts_backends", "contracts_validate_skill"} <= names
    validate = next(t for t in ws.TOOLS if t.name == "contracts_validate_skill")
    assert "WITHOUT installing" in validate.description
    assert validate.inputSchema["required"] == ["manifest"]


def test_the_backend_rendering_says_why_something_is_unavailable(client):
    text = ws.render_backends(client.get("/api/contracts/backends").json())
    assert "local [available]" in text
    assert "docker_workspace [unavailable]" in text
    assert "not built yet" in text
    assert "attended-only" in text
    assert "a CLI on PATH does not prove a daemon is running" in text


def test_the_coordinator_reads_the_undeclared_approvals_in_the_text(client):
    body = client.post("/api/contracts/skill/validate", json={"manifest": SNEAKY}).json()
    text = ws.render_validation(body)
    assert text.startswith("OK ·")
    assert "approval cards: network, publish, secrets" in text
    assert "UNDECLARED but earned by the permissions asked for: network, secrets" in text
    assert "no media_worker: not_implemented" in text


def test_a_rejection_renders_as_the_field_that_is_wrong(client):
    body = client.post("/api/contracts/skill/validate",
                       json={"manifest": {**GOOD, "version": "1"}}).json()
    text = ws.render_validation(body)
    assert text.startswith("REJECTED at skill.version:")
    assert "semantic version" in text


def test_a_clean_manifest_does_not_invent_an_undeclared_line(client):
    text = ws.render_validation(
        client.post("/api/contracts/skill/validate", json={"manifest": GOOD}).json())
    assert "UNDECLARED" not in text
    assert "approval cards: none" in text
