"""Lote 84 -- backend: listing performance (fewer `git` calls, parallel
summaries, `?light=1`), discovery caching/invalidation, and repo dedupe.

Real `git` throughout (skipped cleanly when it isn't on PATH).
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


# ---------------------------------------------------------------------------
# git fixture helpers -- real git, never through git_panel.
# ---------------------------------------------------------------------------
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


def _commit(path, filename, content, message):
    fp = path / filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    _run(["add", "-A"], cwd=path)
    _run(["commit", "-m", message], cwd=path)
    return _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()


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
    git_panel.invalidate_discovery_cache()
    yield
    git_panel.invalidate_discovery_cache()


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


def _project(store, workspace, *, name="Proj", owner=OWNER):
    return store.create(name=name, folder=name, workspace=str(workspace), owner=owner, scaffold_memory=False)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


def _counting_run_git(monkeypatch, calls):
    """Wraps `git_panel.run_git` to append every call's args to `calls`
    while still delegating to the real thing -- the "double" the contract
    asks for, minus faking git's actual output."""
    orig = git_panel.run_git

    def wrapper(repo_dir, *args, **kwargs):
        calls.append(args)
        return orig(repo_dir, *args, **kwargs)

    monkeypatch.setattr(git_panel, "run_git", wrapper)


# ---------------------------------------------------------------------------
# Performance: git call counts
# ---------------------------------------------------------------------------
def test_repo_summary_full_makes_at_most_4_git_calls(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "counted")
    _commit(repo, "a.txt", "1\n", "initial")

    calls = []
    _counting_run_git(monkeypatch, calls)

    row = git_panel.repo_summary(str(repo), project_id="p", project_name="P",
                                 root_folder=str(tmp_path), parent_repo_id=None)
    assert len(calls) <= 4, calls
    assert row["branch"] == "master"
    assert row["user"] == {"name": "Repo User", "email": "repo-user@example.com"}
    assert row["remotes"] == []


def test_repo_summary_light_makes_exactly_1_git_call(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "counted_light")
    _commit(repo, "a.txt", "1\n", "initial")

    calls = []
    _counting_run_git(monkeypatch, calls)

    row = git_panel.repo_summary(str(repo), project_id="p", project_name="P",
                                 root_folder=str(tmp_path), parent_repo_id=None, light=True)
    assert len(calls) == 1, calls
    assert row["branch"] == "master"
    assert "user" not in row and "remotes" not in row
    assert row["dirty"] == {"staged": 0, "unstaged": 0, "untracked": 0}


def test_light_omits_user_remotes_identity_policy_but_keeps_branch_status(client, store, tmp_path):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)

    resp = client.get("/api/git/repos", params={"light": 1}, headers=_hdr())
    assert resp.status_code == 200
    row = resp.json()["repos"][0]
    for key in ("user", "remotes", "identity", "policy"):
        assert key not in row, key
    for key in ("branch", "ahead", "behind", "dirty", "detached", "upstream"):
        assert key in row, key


def test_repo_summaries_batches_in_parallel_and_matches_serial(tmp_path):
    metas = []
    for i in range(5):
        repo = _init_repo(tmp_path / f"r{i}")
        _commit(repo, "a.txt", f"{i}\n", "initial")
        metas.append({
            "id": git_panel.compute_repo_id(str(repo)), "path": str(repo), "name": repo.name,
            "project_id": "p", "project_name": "P", "root_folder": str(tmp_path), "parent_repo_id": None,
        })

    batched = git_panel.repo_summaries(metas)
    serial = [git_panel.repo_summary(m["path"], project_id=m["project_id"], project_name=m["project_name"],
                                     root_folder=m["root_folder"], parent_repo_id=m["parent_repo_id"])
              for m in metas]
    assert {r["id"] for r in batched} == {r["id"] for r in serial}
    assert len(batched) == 5
    for row in batched:
        assert row["branch"] == "master"


def test_repo_summaries_empty_list_short_circuits():
    assert git_panel.repo_summaries([]) == []


# ---------------------------------------------------------------------------
# Discovery caching (30s per owner, invalidated on create)
# ---------------------------------------------------------------------------
def _counting_walk(monkeypatch, calls):
    orig = git_panel._walk_projects

    def wrapper(projects):
        calls.append(1)
        return orig(projects)

    monkeypatch.setattr(git_panel, "_walk_projects", wrapper)


def test_discovery_is_cached_within_ttl_and_shared_across_calls(store, tmp_path, monkeypatch):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)

    calls = []
    _counting_walk(monkeypatch, calls)

    first = git_panel.discover_repos_for_owner(OWNER)
    second = git_panel.discover_repos_for_owner(OWNER)
    assert len(calls) == 1  # the second call was served from the 30s cache
    assert len(first) == 1 and len(second) == 1
    assert first[0]["path"] == str(root)


