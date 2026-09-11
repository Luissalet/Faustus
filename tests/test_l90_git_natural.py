"""Lote 90 -- natural language for git (OBJ-4).

Luis's own framing: "que no tenga que ser todo tan explicito ... un usuario
no deberia saberse de memoria todas las tools de Faustus." Three things this
covers:

1. `src/agent_tools/git_tools.py` resolves a repo without an explicit `path`:
   `repo` (by name, exact or a unique case-insensitive prefix) among the
   repos linked to the session's PROJECT, then the repo containing the
   active workspace, then the project's only repo, then `error_class`
   `git.which_repo` (several repos, nothing picked one) so the model asks.
2. `src/agent_loop.py::_project_repos_block` -- the "Repositories in this
   project" system-prompt section that tells the model what "the repo"
   means, present only when the project has repos.
3. The `_git_intent` regex recognizes more Spanish synonyms (mergea,
   fusiona, sube, sincroniza, ...).

Fixture pattern copied from `tests/test_l87_git_tools.py` (isolated HOME/git
config, isolated DATA_DIR/settings) and `tests/test_l89_git_merge.py`
(isolated `services.projects.ProjectStore` so discovery reads a throwaway
`projects.json`, never the real one).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as projects_mod  # noqa: E402
from src import agent_loop  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src import git_panel  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.agent_tools.git_tools import GitStatusTool  # noqa: E402

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


def _init_repo(path, branch="master"):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], cwd=path)
    _run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=path)
    _run(["config", "user.name", "Repo User"], cwd=path)
    _run(["config", "user.email", "repo-user@example.com"], cwd=path)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _run(["add", "-A"], cwd=path)
    _run(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
          "commit", "-m", "Initial commit"], cwd=path)
    return path


def _run_async(coro):
    return asyncio.run(coro)


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
    monkeypatch.setattr(te, "owner_is_admin_or_single_user", lambda owner: True)
    git_panel.invalidate_discovery_cache()
    agent_loop._REPOS_BLOCK_CACHE.clear()
    yield
    git_panel.invalidate_discovery_cache()
    agent_loop._REPOS_BLOCK_CACHE.clear()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


def _project(store, workspace, *, owner=OWNER, name="Proj"):
    return store.create(name=name, folder=name, workspace=str(workspace), owner=owner)


# ---------------------------------------------------------------------------
# 1a. Resolution by `repo` name -- exact and unique prefix.
# ---------------------------------------------------------------------------
def test_resolves_repo_by_exact_name(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    alpha = _init_repo(root / "alpha")
    _init_repo(root / "beta")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        json.dumps({"repo": "alpha"}), {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(alpha))


def test_resolves_repo_by_unique_prefix_case_insensitive(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    alpha = _init_repo(root / "alpha-service")
    _init_repo(root / "beta-service")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        json.dumps({"repo": "ALPHA"}), {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(alpha))


def test_ambiguous_repo_prefix_is_which_repo(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root / "svc-a")
    _init_repo(root / "svc-b")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        json.dumps({"repo": "svc"}), {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.which_repo"
    assert set(result["repos"]) == {"svc-a", "svc-b"}


def test_unknown_repo_name_is_repo_not_found(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root / "alpha")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        json.dumps({"repo": "nope"}), {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.repo_not_found"
    assert result["repos"] == ["alpha"]


# ---------------------------------------------------------------------------
# 1b. Resolution by the active workspace -- unchanged fallback, still wins
# over "the project has several repos" when the workspace itself is inside
# one of them.
# ---------------------------------------------------------------------------
def test_active_workspace_wins_when_no_repo_name_given(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    alpha = _init_repo(root / "alpha")
    _init_repo(root / "beta")
    project = _project(store, root)

    ws_token = te._active_workspace.set(str(alpha))
    roots_token = te._active_workspace_roots.set((str(alpha),))
    try:
        result = _run_async(GitStatusTool().execute(
            "{}", {"owner": OWNER, "project_id": project["id"]},
        ))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(alpha))


def test_explicit_repo_name_beats_a_different_active_workspace(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    alpha = _init_repo(root / "alpha")
    beta = _init_repo(root / "beta")
    project = _project(store, root)

    # Active workspace is inside beta, but the call names alpha -- alpha wins.
    ws_token = te._active_workspace.set(str(beta))
    roots_token = te._active_workspace_roots.set((str(beta),))
    try:
        result = _run_async(GitStatusTool().execute(
            json.dumps({"repo": "alpha"}), {"owner": OWNER, "project_id": project["id"]},
        ))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(alpha))


# ---------------------------------------------------------------------------
# 1c. Single-repo project -- resolves with no path, no repo, no workspace.
# ---------------------------------------------------------------------------
def test_single_repo_project_resolves_with_nothing_given(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    only = _init_repo(root / "only")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        "{}", {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(only))


# ---------------------------------------------------------------------------
# 1d. Several repos, nothing picks one -> git.which_repo (ask_user territory).
# ---------------------------------------------------------------------------
def test_multiple_repos_nothing_given_is_which_repo(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root / "alpha")
    _init_repo(root / "beta")
    project = _project(store, root)

    result = _run_async(GitStatusTool().execute(
        "{}", {"owner": OWNER, "project_id": project["id"]},
    ))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.which_repo"
    assert set(result["repos"]) == {"alpha", "beta"}


def test_backward_compatible_no_project_no_workspace_is_outside_workspace(tmp_path):
    # No project_id in ctx, no active workspace -- same refusal L87 already
    # tests (`test_no_active_workspace_is_refused`), proving the new
    # resolution order didn't change the pre-Lote-90 no-signal-at-all case.
    result = _run_async(GitStatusTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.outside_workspace"


# ---------------------------------------------------------------------------
# 1e. End-to-end through execute_tool_block: proves ctx["project_id"] really
# arrives at the git tool from turn_options["harness_options"]["project_id"]
# the way services/projects.py::agent_options + routes/chat_routes.py wire
# it for a real turn -- not just a hand-built ctx in the unit tests above.
# ---------------------------------------------------------------------------
def test_execute_tool_block_supplies_project_id_from_harness_options(tmp_path, store):
    from src.tool_capabilities import ToolRunSecurityContext

    root = tmp_path / "ws"
    root.mkdir()
    alpha = _init_repo(root / "alpha")
    _init_repo(root / "beta")
    project = _project(store, root)

    desc, result = _run_async(te.execute_tool_block(
        ToolBlock("git_status", json.dumps({"repo": "alpha"})),
        session_id="sess-l90", owner=OWNER,
        security_context=ToolRunSecurityContext(),
        turn_options={"harness_options": {"project_id": project["id"]}},
    ))
    assert result["exit_code"] == 0, result
    assert result["repo_root"] == os.path.realpath(str(alpha))


# ---------------------------------------------------------------------------
# 2. "Repositories in this project" system-prompt block.
# ---------------------------------------------------------------------------
def test_repos_block_lists_repos_with_branch_and_dirty_count(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root / "alpha")
    beta = _init_repo(root / "beta")
    (beta / "dirty.txt").write_text("x\n", encoding="utf-8")
    project = _project(store, root)

    block = agent_loop._project_repos_block(OWNER, project["id"])
    assert "## Repositories in this project" in block
    assert "- alpha: master" in block
    assert "- beta: master" in block
    assert "1d" in block  # beta has one dirty (untracked) file


def test_repos_block_empty_without_project_or_without_repos(tmp_path, store):
    # No project at all.
    assert agent_loop._project_repos_block(OWNER, "") == ""
    assert agent_loop._project_repos_block("", "some-project") == ""

    # A real project with no repos under its workspace.
    empty_ws = tmp_path / "empty_ws"
    empty_ws.mkdir()
    project = _project(store, empty_ws, name="EmptyProj")
    assert agent_loop._project_repos_block(OWNER, project["id"]) == ""


def test_repos_block_is_cached(tmp_path, store, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    _init_repo(root / "alpha")
    project = _project(store, root)

    first = agent_loop._project_repos_block(OWNER, project["id"])
    assert "alpha" in first

    # Make discovery explode; a cached call must not need it again within TTL.
    def _boom(*a, **kw):
        raise AssertionError("discover_repos_for_owner should not run on a cache hit")
    monkeypatch.setattr(git_panel, "discover_repos_for_owner", _boom)
    second = agent_loop._project_repos_block(OWNER, project["id"])
    assert second == first


def test_repos_block_capped_at_twelve_repos_and_under_400_chars(tmp_path, store):
    root = tmp_path / "ws"
    root.mkdir()
    for i in range(15):
        _init_repo(root / f"repo{i:02d}")
    project = _project(store, root)

    block = agent_loop._project_repos_block(OWNER, project["id"])
    assert len(block) <= agent_loop._REPOS_BLOCK_MAX_CHARS
    # Discovery itself found all 15; the block only ever renders the capped
    # 12-repo slice it fetched, so at most 12 "- name:" lines can appear.
    assert block.count("\n- ") <= agent_loop._REPOS_BLOCK_MAX_REPOS


# ---------------------------------------------------------------------------
# 3. Agent rules mention the natural-language resolution.
# ---------------------------------------------------------------------------
def test_agent_rules_mention_repo_resolution_without_path():
    for rules in (agent_loop._AGENT_RULES, agent_loop._API_AGENT_RULES):
        assert "git_*" in rules and "repo" in rules and "which one" in rules


# ---------------------------------------------------------------------------
# 4. `_git_intent` regex recognizes the new Spanish synonyms.
# ---------------------------------------------------------------------------
def _git_intent_pattern():
    src = open(agent_loop.__file__, encoding="utf-8").read()
    start = src.index("Tools the user NAMED are offered")
    block = src[start:start + 3500]
    m = re.search(r're\.search\(\s*r"(.+?)"\s*r"(.+?)",', block, re.S)
    assert m, "intent regex not found"
    return re.compile(m.group(1) + m.group(2), re.IGNORECASE)


@pytest.mark.parametrize("text", [
    "mergea la rama pruebas en main",
    "puedes mergear esto",
    "fusiona develop con main",
    "sube los cambios",
    "sincroniza el repo",
    "commitea lo que has cambiado",
    "haz un pull request",
])
def test_git_intent_regex_recognizes_new_synonyms(text):
    rx = _git_intent_pattern()
    assert rx.search(text), text


def test_git_intent_regex_still_rejects_unrelated_text():
    rx = _git_intent_pattern()
    assert not rx.search("resume este documento")
