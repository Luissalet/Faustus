"""Lote 80 -- the version-control panel backend (OBJ-4).

Real `git` throughout (skipped cleanly when it isn't on PATH). Every test
runs with HOME and the git global config pointed at an empty tmp dir and
GIT_CONFIG_NOSYSTEM=1, so nothing here can accidentally depend on -- or leak
into -- the machine's real `~/.gitconfig`; fixture commits carry their own
identity via GIT_AUTHOR_*/GIT_COMMITTER_* env plus `-c user.name=`/`-c
user.email=` on the commit itself, exactly as the brief asks, so a fixture
repo's *persistent* `user.name`/`user.email` is controlled separately (set
with `git config` when a test wants the repo "configured", left unset for the
"no identity" case) from whatever it took to make the commit exist.
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
from src import git_panel  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
OTHER = "mallory"


# ---------------------------------------------------------------------------
# git fixture helpers -- real git, never through git_panel (which must never
# see these as anything other than "a repository that already exists").
# ---------------------------------------------------------------------------
def _run(args, cwd, env=None, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=env or os.environ.copy())
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed (rc={proc.returncode}): {proc.stderr}")
    return proc


def _fixture_commit_env():
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "Fixture Author", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    })
    return env


def _init_repo(path, *, configure_identity=True):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    # Pin the initial branch name regardless of the host's init.defaultBranch.
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=path)
    if configure_identity:
        _run(["config", "user.name", "Repo User"], cwd=path)
        _run(["config", "user.email", "repo-user@example.com"], cwd=path)
    return path


def _write(path, filename, content):
    fp = path / filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return fp


def _commit(path, filename, content, message):
    _write(path, filename, content)
    _run(["add", "-A"], cwd=path)
    _run(["-c", "user.name=Fixture Author", "-c", "user.email=fixture@example.com",
          "commit", "-m", message], cwd=path, env=_fixture_commit_env())
    return _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def _bare(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "--bare"], cwd=path)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_git_identity(tmp_path_factory, monkeypatch):
    """No ambient identity, ever: an empty HOME/global config and
    GIT_CONFIG_NOSYSTEM=1, so a "no identity" repo really has none, and a
    "configured" repo's identity is exactly what the test set on it."""
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
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
    return store.create(name="GitPanelProj", folder="GitPanelProj", workspace=str(workspace),
                        owner=owner, scaffold_memory=False)


def _repo_id(path):
    return git_panel.compute_repo_id(str(path))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discovery_finds_nested_repo_and_skips_excluded_dirs(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "hello\n", "initial")

    nested = root / "vendor" / "lib"
    _init_repo(nested)
    _commit(nested, "lib.py", "print(1)\n", "vendor init")

    # A `.git` sitting inside an excluded directory must never surface.
    excluded = root / "node_modules" / "pkg"
    _init_repo(excluded)
    _commit(excluded, "index.js", "1\n", "should not be found")

    _project(store, root)

    resp = client.get("/api/git/repos")
    assert resp.status_code == 200
    body = resp.json()
    paths = {r["path"] for r in body["repos"]}
    assert str(root) in paths
    assert str(nested) in paths
    assert str(excluded) not in paths
    assert body["git_version"]

    nested_row = next(r for r in body["repos"] if r["path"] == str(nested))
    root_row = next(r for r in body["repos"] if r["path"] == str(root))
    assert nested_row["parent_repo_id"] == root_row["id"]
    assert root_row["parent_repo_id"] is None


