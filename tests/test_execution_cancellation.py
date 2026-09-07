"""Cancellation reaches owned processes, rather than only changing a label."""

from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.bounded_process_output import capture
from src.contracts import ExecutionSpec
from src.execution_backends import DockerWorkspaceBackend, LocalAttendedBackend


def test_capture_cancels_before_timeout_and_preserves_output():
    proc = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 0.5
    result = capture(proc, timeout=20, stop=proc.kill, cancel_requested=lambda: time.monotonic() >= deadline)
    assert result.cancelled and not result.timed_out
    assert b"started" in result.stdout
    assert proc.poll() is not None


def test_cancellation_probe_error_also_stops_owned_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def unavailable():
        raise ConnectionError("Lost cancellation authority")

    with pytest.raises(ConnectionError):
        capture(proc, timeout=20, stop=proc.kill, cancel_requested=unavailable)
    assert proc.poll() is not None


def test_local_cancellation_stops_before_late_write(tmp_path):
    spec = ExecutionSpec.parse({"backend": "local", "isolation": "none", "attended_ack": True,
                                "workspace": str(tmp_path), "limits": {"seconds": 20}})
    ready, late = tmp_path / "ready", tmp_path / "late"
    result = LocalAttendedBackend().run(spec, [sys.executable, "-c",
        "from pathlib import Path; import time; Path('ready').touch(); time.sleep(5); Path('late').touch()"],
        run_id="cancel-fixture", cancel_requested=ready.exists)
    assert result.status == "cancelled"
    assert not late.exists()


def test_already_cancelled_local_work_never_starts(tmp_path):
    spec = ExecutionSpec.parse({"backend": "local", "isolation": "none", "attended_ack": True,
                                "workspace": str(tmp_path), "limits": {"seconds": 20}})
    result = LocalAttendedBackend().run(spec, [sys.executable, "-c", "from pathlib import Path; Path('bad').touch()"],
                                       cancel_requested=lambda: True)
    assert result.status == "cancelled"
    assert not (tmp_path / "bad").exists()


def test_real_container_cancellation_removes_the_container_and_stops_work(tmp_path):
    backend = DockerWorkspaceBackend(image="alpine:3.20")
    if not backend.probe()["ok"]:
        pytest.skip("Installed alpine:3.20 Docker fixture unavailable")
    spec = ExecutionSpec.parse({"backend": "docker_workspace", "isolation": "container",
                                "workspace": str(tmp_path), "limits": {"seconds": 20}})
    ids = []
    original = backend._docker

    def record(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "create" and result.returncode == 0:
            ids.append(result.stdout.decode().strip())
        return result

    backend._docker = record
    result = backend.run(spec, ["sh", "-c", "touch /workspace/ready; sleep 8; touch /workspace/late"],
                         run_id="cancel-container-fixture", cancel_requested=(tmp_path / "ready").exists)
    assert result.status == "cancelled"
    assert not (tmp_path / "late").exists()
    assert len(ids) == 1
    assert original(["inspect", ids[0]], timeout=10).returncode != 0


@pytest.mark.parametrize("probe_error", [False, True])
def test_cancel_during_creation_never_starts_and_cleans_exact_container(tmp_path, monkeypatch, probe_error):
    backend = DockerWorkspaceBackend(image="alpine:3.20")
    monkeypatch.setattr(backend, "preflight", lambda spec: {"ok": True})
    calls = []
    cid = "a" * 64

    def docker(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, (cid + "\n").encode(), b"")

    def cancelled():
        if calls and probe_error:
            raise ConnectionError("Lost authority after create")
        return bool(calls)

    def cannot_start(*args, **kwargs):
        pytest.fail("Cancelled work reached process start")

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(subprocess, "Popen", cannot_start)
    spec = ExecutionSpec.parse({"backend": "docker_workspace", "isolation": "container", "workspace": str(tmp_path)})
    if probe_error:
        with pytest.raises(ConnectionError):
            backend.run(spec, ["sh", "-c", "exit 0"], cancel_requested=cancelled)
    else:
        assert backend.run(spec, ["sh", "-c", "exit 0"], cancel_requested=cancelled).status == "cancelled"
    assert calls[0][0] == "create"
    assert calls[0][calls[0].index("--pull") + 1] == "never"
    assert calls[-1] == ["rm", "--force", cid]
