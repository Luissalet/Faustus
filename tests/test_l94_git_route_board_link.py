"""Lote 94 -- wiring for the board<->git hook that lote 92 built but never
plugged into the two real call sites a human actually uses:

* `POST /api/git/repos/{id}/commit` (`routes.git_routes.post_commit`) never
  passed `project_id` to `git_panel.commit`, so a commit made from the
  Source control panel could never close a board issue -- only a synthetic
  `git_panel.commit(..., project_id=...)` call in a lote 92 unit test did.
* `GitCommitTool` (`git_commit`, the agent's own tool) never passed
  `project_id` either, for the same reason.

This test covers the route: a real repo linked to a real project, a commit
through `POST .../commit` whose message says "fixes FAU-1", and the board
issue closing as a result -- proof the route is now wired all the way
through to `src.project_board.link_commit`, not just `git_panel.commit`
itself (already covered end-to-end by `tests/test_l92_board_git_link.py`).

Fixture pattern copied from `tests/test_l89_git_merge.py` (routes half) plus
`tests/test_l92_board_git_link.py` (board half).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import git_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod, git_panel, project_board  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.agent_tools.git_tools import GitCommitTool  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


def _run(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr}")
    return proc


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=path)
    _run(["config", "user.name", "Repo User"], cwd=path)
    _run(["config", "user.email", "repo-user@example.com"], cwd=path)
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(data_dir / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(te, "owner_is_admin_or_single_user", lambda owner: True)
    yield


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or None
    monkeypatch.setattr(git_routes, "effective_user", _owner_from_header)
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    return TestClient(app)


@pytest.fixture()
def board():
    return project_board.Store()  # default_path() resolves under the isolated DATA_DIR


def _headers():
    return {"x-test-owner": OWNER}


def test_commit_route_fixes_message_closes_the_board_issue(client, store, board, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    project = store.create(name="GitBoardProj", folder="GitBoardProj", workspace=str(root),
                            owner=OWNER, scaffold_memory=False)
    issue = board.create_issue(project["id"], "FAU", type="bug", title="retry leak")
    assert issue["id"] == "FAU-1"

    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _run(["add", "-A"], cwd=root)
    rid = git_panel.compute_repo_id(str(root))

    resp = client.post(f"/api/git/repos/{rid}/commit",
                        json={"message": f"fixes {issue['id']}"}, headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    full = board.get(issue["id"])
    assert full["status"] == "done"
    assert full["refs"][0]["value"] == resp.json()["sha"]


def test_git_commit_tool_fixes_message_closes_the_board_issue(board, tmp_path):
    """The agent's own `git_commit` tool: `ctx["project_id"]` (already how
    `git_tools._resolve_repo_root` picks the repo -- see
    `git_tools._repo_root`) must reach `git_panel.commit` too."""
    project_id = "proj-git-commit-tool"
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _run(["add", "-A"], cwd=root)
    _run(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
          "commit", "-m", "Initial commit"], cwd=root)
    issue = board.create_issue(project_id, "FAU", type="bug", title="retry leak")

    (root / "b.txt").write_text("two\n", encoding="utf-8")
    from src import agent_git_policy
    agent_git_policy.set_global_policy({"commit": True})

    ws_token = te._active_workspace.set(str(root))
    roots_token = te._active_workspace_roots.set((str(root),))
    try:
        result = asyncio.run(GitCommitTool().execute(
            json.dumps({"message": f"fixes {issue['id']}", "paths": ["b.txt"],
                        "user_confirmed": True}),
            {"owner": OWNER, "project_id": project_id},
        ))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)

    assert result["exit_code"] == 0, result
    full = board.get(issue["id"])
    assert full["status"] == "done"
    assert full["refs"][0]["value"] == result["sha"]
