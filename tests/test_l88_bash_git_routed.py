"""Lote 88: git that CHANGES a repository never runs from bash -- it is
refused with the git_* tool to use instead (policy + panel). Reads pass."""
from __future__ import annotations

import pytest

from src.agent_tools.subprocess_tools import git_mutation_routed_to_tools


@pytest.fixture(autouse=True)
def _ask_mode(monkeypatch):
    """This routing is the ask/auto contract. Full access, the default since 15-10,
    runs git in the shell on purpose (316cd3ec; see test_full_git_shell)."""
    monkeypatch.setattr("src.tool_capabilities.tool_approval_mode", lambda: "ask")


@pytest.mark.parametrize("cmd,sub,tool", [
    ("git push origin main", "push", "git_push"),
    ("cd repo && git commit -m 'x'", "commit", "git_commit"),
    ("git -C D:/LocalAI/faustus-panel-test checkout -b feat", "checkout", "git_checkout"),
    ("ls; git branch nueva", "branch", "git_branch"),
    ("sudo git pull", "pull", "git_pull"),
    ("git add -A", "add", "git_commit"),
])
def test_mutations_are_routed(cmd, sub, tool):
    out = git_mutation_routed_to_tools(cmd)
    assert out is not None and out["exit_code"] == 2
    assert out["git_subcommand"] == sub
    assert out["use_instead"] == tool
    assert tool in out["error"]


@pytest.mark.parametrize("cmd", [
    "git status -sb",
    "git -C D:/x log --oneline -5",
    "git diff HEAD~1",
    "git show abc123",
    "echo committed && ls",       # the word, not the command
    "python -c 'print(1)'",
    "digit add",                  # not `git` at a command boundary
])
def test_reads_and_unrelated_commands_pass(cmd):
    assert git_mutation_routed_to_tools(cmd) is None


def test_reset_has_no_tool_and_runs_through_the_shell_gate():
    # Since d3475e1c, git subcommands without a git_* tool (reset, clone, stash,
    # tag, clean...) run in the shell under its normal approval gate.
    assert git_mutation_routed_to_tools("git reset --hard") is None


def test_git_config_write_cannot_bypass_the_git_tools():
    out = git_mutation_routed_to_tools('git config remote.origin.url "owner:repo.git"')
    assert out is not None
    assert out["git_subcommand"] == "config"


def test_git_config_reads_still_work_in_shell():
    assert git_mutation_routed_to_tools("git config --get remote.origin.url") is None
    assert git_mutation_routed_to_tools("git config user.name") is None
