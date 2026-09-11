"""Lote 88: git that CHANGES a repository never runs from bash -- it is
refused with the git_* tool to use instead (policy + panel). Reads pass."""
from __future__ import annotations

import pytest

from src.agent_tools.subprocess_tools import git_mutation_routed_to_tools


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


def test_reset_is_refused_without_a_tool_but_points_to_the_panel():
    out = git_mutation_routed_to_tools("git reset --hard")
    assert out is not None and out["use_instead"] is None
    assert "Source control panel" in out["error"]
