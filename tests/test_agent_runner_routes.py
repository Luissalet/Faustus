"""The agent-runners API (routes/agent_runner_routes.py).

Three things it must get right:

  * the catalogue is a READ, admin-only, and answers even when this machine has
    no Ollama at all (the built-in table, everything marked not installed);
  * `POST /{key}/launch` INSTALLS software, so it is blocked from `app_api` —
    the loopback that carries the internal-tool token `require_admin` accepts
    with no cookie and no approval card — while the GETs stay open there;
  * nothing in the read path ever runs `ollama launch`.
"""
from __future__ import annotations

import json

import pytest

from src import agent_runners as reg
from tests.test_agent_runners import HELP, fake_which


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import agent_runner_routes

    monkeypatch.setattr(reg, "help_text", lambda **kw: HELP)
    monkeypatch.setattr(reg.shutil, "which", fake_which)
    reg.reset_cache()
    app = FastAPI()
    app.include_router(agent_runner_routes.setup_agent_runner_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_the_catalogue_lists_every_agent_with_the_two_facts_and_the_guard_note(client):
    body = client.get("/api/agent-runners").json()
    assert body["status"] == "success"
    rows = {r["key"]: r for r in body["runners"]}
    assert len(rows) == 18
    assert rows["claude"]["installed"] is True and rows["claude"]["runnable_as_worker"] is True
    assert rows["vscode"]["installed"] is True and rows["vscode"]["runnable_as_worker"] is False
    assert rows["opencode"]["installed"] is False
    assert {r["licence"] for r in body["runners"]} <= set(reg.LICENCES)
    assert body["enabled"] is False              # the feature ships off
    assert "command guard does not see" in body["guard_note"]
    assert body["installed_count"] == 2 and body["runnable_count"] == 1


def test_one_runner_carries_its_launch_commands_and_its_notes(client):
    body = client.get("/api/agent-runners/claude").json()
    assert body["runner"]["key"] == "claude"
    assert body["runner"]["launch_command"] == "ollama launch claude -y"
    assert body["runner"]["launch_config_command"] == "ollama launch claude --config"
    assert body["runner"]["licence"] == "subscription"
    assert body["runner"]["notes"]
    # an alias resolves, and a name nobody knows is a 404
    assert client.get("/api/agent-runners/moltbot").json()["runner"]["key"] == "openclaw"
    assert client.get("/api/agent-runners/ghost").status_code == 404


def test_the_read_path_never_runs_a_launch(client, monkeypatch):
    import subprocess

    def boom(*a, **k):   # pragma: no cover - the assertion is that it is not called
        raise AssertionError("reading the catalogue must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert client.get("/api/agent-runners").status_code == 200
    assert client.get("/api/agent-runners/claude").status_code == 200


def test_launching_needs_ollama_and_says_so(client, monkeypatch):
    monkeypatch.setattr(reg.shutil, "which", lambda name: fake_which(name))
    from routes import agent_runner_routes
    monkeypatch.setattr(agent_runner_routes.shutil, "which", lambda name: None)
    res = client.post("/api/agent-runners/claude/launch", json={})
    assert res.status_code == 400 and "ollama is not installed" in res.json()["detail"]


def test_launching_an_unknown_runner_is_a_404(client):
    assert client.post("/api/agent-runners/ghost/launch", json={}).status_code == 404


# ── the app_api gate ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_app_api_refuses_to_launch_an_agent_installer(monkeypatch):
    """`ollama launch` installs software on the operator's machine. `app_api`
    loops back with the internal-tool token, which `require_admin` accepts
    with no cookie and no approval card — so it must be refused BEFORE the
    loopback, like the package install and the engine rebuild."""
    import httpx
    from src.tool_implementations import do_app_api

    class Unexpected:
        def __init__(self, *a, **k):
            raise AssertionError("app_api must block the launch before the loopback")

    monkeypatch.setattr(httpx, "AsyncClient", Unexpected)
    result = await do_app_api(json.dumps({"action": "call", "method": "POST",
                                          "path": "/api/agent-runners/claude/launch",
                                          "body": {}}), owner="admin")
    assert result["exit_code"] == 1
    assert "blocked" in result["error"].lower() and "INSTALLS" in result["error"]
    # the refusal names the surface the human owns, or the model just retries
    assert "Agent runners" in result["error"]


def test_the_blocklist_entry_exists_and_only_covers_the_write():
    from src.tools.system import _APP_API_BLOCKLIST_METHOD_PATH
    entries = [row for row in _APP_API_BLOCKLIST_METHOD_PATH if row[1].startswith("/api/agent-runners")]
    assert entries == [("POST", "/api/agent-runners")]
    # reading what is installed is exactly what the model should be able to do
    assert all(m != "GET" for m, _ in entries)
