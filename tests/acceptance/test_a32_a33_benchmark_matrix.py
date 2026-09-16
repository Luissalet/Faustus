"""A32/A33 — benchmark matrix cost aggregation, completeness and judge
staleness (scripts/benchmark_matrix.py).

Real code under test: ``scripts.benchmark_matrix`` end to end via its CLI
``run()`` over a real JSONL fixture written to ``tmp_path`` (no fakes) —
mirrors ``tests/test_acceptance_run.py``'s own approach to
``scripts/acceptance_run.py``.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import benchmark_matrix
from tests.acceptance.conftest import record_evidence


def _write_cells(path: Path, cells: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for cell in cells:
            f.write(json.dumps(cell) + "\n")


@pytest.mark.acceptance("A32")
def test_failed_paid_attempt_followed_by_success_sums_both_known_costs(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    _write_cells(cells_path, [
        {
            "case_id": "A01", "system": "faustus", "model": "gpt-4o",
            "attempts": [
                {"ok": False, "cost": 0.02, "cost_status": "known"},
                {"ok": True, "cost": 0.03, "cost_status": "known"},
            ],
            "judged_output": "the final transcript",
            "judge": {
                "verdict": "pass", "judged_at": "2026-09-16T00:00:00Z",
                "input_hash": benchmark_matrix.sha256_text("the final transcript"),
            },
        },
    ])

    results = benchmark_matrix.build_matrix(benchmark_matrix.load_cells(cells_path))
    assert len(results) == 1
    cell = results[0]
    assert cell.present is True
    assert cell.attempts_count == 2
    # Both the failed paid attempt AND the successful one are counted, not
    # just the winner: 0.02 + 0.03, never 0.03 alone.
    assert cell.total_cost == pytest.approx(0.05)
    assert cell.verdict == "pass"
    record_evidence(request, case="A01", total_cost=cell.total_cost)


@pytest.mark.acceptance("A32")
def test_any_unknown_cost_in_the_cell_reports_unknown_never_zero(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    _write_cells(cells_path, [
        {
            "case_id": "A02", "system": "faustus", "model": "local-qwen",
            "attempts": [
                {"ok": False, "cost": 0.01, "cost_status": "known"},
                {"ok": False, "cost_status": "unknown"},
                {"ok": True, "cost_status": "free"},
            ],
            "judged_output": "x",
            "judge": None,
        },
    ])

    results = benchmark_matrix.build_matrix(benchmark_matrix.load_cells(cells_path))
    cell = results[0]
    # A single unknown-cost attempt poisons the cell total to the literal
    # string "unknown" — it must never silently collapse to 0, which would
    # under-report the one attempt whose real cost nobody could price.
    assert cell.total_cost == "unknown"
    assert cell.total_cost != 0
    record_evidence(request, case="A02", total_cost=cell.total_cost)


@pytest.mark.acceptance("A32")
def test_all_attempts_failing_still_reports_their_known_costs_and_fails(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    _write_cells(cells_path, [
        {
            "case_id": "A03", "system": "faustus", "model": "gpt-4o",
            "attempts": [
                {"ok": False, "cost": 0.10, "cost_status": "known"},
                {"ok": False, "cost": 0.10, "cost_status": "known"},
            ],
            "judged_output": "still broken",
            "judge": None,
        },
    ])

    results = benchmark_matrix.build_matrix(benchmark_matrix.load_cells(cells_path))
    cell = results[0]
    assert cell.total_cost == pytest.approx(0.20)
    assert cell.verdict == "fail"
    record_evidence(request, case="A03")


@pytest.mark.acceptance("A33")
def test_missing_cell_is_reported_incomplete_and_denominator_is_not_reduced(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    # Only one of the two expected (case, system, model) triples has a cell.
    _write_cells(cells_path, [
        {
            "case_id": "A01", "system": "faustus", "model": "gpt-4o",
            "attempts": [{"ok": True, "cost": 0.01, "cost_status": "known"}],
            "judged_output": "ok", "judge": None,
        },
    ])
    cells = benchmark_matrix.load_cells(cells_path)
    results = benchmark_matrix.build_matrix(
        cells, cases=["A01", "A02"], systems=["faustus"], models=["gpt-4o"],
    )
    assert len(results) == 2  # denominator stays 2, not shrunk to 1
    present = {(r.case_id, r.present) for r in results}
    assert ("A01", True) in present
    assert ("A02", False) in present

    report = benchmark_matrix.render_report_md(results)
    assert "INCOMPLETE" in report
    assert "A02" in report
    # Pass rate must be computed against the full expected denominator (2),
    # not against only the cells that happened to exist (1).
    assert "1/2" in report

    out_dir = tmp_path / "out"
    benchmark_matrix.render_summary_csv(results, out_dir / "summary.csv")
    with (out_dir / "summary.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    missing_row = next(r for r in rows if r["case_id"] == "A02")
    assert missing_row["verdict"] == "missing"
    record_evidence(request, case="A33-missing-cell")


@pytest.mark.acceptance("A33")
def test_judge_verdict_whose_input_hash_no_longer_matches_is_stale_not_a_pass(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    _write_cells(cells_path, [
        {
            "case_id": "A04", "system": "faustus", "model": "gpt-4o",
            "attempts": [{"ok": True, "cost": 0.01, "cost_status": "known"}],
            # The output on record now differs from what was judged (code
            # or prompt changed since); input_hash is the OLD text's hash.
            "judged_output": "new output after a code change",
            "judge": {
                "verdict": "pass", "judged_at": "2026-09-15T00:00:00Z",
                "input_hash": benchmark_matrix.sha256_text("old output before the change"),
            },
        },
    ])
    results = benchmark_matrix.build_matrix(benchmark_matrix.load_cells(cells_path))
    cell = results[0]
    assert cell.stale is True
    assert cell.verdict == "stale"
    assert cell.verdict != "pass"

    report = benchmark_matrix.render_report_md(results)
    assert "Stale judge verdicts" in report
    assert "0/1" in report  # the stale cell must not count toward the pass rate
    record_evidence(request, case="A33-stale-judge")


@pytest.mark.acceptance("A33")
def test_cli_run_writes_summary_and_report_and_exits_nonzero_when_incomplete(request, tmp_path):
    cells_path = tmp_path / "cells.jsonl"
    _write_cells(cells_path, [
        {
            "case_id": "A01", "system": "faustus", "model": "gpt-4o",
            "attempts": [{"ok": True, "cost": 0.0, "cost_status": "free"}],
            "judged_output": "ok", "judge": None,
        },
    ])
    out_dir = tmp_path / "run_out"
    exit_code = benchmark_matrix.run([
        "--cells", str(cells_path),
        "--cases", "A01,A02",
        "--systems", "faustus",
        "--models", "gpt-4o",
        "--out-dir", str(out_dir),
    ])
    assert exit_code == 1  # A02 is missing -> incomplete -> nonzero exit
    assert (out_dir / "summary.csv").exists()
    assert (out_dir / "report.md").exists()
    record_evidence(request, case="A33-cli", out_dir=str(out_dir))
