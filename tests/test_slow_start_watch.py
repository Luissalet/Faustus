import faulthandler
import sys
import time

import pytest

from core import run_marker


@pytest.fixture(autouse=True)
def restore_faulthandler():
    """enable_fault_log points the process-wide fault handler at a tmp file;
    point it back at stderr so a later crash is not written into a deleted
    file."""
    yield
    run_marker.startup_finished()
    try:
        faulthandler.enable(sys.__stderr__, all_threads=True)
    except Exception:  # noqa: BLE001
        faulthandler.disable()


def test_a_slow_start_leaves_every_threads_stack_in_crash_log(tmp_path, monkeypatch):
    monkeypatch.setattr(run_marker, "_fault_file", None)
    path = run_marker.enable_fault_log(str(tmp_path))
    try:
        assert run_marker.watch_slow_startup(0.3)
        time.sleep(0.8)
    finally:
        run_marker.startup_finished()
    text = open(path, encoding="utf-8").read()
    assert "slow-start watch armed" in text
    assert "Thread" in text or "Current thread" in text


def test_a_finished_start_leaves_no_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(run_marker, "_fault_file", None)
    path = run_marker.enable_fault_log(str(tmp_path))
    assert run_marker.watch_slow_startup(0.5)
    run_marker.startup_finished()
    time.sleep(0.8)
    assert "Current thread" not in open(path, encoding="utf-8").read()
