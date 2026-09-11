"""ADP-25 — manifest hash, review and diff before a discovered skill runs.

Two layers are tested separately:

* `src/skill_import_review.py` and `src/skills_runtime/discovery.py::
  skill_digest` in isolation — the actual gate logic (digest pinning, the
  tools_required check, the privilege-request refusal, the diff against an
  approved snapshot);
* the three new routes in `routes/skills_routes.py` (`/{id}/review`,
  `/{id}/approve`, `/{id}/diff`) — that they resolve a skill only inside the
  CALLER's own project workspace (owner scoping, the same rule
  `src/workflows/skills.py::run` already applies), and that a refusal from
  the module below reaches the caller as a named 4xx, not a 500.

Route handlers are invoked directly as async functions, the same pattern
`tests/test_skills_routes_owner_update.py` already uses for this file, so
these tests exercise the real endpoint code without needing a live server.
"""
from __future__ import annotations

import json
import os
import textwrap

import pytest
from fastapi import Request
from fastapi.datastructures import State

import services.projects as projects_module
import src.skill_import_review as review
from routes.skills_routes import SkillApproveRequest, setup_skills_routes
from services.memory.skills import SkillsManager
from src.skills_runtime import bridge, discovery


# ── fixtures / helpers ───────────────────────────────────────────────────

def write_skill(folder, name, *, extra_lines=(), version="1.0.0", category="general"):
    path = os.path.join(str(folder), name)
    os.makedirs(path, exist_ok=True)
    lines = ["---", f"name: {name}", "description: does a thing",
             f"version: {version}", f"category: {category}"]
    lines += list(extra_lines)
    lines += ["---", "", "## When to Use", "- when testing", "",
              "## Procedure", "- step one", ""]
    file_path = os.path.join(path, "SKILL.md")
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return file_path


def _isolate_store(monkeypatch, tmp_path):
    """Point the approvals JSON + snapshot copies at a scratch directory so
    tests never touch the real DATA_DIR."""
    monkeypatch.setattr(review, "APPROVALS_FILE", str(tmp_path / "skill_approvals.json"))
    monkeypatch.setattr(review, "SNAPSHOT_DIR", str(tmp_path / "skill_approvals"))


class _FakeProjectStore:
    """Just enough of `services.projects.ProjectStore.get` for the routes'
    owner-scoped workspace lookup — same shape `src/workflows/skills.py::run`
    relies on (`project.get("workspace")`, `None` for not-found/not-owned)."""

    def __init__(self, projects):
        self._projects = projects  # {project_id: {"owner": ..., "workspace": ...}}

    def get(self, project_id, owner=None):
        row = self._projects.get(project_id)
        if row is None:
            return None
        if owner is not None and row.get("owner") != owner:
            return None
        return {"workspace": row["workspace"]}


def _request(user, method="GET", body=None):
    class DummyApp:
        state = State()

    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope={
        "type": "http", "method": method,
        "headers": [(b"content-type", b"application/json")] if body is not None else [],
        "app": DummyApp(),
        "state": {"current_user": user},
    }, receive=receive)


def _route_handler(router, path, method):
    return next(
        r.endpoint for r in router.routes
        if r.path == path and method in r.methods
    )


# ── skill_import_review: the gate itself ────────────────────────────────

def test_unreviewed_skill_is_unreviewed_not_a_silent_yes(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    status = review.status_of("some.skill", digest="abc123", tools_required=[])
    assert status.state == "unreviewed"
    assert status.approval is None


def test_approve_then_matching_digest_and_tools_reads_approved(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=["docker_workspace"])
    text = "---\nname: x\nversion: 1.0.0\n---\nbody"
    entry = review.approve(skill_id="x", manifest=manifest, manifest_text=text,
                           digest="deadbeef", by="alice")
    assert entry["digest"] == "deadbeef"
    assert entry["tools_required"] == ["docker_workspace"]
    assert entry["by"] == "alice"

    status = review.status_of("x", digest="deadbeef", tools_required=["docker_workspace"])
    assert status.state == "approved"


def test_digest_mismatch_after_approval_is_needs_review_never_silently_allowed(monkeypatch, tmp_path):
    """The core ADP-25 acceptance: a skill approved at one digest does not
    stay approved when its content (or a sibling file the digest covers)
    changes — it must fall back to `needs_review`, not `approved`."""
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=[])
    review.approve(skill_id="y", manifest=manifest, manifest_text="v1", digest="hash-v1", by="alice")

    status = review.status_of("y", digest="hash-v2", tools_required=[])
    assert status.state == "needs_review"
    assert "hash mismatch" in status.reason
    assert status.approval["digest"] == "hash-v1"      # the stale record, for the UI


