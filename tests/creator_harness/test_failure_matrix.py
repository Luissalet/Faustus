"""tests/creator_harness/test_failure_matrix.py — WP36: the minimum failure
matrix, proved row by row against `fake_engines.FakeAdapter`.

Every row here is `fake_engine` evidence (see `docs/spec/creator/WP36.md`,
"Niveles de evidencia") — it proves the RUNTIME/CONTRACT shape a real
adapter must also honour (the same `AdapterPort` types, the same
`submit()`/`cancel()`/`collect()` vocabulary WP10's real adapters return),
never that a real ComfyUI/ffmpeg engine behaves this way under the same
fault. `FakeAdapter.describe().engine == "fake"` on every row, so a report
generated from this file cannot be mistaken for real-engine coverage.

`test_resources_under_failure.py` covers the one `owner="wp36"` matrix row
this file does NOT (`resources_two_endpoints_same_gpu`) — that one is about
`src/creator/resources.py`'s admission gate, not an adapter.

A second block below proves the SAME four rows (no engine, cancel tardío,
salida corrupta, timeout) against the REAL `WhisperAdapter` (WP15), fed a
deterministic fake `faster_whisper` engine — `WhisperAdapter.__init__`'s own
`model_loader`/`engine_available` injection points, the SAME mechanism
`tests/test_creator_wp15_asr.py` uses (no `faster-whisper` installed or
imported here). `FakeAdapter` above proves the GENERIC port contract; this
block proves whisper's OWN queue/cancel/collect implementation honours it
under the same faults.
"""
from __future__ import annotations

import os
import threading
import time
from typing import List, Optional

import pytest

from src.creator.adapter_port import CANCEL_OUTCOMES, STATUS_STATES, SUBMIT_STATES, Staging
from tests.creator_harness import fixtures as fx
from tests.creator_harness.fake_engines import FakeAdapter, disk_full
from tests.creator_harness.matrix import MATRIX, rows_for_adapter

#: Every row this file is expected to cover — a stray row added to
#: `matrix.py` with `owner="wp36"` and no matching test here fails
#: `test_every_wp36_row_has_a_fake_engine_test` loudly instead of quietly
#: not being checked.
_COVERED_BY_THIS_FILE = frozenset({
    "submit_response_lost", "execution_worker_restart_loses_lease",
    "cancel_races_completion", "cancel_arrives_too_late",
    "collect_disk_full", "collect_corrupt_output", "collect_truncated_output",
})


def _staging(workdir: str) -> Staging:
    return Staging(owner="alice", project_id="p1", workdir=workdir, input_paths={})


def _submit(adapter: FakeAdapter, scenario: str, workdir: str):
    plan = adapter.plan("noop", {"scenario": scenario}, [])
    assert plan.ok is True, f"fake plan() for scenario {scenario!r} must itself be valid"
    return adapter.submit(plan, _staging(workdir))


@pytest.fixture()
def adapter() -> FakeAdapter:
    return FakeAdapter(slow_seconds=0.01)


# ═══════════════════════════════════════════════════════════════════════
# Submit — "engine acepta pero la respuesta se pierde" → estado incierto
# ═══════════════════════════════════════════════════════════════════════

def test_submit_response_lost_is_accepted_uncertain(adapter, tmp_path):
    result = _submit(adapter, "submit_lost_response", str(tmp_path))
    assert result.state == "accepted_uncertain"
    assert result.state in SUBMIT_STATES
    # And the job DID land on the engine side — reconcile(), not a second
    # submit, is how a caller is expected to resolve this.
    status = adapter.status(result.job_id)
    assert status.state in ("queued", "running", "completed")


# ═══════════════════════════════════════════════════════════════════════
# Ejecución — "worker reinicia o pierde su lease" → resultado antiguo no
# se puede aplicar a otro intento (fencing: reading a crashed job's status
# again gives `unknown`, never a stale `completed` resurrected from nowhere)
# ═══════════════════════════════════════════════════════════════════════

