"""Preconditions that must hold before anything commits into the user's repo.

An auto-commit fired at a repository in the wrong state is destructive and
silent: mid-rebase it turns a conflict resolution into a lost one, on a
detached HEAD it makes a commit nothing points at, and in the wrong repository
it lands somewhere nobody will look.
"""
import os
import shutil
import subprocess

import pytest

from src.git_invariants import Preconditions, canonical_git_remote, check_preconditions

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

GIT_ID = ("-c", "user.name=t", "-c", "user.email=t@x")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    """A clean repository with one commit, on a branch, no remote."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


# ── the happy path ─────────────────────────────────────────────────────────

def test_clean_repo_has_no_problems(repo):
    result = check_preconditions(str(repo))
    assert result.ok and result.problems == []


def test_preconditions_is_truthy_when_ok(repo):
    assert check_preconditions(str(repo))


def test_a_subdirectory_of_the_repo_passes(repo):
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert check_preconditions(str(sub)).ok


def test_a_repo_with_no_commits_yet_passes(tmp_path):
    # An unborn branch is a branch: HEAD points at refs/heads/<name>.
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    assert check_preconditions(str(root)).ok


# ── not a work tree ────────────────────────────────────────────────────────

def test_a_plain_directory_is_refused(tmp_path):
    result = check_preconditions(str(tmp_path))
    assert not result.ok and "not inside a git repository" in result.problems[0]


def test_a_missing_directory_is_refused(tmp_path):
    result = check_preconditions(str(tmp_path / "nope"))
    assert not result.ok and result.problems


def test_a_bare_repository_is_refused(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    result = check_preconditions(str(bare))
    assert not result.ok
    assert any("bare repository" in p for p in result.problems)


def test_inside_the_git_directory_is_refused(repo):
    result = check_preconditions(str(repo / ".git"))
    assert not result.ok
    assert any(".git directory" in p for p in result.problems)


# ── an operation is half-finished ──────────────────────────────────────────

def test_a_conflicted_merge_is_refused(repo):
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n", encoding="utf-8")
    git(repo, "commit", "-qam", "side")
    git(repo, "checkout", "-q", "-")
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    git(repo, "commit", "-qam", "main")
    git(repo, "merge", "side", check=False)
    result = check_preconditions(str(repo))
    assert not result.ok
    assert any("merge is in progress" in p.lower() for p in result.problems)


def test_a_stopped_rebase_is_refused(repo):
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n", encoding="utf-8")
    git(repo, "commit", "-qam", "side")
    git(repo, "checkout", "-q", "-")
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    git(repo, "commit", "-qam", "main")
    subprocess.run(["git", *GIT_ID, "rebase", "side"], cwd=str(repo), capture_output=True,
                   text=True, env={**os.environ, "GIT_EDITOR": "true"})
    result = check_preconditions(str(repo))
    assert not result.ok
    assert any("rebase is in progress" in p.lower() for p in result.problems)


def test_an_in_progress_operation_is_reported_once(repo):
    # rebase-merge/ and REBASE_HEAD both exist mid-rebase; one sentence, not two.
    git_dir = repo / ".git"
    (git_dir / "REBASE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    (git_dir / "rebase-merge").mkdir()
    problems = check_preconditions(str(repo)).problems
    assert len([p for p in problems if "rebase is in progress" in p.lower()]) == 1


def test_a_cherry_pick_in_progress_is_refused(repo):
    (repo / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n", encoding="utf-8")
    result = check_preconditions(str(repo))
    assert not result.ok
    assert any("cherry-pick is in progress" in p.lower() for p in result.problems)


def test_a_bisect_in_progress_is_refused(repo):
    (repo / ".git" / "BISECT_LOG").write_text("git bisect start\n", encoding="utf-8")
    result = check_preconditions(str(repo))
    assert not result.ok
    assert any("bisect is in progress" in p.lower() for p in result.problems)


# ── detached HEAD ──────────────────────────────────────────────────────────

def test_detached_head_is_refused(repo):
    git(repo, "checkout", "-q", "--detach", "HEAD")
    result = check_preconditions(str(repo))
    assert not result.ok
    assert any("detached" in p.lower() for p in result.problems)


def test_detached_head_is_allowed_when_asked_for(repo):
    git(repo, "checkout", "-q", "--detach", "HEAD")
    assert check_preconditions(str(repo), allow_detached=True).ok


# ── the remote is the one we think it is ───────────────────────────────────

def test_no_remote_reads_as_none(repo):
    assert canonical_git_remote(str(repo)) is None


def test_ssh_and_https_forms_of_one_remote_are_equal(repo):
    git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    assert canonical_git_remote(str(repo)) == "github.com/owner/repo"
    for form in ("https://github.com/owner/repo",
                 "https://github.com/owner/repo.git",
                 "ssh://git@github.com/owner/repo.git",
                 "ssh://git@github.com:22/owner/repo",
                 "git@github.com:owner/repo"):
        assert check_preconditions(str(repo), expect_remote=form).ok, form


def test_an_ssh_alias_keeps_the_alias_as_the_host(repo):
    # Luis's own setup uses a Host alias from ~/.ssh/config, not a real hostname.
    git(repo, "remote", "add", "origin", "git@Luissalet:owner/repo.git")
    assert canonical_git_remote(str(repo)) == "luissalet/owner/repo"
    assert check_preconditions(str(repo), expect_remote="git@Luissalet:owner/repo.git").ok


def test_a_different_remote_is_refused(repo):
    git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    result = check_preconditions(str(repo), expect_remote="https://github.com/someone/else")
    assert not result.ok
    assert any("not the expected" in p for p in result.problems)


def test_a_missing_origin_against_an_expectation_is_refused(repo):
    result = check_preconditions(str(repo), expect_remote="git@github.com:owner/repo.git")
    assert not result.ok
    assert any("no 'origin' remote" in p for p in result.problems)


def test_the_remote_is_not_checked_when_nothing_is_expected(repo):
    git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    assert check_preconditions(str(repo)).ok


# ── the branch is the one we think it is ───────────────────────────────────

def test_the_expected_branch_passes(repo):
    git(repo, "checkout", "-q", "-b", "work")
    assert check_preconditions(str(repo), expect_branch="work").ok


def test_a_different_branch_is_refused(repo):
    git(repo, "checkout", "-q", "-b", "work")
    result = check_preconditions(str(repo), expect_branch="main")
    assert not result.ok
    assert any("not the expected 'main'" in p for p in result.problems)


def test_a_branch_expectation_against_a_detached_head_is_refused(repo):
    git(repo, "checkout", "-q", "--detach", "HEAD")
    result = check_preconditions(str(repo), expect_branch="main", allow_detached=True)
    assert not result.ok
    assert any("HEAD is detached" in p for p in result.problems)


# ── the problems are usable ────────────────────────────────────────────────

def test_every_problem_is_a_sentence(repo):
    git(repo, "checkout", "-q", "--detach", "HEAD")
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    result = check_preconditions(str(repo), expect_branch="main")
    assert len(result.problems) >= 2
    for problem in result.problems:
        assert problem.endswith("."), problem
        assert len(problem.split()) >= 5, problem


def test_problems_default_to_empty():
    assert Preconditions(True).problems == []


# ── wired into the auto-commit ─────────────────────────────────────────────

def _configured(repo):
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@x")
    return repo


def test_the_auto_commit_refuses_mid_merge(repo):
    from src import workspace_checkpoints as wc
    _configured(repo)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    result = wc.user_git_commit(str(repo), ["a.txt"], "should not happen")
    assert result["ok"] is False and result.get("refused") is True
    assert "merge is in progress" in result["error"].lower()
    assert result["problems"]


def test_a_refused_commit_writes_nothing(repo):
    from src import workspace_checkpoints as wc
    _configured(repo)
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n", encoding="utf-8")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    wc.user_git_commit(str(repo), ["a.txt"], "should not happen")
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert "a.txt" in git(repo, "status", "--porcelain").stdout


def test_the_auto_commit_refuses_on_a_detached_head(repo):
    from src import workspace_checkpoints as wc
    _configured(repo)
    git(repo, "checkout", "-q", "--detach", "HEAD")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    result = wc.user_git_commit(str(repo), ["a.txt"], "should not happen")
    assert result["ok"] is False and result.get("refused") is True
    assert "detached" in result["error"].lower()


def test_the_refusal_reaches_the_caller_as_an_error_string(repo):
    # routes/workspace_routes.py surfaces res["error"] verbatim as the 400 detail.
    from src import workspace_checkpoints as wc
    _configured(repo)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    result = wc.user_git_commit(str(repo), ["a.txt"], "m")
    assert isinstance(result.get("error"), str) and result["error"].strip()


def test_a_healthy_repo_still_commits(repo):
    from src import workspace_checkpoints as wc
    _configured(repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    result = wc.user_git_commit(str(repo), ["a.txt"], "Change a")
    assert result["ok"] is True, result
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "Change a"


def test_the_commit_child_does_not_inherit_the_venv(repo, monkeypatch):
    # A pre-commit hook is the user's own code; ours must not shadow theirs.
    from src import workspace_checkpoints as wc
    _configured(repo)
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/faustus/venv")
    monkeypatch.setenv("PYTHONPATH", "/srv/faustus/venv/lib/site-packages")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    seen = repo / "hook-env.txt"
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f'printf "VIRTUAL_ENV=[%s] PYTHONPATH=[%s]\\n" "$VIRTUAL_ENV" "$PYTHONPATH" > "{seen}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(hook, 0o755)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    result = wc.user_git_commit(str(repo), ["a.txt"], "Change a")
    assert result["ok"] is True, result
    assert seen.read_text(encoding="utf-8").strip() == "VIRTUAL_ENV=[] PYTHONPATH=[]"
