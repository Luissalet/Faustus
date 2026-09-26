"""Startup recovery learns a finished log's status from its tail, without
parsing every event (a finished exam run is tens of MB)."""
import json
import os

from src import agent_runs


def _log(path, status_lines, n_events=0, tail_status=None):
    with open(path, "w", encoding="utf-8") as f:
        for line in status_lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        pad = "x" * 200
        for i in range(n_events):
            f.write(json.dumps({"seq": i, "ev": f'data: {{"delta": "{pad}"}}'}) + "\n")
        if tail_status:
            f.write(json.dumps({"status": tail_status, "ts": 1.0}) + "\n")


def test_peek_reads_the_last_status_from_the_tail(tmp_path):
    p = tmp_path / "a.jsonl"
    _log(p, [{"status": "running", "run_id": "r", "session_id": "s"}], n_events=2000, tail_status="done")
    assert os.path.getsize(p) > agent_runs._PEEK_TAIL_BYTES
    assert agent_runs._peek_status(str(p)) == "done"


def test_peek_gives_none_for_a_running_log(tmp_path):
    p = tmp_path / "b.jsonl"
    _log(p, [{"status": "running", "run_id": "r", "session_id": "s"}], n_events=2000)
    assert agent_runs._peek_status(str(p)) is None
    small = tmp_path / "c.jsonl"
    _log(small, [{"status": "running", "run_id": "r", "session_id": "s"}], n_events=3)
    assert agent_runs._peek_status(str(small)) == "running"


def test_recovery_skips_full_reads_of_finished_logs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    _log(runs / "done1.jsonl", [{"status": "running", "run_id": "r1", "session_id": "s1"}], 2000, "done")
    _log(runs / "run2.jsonl", [{"status": "running", "run_id": "r2", "session_id": "s2"}], 5)
    monkeypatch.setattr(agent_runs, "_runs_dir", lambda: str(runs))
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default, raising=False)
    read = []
    real = agent_runs._read_log
    monkeypatch.setattr(agent_runs, "_read_log", lambda path: (read.append(os.path.basename(path)), real(path))[1])
    out = agent_runs.recover_interrupted_runs(None)
    assert read == ["run2.jsonl"]
    assert [e["session_id"] for e in out] == ["s2"]
