"""
tests/eval/ablation.py — EVAL-02: ablations and comparable cost.

Runs the SAME representative-task fixtures (`tests/eval/tasks.py`, reused
verbatim — never a second task list for "the ablation version") through
`tests/eval/harness.py::EvalApp` once per named dimension in `DIMENSIONS`,
changing exactly one setting at a time, and reports success rate plus
p50/p95 latency and tokens over `repeats` runs — sample size and spread,
not a single point estimate, per the acceptance line's own frontend ask.

Each dimension's setting override is written to `<data_dir>/settings.json`
BEFORE `EvalApp.start()` launches its subprocess — the running app reads
real settings (`src/settings.py`) at startup from `ODYSSEUS_DATA_DIR`,
which `EvalApp` already points at that directory; this module never pokes
a setting into a live process, it decides what the NEXT process boots with.

`judge_improvement` is the acceptance line made literal: "anadir dos
agentes no se declara mejora si solo gasta cinco veces mas para resolver
las mismas tareas." A variant counts as `improved` only when its success
rate is at least the baseline's AND its cost stays within `cost_ratio_max`
of the baseline's — never on success alone, and never on cost alone.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tests.eval.harness import EvalApp, TurnResult

#: One settings override per named dimension, applied before the eval
#: server starts. "minimal" — the harness's own defaults, nothing added —
#: is the baseline every other dimension is compared against.
DIMENSIONS: Dict[str, Dict[str, Any]] = {
    "minimal": {},
    "context": {"agent_context_engine": True},
    "no_subagents": {"agent_subagent_supervisor": False, "agent_autonomy_max_subagents": 0},
}


def _percentiles(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0}
    ordered = sorted(values)

    def _pct(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return ordered[idx]

    return {"p50": _pct(0.5), "p95": _pct(0.95)}


@dataclass
class DimensionResult:
    dimension: str
    runs: List[TurnResult] = field(default_factory=list)
    wall_seconds: List[float] = field(default_factory=list)
    verified: List[bool] = field(default_factory=list)

    def success_rate(self) -> float:
        if not self.verified:
            return 0.0
        return sum(1 for v in self.verified if v) / len(self.verified)

    def token_totals(self) -> List[float]:
        out = []
        for r in self.runs:
            data = r.metrics or {}
            out.append(float(data.get("input_tokens", 0)) + float(data.get("output_tokens", 0)))
        return out

    def mean_tokens(self) -> float:
        totals = self.token_totals()
        return statistics.mean(totals) if totals else 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "sample_size": len(self.runs),
            "success_rate": self.success_rate(),
            "latency_seconds": _percentiles(self.wall_seconds),
            "tokens": _percentiles(self.token_totals()),
            "mean_tokens": self.mean_tokens(),
        }


def run_dimension(dimension: str, task, *, repeats: int = 1,
                  settings_overrides: Optional[Dict[str, Any]] = None) -> DimensionResult:
    """Run `task` `repeats` times under one named `DIMENSIONS` entry (or an
    explicit `settings_overrides`, for a caller composing its own). Each
    repeat is a FRESH `EvalApp` — a new subprocess, a new settings file —
    so no run's measurements are contaminated by a previous run's warm
    caches or mutated state."""
    overrides = settings_overrides if settings_overrides is not None else DIMENSIONS[dimension]
    result = DimensionResult(dimension=dimension)
    for _ in range(repeats):
        app = EvalApp()
        if overrides:
            settings_path = os.path.join(app.data_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(overrides, f)
        app.start()
        try:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="odysseus-eval-ws-") as ws:
                app.script(task.script)
                session_id = app.new_session(f"ablation-{dimension}")
                t0 = time.monotonic()
                turn = app.send_turn(session_id, task.message, workspace=ws)
                elapsed = time.monotonic() - t0
                outcome = task.verify(__import__("pathlib").Path(ws), turn)
                result.runs.append(turn)
                result.wall_seconds.append(elapsed)
                result.verified.append(bool(outcome.get("ok")))
        finally:
            app.stop()
    return result


def judge_improvement(baseline: DimensionResult, variant: DimensionResult, *,
                      cost_ratio_max: float = 2.0) -> Dict[str, Any]:
    """The acceptance line's own arithmetic: `variant` is `improved` only
    when it is AT LEAST as successful as `baseline` AND its mean token cost
    is within `cost_ratio_max` of the baseline's. Adding two agents that
    fixes nothing extra while spending 5x the tokens is `cost_ratio` = 5.0
    against a default ceiling of 2.0 — refused, by construction, not by a
    reviewer remembering to check."""
    baseline_success = baseline.success_rate()
    variant_success = variant.success_rate()
    baseline_cost = baseline.mean_tokens()
    variant_cost = variant.mean_tokens()
    cost_ratio = (variant_cost / baseline_cost) if baseline_cost > 0 else (
        float("inf") if variant_cost > 0 else 1.0)

    if variant_success < baseline_success:
        return {"improved": False, "reason": "lower success rate", "cost_ratio": cost_ratio,
                "baseline_success": baseline_success, "variant_success": variant_success}
    if cost_ratio > cost_ratio_max:
        return {"improved": False, "reason": "cost exceeds the ratio ceiling",
                "cost_ratio": cost_ratio, "cost_ratio_max": cost_ratio_max,
                "baseline_success": baseline_success, "variant_success": variant_success}
    return {"improved": True, "reason": "at least as successful, within cost ceiling",
            "cost_ratio": cost_ratio, "baseline_success": baseline_success,
            "variant_success": variant_success}