def test_discovery_unknown_project_id_404s(client, store, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    _project(store, root)
    resp = client.get("/api/git/repos", params={"project_id": "nope"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def test_status_reports_staged_unstaged_untracked(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "one\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "one\ntwo\n")            # unstaged modification
    _run(["add", "a.txt"], cwd=root)                # ...now staged
    _write(root, "a.txt", "one\ntwo\nthree\n")      # further unstaged change on top
    _write(root, "new.txt", "brand new\n")           # untracked

    resp = client.get(f"/api/git/repos/{rid}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch"] == "master"
    assert body["detached"] is False
    assert [s["path"] for s in body["staged"]] == ["a.txt"]
    assert [s["path"] for s in body["unstaged"]] == ["a.txt"]
    assert [u["path"] for u in body["untracked"]] == ["new.txt"]
    assert body["conflicts"] == []

    repo_get = client.get(f"/api/git/repos/{rid}").json()
    assert repo_get["dirty"] == {"staged": 1, "unstaged": 1, "untracked": 1}
    assert repo_get["user"] == {"name": "Repo User", "email": "repo-user@example.com"}


# ---------------------------------------------------------------------------
# Log + cursor pagination
# ---------------------------------------------------------------------------
def test_log_paginates_with_cursor(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    shas = [_commit(root, "a.txt", f"v{i}\n", f"commit {i}") for i in range(5)]
    shas.reverse()  # newest first, matching log order
    _project(store, root)
    rid = _repo_id(root)

    page1 = client.get(f"/api/git/repos/{rid}/log", params={"limit": 2}).json()
    assert [c["sha"] for c in page1["commits"]] == shas[0:2]
    assert page1["next_cursor"] == shas[1]

    page2 = client.get(f"/api/git/repos/{rid}/log",
                       params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [c["sha"] for c in page2["commits"]] == shas[2:4]
    assert page2["next_cursor"] == shas[3]

    page3 = client.get(f"/api/git/repos/{rid}/log",
                       params={"limit": 2, "cursor": page2["next_cursor"]}).json()
    assert [c["sha"] for c in page3["commits"]] == shas[4:5]

    last = page1["commits"][0]
    assert last["short"] and last["message"] == "commit 4"
    assert last["date"].endswith("Z")
    assert last["refs"]  # HEAD -> master at minimum on the newest commit


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------
def test_branches_local_and_remote(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _run(["branch", "feature"], cwd=root)
    bare = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(bare)], cwd=root)
    _run(["push", "origin", "master"], cwd=root)
    _run(["fetch", "origin"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    body = client.get(f"/api/git/repos/{rid}/branches").json()
    assert body["current"] == "master"
    names = {b["name"] for b in body["local"]}
    assert {"master", "feature"} <= names
    current_row = next(b for b in body["local"] if b["name"] == "master")
    assert current_row["is_current"] is True
    other_row = next(b for b in body["local"] if b["name"] == "feature")
    assert other_row["is_current"] is False
    assert any(r["name"] == "origin/master" for r in body["remote"])
    assert all(not r["name"].endswith("/HEAD") for r in body["remote"])


# ---------------------------------------------------------------------------
# Commit detail + diff
# ---------------------------------------------------------------------------
def test_commit_detail_and_diff(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "line1\n", "initial")
    sha = _commit(root, "a.txt", "line1\nline2\n", "add line2")
    _project(store, root)
    rid = _repo_id(root)

    detail = client.get(f"/api/git/repos/{rid}/commits/{sha}").json()
    assert detail["sha"] == sha
    assert detail["message"] == "add line2"
    assert detail["files"] == [{"path": "a.txt", "status": "M", "additions": 1, "deletions": 0}]

    diff = client.get(f"/api/git/repos/{rid}/commits/{sha}/diff", params={"path": "a.txt"}).json()
    assert diff["path"] == "a.txt"
    assert "+line2" in diff["diff"]
    assert diff["truncated"] is False
    assert diff["binary"] is False

    unknown = client.get(f"/api/git/repos/{rid}/commits/{'0' * 40}")
    assert unknown.status_code == 404


def test_working_tree_diff_staged_and_unstaged(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "line1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "line1\nline2\n")
    unstaged = client.get(f"/api/git/repos/{rid}/diff", params={"path": "a.txt", "staged": 0}).json()
    assert "+line2" in unstaged["diff"]

    _run(["add", "a.txt"], cwd=root)
    staged = client.get(f"/api/git/repos/{rid}/diff", params={"path": "a.txt", "staged": 1}).json()
    assert "+line2" in staged["diff"]


def test_diff_path_outside_repo_is_404(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "x\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.get(f"/api/git/repos/{rid}/diff", params={"path": "../../etc/passwd"})
    assert resp.status_code == 404
    resp2 = client.get(f"/api/git/repos/{rid}/diff", params={"path": "/etc/passwd"})
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Checkout (clean + dirty), create branch
# ---------------------------------------------------------------------------
def test_checkout_clean_switches_branch(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "master\n", "initial")
    _run(["checkout", "-b", "dev"], cwd=root)
    _commit(root, "a.txt", "dev\n", "dev change")
    _run(["checkout", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/checkout", json={"branch": "dev"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["branch"] == "dev"
    assert body["repo"]["branch"] == "dev"


def test_checkout_dirty_refuses_with_409(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "master\n", "initial")
    _run(["checkout", "-b", "other"], cwd=root)
    _commit(root, "a.txt", "other-version\n", "other change")
    _run(["checkout", "master"], cwd=root)
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "uncommitted-dirty\n")  # uncommitted, conflicts with `other`'s version

    resp = client.post(f"/api/git/repos/{rid}/checkout", json={"branch": "other"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.dirty"
    assert "a.txt" in body["dirty"]
    # The checkout must not have happened.
    assert client.get(f"/api/git/repos/{rid}").json()["branch"] == "master"


def test_create_branch(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/branches", json={"name": "feature/x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["branch"] == "feature/x"
    assert body["repo"]["branch"] == "feature/x"

    branches = client.get(f"/api/git/repos/{rid}/branches").json()
    assert any(b["name"] == "feature/x" for b in branches["local"])


# ---------------------------------------------------------------------------
# Stage / unstage / discard
# ---------------------------------------------------------------------------
def test_stage_unstage_discard(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "orig\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "changed\n")
    stage_resp = client.post(f"/api/git/repos/{rid}/stage", json={"paths": ["a.txt"]})
    assert stage_resp.status_code == 200
    assert stage_resp.json()["repo"]["dirty"]["staged"] == 1

    unstage_resp = client.post(f"/api/git/repos/{rid}/unstage", json={"all": True})
    assert unstage_resp.status_code == 200
    assert unstage_resp.json()["repo"]["dirty"]["staged"] == 0
    assert unstage_resp.json()["repo"]["dirty"]["unstaged"] == 1

    discard_no_confirm = client.post(f"/api/git/repos/{rid}/discard", json={"paths": ["a.txt"]})
    assert discard_no_confirm.status_code == 400

    discard_resp = client.post(f"/api/git/repos/{rid}/discard",
                               json={"paths": ["a.txt"], "confirm": True})
    assert discard_resp.status_code == 200
    assert discard_resp.json()["repo"]["dirty"] == {"staged": 0, "unstaged": 0, "untracked": 0}
    assert (root / "a.txt").read_text(encoding="utf-8") == "orig\n"


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------
def test_commit_empty_message_400(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/commit", json={"message": "  "})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "git.nothing_to_commit"


def test_commit_nothing_staged_400(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/commit", json={"message": "nothing to see"})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "git.nothing_to_commit"


def test_commit_no_identity_409(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root, configure_identity=False)
    _commit(root, "a.txt", "1\n", "initial")  # per-call identity only, nothing persisted
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "2\n")
    _run(["add", "a.txt"], cwd=root)

    resp = client.post(f"/api/git/repos/{rid}/commit", json={"message": "second"})
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "git.no_identity"


def test_commit_succeeds_with_configured_identity(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    _write(root, "a.txt", "2\n")
    _run(["add", "a.txt"], cwd=root)

    resp = client.post(f"/api/git/repos/{rid}/commit", json={"message": "second commit"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["message"] == "second commit"
    assert body["sha"] == _run(["rev-parse", "HEAD"], cwd=root).stdout.strip()
    assert body["repo"]["dirty"] == {"staged": 0, "unstaged": 0, "untracked": 0}

    # The repo's own identity was used -- not something the server injected.
    author = _run(["log", "-1", "--format=%an <%ae>"], cwd=root).stdout.strip()
    assert author == "Repo User <repo-user@example.com>"


# ---------------------------------------------------------------------------
# fetch / pull / push / sync against a real bare remote
# ---------------------------------------------------------------------------
def _seed_bare_and_clone(tmp_path, name):
    bare = _bare(tmp_path / f"{name}.git")
    seed = tmp_path / f"{name}_seed"
    _init_repo(seed)
    _commit(seed, "a.txt", "1\n", "initial")
    _run(["remote", "add", "origin", str(bare)], cwd=seed)
    _run(["push", "-u", "origin", "master"], cwd=seed)
    return bare


def _clone(tmp_path, bare, name):
    dest = tmp_path / name
    _run(["clone", str(bare), str(dest)], cwd=tmp_path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=dest)
    return dest


def test_fetch_updates_remote_tracking_without_merging(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "fetchrepo")
    local = _clone(tmp_path, bare, "fetchrepo_local")
    other = _clone(tmp_path, bare, "fetchrepo_other")
    _commit(other, "a.txt", "2\n", "remote-side change")
    _run(["push", "origin", "master"], cwd=other)

    _project(store, local)
    rid = _repo_id(local)

    resp = client.post(f"/api/git/repos/{rid}/fetch", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["repo"]["behind"] == 1
    assert body["repo"]["ahead"] == 0
    # fetch must not have merged -- the working file is unchanged.
    assert (local / "a.txt").read_text(encoding="utf-8") == "1\n"


def test_pull_fast_forwards(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "pullrepo")
    local = _clone(tmp_path, bare, "pullrepo_local")
    other = _clone(tmp_path, bare, "pullrepo_other")
    _commit(other, "a.txt", "2\n", "remote-side change")
    _run(["push", "origin", "master"], cwd=other)

    _project(store, local)
    rid = _repo_id(local)

    resp = client.post(f"/api/git/repos/{rid}/pull", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["repo"]["behind"] == 0
    assert (local / "a.txt").read_text(encoding="utf-8") == "2\n"


def test_pull_diverged_409(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "divergerepo")
    local = _clone(tmp_path, bare, "divergerepo_local")
    other = _clone(tmp_path, bare, "divergerepo_other")
    _commit(other, "a.txt", "remote-side\n", "remote change")
    _run(["push", "origin", "master"], cwd=other)
    _commit(local, "a.txt", "local-side\n", "local change")  # local diverges too

    _project(store, local)
    rid = _repo_id(local)

    resp = client.post(f"/api/git/repos/{rid}/pull", json={})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.diverged"
    assert body["ahead"] == 1
    assert body["behind"] == 1


def test_push_sets_upstream_and_succeeds(client, store, tmp_path):
    bare = _bare(tmp_path / "pushrepo.git")
    local = tmp_path / "pushrepo_local"
    _init_repo(local)
    _commit(local, "a.txt", "1\n", "initial")
    _run(["remote", "add", "origin", str(bare)], cwd=local)

    _project(store, local)
    rid = _repo_id(local)

    resp = client.post(f"/api/git/repos/{rid}/push", json={"set_upstream": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["repo"]["upstream"] == "origin/master"
    assert body["repo"]["ahead"] == 0 and body["repo"]["behind"] == 0

    bare_sha = _run(["rev-parse", "master"], cwd=bare).stdout.strip()
    local_sha = _run(["rev-parse", "HEAD"], cwd=local).stdout.strip()
    assert bare_sha == local_sha


def test_push_force_is_rejected_before_running_git(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    resp = client.post(f"/api/git/repos/{rid}/push", json={"force": True})
    assert resp.status_code == 400


def test_push_rejected_non_fast_forward_409(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "rejectrepo")
    a = _clone(tmp_path, bare, "rejectrepo_a")
    b = _clone(tmp_path, bare, "rejectrepo_b")
    _commit(b, "a.txt", "from-b\n", "b's commit")
    _run(["push", "origin", "master"], cwd=b)  # bare is now ahead of `a`

    _commit(a, "a.txt", "from-a\n", "a's commit")  # `a` diverges without fetching

    _project(store, a)
    rid = _repo_id(a)

    resp = client.post(f"/api/git/repos/{rid}/push", json={})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.rejected"
    assert body["stderr"]


def test_sync_pulls_then_pushes(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "syncrepo")
    local = _clone(tmp_path, bare, "syncrepo_local")
    other = _clone(tmp_path, bare, "syncrepo_other")
    _commit(other, "a.txt", "remote-side\n", "remote change")
    _run(["push", "origin", "master"], cwd=other)

    _project(store, local)
    rid = _repo_id(local)

    resp = client.post(f"/api/git/repos/{rid}/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pull"]["ok"] is True
    assert body["push"]["ok"] is True
    assert body["repo"]["ahead"] == 0 and body["repo"]["behind"] == 0
    assert (local / "a.txt").read_text(encoding="utf-8") == "remote-side\n"


def test_sync_skips_push_when_pull_fails(client, store, tmp_path):
    bare = _seed_bare_and_clone(tmp_path, "syncdivrepo")
    local = _clone(tmp_path, bare, "syncdivrepo_local")
    other = _clone(tmp_path, bare, "syncdivrepo_other")
    _commit(other, "a.txt", "remote-side\n", "remote change")
    _run(["push", "origin", "master"], cwd=other)
    _commit(local, "a.txt", "local-side\n", "local change")

    _project(store, local)
    rid = _repo_id(local)

    before_sha = _run(["rev-parse", "HEAD"], cwd=local).stdout.strip()
    resp = client.post(f"/api/git/repos/{rid}/sync")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "git.diverged"
    assert body["push"] is None
    # No push attempted: local HEAD is unchanged.
    assert _run(["rev-parse", "HEAD"], cwd=local).stdout.strip() == before_sha


# ---------------------------------------------------------------------------
# Ownership and dependency-missing
# ---------------------------------------------------------------------------
def test_repo_owned_by_someone_else_is_404(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root, owner=OWNER)
    rid = _repo_id(root)

    mine = client.get(f"/api/git/repos/{rid}", headers={"x-test-owner": OWNER})
    assert mine.status_code == 200

    foreign = client.get(f"/api/git/repos/{rid}", headers={"x-test-owner": OTHER})
    assert foreign.status_code == 404


def test_git_missing_returns_503(client, store, tmp_path, monkeypatch):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)
    rid = _repo_id(root)

    monkeypatch.setattr(git_panel, "git_available", lambda: False)
    resp = client.get(f"/api/git/repos/{rid}")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_class"] == "dependency.missing"
    assert "detail" in body
