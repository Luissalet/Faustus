"""Full access runs local Git through the selected native shell."""
import subprocess

import pytest

from src.agent_tools import subprocess_tools as tools


@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_other_modes_keep_the_selected_git_workflow(monkeypatch, mode):
    monkeypatch.setattr("src.tool_capabilities.tool_approval_mode", lambda: mode)
    result = tools.git_mutation_routed_to_tools("git merge --ff-only feature")
    assert result["use_instead"] == "git_merge"


@pytest.mark.parametrize("command", [
    "git init", "git add note.txt", "git commit -m 'Record note'",
    "git merge --ff-only feature", "git config user.name Luissalet",
])
def test_full_does_not_impose_another_tool(monkeypatch, command):
    monkeypatch.setattr("src.tool_capabilities.tool_approval_mode", lambda: "full")
    assert tools.git_mutation_routed_to_tools(command) is None


@pytest.mark.asyncio
async def test_full_bash_commits_exact_named_file_in_private_repository(monkeypatch, tmp_path):
    from src import tool_execution, sandbox_exec

    monkeypatch.setattr("src.tool_capabilities.tool_approval_mode", lambda: "full")
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tmp_path))
    async def no_sandbox(*args, **kwargs):
        return None
    monkeypatch.setattr(sandbox_exec, "run", no_sandbox)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "note.txt").write_text("Owned QA file\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("Must remain untracked\n", encoding="utf-8")
    result = await tools.BashTool().execute(
        "git add note.txt && git -c user.name=Luissalet "
        "-c user.email=luissalet@users.noreply.github.com commit -m 'Record note'", {})
    assert result.get("exit_code") == 0, result
    tracked = subprocess.check_output(["git", "-C", str(tmp_path), "ls-files"], text=True)
    assert tracked.splitlines() == ["note.txt"]
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "Must remain untracked\n"


@pytest.mark.asyncio
async def test_full_powershell_writes_local_git_setting(monkeypatch, tmp_path):
    from src import tool_execution

    if not tools.find_powershell():
        pytest.skip("PowerShell is not installed on this host")
    monkeypatch.setattr("src.tool_capabilities.tool_approval_mode", lambda: "full")
    monkeypatch.setattr(tool_execution, "agent_cwd", lambda: str(tmp_path))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    result = await tools.PowerShellTool().execute("git config --local qa292.marker powershell", {})
    assert result.get("exit_code") == 0, result
    marker = subprocess.check_output(
        ["git", "-C", str(tmp_path), "config", "--get", "qa292.marker"], text=True)
    assert marker.strip() == "powershell"
