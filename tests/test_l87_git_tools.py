"""Lote 87 -- git tools for the agent (OBJ-4): src/agent_tools/git_tools.py.

Real `git` throughout (skipped cleanly when it isn't on PATH), real repos in
tmp_path, a real local bare remote for push/pull/fetch. Isolation pattern
copied from tests/test_l82_agent_git_policy.py: HOME/GIT_CONFIG repointed so
nothing here can read or write the machine's real ~/.gitconfig, and
DATA_DIR/settings.SETTINGS_FILE repointed so agent git policy reads/writes
land in a tmp file, never the real one.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_git_policy, constants as constants_mod, git_panel  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.agent_tools.git_tools import (  # noqa: E402
    GitBranchTool, GitCheckoutTool, GitCommitTool, GitDiffTool, GitFetchTool,
    GitLogTool, GitPullTool, GitPushTool, GitStatusTool,
)
from src.tool_approvals import ToolApprovalStore  # noqa: E402
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"
SESSION = "sess-l87"

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


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
    settings_mod._invalidate_caches()
    # Git tools are in NON_ADMIN_BLOCKED_TOOLS (same privilege class as
    # bash/read_file/write_file -- see tool_security.py), so execute_tool_block's
    # public-tool gate (_owner_is_admin) must pass before a git tool ever
    # dispatches. Pattern copied from tests/test_workspace_confine.py's `admin`
    # fixture and tests/test_edit_file.py: this suite is about the git tools'
    # own behaviour (workspace confinement, agent_git_policy gate, wiring), not
    # about the admin/public-user boundary, which is covered elsewhere.
    monkeypatch.setattr(te, "owner_is_admin_or_single_user", lambda owner: True)
    yield


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    _commit(r, "a.txt", "one\n", "Initial commit")
    return r


@pytest.fixture
def ws(repo):
    """Bind `repo` as the active turn workspace, the way execute_tool_block
    would -- for tests that call a Tool().execute() directly instead of
    going through the dispatcher."""
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


def _run_async(coro):
    return asyncio.run(coro)


def _repo_id(repo_path) -> str:
    return git_panel.compute_repo_id(str(repo_path))


def _set_policy(**patch):
    return agent_git_policy.set_global_policy(patch)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def test_git_status_reports_branch_and_last_commit(ws):
    result = _run_async(GitStatusTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert result["branch"] == "master"
    assert result["ahead"] == 0 and result["behind"] == 0
    assert len(result["commits"]) == 1
    assert result["commits"][0]["message"] == "Initial commit"


def test_git_status_shows_dirty_files(ws, repo):
    _write(repo, "b.txt", "new\n")
    result = _run_async(GitStatusTool().execute("{}", {"owner": OWNER}))
    assert result["untracked"] == [{"path": "b.txt"}]


def test_git_log_limit(ws, repo):
    _commit(repo, "a.txt", "two\n", "Second commit")
    result = _run_async(GitLogTool().execute(json.dumps({"limit": 1}), {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert len(result["commits"]) == 1
    assert result["commits"][0]["message"] == "Second commit"


def test_git_diff_working_tree_and_staged(ws, repo):
    _write(repo, "a.txt", "one\ntwo\n")
    unstaged = _run_async(GitDiffTool().execute("{}", {"owner": OWNER}))
    assert unstaged["exit_code"] == 0
    assert "two" in unstaged["diff"]
    assert unstaged["truncated"] is False

    _run(["add", "-A"], cwd=repo)
    staged = _run_async(GitDiffTool().execute(json.dumps({"staged": True}), {"owner": OWNER}))
    assert "two" in staged["diff"]

    still_unstaged = _run_async(GitDiffTool().execute("{}", {"owner": OWNER}))
    assert still_unstaged["diff"] == ""  # nothing left unstaged once everything is staged


def test_git_diff_of_a_commit(ws, repo):
    sha = _commit(repo, "a.txt", "one\ntwo\nthree\n", "Third commit")
    result = _run_async(GitDiffTool().execute(json.dumps({"commit": sha}), {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert "three" in result["diff"]


# ---------------------------------------------------------------------------
# Workspace confinement
# ---------------------------------------------------------------------------
def test_outside_workspace_is_refused(tmp_path, repo):
    other = tmp_path / "elsewhere"
    _init_repo(other)
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        result = _run_async(GitStatusTool().execute(json.dumps({"path": str(other)}), {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.outside_workspace"


def test_no_active_workspace_is_refused(repo):
    # No `ws` fixture here: _active_workspace / _active_workspace_roots are
    # unset (their ContextVar defaults), so a call with no `path` has
    # nothing to confine to.
    result = _run_async(GitStatusTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.outside_workspace"


def test_workspace_with_no_repo_is_not_a_repo(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    ws_token = te._active_workspace.set(str(plain))
    roots_token = te._active_workspace_roots.set((str(plain),))
    try:
        result = _run_async(GitStatusTool().execute("{}", {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.not_a_repo"


# ---------------------------------------------------------------------------
# Agent git policy gate: refuses without approval, allows with approval
# ---------------------------------------------------------------------------
def test_git_branch_refused_when_policy_denies(ws):
    # DEFAULT_POLICY.use_branch is False.
    result = _run_async(GitBranchTool().execute(json.dumps({"name": "feature/x"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["git_policy_field"] == "use_branch"
    assert "use_branch=false" in result["error"]


def test_git_branch_allowed_with_user_confirmed(ws):
    result = _run_async(GitBranchTool().execute(
        json.dumps({"name": "feature/x", "user_confirmed": True}), {"owner": OWNER}))
    assert result["exit_code"] == 0
    assert result["branch"] == "feature/x"


def test_git_branch_allowed_with_ctx_human_approved(ws):
    result = _run_async(GitBranchTool().execute(
        json.dumps({"name": "feature/y"}), {"owner": OWNER, "human_approved": True}))
    assert result["exit_code"] == 0


def test_git_branch_allowed_when_policy_enabled(ws):
    _set_policy(use_branch=True)
    result = _run_async(GitBranchTool().execute(json.dumps({"name": "feature/z"}), {"owner": OWNER}))
    assert result["exit_code"] == 0


def test_git_checkout_gated_the_same_way(ws, repo):
    _run(["branch", "other"], cwd=repo)
    denied = _run_async(GitCheckoutTool().execute(json.dumps({"branch": "other"}), {"owner": OWNER}))
    assert denied["exit_code"] == 1 and denied["git_policy_field"] == "use_branch"

    allowed = _run_async(GitCheckoutTool().execute(
        json.dumps({"branch": "other", "user_confirmed": True}), {"owner": OWNER}))
    assert allowed["exit_code"] == 0
    assert allowed["branch"] == "other"


def test_git_commit_requires_explicit_paths_never_dash_a(ws, repo):
    _set_policy(commit=True)
    _write(repo, "b.txt", "new\n")
    result = _run_async(GitCommitTool().execute(
        json.dumps({"message": "add b"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert "paths" in result["error"]


def test_git_commit_refused_when_policy_denies(ws, repo):
    _write(repo, "b.txt", "new\n")
    result = _run_async(GitCommitTool().execute(
        json.dumps({"message": "add b", "paths": ["b.txt"]}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["git_policy_field"] == "commit"
    # Refusing must not have staged or committed anything.
    status = git_panel.repo_status(str(repo))
    assert status["staged"] == [] and len(git_panel.log_commits(str(repo))["commits"]) == 1


def test_git_commit_allowed_with_approval_stages_exactly_named_paths(ws, repo):
    _write(repo, "b.txt", "new\n")
    _write(repo, "c.txt", "also new\n")
    result = _run_async(GitCommitTool().execute(
        json.dumps({"message": "add b only", "paths": ["b.txt"], "user_confirmed": True}),
        {"owner": OWNER},
    ))
    assert result["exit_code"] == 0
    assert result["paths"] == ["b.txt"]
    status = git_panel.repo_status(str(repo))
    # c.txt was never staged/committed -- no implicit -A.
    assert [u["path"] for u in status["untracked"]] == ["c.txt"]


def test_git_commit_no_identity_is_a_clean_409_style_error(tmp_path):
    r = tmp_path / "no_identity_repo"
    _init_repo(r, configure_identity=False)
    _write(r, "a.txt", "one\n")
    ws_token = te._active_workspace.set(str(r))
    roots_token = te._active_workspace_roots.set((str(r),))
    try:
        result = _run_async(GitCommitTool().execute(
            json.dumps({"message": "x", "paths": ["a.txt"], "user_confirmed": True}), {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.no_identity"


# ---------------------------------------------------------------------------
# Remote tools
# ---------------------------------------------------------------------------
def test_git_push_refused_when_policy_denies(tmp_path, repo):
    remote = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(remote)], cwd=repo)
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        result = _run_async(GitPushTool().execute("{}", {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 1 and result["git_policy_field"] == "push"


def test_git_push_allowed_with_approval_and_never_forces(tmp_path, repo):
    remote = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(remote)], cwd=repo)
    ws_token = te._active_workspace.set(str(repo))
    roots_token = te._active_workspace_roots.set((str(repo),))
    try:
        result = _run_async(GitPushTool().execute(
            json.dumps({"set_upstream": True, "user_confirmed": True}), {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 0
    # git_panel.push has no force flag at all -- nothing here could have
    # requested one, which is the acceptance criterion, not a mock check.
    remote_log = _run(["log", "--oneline", "master"], cwd=remote)
    assert "Initial commit" in remote_log.stdout


def test_git_pull_and_fetch_are_not_policy_gated(tmp_path, repo):
    remote = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(remote)], cwd=repo)
    _run(["push", "-u", "origin", "master"], cwd=repo)

    clone = tmp_path / "clone"
    _run(["clone", str(remote), str(clone)], cwd=tmp_path)
    _run(["config", "user.name", "Repo User"], cwd=clone)
    _run(["config", "user.email", "repo-user@example.com"], cwd=clone)

    # A second commit lands on the original clone and is pushed upstream.
    _commit(repo, "a.txt", "two\n", "Second commit")
    _run(["push", "origin", "master"], cwd=repo)

    ws_token = te._active_workspace.set(str(clone))
    roots_token = te._active_workspace_roots.set((str(clone),))
    try:
        fetched = _run_async(GitFetchTool().execute("{}", {"owner": OWNER}))
        assert fetched["exit_code"] == 0
        pulled = _run_async(GitPullTool().execute("{}", {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert pulled["exit_code"] == 0
    assert (clone / "a.txt").read_text() == "two\n"


def test_git_pull_diverged_is_a_clean_error(tmp_path, repo):
    remote = _bare(tmp_path / "remote.git")
    _run(["remote", "add", "origin", str(remote)], cwd=repo)
    _run(["push", "-u", "origin", "master"], cwd=repo)

    clone = tmp_path / "clone"
    _run(["clone", str(remote), str(clone)], cwd=tmp_path)
    _run(["config", "user.name", "Repo User"], cwd=clone)
    _run(["config", "user.email", "repo-user@example.com"], cwd=clone)

    _commit(repo, "a.txt", "from origin\n", "Origin-side commit")
    _run(["push", "origin", "master"], cwd=repo)
    _commit(clone, "a.txt", "from clone\n", "Clone-side commit")

    ws_token = te._active_workspace.set(str(clone))
    roots_token = te._active_workspace_roots.set((str(clone),))
    try:
        result = _run_async(GitPullTool().execute("{}", {"owner": OWNER}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.diverged"


# ---------------------------------------------------------------------------
# End-to-end: the sealed approval card (tool_approvals.py) unlocks a
# policy-denied call through execute_tool_block, exactly like every other
# approval-gated tool -- this is what ctx["human_approved"] reflects.
# ---------------------------------------------------------------------------
def test_execute_tool_block_wires_a_sealed_approval_into_human_approved(repo):
    content = json.dumps({"message": "sealed commit", "paths": ["a.txt"]})
    _write(repo, "a.txt", "changed\n")

    store = ToolApprovalStore()
    pending = store.create(
        owner=OWNER, session_id=SESSION, origin_run_id="run-1",
        tool_name="git_commit", content=content, workspace=str(repo),
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("git_commit", content),
    )
    grant = store.consume(pending.approval_id, decision="approve_task", owner=OWNER, session_id=SESSION)
    assert grant is not None

    desc, result = _run_async(te.execute_tool_block(
        ToolBlock("git_commit", content),
        session_id=SESSION, owner=OWNER, workspace=str(repo),
        security_context=ToolRunSecurityContext(external_untrusted_context_seen=True),
        exact_approval=grant,
    ))
    # policy.commit is still False (never changed in this test) -- only the
    # sealed, human-answered approval card unlocks it.
    assert result["exit_code"] == 0, result
    assert result["sha"]


def test_execute_tool_block_without_approval_still_refuses_by_policy(repo):
    content = json.dumps({"message": "unsealed commit", "paths": ["a.txt"]})
    _write(repo, "a.txt", "changed again\n")
    desc, result = _run_async(te.execute_tool_block(
        ToolBlock("git_commit", content),
        session_id=SESSION, owner=OWNER, workspace=str(repo),
        security_context=ToolRunSecurityContext(),
    ))
    assert result["exit_code"] == 1
    assert result.get("git_policy_field") == "commit"


# ---------------------------------------------------------------------------
# Registration coherence -- the exact gap test_l54_tool_wiring.py already
# guards for lote 54's own tools, checked here for the nine git tools.
# ---------------------------------------------------------------------------
GIT_TOOL_NAMES = (
    "git_status", "git_log", "git_diff", "git_branch", "git_checkout",
    "git_commit", "git_push", "git_pull", "git_fetch",
)


@pytest.mark.parametrize("name", GIT_TOOL_NAMES)
def test_git_tool_reaches_every_registry(name):
    import src.agent_tools as agent_tools_mod
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_registry import snapshot
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    native_names = {(e.get("function") or {}).get("name") for e in FUNCTION_TOOL_SCHEMAS}
    assert name in agent_tools_mod.TOOL_TAGS, f"{name} missing from TOOL_TAGS"
    assert name in agent_tools_mod.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"
    assert name in native_names, f"{name} missing from FUNCTION_TOOL_SCHEMAS"
    assert name in KNOWN_CAPABILITY_TOOLS, f"{name} missing an explicit tool_capabilities entry"
    assert name in BUILTIN_TOOL_DESCRIPTIONS, f"{name} missing a tool_index description"
    assert name in {d.name for d in snapshot()}, f"{name} missing from ToolRegistry.snapshot()"


def test_git_write_and_remote_tools_carry_the_effects_the_contract_asked_for():
    from src.tool_capabilities import ToolEffect, capabilities_for_tool

    reads = {"git_status", "git_log", "git_diff"}
    for name in reads:
        assert ToolEffect.READ_WORKSPACE in capabilities_for_tool(name).effects

    writes = {"git_branch", "git_checkout", "git_commit"}
    for name in writes:
        assert ToolEffect.WRITE_WORKSPACE in capabilities_for_tool(name).effects

    for name in ("git_push", "git_pull", "git_fetch"):
        assert ToolEffect.NETWORK_EGRESS in capabilities_for_tool(name).effects
