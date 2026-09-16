"""src/fanout/score.py — OpenMontage-style multi-dimension scoring (INFORME
§3: "motor de 7 dimensiones... cada decisión queda logueada con
alternativas consideradas, score y razonamiento") applied to fan-out
candidates instead of video-provider picks.

Six dimensions, each normalised to [0, 1] and combined by a weight that a
setting can override without touching this module:

  * ``tests``    0.35 — the target project's own test suite, run inside the
                 candidate's isolated copy (``src.alternatives.run_tests``
                 via ``runner.py``). 1.0 pass, 0.0 fail, 0.5 "no runner
                 detected" (neither proven nor disproven).
  * ``harness``  0.20 — the worker's own stop reason/error/rejections, read
                 the way ``src.tool_outcome``/``SubagentRun.report`` already
                 classify a run — never the model's prose.
  * ``diff_size`` 0.10 — additions+deletions vs. the base; penalises an
                 enormous diff (rule: "penaliza diffs enormes"), but a
                 candidate that changed NOTHING is not rewarded either.
  * ``cost``     0.15 — lower is better; an UNKNOWN cost is never treated as
                 free (the alternatives.py `_default_cost` convention,
                 restated for scoring: unknown is not zero).
  * ``latency``  0.10 — lower is better, normalised against the slowest
                 candidate in this same ranking.
  * ``judge``    0.10 — an optional caller-supplied reviewer
                 (``src.claim_verify.verify``/``src.expert_review``-style
                 callable); neutral 0.5 when none is given, never silently
                 dropped from the weighted sum.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

DEFAULT_WEIGHTS: Dict[str, float] = {
    "tests": 0.35, "harness": 0.20, "diff_size": 0.10,
    "cost": 0.15, "latency": 0.10, "judge": 0.10,
}

#: Diffs at/under this many changed lines take no size penalty at all.
DIFF_SIZE_FREE_LINES = 80
#: Diffs at/over this many changed lines score 0 on the size dimension.
DIFF_SIZE_ZERO_LINES = 2000


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_tests(tests: Optional[Dict[str, Any]]) -> float:
    if not tests:
        return 0.5
    ok = tests.get("ok")
    if ok is True:
        return 1.0
    if ok is False:
        return 0.0
    return 0.5  # ok is None: no runner detected -- inconclusive, not a failure


def _score_harness(worker_report: Optional[Dict[str, Any]]) -> float:
    if not worker_report:
        return 0.5
    if worker_report.get("error"):
        return 0.0
    status = worker_report.get("status")
    if status == "done":
        base = 1.0
    elif status == "partial":
        base = 0.5
    else:
        base = 0.2
    rejections = int(worker_report.get("rejections") or 0)
    failed = int(worker_report.get("failed_calls") or 0)
    penalty = min(0.5, 0.1 * rejections + 0.05 * failed)
    return _clamp01(base - penalty)


def _score_diff_size(diff: Optional[Dict[str, Any]]) -> float:
    if not diff:
        return 0.0  # no diff at all recorded == did not do the task
    lines = int(diff.get("additions") or 0) + int(diff.get("deletions") or 0)
    if lines == 0:
        return 0.0  # touched nothing
    if lines <= DIFF_SIZE_FREE_LINES:
        return 1.0
    if lines >= DIFF_SIZE_ZERO_LINES:
        return 0.0
    span = DIFF_SIZE_ZERO_LINES - DIFF_SIZE_FREE_LINES
    return _clamp01(1.0 - (lines - DIFF_SIZE_FREE_LINES) / span)


def _score_cost(cost: Any, known_costs: List[float]) -> float:
    if cost is None or cost == "unknown":
        # Unknown is not free: a worse-than-average, never-best score, so an
        # unpriced local run cannot silently outscore a priced remote one on
        # cost alone just because nobody billed it yet.
        return 0.4
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return 0.4
    if not known_costs:
        return 0.8  # the only priced candidate: cheap relative to nothing else
    lo, hi = min(known_costs), max(known_costs)
    if hi <= lo:
        return 1.0  # every priced candidate costs the same -- none is cheaper
    return _clamp01(1.0 - (cost - lo) / (hi - lo))


def _score_latency(latency_s: Any, all_latencies: List[float]) -> float:
    try:
        latency_s = float(latency_s)
    except (TypeError, ValueError):
        return 0.5
    if not all_latencies:
        return 0.5
    worst = max(all_latencies)
    if worst <= 0:
        return 1.0
    return _clamp01(1.0 - (latency_s / worst))


def rank(candidates: List[Dict[str, Any]], *, weights: Optional[Dict[str, float]] = None,
          judge: Optional[Callable[[Dict[str, Any]], float]] = None) -> List[Dict[str, Any]]:
    """Score every candidate checkpoint dict (`runner.py`'s shape) and
    return them ranked best-first, each with a `score` breakdown and a
    one-paragraph `reasoning` string. Never raises on a candidate missing a
    field -- a candidate that never finished just scores low, it does not
    take the ranking down with it.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in DEFAULT_WEIGHTS})
    total_w = sum(w.values()) or 1.0

    known_costs = [float(c["cost"]) for c in candidates
                   if c.get("cost") not in (None, "unknown") and _is_number(c.get("cost"))]
    all_latencies = [float(c["latency_s"]) for c in candidates if _is_number(c.get("latency_s"))]

    scored: List[Dict[str, Any]] = []
    for cand in candidates:
        state = cand.get("state")
        dims = {
            "tests": _score_tests(cand.get("tests")),
            "harness": _score_harness(cand.get("worker_report")),
            "diff_size": _score_diff_size(cand.get("diff")),
            "cost": _score_cost(cand.get("cost"), known_costs),
            "latency": _score_latency(cand.get("latency_s"), all_latencies),
            "judge": _clamp01(float(judge(cand))) if judge is not None else 0.5,
        }
        if state == "error":
            # An error still gets a real (low) score rather than being
            # dropped from the ranking -- the caller decides whether to show
            # it, but "why did candidate X lose" must always be answerable.
            dims = {k: (0.0 if k in ("tests", "harness", "diff_size") else v) for k, v in dims.items()}
        weighted = sum(dims[k] * w[k] for k in dims) / total_w
        reasoning = _reasoning(cand, dims, weighted)
        scored.append({
            "label": cand.get("label"), "state": state, "total_score": round(weighted, 4),
            "dimensions": {k: round(v, 4) for k, v in dims.items()}, "weights": w,
            "reasoning": reasoning,
        })

    scored.sort(key=lambda s: s["total_score"], reverse=True)
    for i, s in enumerate(scored):
        s["rank"] = i + 1
    return scored


def _is_number(x: Any) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _reasoning(cand: Dict[str, Any], dims: Dict[str, float], total: float) -> str:
    label = cand.get("label", "?")
    tests = cand.get("tests") or {}
    tests_txt = ("tests pass" if tests.get("ok") is True else
                 "tests fail" if tests.get("ok") is False else "no test runner detected")
    diff = cand.get("diff") or {}
    diff_txt = f"{diff.get('files_changed', 0)} file(s), +{diff.get('additions', 0)}/-{diff.get('deletions', 0)}"
    cost = cand.get("cost")
    cost_txt = "unknown cost" if cost in (None, "unknown") else f"cost {cost}"
    latency = cand.get("latency_s")
    latency_txt = f"{latency:.1f}s" if _is_number(latency) else "latency unknown"
    weakest = min(dims, key=lambda k: dims[k])
    return (
        f"{label}: score {total:.2f} — {tests_txt}; diff {diff_txt}; {cost_txt}; {latency_txt}. "
        f"Weakest dimension: {weakest} ({dims[weakest]:.2f})."
    )
