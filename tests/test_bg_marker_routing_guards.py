"""A `#!bg` job goes through the same routing guards as the shell tools: a
repository mutation goes to the git tools, and on Windows a PowerShell or
cmd launch typed into bash goes to the powershell tool."""
from collections import namedtuple

import pytest

from src.agent_tools import subprocess_tools
from src.tool_execution import _execute_tool_block_impl

Block = namedtuple("ToolBlock", ["tool_type", "content"])


@pytest.fixture
def launched(monkeypatch):
    # The owner is an admin here, wherever the suite runs (a test machine
    # without a user database would otherwise refuse the shell tools).
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    seen = []

    def _launch(command, session_id, cwd=None, max_runtime_s=3600, shell="bash"):
        seen.append((command, shell))
        return {"id": "job-1"}
    monkeypatch.setattr("src.bg_jobs.launch", _launch)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["bash", "powershell"])
async def test_a_background_git_push_goes_to_the_git_tools(launched, tool):
    desc, result = await _execute_tool_block_impl(Block(tool, "#!bg\ngit push origin main"),
                                                  session_id="s1", owner="admin")
    assert launched == []
    assert result["exit_code"] != 0 and result.get("error")
    assert result["error"].startswith(f"{tool}:")


@pytest.mark.asyncio
async def test_powershell_typed_into_background_bash_goes_to_the_powershell_tool(launched, monkeypatch):
    monkeypatch.setattr(subprocess_tools, "IS_WINDOWS", True)
    desc, result = await _execute_tool_block_impl(
        Block("bash", "#!bg\npowershell -NoProfile -File install.ps1"), session_id="s1", owner="admin")
    assert launched == []
    assert result.get("use_instead") == "powershell" and "#!bg" in result["error"]


@pytest.mark.asyncio
async def test_an_ordinary_background_job_still_starts(launched):
    desc, result = await _execute_tool_block_impl(Block("bash", "#!bg\npip download requests"),
                                                  session_id="s1", owner="admin")
    assert launched == [("pip download requests", "bash")] and result["bg_job_id"] == "job-1"
