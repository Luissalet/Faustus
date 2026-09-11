"""Lote 82 -- create/clone repos and the repo <-> identity/policy routes
(OBJ-4). Real `git` throughout (skipped cleanly when it isn't on PATH).
"""
from __future__ import annotations

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
from src import constants as constants_mod  # noqa: E402
from src import git_identities, git_panel, settings as settings_mod  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
OTHER = "mallory"


def _run(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr}")
    return proc


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "--bare"], cwd=path)
    return path


@pytest.fixture(autouse=True)
def isolated_git_identity(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: expanduser ignores HOME
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", os.path.join(constants_mod.DATA_DIR, "settings.json"))
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
    return store.create(name="CreateRepoProj", folder="CreateRepoProj", workspace=str(workspace),
                        owner=owner, scaffold_memory=False)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


# ---------------------------------------------------------------------------
# GET /api/git/folders
# ---------------------------------------------------------------------------
def test_get_folders_lists_linked_folder_deduplicated(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    resp = client.get("/api/git/folders", headers=_hdr())
    assert resp.status_code == 200
    paths = [f["path"] for f in resp.json()["folders"]]
    assert str(root) in paths
    assert len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# POST /api/git/repos -- init
# ---------------------------------------------------------------------------
def test_create_repo_init_makes_initial_commit_on_default_branch(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    _run(["config", "--global", "user.name", "Global User"], cwd=tmp_path)
    _run(["config", "--global", "user.email", "global@example.com"], cwd=tmp_path)

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "newrepo",
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    repo = resp.json()["repo"]
    assert repo["branch"] == "main"
    assert repo["head_sha"]
    target = root / "newrepo"
    assert (target / "README.md").exists()
    log = _run(["log", "--format=%s"], cwd=target).stdout.strip()
    assert log == "Initial commit"


def test_create_repo_init_without_initial_commit_leaves_it_uncommitted(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)

    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "bare_new",
        "initial_commit": False, "default_branch": "trunk",
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    repo = resp.json()["repo"]
    assert repo["branch"] == "trunk"
    assert repo["head_sha"] is None
    assert not (root / "bare_new" / "README.md").exists()


def test_create_repo_rejects_invalid_name(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "../escape",
    }, headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "git.invalid_name"


def test_create_repo_conflicts_on_nonempty_target(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "taken").mkdir()
    (root / "taken" / "file.txt").write_text("x", encoding="utf-8")
    _project(store, root)
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "taken",
    }, headers=_hdr())
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "git.exists"


def test_create_repo_parent_folder_outside_linked_folders_is_403(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(outside), "name": "sneaky",
    }, headers=_hdr())
    assert resp.status_code == 403
    assert resp.json()["error_class"] == "git.folder_not_allowed"
    assert not (outside / "sneaky").exists()


def test_create_repo_in_a_subdirectory_of_a_linked_folder_is_allowed(client, store, tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    _project(store, root)
    _run(["config", "--global", "user.name", "Global User"], cwd=tmp_path)
    _run(["config", "--global", "user.email", "global@example.com"], cwd=tmp_path)
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root / "sub"), "name": "deep",
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    assert resp.json()["repo"]["path"] == str(root / "sub" / "deep")


def test_create_repo_init_no_identity_still_creates_repo_but_409s(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    # No global git identity anywhere (isolated_git_identity fixture) and no
    # identity_id given -- the initial commit cannot happen.
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "noident",
    }, headers=_hdr())
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "git.no_identity"
    assert (root / "noident" / ".git").exists()  # the repo itself was created


