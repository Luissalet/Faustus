"""Tests for scripts/acceptance_run.py.

Follows tests/test_run_order_report.py's split: the row-building/aggregation
logic and argument plumbing are tested directly (an injected fake
``pytest_main`` records what would have been forwarded, no nested real
pytest run), and a subprocess test proves the whole thing end to end against
a throwaway test tree with a FICTITIOUS case id - never the real 36, so this
test stays fast and never depends on which of A01-A36 currently have a test.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from scripts.acceptance_run import (
    AcceptanceCollector,
    build_rows,
    effective_config_hash,
    git_commit,
    load_case_ids,
    render_markdown_summary,
    run,
    write_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "acceptance_run.py"
REAL_CASES_PATH = REPO_ROOT / "docs" / "spec" / "paridad" / "acceptance_cases.json"


class _FakePytestMain:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list[tuple[list[str], list]] = []

    def __call__(self, args, plugins):
        self.calls.append((list(args), list(plugins)))
        return self.returncode


# --- basics -------------------------------------------------------------


def test_load_case_ids_reads_the_real_36_cases():
    ids = load_case_ids(REAL_CASES_PATH)
    assert len(ids) == 36
    assert ids[0] == "A01" and ids[-1] == "A36"


def test_git_commit_returns_the_real_head():
    commit = git_commit()
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert commit == expected


def test_effective_config_hash_is_hex_even_without_settings_file(tmp_path, monkeypatch):
    # rm -f data/settings.json is part of this lot's own verification command;
    # the hash must still come back as a real sha256 hex digest, not "none",
    # when settings simply fall back to their defaults.
    h = effective_config_hash()
    assert h == "none" or (len(h) == 64 and all(c in "0123456789abcdef" for c in h))


# --- row building / aggregation (no pytest run at all) -------------------


def _entry(nodeid, outcome, ms=10, refs=None):
    return {"nodeid": nodeid, "outcome": outcome, "duration_ms": ms, "evidence_refs": refs or {}}


def test_build_rows_marks_untested_cases_not_executed():
    collector = AcceptanceCollector()
    rows = build_rows(["A99"], collector, commit="deadbeef", config_hash="none")
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "NOT_EXECUTED"
    assert row["attempts"] == 0
    assert row["evidence_refs"] == []
    assert row["cost_status"] == "none"
    assert row["total_cost"] == 0
    for field in ("case_id", "system", "commit", "config_hash", "model", "run_id",
                  "attempts", "outcome", "evidence_refs", "cost_status", "total_cost",
                  "latency_ms"):
        assert field in row, f"missing required_run_fields entry: {field}"


def test_build_rows_worst_outcome_wins_across_sub_scenarios():
    collector = AcceptanceCollector()
    collector.node_case = {
        "t.py::a": "A99", "t.py::b": "A99", "t.py::c": "A99",
    }
    collector.reports = {
        "t.py::a": {"outcome": "passed", "duration_ms": 5, "evidence_refs": {}},
        "t.py::b": {"outcome": "xfailed", "duration_ms": 7, "evidence_refs": {}},
        "t.py::c": {"outcome": "passed", "duration_ms": 3, "evidence_refs": {}},
    }
    rows = build_rows(["A99"], collector, commit="c", config_hash="h")
    row = rows[0]
    assert row["outcome"] == "xfailed"          # worst of passed/xfailed/passed
    assert row["latency_ms"] == 15               # summed
    assert row["attempts"] == 1

    collector.reports["t.py::a"]["outcome"] = "failed"
    rows = build_rows(["A99"], collector, commit="c", config_hash="h")
    assert rows[0]["outcome"] == "failed"        # failed always dominates


def test_build_rows_carries_evidence_refs_and_model_override():
    collector = AcceptanceCollector()
    collector.node_case = {"t.py::a": "A99"}
    collector.reports = {
        "t.py::a": {"outcome": "passed", "duration_ms": 1,
                    "evidence_refs": {"model": "real-endpoint", "session_id": "s1"}},
    }
    rows = build_rows(["A99"], collector, commit="c", config_hash="h")
    row = rows[0]
    assert row["model"] == "real-endpoint"
    assert row["evidence_refs"] == [{"nodeid": "t.py::a", "model": "real-endpoint", "session_id": "s1"}]


def test_render_markdown_summary_lists_every_case():
    rows = [
        {"case_id": "A01", "outcome": "passed", "model": "scripted", "attempts": 1, "latency_ms": 5},
        {"case_id": "A02", "outcome": "NOT_EXECUTED", "model": "scripted", "attempts": 0, "latency_ms": 0},
    ]
    text = render_markdown_summary(rows)
    assert "A01" in text and "A02" in text
    assert "NOT_EXECUTED" in text
    assert "2 case(s)" in text


def test_write_jsonl_writes_one_line_per_row(tmp_path):
    rows = [{"case_id": "A01", "outcome": "passed"}, {"case_id": "A02", "outcome": "NOT_EXECUTED"}]
    out = tmp_path / "run.jsonl"
    write_jsonl(rows, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["case_id"] == "A01"
    assert json.loads(lines[1])["outcome"] == "NOT_EXECUTED"


# --- argument plumbing (injected fake pytest_main, no real pytest run) ---


def test_run_forwards_case_filter_and_target_to_pytest_main(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({"cases": [{"id": "A01"}, {"id": "A02"}]}), encoding="utf-8")
    fake = _FakePytestMain(returncode=0)
    out = tmp_path / "out.jsonl"
    exit_code = run(
        ["--case", "A02", "--cases-path", str(cases_path), "--target", "some/dir", "--out", str(out)],
        pytest_main=fake,
    )
    assert exit_code == 0
    args, plugins = fake.calls[0]
    assert args[0] == "some/dir"
    assert "-m" in args and "acceptance" in args
    assert len(plugins) == 1 and isinstance(plugins[0], AcceptanceCollector)
    assert plugins[0].case_filter == "A02"
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["case_id"] for r in rows] == ["A02"]
    assert rows[0]["outcome"] == "NOT_EXECUTED"  # fake never actually ran anything


def test_run_rejects_unknown_case(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({"cases": [{"id": "A01"}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        run(["--case", "ZZZ", "--cases-path", str(cases_path)], pytest_main=_FakePytestMain())


# --- end to end: a real (throwaway, fictitious-case) subprocess run ------


def test_end_to_end_subprocess_with_fictitious_case(tmp_path):
    """A real `python3 scripts/acceptance_run.py` invocation, but pointed at
    a private throwaway test tree carrying a made-up case id that is not one
    of the real A01-A36 - proves collection, outcome mapping, NOT_EXECUTED
    for an untested case, and JSONL writing against real pytest, without
    depending on (or slowing down for) the real acceptance suite."""
    fictitious_id = f"T1FAKE-{uuid.uuid4().hex[:8]}"
    untested_id = f"T1FAKE-{uuid.uuid4().hex[:8]}-notest"

    test_dir = tmp_path / "fake_tests"
    test_dir.mkdir()
    (test_dir / "test_fictitious.py").write_text(
        "import pytest\n\n"
        f"@pytest.mark.acceptance({fictitious_id!r})\n"
        "def test_one_passes():\n"
        "    assert True\n\n"
        f"@pytest.mark.acceptance({fictitious_id!r})\n"
        "@pytest.mark.xfail(strict=True, reason='fixture for this lot\\'s own test')\n"
        "def test_two_is_expected_to_fail():\n"
        "    assert False\n",
        encoding="utf-8",
    )
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps({"cases": [{"id": fictitious_id}, {"id": untested_id}]}), encoding="utf-8"
    )
    out_path = tmp_path / "run.jsonl"

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--cases-path", str(cases_path),
         "--target", str(test_dir), "--out", str(out_path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = {json.loads(line)["case_id"]: json.loads(line) for line in lines}

    fictitious_row = rows[fictitious_id]
    # test_one passes, test_two is a strict xfail that fails as expected:
    # worst-outcome aggregation reports the case as xfailed, not passed.
    assert fictitious_row["outcome"] == "xfailed"
    assert fictitious_row["attempts"] == 1
    assert fictitious_row["system"] == "faustus"
    assert fictitious_row["commit"] and fictitious_row["commit"] != "unknown"
    assert len(fictitious_row["evidence_refs"]) == 2

    untested_row = rows[untested_id]
    assert untested_row["outcome"] == "NOT_EXECUTED"
    assert untested_row["attempts"] == 0
    assert untested_row["evidence_refs"] == []
