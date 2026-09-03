"""Which spawn sites lose Faustus's virtualenv, and which must keep it.

`src/native_env.py` decides what a FOREIGN child's environment looks like.
These tests are about the wiring: that the sites which run someone else's code
call it, and — just as important — that the sites where Faustus runs its OWN
python still inherit the venv, because there the inheritance is the point.

Every test drives a synthetic host (`fake_venv`) rather than the interpreter
the suite really runs in, so the result does not depend on whether this machine
has a venv, or where it is.
"""
import asyncio
import os
import sys

import pytest

from src import bg_jobs, builtin_mcp, builtin_actions, project_tests
from src.agent_tools import subprocess_tools as st
from src.native_env import VENV_MARKERS

VENV = "/srv/faustus/venv"
VENV_BIN = VENV + "/bin"
SITE_PACKAGES = VENV + "/lib/python3.11/site-packages"


@pytest.fixture
def fake_venv(monkeypatch):
    """A host that looks like an activated Faustus install.

    sys.prefix is pinned to a non-venv so the only environment in play is the
    invented one — otherwise the result would differ between a developer inside
    a venv and CI outside one.
    """
    monkeypatch.setattr(sys, "prefix", "/usr", raising=False)
    monkeypatch.setattr(sys, "base_prefix", "/usr", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.setenv("VIRTUAL_ENV", VENV)
    monkeypatch.setenv("PYTHONPATH", SITE_PACKAGES)
    monkeypatch.setenv("PATH", os.pathsep.join([VENV_BIN, "/usr/local/bin", "/usr/bin"]))
    monkeypatch.setenv("HOME", "/home/user")


def assert_is_native(env):
    """`env` carries no trace of the host's virtualenv, but is still usable."""
    assert env is not None, "the child was left to inherit our environment"
    for marker in VENV_MARKERS:
        assert marker not in env, f"{marker} leaked into a foreign child"
    entries = env["PATH"].split(os.pathsep)
    assert VENV_BIN not in entries, "our venv's bin still leads the child's PATH"
    assert "/usr/bin" in entries, "the rest of PATH must survive"
    assert env["HOME"] == "/home/user", "only the venv is stripped, nothing else"


class FakeProc:
    """Enough of a process for the code under test to finish its call."""

    def __init__(self, stdout="", returncode=0):
        self.returncode = returncode
        self._stdout = stdout
        self.pid = 424242

    def communicate(self, timeout=None):
        return self._stdout, ""

    async def wait(self):
        return self.returncode


# ── the user's own project tests (src/project_tests.py) ────────────────────

def test_project_test_runs_do_not_inherit_our_venv(fake_venv, tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc(stdout="1 passed in 0.1s")

    monkeypatch.setattr(project_tests.subprocess, "Popen", fake_popen)
    project_tests.run_tests(str(tmp_path), {"kind": "pytest", "argv": ["/proj/.venv/bin/python", "-m", "pytest"]})

    assert_is_native(captured["env"])


def test_project_test_runs_keep_their_runner_settings(fake_venv):
    env = project_tests._clean_env()
    # Dropping the venv must not drop the flags the runners are configured with.
    assert env["CI"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


# ── the agent's Bash tool (src/agent_tools/subprocess_tools.py) ────────────

def test_agent_bash_does_not_inherit_our_venv(fake_venv, monkeypatch):
    captured = {}

    async def fake_shell(command, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(st, "IS_WINDOWS", False)
    monkeypatch.setattr(st.asyncio, "create_subprocess_shell", fake_shell)
    # The caller hands the tool a copy of our own environment; the leak is that
    # copy, so the test has to supply it rather than leave env unset.
    asyncio.run(st._create_bash_subprocess("pip install requests", env=dict(os.environ)))

    assert_is_native(captured["env"])


def test_agent_bash_on_windows_does_not_inherit_our_venv(fake_venv, monkeypatch):
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(st, "IS_WINDOWS", True)
    monkeypatch.setattr(st, "find_bash", lambda: "C:/Git/bin/bash.exe")
    monkeypatch.setattr(st.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(st._create_bash_subprocess("pytest", env=dict(os.environ)))

    assert_is_native(captured["env"])


def test_agent_bash_without_a_given_env_still_loses_the_venv(fake_venv, monkeypatch):
    """env=None means "inherit ours", which is the leak in its purest form."""
    captured = {}

    async def fake_shell(command, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(st, "IS_WINDOWS", False)
    monkeypatch.setattr(st.asyncio, "create_subprocess_shell", fake_shell)
    asyncio.run(st._create_bash_subprocess("pytest"))

    assert_is_native(captured["env"])


def test_agent_bash_tmux_session_is_started_without_the_venv(fake_venv, monkeypatch):
    """The tmux path is the one POSIX actually takes, so it leaks too.

    The session's shell is spawned by the tmux server, not by us, so the fix
    has to travel in the `env` command tmux is told to run.
    """
    calls = []

    async def fake_run_exec(*argv, timeout=10):
        calls.append(argv)
        return "", "", 0

    existing = {"seen": False}

    async def fake_has_session(name):
        # False first (so a session is created), True after.
        was = existing["seen"]
        existing["seen"] = True
        return was

    monkeypatch.setattr(st, "_run_exec", fake_run_exec)
    monkeypatch.setattr(st, "_tmux_has_session", fake_has_session)
    asyncio.run(st._ensure_tmux_session("ody-agent-x", "/work", {"TERM": "xterm-256color"}))

    new_session = next(argv for argv in calls if "new-session" in argv)
    for marker in VENV_MARKERS:
        assert "-u" in new_session and marker in new_session, (
            f"the agent's tmux shell would still inherit {marker}"
        )
    path_arg = next(a for a in new_session if a.startswith("PATH="))
    entries = path_arg[len("PATH="):].split(os.pathsep)
    assert VENV_BIN not in entries
    assert "/usr/bin" in entries


# ── the shell service (services/shell/service.py) ──────────────────────────

def test_shell_service_does_not_inherit_our_venv(fake_venv, monkeypatch):
    from services.shell import service as shell_service

    captured = {}

    async def fake_shell(command, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    async def fake_communicate():
        return b"", b""

    proc = FakeProc()
    proc.communicate = fake_communicate

    async def fake_shell_returning_proc(command, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(shell_service.asyncio, "create_subprocess_shell", fake_shell_returning_proc)
    asyncio.run(shell_service.ShellService().execute("pip list"))

    assert_is_native(captured["env"])


# ── the user's automations (src/builtin_actions.py) ────────────────────────

def test_action_scripts_do_not_inherit_our_venv(fake_venv, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)

        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _R()

    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", fake_run)
    asyncio.run(builtin_actions.action_run_local("owner", script="python -c 'import sys'"))

    assert_is_native(captured["env"])


# ── the other half: what must KEEP the venv ────────────────────────────────

def test_faustus_own_python_servers_still_get_our_site_packages(fake_venv):
    """The built-in MCP servers run on OUR interpreter and import OUR packages.

    If a venv sweep ever reached this function the built-in servers would stop
    importing, so the inheritance is asserted rather than merely intended.
    """
    env = builtin_mcp.builtin_python_env("/srv/faustus")
    assert "/srv/faustus" in env["PYTHONPATH"].split(os.pathsep)
    assert SITE_PACKAGES in env["PYTHONPATH"].split(os.pathsep)


def test_the_cookbook_runner_still_inherits_the_venv(fake_venv, tmp_path, monkeypatch):
    """/api/shell/stream is the Cookbook, and its runner scripts are ours.

    They put our venv's bin on PATH deliberately and read ${VIRTUAL_ENV:-…} to
    locate the CUDA wheels that /api/cookbook/install-package put in our own
    site-packages. Passing a native environment here would strand a local vLLM
    serve with no libnvrtc, so the absence of `env` is the assertion.
    """
    routes_shell = pytest.importorskip("routes.shell_routes")
    captured = {}

    class _Stderr:
        async def read(self):
            return b"tmux missing"

    class _TmuxProc(FakeProc):
        def __init__(self):
            super().__init__(returncode=1)
            self.stderr = _Stderr()

    async def fake_shell(command, **kwargs):
        captured["kwargs"] = kwargs
        return _TmuxProc()

    class _Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(routes_shell, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(routes_shell.asyncio, "create_subprocess_shell", fake_shell)

    async def drain():
        return [chunk async for chunk in routes_shell._generate_tmux("vllm serve x", _Request())]

    asyncio.run(drain())

    assert "env" not in captured["kwargs"], (
        "the Cookbook runner must inherit Faustus's venv; it depends on it"
    )


def test_a_detached_background_job_does_not_inherit_our_venv(fake_venv, tmp_path, monkeypatch):
    """`#!bg` is the agent's Bash tool detached, and it is what the agent is
    told to use for installs — so it carries the same leak, at its worst."""
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path)
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(bg_jobs, "git_bash_path", lambda p: str(p))
    monkeypatch.setattr(bg_jobs, "detached_popen_kwargs", dict)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", fake_popen)
    bg_jobs.launch("pip install requests", "sess-a")

    assert_is_native(captured["env"])