def test_discovery_cache_expires_after_ttl(store, tmp_path, monkeypatch):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    _project(store, root)

    calls = []
    _counting_walk(monkeypatch, calls)
    fake_now = [1_000_000.0]
    monkeypatch.setattr(git_panel.time, "monotonic", lambda: fake_now[0])

    git_panel.discover_repos_for_owner(OWNER)
    assert len(calls) == 1
    fake_now[0] += 10.0
    git_panel.discover_repos_for_owner(OWNER)
    assert len(calls) == 1  # still inside the 30s window

    fake_now[0] += 25.0  # 35s total: past the TTL
    git_panel.discover_repos_for_owner(OWNER)
    assert len(calls) == 2


def test_create_repo_invalidates_the_cache_for_its_owner(store, tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    _project(store, root)

    calls = []
    _counting_walk(monkeypatch, calls)

    before = git_panel.discover_repos_for_owner(OWNER)
    assert before == []
    assert len(calls) == 1

    _run(["config", "--global", "user.name", "Global User"], cwd=tmp_path)
    _run(["config", "--global", "user.email", "global@example.com"], cwd=tmp_path)
    parent = git_panel.resolve_allowed_parent_folder(OWNER, str(root))
    git_panel.create_repo(parent, "newrepo", mode="init", owner=OWNER)

    after = git_panel.discover_repos_for_owner(OWNER)
    assert len(calls) == 2  # a fresh walk, not the stale cached empty list
    assert len(after) == 1
    assert after[0]["name"] == "newrepo"


def test_discovery_cache_is_per_owner(store, tmp_path, monkeypatch):
    root_a = tmp_path / "a"
    _init_repo(root_a)
    _commit(root_a, "a.txt", "1\n", "initial")
    _project(store, root_a, name="ProjA", owner="alice")

    root_b = tmp_path / "b"
    root_b.mkdir()
    _project(store, root_b, name="ProjB", owner="bob")

    calls = []
    _counting_walk(monkeypatch, calls)

    assert len(git_panel.discover_repos_for_owner("alice")) == 1
    assert len(git_panel.discover_repos_for_owner("bob")) == 0
    assert len(calls) == 2  # one walk per owner -- bob's empty result isn't alice's cache


def test_project_scoped_discovery_always_walks_fresh(store, tmp_path, monkeypatch):
    root = tmp_path / "work"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    proj = _project(store, root)

    calls = []
    _counting_walk(monkeypatch, calls)
    git_panel.discover_repos_for_owner(OWNER, project_id=proj["id"])
    git_panel.discover_repos_for_owner(OWNER, project_id=proj["id"])
    assert len(calls) == 2  # never cached -- always a fresh, cheap single-project walk


def test_unknown_project_id_still_404s_through_the_route(client, store, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    _project(store, root)
    resp = client.get("/api/git/repos", params={"project_id": "nope"}, headers=_hdr())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dedupe: same real path linked by two different projects
# ---------------------------------------------------------------------------
def test_dedupe_same_repo_linked_by_two_projects(client, store, tmp_path):
    root = tmp_path / "shared"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")

    p1 = _project(store, root, name="ProjA")
    p2 = _project(store, root, name="ProjB")

    resp = client.get("/api/git/repos", headers=_hdr())
    assert resp.status_code == 200
    rows = [r for r in resp.json()["repos"] if r["path"] == str(root)]
    assert len(rows) == 1, rows
    row = rows[0]
    # Compatibility: project_id/project_name stay the FIRST project.
    assert row["project_id"] == p1["id"]
    assert row["project_name"] == p1["name"]
    proj_ids = {p["id"] for p in row["projects"]}
    assert proj_ids == {p1["id"], p2["id"]}


def test_dedupe_project_workspace_and_folder_link_to_same_root_lists_once(client, store, tmp_path):
    # One project whose `workspace` AND a `folder` context link both point at
    # the very same real folder -- `_project_root_folders`'s own dedup
    # already collapses this within one project; asserted here as the
    # boundary `_dedupe_repos` (the cross-project case) must not regress.
    root = tmp_path / "onlyone"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    proj = store.create(name="Solo", folder="Solo", workspace=str(root), owner=OWNER, scaffold_memory=False)
    store.upsert_link(proj["id"], {"kind": "folder", "path": str(root)}, owner=OWNER)

    resp = client.get("/api/git/repos", headers=_hdr())
    rows = [r for r in resp.json()["repos"] if r["path"] == str(root)]
    assert len(rows) == 1


def test_dedupe_preserves_a_repos_own_identity_and_status(client, store, tmp_path):
    root = tmp_path / "shared2"
    _init_repo(root)
    _commit(root, "a.txt", "1\n", "initial")
    (root / "b.txt").write_text("dirty\n", encoding="utf-8")

    _project(store, root, name="X")
    _project(store, root, name="Y")

    resp = client.get("/api/git/repos", headers=_hdr())
    rows = [r for r in resp.json()["repos"] if r["path"] == str(root)]
    assert len(rows) == 1
    assert rows[0]["dirty"]["untracked"] == 1
