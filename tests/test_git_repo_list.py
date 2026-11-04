"""The Source control repo list within a time budget (src/git_repo_list.py):
shared bounded pool, one read per repo at a time, fresh statuses from
memory, config reads cached by mtime, stale/pending rows by the deadline,
and `GET /api/git/repos` never waiting past it. Real `git` throughout."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import pytest

from src import git_panel, git_repo_list as grl

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _run(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("git_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    grl.clear_cache()
    yield
    grl.clear_cache()


def _repo(path, branch="main"):
    path.mkdir(parents=True)
    _run(["init"], path)
    _run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], path)
    _run(["config", "user.name", "Repo User"], path)
    _run(["config", "user.email", "repo@example.com"], path)
    (path / "a.txt").write_text("1\n", encoding="utf-8")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "init"], path)
    return path


def _meta(path):
    return {"path": str(path), "project_id": "p", "project_name": "P", "root_folder": str(path.parent),
            "parent_repo_id": None, "projects": None, "watched": False}


def _counting(monkeypatch):
    calls = []
    orig = git_panel.run_git

    def wrapper(repo_dir, *args, **kwargs):
        calls.append(args[0] if args else "")
        return orig(repo_dir, *args, **kwargs)

    monkeypatch.setattr(git_panel, "run_git", wrapper)
    return calls


def _strip(row):
    return {k: v for k, v in row.items() if k not in ("status_state", "status_age_s", "_local_identity")}


def test_rows_match_repo_summaries_full_and_light(tmp_path):
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b", branch="dev")
    (b / "new.txt").write_text("x", encoding="utf-8")
    (b / "a.txt").write_text("2\n", encoding="utf-8")
    metas = [_meta(a), _meta(b)]
    for light in (False, True):
        grl.clear_cache()
        rows, counts = grl.summaries(metas, light=light)
        assert [_strip(r) for r in rows] == git_panel.repo_summaries(metas, light=light)
        assert counts["fresh"] == 2 and counts["stale"] == counts["pending"] == 0
        assert all(r["status_state"] == "fresh" for r in rows)
    assert rows[1]["dirty"] == {"staged": 0, "unstaged": 1, "untracked": 1}


def test_fresh_status_and_unchanged_config_are_not_read_again(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a")
    grl.summaries([_meta(a)])
    calls = _counting(monkeypatch)
    rows, _ = grl.summaries([_meta(a)])
    assert calls == []
    assert rows[0]["user"] == {"name": "Repo User", "email": "repo@example.com"}
    # An old status is read again; config is not, until a config file changes.
    monkeypatch.setattr(grl, "fresh_for", lambda duration: 0.0)
    grl.summaries([_meta(a)])
    assert calls == ["status"]
    calls.clear()
    _run(["config", "user.name", "Someone Else"], a)
    rows, _ = grl.summaries([_meta(a)])
    assert sorted(calls) == ["config", "config", "remote", "status"]
    assert rows[0]["user"]["name"] == "Someone Else"


def test_a_slow_repo_is_pending_then_stale_never_missing(tmp_path, monkeypatch):
    fast = _repo(tmp_path / "fast")
    slow = _repo(tmp_path / "slow", branch="feature/x")
    gate = threading.Event()
    real = grl._read_status

    def read(path):
        if path == str(slow):
            gate.wait(10)
        return real(path)

    monkeypatch.setattr(grl, "_read_status", read)
    t0 = time.monotonic()
    rows, counts = grl.summaries([_meta(fast), _meta(slow)], light=True, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - t0 < 3
    assert counts == {"fresh": 1, "stale": 0, "pending": 1, "extras_pending": 0}
    pending = rows[1]
    assert pending["status_state"] == "pending" and pending["status_age_s"] is None
    assert pending["branch"] == "feature/x" and pending["name"] == "slow"
    gate.set()
    for _ in range(50):  # the read finishes in the background
        if str(slow) in grl._status_cache:
            break
        time.sleep(0.1)
    monkeypatch.setattr(grl, "fresh_for", lambda duration: 0.0)
    gate.clear()
    rows, counts = grl.summaries([_meta(slow)], light=True, deadline=time.monotonic() + 0.3)
    assert rows[0]["status_state"] == "stale" and rows[0]["status_age_s"] is not None
    assert rows[0]["branch"] == "feature/x"
    gate.set()


def test_overlapping_lists_share_one_read_per_repo(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a")
    gate = threading.Event()
    started = []
    real = grl._read_status

    def read(path):
        started.append(path)
        gate.wait(10)
        return real(path)

    monkeypatch.setattr(grl, "_read_status", read)
    out = []
    threads = [threading.Thread(target=lambda: out.append(grl.summaries([_meta(a)], light=True,
                                                                        deadline=time.monotonic() + 5)))
               for _ in range(3)]
    for th in threads:
        th.start()
    time.sleep(0.5)
    gate.set()
    for th in threads:
        th.join(10)
    assert started == [str(a)]
    assert [rows[0]["status_state"] for rows, _ in out] == ["fresh"] * 3


def test_head_status_reads_a_worktree_file(tmp_path):
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _run(["worktree", "add", "-b", "side", str(wt)], main)
    assert grl.head_status(str(wt))["branch"] == "side"
    assert grl.head_status(str(tmp_path / "nothing"))["branch"] is None


def test_route_answers_by_its_budget_and_says_it_is_refreshing(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import git_routes
    from services import projects as projects_mod

    monkeypatch.setenv("AUTH_ENABLED", "false")
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    monkeypatch.setattr(git_routes, "effective_user", lambda request: "luis")
    git_panel.invalidate_discovery_cache()
    ws = tmp_path / "ws"
    _repo(ws / "Proj" / "quick")
    _repo(ws / "Proj" / "big")
    st.create(name="Proj", folder="Proj", workspace=str(ws), owner="luis", scaffold_memory=False)
    gate = threading.Event()
    real_status, real_extras = grl._read_status, grl._read_extras
    def slow(real):
        def run(p):
            if p.endswith("big"):
                gate.wait(60)
            return real(p)
        return run

    # The proof that the route answers by its budget is that "big" comes back
    # pending while its read is still held at the gate (asserted below). The
    # wall clock only has to stay clearly under the gate: a loaded
    # eight-worker run took 12 s against the old 8 s bound with a 1 s budget,
    # and a 1 s budget could leave even "quick" unread there.
    monkeypatch.setattr(grl, "_read_status", slow(real_status))
    monkeypatch.setattr(grl, "_read_extras", slow(real_extras))
    monkeypatch.setattr(grl, "LIST_BUDGET_S", 5.0)
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    t0 = time.monotonic()
    data = TestClient(app).get("/api/git/repos").json()
    assert time.monotonic() - t0 < 45
    gate.set()
    by_name = {r["name"]: r for r in data["repos"]}
    assert set(by_name) >= {"quick", "big"}
    assert data["refreshing"] is True and data["status_counts"]["pending"] >= 1
    assert by_name["big"]["status_state"] == "pending" and by_name["big"]["extras_pending"] is True
    assert by_name["big"]["identity"] is None and "policy" in by_name["big"]
    assert by_name["quick"]["status_state"] == "fresh" and "_local_identity" not in by_name["quick"]
    assert by_name["quick"]["user"]["name"] == "Repo User"
    git_panel.invalidate_discovery_cache()


def test_the_slowest_repo_is_read_first(tmp_path, monkeypatch):
    a, b, c = (_repo(tmp_path / n) for n in ("a", "b", "c"))
    grl._durations.update({str(a): 0.1, str(b): 6.0, str(c): 0.2})
    order = []
    real = grl._submit
    monkeypatch.setattr(grl, "_submit", lambda kind, path, fn: order.append((kind, path)) or real(kind, path, fn))
    grl.summaries([_meta(a), _meta(b), _meta(c)], light=True)
    assert [p for k, p in order if k == "status"] == [str(b), str(c), str(a)]


def test_identity_list_is_read_once_per_full_list(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import git_routes
    from services import projects as projects_mod
    from src import git_identities

    monkeypatch.setenv("AUTH_ENABLED", "false")
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    monkeypatch.setattr(git_routes, "effective_user", lambda request: "luis")
    git_panel.invalidate_discovery_cache()
    ws = tmp_path / "ws"
    for name in ("r1", "r2", "r3"):
        repo = _repo(ws / "Proj" / name)
        _run(["remote", "add", "origin", "git@gh-work:me/x.git"], repo)
    st.create(name="Proj", folder="Proj", workspace=str(ws), owner="luis", scaffold_memory=False)
    reads = []
    monkeypatch.setattr(git_identities, "list_identities",
                        lambda owner, **k: reads.append(owner) or {"identities": [
                            {"id": "w", "label": "Work", "ssh_host": "gh-work", "github_login": "me"}]})
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    data = TestClient(app).get("/api/git/repos").json()
    assert reads == ["luis"]
    assert [r["identity"]["id"] for r in data["repos"]] == ["w", "w", "w"]
    assert set(data["timing_s"]) == {"discover", "statuses", "identity_policy"}
    git_panel.invalidate_discovery_cache()


def test_stale_ok_discovery_answers_from_the_last_walk_and_walks_behind(tmp_path, monkeypatch):
    from services import projects as projects_mod

    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    git_panel.invalidate_discovery_cache()
    ws = tmp_path / "ws"
    _repo(ws / "Proj" / "one")
    st.create(name="Proj", folder="Proj", workspace=str(ws), owner="luis", scaffold_memory=False)
    first = git_panel.discover_repos_for_owner("luis")
    assert [m["name"] for m in first] == ["one"]
    _repo(ws / "Proj" / "two")
    monkeypatch.setattr(git_panel, "_DISCOVERY_TTL", 0.0)
    walks = []
    real = git_panel._walk_owner
    monkeypatch.setattr(git_panel, "_walk_owner", lambda *a: walks.append(1) or real(*a))
    stale = git_panel.discover_repos_for_owner("luis", stale_ok=True)
    assert [m["name"] for m in stale] == ["one"]  # at once, from the last walk
    for _ in range(50):
        if not git_panel._DISCOVERY_REFRESHING and walks:
            break
        time.sleep(0.1)
    monkeypatch.setattr(git_panel, "_DISCOVERY_TTL", 30.0)
    assert sorted(m["name"] for m in git_panel.discover_repos_for_owner("luis")) == ["one", "two"]
    assert walks == [1]
    git_panel.invalidate_discovery_cache()


def test_a_repo_slower_than_the_time_left_is_not_waited_for(tmp_path, monkeypatch):
    assert grl.fresh_for(0.1) == grl.STATUS_FRESH_S
    assert grl.fresh_for(10.0) == 30.0 and grl.fresh_for(100.0) == 60.0
    a = _repo(tmp_path / "big")
    grl.summaries([_meta(a)], light=True)
    with grl._lock:
        at, status = grl._status_cache[str(a)]
        grl._status_cache[str(a)] = (at - 300, status)
        grl._durations[str(a)] = 30.0
    gate = threading.Event()
    real = grl._read_status
    monkeypatch.setattr(grl, "_read_status", lambda p: gate.wait(10) and real(p))
    t0 = time.monotonic()
    rows, counts = grl.summaries([_meta(a)], light=True, deadline=time.monotonic() + 5)
    assert time.monotonic() - t0 < 1.5
    assert rows[0]["status_state"] == "stale" and rows[0]["status_age_s"] >= 300
    gate.set()
