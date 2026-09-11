"""Lote 67 — OPS-07: `src.external_worker.run_task` now persists the
`total_cost_usd` it already computed, so `cleanup_service.remote_cost_report`
(which reads `DATA_DIR/runs/*.jsonl` for exactly this event) stops always
answering `unknown_period=True`.

Before this lote, no writer existed outside `cleanup_service.py`'s own
reader (only its docstring named the event shape it was waiting for). This
test proves the writer first (demonstrating the gap: without
`_append_run_cost_event`, `remote_cost_report` over this same runs dir stays
`unknown_period=True`), then proves the roundtrip.
"""
from __future__ import annotations

import json
import os

from src import cleanup_service
from src import external_worker


def test_append_run_cost_event_writes_a_line_remote_cost_report_can_read(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    # Before any writer runs: honestly unknown, never a fabricated 0 (the
    # existing, already-correct behaviour this lote's gap analysis names).
    before = cleanup_service.remote_cost_report()
    assert before["unknown_period"] is True
    assert before["known_total_usd"] is None

    external_worker._append_run_cost_event(
        cost_usd=0.0412, runner_key="claude-cli", run_id="run-1",
        owner="alice", seconds=12.5,
    )

    runs_dir = tmp_path / "runs"
    files = list(runs_dir.glob("*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert line["event"] == "external_worker_result"
    assert line["total_cost_usd"] == 0.0412
    assert line["runner"] == "claude-cli"
    assert line["run_id"] == "run-1"
    assert isinstance(line["ts"], float)

    after = cleanup_service.remote_cost_report()
    assert after["unknown_period"] is False
    assert after["known_total_usd"] == 0.0412


def test_append_run_cost_event_accumulates_across_calls(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    external_worker._append_run_cost_event(
        cost_usd=1.0, runner_key="a", run_id="r1", owner="u", seconds=1.0,
    )
    external_worker._append_run_cost_event(
        cost_usd=2.5, runner_key="a", run_id="r2", owner="u", seconds=1.0,
    )
    report = cleanup_service.remote_cost_report()
    assert report["known_total_usd"] == 3.5
    assert report["unknown_period"] is False


def test_append_run_cost_event_never_raises_when_the_runs_dir_is_unwritable(monkeypatch):
    """Best-effort: a failed cost write must never fail the run's own
    result (external_worker.run_task's caller gets its answer either way)."""
    monkeypatch.setattr(external_worker.os, "makedirs",
                         lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    external_worker._append_run_cost_event(
        cost_usd=1.0, runner_key="a", run_id="r1", owner="u", seconds=1.0,
    )  # must not raise
