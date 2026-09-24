"""src/git_radar.py -- which repos are waiting for a commit or a push.

Real `git` against temp repos (the module is a thin classification over
`git_panel`'s own status calls, so a mocked status would test nothing).
"""
from __future__ import annotations

import os
import subprocess

import pytest

from src import git_panel, git_radar

pytestmark = pytest.mark.skipif(not git_panel.git_available(), reason="git not installed")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                        "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(cwd)})


def _repo(path, *, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    if commit:
        (path / "a.txt").write_text("a")
        _git(path, "add", "a.txt")
        _git(path, "commit", "-q", "-m", "init")
    return path


def _with_remote(path, bare):
    bare.mkdir(parents=True, exist_ok=True)
    _git(bare, "init", "-q", "--bare", "-b", "main")
    _git(path, "remote", "add", "origin", str(bare))
    _git(path, "push", "-q", "-u", "origin", "main")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    root = tmp_path / "watched"
    root.mkdir()
    monkeypatch.setattr(git_panel, "watch_roots", lambda: [str(root)])
    monkeypatch.setattr(git_panel, "_walk_projects", lambda projects: [])

    class _Store:
        def list(self, owner):
            return []

        def get(self, pid, owner):
            return None

    import services.projects as projects_mod
    monkeypatch.setattr(projects_mod, "get_store", lambda: _Store())
    git_panel.invalidate_discovery_cache()
    git_radar.invalidate()
    return root


def test_classify_reads_the_summary_row():
    row = {"branch": "main", "head_sha": "abc", "upstream": "origin/main", "ahead": 2, "behind": 1,
           "dirty": {"staged": 1, "unstaged": 0, "untracked": 2}, "conflicts": 0, "detached": False}
    kinds = [r["kind"] for r in git_radar.classify(row)]
    assert kinds == ["uncommitted", "unpushed", "behind"]
    counts = {r["kind"]: r["count"] for r in git_radar.classify(row)}
    assert counts["uncommitted"] == 3 and counts["unpushed"] == 2 and counts["behind"] == 1


def test_classify_no_upstream_vs_local_only():
    row = {"branch": "main", "head_sha": "abc", "upstream": None, "ahead": 0, "behind": 0,
           "dirty": {"staged": 0, "unstaged": 0, "untracked": 0}, "conflicts": 0, "detached": False}
    assert [r["kind"] for r in git_radar.classify(row, has_remote=True)] == ["no_upstream"]
    assert [r["kind"] for r in git_radar.classify(row, has_remote=False)] == ["local_only"]
    assert [r["kind"] for r in git_radar.classify(row, has_remote=None)] == ["no_upstream"]
    # An empty repo (no commits) is not "waiting" for anything yet.
    row["head_sha"] = None
    assert git_radar.classify(row, has_remote=False) == []


def test_classify_conflicts_rank_first():
    row = {"branch": "main", "head_sha": "abc", "upstream": "origin/main", "ahead": 1, "behind": 0,
           "dirty": {"staged": 0, "unstaged": 2, "untracked": 0}, "conflicts": 2, "detached": False}
    assert [r["kind"] for r in git_radar.classify(row)] == ["conflicts", "uncommitted", "unpushed"]


def test_scan_finds_repos_under_watched_roots_and_classifies(roots, tmp_path):
    clean = _repo(roots / "clean")
    _with_remote(clean, tmp_path / "clean.git")
    dirty = _repo(roots / "dirty")
    _with_remote(dirty, tmp_path / "dirty.git")
    (dirty / "b.txt").write_text("b")
    ahead = _repo(roots / "ahead")
    _with_remote(ahead, tmp_path / "ahead.git")
    (ahead / "c.txt").write_text("c")
    _git(ahead, "add", "c.txt")
    _git(ahead, "commit", "-q", "-m", "second")
    local = _repo(roots / "local")

    payload = git_radar.scan("alice", refresh=True)
    by_name = {r["name"]: r for r in payload["repos"]}
    assert set(by_name) == {"clean", "dirty", "ahead", "local"}
    assert payload["total"] == 4
    assert by_name["clean"]["attention"] is False and by_name["clean"]["reasons"] == []
    assert [r["kind"] for r in by_name["dirty"]["reasons"]] == ["uncommitted"]
    assert by_name["dirty"]["reasons"][0]["count"] == 1
    assert [r["kind"] for r in by_name["ahead"]["reasons"]] == ["unpushed"]
    assert by_name["ahead"]["ahead"] == 1
    assert [r["kind"] for r in by_name["local"]["reasons"]] == ["local_only"]
    assert all(r["watched"] for r in payload["repos"])
    # Attention rows first, most recent commit first ("ahead" committed last).
    assert payload["attention"][0]["name"] == "ahead"
    assert sorted(r["name"] for r in payload["attention"]) == ["ahead", "dirty", "local"]
    assert payload["repos"][-1]["name"] == "clean"
    assert payload["attention_count"] == 3
    assert payload["counts"]["uncommitted"] == 1 and payload["counts"]["unpushed"] == 1
    assert payload["counts"]["local_only"] == 1
    assert all(r["last_commit_at"] for r in payload["attention"])
    assert by_name["clean"]["last_commit_at"] is None
    assert payload["watch_roots"] == [str(roots)]
    text = git_radar.format_summary(payload)
    assert "3 of 4" in text and "dirty (main): 1 uncommitted" in text and "ahead (main): 1 unpushed" in text


def test_scan_is_cached_until_refresh(roots, tmp_path):
    repo = _repo(roots / "one")
    _with_remote(repo, tmp_path / "one.git")
    first = git_radar.scan("bob", refresh=True)
    assert first["attention_count"] == 0 and first["stale"] is False
    (repo / "z.txt").write_text("z")
    assert git_radar.scan("bob") is first  # cached
    assert git_radar.scan("bob", refresh=True)["attention_count"] == 1


def test_expired_cache_answers_stale_at_once_and_rescans_in_background(roots, tmp_path, monkeypatch):
    repo = _repo(roots / "one")
    _with_remote(repo, tmp_path / "one.git")
    first = git_radar.scan("dora", refresh=True)
    (repo / "z.txt").write_text("z")
    # Expire the cache without waiting.
    with git_radar._LOCK:
        ts, payload = git_radar._CACHE[git_radar.git_panel._owner_cache_key("dora")]
        git_radar._CACHE[git_radar.git_panel._owner_cache_key("dora")] = (ts - git_radar.RADAR_TTL - 1, payload)
    stale = git_radar.scan("dora")
    assert stale["stale"] is True and stale["attention_count"] == first["attention_count"] == 0
    # The background rescan lands shortly after.
    import time as _t
    for _ in range(50):
        _t.sleep(0.1)
        again = git_radar.scan("dora")
        if not again.get("stale"):
            break
    assert again["stale"] is False and again["attention_count"] == 1


def test_all_clean_summary(roots, tmp_path):
    repo = _repo(roots / "one")
    _with_remote(repo, tmp_path / "one.git")
    payload = git_radar.scan("carol", refresh=True)
    assert git_radar.format_summary(payload) == "All 1 repositories are committed and pushed."


def test_set_watch_roots_validates(tmp_path, monkeypatch):
    saved = {}
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "update_settings", lambda patch: saved.update(patch) or {})
    good = tmp_path / "ok"
    good.mkdir()
    bad = git_radar.set_watch_roots(["relative/path"])
    assert bad["ok"] is False and "absolute" in bad["error"] and saved == {}
    missing = git_radar.set_watch_roots([str(tmp_path / "nope")])
    assert missing["ok"] is False and saved == {}
    res = git_radar.set_watch_roots([str(good), str(good), " "])
    assert res["ok"] is False  # blank entry refused
    res = git_radar.set_watch_roots([str(good), str(good)])
    assert res["ok"] is True and res["watch_roots"] == [os.path.realpath(str(good))]
    assert saved["git_watch_roots"] == [os.path.realpath(str(good))]


