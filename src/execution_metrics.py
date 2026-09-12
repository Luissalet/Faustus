"""execution_metrics.py — INF-03: build the per-turn `ExecutionMetrics`
(`src/contracts/inference.py`) a "why did it take this long" view needs.

Pure by design: everything this module needs is passed in as plain
timestamps/dicts, and it does no I/O, no clock reads, no logging. The two
callers (`src/agent_loop.py`'s tool-using turns, `routes/chat_routes.py`'s
plain-chat stream) own reading the clock and the engine's own report; this
module only decides, given those readings, what each phase's `MetricValue`
and `source` should be — the single place that decision is made, so the two
call sites cannot quietly drift on what counts as `computed` versus
`inferred` versus `absent`.

The one rule everything below answers to (CONTRATO_INF03.md): **a number
that was not observed is `absent`, never 0.** A phase this module could not
establish from what it was given stays an absent `MetricValue` — it is
never backfilled with a guess, and nothing here "corrects" an overlap
between phases; that goes in `notes` instead (see `_coherence_notes`).
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from src.contracts.inference import (
    EngineIdentity, ExecutionMetrics, MetricValue, Phases, Tokens,
)

#: Guards against float rounding on the overlap check flagging phases that
#: sum to within a millisecond of `total_ms` — not a real double-count.
_OVERLAP_EPSILON_MS = 1.0


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _ms(value: Any) -> Optional[float]:
    """A `Mapping[str, Any]` engine_timings field, already in milliseconds,
    as a finite float or `None`. Never negative — a malformed engine value
    is treated as not having reported the field at all."""
    v = _finite(value)
    if v is None or v < 0:
        return None
    return v


def _reported(value_ms: Any) -> MetricValue:
    v = _ms(value_ms)
    return MetricValue(value=v, source="reported_engine") if v is not None else MetricValue()


def _tool_event_duration_s(event: Any) -> Optional[float]:
    """One tool call's observed wall-clock duration in seconds, from
    whichever shape the caller's tool_events happen to carry — `duration_ms`
    (`src/tool_execution.py::execute_tool_block`, INF-03), `duration_s`, or
    a `started_at`/`finished_at` pair (same clock, either epoch or
    monotonic seconds). `None` when the event carries none of these: a tool
    call this turn ran but was never timed is not the same fact as a tool
    call that took 0s, so it must not silently contribute 0 to the sum."""
    if not isinstance(event, Mapping):
        return None
    duration_ms = _finite(event.get("duration_ms"))
    if duration_ms is not None and duration_ms >= 0:
        return duration_ms / 1000.0
    duration_s = _finite(event.get("duration_s"))
    if duration_s is not None and duration_s >= 0:
        return duration_s
    started = _finite(event.get("started_at"))
    finished = _finite(event.get("finished_at"))
    if started is not None and finished is not None:
        delta = finished - started
        if delta >= 0:
            return delta
    return None


def _tools_phase(tool_events: Optional[Sequence[Any]]) -> MetricValue:
    if not tool_events:
        return MetricValue()
    total = 0.0
    any_timed = False
    for event in tool_events:
        duration_s = _tool_event_duration_s(event)
        if duration_s is not None:
            any_timed = True
            total += duration_s
    if not any_timed:
        return MetricValue()
    return MetricValue(value=round(total * 1000.0, 3), source="observed_client")


def _tokens_phase(usage_tokens: Optional[Mapping[str, Any]]) -> Tokens:
    if not usage_tokens:
        return Tokens()
    source = usage_tokens.get("source")
    if source not in ("reported_engine", "computed"):
        return Tokens()

    def _count(key: str) -> MetricValue:
        raw = usage_tokens.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return MetricValue()
        if not math.isfinite(raw) or raw < 0:
            return MetricValue()
        return MetricValue(value=float(raw), source=source)

    return Tokens(prompt=_count("prompt"), generated=_count("generated"))


def _coherence_notes(phases: Phases) -> list:
    """§08: phases are spans of a single turn's clock, not independent
    counters — a phase that ran concurrently with another (or was double
    counted across an engine/client boundary) can make prefill+generation+
    tools exceed total. That is never corrected by shrinking a phase to fit;
    it is named so a reader knows the clocks disagree."""
    parts = (phases.prefill_ms, phases.generation_ms, phases.tools_ms)
    if phases.total_ms.value is None:
        return []
    phase_sum = sum(p.value for p in parts if p.source != "absent" and p.value is not None)
    if phase_sum > phases.total_ms.value + _OVERLAP_EPSILON_MS:
        return ["phases overlap: engine and client clocks are not additive"]
    return []


def build_execution_metrics(
    *,
    started_monotonic: float,
    finished_monotonic: float,
    first_token_monotonic: Optional[float] = None,
    queue_wait_s: Optional[float] = None,
    tool_events: Optional[Sequence[Any]] = None,
    engine_timings: Optional[Mapping[str, Any]] = None,
    usage_tokens: Optional[Mapping[str, Any]] = None,
    engine: Optional[EngineIdentity] = None,
    scope: str = "request",
    observed_at: Optional[str] = None,
) -> ExecutionMetrics:
    """Assemble one turn's `ExecutionMetrics` from clock readings and
    whatever the engine/admission gate reported. All timestamps are the
    caller's `time.monotonic()` readings (never wall-clock: a system clock
    step must not manufacture a negative or inflated duration) of the same
    turn; `finished_monotonic` is expected to be >= `started_monotonic`, but
    is clamped defensively rather than trusted blindly.

    `engine_timings` is the dict INF-03 added to the `usage` SSE event in
    `src/llm_core.py` (`engine_timings_from_ollama`/`_llamacpp_engine_timings`):
    `{"load_ms", "prompt_ms", "predicted_ms", "prompt_n", "predicted_n",
    "source"}`, values already in milliseconds, `None` for whatever the
    engine did not report. `None` entirely (a cloud OpenAI-compatible
    provider with no such block) means every engine-reported phase below
    stays absent — never a guess dressed up as one of them.
    """
    total_s = finished_monotonic - started_monotonic
    total_ms = MetricValue(value=round(max(total_s, 0.0) * 1000.0, 3), source="observed_client")

    if queue_wait_s is None:
        queue_wait_ms = MetricValue()
    else:
        queue_wait_ms = MetricValue(value=round(max(queue_wait_s, 0.0) * 1000.0, 3), source="observed_client")

    et = engine_timings or {}
    load_ms = _reported(et.get("load_ms"))
    prefill_ms = _reported(et.get("prompt_ms"))
    generation_ms = _reported(et.get("predicted_ms"))

    single_round_no_tools = not tool_events

    if prefill_ms.source == "absent" and single_round_no_tools and first_token_monotonic is not None:
        send_monotonic = started_monotonic + max(queue_wait_s or 0.0, 0.0)
        computed_prefill = (first_token_monotonic - send_monotonic) * 1000.0
        prefill_ms = MetricValue(value=round(max(computed_prefill, 0.0), 3), source="computed")

    notes: list = []
    if generation_ms.source == "absent" and first_token_monotonic is not None:
        computed_generation = (finished_monotonic - first_token_monotonic) * 1000.0
        if single_round_no_tools:
            generation_ms = MetricValue(value=round(max(computed_generation, 0.0), 3), source="computed")
        else:
            generation_ms = MetricValue(value=round(max(computed_generation, 0.0), 3), source="inferred")
            notes.append(
                "generation_ms inferred: tool calls ran this turn, so the span from first "
                "token to completion also includes tool time, not pure decode"
            )

    tools_ms = _tools_phase(tool_events)

    phases = Phases(
        queue_wait_ms=queue_wait_ms,
        load_ms=load_ms,
        prefill_ms=prefill_ms,
        generation_ms=generation_ms,
        tools_ms=tools_ms,
        total_ms=total_ms,
    )
    notes.extend(_coherence_notes(phases))

    return ExecutionMetrics(
        phases=phases,
        tokens=_tokens_phase(usage_tokens),
        scope=scope,
        engine=engine,
        observed_at=observed_at,
        notes=tuple(notes),
    )
