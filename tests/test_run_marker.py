import json
import os

from core import run_marker as rm


def test_clean_run_leaves_nothing(tmp_path):
    p = str(tmp_path / "running.json")
    assert rm.previous_run_report(p) is None
    rm.mark_running(p)
    assert rm.previous_run_report(p) is None, "our own marker is not a previous run"
    rm.clear(p)
    assert not os.path.exists(p)


def test_a_marker_left_by_another_pid_is_reported(tmp_path):
    p = tmp_path / "running.json"
    p.write_text(json.dumps({"pid": 999999, "started": 1790000000}), encoding="utf-8")
    msg = rm.previous_run_report(str(p))
    assert msg and "999999" in msg and "without shutting down" in msg
    rm.clear(str(p))
    assert p.exists(), "never remove another process's marker"


def test_fault_log_goes_to_crash_log(tmp_path):
    path = rm.enable_fault_log(str(tmp_path))
    assert path and path.endswith("crash.log") and os.path.exists(path)
    assert "started" in open(path, encoding="utf-8").read()