def test_worker_crash_mid_job_reports_unknown_not_a_resurrected_result(adapter, tmp_path):
    result = _submit(adapter, "engine_crash_mid_job", str(tmp_path))
    assert result.state == "accepted"

    first = adapter.status(result.job_id)
    assert first.state == "unknown"
    # Asking again must not somehow recover a "completed" out of nothing —
    # `unknown` is sticky for this scenario, never flips to a finished job
    # id another intento could mistake for its own.
    second = adapter.status(result.job_id)
    assert second.state == "unknown"

    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is False, "an unknown job must never be collectible as if it completed"


# ═══════════════════════════════════════════════════════════════════════
# Cancelación — completar y cancelar a la vez / cancel tardío
# ═══════════════════════════════════════════════════════════════════════

def test_cancel_races_completion_has_a_canonical_winner(adapter, tmp_path):
    result = _submit(adapter, "cancel_races_completion", str(tmp_path))
    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome in CANCEL_OUTCOMES
    assert outcome.outcome == "confirmed"
    # The canonical resolution is consistent: status() agrees with what
    # cancel() just said, not a third, contradictory answer.
    assert adapter.status(result.job_id).state == "cancelled"
    # A cancelled job's bytes are never handed out as a completed collect.
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is False


def test_cancel_after_completion_is_too_late(adapter, tmp_path):
    result = _submit(adapter, "cancel_too_late", str(tmp_path))
    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome == "too_late"
    # And the completed result is untouched by the late cancel attempt.
    assert adapter.status(result.job_id).state == "completed"
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is True


# ═══════════════════════════════════════════════════════════════════════
# Collect — disco lleno, salida corrupta, descarga truncada
# ═══════════════════════════════════════════════════════════════════════

def test_collect_disk_full_fails_without_announcing_completed(adapter, tmp_path):
    result = _submit(adapter, "disk_full", str(tmp_path))
    # Drain the job to "completed" first (status() advances running->completed).
    assert adapter.status(result.job_id).state == "completed"

    with disk_full():
        with pytest.raises(OSError):
            adapter.collect(result.job_id, str(tmp_path / "out"))
    # The disk-full failure must not have left a file behind claiming success
    # once the injected fault is lifted.
    out_dir = tmp_path / "out"
    leftover = list(out_dir.glob("*")) if out_dir.exists() else []
    assert not any(p.stat().st_size > 0 for p in leftover), (
        "a disk-full collect must not leave a completed-looking output file behind")


def test_collect_corrupt_output_is_rejected_not_registered(adapter, tmp_path):
    result = _submit(adapter, "corrupt_output", str(tmp_path))
    assert adapter.status(result.job_id).state == "completed"
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is False
    assert collected.outputs and collected.outputs[0].valid is False
    assert len(collected.outputs[0].sha256) == 64  # still hashed, for forensics — just not trusted


def test_collect_truncated_output_is_rejected_not_registered(adapter, tmp_path):
    result = _submit(adapter, "truncated_output", str(tmp_path))
    assert adapter.status(result.job_id).state == "completed"
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is False
    assert collected.outputs and collected.outputs[0].valid is False


# ═══════════════════════════════════════════════════════════════════════
# baseline: a clean success path exists too — a matrix of only failures
# would not prove the fake engine can also say yes.
# ═══════════════════════════════════════════════════════════════════════

def test_success_scenario_completes_and_collects_cleanly(adapter, tmp_path):
    result = _submit(adapter, "success", str(tmp_path))
    assert result.state == "accepted"
    assert adapter.status(result.job_id).state == "completed"
    collected = adapter.collect(result.job_id, str(tmp_path / "out"))
    assert collected.ok is True
    assert collected.outputs[0].valid is True


