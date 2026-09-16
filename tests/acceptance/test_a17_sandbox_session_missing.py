"""A17 — sandbox removed externally during a session.

Trigger: a session's sandbox (a persistent, per-session container —
`src/sandbox_provider.py`, opt-in via `agent_sandbox_persistent_session`) is
deleted outside Faustus mid-session (`docker rm`, Docker Desktop pruning
it, ...). Expect: the NEXT execution detects it, applies the explicit
`sandbox_missing_policy` (`recreate_empty` | `fail`), and the result never
claims the previous files survived — it says plainly that they did not, and
lists what is known to have been lost.

Drives the REAL `BashTool` twice with the same `session_id`. Only the
Docker CLI wrapper (`DockerSandboxProvider._run_docker`, the actual
subprocess client) is faked, simulating the container existing after the
first call and having been removed before the second. `src/sandbox_exec.py`
and `src/sandbox_provider.py` — the modules under test — run unmodified.
"""
from __future__ import annotations

import subprocess

import pytest

from src import sandbox_exec, sandbox_provider
from src.agent_tools.subprocess_tools import BashTool

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


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """The session ledger (`sandbox_provider._registry_path`) lives under the
    real data dir by default; point it at a scratch file so this test never
    reads or writes another test's (or a real user's) session bookkeeping."""
    reg = tmp_path / "sandbox_sessions.json"
    monkeypatch.setattr(sandbox_provider, "_registry_path", lambda: str(reg))


class FakeDockerClient:
    """Fakes ONLY the Docker CLI wrapper `DockerSandboxProvider._run_docker`
    — the actual subprocess client. Tracks whether the named container is
    "running" so `status()`/`exec()` see it disappear after `.remove()` is
    called, exactly like an operator running `docker rm` by hand between two
    tool calls would look from Faustus's side."""

    def __init__(self):
        self.running = False
        self.calls: list = []

    def remove(self) -> None:
        self.running = False

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        head = args[0] if args else ""
        if head == "version":
            return subprocess.CompletedProcess(args, 0, b"24.0.0", b"")
        if head == "image" and args[1:2] == ["inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:deadbeef", b"")
        if head == "rm":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if head == "run":
            self.running = True
            return subprocess.CompletedProcess(args, 0, b"cid123\n", b"")
        if head == "inspect":
            if self.running:
                return subprocess.CompletedProcess(args, 0, b"true\n", b"")
            return subprocess.CompletedProcess(
                args, 1, b"", b"Error: No such container: faustus-sbx-sess-a17")
        if head == "exec":
            if not self.running:
                return subprocess.CompletedProcess(
                    args, 126, b"", b"Error: No such container: faustus-sbx-sess-a17")
            payload = args[-1]
            marker = "FIRST" if "FIRST" in payload else "SECOND"
            return subprocess.CompletedProcess(
                args, 0, (marker.lower() + "-ok\n").encode(), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")


@pytest.mark.acceptance("A17")
@pytest.mark.asyncio
async def test_next_exec_detects_externally_removed_session_and_never_claims_files_survived(
        workspace, settings, monkeypatch, request):
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    settings.update({
        "agent_sandbox_execution": True,
        "agent_sandbox_mode": "strict",
        "agent_sandbox_persistent_session": True,
        "sandbox_missing_policy": "recreate_empty",
    })

    fake = FakeDockerClient()
    monkeypatch.setattr(sandbox_provider.DockerSandboxProvider, "_run_docker",
                        lambda self, args, timeout=30: fake(args, timeout))

    session_id = "sess-a17"
    ctx = {"session_id": session_id, "sandbox_touches": ["report.md", "notes/plan.txt"]}

    first = await BashTool().execute("echo FIRST", ctx)
    assert first["sandboxed"] is True
    assert first["output"] == "first-ok"
    assert "sandbox_session_note" not in first          # first use, nothing to report
    assert sandbox_provider.session_seen(session_id) is True
    assert sandbox_provider.known_files(session_id) == ["report.md", "notes/plan.txt"]

    # The operator removes the container by hand, mid-session — Faustus does
    # not learn about this until the next call probes it.
    fake.remove()

    second = await BashTool().execute("echo SECOND", {"session_id": session_id})

    assert second["sandboxed"] is True
    assert second["output"] == "second-ok"                       # it ran — in a NEW container
    assert second.get("sandbox_session_recreated") is True
    note = second.get("sandbox_session_note", "")
    # Explicit restore policy, stated plainly.
    assert "recreated" in note.lower() and "empty" in note.lower()
    assert "removed" in note.lower()
    # Never a false claim that the old files survived.
    assert "did not survive" in note.lower() or "did NOT survive" in note
    assert "still" not in note.lower() or "did not" in note.lower()
    # What was known to be lost is named, not hand-waved.
    assert "report.md" in note
    assert second.get("sandbox_lost_files") == ["report.md", "notes/plan.txt"]

    # After the externally-triggered removal, the ledger reflects a fresh,
    # empty session — the lost files are not silently carried forward as if
    # they were still there.
    assert sandbox_provider.known_files(session_id) == []

    record_evidence(
        request,
        tool="BashTool",
        session_id=session_id,
        first_result={k: first[k] for k in ("sandboxed", "output")},
        second_result={k: second[k] for k in
                        ("sandboxed", "output", "sandbox_session_recreated",
                         "sandbox_session_note", "sandbox_lost_files")},
    )


@pytest.mark.asyncio
async def test_fail_policy_refuses_instead_of_recreating(workspace, settings, monkeypatch):
    """Not the acceptance case itself (one marker per function — see the
    test above), but the other half of the explicit policy this case names:
    `sandbox_missing_policy=fail` must refuse rather than silently hand the
    model a fresh container it never asked for."""
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    settings.update({
        "agent_sandbox_execution": True,
        "agent_sandbox_mode": "strict",
        "agent_sandbox_persistent_session": True,
        "sandbox_missing_policy": "fail",
    })
    fake = FakeDockerClient()
    monkeypatch.setattr(sandbox_provider.DockerSandboxProvider, "_run_docker",
                        lambda self, args, timeout=30: fake(args, timeout))

    session_id = "sess-a17-fail"
    ctx = {"session_id": session_id, "sandbox_touches": ["x.txt"]}
    first = await BashTool().execute("echo FIRST", ctx)
    assert first["sandboxed"] is True

    fake.remove()
    second = await BashTool().execute("echo SECOND", {"session_id": session_id})

    assert second["sandbox_refused"] is True
    assert second["sandboxed"] is False
    assert second.get("sandbox_session_recreated") is False
    assert "not run" in second["error"].lower() or "not recreated" in second["error"].lower()
    assert "x.txt" in second["error"]
    assert "SECOND" not in str(second)
