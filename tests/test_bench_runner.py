"""src/bench/runner.py — INF-04 A5.

No real model, ever: `src.llm_core.stream_llm` is replaced with a fake
async generator that emits the same SSE shapes the real one does
(`delta`/`usage` with `engine_timings`/`[DONE]`), and `src.vram_admission.admit`
with a fake that only records calls. Everything here exercises the state
machine, the budget/cancel behaviour, `reconcile_on_start` (T12) and
`compare` (T09/T10) against that fake.
"""
from __future__ import annotations

import json

import pytest

from src.bench import runner, suites
from src.contracts.inference import (
    BenchmarkCase, EngineIdentity, InferenceProfile, ModelDescriptor,
)
from src.contracts.base import now_iso
from src import vram_admission


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    yield


def _profile(model="test-model", objective="interactive", options=None):
    raw = {
        "id": "profile-1", "label": "Test profile",
        "model": {"artifact_id": model},
        "engine": {"implementation": "ollama", "host": "127.0.0.1", "port": 11434},
        "hardware_id": None, "options": options or {}, "objective": objective,
        "created_at": now_iso(),
    }
    return InferenceProfile.parse(raw)


def _case(case_id, prompt="hola", checks=None, max_tokens=None):
    raw = {"id": case_id, "suite": "fake_suite", "prompt": prompt,
           "checks": checks or [], "max_tokens": max_tokens}
    return BenchmarkCase.parse(raw)


def _fake_suite(n_cases=3, checks=None):
    cases = tuple(_case(f"case{i+1}", checks=checks) for i in range(n_cases))
    return {"id": "fake_suite", "version": "1.0.0", "objective": "interactive", "cases": cases}


@pytest.fixture
def fake_suite(monkeypatch):
    suite = _fake_suite()
    monkeypatch.setattr(suites, "load_suite", lambda suite_id: suite)
    return suite


@pytest.fixture
def admit_calls(monkeypatch):
    calls = []

    async def _fake_admit(endpoint_url, model, *, owner="", on_progress=None,
                          mode=None, timeout=None, waited_out=None):
        calls.append((endpoint_url, model, owner))
        if waited_out is not None:
            waited_out["waited_s"] = 0.0
        return "proceed"

    monkeypatch.setattr(vram_admission, "admit", _fake_admit)
    return calls


def _make_stream_llm(call_log=None, output_tokens=5, cancel_after=None, cancel_fn=None):
    """A fake `stream_llm` emitting `delta`/`usage`/`[DONE]` — never a real
    model. `cancel_after`/`cancel_fn` let a test simulate an external
    cancel() arriving while the Nth case's stream is still in progress."""
    call_log = call_log if call_log is not None else []

    async def _stream(url, model, messages, **kwargs):
        call_log.append({"url": url, "model": model, "messages": messages, "kwargs": kwargs})
        idx = len(call_log)
        yield 'data: ' + json.dumps({"delta": "Hola "}) + '\n\n'
        if cancel_after == idx and cancel_fn is not None:
            cancel_fn()
        yield 'data: ' + json.dumps({"delta": "mundo"}) + '\n\n'
        usage = {
            "input_tokens": 10, "output_tokens": output_tokens,
            "engine_timings": {
                "load_ms": 5, "prompt_ms": 15, "predicted_ms": 100,
                "prompt_n": 10, "predicted_n": output_tokens, "source": "ollama",
            },
        }
        yield 'data: ' + json.dumps({"type": "usage", "data": usage}) + '\n\n'
        yield 'data: [DONE]\n\n'

    return _stream, call_log


# ── plan() never touches the model ──────────────────────────────────────────

