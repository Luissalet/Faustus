"""Timeout evidence survives command producers into the normalized effect result."""
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import sandbox_exec, sandbox_provider
from src.agent_tools import subprocess_tools as tools
from src.tool_result import normalize_tool_result


@pytest.fixture
def environment(monkeypatch, tmp_path):
    settings = {"agent_sandbox_execution": False}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(tools, "find_bash", lambda: "fake-bash")
    monkeypatch.setattr(tools, "find_powershell", lambda: "fake-powershell")
    proc = SimpleNamespace(stdin=SimpleNamespace(write=lambda x: None, drain=AsyncMock(), close=lambda: None))
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(tools, "_create_bash_subprocess", AsyncMock(return_value=proc))
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_cls", [tools.BashTool, tools.PythonTool, tools.PowerShellTool])
@pytest.mark.parametrize("timeout", [True, "idle", False])
async def test_host_timeout_is_unknown_but_user_exit124_is_not(environment, monkeypatch, tool_cls, timeout):
    monkeypatch.setattr(tools, "_run_subprocess_streaming", AsyncMock(
        return_value=("partial effect output", "", 124, timeout)))
    result = await tool_cls().execute("echo example", {})
    assert result["exit_code"] == 124
    normalized = normalize_tool_result(result)
    if timeout:
        assert result["timed_out"] is True
        assert normalized.status == "outcome_unknown"
        assert normalized.uncertainty.reconcile_action == "read_current_state_before_retry"
    else:
        assert "timed_out" not in result
        assert normalized.status == "failed"


@pytest.mark.asyncio
async def test_tmux_timeout_propagates(environment, monkeypatch):
    monkeypatch.setattr(tools, "IS_WINDOWS", False)
    monkeypatch.setattr(tools.shutil, "which", lambda command: "/bin/tmux")
    monkeypatch.setattr(tools, "_run_tmux_bash", AsyncMock(return_value=("partial", "", 124, True)))
    result = await tools.BashTool().execute("echo example", {"session_id": "test"})
    assert result["timed_out"] is True
    assert normalize_tool_result(result).status == "outcome_unknown"


@pytest.mark.asyncio
async def test_persistent_sandbox_timeout_reaches_normalization(environment, monkeypatch):
    environment.update({"agent_sandbox_execution": True, "agent_sandbox_persistent_session": True})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    instance = sandbox_provider.DockerSandboxProvider(image="test")
    monkeypatch.setattr(sandbox_provider, "get_provider", lambda **kwargs: instance)
    monkeypatch.setattr(instance, "probe", lambda: sandbox_provider.Availability(True))
    monkeypatch.setattr(instance, "status", lambda session: "exists")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker exec", 1)
    monkeypatch.setattr(instance, "_run_docker", timeout)
    result = await tools.PythonTool().execute("example", {"session_id": "test"})
    assert result["timed_out"] is True
    assert normalize_tool_result(result).status == "outcome_unknown"
