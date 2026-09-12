"""INF-03 Lote A: `build_execution_metrics` (src/execution_metrics.py).

Pure-function tests, no network, no models. Matrix from CONTRATO_INF03.md:
T07 (interrupted stream / engine without metrics), Ollama complete
(reported_engine), llama.cpp (`load_ms` absent), tools mid-stream
(`generation_ms` inferred with a note), clocks (`total_ms` never negative),
and tokens absent without `usage_tokens` even when `engine_timings` exists.
"""
from src.contracts.inference import EngineIdentity, MetricValue
from src.execution_metrics import build_execution_metrics


def test_t07_interrupted_stream_no_engine_metrics_is_absent_not_zero():
    """A stream that never reached its first token and whose engine never
    sent a `usage`/`timings` block: every phase but `total_ms` is `absent`
    — never a fabricated 0 standing in for "we don't know"."""
    m = build_execution_metrics(
        started_monotonic=100.0,
        finished_monotonic=100.4,
        first_token_monotonic=None,
        queue_wait_s=None,
        tool_events=None,
        engine_timings=None,
        usage_tokens=None,
        engine=None,
    )
    assert m.phases.queue_wait_ms.source == "absent"
    assert m.phases.load_ms.source == "absent"
    assert m.phases.prefill_ms.source == "absent"
    assert m.phases.generation_ms.source == "absent"
    assert m.phases.tools_ms.source == "absent"
    assert m.tokens.prompt.source == "absent"
    assert m.tokens.generated.source == "absent"
    # total_ms is the one phase this module can always establish from the
    # caller's own clock readings.
    assert m.phases.total_ms.source == "observed_client"
    assert m.phases.total_ms.value == 400.0


def test_ollama_complete_turn_reports_engine_phases():
    engine_timings = {
        "load_ms": 50.0, "prompt_ms": 200.0, "predicted_ms": 400.0,
        "total_ms": 700.0, "prompt_n": 10, "predicted_n": 20,
        "source": "ollama",
    }
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=0.7,
        first_token_monotonic=0.3,
        queue_wait_s=0.05,
        tool_events=None,
        engine_timings=engine_timings,
        usage_tokens={"prompt": 10, "generated": 20, "source": "reported_engine"},
        engine=EngineIdentity(implementation="ollama"),
    )
    assert m.phases.load_ms == MetricValue(value=50.0, source="reported_engine")
    assert m.phases.prefill_ms.value == 200.0
    assert m.phases.prefill_ms.source == "reported_engine"
    assert m.phases.generation_ms.value == 400.0
    assert m.phases.generation_ms.source == "reported_engine"
    assert m.phases.queue_wait_ms.value == 50.0
    assert m.phases.queue_wait_ms.source == "observed_client"
    assert m.tokens.prompt.value == 10
    assert m.tokens.generated.value == 20
    assert m.tokens.prompt.source == "reported_engine"
    assert m.notes == ()


def test_llamacpp_load_ms_is_absent_but_prefill_and_generation_report():
    engine_timings = {
        "load_ms": None, "prompt_ms": 120.5, "predicted_ms": 900.25,
        "prompt_n": 50, "predicted_n": 30, "source": "llamacpp",
    }
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=1.05,
        first_token_monotonic=0.12,
        queue_wait_s=None,
        tool_events=None,
        engine_timings=engine_timings,
        usage_tokens={"prompt": 50, "generated": 30, "source": "reported_engine"},
        engine=None,
    )
    assert m.phases.load_ms.source == "absent"
    assert m.phases.load_ms.value is None
    assert m.phases.prefill_ms.source == "reported_engine"
    assert m.phases.prefill_ms.value == 120.5
    assert m.phases.generation_ms.source == "reported_engine"
    assert m.phases.generation_ms.value == 900.25
    # No door for this turn -> absent, not a fabricated 0 wait.
    assert m.phases.queue_wait_ms.source == "absent"


def test_tools_between_first_token_and_completion_infer_generation_with_note():
    tool_events = [
        {"duration_ms": 250.0},
        {"duration_ms": 750.0},
    ]
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=2.0,
        first_token_monotonic=0.5,
        queue_wait_s=None,
        tool_events=tool_events,
        engine_timings=None,   # engine reported nothing this turn
        usage_tokens=None,
        engine=None,
    )
    assert m.phases.generation_ms.source == "inferred"
    # (finished - first_token) = 1.5s = 1500ms, includes the tool time.
    assert m.phases.generation_ms.value == 1500.0
    assert any("inferred" in n for n in m.notes)
    # Multi-signal turn (tools ran): prefill is not computed, only reported.
    assert m.phases.prefill_ms.source == "absent"
    # tools_ms sums the observed per-call durations.
    assert m.phases.tools_ms.source == "observed_client"
    assert m.phases.tools_ms.value == 1000.0


