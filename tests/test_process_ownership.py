"""A kill must be aimed at a process this application still owns.

`_kill_tree` reaches for `taskkill /T /F` (Windows) and `killpg(…, SIGKILL)`
(POSIX). Both are unconditional and take the children with them, so aiming one
at a pid the OS has handed to somebody else destroys work that has nothing to
do with the agent — on this user's machine, plausibly the Ollama server with
models loaded on two GPUs. These pin the rule: kill what we hold, refuse what
we do not, and say so in a message the user can act on.
"""
import inspect

import pytest

from src import process_ownership as po
from src.agent_tools import subprocess_tools as st


class LiveProc:
    """Stands in for a Popen / asyncio Process we started and still hold."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True


class ExitedProc(LiveProc):
    def __init__(self, pid=4242, returncode=0):
        super().__init__(pid)
        self.returncode = returncode


class PidOnly:
    """Something carrying a pid and nothing that proves where it came from."""

    def __init__(self, pid=11434):
        self.pid = pid


@pytest.fixture(autouse=True)
def _no_spawn_records():
    for pid in po.started_pids():
        po.forget(pid)
    yield
    for pid in po.started_pids():
        po.forget(pid)


def _record_kills(monkeypatch, windows):
    """Capture what `_kill_tree` would have signalled, without signalling it."""
    calls = {"taskkill": [], "killpg": []}
    monkeypatch.setattr(st, "IS_WINDOWS", windows)
    monkeypatch.setattr(po, "IS_WINDOWS", windows)
    monkeypatch.setattr(st.subprocess, "run", lambda argv, **kw: calls["taskkill"].append(argv))
    monkeypatch.setattr(st.os, "getpgid", lambda pid: 900000 + (pid or 0))
    monkeypatch.setattr(st.os, "killpg", lambda pgid, sig: calls["killpg"].append((pgid, sig)))
    return calls


def test_a_live_process_we_started_is_killed_with_its_tree(monkeypatch):
    calls = _record_kills(monkeypatch, windows=False)
    proc = LiveProc()
    assert st._kill_tree(proc) is None
    assert calls["killpg"] == [(900000 + 4242, st.signal.SIGKILL)]
    assert proc.killed


def test_windows_taskkill_still_takes_a_live_tree(monkeypatch):
    calls = _record_kills(monkeypatch, windows=True)
    assert st._kill_tree(LiveProc(pid=777)) is None
    assert calls["taskkill"] == [["taskkill", "/T", "/F", "/PID", "777"]]


def test_a_bare_pid_is_refused_and_named(monkeypatch):
    calls = _record_kills(monkeypatch, windows=True)
    refusal = st._kill_tree(11434)

    assert calls == {"taskkill": [], "killpg": []}
    assert "11434" in refusal
    assert "Refusing to kill" in refusal
    # The user is told how to do it themselves rather than left with nothing.
    assert "taskkill /PID 11434" in refusal


def test_an_object_carrying_only_a_pid_is_refused(monkeypatch):
    calls = _record_kills(monkeypatch, windows=False)
    refusal = st._kill_tree(PidOnly())

    assert calls == {"taskkill": [], "killpg": []}
    assert "not evidence" in refusal
    assert "kill 11434" in refusal


def test_an_already_exited_process_is_not_killed_by_pid(monkeypatch):
    """The pid of a reaped child may already belong to somebody else."""
    calls = _record_kills(monkeypatch, windows=True)
    proc = ExitedProc(pid=11434)

    message = st._kill_tree(proc)

    assert calls == {"taskkill": [], "killpg": []}
    assert not proc.killed
    assert "already exited" in message


def test_a_recycled_pid_is_refused_even_while_the_object_looks_live(monkeypatch):
    """Start time, not the pid, settles whether this is still our process."""
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=11434)
    po.note_started(proc, command="npm run build")

    monkeypatch.setattr(po, "_create_time", lambda pid: 9000.0)
    verdict = po.check(proc)

    assert verdict.owned is False
    assert verdict.code == "recycled"
    calls = _record_kills(monkeypatch, windows=True)
    assert "Refusing to kill" in st._kill_tree(proc)
    assert calls == {"taskkill": [], "killpg": []}


def test_a_registered_pid_whose_start_time_matches_is_still_ours(monkeypatch):
    monkeypatch.setattr(po, "_create_time", lambda pid: 1000.0)
    proc = LiveProc(pid=11434)
    po.note_started(proc)
    assert po.check(proc).owned is True


def test_missing_start_times_do_not_block_a_kill(monkeypatch):
    """psutil is optional; without it the object's own liveness has to carry it."""
    monkeypatch.setattr(po, "_create_time", lambda pid: None)
    proc = LiveProc()
    po.note_started(proc)
    assert po.check(proc).owned is True


def test_kill_tree_has_no_force_override():
    """The caller upstream is a model: a flag it could set is not a safeguard."""
    params = list(inspect.signature(st._kill_tree).parameters)
    assert params == ["proc"]
    assert list(inspect.signature(st._kill_tree_async).parameters) == ["proc"]


def test_streaming_runner_registers_and_releases_the_spawn(monkeypatch):
    """The watchdog's kill must be able to tell our child from a reused pid."""
    import asyncio

    seen = {}

    class DoneProc(LiveProc):
        def __init__(self):
            super().__init__(pid=5150)
            self.stdout = None
            self.stderr = None

        async def wait(self):
            seen["registered_during_run"] = self.pid in po.started_pids()
            self.returncode = 0
            return 0

    asyncio.run(st._run_subprocess_streaming(DoneProc(), timeout=5, idle_timeout=0))

    assert seen["registered_during_run"] is True
    assert 5150 not in po.started_pids()
