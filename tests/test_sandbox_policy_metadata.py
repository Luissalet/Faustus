"""H02: results report requested versus effective policy without new authority."""
import asyncio
import contextvars
import subprocess

import pytest

from src import sandbox_exec as sandbox
from src import sandbox_provider as provider
from src.agent_tools import subprocess_tools as tools
from src.execution_backends import DockerWorkspaceBackend


@pytest.fixture
def settings(monkeypatch, tmp_path):
    values = {"agent_sandbox_execution": True, "agent_sandbox_mode": "auto"}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "strict"])
async def test_windows_host_consumer_receives_requested_and_effective_policy(settings, monkeypatch, mode):
    settings["agent_sandbox_mode"] = mode
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: True)
    result = await tools.PythonTool().execute("print('policy-result')", {})
    assert result["output"] == "policy-result"
    assert result["requested_policy"] == "sandbox_" + mode
    assert result["effective_policy"] == "host"
    assert "Windows" in result["fallback_reason"]
    assert result["execution_target"]["kind"] != "container"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "strict"])
async def test_unavailable_docker_reports_host_or_refusal(settings, monkeypatch, mode):
    settings["agent_sandbox_mode"] = mode
    monkeypatch.setattr(sandbox, "_host_is_windows", lambda: False)
    monkeypatch.setattr(DockerWorkspaceBackend, "probe", lambda self: {
        "ok": False, "reason": "daemon", "detail": "not responding"})
    result = await tools.PythonTool().execute("print('host-fallback')", {})
    assert result["requested_policy"] == "sandbox_" + mode
    if mode == "auto":
        assert result["effective_policy"] == "host"
        assert "daemon" in result["fallback_reason"]
        assert result["output"] == "host-fallback"
    else:
        assert result["effective_policy"] == "not_executed"
        assert result["sandbox_refused"] and result["exit_code"] == 126
        assert result["execution_target"]["kind"] == "not_executed"
        assert result["fallback_reason"] == ""


@pytest.mark.asyncio
async def test_requested_policy_is_task_local_snapshot(monkeypatch):
    mode = contextvars.ContextVar("test_mode")
    monkeypatch.setattr(sandbox, "mode", lambda: mode.get())
    monkeypatch.setattr(sandbox, "enabled", lambda: True)
    async def dispatch(tool, command, ctx):
        sandbox._note_skip(tool, command)
        await asyncio.sleep(0)
        return None
    monkeypatch.setattr(sandbox, "_run", dispatch)
    async def call(requested, reason):
        mode.set(requested)
        await sandbox.run("python", reason, {})
        skip = sandbox.consume_skip_reason()
        mode.set("changed-after-dispatch")
        await asyncio.sleep(0)
        return tools._mark_sandbox_skip({}, skip)
    first, second = await asyncio.gather(call("auto", "first"), call("strict", "second"))
    assert (first["requested_policy"], first["fallback_reason"]) == ("sandbox_auto", "first")
    assert (second["requested_policy"], second["fallback_reason"]) == ("sandbox_strict", "second")


@pytest.mark.asyncio
async def test_off_preserves_result_shape_and_clears_stale_skip(settings, monkeypatch):
    sandbox._note_skip("python", "stale")
    settings["agent_sandbox_execution"] = False
    assert await sandbox.run("python", "print('x')", {}) is None
    assert sandbox.consume_skip_reason() == ""
    assert tools._mark_sandbox_skip({"output": "x"}, "") == {"output": "x"}


@pytest.mark.parametrize("rc,effective", [(0, "docker_container"), (126, "not_executed")])
def test_provider_result_preserves_its_effective_environment(monkeypatch, rc, effective):
    instance = provider.DockerSandboxProvider(image="test")
    monkeypatch.setattr(instance, "_run_docker", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], rc, b"result", b""))
    result = instance.exec("session", ["echo", "result"])
    assert result["requested_policy"] == "docker_container"
    assert result["effective_policy"] == effective
    assert result["fallback_reason"] == ""