def test_watch_roots_setting_drops_bad_entries(tmp_path, monkeypatch):
    good = tmp_path / "g"
    good.mkdir()
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: [str(good), "rel", 3, str(tmp_path / "missing"), str(good)]
                        if key == "git_watch_roots" else default)
    assert git_panel.watch_roots() == [os.path.realpath(str(good))]


def test_dedupe_keeps_project_entry_over_watched_one():
    entries = [
        {"id": "x", "path": "/r/a", "name": "a", "project_id": "p1", "project_name": "P1",
         "root_folder": "/r", "parent_repo_id": None},
        {"id": "x", "path": "/r/a", "name": "a", "project_id": None, "project_name": None,
         "root_folder": "/r", "parent_repo_id": None, "watched": True},
    ]
    out = git_panel._dedupe_repos(entries)
    assert len(out) == 1
    assert out[0]["projects"] == [{"id": "p1", "name": "P1"}]
    assert out[0]["watched"] is False


# ---------------------------------------------------------------------------
# Routes: /api/git/radar and /api/git/watch-roots
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import git_routes
    from services import projects as projects_mod

    monkeypatch.setenv("AUTH_ENABLED", "false")
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    monkeypatch.setattr(git_routes, "effective_user", lambda request: request.headers.get("x-test-owner") or None)
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    git_panel.invalidate_discovery_cache()
    git_radar.invalidate()
    return TestClient(app)


