"""src/bench/runner.py — INF-04 A3: plan / run / compare a benchmark.

Four operations, each doing exactly what its name says and nothing a caller
would have to double-check:

  plan()      pure bookkeeping — loads the suite, computes how many cases
              the budget actually allows, and (if this model's decode speed
              has ever been learned, `llm_core.local_speed`) a duration
              estimate. NEVER touches a model, a process, or the admission
              gate. `state` starts and stays `"planned"` until `start()`.

  start()     the only place a model is actually called. Sequential, one
              case at a time — the whole point of the "un solo modelo
              grande a la vez y en serie" rule is that nothing here EVER
              awaits two `stream_llm` calls concurrently, so there is no
              lock to forget: the loop itself is the serialization.  Every
              case goes through `vram_admission.admit()` first, the exact
              same gate `routes/chat_routes.py` calls before a chat turn —
              no "internal benchmark" exception (T13/T16). Persists the run
              after every case (`DATA_DIR/benchmarks/<id>.json`, atomic), so
              a kill -9 mid-run loses at most the sample in flight.

  cancel()    cooperative: flips a flag `start()`'s loop checks between
              chunks of the CURRENT case's stream and after every case.
              Never touches another run's reservations or processes.

  compare()   pure — reads two persisted runs, never a live probe, and
              never mutates either one.

`reconcile_on_start()` is T12: since a benchmark run's cases execute INSIDE
this process (there is no detached subprocess or external engine job the
way `src/agent_runs.py`/`src/media_runs.py` runs have), a run still marked
`running`/`preparing`/`evaluating`/`waiting_resources` when this process
starts up again can only mean the previous process died mid-run — there is
no "ask the engine" reconciliation step to run first, unlike those two
modules. It is unconditionally moved to `interrupted`.

Why `compare()` does not import `tests/eval/ablation.py::judge_improvement`
(CONTRATO_INF04 A3 asks to check): that function judges a DIFFERENT pair of
axes (success rate vs. mean token COST, with a cost-ratio ceiling) over
`DimensionResult` objects built from a live `EvalApp` subprocess — there is
no cost ratio here (§10 is about SPEED and QUALITY, not tokens spent), and
this module's inputs are two already-finished `BenchmarkRun`s, not a
subprocess harness. The judging SHAPE is deliberately mirrored instead:
quality is the gate that must not regress FIRST, and only once it holds
does the secondary axis (`judge_improvement`'s cost ratio; here, the
gen_tps median move against a variability threshold) decide the verdict —
same discipline, honestly not the same function, because forcing one
functions's shape onto the other's inputs would need to invent facts
neither one measures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.atomic_io import atomic_write_json
from src.bench import suites
from src.contracts.base import now_iso
from src.contracts.inference import (
    BenchmarkRun, Comparison, ComparisonDeltas, InferenceProfile,
    RunBudget, RunConditions, RunInterruption, RunSample, RunStat,
    RunSummary, SampleQuality, SampleSizes,
    RUN_STATES_IN_FLIGHT, RUN_STATES_TERMINAL,
)
from src.execution_metrics import build_execution_metrics

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

#: A case with no `max_tokens` of its own only affects the PLAN-TIME duration
#: estimate below — never how many tokens `start()` actually asks for
#: (`_run_one_case` uses the case's own limit, or `stream_llm`'s own
#: default, when a case does not set one).
_DEFAULT_MAX_TOKENS_ESTIMATE = 256

#: `compare()`'s two numeric gates (A3): a candidate must be at least this
#: much faster (median gen_tps, percent) to even be CONSIDERED an
#: improvement, and this many samples deep on both sides before the
#: comparison is trusted at all — "pocas muestras... -> inconclusive" (T10).
_MIN_IMPROVEMENT_PCT = 10.0
_MIN_SAMPLES = 3


# ── storage: DATA_DIR/benchmarks/<run_id>.json, one file per run ────────────

def _runs_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "benchmarks")


class RunNotFound(ValueError):
    """No run file exists for the requested id."""


class RunError(Exception):
    """A run file exists but could not be read (corrupt/unreadable)."""


class RunStateError(ValueError):
    """An operation was asked for on a run in the wrong state to allow it."""


def _validate_run_id(run_id: str) -> str:
    if not run_id or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


def _run_path(run_id: str) -> str:
    return os.path.join(_runs_dir(), f"{_validate_run_id(run_id)}.json")


def _load_envelope(run_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_run_path(run_id), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise RunError(f"run {run_id!r} is unreadable: {e}") from e
    return raw if isinstance(raw, dict) else None


def _save(run: BenchmarkRun, *, owner: Optional[str] = None) -> None:
    """`owner` is bookkeeping A1's `BenchmarkRun` contract has no field for
    (a run's SHAPE does not depend on who asked for it) — kept alongside
    the contract's own `to_dict()`, the same envelope pattern
    `src/launch_receipts.py` uses for `history` and `src/bench/profiles.py`
    for `last_run_id`. `owner=None` (every call except the one inside
    `plan()`) keeps whatever owner the file already had, so `start()`'s
    repeated saves-after-each-case never have to re-thread it by hand."""
    os.makedirs(_runs_dir(), exist_ok=True)
    if owner is None:
        existing = _load_envelope(run.id)
        owner = (existing or {}).get("owner") or ""
    atomic_write_json(_run_path(run.id), {"run": run.to_dict(), "owner": owner}, indent=2)


def get(run_id: str) -> Optional[BenchmarkRun]:
    envelope = _load_envelope(run_id)
    if envelope is None:
        return None
    return BenchmarkRun.parse(envelope.get("run"))


def owner_of(run_id: str) -> str:
    envelope = _load_envelope(run_id)
    return str((envelope or {}).get("owner") or "")


def list_runs(limit: int = 50) -> List[BenchmarkRun]:
    """Every run, most recently touched first. A run file that fails to
    parse is skipped, never crashes the whole listing."""
    d = _runs_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)
    out: List[BenchmarkRun] = []
    for name in names[:max(0, int(limit))]:
        run_id = name[:-len(".json")]
        try:
            run = get(run_id)
        except RunError as e:
            logger.warning("skipping unreadable benchmark run %s: %s", run_id, e)
            continue
        if run is not None:
            out.append(run)
    return out


# ── plan() — pure, never touches a model ────────────────────────────────────

def _estimate_seconds(profile: InferenceProfile, cases: Sequence[Any], cases_planned: int) -> Optional[float]:
    """A3: "una estimación de duración a partir de `llm_core.local_speed`
    ... `null` si no hay velocidad conocida — nunca inventes". No learned
    speed for this exact model (never generalized from a different one, a
    different quantization, or a different machine) means `None`, full
    stop — this function does not fall back to a hardcoded guess."""
    if cases_planned <= 0:
        return None
    try:
        from src import llm_core
        speed = llm_core.local_speed(profile.model.artifact_id)
    except Exception:  # noqa: BLE001 - a missing estimate must never break planning
        speed = None
    if not speed or speed <= 0:
        return None
    known_limits = [c.max_tokens for c in cases if c.max_tokens]
    avg_tokens = statistics.mean(known_limits) if known_limits else _DEFAULT_MAX_TOKENS_ESTIMATE
    return round((cases_planned * avg_tokens) / speed, 1)


def plan(
    profile: InferenceProfile, suite_id: str, budget: Any, owner: str, *,
    baseline_run_id: Optional[str] = None,
) -> BenchmarkRun:
    """A3: build a `BenchmarkRun` in state `"planned"`. Loads the suite (so
    a bad `suite_id` fails HERE, not on `start()`), resolves the budget into
    `cases_planned`, and computes the duration estimate — nothing else.
    Persisted immediately so `start(run.id)` can be a separate call/request,
    per §01's "abrir la pantalla no ejecuta nada": a plan is real evidence a
    person can look at before deciding to spend anything.
    """
    suite = suites.load_suite(suite_id)  # SuiteNotFound propagates as-is
    parsed_budget = budget if isinstance(budget, RunBudget) else RunBudget.parse(budget or {}, "budget")

    total_cases = len(suite["cases"]) * parsed_budget.repeats
    cases_planned = total_cases if parsed_budget.max_cases is None else min(total_cases, parsed_budget.max_cases)
    estimate_seconds = _estimate_seconds(profile, suite["cases"], cases_planned)

    run_id = f"bench_{uuid.uuid4().hex[:12]}"
    raw = {
        "id": run_id,
        "suite_id": suite["id"],
        "suite_version": suite["version"],
        "profile": profile.to_dict(),
        "baseline_run_id": baseline_run_id,
        "state": "planned",
        "budget": parsed_budget.to_dict(),
        "conditions": {},
        "samples": [],
        "summary": {"cases_planned": cases_planned, "estimate_seconds": estimate_seconds},
        "interruptions": [],
        "started_at": None,
        "finished_at": None,
    }
    run = BenchmarkRun.parse(raw)
    _save(run, owner=str(owner or ""))
    return run


# ── start()/cancel() — the only place a model is actually called ───────────

#: run_id -> True once `cancel()` has been asked for. Checked by `start()`'s
#: own loop (between chunks of the in-flight case, and between cases) —
#: in-process only, same lifetime as the run's execution itself: a fresh
#: process (T12's `reconcile_on_start`) never has a stale entry to worry
#: about, because nothing here survives a restart in memory anyway.
_cancel_requested: Dict[str, bool] = {}


def _is_cancelled(run_id: str) -> bool:
    return bool(_cancel_requested.get(run_id))


def _case_messages(case: Any) -> List[Dict[str, Any]]:
    if case.prompt is not None:
        return [{"role": "user", "content": case.prompt}]
    return [dict(m) for m in (case.messages or ())]


def _ollama_gen_overrides(profile: InferenceProfile) -> Optional[Dict[str, Any]]:
    """A3: "para Ollama num_ctx/keep_alive/num_gpu vía
    src/model_load_options; para llama-server ninguna". Only these three
    per-request knobs travel — never the whole `profile.options` blob,
    which may carry server-start-only fields for OTHER implementations that
    would be silently wrong here. Validated through the same
    `sanitize_options` every saved default already goes through, so a
    malformed profile cannot smuggle an unsafe value into the request."""
    if profile.engine.implementation != "ollama" or not profile.options:
        return None
    from src.model_load_options import sanitize_options
    candidate = {k: v for k, v in profile.options.items() if k in ("num_ctx", "num_gpu", "keep_alive")}
    if not candidate:
        return None
    try:
        cleaned = sanitize_options(candidate)
    except ValueError as e:
        logger.warning("bench profile options rejected for the request (%s); running without overrides", e)
        return None
    return cleaned or None


def _parse_sse_events(chunk: str) -> List[Dict[str, Any]]:
    """`stream_llm`'s SSE text, one chunk at a time, into typed event dicts.
    Handles both `data: {...}` lines (delta/type events) and the
    `event: error\\ndata: {...}` shape `_stream_error_chunk`/
    `_stream_status_error_chunk` use — the two formats every caller of this
    generator already has to distinguish (`src/agent_loop.py`'s own
    consumer loop does the same `startswith("data: ")` check)."""
    events: List[Dict[str, Any]] = []
    is_error_event = chunk.startswith("event: error")
    for line in chunk.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if is_error_event and "type" not in data:
            data = {"type": "error", **data}
        events.append(data)
    return events


async def _run_one_case(
    run_id: str, profile: InferenceProfile, conditions: RunConditions,
    case: Any, repeat: int, owner: str,
) -> Tuple[RunSample, int]:
    """Run exactly one (case, repeat) pair through the SAME `stream_llm`/
    `vram_admission.admit` path a chat turn uses — never a shortcut that
    would let a benchmark's numbers reflect a path real usage does not take
    (§10: "reutilizar el arnés... no crear un camino especial"). Returns the
    sample and how many tokens it generated (for the run's token budget)."""
    from src import llm_core, vram_admission

    engine = profile.engine
    if not engine.host or not engine.port:
        return RunSample(
            case_id=case.id, repeat=repeat, metrics=None, quality=SampleQuality(),
            output_chars=None, error="profile has no known endpoint (engine.host/engine.port are absent)",
        ), 0

    endpoint_url = f"http://{engine.host}:{engine.port}"
    model = profile.model.artifact_id
    messages = _case_messages(case)
    max_tokens = case.max_tokens or 512
    gen_overrides = _ollama_gen_overrides(profile)

    waited_out: Dict[str, Any] = {}
    try:
        await vram_admission.admit(endpoint_url, model, owner=owner, waited_out=waited_out)
    except vram_admission.AdmissionCancelled as e:
        return RunSample(
            case_id=case.id, repeat=repeat, metrics=None, quality=SampleQuality(),
            output_chars=None, error=f"admission refused: {e}",
        ), 0

    kwargs: Dict[str, Any] = {"max_tokens": max_tokens, "workload": "background"}
    if conditions.temperature is not None:
        kwargs["temperature"] = conditions.temperature
    if conditions.seed is not None and gen_overrides is not None:
        gen_overrides = dict(gen_overrides)
        gen_overrides.setdefault("seed", conditions.seed)
    if gen_overrides:
        kwargs["gen_overrides"] = gen_overrides

    started_monotonic = time.monotonic()
    first_token_monotonic: Optional[float] = None
    parts: List[str] = []
    usage_payload: Dict[str, Any] = {}
    error_text: Optional[str] = None

    agen = llm_core.stream_llm(endpoint_url, model, messages, **kwargs)
    try:
        async for chunk in agen:
            for event in _parse_sse_events(chunk):
                if event.get("type") == "error":
                    error_text = str(event.get("error") or event.get("text") or "stream error")
                elif event.get("type") == "usage":
                    usage_payload = event.get("data") or {}
                elif "delta" in event:
                    if first_token_monotonic is None:
                        first_token_monotonic = time.monotonic()
                    parts.append(str(event.get("delta") or ""))
            if _is_cancelled(run_id):
                # §09/A3: the in-flight case is cancelled through stream_llm's
                # OWN generator-close path, not by aborting the HTTP call out
                # from under it or discarding what streamed so far unlabeled.
                error_text = error_text or "cancelled"
                await agen.aclose()
                break
    finally:
        await agen.aclose()
    finished_monotonic = time.monotonic()

    output = "".join(parts)
    engine_timings = usage_payload.get("engine_timings") if isinstance(usage_payload, dict) else None
    usage_tokens: Optional[Dict[str, Any]] = None
    generated_count = 0
    output_tokens = usage_payload.get("output_tokens") if isinstance(usage_payload, dict) else None
    if isinstance(output_tokens, (int, float)) and not isinstance(output_tokens, bool):
        generated_count = int(output_tokens)
        usage_tokens = {
            "prompt": usage_payload.get("input_tokens"), "generated": output_tokens,
            "source": "reported_engine",
        }
    elif isinstance(engine_timings, dict) and isinstance(engine_timings.get("predicted_n"), (int, float)):
        generated_count = int(engine_timings["predicted_n"])
        usage_tokens = {
            "prompt": engine_timings.get("prompt_n"), "generated": engine_timings.get("predicted_n"),
            "source": "computed",
        }

    metrics = build_execution_metrics(
        started_monotonic=started_monotonic, finished_monotonic=finished_monotonic,
        first_token_monotonic=first_token_monotonic, queue_wait_s=waited_out.get("waited_s"),
        tool_events=None, engine_timings=engine_timings, usage_tokens=usage_tokens, engine=engine,
    )

    if error_text:
        quality = SampleQuality()  # not evaluated: a cut/erroring sample proves nothing about quality
    else:
        checked = suites.run_checks(case, output)
        quality = SampleQuality(passed=checked["passed"], failed_checks=tuple(checked["failed_checks"]))

    sample = RunSample(
        case_id=case.id, repeat=repeat, metrics=metrics, quality=quality,
        output_chars=len(output), error=error_text,
    )
    return sample, generated_count


def _stat(values: Sequence[float]) -> RunStat:
    if not values:
        return RunStat()
    ordered = sorted(values)
    n = len(ordered)
    median = statistics.median(ordered)
    p95_index = min(n - 1, max(0, int(round(0.95 * (n - 1)))))
    return RunStat(median=round(median, 3), p95=round(ordered[p95_index], 3), n=n)


def _summarize(samples: Sequence[RunSample], *, cases_planned: int, estimate_seconds: Optional[float]) -> RunSummary:
    judged = [s.quality.passed for s in samples if s.quality.passed is not None]
    quality_pass_rate = (sum(1 for p in judged if p) / len(judged)) if judged else None

    gen_tps_values: List[float] = []
    ttft_values: List[float] = []
    total_ms_values: List[float] = []
    for sample in samples:
        if sample.metrics is None:
            continue
        gen_ms = sample.metrics.phases.generation_ms.value
        gen_tokens = sample.metrics.tokens.generated.value
        if gen_ms and gen_ms > 0 and gen_tokens and gen_tokens > 0:
            gen_tps_values.append(gen_tokens / (gen_ms / 1000.0))
        prefill_ms = sample.metrics.phases.prefill_ms.value
        if prefill_ms is not None:
            ttft_values.append(prefill_ms)
        if sample.metrics.phases.total_ms.value is not None:
            total_ms_values.append(sample.metrics.phases.total_ms.value)

    return RunSummary(
        cases_run=len(samples), cases_planned=cases_planned, quality_pass_rate=quality_pass_rate,
        gen_tps=_stat(gen_tps_values), ttft_ms=_stat(ttft_values),
        total_ms=(round(sum(total_ms_values), 3) if total_ms_values else None),
        estimate_seconds=estimate_seconds,
    )


async def start(run_id: str) -> BenchmarkRun:
    """A3: run every planned case, one at a time, in the exact order the
    suite lists them (repeated `budget.repeats` times). Only callable from
    `"planned"` (`RunStateError` otherwise — a second `start()` on the same
    run, or one already cancelled/interrupted, must never silently re-run
    or resume from a stale samples list)."""
    envelope = _load_envelope(run_id)
    if envelope is None:
        raise RunNotFound(run_id)
    run = BenchmarkRun.parse(envelope["run"])
    owner = str(envelope.get("owner") or "")
    if run.state != "planned":
        raise RunStateError(f"cannot start run {run_id!r} in state {run.state!r}; must be 'planned'")

    suite = suites.load_suite(run.suite_id)
    if suite["version"] != run.suite_version:
        # T04-adjacent: the suite changed on disk between plan() and
        # start() — scoring the planned cases against a DIFFERENT suite
        # version would misattribute the result to a version that never
        # actually ran.
        run = replace(
            run, state="failed", finished_at=now_iso(),
            notes=run.notes + (
                f"suite {run.suite_id!r} changed from version {run.suite_version!r} "
                f"to {suite['version']!r} between plan() and start()",
            ),
        )
        _save(run, owner=owner)
        return run

    if not run.profile.engine.host or not run.profile.engine.port:
        run = replace(
            run, state="failed", finished_at=now_iso(),
            notes=run.notes + ("profile has no known endpoint (engine.host/engine.port are absent)",),
        )
        _save(run, owner=owner)
        return run

    expanded = [(case, r) for r in range(1, run.budget.repeats + 1) for case in suite["cases"]]
    if run.budget.max_cases is not None:
        expanded = expanded[:run.budget.max_cases]

    run = replace(run, state="preparing", started_at=now_iso())
    _save(run, owner=owner)
    run = replace(run, state="running")
    _save(run, owner=owner)

    _cancel_requested.pop(run_id, None)
    samples: List[RunSample] = list(run.samples)
    interruptions: List[RunInterruption] = list(run.interruptions)
    total_generated_tokens = 0
    started_monotonic = time.monotonic()
    cut_short = False

    try:
        for case, repeat in expanded:
            if _is_cancelled(run_id):
                interruptions.append(RunInterruption(at=now_iso(), reason="cancelled"))
                cut_short = True
                break
            if run.budget.max_seconds is not None and (time.monotonic() - started_monotonic) >= run.budget.max_seconds:
                interruptions.append(RunInterruption(at=now_iso(), reason="budget_seconds"))
                cut_short = True
                break
            if run.budget.max_generated_tokens is not None and total_generated_tokens >= run.budget.max_generated_tokens:
                interruptions.append(RunInterruption(at=now_iso(), reason="budget_tokens"))
                cut_short = True
                break

            sample, generated = await _run_one_case(run_id, run.profile, run.conditions, case, repeat, owner)
            samples.append(sample)
            total_generated_tokens += generated
            run = replace(run, samples=tuple(samples), interruptions=tuple(interruptions))
            _save(run, owner=owner)

            if _is_cancelled(run_id):
                interruptions.append(RunInterruption(at=now_iso(), reason="cancelled"))
                cut_short = True
                run = replace(run, interruptions=tuple(interruptions))
                _save(run, owner=owner)
                break
    finally:
        _cancel_requested.pop(run_id, None)

    run = replace(run, state="evaluating")
    _save(run, owner=owner)

    summary = _summarize(samples, cases_planned=run.summary.cases_planned, estimate_seconds=run.summary.estimate_seconds)
    if any(i.reason == "cancelled" for i in interruptions):
        final_state = "cancelled"
    elif cut_short or len(samples) < len(expanded):
        final_state = "partial"
    else:
        final_state = "completed"

    run = replace(
        run, samples=tuple(samples), interruptions=tuple(interruptions),
        summary=summary, state=final_state, finished_at=now_iso(),
    )
    _save(run, owner=owner)
    return run


def cancel(run_id: str) -> BenchmarkRun:
    """A3: stop a run. Idempotent on an already-terminal run (returns it
    unchanged, never raises "already cancelled") — a retried click must not
    become an error. A run still `"planned"` (never started) is settled
    immediately, since there is no in-flight case or reservation to wait
    on; anything else is flagged for `start()`'s own loop to notice and
    settle, preserving every sample collected so far."""
    envelope = _load_envelope(run_id)
    if envelope is None:
        raise RunNotFound(run_id)
    run = BenchmarkRun.parse(envelope["run"])
    owner = str(envelope.get("owner") or "")
    if run.state in RUN_STATES_TERMINAL:
        return run
    _cancel_requested[run_id] = True
    if run.state == "planned":
        run = replace(
            run, state="cancelled", finished_at=now_iso(),
            interruptions=run.interruptions + (RunInterruption(at=now_iso(), reason="cancelled_before_start"),),
        )
        _save(run, owner=owner)
    return run


# ── reconcile_on_start() — T12 ───────────────────────────────────────────────

def reconcile_on_start() -> Dict[str, Any]:
    """Called once from the app's own startup reconciliation pass
    (`app.py`, alongside `src.agent_runs.recover_interrupted_runs` and
    `src.crash_recovery.boot_scan`). A benchmark run's cases execute INSIDE
    this process — there is no detached subprocess or external engine job
    to ask "are you still there" the way `src/agent_runs.py`/
    `src/media_runs.py` can — so any run this fresh process finds still in
    `RUN_STATES_IN_FLIGHT` can only be one the PREVIOUS process was
    running when it stopped. Moved straight to `"interrupted"`, never
    resumed (§09: "no repetir automáticamente acciones... de un benchmark
    agentic" — the same caution, applied here to the whole run)."""
    d = _runs_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return {"checked": 0, "interrupted": []}
    interrupted: List[str] = []
    for name in names:
        run_id = name[:-len(".json")]
        _cancel_requested.pop(run_id, None)
        try:
            envelope = _load_envelope(run_id)
        except RunError as e:
            logger.warning("skipping unreadable benchmark run %s during reconcile: %s", run_id, e)
            continue
        if envelope is None:
            continue
        try:
            run = BenchmarkRun.parse(envelope["run"])
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the sweep
            logger.warning("skipping unparseable benchmark run %s during reconcile: %s", run_id, e)
            continue
        if run.state not in RUN_STATES_IN_FLIGHT:
            continue
        updated = replace(
            run, state="interrupted", finished_at=now_iso(),
            interruptions=run.interruptions + (RunInterruption(at=now_iso(), reason="process_restarted"),),
        )
        _save(updated, owner=envelope.get("owner"))
        interrupted.append(run_id)
    return {"checked": len(names), "interrupted": interrupted}


# ── compare() — pure ─────────────────────────────────────────────────────────

def _incomparable(baseline_run_id: str, candidate_run_id: str, reasons: Sequence[str],
                   sample_sizes: SampleSizes) -> Comparison:
    return Comparison(
        baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id,
        verdict="inconclusive", reasons=tuple(reasons), deltas=ComparisonDeltas(),
        sample_sizes=sample_sizes, comparable=False,
    )


def compare(baseline_run_id: str, candidate_run_id: str) -> Comparison:
    """A3: judge a candidate run against a baseline run. Pure — reads two
    persisted `BenchmarkRun`s and returns a verdict, never mutates either
    one or calls a model. See the module docstring for why this does not
    import `tests/eval/ablation.py::judge_improvement` directly.

    `comparable=False` (different suite/version/model/objective) is always
    `"inconclusive"` with a `reasons` entry naming exactly what differed —
    never silently compared anyway."""
    baseline = get(baseline_run_id)
    if baseline is None:
        raise RunNotFound(baseline_run_id)
    candidate = get(candidate_run_id)
    if candidate is None:
        raise RunNotFound(candidate_run_id)

    sample_sizes = SampleSizes(baseline=baseline.summary.gen_tps.n, candidate=candidate.summary.gen_tps.n)

    reasons: List[str] = []
    if baseline.suite_id != candidate.suite_id or baseline.suite_version != candidate.suite_version:
        reasons.append(
            f"different suite ({baseline.suite_id}@{baseline.suite_version} vs "
            f"{candidate.suite_id}@{candidate.suite_version})"
        )
    if baseline.profile.model.artifact_id != candidate.profile.model.artifact_id:
        reasons.append(
            f"different model ({baseline.profile.model.artifact_id} vs {candidate.profile.model.artifact_id})"
        )
    if baseline.profile.objective != candidate.profile.objective:
        reasons.append(f"different objective ({baseline.profile.objective} vs {candidate.profile.objective})")
    if reasons:
        return _incomparable(baseline_run_id, candidate_run_id, reasons, sample_sizes)

    b_tps, c_tps = baseline.summary.gen_tps.median, candidate.summary.gen_tps.median
    b_ttft, c_ttft = baseline.summary.ttft_ms.median, candidate.summary.ttft_ms.median
    b_qpr, c_qpr = baseline.summary.quality_pass_rate, candidate.summary.quality_pass_rate

    gen_tps_pct = round(((c_tps - b_tps) / b_tps) * 100.0, 2) if (b_tps and b_tps > 0 and c_tps is not None) else None
    ttft_pct = round(((c_ttft - b_ttft) / b_ttft) * 100.0, 2) if (b_ttft and b_ttft > 0 and c_ttft is not None) else None
    qpr_delta = round(c_qpr - b_qpr, 4) if (b_qpr is not None and c_qpr is not None) else None
    deltas = ComparisonDeltas(gen_tps_median_pct=gen_tps_pct, ttft_median_pct=ttft_pct, quality_pass_rate_delta=qpr_delta)

    def _verdict(verdict: str, reason: str) -> Comparison:
        return Comparison(
            baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id,
            verdict=verdict, reasons=(reason,), deltas=deltas, sample_sizes=sample_sizes, comparable=True,
        )

    # T10: too few samples on either side — a comparison that cannot rule
    # out noise is inconclusive, never an accidental "no_change"/"improvement".
    if sample_sizes.baseline < _MIN_SAMPLES or sample_sizes.candidate < _MIN_SAMPLES:
        return _verdict(
            "inconclusive",
            f"sample too small (baseline n={sample_sizes.baseline}, candidate n={sample_sizes.candidate}; "
            f"need >= {_MIN_SAMPLES} in both)",
        )
    if b_qpr is None or c_qpr is None:
        return _verdict("inconclusive", "quality pass rate is unknown for at least one run")

    # Quality is the gate that must hold FIRST (§10's puerta de calidad) —
    # a candidate that regresses quality never reaches the speed check,
    # no matter how much faster it is (T09).
    if c_qpr < b_qpr:
        return _verdict("regression", f"quality pass rate dropped from {b_qpr:.2f} to {c_qpr:.2f}")

    if gen_tps_pct is None or b_tps is None or c_tps is None:
        return _verdict("inconclusive", "no measured decode speed to compare")

    if gen_tps_pct <= -_MIN_IMPROVEMENT_PCT:
        return _verdict("regression", f"gen_tps median fell {abs(gen_tps_pct):.1f}% (> {_MIN_IMPROVEMENT_PCT:.0f}%)")

    if gen_tps_pct < _MIN_IMPROVEMENT_PCT:
        return _verdict("no_change", f"gen_tps median moved {gen_tps_pct:.1f}%, below the {_MIN_IMPROVEMENT_PCT:.0f}% improvement threshold")

    # The gain clears the 10% bar; it must ALSO clear the p95-median spread
    # observed on either side (A3's explicit variability proxy — "no es un
    # test estadístico", just a guard against calling noise an improvement).
    b_spread = (baseline.summary.gen_tps.p95 - baseline.summary.gen_tps.median) \
        if (baseline.summary.gen_tps.p95 is not None and baseline.summary.gen_tps.median is not None) else 0.0
    c_spread = (candidate.summary.gen_tps.p95 - candidate.summary.gen_tps.median) \
        if (candidate.summary.gen_tps.p95 is not None and candidate.summary.gen_tps.median is not None) else 0.0
    spread = max(b_spread, c_spread, 0.0)
    absolute_gain = c_tps - b_tps

    if absolute_gain > spread:
        return _verdict(
            "improvement",
            f"gen_tps median rose {gen_tps_pct:.1f}% (>= {_MIN_IMPROVEMENT_PCT:.0f}%), quality held, "
            f"and the gain ({absolute_gain:.2f} tok/s) exceeds the observed p95-median spread ({spread:.2f})",
        )
    return _verdict(
        "inconclusive",
        f"gen_tps median rose {gen_tps_pct:.1f}% but the gain ({absolute_gain:.2f} tok/s) does not exceed "
        f"the observed p95-median spread ({spread:.2f}); noise comparable to the improvement",
    )