# ---------------------------------------------------------------------------
# POST /api/git/repos -- clone
# ---------------------------------------------------------------------------
def test_create_repo_clone_from_local_bare(client, store, tmp_path):
    bare = _bare(tmp_path / "seed.git")
    seed = tmp_path / "seed_wt"
    seed.mkdir()
    _run(["init"], cwd=seed)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=seed)
    _run(["config", "user.name", "Seeder"], cwd=seed)
    _run(["config", "user.email", "seed@example.com"], cwd=seed)
    (seed / "a.txt").write_text("1\n", encoding="utf-8")
    _run(["add", "-A"], cwd=seed)
    _run(["commit", "-m", "seed"], cwd=seed)
    _run(["remote", "add", "origin", str(bare)], cwd=seed)
    _run(["push", "origin", "master"], cwd=seed)

    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)

    resp = client.post("/api/git/repos", json={
        "mode": "clone", "parent_folder": str(root), "name": "cloned", "url": str(bare),
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    repo = resp.json()["repo"]
    assert (root / "cloned" / "a.txt").read_text(encoding="utf-8") == "1\n"
    assert repo["remotes"][0]["fetch_url"] == str(bare)


def test_create_repo_clone_requires_url(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    resp = client.post("/api/git/repos", json={
        "mode": "clone", "parent_folder": str(root), "name": "noturl",
    }, headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "git.invalid_name"


def test_create_repo_clone_failure_is_409_with_stderr(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    resp = client.post("/api/git/repos", json={
        "mode": "clone", "parent_folder": str(root), "name": "badclone",
        "url": str(tmp_path / "does-not-exist.git"),
    }, headers=_hdr())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.clone_failed"
    assert body["stderr"]


# ---------------------------------------------------------------------------
# Repo identity routes
# ---------------------------------------------------------------------------
def test_repo_identity_get_and_put(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    _run(["config", "--global", "user.name", "Global User"], cwd=tmp_path)
    _run(["config", "--global", "user.email", "global@example.com"], cwd=tmp_path)
    resp = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "identrepo",
    }, headers=_hdr())
    repo_id = resp.json()["repo"]["id"]
    repo_path = root / "identrepo"
    _run(["remote", "add", "origin", "https://github.com/someone/repo.git"], cwd=repo_path)

    before = client.get(f"/api/git/repos/{repo_id}/identity", headers=_hdr()).json()
    assert before["active"] is None
    assert before["remote_url"] == "https://github.com/someone/repo.git"

    create_resp = client.post("/api/git/identities", json={
        "label": "Alt", "ssh_host": "altalias", "identity_file": str(root),
    }, headers=_hdr())
    # identity_file must be an existing FILE, not a dir -- proves the 400 path.
    assert create_resp.status_code == 400
    assert create_resp.json()["error_class"] == "git.identity_file_missing"

    key_file = tmp_path / "id_alt"
    key_file.write_text("PRIVATE\n", encoding="utf-8")
    create_resp = client.post("/api/git/identities", json={
        "label": "Alt", "ssh_host": "altalias", "identity_file": str(key_file),
        "git_user_name": "Alt Bot", "git_user_email": "alt@example.com",
    }, headers=_hdr())
    assert create_resp.status_code == 201, create_resp.text
    identity_id = create_resp.json()["identity"]["id"]

    put_resp = client.put(f"/api/git/repos/{repo_id}/identity", json={
        "identity_id": identity_id,
    }, headers=_hdr())
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["remote_url_after"] == "git@altalias:someone/repo.git"
    assert body["repo"]["identity"]["id"] == identity_id

    after = client.get(f"/api/git/repos/{repo_id}/identity", headers=_hdr()).json()
    assert after["active"]["id"] == identity_id
    assert after["git_user"] == {"name": "Alt Bot", "email": "alt@example.com", "scope": "local"}


def test_identity_delete_refuses_ssh_config_sourced(client, store, tmp_path, monkeypatch):
    home = tmp_path / "home2"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_x").write_text("k\n", encoding="utf-8")
    (home / ".ssh" / "config").write_text(
        f"Host fromcfg\n    HostName github.com\n    IdentityFile {home / '.ssh' / 'id_x'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: expanduser ignores HOME
    cfg_id = git_identities.list_identities(OWNER)["identities"][0]["id"]
    resp = client.delete(f"/api/git/identities/{cfg_id}", headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "git.identity_not_manual"


# ---------------------------------------------------------------------------
# Global + per-repo policy routes
# ---------------------------------------------------------------------------
def test_global_policy_get_put_roundtrip(client):
    default = client.get("/api/git/policy", headers=_hdr()).json()["policy"]
    assert default["use_branch"] is False

    put = client.put("/api/git/policy", json={"use_branch": True, "commit": True}, headers=_hdr())
    assert put.status_code == 200
    assert put.json()["policy"]["use_branch"] is True
    assert put.json()["policy"]["commit"] is True
    assert put.json()["policy"]["push"] is False  # untouched field stays at its previous value

    again = client.get("/api/git/policy", headers=_hdr()).json()["policy"]
    assert again["use_branch"] is True


def test_repo_policy_override_and_inherit(client, store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _project(store, root)
    _run(["config", "--global", "user.name", "G"], cwd=tmp_path)
    _run(["config", "--global", "user.email", "g@example.com"], cwd=tmp_path)
    created = client.post("/api/git/repos", json={
        "mode": "init", "parent_folder": str(root), "name": "policyrepo",
    }, headers=_hdr())
    repo_id = created.json()["repo"]["id"]

    base = client.get(f"/api/git/repos/{repo_id}/policy", headers=_hdr()).json()["policy"]
    assert base["overridden"] is False

    override = client.put(f"/api/git/repos/{repo_id}/policy", json={"push": True}, headers=_hdr())
    assert override.status_code == 200
    pol = override.json()["policy"]
    assert pol["overridden"] is True
    assert pol["effective"]["push"] is True

    repo_after = client.get(f"/api/git/repos/{repo_id}", headers=_hdr()).json()
    assert repo_after["policy"]["overridden"] is True
    assert repo_after["policy"]["effective"]["push"] is True

    cleared = client.put(f"/api/git/repos/{repo_id}/policy", json={"inherit": True}, headers=_hdr())
    assert cleared.json()["policy"]["overridden"] is False
