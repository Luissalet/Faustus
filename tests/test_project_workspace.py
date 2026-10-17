"""Real stores: preservation, project scope, import safety and activation pins."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import projects
from src import project_board, project_workspace as pw, skill_import_review as review
from src.agent_tools.project_workspace_tools import ProjectWorkspaceTool
from routes import project_workspace_routes as routes

MD = '---\nname: check-results\ndescription: Check the output against acceptance criteria\nversion: "1.0.0"\n---\n\n## Procedure\n\n1. Read the result and compare it to the criteria.\n'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    store = projects.ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects, "_store", store)
    monkeypatch.setattr(projects, "get_store", lambda: store)
    monkeypatch.setattr(routes, "get_store", lambda: store)
    monkeypatch.setattr(review, "APPROVALS_FILE", str(tmp_path / "approvals.json"))
    monkeypatch.setattr(review, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    project = store.create(name="Test project", folder="Test project", workspace=str(root), owner="alice", scaffold_memory=False)
    return project, store, root


def test_install_never_activates_and_edit_invalidates_review(setup):
    project, _, root = setup
    result = pw.install_files(project, {"skill/SKILL.md": MD, "skill/references/check.txt": "keep exactly"})
    assert result["active"] is False
    assert (root / ".faustus/skills/check-results/references/check.txt").read_text() == "keep exactly"
    key = result["key"]
    with pytest.raises(projects.ProjectError, match="Review"):
        pw.activate(project, key)
    digest = pw.read_skill(project, key)["digest"]
    pw.activate(project, key, reviewed=True, expected_digest=digest, actor="alice")
    assert pw.list_skills(project)[0]["active"] is True
    assert key in pw.prompt_index(project)
    (root / key).write_text(MD + "\nChanged", encoding="utf-8")
    assert pw.list_skills(project)[0]["changed"] is True
    assert pw.prompt_index(project) == ""
    with pytest.raises(projects.ProjectError, match="changed"):
        pw.activate(project, key, reviewed=True, expected_digest=digest)
    with pytest.raises(projects.ProjectError, match="before activation"):
        pw.activate(project, key)


@pytest.mark.parametrize("path", ["../secret.md", "/absolute.md", "C:/secret.md", "file.md:evil", "AUX.txt", "folder/../secret.md", "folder./secret.md"])
def test_bad_local_paths_leave_project_untouched(setup, path):
    project, _, root = setup
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    with pytest.raises(projects.ProjectError):
        pw.install_files(project, {"SKILL.md": MD, path: "bad"})
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


def test_duplicate_and_multiskill_import_preserve_existing(setup):
    project, _, root = setup
    key = pw.install_files(project, {"SKILL.md": MD})["key"]
    with pytest.raises(projects.ProjectError, match="already exists"):
        pw.install_files(project, {"SKILL.md": MD.replace("Read the", "Do not read the")})
    assert (root / key).read_text(encoding="utf-8") == MD
    with pytest.raises(projects.ProjectError, match="exactly one"):
        pw.install_files(project, {"a/SKILL.md": MD, "b/SKILL.md": MD})


def test_chat_writes_real_instructions_memory_and_scopes_owner(setup):
    project, store, _ = setup
    tool = ProjectWorkspaceTool()
    ctx = {"project_id": project["id"], "owner": "alice"}
    def call(**args):
        return asyncio.run(tool.execute(json.dumps(args), ctx))
    assert call(action="instructions_write", content="Use Spanish.")["exit_code"] == 0
    assert store.get(project["id"], "alice")["instructions"] == "Use Spanish."
    assert call(action="memory_write", name="decisions.md", content="Keep stable IDs.")["exit_code"] == 0
    assert call(action="memory_read", name="decisions.md")["content"] == "Keep stable IDs."
    foreign = asyncio.run(tool.execute('{"action":"instructions_write","content":"bad"}', {"project_id": project["id"], "owner": "bob"}))
    assert foreign["exit_code"] == 1
    assert store.get(project["id"], "alice")["instructions"] == "Use Spanish."
    assert call(action="memory_write", name="../secret", content="bad")["exit_code"] == 1


def test_chat_cannot_self_approve(setup):
    project, _, _ = setup
    key = pw.install_files(project, {"SKILL.md": MD})["key"]
    args = {"action": "skill_activate", "key": key, "reviewed": True}
    result = asyncio.run(ProjectWorkspaceTool().execute(json.dumps(args), {"project_id": project["id"], "owner": "alice"}))
    assert result["exit_code"] == 1
    assert pw.list_skills(project)[0]["active"] is False


def test_activation_is_project_scoped(setup):
    project, store, root = setup
    key = pw.install_files(project, {"SKILL.md": MD})["key"]
    pw.activate(project, key, reviewed=True)
    # A separately bound project cannot reuse the other's approval by name.
    other = {**project, "id": "different-project"}
    assert pw.list_skills(other)[0]["active"] is False
    with pytest.raises(projects.ProjectError):
        pw.activate(other, key)
    pw.deactivate(project, key)
    assert (root / key).is_file()
    assert pw.list_skills(project)[0]["active"] is False


def test_privilege_request_cannot_activate(setup):
    project, _, _ = setup
    key = pw.install_files(project, {"SKILL.md": MD.replace('version:', 'skip_approval: true\nversion:')})["key"]
    with pytest.raises(review.SkillReviewError):
        pw.activate(project, key, reviewed=True, override=True)
    assert not pw.list_skills(project)[0]["active"]


def test_ui_owner_and_stale_digest(setup, monkeypatch):
    project, _, root = setup
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(routes, "effective_user", lambda request: request.headers.get("x-test-owner"))
    app = FastAPI()
    app.include_router(routes.setup_project_workspace_routes())
    client = TestClient(app)
    base = f"/api/projects/{project['id']}/workspace"
    assert client.post(base + "/action", json={"action": "instructions_read"}, headers={"x-test-owner": "bob"}).status_code == 404
    key = pw.install_files(project, {"SKILL.md": MD})["key"]
    digest = pw.read_skill(project, key)["digest"]
    (root / key).write_text(MD + "changed", encoding="utf-8")
    assert client.post(base + "/skills/review-activate", json={"key": key, "digest": digest}, headers={"x-test-owner": "alice"}).status_code == 409


def test_board_migration_preserves_old_rows_and_history(tmp_path):
    path = tmp_path / "board.db"
    store = project_board.Store(path)
    item = store.create_issue("p", "QA", type="task", title="Preserve me", labels=["old label"])
    store.add_comment(item["id"], "old comment", author="alice")
    before = store.get(item["id"])
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE issues DROP COLUMN acceptance_json")
        db.execute("PRAGMA user_version=1")
    assert store.get(item["id"])["acceptance"] == []
    updated = store.update_issue(item["id"], {"acceptance": ["passes reload"], "labels": ["new tag"], "assignee": "agent"})
    assert updated["id"] == before["id"]
    assert updated["comments"] == before["comments"]
    assert updated["body_md"] == before["body_md"]
    assert updated["acceptance"] == ["passes reload"]
    assert updated["labels"] == ["new tag"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
