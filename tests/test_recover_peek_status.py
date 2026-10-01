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
    """A terminal log is read in full once (commit cdcd04c0: it may hold an
    uncertain effect that needs a notice, so a status alone cannot clear it).
    Recovery then records that it was scanned, and later startups never open
    it again - the property the tail peek was introduced for."""
    runs = tmp_path / "runs"
    runs.mkdir()
    _log(runs / "done1.jsonl", [{"status": "running", "run_id": "r1", "session_id": "s1"}], 2000, "done")
    _log(runs / "run2.jsonl", [{"status": "running", "run_id": "r2", "session_id": "s2"}], 5)
    monkeypatch.setattr(agent_runs, "_runs_dir", lambda: str(runs))
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default, raising=False)
    read = []
    real = agent_runs._read_log
    monkeypatch.setattr(agent_runs, "_read_log", lambda path, **k: (read.append(os.path.basename(path)), real(path, **k))[1])
    out = agent_runs.recover_interrupted_runs(None)
    assert read == ["done1.jsonl", "run2.jsonl"]
    assert [e["session_id"] for e in out] == ["s2"]
    assert agent_runs._read_status_sidecar_parts(str(runs / "done1.jsonl")) == ("done", True)
    # Next startup: the finished log is not opened; the interrupted one was
    # closed by the first pass and is scanned once like any terminal log.
    read.clear()
    assert agent_runs.recover_interrupted_runs(None) == []
    assert "done1.jsonl" not in read
    read.clear()
    assert agent_runs.recover_interrupted_runs(None) == []
    assert read == []


def test_a_finished_log_with_a_status_sidecar_is_never_opened(tmp_path, monkeypatch):
    """The antivirus holds the first open of a big changed file for minutes
    (26-09: 145 s for a 107 MB exam log); the sidecar answers without it."""
    runs = tmp_path / "runs"
    runs.mkdir()
    big = runs / "exam.jsonl"
    _log(big, [{"status": "running", "run_id": "r1", "session_id": "s1"}], 50, "done")
    # Only a sidecar that vouches for a completed effect scan lets recovery skip
    # the log (a plain status sidecar still gets one full read; see above).
    agent_runs._write_status_sidecar(str(big), "done", effects_scanned=True)
    monkeypatch.setattr(agent_runs, "_runs_dir", lambda: str(runs))
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default, raising=False)
    opened = []
    monkeypatch.setattr(agent_runs, "_peek_status", lambda path: opened.append(path) or None)
    monkeypatch.setattr(agent_runs, "_read_log", lambda path, **k: opened.append(path) or {"status": "running"})
    assert agent_runs.recover_interrupted_runs(None) == []
    assert opened == []


def test_a_plain_status_sidecar_does_not_skip_the_effect_scan(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    log = runs / "exam.jsonl"
    _log(log, [{"status": "running", "run_id": "r1", "session_id": "s1"}], 50, "done")
    agent_runs._write_status_sidecar(str(log), "done")
    monkeypatch.setattr(agent_runs, "_runs_dir", lambda: str(runs))
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: default, raising=False)
    opened = []
    real = agent_runs._read_log
    monkeypatch.setattr(agent_runs, "_read_log", lambda path, **k: (opened.append(path), real(path, **k))[1])
    assert agent_runs.recover_interrupted_runs(None) == []
    assert opened == [str(log)]


def test_a_stale_sidecar_falls_back_to_the_log(tmp_path):
    p = tmp_path / "x.jsonl"
    _log(p, [{"status": "running", "run_id": "r", "session_id": "s"}], 3, "done")
    agent_runs._write_status_sidecar(str(p), "done")
    assert agent_runs._read_status_sidecar(str(p)) == "done"
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"seq": 99, "ev": "more"}\n')
    assert agent_runs._read_status_sidecar(str(p)) is None, "a log that grew after its sidecar is read"


def test_the_run_log_keeps_the_sidecar_current(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runs, "_runs_dir", lambda: str(tmp_path))
    # `_run_log_path` needs a 32-hex run id and a start time (log names are
    # session-hashed and time-stamped since the run-log namespace change).
    run = type("R", (), {"run_id": "9" * 32, "lane": "chat", "label": "t", "started_at": 1.0})()
    log = agent_runs._RunLog("sess-9", run)
    assert agent_runs._read_status_sidecar(log.path) is None, "a running log has no sidecar yet"
    log.finish("done")
    assert agent_runs._read_status_sidecar(log.path) == "done"
