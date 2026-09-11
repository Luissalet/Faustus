"""Lote 89 -- merge and branch deletion, panel + agent (OBJ-4).

Real `git` throughout (skipped cleanly when it isn't on PATH), real repos in
tmp_path. Two halves, isolation copied from the lots this one extends:

* Routes (`POST .../merge`, `POST .../merge/abort`, `DELETE .../branches/{name}`)
  -- fixture pattern from `tests/test_l80_git_panel.py` (a `TestClient` over
  `routes/git_routes.py`, an isolated `HOME`/git-config so nothing here can
  touch the real `~/.gitconfig`).
* Agent tools (`git_merge`, `git_delete_branch`) -- fixture pattern from
  `tests/test_l87_git_tools.py` (an isolated `DATA_DIR`/settings so the
  agent git policy reads/writes never touch the real ones, plus the active-
  workspace contextvars `execute_tool_block` would normally set).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections import namedtuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import git_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from src import agent_git_policy, constants as constants_mod, git_panel  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.agent_tools.git_tools import GitDeleteBranchTool, GitMergeTool  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


# ---------------------------------------------------------------------------
# git fixture helpers -- real git, never through git_panel.
# ---------------------------------------------------------------------------
def _run(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr}")
    return proc


def _init_repo(path, *, configure_identity=True):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=path)
    if configure_identity:
        _run(["config", "user.name", "Repo User"], cwd=path)
        _run(["config", "user.email", "repo-user@example.com"], cwd=path)
    return path


def _write(path, name, content):
    fp = path / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return fp


def _commit(path, name, content, message):
    _write(path, name, content)
    _run(["add", "-A"], cwd=path)
    _run(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
          "commit", "-m", message], cwd=path)
    return _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "--bare"], cwd=path)
    return path


@pytest.fixture(autouse=True)
def isolated_git_identity(tmp_path_factory, monkeypatch):
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


def _project(store, workspace, *, owner=OWNER):
    return store.create(name="GitMergeProj", folder="GitMergeProj", workspace=str(workspace),
                        owner=owner, scaffold_memory=False)


def _repo_id(path):
    return git_panel.compute_repo_id(str(path))


def _headers():
    return {"x-test-owner": OWNER}


def _run_async(coro):
    return asyncio.run(coro)


def _set_policy(**patch):
    return agent_git_policy.set_global_policy(patch)


# ---------------------------------------------------------------------------
# Routes: merge -- fast-forward
# ---------------------------------------------------------------------------
def test_merge_fast_forward(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["checkout", "-b", "feature"], cwd=root)
    feature_sha = _commit(root, "a.txt", "2\n", "feature change")
    _run(["checkout", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/merge", json={"branch": "feature"}, headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["fast_forward"] is True
    assert body["sha"] == feature_sha
    assert body["conflicts"] == []
    assert body["repo"]["branch"] == "master"
    assert client.get(f"/api/git/repos/{rid}", headers=_headers()).json()["head_sha"] == feature_sha


# ---------------------------------------------------------------------------
# Routes: merge -- ff="no" forces a merge commit even when ff would work
# ---------------------------------------------------------------------------
def test_merge_no_ff_creates_merge_commit_even_when_ff_possible(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["checkout", "-b", "feature"], cwd=root)
    _commit(root, "a.txt", "2\n", "feature change")
    _run(["checkout", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/merge",
                       json={"branch": "feature", "ff": "no", "message": "merge feature"},
                       headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["fast_forward"] is False
    parents = _run(["show", "-s", "--format=%P", body["sha"]], cwd=root).stdout.split()
    assert len(parents) == 2
    assert (root / "a.txt").read_text(encoding="utf-8") == "2\n"


# ---------------------------------------------------------------------------
# Routes: merge -- conflict, default (aborts automatically)
# ---------------------------------------------------------------------------
def _diverge_into_conflict(root):
    _init_repo(root)
    _commit(root, "a.txt", "base\n", "initial")
    _run(["checkout", "-b", "other"], cwd=root)
    _commit(root, "a.txt", "other change\n", "other change")
    _run(["checkout", "master"], cwd=root)
    _commit(root, "a.txt", "master change\n", "master change")


def test_merge_conflict_aborts_automatically_by_default(client, store, tmp_path):
    root = tmp_path / "work"
    _diverge_into_conflict(root)
    master_sha_before = _run(["rev-parse", "HEAD"], cwd=root).stdout.strip()
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/merge", json={"branch": "other"}, headers=_headers())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.merge_conflict"
    assert body["aborted"] is True
    assert body["conflicts"] == ["a.txt"]

    # The merge must have been rolled all the way back: no MERGE_HEAD, no
    # pending conflicts, HEAD exactly where it was, working tree clean.
    assert not (root / ".git" / "MERGE_HEAD").exists()
    status = client.get(f"/api/git/repos/{rid}/status", headers=_headers()).json()
    assert status["conflicts"] == [] and status["staged"] == [] and status["unstaged"] == []
    assert _run(["rev-parse", "HEAD"], cwd=root).stdout.strip() == master_sha_before


# ---------------------------------------------------------------------------
# Routes: merge -- conflict, kept for manual resolution + explicit abort
# ---------------------------------------------------------------------------
def test_merge_conflict_kept_then_explicit_abort(client, store, tmp_path):
    root = tmp_path / "work"
    _diverge_into_conflict(root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/merge",
                       json={"branch": "other", "keep_conflicts": True}, headers=_headers())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.merge_conflict"
    assert body["aborted"] is False
    assert body["conflicts"] == ["a.txt"]

    # Left exactly as git left it: MERGE_HEAD present, status exposes the
    # same conflict.
    assert (root / ".git" / "MERGE_HEAD").exists()
    status = client.get(f"/api/git/repos/{rid}/status", headers=_headers()).json()
    assert [c["path"] for c in status["conflicts"]] == ["a.txt"]

    abort_resp = client.post(f"/api/git/repos/{rid}/merge/abort", headers=_headers())
    assert abort_resp.status_code == 200, abort_resp.text
    assert abort_resp.json()["ok"] is True
    assert not (root / ".git" / "MERGE_HEAD").exists()
    status_after = client.get(f"/api/git/repos/{rid}/status", headers=_headers()).json()
    assert status_after["conflicts"] == []


# ---------------------------------------------------------------------------
# Routes: merge -- dirty (uncommitted changes the merge would overwrite)
# ---------------------------------------------------------------------------
def test_merge_dirty_refuses_with_409(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "base\n", "initial")
    _run(["checkout", "-b", "other"], cwd=root)
    _commit(root, "a.txt", "other version\n", "other change")
    _run(["checkout", "master"], cwd=root)
    _write(root, "a.txt", "uncommitted local edit\n")  # never staged/committed
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/merge", json={"branch": "other"}, headers=_headers())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.dirty"
    assert "a.txt" in body["dirty"]
    # Nothing merged, nothing aborted-and-rewritten: the file still holds
    # exactly the uncommitted edit.
    assert (root / "a.txt").read_text(encoding="utf-8") == "uncommitted local edit\n"


# ---------------------------------------------------------------------------
# Routes: delete branch -- merged / unmerged / current / remote
# ---------------------------------------------------------------------------
def test_delete_branch_merged_succeeds_without_force(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["checkout", "-b", "topic"], cwd=root)
    _commit(root, "a.txt", "2\n", "topic change")
    _run(["checkout", "master"], cwd=root)
    _run(["merge", "--ff-only", "topic"], cwd=root)  # master now contains topic's commit
    _project(store, root)
    rid = _repo_id(root)

    resp = client.delete(f"/api/git/repos/{rid}/branches/topic", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["deleted"] == "topic"
    branches = client.get(f"/api/git/repos/{rid}/branches", headers=_headers()).json()
    assert all(b["name"] != "topic" for b in branches["local"])


def test_delete_branch_unmerged_needs_force(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["checkout", "-b", "topic"], cwd=root)
    _commit(root, "a.txt", "2\n", "topic change, never merged")
    _run(["checkout", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    denied = client.delete(f"/api/git/repos/{rid}/branches/topic", headers=_headers())
    assert denied.status_code == 409
    assert denied.json()["error_class"] == "git.branch_unmerged"
    # Refusing must not have deleted it.
    branches = client.get(f"/api/git/repos/{rid}/branches", headers=_headers()).json()
    assert any(b["name"] == "topic" for b in branches["local"])

    forced = client.delete(f"/api/git/repos/{rid}/branches/topic", params={"force": 1}, headers=_headers())
    assert forced.status_code == 200, forced.text
    branches_after = client.get(f"/api/git/repos/{rid}/branches", headers=_headers()).json()
    assert all(b["name"] != "topic" for b in branches_after["local"])


def test_delete_branch_with_slash_in_name(client, store, tmp_path):
    # `:path` route param -- a branch like "feature/x" must round-trip
    # through the URL (percent-encoded or literal) without being mistaken
    # for two path segments.
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["checkout", "-b", "feature/x"], cwd=root)
    _commit(root, "a.txt", "2\n", "feature change")
    _run(["checkout", "master"], cwd=root)
    _run(["merge", "--ff-only", "feature/x"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.delete(f"/api/git/repos/{rid}/branches/feature%2Fx", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == "feature/x"
    branches = client.get(f"/api/git/repos/{rid}/branches", headers=_headers()).json()
    assert all(b["name"] != "feature/x" for b in branches["local"])


def test_delete_current_branch_refused(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.delete(f"/api/git/repos/{rid}/branches/master", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "git.branch_is_current"


def test_delete_branch_with_remote_true_also_deletes_on_origin(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    bare = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(bare)], cwd=root)
    _run(["push", "origin", "master"], cwd=root)
    _run(["checkout", "-b", "topic"], cwd=root)
    _commit(root, "a.txt", "2\n", "topic change")
    _run(["push", "origin", "topic"], cwd=root)
    _run(["checkout", "master"], cwd=root)
    _run(["merge", "--ff-only", "topic"], cwd=root)
    _run(["push", "origin", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.delete(f"/api/git/repos/{rid}/branches/topic", params={"remote": 1}, headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["remote_deleted"] is True
    remote_refs = _run(["ls-remote", "--heads", str(bare)], cwd=root).stdout
    assert "refs/heads/topic" not in remote_refs


# ---------------------------------------------------------------------------
# Agent tools: git_merge / git_delete_branch -- workspace binding
# ---------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    _commit(r, "a.txt", "one\n", "Initial commit")
    return r


@pytest.fixture
def ws(repo):
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


def test_git_merge_refused_when_policy_denies(ws, repo):
    _run(["checkout", "-b", "feature"], cwd=repo)
    _commit(repo, "a.txt", "two\n", "feature change")
    _run(["checkout", "master"], cwd=repo)
    result = _run_async(GitMergeTool().execute(json.dumps({"branch": "feature"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["git_policy_field"] == "use_branch"


def test_git_merge_allowed_with_user_confirmed_fast_forwards(ws, repo):
    _run(["checkout", "-b", "feature"], cwd=repo)
    feature_sha = _commit(repo, "a.txt", "two\n", "feature change")
    _run(["checkout", "master"], cwd=repo)
    result = _run_async(GitMergeTool().execute(
        json.dumps({"branch": "feature", "user_confirmed": True}), {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert result["fast_forward"] is True
    assert result["sha"] == feature_sha


def test_git_merge_conflict_reports_and_leaves_repo_clean(ws, repo):
    _run(["checkout", "-b", "other"], cwd=repo)
    _commit(repo, "a.txt", "other version\n", "other change")
    _run(["checkout", "master"], cwd=repo)
    _commit(repo, "a.txt", "master version\n", "master change")
    result = _run_async(GitMergeTool().execute(
        json.dumps({"branch": "other", "user_confirmed": True}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.merge_conflict"
    assert result["conflicts"] == ["a.txt"]
    assert result["aborted"] is True
    # The tool never keeps a conflict pending for the model to sort out.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git_panel.repo_status(str(repo))["conflicts"] == []


def test_git_delete_branch_refused_when_policy_denies(ws, repo):
    _run(["branch", "topic"], cwd=repo)
    result = _run_async(GitDeleteBranchTool().execute(json.dumps({"name": "topic"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["git_policy_field"] == "use_branch"


def test_git_delete_branch_allowed_when_merged(ws, repo):
    _run(["checkout", "-b", "topic"], cwd=repo)
    _commit(repo, "a.txt", "two\n", "topic change")
    _run(["checkout", "master"], cwd=repo)
    _run(["merge", "--ff-only", "topic"], cwd=repo)
    result = _run_async(GitDeleteBranchTool().execute(
        json.dumps({"name": "topic", "user_confirmed": True}), {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert result["name"] == "topic"
    remaining = git_panel.list_branches(str(repo))["local"]
    assert all(b["name"] != "topic" for b in remaining)


def test_git_delete_branch_current_branch_error(ws, repo):
    result = _run_async(GitDeleteBranchTool().execute(
        json.dumps({"name": "master", "user_confirmed": True}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.branch_is_current"


def test_git_delete_branch_unmerged_needs_force(ws, repo):
    _run(["checkout", "-b", "topic"], cwd=repo)
    _commit(repo, "a.txt", "two\n", "topic change, never merged")
    _run(["checkout", "master"], cwd=repo)
    denied = _run_async(GitDeleteBranchTool().execute(
        json.dumps({"name": "topic", "user_confirmed": True}), {"owner": OWNER}))
    assert denied["exit_code"] == 1
    assert denied["error_class"] == "git.branch_unmerged"

    forced = _run_async(GitDeleteBranchTool().execute(
        json.dumps({"name": "topic", "force": True, "user_confirmed": True}), {"owner": OWNER}))
    assert forced["exit_code"] == 0
