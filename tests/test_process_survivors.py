"""#1105 (product side of #1083): processes that outlive the agent command that started them.

The incident: a model-written test started a server with Popen, failed with a TypeError
before p.terminate(), and the bash tool returned rc 1 while the server kept running,
unseen by the model and by the process manager. Each host command now starts suspended
inside its own Job Object, so every descendant is known from its first instruction.
Members still alive when the command ends are adopted as one handle of the session and
reported. Nothing is killed automatically: deliberate persistence stays alive.

Real processes, Windows only (the Job Object mechanism). The process store is redirected
to a temporary file.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time

import psutil
import pytest

from src import process_manager as pm
from src import process_survivors as ps
from src.process_manager import Caller

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object scopes are Windows-only")

PY = sys.executable
ME = Caller(owner="luis", session_id="s-1105")
CTX = {"owner": "luis", "session_id": "s-1105"}


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_PROCESS_STORE", str(tmp_path / "handles.json"))
    values = {}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    pm.reset_for_tests()
    manager = pm.manager()
    yield manager
    for handle, rec in list(manager._records.items()):
        if rec.get("state") in pm.LIVE_STATES:
            manager.stop(handle, Caller(owner=rec.get("owner", ""), session_id=rec.get("session_id", "")))
    pm.reset_for_tests()


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill(pid: int) -> None:
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _sleeper_code(seconds: int = 60) -> str:
    return f"import time; time.sleep({seconds})"


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))


async def _run_python_tool(code: str, tmp_path):
    from src.agent_tools.subprocess_tools import PythonTool
    return await PythonTool().execute(code, dict(CTX))


def _scoped(code: str, *, cwd: str):
    """Start `python -c code` the way the tools do, through a CommandScope."""
    async def go():
        scope = ps.CommandScope.create()
        proc = await asyncio.create_subprocess_exec(
            PY, "-c", code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd, **scope.spawn_kwargs())
        scope.attach_and_resume(proc)
        out, err = await proc.communicate()
        return scope, proc, out, err
    return asyncio.run(go())


def test_scope_starts_suspended_resumes_and_tracks_descendants(tmp_path):
    code = textwrap.dedent(f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", {_sleeper_code()!r}])
        print(p.pid)
    """)
    scope, proc, out, _ = _scoped(code, cwd=str(tmp_path))
    child = int(out.decode().strip())
    try:
        assert scope.mechanism == "job_object"
        assert proc.returncode == 0          # it ran: resumed, not left suspended
        survivors = scope.settle()
        assert [s["pid"] for s in survivors] == [child]
        assert survivors[0]["created"] == pytest.approx(psutil.Process(child).create_time(), abs=1)
    finally:
        scope.close()
    # Closing Faustus's job handle does not end its members (no KILL_ON_JOB_CLOSE).
    time.sleep(0.3)
    assert _alive(child)
    _kill(child)


def test_child_that_finished_is_not_a_survivor(tmp_path):
    code = textwrap.dedent(f"""
        import subprocess, sys
        subprocess.run([sys.executable, "-c", "print(1)"], check=True)
    """)
    scope, proc, _, _ = _scoped(code, cwd=str(tmp_path))
    try:
        assert proc.returncode == 0
        assert scope.settle() == []
    finally:
        scope.close()