def test_single_round_no_tools_computes_prefill_and_generation():
    m = build_execution_metrics(
        started_monotonic=10.0,
        finished_monotonic=11.5,
        first_token_monotonic=10.4,
        queue_wait_s=0.1,
        tool_events=[],
        engine_timings=None,
        usage_tokens=None,
        engine=None,
    )
    # send = started + queue_wait = 10.1; first_token - send = 0.3s = 300ms
    assert m.phases.prefill_ms.source == "computed"
    assert m.phases.prefill_ms.value == 300.0
    # finished - first_token = 1.1s = 1100ms, no tools -> plain "computed"
    assert m.phases.generation_ms.source == "computed"
    assert m.phases.generation_ms.value == 1100.0
    assert m.notes == ()


def test_total_ms_never_negative_even_on_clock_skew():
    m = build_execution_metrics(
        started_monotonic=5.0,
        finished_monotonic=4.999,  # should never happen; defend anyway
        first_token_monotonic=None,
        queue_wait_s=None,
        tool_events=None,
        engine_timings=None,
        usage_tokens=None,
        engine=None,
    )
    assert m.phases.total_ms.source == "observed_client"
    assert m.phases.total_ms.value == 0.0


def test_tokens_absent_without_usage_tokens_even_with_engine_timings():
    # engine_timings carries prompt_n/predicted_n, but tokens is populated
    # only from usage_tokens — engine_timings alone must not leak into it.
    engine_timings = {
        "load_ms": None, "prompt_ms": 10.0, "predicted_ms": 20.0,
        "prompt_n": 5, "predicted_n": 7, "source": "llamacpp",
    }
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=0.03,
        first_token_monotonic=0.01,
        queue_wait_s=None,
        tool_events=None,
        engine_timings=engine_timings,
        usage_tokens=None,
        engine=None,
    )
    assert m.tokens.prompt.source == "absent"
    assert m.tokens.generated.source == "absent"


def test_phase_overlap_flagged_in_notes_not_silently_corrected():
    # prefill(400) + generation(900) + tools(200) = 1500 > total(1000):
    # never shrunk to fit, just named.
    engine_timings = {
        "load_ms": None, "prompt_ms": 400.0, "predicted_ms": 900.0,
        "prompt_n": None, "predicted_n": None, "source": "llamacpp",
    }
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=1.0,
        first_token_monotonic=0.4,
        queue_wait_s=None,
        tool_events=[{"duration_ms": 200.0}],
        engine_timings=engine_timings,
        usage_tokens=None,
        engine=None,
    )
    assert m.phases.prefill_ms.value == 400.0
    assert m.phases.generation_ms.value == 900.0
    assert m.phases.tools_ms.value == 200.0
    assert m.phases.total_ms.value == 1000.0
    assert "phases overlap: engine and client clocks are not additive" in m.notes


def test_usage_tokens_computed_source_is_preserved():
    m = build_execution_metrics(
        started_monotonic=0.0,
        finished_monotonic=0.2,
        first_token_monotonic=None,
        queue_wait_s=None,
        tool_events=None,
        engine_timings=None,
        usage_tokens={"prompt": 100, "generated": 25, "source": "computed"},
        engine=None,
    )
    assert m.tokens.prompt.value == 100
    assert m.tokens.prompt.source == "computed"
    assert m.tokens.generated.value == 25
    assert m.tokens.generated.source == "computed"


def test_no_gate_queue_wait_absent_vs_measured_zero_wait():
    absent = build_execution_metrics(
        started_monotonic=0.0, finished_monotonic=0.1,
        queue_wait_s=None,
    )
    measured_zero = build_execution_metrics(
        started_monotonic=0.0, finished_monotonic=0.1,
        queue_wait_s=0.0,
    )
    assert absent.phases.queue_wait_ms.source == "absent"
    assert absent.phases.queue_wait_ms.value is None
    assert measured_zero.phases.queue_wait_ms.source == "observed_client"
    assert measured_zero.phases.queue_wait_ms.value == 0.0
