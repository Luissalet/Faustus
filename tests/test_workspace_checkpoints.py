"""Checkpoint git timeouts must not wait for inherited child output handles."""
import subprocess
import sys
import time

import pytest

from src import workspace_checkpoints as wc


def _git_helper(monkeypatch, tmp_path, source):
    helper = tmp_path / "git_helper.py"
    helper.write_text(source, encoding="utf-8")
    real_popen = subprocess.Popen

    def spawn(_command, **kwargs):
        return real_popen([sys.executable, str(helper)], **kwargs)

    monkeypatch.setattr(wc.subprocess, "Popen", spawn)


def test_timeout_does_not_wait_for_descendant_output_handles(monkeypatch, tmp_path):
    # Measure process teardown, not the first import of the application's core.
    from src import process_ownership  # noqa: F401
    child_pid = tmp_path / "child.pid"
    _git_helper(monkeypatch, tmp_path,
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "time.sleep(10)\n")
    started = time.monotonic()
    result = wc._run(str(tmp_path), ["add", "-A"], timeout=0.6)
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 2.5, f"timeout cleanup waited {elapsed:.2f}s for a descendant"
    assert child_pid.exists(), "the inherited-handle child must really have started"
    import psutil
    assert not psutil.pid_exists(int(child_pid.read_text()))


@pytest.mark.parametrize("binary", [False, True])
def test_git_output_and_failure_status_are_preserved(monkeypatch, tmp_path, binary):
    _git_helper(monkeypatch, tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'hello\\xff')\n"
        "sys.stderr.buffer.write(b'failure\\xfe')\n"
        "sys.exit(7)\n")
    result = wc._run(str(tmp_path), ["version"], timeout=3, binary=binary, check=True)
    assert result.returncode == 7
    assert result.stdout == (b"hello\xff" if binary else "hello\ufffd")
    assert result.stderr == (b"failure\xfe" if binary else "failure\ufffd")


def test_text_output_keeps_universal_newline_semantics(monkeypatch, tmp_path):
    _git_helper(monkeypatch, tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'one\\r\\ntwo\\rthree\\n')\n")
    result = wc._run(str(tmp_path), ["version"], timeout=3)
    assert result.returncode == 0
    assert result.stdout == "one\ntwo\nthree\n"
