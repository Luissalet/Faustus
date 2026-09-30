"""src/check_scoring.py — a health score that knows how much it has actually checked.

A review is a list of checks. Each check has a status and a severity:

    status    pass | fail | unknown | not_applicable
    severity  high (weight 5) | medium (3) | low (1) | info (0)

A plain pass rate over what ran says "100 %" for a review where most checks
could not be made. This module separates the two questions:

* **health**   — of the weight that was actually assessed (pass + fail), how
  much passed. `not_applicable` checks are out of the picture entirely.
* **coverage** — of the weight that applies (everything but `not_applicable`),
  how much was assessed. `unknown` checks lower coverage, never health.

The coverage decides what may be said:

    coverage >= 80 %   "score"        health is reported as the score
    60 % <= coverage   "provisional"  health is reported, marked provisional
    coverage < 60 %    "no_score"     no number is given, only what is missing

A score never decides a blocking failure: every failed `high` check is listed
under `blockers` whatever the health is, and `blocked` is true while there is
one. Reviews made only of `info` checks (weight 0) fall back to counting.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

STATUSES = ("pass", "fail", "unknown", "not_applicable")
SEVERITY_WEIGHTS: Dict[str, int] = {"high": 5, "medium": 3, "low": 1, "info": 0}
SCORE_COVERAGE = 0.80
PROVISIONAL_COVERAGE = 0.60
MAX_CHECKS = 500

_STATUS_ALIASES = {
    "passed": "pass", "ok": "pass", "green": "pass", "success": "pass", "true": "pass",
    "failed": "fail", "failure": "fail", "error": "fail", "red": "fail", "false": "fail",
    "skipped": "unknown", "inconclusive": "unknown", "not_run": "unknown", "missing": "unknown",
    "n/a": "not_applicable", "na": "not_applicable", "not applicable": "not_applicable",
}
_SEVERITY_ALIASES = {"critical": "high", "blocker": "high", "major": "medium", "moderate": "medium",
                     "minor": "low", "none": "info", "note": "info"}


def normalize_status(value: Any) -> str:
    text = str(value if value is not None else "").strip().lower().replace("-", "_")
    text = _STATUS_ALIASES.get(text, text)
    return text if text in STATUSES else "unknown"


def normalize_severity(value: Any) -> str:
    text = str(value if value is not None else "").strip().lower()
    text = _SEVERITY_ALIASES.get(text, text)
    return text if text in SEVERITY_WEIGHTS else "medium"


def normalize_check(raw: Any, index: int = 0) -> Dict[str, Any]:
    """A check dict with a clean name, status and severity (never raises)."""
    data = raw if isinstance(raw, dict) else {"name": str(raw)}
    return {
        "name": str(data.get("name") or data.get("id") or f"check {index + 1}")[:200],
        "status": normalize_status(data.get("status")),
        "severity": normalize_severity(data.get("severity")),
        "note": str(data.get("note") or data.get("detail") or "")[:300],
    }


def _ratio(num: float, den: float) -> Optional[float]:
    return None if den <= 0 else num / den


def score_checks(checks: Iterable[Any]) -> Dict[str, Any]:
    """Score a list of checks. The result is plain data, ready for a report."""
    rows = [normalize_check(c, i) for i, c in enumerate(list(checks or [])[:MAX_CHECKS])]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    applicable = [r for r in rows if r["status"] != "not_applicable"]
    assessed = [r for r in applicable if r["status"] in ("pass", "fail")]
    passed = [r for r in assessed if r["status"] == "pass"]

    weights = {k: v for k, v in SEVERITY_WEIGHTS.items()}
    use_weights = any(weights[r["severity"]] > 0 for r in applicable)

    def w(rs: List[Dict[str, Any]]) -> float:
        return float(sum(weights[r["severity"]] for r in rs)) if use_weights else float(len(rs))

    coverage = _ratio(w(assessed), w(applicable))
    health = _ratio(w(passed), w(assessed))
    if not applicable:
        verdict, coverage_pct, health_pct = "no_checks", None, None
    else:
        coverage_pct = round(coverage * 100, 1) if coverage is not None else 0.0
        health_pct = round(health * 100, 1) if health is not None else None
        if coverage is None or coverage < PROVISIONAL_COVERAGE or health is None:
            verdict = "no_score"
        elif coverage < SCORE_COVERAGE:
            verdict = "provisional"
        else:
            verdict = "score"
    blockers = [r for r in rows if r["status"] == "fail" and r["severity"] == "high"]
    unchecked = [r for r in applicable if r["status"] == "unknown"]
    result: Dict[str, Any] = {
        "verdict": verdict,
        "score": health_pct if verdict in ("score", "provisional") else None,
        "provisional": verdict == "provisional",
        "health": health_pct,
        "coverage": coverage_pct,
        "counts": counts,
        "weighted": use_weights,
        "blocked": bool(blockers),
        "blockers": [r["name"] for r in blockers],
        "unchecked": [r["name"] for r in sorted(unchecked, key=lambda r: -weights[r["severity"]])],
        "checks": rows,
    }
    result["summary"] = summarize(result)
    return result


def summarize(result: Dict[str, Any]) -> str:
    """One line a report can open with."""
    counts = result.get("counts", {})
    tail = (f"{counts.get('pass', 0)} passed, {counts.get('fail', 0)} failed, "
            f"{counts.get('unknown', 0)} could not be checked, {counts.get('not_applicable', 0)} not applicable")
    verdict = result.get("verdict")
    if verdict == "no_checks":
        return "No applicable checks."
    cov = result.get("coverage")
    if verdict == "score":
        head = f"Health {result['score']:g} % with {cov:g} % coverage"
    elif verdict == "provisional":
        head = f"Provisional health {result['score']:g} % (coverage only {cov:g} %)"
    else:
        head = f"No score: only {cov:g} % of the applicable checks could be made"
    if result.get("blocked"):
        head += "; blocked by " + ", ".join(result["blockers"][:5])
    return f"{head} ({tail})."


def markdown_block(result: Dict[str, Any], title: str = "Scorecard") -> str:
    """The scorecard as a short Markdown section."""
    lines = [f"## {title}", "", result["summary"]]
    if result.get("unchecked"):
        lines += ["", "Not checked: " + ", ".join(result["unchecked"][:10])
                  + (" ..." if len(result["unchecked"]) > 10 else "")]
    return "\n".join(lines)


__all__ = ["STATUSES", "SEVERITY_WEIGHTS", "SCORE_COVERAGE", "PROVISIONAL_COVERAGE", "normalize_status",
           "normalize_severity", "normalize_check", "score_checks", "summarize", "markdown_block"]
