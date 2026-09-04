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
    """Docker is stubbed *down* for these tests on purpose.

    Whether the daemon is running on the machine running the suite is not a
    property of the routes, and a test that asserted "available" would pass in
    CI for the wrong reason and fail on a laptop with Docker Desktop closed.
    What is being checked here is that the answer carries the evidence."""
    from src import capability_registry as registry
    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "unavailable",
        "backend_unavailable: the docker CLI is installed but the daemon did not answer",
        stamp))
    # The media engine is stubbed DOWN for the same reason as the daemon:
    # whether a ComfyUI happens to be running on the machine running the
    # tests is not a property of this code, and a route test that only passes
    # when it is absent fails on the first machine that has one.
    monkeypatch.setattr(registry, "_probe_comfyui", lambda stamp: Observation(
        "media_worker", "unavailable",
        "backend_unavailable: nothing answered at http://127.0.0.1:8188", stamp))
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
    # Declared as built, observed as not answering — the two halves disagree
    # on purpose, and both travel.
    assert rows["docker_workspace"]["declared"]["implemented"] is True
    assert rows["docker_workspace"]["observed"]["state"] == "unavailable"
    assert "daemon did not answer" in rows["docker_workspace"]["observed"]["evidence"]
    # The same disagreement for the media engine: the code exists, and the
    # engine is not answering. Both halves travel, and neither overrules the
    # other — which is the rule this endpoint exists for.
    assert rows["media_worker"]["declared"]["implemented"] is True
    assert rows["media_worker"]["observed"]["state"] == "unavailable"
    assert "nothing answered" in rows["media_worker"]["observed"]["evidence"]
    # And one that genuinely has no code says so instead.
    assert rows["remote_worker"]["declared"]["implemented"] is False
    assert "not implemented" in rows["remote_worker"]["observed"]["evidence"]
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
    assert "daemon did not answer" in text          # built, and not answering
    assert "media_worker [unavailable]" in text
    assert "nothing answered" in text               # built, and nothing is listening
    assert "remote_worker [unavailable]" in text
    assert "not built yet" in text                  # a different problem, said differently
    assert "attended-only" in text
    assert "a CLI on PATH does not prove a daemon is running" in text


def test_the_coordinator_reads_the_undeclared_approvals_in_the_text(client):
    body = client.post("/api/contracts/skill/validate", json={"manifest": SNEAKY}).json()
    text = ws.render_validation(body)
    assert text.startswith("OK ·")
    assert "approval cards: network, publish, secrets" in text
    assert "UNDECLARED but earned by the permissions asked for: network, secrets" in text
    # The backend it asked for cannot take it, and the line says why it was
    # asked rather than just that it failed — here, the engine is not running.
    assert "no media_worker: unavailable" in text


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


# ── planning a run without running it ──────────────────────────────────────

def test_planning_says_where_it_would_go_and_creates_nothing(client, tmp_path, monkeypatch):
    from src import capability_registry as registry
    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "available", "docker 99.0 (stubbed)", stamp))

    root = tmp_path / "runs"
    body = client.post("/api/contracts/skill/plan", json={
        "manifest": GOOD, "workspace": str(tmp_path), "run_id": "plan-1",
        "artifacts_root": str(root)}).json()

    assert body["ok"] is True
    decision = body["decision"]
    assert decision["ok"] is True and decision["backend"] == "docker_workspace"
    assert decision["spec"]["isolation"] == "container"
    assert decision["spec"]["network"] is False
    assert not root.exists(), "planning must not leave a scratch directory behind"


def test_planning_with_docker_down_refuses_and_does_not_offer_the_host(client, tmp_path):
    body = client.post("/api/contracts/skill/plan", json={
        "manifest": GOOD, "workspace": str(tmp_path), "run_id": "plan-2"}).json()
    decision = body["decision"]
    assert decision["ok"] is False
    assert decision["backend"] != "local"
    text = ws.render_plan(body)
    assert "WOULD NOT RUN" in text
    assert "daemon did not answer" in text


def test_the_plan_text_shows_the_spec_a_coordinator_should_read(client, tmp_path, monkeypatch):
    from src import capability_registry as registry
    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "available", "stubbed", stamp))
    body = client.post("/api/contracts/skill/plan",
                       json={"manifest": GOOD, "workspace": str(tmp_path)}).json()
    text = ws.render_plan(body)
    assert "would run on docker_workspace (container)" in text
    assert "network=False" in text and "secrets=none" in text
    assert "approval cards it will raise: none" in text


def test_the_plan_tool_is_offered_and_promises_no_side_effect():
    plan = next(t for t in ws.TOOLS if t.name == "contracts_plan_run")
    assert "Nothing runs" in plan.description
    assert "never falls back to the host" in plan.description
    assert plan.inputSchema["required"] == ["manifest"]
