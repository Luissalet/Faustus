"""Exercise the actual host lock API, including process death."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.file_lock import LockTimeout
from core.kernel_file_lock import KernelFileLock


ROOT = Path(__file__).resolve().parents[1]


def test_process_death_releases_lock_without_age_based_takeover(tmp_path):
    path = tmp_path / "resource.lock"
    ready = tmp_path / "ready"
    child = subprocess.Popen([
        sys.executable, "-c",
        "import sys,time; from pathlib import Path; "
        "from core.kernel_file_lock import KernelFileLock; "
        "lock=KernelFileLock(sys.argv[1]).acquire(); "
        "Path(sys.argv[2]).touch(); time.sleep(60)",
        str(path), str(ready),
    ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "Child did not acquire its lock"
        os.utime(path, (1, 1))  # even an ancient file belongs to its live holder
        with pytest.raises(LockTimeout):
            with KernelFileLock(path, timeout=0.1):
                pytest.fail("A live process's lock was stolen")
        child.terminate()  # simulate a crash of this test-owned process only
        child.wait(timeout=10)
        with KernelFileLock(path, timeout=1):
            assert path.exists()
        assert path.exists()  # deleting a lock file would split its identity
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_exception_releases_and_instance_can_be_reused(tmp_path):
    lock = KernelFileLock(tmp_path / "resource.lock")
    with pytest.raises(ValueError):
        with lock:
            raise ValueError("Failed operation")
    with lock:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    lock.release()


def test_open_failure_does_not_enter_unlocked(tmp_path):
    with pytest.raises(OSError):
        with KernelFileLock(tmp_path):  # a directory is not a lock file
            pytest.fail("Lock failure was ignored")
