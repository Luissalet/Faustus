#!/usr/bin/env python3
"""acceptance_run.py — PR1's acceptance executor for TrueForge parity.

Runs every test marked ``@pytest.mark.acceptance("A0N")`` under ``tests/``
(real code, real routes — see docs/spec/trueforge/README.md's rule that
nothing closes by existing or by a mock of the module under test), and
writes one JSONL line PER CASE from docs/spec/trueforge/acceptance_cases.json
— all 36 by default, even the ones with no test yet, which get
``outcome: "NOT_EXECUTED"`` instead of being silently absent from the file.

    python3 scripts/acceptance_run.py
        Runs every acceptance-marked test under tests/, writes
        data/acceptance/<run_id>.jsonl (36 lines) and a Markdown summary to
        stdout.

    python3 scripts/acceptance_run.py --case A07
        Same, but only case A07's line is written (still NOT_EXECUTED if
        A07 has no test yet).

Each JSONL line has exactly the fields
docs/spec/trueforge/acceptance_cases.json's ``required_run_fields`` lists:
case_id, system, commit, config_hash, model, run_id, attempts, outcome,
evidence_refs, cost_status, total_cost, latency_ms. See
docs/api/acceptance_runner.md for the field semantics and
tests/test_acceptance_run.py for this script's own test (a fictitious case
against a throwaway test tree — never the real 36 — so the test stays fast
and does not depend on which of A01-A36 currently have tests).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES_PATH = REPO_ROOT / "docs" / "spec" / "trueforge" / "acceptance_cases.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "acceptance"

# Precedence used to reduce several test functions sharing one case_id (e.g.
# A01's (a)/(b)/(c) sub-scenarios) to a single case-level outcome: the worst
# signal wins, so a case is never reported healthier than its weakest test.
_OUTCOME_PRECEDENCE = ("failed", "xfailed", "skipped", "passed")


def load_case_ids(cases_path: Path) -> List[str]:
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    return [c["id"] for c in data["cases"]]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def effective_config_hash() -> str:
    """sha256 of the effective settings this run's tests would see, or "none"
    if settings could not be loaded (e.g. an unrelated import failure) —
    never a value invented to look like a real hash."""
    try:
        from src import settings as settings_mod

        effective = settings_mod.load_settings()
        encoded = json.dumps(effective, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except Exception:
        return "none"


class AcceptanceCollector:
    """In-process pytest plugin: maps each acceptance-marked item to its
    case_id at collection time, then records each item's outcome, duration
    and evidence_refs (from ``tests/acceptance/conftest.py::record_evidence``)
    as it runs. Injected into ``pytest.main(..., plugins=[...])`` exactly
    like ``tests/run_order_report.py::OrderShuffle`` — no real pytest run
    happens inside this module's own tests, only inside a script invocation
    or an injected fake (see tests/test_acceptance_run.py).
    """

    def __init__(self, case_filter: Optional[str] = None):
        self.case_filter = case_filter
        self.node_case: Dict[str, str] = {}
        self.reports: Dict[str, Dict[str, Any]] = {}

    def pytest_collection_modifyitems(self, items: list) -> None:
        kept = []
        for item in items:
            marker = item.get_closest_marker("acceptance")
            case_id = marker.args[0] if marker and marker.args else None
            if case_id is None:
                continue
            if self.case_filter and case_id != self.case_filter:
                continue
            self.node_case[item.nodeid] = case_id
            kept.append(item)
        items[:] = kept

    def pytest_runtest_logreport(self, report) -> None:
        nodeid = report.nodeid
        if nodeid not in self.node_case:
            return
        if hasattr(report, "wasxfail"):
            outcome = "xfailed" if report.outcome == "skipped" else "failed"
        elif report.passed:
            outcome = "passed"
        elif report.skipped:
            outcome = "skipped"
        elif report.failed:
            outcome = "failed"
        else:
            outcome = "unknown"

        refs = dict(getattr(report, "user_properties", []) or {}).get("evidence_refs") or {}
        duration_ms = int(round(float(getattr(report, "duration", 0.0)) * 1000))

        if report.when == "call":
            self.reports[nodeid] = {
                "outcome": outcome, "duration_ms": duration_ms, "evidence_refs": refs,
            }
        elif report.when == "setup" and nodeid not in self.reports and outcome in ("skipped", "failed"):
            # Fixture/setup failure or skip before the test body ever runs -
            # there will be no "call" phase report for this item at all.
            self.reports[nodeid] = {
                "outcome": outcome, "duration_ms": duration_ms, "evidence_refs": refs,
            }


def _aggregate_case(case_id: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    outcomes = [e["outcome"] for e in entries]
    agg_outcome = next((o for o in _OUTCOME_PRECEDENCE if o in outcomes), "unknown")
    latency_ms = sum(e["duration_ms"] for e in entries)
    evidence_refs = [
        {"nodeid": e["nodeid"], **(e["evidence_refs"] or {})} for e in entries
    ]
    model = next((e["evidence_refs"].get("model") for e in entries if e["evidence_refs"].get("model")), "scripted")
    cost_status = next(
        (e["evidence_refs"].get("cost_status") for e in entries if e["evidence_refs"].get("cost_status")),
        "unpriced",
    )
    total_cost = next(
        (e["evidence_refs"].get("total_cost") for e in entries if "total_cost" in e["evidence_refs"]),
        "unknown" if cost_status == "unknown" else 0,
    )
    return {
        "case_id": case_id, "model": model, "attempts": 1, "outcome": agg_outcome,
        "evidence_refs": evidence_refs, "cost_status": cost_status,
        "total_cost": total_cost, "latency_ms": latency_ms,
    }


def build_rows(
    case_ids: Sequence[str],
    collector: AcceptanceCollector,
    *,
    commit: str,
    config_hash: str,
    system: str = "faustus",
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    run_id = run_id or str(uuid.uuid4())
    by_case: Dict[str, List[Dict[str, Any]]] = {}
    for nodeid, case_id in collector.node_case.items():
        result = collector.reports.get(nodeid)
        entry = {
            "nodeid": nodeid,
            "outcome": (result or {}).get("outcome", "failed"),
            "duration_ms": (result or {}).get("duration_ms", 0),
            "evidence_refs": (result or {}).get("evidence_refs") or {},
        }
        by_case.setdefault(case_id, []).append(entry)

    rows: List[Dict[str, Any]] = []
    for case_id in case_ids:
        entries = by_case.get(case_id)
        if not entries:
            row = {
                "case_id": case_id, "system": system, "commit": commit,
                "config_hash": config_hash, "model": "scripted", "run_id": run_id,
                "attempts": 0, "outcome": "NOT_EXECUTED", "evidence_refs": [],
                "cost_status": "none", "total_cost": 0, "latency_ms": 0,
            }
        else:
            agg = _aggregate_case(case_id, entries)
            row = {
                "system": system, "commit": commit, "config_hash": config_hash,
                "run_id": run_id, **agg,
            }
        rows.append(row)
    return rows


def write_jsonl(rows: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def render_markdown_summary(rows: List[Dict[str, Any]]) -> str:
    lines = ["| case_id | outcome | model | attempts | latency_ms |",
             "|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['case_id']} | {row['outcome']} | {row['model']} | "
            f"{row['attempts']} | {row['latency_ms']} |"
        )
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines.append("")
    lines.append(f"**{len(rows)} case(s)** — {summary}")
    return "\n".join(lines)


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    pytest_main: Optional[Callable[..., int]] = None,
) -> int:
    """Parse ``argv``, run the acceptance suite, write the JSONL run, print
    the Markdown summary. ``pytest_main`` is injected so tests can drive
    this against a throwaway test tree without a nested real pytest run of
    the whole suite (mirrors tests/run_order_report.py::run)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=None, help="only run/report this one case_id")
    parser.add_argument("--cases-path", default=str(DEFAULT_CASES_PATH),
                        help="acceptance_cases.json to read the case id list from")
    parser.add_argument("--target", default="tests",
                        help="where pytest looks for acceptance-marked tests")
    parser.add_argument("--out", default="", help="JSONL output path (default: data/acceptance/<run_id>.jsonl)")
    parser.add_argument("--system", default="faustus")
    args = parser.parse_args(argv)

    cases_path = Path(args.cases_path)
    case_ids = load_case_ids(cases_path)
    if args.case:
        if args.case not in case_ids:
            parser.error(f"--case {args.case!r} is not in {cases_path}")
        case_ids = [args.case]

    if pytest_main is None:
        import pytest as _pytest
        pytest_main = _pytest.main

    collector = AcceptanceCollector(case_filter=args.case)
    pytest_args = [args.target, "-m", "acceptance", "-p", "no:cacheprovider", "-q"]
    print(f"[acceptance_run] running: pytest {' '.join(pytest_args)}")
    exit_code = int(pytest_main(pytest_args, plugins=[collector]))
    print(f"[acceptance_run] pytest exit code: {exit_code}")

    run_id = str(uuid.uuid4())
    commit = git_commit()
    config_hash = effective_config_hash()
    rows = build_rows(
        case_ids, collector, commit=commit, config_hash=config_hash,
        system=args.system, run_id=run_id,
    )

    out_path = Path(args.out) if args.out else (DEFAULT_OUT_DIR / f"{run_id}.jsonl")
    write_jsonl(rows, out_path)
    print(f"[acceptance_run] wrote {out_path}")
    print(render_markdown_summary(rows))

    executed_failed = any(r["outcome"] == "failed" for r in rows)
    return 1 if executed_failed else 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
