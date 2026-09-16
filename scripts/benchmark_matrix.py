#!/usr/bin/env python3
"""benchmark_matrix.py — {case × system × model} benchmark matrix (A32/A33).

Builds on ``scripts/acceptance_run.py``'s per-run JSONL shape (one row per
case, with ``attempts``/``outcome``/``cost_status``/``total_cost``) but adds
the axis that a single acceptance run does not carry: the SAME case run
against more than one system/model, so a scorecard can compare them
honestly (TF01/TF24, ``docs/spec/paridad/MATRIZ_PARIDAD.md`` row 21).

Input: a JSONL file of *cells*, one line per (case_id, system, model):

    {
      "case_id": "A01", "system": "faustus", "model": "gpt-4o",
      "attempts": [
        {"ok": false, "cost": 0.02, "cost_status": "known"},
        {"ok": true,  "cost": 0.03, "cost_status": "known"}
      ],
      "judged_output": "<the exact transcript/output the judge graded>",
      "judge": {"verdict": "pass", "judged_at": "2026-09-16T00:00:00Z",
                "input_hash": "<sha256 of judged_output at judging time>"}
    }

``attempts[i].cost_status`` is one of ``known`` (a real priced cost in
``cost``), ``free`` (ran at zero cost, still a known number — 0), or
``unknown`` (cost could not be determined; ``cost`` is ``None``/absent).

A32 — cost aggregation
-----------------------
A cell's total cost sums EVERY attempt whose cost is known (``known`` or
``free``) — a failed paid attempt followed by a successful one counts BOTH,
never just the winning attempt or just the first. If ANY attempt in the
cell has ``cost_status == "unknown"``, the cell's total is the string
``"unknown"`` — never silently coerced to ``0``, which would under-report
real spend. This mirrors ``scripts/acceptance_run.py``'s own rule (never
report ``0.0`` for a cost nothing priced).

A33 — completeness and judge staleness
---------------------------------------
* The report is built against an EXPECTED set of (case, system, model)
  triples (``--cases``/``--systems``/``--models``, or all combinations
  implied by the cells file when none are given). Any expected triple with
  no cell in the input is ``MISSING`` in the report; the pass RATE's
  denominator is the full expected count — a missing cell is never dropped
  from it, so it can only ever pull the rate down, not make it look smaller
  by shrinking the count of cases considered.
* A cell's ``judge.input_hash`` is checked against
  ``sha256(cell["judged_output"])`` computed now. A mismatch — the judged
  text has since changed (code changed, prompt changed, re-run) — makes the
  verdict ``stale`` regardless of what ``judge.verdict`` says, and ``stale``
  never counts as a pass.

Outputs (via ``--out-dir``, default ``data/benchmark/<run_id>/``):
``summary.csv`` (one row per expected triple) and ``report.md`` (pass rate,
INCOMPLETE flag, missing/stale detail).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "benchmark"

COST_STATUS_VALUES = ("known", "free", "unknown")
_KNOWN_COST_STATUSES = frozenset({"known", "free"})


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CellResult:
    case_id: str
    system: str
    model: str
    present: bool
    attempts_count: int = 0
    total_cost: Any = 0  # float, or the literal string "unknown"
    verdict: str = "missing"  # pass | fail | stale | missing
    stale: bool = False
    notes: List[str] = field(default_factory=list)


def load_cells(path: Path) -> List[Dict[str, Any]]:
    """Parse the JSONL cells file. Blank lines are skipped; a malformed line
    raises with its 1-based line number rather than corrupting the matrix
    silently."""
    cells: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                cells.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON cell: {exc}") from exc
    return cells


def compute_total_cost(attempts: Sequence[Dict[str, Any]]) -> Any:
    """Sum every attempt with a known cost (``known`` or ``free`` —
    ``free`` contributes 0 but is still a KNOWN number). A single attempt
    with ``cost_status == "unknown"`` makes the whole cell's total the
    string ``"unknown"`` — a paid attempt that failed and was retried still
    has both its costs counted; nothing is ever silently treated as free."""
    if any(a.get("cost_status") == "unknown" for a in attempts):
        return "unknown"
    total = 0.0
    for a in attempts:
        status = a.get("cost_status")
        if status not in _KNOWN_COST_STATUSES:
            raise ValueError(
                f"attempt has unrecognised cost_status={status!r} "
                f"(expected one of {COST_STATUS_VALUES})"
            )
        if status == "free":
            continue
        cost = a.get("cost")
        if cost is None:
            raise ValueError("attempt cost_status='known' but cost is null")
        total += float(cost)
    return total


def attempts_verdict(attempts: Sequence[Dict[str, Any]]) -> str:
    """Fallback verdict when the cell carries no judge: pass if ANY attempt
    in the cell succeeded (the same "did it eventually get there" reading
    acceptance_run.py's outcome precedence uses for a case with retries)."""
    if not attempts:
        return "fail"
    return "pass" if any(bool(a.get("ok")) for a in attempts) else "fail"


def judge_verdict(cell: Dict[str, Any]) -> Tuple[str, bool]:
    """Returns (verdict, stale). A judge whose recorded ``input_hash``
    does not match sha256(judged_output) computed now is stale — its
    verdict is discarded and the cell reports 'stale', which never counts
    as a pass, regardless of what the recorded verdict says."""
    judge = cell.get("judge")
    if not judge:
        return attempts_verdict(cell.get("attempts") or []), False
    judged_output = cell.get("judged_output", "")
    current_hash = sha256_text(str(judged_output))
    recorded_hash = judge.get("input_hash")
    if recorded_hash != current_hash:
        return "stale", True
    verdict = str(judge.get("verdict") or "fail")
    return verdict, False


def aggregate_cell(cell: Dict[str, Any]) -> CellResult:
    attempts = cell.get("attempts") or []
    verdict, stale = judge_verdict(cell)
    notes: List[str] = []
    if stale:
        notes.append(
            f"judge.input_hash={cell.get('judge', {}).get('input_hash')!r} does not match "
            f"sha256(judged_output)={sha256_text(str(cell.get('judged_output', '')))!r} — stale verdict"
        )
    return CellResult(
        case_id=cell["case_id"], system=cell["system"], model=cell["model"],
        present=True, attempts_count=len(attempts),
        total_cost=compute_total_cost(attempts), verdict=verdict, stale=stale,
        notes=notes,
    )


def expected_triples(
    cells: Sequence[Dict[str, Any]],
    cases: Optional[Sequence[str]],
    systems: Optional[Sequence[str]],
    models: Optional[Sequence[str]],
) -> List[Tuple[str, str, str]]:
    """The full set of (case, system, model) the report must account for.
    Explicit --cases/--systems/--models win; otherwise it's every axis
    value seen anywhere in the cells file, crossed (NOT just the triples
    actually present) — a case×system×model combination nobody ran a cell
    for is exactly the MISSING case this function exists to surface."""
    seen_cases = sorted({c["case_id"] for c in cells}) if cases is None else list(cases)
    seen_systems = sorted({c["system"] for c in cells}) if systems is None else list(systems)
    seen_models = sorted({c["model"] for c in cells}) if models is None else list(models)
    return [
        (c, s, m)
        for c in seen_cases
        for s in seen_systems
        for m in seen_models
    ]


def build_matrix(
    cells: Sequence[Dict[str, Any]],
    *,
    cases: Optional[Sequence[str]] = None,
    systems: Optional[Sequence[str]] = None,
    models: Optional[Sequence[str]] = None,
) -> List[CellResult]:
    by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {
        (c["case_id"], c["system"], c["model"]): c for c in cells
    }
    triples = expected_triples(cells, cases, systems, models)
    results: List[CellResult] = []
    for triple in triples:
        raw = by_key.get(triple)
        if raw is None:
            case_id, system, model = triple
            results.append(CellResult(case_id=case_id, system=system, model=model, present=False))
        else:
            results.append(aggregate_cell(raw))
    return results


def render_summary_csv(results: Sequence[CellResult], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "system", "model", "verdict", "attempts", "total_cost", "notes"])
        for r in results:
            writer.writerow([
                r.case_id, r.system, r.model, r.verdict, r.attempts_count,
                r.total_cost, "; ".join(r.notes),
            ])


