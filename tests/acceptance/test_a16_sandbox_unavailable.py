"""A16 — host sandbox unavailable or unsupported.

Trigger: the sandbox is turned ON (`agent_sandbox_execution`) but cannot
serve this call — no Docker daemon (POSIX) or a platform the container can
never verify (native Windows). Expect: a clear, explicit "unavailable"
result, and NEVER a silent privileged fallback to running the command on
the host.

This drives the REAL `BashTool` (`src/agent_tools/subprocess_tools.py`, not
owned by this lot but the only caller of `src/sandbox_exec.py`) with
`agent_sandbox_mode=strict`, which is the setting that governs whether a
host fallback is even permitted (`auto` is the explicit opt-in the contract
allows; `strict` is "never"). Only the Docker CLI wrapper
(`DockerWorkspaceBackend._docker`, the actual subprocess client) is faked —
`src/sandbox_exec.py` and `src/sandbox_provider.py`, the modules under test,
run unmodified. The host launcher (`_create_bash_subprocess`) is spied on to
prove it is never invoked.
"""
from __future__ import annotations

import subprocess

import pytest

from src import sandbox_exec, sandbox_provider
from src.agent_tools import subprocess_tools as spt
from src.agent_tools.subprocess_tools import BashTool
from src.execution_backends import DockerWorkspaceBackend

from tests.acceptance.conftest import record_evidence


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    import src.tool_execution as te
    monkeypatch.setattr(te, "agent_cwd", lambda: str(ws))
    return str(ws)


@pytest.fixture()
def settings(monkeypatch):
    values: dict = {}
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values.get(key, default))
    return values


def _no_daemon(self, args, timeout=60):
    """Fakes the Docker CLI wrapper only: `docker version` never answers,
    as if the daemon were not running — exactly the failure `probe()` is
    supposed to turn into an `Availability(available=False, ...)`."""
    return subprocess.CompletedProcess(
        args, 1, b"", b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock")


class _HostSpy:
    """Counts real calls to the host process launcher without changing its
    behaviour, so a test can assert on "never called" rather than trust a
    setting that merely claims it wasn't needed."""

    def __init__(self, real):
        self.calls = 0
        self._real = real

    async def __call__(self, *a, **kw):
        self.calls += 1
        return await self._real(*a, **kw)


@pytest.mark.acceptance("A16")
@pytest.mark.asyncio
async def test_unavailable_sandbox_never_falls_back_to_host_privileges(
        workspace, settings, monkeypatch, request):
    # ── POSIX: daemon absent, strict mode → explicit refusal, no host run ──
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    monkeypatch.setattr(DockerWorkspaceBackend, "_docker", _no_daemon)
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})

    spy = _HostSpy(spt._create_bash_subprocess)
    monkeypatch.setattr(spt, "_create_bash_subprocess", spy)

    result = await BashTool().execute("echo THIS-MUST-NOT-RUN-ON-THE-HOST", {})

    assert result["sandbox_refused"] is True
    assert result["sandboxed"] is False
    assert result["exit_code"] == 126
    # The literal, model-readable verdict — not just a truthy flag.
    assert "sandbox unavailable:" in result["error"]
    assert "daemon" in result["error"].lower()
    # The command text itself never reached anywhere the result could echo it.
    assert "THIS-MUST-NOT-RUN-ON-THE-HOST" not in str(result)
    # No privileged host fallback happened — not "zero output", the launcher
    # itself was never invoked.
    assert spy.calls == 0

    # ── the new port answers the same way directly, plus the "unsupported
    #    platform" half of the trigger (native Windows: a Linux sandbox
    #    container can never verify that host) ──────────────────────────
    provider = sandbox_provider.get_provider(image=sandbox_exec.image(), workspace=workspace)
    avail_no_daemon = provider.probe()
    assert avail_no_daemon.available is False
    assert avail_no_daemon.kind == "daemon"
    assert "daemon" in avail_no_daemon.reason.lower()

    import core.platform_compat as pc
    monkeypatch.setattr(pc, "IS_WINDOWS", True, raising=False)
    avail_unsupported = provider.probe()
    assert avail_unsupported.available is False
    assert avail_unsupported.kind == "platform"
    assert "windows" in avail_unsupported.reason.lower()

    record_evidence(
        request,
        tool="BashTool",
        mode="strict",
        error=result["error"],
        host_launcher_calls=spy.calls,
        provider_probe_no_daemon=avail_no_daemon.to_dict(),
        provider_probe_unsupported_platform=avail_unsupported.to_dict(),
    )
