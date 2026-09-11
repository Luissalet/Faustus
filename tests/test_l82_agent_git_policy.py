"""Lote 82 -- the agent's own git policy (OBJ-4): src/agent_git_policy.py.

Real `git` throughout (skipped cleanly when it isn't on PATH), real repos in
tmp_path, a real local bare remote for the push tests. Global policy goes
through `src.settings` with SETTINGS_FILE repointed at a tmp file; per-repo
overrides go through DATA_DIR/git_repo_policies.json with DATA_DIR repointed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_git_policy, constants as constants_mod, git_panel  # noqa: E402
from src import settings as settings_mod  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
SESSION = "sess_ABC-123!!"


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
    # settings.py's tiny TTL read cache is keyed by process, not by
    # SETTINGS_FILE -- without this, a value another test wrote (to ITS OWN
    # tmp settings file) within the last _CACHE_TTL seconds can leak in here.
    settings_mod._invalidate_caches()
    yield


def _set_global(**patch):
    return agent_git_policy.set_global_policy(patch)


# ---------------------------------------------------------------------------
# Global / per-repo policy storage
# ---------------------------------------------------------------------------
def test_get_global_policy_defaults_all_off():
    pol = agent_git_policy.get_global_policy()
    assert pol == agent_git_policy.DEFAULT_POLICY


def test_set_global_policy_merges_partial_patch():
    _set_global(use_branch=True)
    _set_global(commit=True)
    pol = agent_git_policy.get_global_policy()
    assert pol["use_branch"] is True
    assert pol["commit"] is True
    assert pol["push"] is False


def test_set_global_policy_rejects_unknown_field():
    with pytest.raises(agent_git_policy.GitPolicyError) as exc:
        _set_global(bogus_field=True)
    assert exc.value.code == "unknown_field"


def test_repo_override_layers_over_global_and_inherit_clears_it():
    _set_global(commit=True)
    eff0 = agent_git_policy.effective_policy(OWNER, "repo1")
    assert eff0["overridden"] is False
    assert eff0["effective"]["commit"] is True

    agent_git_policy.set_repo_override(OWNER, "repo1", {"push": True})
    eff1 = agent_git_policy.effective_policy(OWNER, "repo1")
    assert eff1["overridden"] is True
    assert eff1["effective"]["commit"] is True  # inherited from global
    assert eff1["effective"]["push"] is True    # overridden

    # A different repo (or owner) is untouched.
    assert agent_git_policy.effective_policy(OWNER, "repo2")["overridden"] is False
    assert agent_git_policy.effective_policy("mallory", "repo1")["overridden"] is False

    agent_git_policy.set_repo_override(OWNER, "repo1", {"inherit": True})
    eff2 = agent_git_policy.effective_policy(OWNER, "repo1")
    assert eff2["overridden"] is False
    assert eff2["effective"]["push"] is False  # back to the global default


# ---------------------------------------------------------------------------
# before_turn: branch creation
# ---------------------------------------------------------------------------
def test_before_turn_noop_when_policy_off(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    assert agent_git_policy.before_turn(str(repo), SESSION, OWNER) is None
    assert git_panel.current_branch(str(repo))[0] == "master"


def test_before_turn_noop_when_not_a_repo(tmp_path):
    plain = tmp_path / "notrepo"
    plain.mkdir()
    assert agent_git_policy.before_turn(str(plain), SESSION, OWNER) is None


def test_before_turn_creates_and_checks_out_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"use_branch": True})

    event = agent_git_policy.before_turn(str(repo), SESSION, OWNER)
    assert event["action"] == "branch" and event["ok"] is True
    branch = event["branch"]
    assert branch.startswith("faustus/")
    assert git_panel.current_branch(str(repo))[0] == branch


def test_before_turn_second_call_same_session_reuses_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"use_branch": True})
    first = agent_git_policy.before_turn(str(repo), SESSION, OWNER)
    # Turn 2: HEAD is already on the agent branch -- nothing to report, and
    # no second branch was created.
    second = agent_git_policy.before_turn(str(repo), SESSION, OWNER)
    assert second is None
    assert git_panel.current_branch(str(repo))[0] == first["branch"]
    branches = {b["name"] for b in git_panel.list_branches(str(repo))["local"]}
    assert sum(1 for b in branches if b.startswith("faustus/")) == 1


def test_before_turn_dirty_tree_is_skipped(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"use_branch": True})
    _write(repo, "a.txt", "dirty\n")  # uncommitted change

    event = agent_git_policy.before_turn(str(repo), SESSION, OWNER)
    assert event == {"action": "branch", "ok": False, "skipped": "dirty", "branch": "master"}
    assert git_panel.current_branch(str(repo))[0] == "master"  # untouched
    assert (repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"  # never clobbered


def test_before_turn_custom_prefix_and_workspace_is_a_subdirectory(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    (repo / "sub").mkdir()
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"use_branch": True, "branch_prefix": "agent/"})
    event = agent_git_policy.before_turn(str(repo / "sub"), SESSION, OWNER)
    assert event["ok"] is True
    assert event["branch"].startswith("agent/")


# ---------------------------------------------------------------------------
# after_turn: commit
# ---------------------------------------------------------------------------
def test_after_turn_noop_when_policy_off(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    _write(repo, "a.txt", "2\n")
    events = agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "did a thing")
    assert events == []
    assert git_panel.repo_status(str(repo))["unstaged"]  # nothing was staged/committed


def test_after_turn_noop_when_no_touched_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)), {"commit": True})
    assert agent_git_policy.after_turn(str(repo), SESSION, OWNER, [], "nothing") == []


def test_after_turn_commits_only_the_touched_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    repo_id = git_panel.compute_repo_id(str(repo))
    agent_git_policy.set_repo_override(OWNER, repo_id, {"commit": True})

    _write(repo, "a.txt", "2\n")           # touched by the turn
    _write(repo, "untouched.txt", "x\n")    # NOT touched -- must stay untracked

    events = agent_git_policy.after_turn(
        str(repo), SESSION, OWNER, [str(repo / "a.txt")], "Fixed the thing\nlonger body",
    )
    assert len(events) == 1
    commit_event = events[0]
    assert commit_event["action"] == "commit" and commit_event["ok"] is True
    assert commit_event["sha"]

    status = git_panel.repo_status(str(repo))
    assert status["untracked"] == [{"path": "untouched.txt"}]
    assert status["staged"] == [] and status["unstaged"] == []
    log = _run(["log", "-1", "--format=%s"], cwd=repo).stdout.strip()
    assert log == "faustus: Fixed the thing"  # default prefix + first line only


def test_after_turn_no_identity_skips_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo", configure_identity=False)
    _commit(repo, "a.txt", "1\n", "initial")  # per-call identity only, nothing persisted
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)), {"commit": True})
    _write(repo, "a.txt", "2\n")

    events = agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "turn")
    assert events == [{"action": "commit", "ok": False, "detail": "git.no_identity"}]
    assert git_panel.repo_status(str(repo))["unstaged"]  # untouched


def test_after_turn_commit_message_prefix_is_configurable(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"commit": True, "commit_message_prefix": "[agent] "})
    _write(repo, "a.txt", "2\n")
    agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "did stuff")
    log = _run(["log", "-1", "--format=%s"], cwd=repo).stdout.strip()
    assert log == "[agent] did stuff"


def test_after_turn_conflicts_block_commit(tmp_path):
    a = _init_repo(tmp_path / "a")
    _commit(a, "f.txt", "base\n", "base")
    b_bare = _bare(tmp_path / "b.git")
    _run(["remote", "add", "origin", str(b_bare)], cwd=a)
    _run(["push", "origin", "master"], cwd=a)
    b = tmp_path / "b"
    _run(["clone", str(b_bare), str(b)], cwd=tmp_path)
    _run(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=b)
    _run(["config", "user.name", "B"], cwd=b)
    _run(["config", "user.email", "b@example.com"], cwd=b)
    _commit(a, "f.txt", "from-a\n", "a change")
    _run(["push", "origin", "master"], cwd=a)
    _commit(b, "f.txt", "from-b\n", "b change")  # diverges
    _run(["fetch", "origin"], cwd=b)
    merge = subprocess.run(["git", "merge", "origin/master"], cwd=b, capture_output=True, text=True)
    assert merge.returncode != 0  # conflict, as intended

    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(b)), {"commit": True})
    events = agent_git_policy.after_turn(str(b), SESSION, OWNER, [str(b / "f.txt")], "turn")
    assert events == [{"action": "commit", "ok": False, "detail": "conflicts"}]


# ---------------------------------------------------------------------------
# after_turn: push (only after a successful commit)
# ---------------------------------------------------------------------------
def test_after_turn_push_only_fires_after_successful_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    bare = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(bare)], cwd=repo)

    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"push": True})  # commit is OFF
    _write(repo, "a.txt", "2\n")
    events = agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "turn")
    assert events == []  # nothing committed this call -> push never attempted
    assert git_panel.repo_status(str(repo))["unstaged"]


def test_after_turn_commits_and_pushes_to_bare_with_upstream(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    bare = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(bare)], cwd=repo)

    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"commit": True, "push": True})
    _write(repo, "a.txt", "2\n")
    events = agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "turn")
    assert [e["action"] for e in events] == ["commit", "push"]
    assert events[0]["ok"] is True and events[1]["ok"] is True

    local_sha = _run(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    bare_sha = _run(["rev-parse", "master"], cwd=bare).stdout.strip()
    assert local_sha == bare_sha
    upstream = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=repo).stdout.strip()
    assert upstream == "origin/master"


def test_after_turn_push_failure_reports_stderr_and_does_not_raise(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1\n", "initial")
    # A remote that isn't reachable at all: push must fail cleanly.
    _run(["remote", "add", "origin", str(tmp_path / "does-not-exist.git")], cwd=repo)

    agent_git_policy.set_repo_override(OWNER, git_panel.compute_repo_id(str(repo)),
                                       {"commit": True, "push": True})
    _write(repo, "a.txt", "2\n")
    events = agent_git_policy.after_turn(str(repo), SESSION, OWNER, [str(repo / "a.txt")], "turn")
    assert events[0]["ok"] is True
    assert events[1]["action"] == "push" and events[1]["ok"] is False
    assert events[1]["detail"]


# ---------------------------------------------------------------------------
# Global policy hook wiring (routes/chat_routes.py)
# ---------------------------------------------------------------------------
def test_chat_routes_wires_before_and_after_turn_hooks():
    import inspect
    import routes.chat_routes as chat_routes

    src = inspect.getsource(chat_routes)
    assert "agent_git_policy.before_turn(workspace, session, _user)" in src
    assert "agent_git_policy.after_turn(" in src
    assert src.count("'git_policy'") >= 2  # one SSE event site per hook