def render_report_md(results: Sequence[CellResult]) -> str:
    total = len(results)
    passed = sum(1 for r in results if r.verdict == "pass")
    missing = [r for r in results if not r.present]
    stale = [r for r in results if r.stale]
    incomplete = bool(missing)
    rate = (passed / total) if total else 0.0

    lines = ["# Benchmark matrix report", ""]
    status = "INCOMPLETE" if incomplete else "COMPLETE"
    lines.append(f"**Status:** {status}  ")
    lines.append(
        f"**Pass rate:** {passed}/{total} = {rate:.1%} "
        f"(denominator is every expected cell — missing cells are NOT dropped from it)"
    )
    lines.append("")
    if missing:
        lines.append(f"## Missing cells ({len(missing)})")
        for r in missing:
            lines.append(f"- {r.case_id} × {r.system} × {r.model} — no cell in the input")
        lines.append("")
    if stale:
        lines.append(f"## Stale judge verdicts ({len(stale)})")
        for r in stale:
            lines.append(f"- {r.case_id} × {r.system} × {r.model} — {'; '.join(r.notes)}")
        lines.append("")
    lines.append("## All cells")
    lines.append("| case_id | system | model | verdict | attempts | total_cost |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| {r.case_id} | {r.system} | {r.model} | {r.verdict} | "
            f"{r.attempts_count if r.present else '—'} | {r.total_cost if r.present else '—'} |"
        )
    return "\n".join(lines) + "\n"


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", required=True, help="JSONL file of matrix cells")
    parser.add_argument("--cases", default=None, help="comma-separated case ids; default = seen in cells")
    parser.add_argument("--systems", default=None, help="comma-separated systems; default = seen in cells")
    parser.add_argument("--models", default=None, help="comma-separated models; default = seen in cells")
    parser.add_argument("--out-dir", default="", help="default: data/benchmark/<run_id>/")
    args = parser.parse_args(argv)

    cells = load_cells(Path(args.cells))
    cases = args.cases.split(",") if args.cases else None
    systems = args.systems.split(",") if args.systems else None
    models = args.models.split(",") if args.models else None

    results = build_matrix(cells, cases=cases, systems=systems, models=models)

    out_dir = Path(args.out_dir) if args.out_dir else (DEFAULT_OUT_DIR / str(uuid.uuid4()))
    render_summary_csv(results, out_dir / "summary.csv")
    report = render_report_md(results)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"[benchmark_matrix] wrote {out_dir / 'summary.csv'} and {out_dir / 'report.md'}")
    print(report)

    missing = any(not r.present for r in results)
    return 1 if missing else 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