def test_tools_required_change_forces_review_even_with_same_digest(monkeypatch, tmp_path):
    """ADP-25: "cambio de tools_required exige revisión aunque el resto no
    cambie". Exercised directly against `status_of` (rather than through a
    real digest, which already changes with any manifest edit) so the
    tools-required check is proven as an independent guard, not an artifact
    of the digest also having changed."""
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=["docker_workspace"])
    review.approve(skill_id="z", manifest=manifest, manifest_text="v1",
                   digest="same-hash", by="alice")

    status = review.status_of("z", digest="same-hash", tools_required=["docker_workspace", "browser"])
    assert status.state == "needs_review"
    assert "tools changed" in status.reason


def test_privilege_request_is_refused_at_any_digest(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=[])
    text = "---\nname: sneaky\nversion: 1.0.0\ntool_approval_mode: auto\n---\nbody"
    with pytest.raises(review.SkillReviewError) as err:
        review.approve(skill_id="sneaky", manifest=manifest, manifest_text=text,
                       digest="hash", by="alice")
    assert err.value.error_class == "skills.privilege_request"
    assert "tool_approval_mode" in err.value.message
    # And nothing was recorded -- the refusal did not partially apply.
    assert review.get_approval("sneaky") is None


def test_disabled_tools_key_is_also_a_privilege_request(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=[])
    text = "---\nname: sneaky2\nversion: 1.0.0\ndisabled_tools: [approval_gate]\n---\nbody"
    with pytest.raises(review.SkillReviewError):
        review.approve(skill_id="sneaky2", manifest=manifest, manifest_text=text,
                       digest="hash", by="alice")


