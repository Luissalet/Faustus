"""Lot A wiring gap: `github_issue`/`git_open_pr` (src/agent_tools/git_tools.py,
src/github_pr.py) are fully functional today via `dynamic_handlers` (see
`src/tool_execution.py`'s `elif tool in dynamic_handlers:` branch, which
already passes `owner` -- just not the sealed-approval `human_approved` flag
`_GIT_TOOL_NAMES` tools get) -- nothing here BLOCKS the agent by default, the
`user_confirmed: true` retry path always works. This test only tracks the
integrator-owned wiring described in A_wiring.md that would let a git_open_pr
call ride an already-sealed approval card the same way git_push does, and let
the "git tools offered" floor in `agent_loop.py` add both new tools to a turn
whose intent mentions issues/pull requests.

Marked xfail(strict=True): flip to a real assertion (or delete) once
`_GIT_TOOL_NAMES` in `src/tool_execution.py` includes `"github_issue"` and
`"git_open_pr"`, per A_wiring.md.
"""
from __future__ import annotations

import pytest


def test_git_tool_names_includes_github_pr_tools():
    from src.tool_execution import _GIT_TOOL_NAMES

    assert "github_issue" in _GIT_TOOL_NAMES
    assert "git_open_pr" in _GIT_TOOL_NAMES