def test_plan_does_not_call_the_model(fake_suite, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("plan() must never call stream_llm")
    monkeypatch.setattr("src.llm_core.stream_llm", _boom)

    async def _boom_admit(*a, **k):
        raise AssertionError("plan() must never call vram_admission.admit")
    monkeypatch.setattr(vram_admission, "admit", _boom_admit)

    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    assert run.state == "planned"
    assert run.summary.cases_planned == 3
    assert run.samples == ()


def test_plan_caps_cases_planned_at_max_cases(fake_suite):
    run = runner.plan(_profile(), "fake_suite", {"repeats": 2, "max_cases": 4}, "alice")
    assert run.summary.cases_planned == 4  # 3 cases * 2 repeats = 6, capped at 4


def test_plan_estimate_seconds_null_without_a_learned_speed(fake_suite, monkeypatch):
    monkeypatch.setattr("src.llm_core.local_speed", lambda model: None)
    run = runner.plan(_profile(), "fake_suite", {}, "alice")
    assert run.summary.estimate_seconds is None


def test_plan_estimate_seconds_uses_local_speed_when_known(fake_suite, monkeypatch):
    monkeypatch.setattr("src.llm_core.local_speed", lambda model: 20.0)
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    assert run.summary.estimate_seconds is not None
    assert run.summary.estimate_seconds > 0


# ── start(): sequential, admit before every case ────────────────────────────

@pytest.mark.asyncio
async def test_start_runs_sequentially_and_admits_every_case(fake_suite, admit_calls, monkeypatch):
    call_log = []
    stream_fn, _ = _make_stream_llm(call_log=call_log)
    monkeypatch.setattr("src.llm_core.stream_llm", stream_fn)

    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    result = await runner.start(run.id)

    assert result.state == "completed"
    assert len(result.samples) == 3
    assert len(admit_calls) == 3
    assert [c[1] for c in admit_calls] == ["test-model"] * 3
    assert [s.case_id for s in result.samples] == ["case1", "case2", "case3"]
    for sample in result.samples:
        assert sample.error is None
        assert sample.quality.passed is True  # no checks -> trivially true
        assert sample.metrics is not None


@pytest.mark.asyncio
async def test_start_refuses_a_run_not_in_planned_state(fake_suite, admit_calls, monkeypatch):
    stream_fn, _ = _make_stream_llm()
    monkeypatch.setattr("src.llm_core.stream_llm", stream_fn)
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    await runner.start(run.id)
    with pytest.raises(runner.RunStateError):
        await runner.start(run.id)  # already completed


# ── budget: max_seconds / max_generated_tokens -> partial ──────────────────

@pytest.mark.asyncio
async def test_budget_seconds_exhausted_yields_partial_with_interruption(fake_suite, admit_calls, monkeypatch):
    import asyncio
    call_log = []

    async def _slow_stream(url, model, messages, **kwargs):
        call_log.append(1)
        await asyncio.sleep(0.05)
        yield 'data: ' + json.dumps({"delta": "hi"}) + '\n\n'
        yield 'data: ' + json.dumps({"type": "usage", "data": {"output_tokens": 5}}) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.llm_core.stream_llm", _slow_stream)
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1, "max_seconds": 0.02}, "alice")
    result = await runner.start(run.id)

    assert result.state == "partial"
    assert len(result.samples) == 1  # the in-flight case finished; the rest never started
    assert any(i.reason == "budget_seconds" for i in result.interruptions)


@pytest.mark.asyncio
async def test_budget_generated_tokens_exhausted_yields_partial(fake_suite, admit_calls, monkeypatch):
    stream_fn, call_log = _make_stream_llm(output_tokens=100)
    monkeypatch.setattr("src.llm_core.stream_llm", stream_fn)
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1, "max_generated_tokens": 50}, "alice")
    result = await runner.start(run.id)

    assert result.state == "partial"
    assert len(result.samples) == 1
    assert len(call_log) == 1  # the second/third cases never called the model
    assert any(i.reason == "budget_tokens" for i in result.interruptions)


# ── cancel(): preserves partial samples, stops the rest ─────────────────────

@pytest.mark.asyncio
async def test_cancel_mid_run_preserves_partial_samples_and_stops(fake_suite, admit_calls, monkeypatch):
    run_holder = {}
    call_log = []
    stream_fn, _ = _make_stream_llm(
        call_log=call_log, cancel_after=2, cancel_fn=lambda: runner.cancel(run_holder["id"]),
    )
    monkeypatch.setattr("src.llm_core.stream_llm", stream_fn)

    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    run_holder["id"] = run.id
    result = await runner.start(run.id)

    assert result.state == "cancelled"
    assert len(result.samples) == 2  # case1 completed, case2 cut mid-stream
    assert result.samples[0].error is None
    assert result.samples[1].error == "cancelled"
    assert len(call_log) == 2  # case3 never called the model
    assert any(i.reason == "cancelled" for i in result.interruptions)


@pytest.mark.asyncio
async def test_cancel_a_planned_run_settles_immediately(fake_suite):
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    cancelled = runner.cancel(run.id)
    assert cancelled.state == "cancelled"
    assert cancelled.finished_at is not None


def test_cancel_is_idempotent_on_a_terminal_run(fake_suite):
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    once = runner.cancel(run.id)
    twice = runner.cancel(run.id)
    assert once.state == twice.state == "cancelled"


# ── reconcile_on_start(): T12 ────────────────────────────────────────────────

def test_reconcile_on_start_marks_running_runs_as_interrupted(fake_suite):
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    # Simulate what a previous process left behind: still "running" on disk.
    from dataclasses import replace
    stuck = replace(run, state="running", started_at=now_iso())
    runner._save(stuck, owner="alice")

    result = runner.reconcile_on_start()

    assert run.id in result["interrupted"]
    reconciled = runner.get(run.id)
    assert reconciled.state == "interrupted"
    assert any(i.reason == "process_restarted" for i in reconciled.interruptions)