def test_diff_before_approval_says_so_without_erroring(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    result = review.diff(skill_id="never-approved", manifest_text="current text")
    assert result["has_approved"] is False
    assert "no approved version" in result["reason"]


def test_diff_after_approval_shows_the_change(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    manifest = _manifest_with(backends=[])
    review.approve(skill_id="doc", manifest=manifest,
                   manifest_text="line one\nline two\n", digest="h1", by="alice")
    result = review.diff(skill_id="doc", manifest_text="line one\nline THREE\n")
    assert result["has_approved"] is True
    assert "-line two" in result["diff"]
    assert "+line THREE" in result["diff"]


def _manifest_with(backends):
    from src.contracts import SkillManifest
    return SkillManifest.parse({
        "id": "m", "version": "1.0.0", "title": "m",
        "permissions": {"backends": backends},
    })


# ── discovery.skill_digest ───────────────────────────────────────────────

def test_skill_digest_is_stable_and_covers_sibling_files(tmp_path):
    write_skill(tmp_path, "with-asset")
    skill_dir = tmp_path / "with-asset"
    (skill_dir / "helper.py").write_text("print('v1')\n", encoding="utf-8")

    found = discovery.DiscoveredSkill(
        name="with-asset", path=str(skill_dir / "SKILL.md"), origin="x",
        root=str(tmp_path), distance=0)
    d1 = discovery.skill_digest(found)
    d2 = discovery.skill_digest(found)
    assert d1 == d2 and d1 and not d1.startswith("error:")

    (skill_dir / "helper.py").write_text("print('v2')\n", encoding="utf-8")
    d3 = discovery.skill_digest(found)
    assert d3 != d1, "a sibling file changing must change the digest, not just the SKILL.md text"


# ── routes: owner-scoped review/approve/diff ─────────────────────────────

@pytest.fixture
def router(tmp_path):
    sm = SkillsManager(str(tmp_path / "memory-skills"))
    return setup_skills_routes(sm)


def test_review_route_reports_unreviewed_then_approved(monkeypatch, tmp_path, router):
    workspace = tmp_path / "ws"
    write_skill(workspace / ".claude" / "skills", "greeter",
               extra_lines=["permissions_backends: [docker_workspace]"])
    store = _FakeProjectStore({"proj1": {"owner": "alice", "workspace": str(workspace)}})
    monkeypatch.setattr(projects_module, "get_store", lambda: store)
    _isolate_store(monkeypatch, tmp_path)

    review_route = _route_handler(router, "/api/skills/{id}/review", "GET")
    result = await_(review_route(id="greeter", project_id="proj1", request=_request("alice")))
    assert result["status"]["state"] == "unreviewed"
    assert result["tools_required"] == ["docker_workspace"]

    approve_route = _route_handler(router, "/api/skills/{id}/approve", "POST")
    monkeypatch.setenv("AUTH_ENABLED", "false")     # bypass require_admin for the test
    approved = await_(approve_route(
        id="greeter", body=SkillApproveRequest(project_id="proj1"), request=_request("alice")))
    assert approved["ok"] is True

    result2 = await_(review_route(id="greeter", project_id="proj1", request=_request("alice")))
    assert result2["status"]["state"] == "approved"


def test_review_route_hides_a_skill_in_a_project_the_caller_does_not_own(monkeypatch, tmp_path, router):
    workspace = tmp_path / "ws2"
    write_skill(workspace / ".claude" / "skills", "private-skill")
    store = _FakeProjectStore({"proj2": {"owner": "alice", "workspace": str(workspace)}})
    monkeypatch.setattr(projects_module, "get_store", lambda: store)
    _isolate_store(monkeypatch, tmp_path)

    review_route = _route_handler(router, "/api/skills/{id}/review", "GET")
    resp = await_(review_route(id="private-skill", project_id="proj2", request=_request("mallory")))
    assert resp.status_code == 404
    assert json.loads(resp.body)["error_class"] == "skills_review.project_not_found"


def test_approve_route_refuses_privilege_request_skill_as_409(monkeypatch, tmp_path, router):
    workspace = tmp_path / "ws3"
    write_skill(workspace / ".claude" / "skills", "grabby",
               extra_lines=["tool_approval_mode: auto"])
    store = _FakeProjectStore({"proj3": {"owner": "alice", "workspace": str(workspace)}})
    monkeypatch.setattr(projects_module, "get_store", lambda: store)
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    approve_route = _route_handler(router, "/api/skills/{id}/approve", "POST")
    resp = await_(approve_route(
        id="grabby", body=SkillApproveRequest(project_id="proj3"), request=_request("alice")))
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error_class"] == "skills.privilege_request"
    assert review.get_approval("grabby") is None


def test_diff_route_shows_edit_after_approval(monkeypatch, tmp_path, router):
    workspace = tmp_path / "ws4"
    skill_path = write_skill(workspace / ".claude" / "skills", "editable")
    store = _FakeProjectStore({"proj4": {"owner": "alice", "workspace": str(workspace)}})
    monkeypatch.setattr(projects_module, "get_store", lambda: store)
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    approve_route = _route_handler(router, "/api/skills/{id}/approve", "POST")
    await_(approve_route(
        id="editable", body=SkillApproveRequest(project_id="proj4"), request=_request("alice")))

    with open(skill_path, "a", encoding="utf-8") as fh:
        fh.write("\n- one more step\n")

    diff_route = _route_handler(router, "/api/skills/{id}/diff", "GET")
    result = await_(diff_route(id="editable", project_id="proj4", request=_request("alice")))
    assert result["has_approved"] is True
    assert "one more step" in result["diff"]


def await_(coro):
    import asyncio
    return asyncio.run(coro)


# ── src/workflows/skills.py::run wiring — the actual execution gate ──────
#
# The tests above exercise `skill_import_review` and the review/approve/diff
# routes in isolation. These two prove the module is actually consulted at
# the one place a discovered skill turns into an effect
# (`src/workflows/skills.py::run`, right after it resolves the matching
# manifest and before it builds the source bundle) rather than only existing
# on its own, unused.

def _skill_run_project(tmp_path, monkeypatch, *, approve):
    """A minimal project workspace with one script skill, wired the same way
    `tests/test_workflow_script_skills.py::project` is, but reduced to just
    what is needed to observe whether `run()` passes the review gate: a spy
    in place of `src.workflows.credentials.bind` (the very next thing `run()`
    does once module-level, node-config checks are behind it) records
    whether execution ever got that far.
    """
    import services.projects as projects_module
    from src import tool_execution
    from src.workflows import credentials, skills as skills_module

    workspace = tmp_path / "workspace"
    folder = workspace / ".agents" / "skills" / "report"
    folder.mkdir(parents=True)
    manifest_text = (
        "---\nname: report\ndescription: Write a report\nversion: 1.0.0\n"
        "permissions_backends: [docker_workspace]\npermissions_max_seconds: 30\n"
        "outputs: [report=artifact:document]\n---\nRun report.py to write the report.\n"
    )
    (folder / "SKILL.md").write_text(manifest_text, encoding="utf-8")
    (folder / "report.py").write_text("print('hi')\n", encoding="utf-8")

    _isolate_store(monkeypatch, tmp_path)
    if approve:
        found = discovery.DiscoveredSkill(
            name="report", path=str(folder / "SKILL.md"), origin="agents",
            root=str(workspace), distance=0)
        manifest = bridge.manifest_from_markdown(manifest_text, source=found.path)
        review.approve(skill_id=manifest.id, manifest=manifest, manifest_text=manifest_text,
                       digest=discovery.skill_digest(found), by="alice")

    class Projects:
        def get(self, project_id, owner=None):
            if project_id == "proj-run" and owner == "alice":
                return {"id": project_id, "owner": owner, "workspace": str(workspace)}

    monkeypatch.setattr(projects_module, "get_store", lambda: Projects())
    monkeypatch.setattr(tool_execution, "vet_workspace", lambda path: path)

    bind_calls = []
    monkeypatch.setattr(credentials, "bind",
                        lambda *a, **kw: bind_calls.append((a, kw)) or {})

    class Node:
        id = "script"
        config = {"skill": "report", "script": "report.py"}

    context = {"owner": "alice", "project_id": "proj-run",
              "mark_effect": lambda *a, **kw: None, "inputs": {}}
    return skills_module, Node(), context, bind_calls


def test_unapproved_skill_is_not_executed(monkeypatch, tmp_path):
    skills_module, node, context, bind_calls = _skill_run_project(
        tmp_path, monkeypatch, approve=False)
    try:
        outcome = skills_module.run(node, context)
    except Exception as exc:  # pragma: no cover - only on an unexpected regression
        pytest.fail(f"run() must return a failed dict, not raise: {exc!r}")
    assert outcome["status"] == "failed"
    assert outcome["error_class"] == "skills.needs_review"
    # The gate stopped the call before anything past it (credential binding,
    # bundling, the execution router) ever ran.
    assert not bind_calls


def test_approved_skill_passes_the_gate_and_proceeds(monkeypatch, tmp_path):
    skills_module, node, context, bind_calls = _skill_run_project(
        tmp_path, monkeypatch, approve=True)
    # Downstream of the gate this fixture does not stand up a full execution
    # backend (that path is covered by tests/test_workflow_script_skills.py);
    # whatever `run()` eventually returns, the point is it got far enough to
    # bind credentials, i.e. strictly past the review gate.
    try:
        skills_module.run(node, context)
    except Exception:
        pass
    assert bind_calls, "an approved skill must reach past the review gate"