def test_breakaway_is_still_allowed(tmp_path):
    code = textwrap.dedent(f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", {_sleeper_code()!r}], creationflags=0x01000000)
        print(p.pid)
    """)
    scope, proc, out, err = _scoped(code, cwd=str(tmp_path))
    try:
        assert proc.returncode == 0, err.decode(errors="replace")
        child = int(out.decode().strip())
        assert scope.settle() == []        # it left the job on purpose: not ours to track
    finally:
        scope.close()
    _kill(child)


def test_failed_test_with_popen_server_is_reported_and_adopted(mgr, tmp_path):
    """The #1083 pattern: Popen, then an exception before terminate()."""
    code = textwrap.dedent(f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", {_sleeper_code()!r}])
        print("server", p.pid, flush=True)
        raise TypeError("req() got multiple values for argument 'body'")
    """)
    result = asyncio.run(_run_python_tool(code, tmp_path))
    text = str(result.get("output") or "") + str(result.get("error") or "")
    assert result.get("exit_code") == 1
    assert "TypeError" in text                       # the original failure stays visible
    still = result.get("still_running") or []
    assert len(still) == 1
    pid, handle = still[0]["pid"], still[0]["handle"]
    assert _alive(pid)
    assert handle in text and "process_stop" in text  # the model is told how to manage it
    listed = {r["handle"]: r for r in mgr.list(ME)["handles"]}
    assert listed[handle]["kind"] == "adopted" and listed[handle]["state"] == "running"
    stopped = mgr.stop(handle, ME)
    assert stopped["ok"] and stopped["state"] == "stopped" and stopped["signalled"]
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_deliberate_background_server_stays_alive(mgr, tmp_path):
    from src.agent_tools.subprocess_tools import BashTool
    from core.platform_compat import find_bash
    if not find_bash():
        pytest.skip("Git Bash not installed")
    py = PY.replace("\\", "/")
    result = asyncio.run(BashTool()._on_host(
        f'"{py}" -c "import time; time.sleep(60)" > /dev/null 2>&1 &\necho started',
        None, None, "s-1105", caller=ME))
    assert result.get("exit_code") == 0 and "started" in result.get("output", "")
    still = result.get("still_running") or []
    assert len(still) == 1
    pid = still[0]["pid"]
    time.sleep(1.0)
    assert _alive(pid)                                  # never killed automatically
    rec = mgr._records[still[0]["handle"]]
    assert rec["state"] == "running" and rec["session_id"] == "s-1105"
    mgr.stop(still[0]["handle"], ME)


def test_adopted_handle_exits_when_its_processes_end(mgr, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "ADOPTED_POLL_S", 0.1)
    code = textwrap.dedent("""
        import subprocess, sys
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    """)
    result = asyncio.run(_run_python_tool(code, tmp_path))
    handle = (result.get("still_running") or [{}])[0].get("handle")
    assert handle
    deadline = time.time() + 10
    while mgr._records[handle]["state"] in pm.LIVE_STATES and time.time() < deadline:
        time.sleep(0.05)
    rec = mgr._records[handle]
    assert rec["state"] == "exited"
    assert rec["exit_code"] is None and "exit" in rec["termination_reason"]


def test_other_session_cannot_stop_an_adopted_handle(mgr, tmp_path):
    code = textwrap.dedent(f"""
        import subprocess, sys
        subprocess.Popen([sys.executable, "-c", {_sleeper_code()!r}])
    """)
    result = asyncio.run(_run_python_tool(code, tmp_path))
    item = result["still_running"][0]
    denied = mgr.stop(item["handle"], Caller(owner="luis", session_id="other"))
    assert denied["ok"] is False and denied["code"] == "not_owner"
    assert _alive(item["pid"])
    mgr.stop(item["handle"], ME)


def test_assignment_failure_still_runs_the_command(monkeypatch, tmp_path):
    scope = ps.CommandScope.create()
    real = ps._win()[0]

    class _NoAssign:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        def AssignProcessToJobObject(job, handle):
            return False

    monkeypatch.setattr(ps, "_win", lambda: (_NoAssign(), ps._API[1]))

    async def go():
        proc = await asyncio.create_subprocess_exec(
            PY, "-c", "print('ran')", stdout=asyncio.subprocess.PIPE, **scope.spawn_kwargs())
        scope.attach_and_resume(proc)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return proc.returncode, out

    rc, out = asyncio.run(go())
    assert rc == 0 and out.strip() == b"ran"           # resumed even though not tracked
    assert scope.mechanism == "none" and scope.settle() == []
    scope.close()
