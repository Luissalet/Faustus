"""The agent-profiles API (routes/agent_profiles_routes.py).

Four properties, and each of them is a hole somebody would otherwise find:

* **`POST /resolve` takes no identity from the body.** `owner` and `project_id`
  decide what a run may touch — work roots, project defaults, every restriction
  that hangs off them — so a preview that read them from JSON would be a
  privilege escalation wearing a diagnostic's clothes. They are ignored, and
  listed back in `ignored_fields` so the caller is not left wondering;

* **a refusal is a 200 that names the field.** An unknown slug answers
  `{"ok": false, "error": {"path", "message"}}`, the convention the contracts
  and context routes already keep; the 4xx codes stay reserved for a body that
  is not JSON;

* **the gate is admin.** A resolution says what a worker on this machine may
  do, and the repo lane reads files out of the linked folder;

* **nothing here runs anything.** A preview is a preview: same request twice,
  same answer, and `preview: true` on the payload so a caller cannot mistake it
  for a dispatch.

The router is mounted on a bare `FastAPI()` rather than on `app.py`: these
tests are about the routes, and standing up the whole application would make
them a test of the application's imports.
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import middleware                                       # noqa: E402
from routes.agent_profiles_routes import setup_agent_profiles_routes  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """Auth explicitly disabled — the single-user mode `require_admin` lets
    through — so these tests exercise the routes and not the login flow."""
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_agent_profiles_routes())
    return TestClient(app)


@pytest.fixture
def locked(monkeypatch):
    """The same app with auth ON and no auth manager configured: every route
    answers 403."""
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_agent_profiles_routes())
    return TestClient(app, raise_server_exceptions=False)


# ── the catalogue side ─────────────────────────────────────────────────────

def test_the_definitions_carry_the_fields_the_profiles_plan_added(client):
    body = client.get("/api/agent-profiles").json()
    assert body["ok"] is True
    assert body["agents"], "a fresh install still has its built-ins"
    row = body["agents"][0]
    for field in ("default_completion_mode", "capabilities", "specialties",
                  "verification_profile", "context_profile", "budget_profile",
                  "collaboration_profile", "output_contract"):
        assert field in row, field
    assert body["precedence"][0] == "system_policy"


def test_one_definition_comes_with_the_content_of_the_profiles_it_references(client):
    body = client.get("/api/agent-profiles/reviewer").json()
    assert body["ok"] is True
    assert body["agent"]["slug"] == "reviewer"
    assert body["agent"]["rules"], "the page shows resolved rules, never frontmatter"
    assert "verification" in body["profiles"]
    assert body["profiles"]["verification"]["checks"]


def test_an_unknown_slug_is_a_200_that_names_the_field(client):
    response = client.get("/api/agent-profiles/no-such-agent")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["path"] == "slug"
    assert "no-such-agent" in body["error"]["message"]


def test_the_catalogue_serves_all_five_families(client):
    body = client.get("/api/agent-profiles/catalog").json()
    assert body["ok"] is True
    assert set(body["kinds"]) == {"context", "verification", "budget", "collaboration", "output"}
    for kind in body["kinds"]:
        assert body["profiles"][kind], kind


def test_the_completion_modes_come_with_their_ladder(client):
    body = client.get("/api/agent-profiles/completion-modes").json()
    assert [m["mode"] for m in body["modes"]] == ["literal", "professional", "greedy", "maximalist"]
    # The three permission levels are absent from the MODE ladder on purpose.
    assert "system_policy" not in body["mode_precedence"]
    assert "system_policy" in body["precedence"]
    assert all("tool" not in field for m in body["modes"] for field in m)


def test_packs_answer_even_before_packs_exist(client):
    body = client.get("/api/agent-profiles/packs").json()
    assert body["ok"] is True
    assert isinstance(body["packs"], list)


# ── the preview ────────────────────────────────────────────────────────────

def test_resolve_previews_the_effective_configuration(client):
    body = client.post("/api/agent-profiles/resolve",
                       json={"agent": "reviewer",
                             "task": {"completion_mode": "literal", "max_rounds": 4}}).json()
    assert body["ok"] is True and body["preview"] is True
    resolution = body["resolution"]
    assert resolution["agent"]["slug"] == "reviewer"
    assert resolution["completion"]["mode"] == "literal"
    assert resolution["max_rounds"] == 4
    assert resolution["agent"]["definition_revision"].startswith("sha256:")
    assert body["identity"].startswith("sha256:")
    # The table the page prints: value, where it came from, what was discarded.
    fields = {row["field"] for row in body["rows"]}
    assert {"completion_mode", "tools", "deny", "max_rounds"} <= fields
    assert "completion_mode" in body["explain"]


def test_resolve_ignores_owner_and_project_id_from_the_body(client):
    """The rule this endpoint exists under. Both fields come from the session;
    a body that sends them changes nothing and is told so."""
    body = client.post("/api/agent-profiles/resolve",
                       json={"agent": "reviewer", "owner": "somebody-else",
                             "project_id": "another-project",
                             "task": {"completion_mode": "literal"},
                             "project_defaults": {"work_roots": ["C:/"],
                                                  "completion_mode": "maximalist"}}).json()
    assert body["ok"] is True
    scope = body["resolution"]["execution_scope"]
    assert scope["owner"] != "somebody-else"
    assert scope["project_id"] != "another-project"
    assert set(body["ignored_fields"]) >= {"owner", "project_id", "project_defaults.work_roots"}
    # The roots that were refused are not in the envelope either.
    assert "C:/" not in body["resolution"]["permissions"]["work_roots"]
    # What a caller MAY state still applied: a task override outranks both the
    # agent's own default and the project's. The project default is asserted to
    # LOSE here rather than to win, because `reviewer` now ships declaring
    # `professional`, and agent_default outranks project_default by design.
    assert body["resolution"]["completion"]["mode"] == "literal"
    assert body["resolution"]["completion"]["source"] == "task_override"


def test_resolve_is_pure_and_answers_the_same_thing_twice(client):
    payload = {"agent": "reviewer", "task": {"completion_mode": "professional"}}
    first = client.post("/api/agent-profiles/resolve", json=payload).json()
    second = client.post("/api/agent-profiles/resolve", json=payload).json()
    assert first["identity"] == second["identity"]
    # Only the occasion differs: the id and the timestamp, never the setup.
    assert first["resolution"]["resolution_id"] != second["resolution"]["resolution_id"]


def test_resolve_refuses_an_unknown_agent_by_name(client):
    response = client.post("/api/agent-profiles/resolve", json={"agent": "not-an-agent"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["path"] == "agent"


def test_resolve_reports_a_forbidden_override_instead_of_failing(client):
    body = client.post("/api/agent-profiles/resolve",
                       json={"agent": "reviewer",
                             "task": {"tools": ["bash"], "max_rounds": 2}}).json()
    assert body["ok"] is True
    assert body["resolution"]["max_rounds"] == 2
    assert any("bash" in c for c in body["resolution"]["caveats"])


def test_a_body_that_is_not_json_is_the_one_4xx(client):
    assert client.post("/api/agent-profiles/resolve", content=b"not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/agent-profiles/resolve", json=["a", "list"]).status_code == 400


def test_select_says_who_it_would_choose_and_why(client):
    body = client.post("/api/agent-profiles/select",
                       json={"task": {"mode": "reviewer"},
                             "instruction": "review this change"}).json()
    assert body["ok"] is True
    assert body["selection"]["chosen"]
    assert body["selection"]["reason"], "an agent chosen for no stated reason is unauditable"
    assert body["agent"]["definition_revision"].startswith("sha256:")


def test_select_declares_the_degradation_when_there_is_no_immune_system(client):
    body = client.post("/api/agent-profiles/select", json={"task": {}}).json()
    named = [d for d in body["degraded_integrations"] if d.startswith("capability_health")]
    assert named and "Immune System" in named[0]


# ── the gate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("post", "/api/agent-profiles/resolve"),
    ("post", "/api/agent-profiles/select"),
    ("get", "/api/agent-profiles"),
    ("get", "/api/agent-profiles/catalog"),
    ("get", "/api/agent-profiles/completion-modes"),
    ("get", "/api/agent-profiles/packs"),
    ("get", "/api/agent-profiles/reviewer"),
])
def test_every_route_is_admin_only(locked, method, path):
    response = (locked.post(path, json={}) if method == "post" else locked.get(path))
    assert response.status_code == 403, path