@pytest.mark.asyncio
async def test_reconcile_on_start_leaves_completed_runs_alone(fake_suite, admit_calls, monkeypatch):
    stream_fn, _ = _make_stream_llm()
    monkeypatch.setattr("src.llm_core.stream_llm", stream_fn)
    run = runner.plan(_profile(), "fake_suite", {"repeats": 1}, "alice")
    await runner.start(run.id)

    result = runner.reconcile_on_start()
    assert run.id not in result["interrupted"]
    assert runner.get(run.id).state == "completed"


# ── compare(): T09 / T10 ─────────────────────────────────────────────────────

def _saved_run(run_id, *, model="qwen", objective="interactive", suite_id="s", suite_version="1.0.0",
               quality_pass_rate, gen_tps_median, gen_tps_p95, n):
    raw = {
        "id": run_id, "suite_id": suite_id, "suite_version": suite_version,
        "profile": _profile(model=model, objective=objective).to_dict(),
        "baseline_run_id": None, "state": "completed",
        "budget": {"repeats": n}, "conditions": {}, "samples": [], "interruptions": [],
        "summary": {
            "cases_run": n, "cases_planned": n, "quality_pass_rate": quality_pass_rate,
            "gen_tps": {"median": gen_tps_median, "p95": gen_tps_p95, "n": n},
            "ttft_ms": {"median": 100.0, "p95": 120.0, "n": n},
        },
        "started_at": now_iso(), "finished_at": now_iso(),
    }
    from src.contracts.inference import BenchmarkRun
    run = BenchmarkRun.parse(raw)
    runner._save(run, owner="alice")
    return run


def test_compare_faster_candidate_with_held_quality_is_improvement():
    baseline = _saved_run("run-base", quality_pass_rate=0.9, gen_tps_median=20.0, gen_tps_p95=21.0, n=5)
    candidate = _saved_run("run-cand", quality_pass_rate=0.9, gen_tps_median=30.0, gen_tps_p95=31.0, n=5)
    comparison = runner.compare(baseline.id, candidate.id)
    assert comparison.comparable is True
    assert comparison.verdict == "improvement"
    assert comparison.deltas.gen_tps_median_pct == pytest.approx(50.0)


def test_compare_faster_candidate_with_worse_quality_is_regression_T09():
    baseline = _saved_run("run-base2", quality_pass_rate=0.95, gen_tps_median=20.0, gen_tps_p95=21.0, n=5)
    candidate = _saved_run("run-cand2", quality_pass_rate=0.60, gen_tps_median=40.0, gen_tps_p95=41.0, n=5)
    comparison = runner.compare(baseline.id, candidate.id)
    assert comparison.comparable is True
    assert comparison.verdict == "regression"  # a faster candidate that fails the quality gate never promotes


def test_compare_small_sample_is_inconclusive_T10():
    baseline = _saved_run("run-base3", quality_pass_rate=0.9, gen_tps_median=20.0, gen_tps_p95=21.0, n=2)
    candidate = _saved_run("run-cand3", quality_pass_rate=0.9, gen_tps_median=30.0, gen_tps_p95=31.0, n=2)
    comparison = runner.compare(baseline.id, candidate.id)
    assert comparison.comparable is True
    assert comparison.verdict == "inconclusive"
    assert "sample" in comparison.reasons[0]


def test_compare_gain_within_noise_spread_is_inconclusive():
    baseline = _saved_run("run-base4", quality_pass_rate=0.9, gen_tps_median=20.0, gen_tps_p95=30.0, n=5)
    candidate = _saved_run("run-cand4", quality_pass_rate=0.9, gen_tps_median=22.5, gen_tps_p95=32.0, n=5)
    comparison = runner.compare(baseline.id, candidate.id)
    assert comparison.comparable is True
    assert comparison.verdict == "inconclusive"


def test_compare_different_model_is_not_comparable():
    baseline = _saved_run("run-base5", model="model-a", quality_pass_rate=0.9, gen_tps_median=20.0, gen_tps_p95=21.0, n=5)
    candidate = _saved_run("run-cand5", model="model-b", quality_pass_rate=0.9, gen_tps_median=30.0, gen_tps_p95=31.0, n=5)
    comparison = runner.compare(baseline.id, candidate.id)
    assert comparison.comparable is False
    assert comparison.verdict == "inconclusive"
    assert "model" in comparison.reasons[0]


def test_compare_missing_run_raises_not_found():
    with pytest.raises(runner.RunNotFound):
        runner.compare("does-not-exist", "also-missing")
