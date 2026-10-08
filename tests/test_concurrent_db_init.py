"""Two fresh processes must not race schema creation on an empty file-backed DB.

Historical failure: ``table sessions already exists`` / ``chat_messages already
exists`` when two interpreters import ``core.database`` together against a new
``DATABASE_URL``. ``create_all(checkfirst=True)`` is not enough across processes.

A holder that dies inside the init critical section must not wedge the next
boot: KernelFileLock is released by the OS when the process exits.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_WORKER = r"""
import sys, time
from pathlib import Path
barrier = Path(sys.argv[1])
deadline = time.time() + 60
while not barrier.exists():
    if time.time() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.01)
import core.database  # noqa: F401
print("ok", flush=True)
"""

_CRASH_HOLDER = r"""
import os, sys
from pathlib import Path
from core.kernel_file_lock import KernelFileLock
lock_path, ready = sys.argv[1], Path(sys.argv[2])
# Hold the same lock path init_db uses, then die without releasing.
with KernelFileLock(lock_path, timeout=5.0):
    ready.write_text("held", encoding="utf-8")
    os._exit(9)
"""


def test_two_processes_can_init_schema_on_empty_db(tmp_path):
    barrier = tmp_path / "go"
    data = tmp_path / "data"
    data.mkdir()
    db = data / "app.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "ODYSSEUS_DATA_DIR": str(data),
        "FAUSTUS_DATA_DIR": str(data),
        "PYTHONUTF8": "1",
    }
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(barrier)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    time.sleep(0.25)
    barrier.write_text("go", encoding="utf-8")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append((p.returncode, out, err))
    codes = [c for c, _, _ in results]
    assert codes == [0, 0], results
    assert db.exists()
    assert all("ok" in out for _, out, _ in results)


def test_dead_init_lock_holder_is_taken_over_quickly(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "app.db"
    lock_path = f"{db}.init.lock"
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [sys.executable, "-c", _CRASH_HOLDER, lock_path, str(ready)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "holder never acquired the init lock"
    holder.wait(timeout=10)
    assert holder.returncode == 9
    # Stable path remains; the OS advisory lock is gone with the process.
    assert Path(lock_path).exists(), "kernel lock path must stay stable"

    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "ODYSSEUS_DATA_DIR": str(data),
        "FAUSTUS_DATA_DIR": str(data),
        "PYTHONUTF8": "1",
    }
    started = time.monotonic()
    follower = subprocess.run(
        [sys.executable, "-c", "import core.database; print('ok', flush=True)"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - started
    assert follower.returncode == 0, (follower.stdout, follower.stderr)
    assert "ok" in follower.stdout
    assert elapsed < 15.0, f"dead-owner recovery took {elapsed:.1f}s"
    assert Path(lock_path).exists(), "init must not unlink the stable lock path"
    assert db.exists()


_THREE_PROC_WORKER = r"""
import sys, time
from pathlib import Path
from core.kernel_file_lock import KernelFileLock

directory = Path(sys.argv[1])
who = sys.argv[2]
path = str(directory / "critical.lock")


def wait(name):
    stop = time.monotonic() + 20
    while not (directory / name).exists():
        if time.monotonic() > stop:
            raise RuntimeError(name)
        time.sleep(0.01)


# Optional pause after acquire would still keep the OS lock held — there is no
# rename window that could make the path appear free to a third process.
with KernelFileLock(path, timeout=15.0):
    (directory / f"{who}-entered").write_text("held", encoding="utf-8")
    wait("release-" + who)
"""


def test_three_processes_never_overlap_on_kernel_init_lock(tmp_path):
    """Coase #65 r3: renaming a live lock created a window for C to enter.

    KernelFileLock never removes the stable path to steal; A/B/C serialize on
    the OS advisory lock. Force the dead→live→third sequence with real processes.
    """
    lock = tmp_path / "critical.lock"
    holder = subprocess.run(
        [sys.executable, "-c", _CRASH_HOLDER, str(lock), str(tmp_path / "ready")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert holder.returncode == 9
    assert lock.exists()

    def wait(name: str, timeout: float = 20.0) -> None:
        stop = time.monotonic() + timeout
        while not (tmp_path / name).exists():
            if time.monotonic() > stop:
                raise AssertionError(f"timeout waiting for {name}")
            time.sleep(0.01)

    def spawn(who: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", _THREE_PROC_WORKER, str(tmp_path), who],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    # After the dead holder, A acquires. While A holds, B and C must wait —
    # neither may write *-entered until A releases.
    a = spawn("a")
    wait("a-entered")
    b = spawn("b")
    c = spawn("c")
    time.sleep(0.5)
    assert a.poll() is None
    assert not (tmp_path / "b-entered").exists(), "B entered while A held"
    assert not (tmp_path / "c-entered").exists(), "C entered while A held"
    assert lock.exists(), "stable path must not vanish under a live holder"

    (tmp_path / "release-a").write_text("go", encoding="utf-8")
    # Exactly one of B/C enters next; the other waits.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        entered = [who for who in ("b", "c") if (tmp_path / f"{who}-entered").exists()]
        if len(entered) >= 1:
            break
        time.sleep(0.02)
    entered = [who for who in ("b", "c") if (tmp_path / f"{who}-entered").exists()]
    assert len(entered) == 1, f"expected exactly one waiter to enter, got {entered}"
    first = entered[0]
    second = "c" if first == "b" else "b"
    assert not (tmp_path / f"{second}-entered").exists()

    (tmp_path / f"release-{first}").write_text("go", encoding="utf-8")
    wait(f"{second}-entered")
    (tmp_path / f"release-{second}").write_text("go", encoding="utf-8")
    assert a.wait(timeout=10) == 0
    assert b.wait(timeout=10) == 0
    assert c.wait(timeout=10) == 0
    assert lock.exists()