def test_reject_before_queue_never_reaches_status(adapter):
    result = _submit(adapter, "reject_before_queue", "/tmp")
    assert result.state == "rejected_before_queue"
    assert result.job_id == ""
    assert adapter.status("").state == "unknown"


def test_timeout_scenario_never_finishes_on_its_own(adapter, tmp_path):
    result = _submit(adapter, "timeout", str(tmp_path))
    for _ in range(5):
        assert adapter.status(result.job_id).state == "running"


# ═══════════════════════════════════════════════════════════════════════
# WP15 WhisperAdapter — the same four failure rows, against the REAL
# adapter fed a fake faster_whisper engine (WhisperAdapter's own injection
# points, no dependency installed).
# ═══════════════════════════════════════════════════════════════════════

class _FakeWord:
    def __init__(self, word: str, start: Optional[float], end: Optional[float],
                probability: Optional[float]):
        self.word, self.start, self.end, self.probability = word, start, end, probability


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str, words: Optional[List[_FakeWord]] = None):
        self.start, self.end, self.text, self.words = start, end, text, words or []


class _FakeInfo:
    def __init__(self, language: str = "en", language_probability: float = 0.9):
        self.language, self.language_probability = language, language_probability


class _FakeWhisperModel:
    """A deterministic stand-in for `faster_whisper.WhisperModel`, the same
    shape `tests/test_creator_wp15_asr.py::FakeModel` uses: `transcribe()`
    returns `(segments_iterator, info)`, and a `block_event` (when given)
    makes the generator wait before yielding — how a `timeout` scenario is
    driven without a real sleep race. `raise_mid_stream`, when given, is
    raised out of the generator AFTER the first segment — the "el motor
    produce una salida corrupta a mitad" shape for an adapter whose
    `collect()` writes its OWN well-formed JSON (never raw engine bytes), so
    the honest failure mode is the WORKER catching the exception and ending
    the job `failed`, never a `completed` job with garbage inside."""

    def __init__(self, segments: Optional[List[_FakeSegment]] = None,
                block_event: Optional[threading.Event] = None,
                raise_mid_stream: Optional[Exception] = None):
        self.segments = segments if segments is not None else [_FakeSegment(0.0, 1.0, "ok")]
        self.block_event = block_event
        self.raise_mid_stream = raise_mid_stream

    def transcribe(self, path: str, **kwargs):
        def _gen():
            if self.block_event is not None:
                self.block_event.wait(timeout=10)
            for i, seg in enumerate(self.segments):
                if self.raise_mid_stream is not None and i == 1:
                    raise self.raise_mid_stream
                yield seg
        return _gen(), _FakeInfo()


def _whisper_adapter(*, model=None, available=True):
    from src.creator.adapters.whisper import WhisperAdapter
    model = model or _FakeWhisperModel()
    return WhisperAdapter(
        model_loader=lambda model_size, device: model,
        engine_available=lambda: (available, "" if available else "fake: engine not installed", "fake-1.0"))


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture()
def whisper_wav(tmp_path) -> str:
    return fx.write_bytes(str(tmp_path / "in.wav"), fx.wav_bytes(seconds=0.3))


def _whisper_staging(occ_id: str, path: str, workdir: str) -> Staging:
    return Staging(owner="alice", project_id="p1", workdir=workdir, input_paths={occ_id: path})


def test_whisper_no_engine_is_rejected_before_queue(whisper_wav, tmp_path):
    adapter = _whisper_adapter(available=False)
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    assert plan.ok is False
    assert "asr_engine" in plan.missing

    result = adapter.submit(plan, _whisper_staging("occ_1", whisper_wav, str(tmp_path)))
    assert result.state == "rejected_before_queue"
    assert result.state in SUBMIT_STATES
    assert result.job_id == ""


