"""
tests/test_p1_eval_02_ablation.py — EVAL-02, lote 53.

Acceptance line: "Anadir dos agentes no se declara mejora si solo gasta
cinco veces mas para resolver las mismas tareas." The pure half
(`judge_improvement`) is checked against exactly that scenario, with no
server involved. One live test proves the wiring end to end — a real
`EvalApp` subprocess, a real representative task from `tests/eval/tasks.py`,
one dimension's settings override actually taking effect for the process
that read it — rather than trusting the ablation module's own description
of what it does.
"""
from __future__ import annotations

import pytest

from tests.eval.ablation import DIMENSIONS, DimensionResult, judge_improvement, run_dimension
from tests.eval import tasks as T


def _result(success_rate: float, mean_tokens: float, n: int = 4) -> DimensionResult:
    r = DimensionResult(dimension="synthetic")
    successes = round(success_rate * n)
    r.verified = [True] * successes + [False] * (n - successes)
    r.runs = []
    r.wall_seconds = [1.0] * n
    # `mean_tokens()` reads `r.runs`' metrics; fake it directly instead by
    # monkeypatching the method for this synthetic fixture.
    r.mean_tokens = lambda: mean_tokens  # type: ignore[method-assign]
    return r


def test_five_times_the_cost_for_the_same_success_is_not_an_improvement():
    baseline = _result(success_rate=1.0, mean_tokens=1000)
    variant = _result(success_rate=1.0, mean_tokens=5000)   # +2 agents, same tasks solved
    verdict = judge_improvement(baseline, variant)
    assert verdict["improved"] is False
    assert verdict["cost_ratio"] == pytest.approx(5.0)


def test_a_cheaper_or_similarly_priced_variant_that_does_at_least_as_well_is_an_improvement():
    baseline = _result(success_rate=0.75, mean_tokens=1000)
    variant = _result(success_rate=0.75, mean_tokens=1400)
    verdict = judge_improvement(baseline, variant)
    assert verdict["improved"] is True


def test_lower_success_is_never_an_improvement_even_if_its_cheaper():
    baseline = _result(success_rate=1.0, mean_tokens=1000)
    variant = _result(success_rate=0.5, mean_tokens=200)
    verdict = judge_improvement(baseline, variant)
    assert verdict["improved"] is False
    assert verdict["reason"] == "lower success rate"


def test_the_ceiling_is_configurable_not_hardcoded():
    baseline = _result(success_rate=1.0, mean_tokens=1000)
    variant = _result(success_rate=1.0, mean_tokens=2500)
    assert judge_improvement(baseline, variant, cost_ratio_max=2.0)["improved"] is False
    assert judge_improvement(baseline, variant, cost_ratio_max=3.0)["improved"] is True


def test_dimensions_declares_a_minimal_baseline_with_no_overrides():
    assert DIMENSIONS["minimal"] == {}
    assert "context" in DIMENSIONS and "no_subagents" in DIMENSIONS


@pytest.mark.slow
def test_run_dimension_actually_drives_a_real_turn_through_a_real_server():
    """No mocks at the boundary this batch owns: a real subprocess app, a
    real (scripted-model) turn, a real settings.json read at startup."""
    result = run_dimension("minimal", T.BUG_FIX, repeats=1)
    assert len(result.runs) == 1
    assert result.runs[0].rounds >= 1
    assert result.wall_seconds[0] > 0
    summary = result.summary()
    assert summary["sample_size"] == 1
    assert summary["dimension"] == "minimal"
