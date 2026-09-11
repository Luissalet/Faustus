"""Lote 92 (OBJ-6) -- the board's git hook: a commit mentioning a FAU-12
style id links it; one preceded by a magic close word ("fixes FAU-3") closes
it. Covers `src.project_board.link_commit` directly, `src.git_panel.commit`'s
`project_id=` parameter end to end against a real repo, and
`src.agent_git_policy.after_turn`'s own auto-commit path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_git_policy, constants as constants_mod, git_panel, project_board  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from services import projects as projects_mod  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

PROJECT = "proj-git-link"


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
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _run(["add", "-A"], cwd=path)
    _run(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
          "commit", "-m", "Initial commit"], cwd=path)
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
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(data_dir / "settings.json"))
    settings_mod._invalidate_caches()
    yield


@pytest.fixture()
def board():
    return project_board.Store()  # default_path() resolves under the isolated DATA_DIR


@pytest.fixture()
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# project_board.link_commit -- the mechanism itself
# ---------------------------------------------------------------------------
def test_link_commit_mentions_without_closing(board):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="retry leak")
    result = board.link_commit(PROJECT, "abc123", f"wip on {issue['id']}, not done yet")
    assert result["linked"] == [issue["id"]]
    assert result["closed"] == []
    full = board.get(issue["id"])
    assert full["status"] == "open"
    assert full["refs"][0]["value"] == "abc123"
    assert full["refs"][0]["kind"] == "commit"


def test_link_commit_with_fixes_closes_the_issue(board):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="retry leak")
    result = board.link_commit(PROJECT, "deadbeef", f"fixes {issue['id']}: clear sessionId on retry")
    assert result["closed"] == [issue["id"]]
    full = board.get(issue["id"])
    assert full["status"] == "done"
    assert full["closed_at"]
    assert any(e["kind"] == "commit_linked" and e["payload"].get("sha") == "deadbeef"
               for e in full["events"])


def test_link_commit_recognizes_spanish_close_words(board):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="x")
    board.link_commit(PROJECT, "sha1", f"cierra {issue['id']}: arreglado")
    assert board.get(issue["id"])["status"] == "done"


def test_link_commit_never_closes_an_issue_from_another_project(board):
    other = board.create_issue("other-project", "FAU", type="bug", title="unrelated")
    board.link_commit(PROJECT, "sha2", f"fixes {other['id']}")
    # the id belongs to a different project -- link_commit(PROJECT, ...) must
    # not touch it
    assert board.get(other["id"])["status"] == "open"


def test_link_commit_never_reopens_or_re_closes_a_terminal_issue(board):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="x")
    board.update_issue(issue["id"], {"status": "wontfix"})
    board.link_commit(PROJECT, "sha3", f"fixes {issue['id']}")
    assert board.get(issue["id"])["status"] == "wontfix"  # unchanged, not reopened to done-again


def test_link_commit_is_never_fatal_on_garbage_input(board):
    # No project, no sha, no message, mismatched types -- none of this may
    # raise; this hook runs on the hot commit path.
    assert board.link_commit("", "", "") == {"linked": [], "closed": []}
    assert board.link_commit(PROJECT, "sha", "no ids here") == {"linked": [], "closed": []}


# ---------------------------------------------------------------------------
# git_panel.commit(..., project_id=...) end to end
# ---------------------------------------------------------------------------
def test_git_panel_commit_with_project_id_closes_the_board_issue(repo, board):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="retry leak")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    git_panel.stage(str(repo), paths=["a.txt"])
    result = git_panel.commit(
        str(repo), f"fixes {issue['id']}: clear sessionId on retry", project_id=PROJECT,
    )
    assert result["sha"]
    full = board.get(issue["id"])
    assert full["status"] == "done"
    assert full["refs"][0]["value"] == result["sha"]


def test_git_panel_commit_without_project_id_is_unchanged(repo, board):
    """The parameter is optional and defaults to None -- every caller that
    predates Lote 92 (and any board issue that happens to share text with the
    message) is untouched."""
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="retry leak")
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    git_panel.stage(str(repo), paths=["a.txt"])
    git_panel.commit(str(repo), f"fixes {issue['id']}")  # no project_id
    assert board.get(issue["id"])["status"] == "open"


def test_git_panel_commit_board_failure_never_fails_the_commit(repo, board, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("board is unavailable")
    monkeypatch.setattr(project_board, "link_commit", _boom)
    (repo / "a.txt").write_text("four\n", encoding="utf-8")
    git_panel.stage(str(repo), paths=["a.txt"])
    result = git_panel.commit(str(repo), "fixes FAU-1", project_id=PROJECT)
    assert result["sha"]  # the commit itself still succeeded


# ---------------------------------------------------------------------------
# agent_git_policy.after_turn -- the agent's own auto-commit path
# ---------------------------------------------------------------------------
def test_after_turn_auto_commit_closes_the_board_issue(repo, board, monkeypatch):
    issue = board.create_issue(PROJECT, "FAU", type="bug", title="retry leak")
    monkeypatch.setattr(projects_mod, "project_for_session",
                         lambda session_id, owner: {"id": PROJECT})
    agent_git_policy.set_global_policy({"commit": True, "push": False})
    (repo / "a.txt").write_text("five\n", encoding="utf-8")
    events = agent_git_policy.after_turn(
        str(repo), "sess-1", "owner-1", [str(repo / "a.txt")],
        summary=f"fixes {issue['id']}: clear sessionId on retry",
    )
    assert any(e["action"] == "commit" and e["ok"] for e in events)
    assert board.get(issue["id"])["status"] == "done"
