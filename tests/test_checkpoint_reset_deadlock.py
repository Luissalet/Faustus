"""BUG 1 — permanent deadlock in src/workspace_checkpoints.

`checkpoint()` takes the per-workspace `_lock_for(root)` and, when the shadow
repo has outgrown `agent_checkpoint_max_repo_mb`, called the PUBLIC `reset()`
from inside it. `reset()` takes the very same non-reentrant `threading.Lock`,
so the call never returned: from then on checkpoint/changed_since/diff_since/
restore/reset for that workspace hung forever, and the user's turn hung with
them (agent_loop awaits `asyncio.to_thread(...)` with no timeout).

The fix extracts the body of `reset()` into `_reset_locked()` (no lock) and
calls that from both places.
"""

import shutil
import threading

import pytest

_HAS_GIT = shutil.which("git") is not None


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def settings(monkeypatch):
    values = {}
    import src.settings as st
    real = st.get_setting

    def _get(key, default=None):
        return values[key] if key in values else real(key, default)
    monkeypatch.setattr(st, "get_setting", _get, raising=False)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    return root


def _call_with_timeout(fn, seconds=10.0):
    """Run `fn` in a daemon thread; return (finished, result)."""
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:            # noqa: BLE001 - reported to the test
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=seconds)
    return (not t.is_alive()), box


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_checkpoint_over_the_repo_cap_does_not_deadlock(ws, data_dir, settings):
    from src import workspace_checkpoints as wc
    root = wc._norm_root(str(ws))
    try:
        first = wc.checkpoint(str(ws), "before turn 1")
        assert first and first["created"]

        # The shadow repo now exists and is far above a 0.001 MB cap, so the
        # next checkpoint takes the "reset the oversized shadow repo" branch.
        settings["agent_checkpoint_max_repo_mb"] = 0.001
        finished, box = _call_with_timeout(lambda: wc.checkpoint(str(ws), "before turn 2"), 10.0)
        assert finished, "checkpoint() deadlocked on its own per-workspace lock"
        assert "error" not in box, box.get("error")
        cp = box["result"]
        # The turn still gets a usable baseline after the reset.
        assert cp and cp["sha"] and cp["created"] is True
        assert cp["sha"] != first["sha"], "the shadow repo should have been reset first"

        # And the workspace is not poisoned: the lock is free for everyone else.
        finished, box = _call_with_timeout(lambda: wc.changed_since(str(ws), cp["sha"]), 10.0)
        assert finished, "changed_since() blocked on a lock checkpoint() never released"
        assert box.get("result") == []
        finished, box = _call_with_timeout(lambda: wc.reset(str(ws)), 10.0)
        assert finished and box.get("result") is True
    finally:
        # A deadlocked thread would hold this lock for the rest of the session.
        wc._LOCKS.pop(root, None)


@pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")
def test_reset_locked_helper_is_used_and_still_needs_no_lock(ws, data_dir):
    """The extracted helper must do the work WITHOUT taking the lock, so it is
    safe to call from a section that already holds it."""
    from src import workspace_checkpoints as wc
    root = wc._norm_root(str(ws))
    assert wc.checkpoint(str(ws), "t1")
    lock = wc._lock_for(root)
    with lock:
        finished, box = _call_with_timeout(lambda: wc._reset_locked(root), 10.0)
    assert finished, "_reset_locked() must not take the per-workspace lock"
    assert box.get("result") is True
    assert wc.status(str(ws))["present"] is False
