"""Explicit required mode never promotes a command to host compatibility."""
import pytest

from src import sandbox_exec as sandbox, sandbox_provider as provider
from src.agent_tools import subprocess_tools as tools
from src.execution_backends import DockerWorkspaceBackend


@pytest.fixture
def configured(monkeypatch, tmp_path):
    values = {"agent_sandbox_execution": True, "agent_sandbox_mode": "required"}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,command", [
    (tools.BashTool, "echo unsafe > required-sentinel.txt"),
    (tools.PythonTool, "open('required-sentinel.txt', 'w').write('unsafe')"),
    (tools.PowerShellTool, "Set-Content required-sentinel.txt unsafe"),
])
async def test_required_windows_without_a_container_refuses_before_process_creation(
        configured, monkeypatch, tmp_path, tool, command):
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: True)
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self:
                        {"ok": False, "reason": "backend_unavailable", "detail": "daemon down"})
    async def forbidden(*args, **kwargs):
        pytest.fail("required must not create a host process")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(tools, "_create_bash_subprocess", forbidden)
    result = await tool().execute(command, {})
    assert result["requested_policy"] == "sandbox_required"
    assert result["effective_policy"] == "not_executed"
    assert result["sandbox_refused"] and result["exit_code"] == 126
    assert "required" in result["error"] or "unavailable" in result["error"]
    assert not (tmp_path / "required-sentinel.txt").exists()


@pytest.mark.asyncio
async def test_required_windows_powershell_is_refused_even_with_a_working_container(
        configured, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: True)
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self:
                        {"ok": True, "reason": "", "detail": "fake"})
    result = await tools.PowerShellTool().execute("Set-Content required-sentinel.txt unsafe", {})
    assert result["effective_policy"] == "not_executed" and result["sandbox_refused"]
    assert not (tmp_path / "required-sentinel.txt").exists()


@pytest.mark.asyncio
async def test_required_windows_bash_uses_the_container_with_windows_workspace_paths(
        configured, monkeypatch, tmp_path):
    from src.contracts import ExecutionResult
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: True)
    workspace = "C:\\Users\\someone\\proj"
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: workspace)
    monkeypatch.setattr(sandbox.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self:
                        {"ok": True, "reason": "", "detail": "fake"})
    seen = {}

    def fake_run(self, spec, argv, **kwargs):
        seen["argv"] = argv
        seen["docker_args"] = self.docker_args(spec, "n")
        return ExecutionResult.parse({"run_id": "r", "backend": "docker_workspace",
                                      "status": "completed", "exit_code": 0,
                                      "stdout_tail": "/workspace/out.txt", "stderr_tail": "",
                                      "started_at": "2026-01-01T00:00:00Z",
                                      "ended_at": "2026-01-01T00:00:01Z"})
    monkeypatch.setattr(DockerWorkspaceBackend, "run", fake_run)
    result = await tools.BashTool().execute("cat C:\\Users\\someone\\proj\\out.txt", {})
    assert result["effective_policy"] == "docker_container"
    assert seen["argv"][-1] == "cat /workspace/out.txt"
    assert "type=bind,source=C:/Users/someone/proj,target=/workspace" in seen["docker_args"]
    assert result["output"] == "C:\\Users\\someone\\proj/out.txt"


@pytest.mark.asyncio
async def test_required_missing_daemon_refuses_without_effect(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: False)
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self:
                        {"ok": False, "reason": "daemon", "detail": "not available"})
    result = await tools.PythonTool().execute("open('required-sentinel.txt','w').write('unsafe')", {})
    assert result["effective_policy"] == "not_executed"
    assert not (tmp_path / "required-sentinel.txt").exists()


@pytest.mark.asyncio
async def test_required_policy_remains_frozen_across_async_probe(configured, monkeypatch, tmp_path):
    configured["agent_sandbox_persistent_session"] = True
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: False)
    class Missing:
        def probe(self):
            configured["agent_sandbox_mode"] = "auto"
            configured["agent_sandbox_execution"] = False
            return provider.Availability(False, "daemon unavailable", "daemon")
    monkeypatch.setattr(provider, "get_provider", lambda **kwargs: Missing())
    result = await tools.PythonTool().execute("open('required-sentinel.txt','w').write('unsafe')",
                                              {"session_id": "test"})
    assert result["requested_policy"] == "sandbox_required"
    assert result["effective_policy"] == "not_executed"
    assert "`required`" in result["error"]
    assert not (tmp_path / "required-sentinel.txt").exists()
    # The snapshot ends with this dispatch; the user's new preferences remain.
    assert sandbox.enabled() is False and sandbox.mode() == "auto"


@pytest.mark.asyncio
async def test_required_uses_available_provider_without_host(configured, monkeypatch):
    configured["agent_sandbox_persistent_session"] = True
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: False)
    calls = []
    class Available:
        def probe(self):
            return provider.Availability(True)
        def status(self, session):
            return "exists"
        def exec(self, session, argv, **kwargs):
            calls.append(argv)
            return {"executed": True, "exit_code": 0, "stdout": "provider result", "stderr": ""}
    monkeypatch.setattr(provider, "get_provider", lambda **kwargs: Available())
    result = await tools.PythonTool().execute("example", {"session_id": "test"})
    assert len(calls) == 1 and result["output"] == "provider result"
    assert result["requested_policy"] == "sandbox_required"
    assert result["effective_policy"] == "docker_container"
    assert result["fallback_reason"] == ""


def test_required_is_exposed_without_changing_default(configured):
    from src.agent_settings_schema import coerce_setting_value
    from src.settings import DEFAULT_SETTINGS
    assert sandbox.mode() == "required"
    assert coerce_setting_value("agent_sandbox_mode", "required") == "required"
    assert DEFAULT_SETTINGS["agent_sandbox_mode"] == "auto"
    configured["agent_sandbox_execution"] = False
    assert sandbox.confinement_required() is False


def test_windows_prompt_and_doctor_do_not_claim_host_execution(configured, monkeypatch):
    from src import agent_loop, doctor
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: True)
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True)
    assert sandbox.describe()["target"] == "container"
    prompt = agent_loop._execution_environment_block({"bash", "python", "powershell"})
    assert "refused" in prompt and "/workspace" in prompt
    assert "every command runs directly" not in prompt
    assert doctor._agent_sandbox().state == "ok"