def test_radar_route_scans_watched_roots_and_a_watched_repo_is_a_real_repo(client, tmp_path, monkeypatch):
    root = tmp_path / "watched"
    root.mkdir()
    repo = _repo(root / "proj")
    _with_remote(repo, tmp_path / "proj.git")
    (repo / "new.txt").write_text("x")
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: [str(root)] if key == "git_watch_roots" else default)

    r = client.get("/api/git/radar", headers={"x-test-owner": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attention_count"] == 1
    row = body["attention"][0]
    assert row["name"] == "proj" and row["watched"] is True and row["projects"] == []
    assert [x["kind"] for x in row["reasons"]] == ["uncommitted"]
    assert body["watch_roots"] == [os.path.realpath(str(root))]
    assert "1 of 1" in body["summary"]

    # The radar's repo id is the panel's repo id: the row links into the
    # ordinary Source control view and every /repos/{id} route works.
    r2 = client.get(f"/api/git/repos/{row['id']}", headers={"x-test-owner": "alice"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["watched"] is True and r2.json()["dirty"]["untracked"] == 1
    listed = client.get("/api/git/repos", headers={"x-test-owner": "alice"}).json()["repos"]
    assert [x["id"] for x in listed] == [row["id"]]


def test_watch_roots_routes(client, tmp_path, monkeypatch):
    saved = {}
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "update_settings", lambda patch: saved.update(patch) or {})
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: list(saved.get("git_watch_roots", [])) if key == "git_watch_roots" else default)
    good = tmp_path / "code"
    good.mkdir()

    r = client.get("/api/git/watch-roots")
    assert r.status_code == 200 and r.json() == {"watch_roots": [], "configured": [], "exclude": []}

    bad = client.put("/api/git/watch-roots", json={"watch_roots": [str(tmp_path / "missing")]})
    assert bad.status_code == 400
    assert bad.json()["error_class"] == "git.bad_root"
    assert saved == {}

    ok = client.put("/api/git/watch-roots", json={"watch_roots": [str(good)]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["watch_roots"] == [os.path.realpath(str(good))]
    r = client.get("/api/git/watch-roots")
    assert r.json()["watch_roots"] == [os.path.realpath(str(good))]

    bad_ex = client.put("/api/git/watch-roots", json={"watch_roots": [str(good)], "exclude": ["rel/path"]})
    assert bad_ex.status_code == 400 and "git_scan_exclude" not in saved
    ok_ex = client.put("/api/git/watch-roots", json={"watch_roots": [str(good)], "exclude": ["_scratch", str(tmp_path / "vendor"), "_SCRATCH"]})
    assert ok_ex.status_code == 200, ok_ex.text
    assert ok_ex.json()["exclude"] == ["_scratch", str(tmp_path / "vendor")]
    assert saved["git_scan_exclude"] == ["_scratch", str(tmp_path / "vendor")]


def test_walk_skips_excluded_names_paths_and_dot_dirs(tmp_path):
    root = tmp_path / "root"
    keep = _repo(root / "keep")
    _repo(root / "_scratch" / "throwaway")
    _repo(root / "vendor" / "third")
    _repo(root / ".worktrees" / "wt")
    nested_keep = _repo(root / "apps" / "one")
    found = git_panel._walk_repos(str(root), exclusions=(frozenset({"_scratch"}),
                                                          (os.path.normcase(os.path.realpath(str(root / "vendor"))),)))
    assert sorted(os.path.basename(p) for p in found) == sorted([os.path.basename(str(keep)), os.path.basename(str(nested_keep))])
    # Without exclusions only the dot-dir is skipped.
    found_all = git_panel._walk_repos(str(root), exclusions=(frozenset(), ()))
    assert sorted(os.path.basename(p) for p in found_all) == ["keep", "one", "third", "throwaway"]