def test_whisper_cancel_after_completion_is_too_late(whisper_wav, tmp_path):
    adapter = _whisper_adapter()
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _whisper_staging("occ_1", whisper_wav, str(tmp_path)))
    assert result.state == "accepted"
    assert _wait_until(lambda: adapter.status(result.job_id).state == "completed")

    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome in CANCEL_OUTCOMES
    assert outcome.outcome == "too_late"
    # The completed transcript is untouched by the late cancel.
    assert adapter.status(result.job_id).state == "completed"
    collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
    assert collected.ok is True


def test_whisper_engine_crash_mid_stream_fails_the_job_not_a_corrupt_completed(whisper_wav, tmp_path):
    """The whisper-shaped version of "collect valida hash y rechaza salida
    corrupta sin registrar": this adapter's `collect()` always writes ITS
    OWN well-formed JSON from in-memory state, so a "corrupt output"
    fault injected at the ENGINE (faster-whisper raising mid-stream, the
    real-world shape of a driver crash or an OOM kill) must never surface
    as a `completed` job with a garbage transcript — it must fail the job
    outright, and `collect()` on a failed job must refuse."""
    model = _FakeWhisperModel(
        segments=[_FakeSegment(0.0, 1.0, "first ok segment"), _FakeSegment(1.0, 2.0, "never reached")],
        raise_mid_stream=RuntimeError("fake: engine crashed mid-stream"))
    adapter = _whisper_adapter(model=model)
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _whisper_staging("occ_1", whisper_wav, str(tmp_path)))
    assert result.state == "accepted"

    assert _wait_until(lambda: adapter.status(result.job_id).state in ("failed", "completed"))
    status = adapter.status(result.job_id)
    assert status.state == "failed", (
        "an engine crash mid-transcribe must fail the job, never complete it with a partial/garbage transcript")
    assert "fake: engine crashed mid-stream" in status.detail

    collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
    assert collected.ok is False, "collect() must refuse a failed job — nothing here is a valid completed output"


def test_whisper_timeout_never_completes_on_its_own(whisper_wav, tmp_path):
    """A blocked engine (the real shape: a stuck GPU call, a hung decode)
    must leave the job `running` for as long as the caller keeps asking —
    the adapter must never invent a `completed`/`failed` result just
    because time has passed. The caller's own timeout/backoff is what ends
    this, exactly like `fake_engines.FakeAdapter`'s `timeout` scenario."""
    block = threading.Event()  # never set — the engine call hangs
    adapter = _whisper_adapter(model=_FakeWhisperModel(block_event=block))
    plan = adapter.plan("transcribe", {}, ["occ_1"])
    result = adapter.submit(plan, _whisper_staging("occ_1", whisper_wav, str(tmp_path)))
    assert result.state == "accepted"

    assert _wait_until(lambda: adapter.status(result.job_id).state == "running")
    for _ in range(5):
        state = adapter.status(result.job_id).state
        assert state == "running", f"whisper job must stay running while blocked, got {state!r}"
        time.sleep(0.02)

    # Clean up: release the blocked worker thread so the test process can
    # exit promptly (this adapter's ThreadPoolExecutor is module-global).
    block.set()
    assert _wait_until(lambda: adapter.status(result.job_id).state == "completed")


# ═══════════════════════════════════════════════════════════════════════
# coverage bookkeeping — every declared matrix row this package owns has a
# fake-engine test, and every fake_scenario matrix.py declares is exercised.
# ═══════════════════════════════════════════════════════════════════════

def test_every_wp36_row_has_a_fake_engine_test():
    owned = {row.id for row in rows_for_adapter("wp36") if row.fake_scenario}
    missing = owned - _COVERED_BY_THIS_FILE
    assert not missing, f"matrix rows with a fake_scenario but no test here: {sorted(missing)}"


def test_no_stray_scenario_in_the_matrix_without_a_fake_engine_case():
    from tests.creator_harness.fake_engines import SCENARIOS
    declared_here = {row.fake_scenario for row in MATRIX if row.fake_scenario}
    assert declared_here <= set(SCENARIOS)
