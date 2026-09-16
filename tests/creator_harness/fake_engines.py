"""tests/creator_harness/fake_engines.py — WP36: configurable fake adapters.

`src/creator/adapter_port.py`'s module docstring is explicit about what each
call classifies as (pure/query/effect) and what an honest answer looks like
for an uncertain outcome. A REAL engine cannot be made to reliably reproduce
"the response was lost after the job was accepted" or "the worker crashed
mid-render" on demand — so this module is a small family of `AdapterPort`
implementations, one knob (`scenario`) each, that reproduce those exact
frontiers from the failure matrix
(`docs/spec/creator/plan/docs/08_PRUEBAS_Y_ACEPTACION.md`).

`FakeAdapter.describe()` NEVER claims `available=True` by declaring itself a
stand-in for a real engine — its `engine` field is always `"fake"`, so a test
asserting the contract (WP36 closing criterion: "un stub passing no se
presenta como soporte del motor") cannot mistake a green fake-engine test for
evidence a real ComfyUI/ffmpeg run ever happened. See
`docs/spec/creator/WP36.md`, "Niveles de evidencia".
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult, new_job_id,
)

__all__ = [
    "SCENARIOS", "FakeAdapter", "disk_full", "corrupt_bytes_for_job",
]

#: Every scenario this module knows how to reproduce, matched 1:1 to a row
#: of the failure matrix in `matrix.py`. A scenario not in this tuple is a
#: programming error in a test, not a silently-ignored default.
SCENARIOS = (
    "success",
    "timeout",
    "corrupt_output",
    "truncated_output",
    "cancel_too_late",
    "cancel_races_completion",
    "engine_crash_mid_job",
    "disk_full",
    "slow_response",
    "submit_lost_response",
    "reject_before_queue",
)


@dataclass
class _Job:
    scenario: str
    state: str = "running"          # queued|running|completed|failed|cancelled|unknown
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    output_bytes: bytes = b""
    output_media_type: str = "application/octet-stream"


class FakeAdapter:
    """One `AdapterPort` whose behaviour is entirely driven by the
    `scenario` given at `submit()` time (read from `plan.engine_plan
    ["scenario"]`, set by `plan()`), so a single instance can be reused
    across every row of the failure matrix without reconstructing it per
    case. Every method is otherwise a faithful, minimal implementation of
    the real protocol — no method is a no-op stub that only LOOKS like it
    implements the contract."""

    name = "fake"

    def __init__(self, *, slow_seconds: float = 0.05) -> None:
        self._jobs: Dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._slow_seconds = slow_seconds

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        return AdapterManifest(
            name=self.name, engine="fake", version="harness-1",
            tasks=("noop",), supports_reconcile=True, supports_cancel=True,
            available=True, reason="",
            limits={"note": "test double; never evidence a real engine ran"},
        )

    # ── plan (pure) ────────────────────────────────────────────────────

    def plan(self, op: str, params: Mapping[str, Any],
             inputs: Sequence[str]) -> AdapterPlan:
        scenario = str((params or {}).get("scenario") or "success")
        if scenario not in SCENARIOS:
            return AdapterPlan(ok=False, adapter=self.name, task=op,
                                missing=("scenario",),
                                detail=f"unknown scenario {scenario!r}")
        if op != "noop":
            return AdapterPlan(ok=False, adapter=self.name, task=op,
                                missing=("op",), detail=f"fake adapter only supports 'noop', got {op!r}")
        return AdapterPlan(ok=True, adapter=self.name, task=op,
                            estimated_cost={}, engine_plan={"scenario": scenario, "inputs": list(inputs)})

    # ── submit (effect) ───────────────────────────────────────────────

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="invalid_plan", detail=plan.detail)
        scenario = str((plan.engine_plan or {}).get("scenario") or "success")

        if scenario == "reject_before_queue":
            return SubmitResult(job_id="", state="rejected_before_queue",
                                 reason="engine_refused", detail="fake: refused before queueing")

        job_id = new_job_id(self.name)
        job = _Job(scenario=scenario)
        with self._lock:
            self._jobs[job_id] = job

        if scenario == "submit_lost_response":
            # The exact "Submit" row of the matrix: the engine DID accept
            # the job (it is in `self._jobs`, `status()` will find it) but
            # the confirmation never reached the caller. `accepted_uncertain`
            # is the only honest state to return here.
            job.state = "queued"
            return SubmitResult(job_id=job_id, state="accepted_uncertain",
                                 reason="response_lost", detail="fake: ack was dropped in transit")

        # Every other scenario is a normal accept; the OUTCOME diverges in
        # status()/collect()/cancel() below, exactly like a real engine that
        # accepted a job and then misbehaves later in its lifecycle.
        job.state = "running"
        return SubmitResult(job_id=job_id, state="accepted")

    # ── status (query) ────────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return StatusResult(job_id=job_id, state="unknown", detail="no such job")

        if job.scenario == "timeout":
            # A job that never advances past "running" no matter how many
            # times status() is asked — the caller's OWN timeout/backoff is
            # what must end this, never the adapter inventing a result.
            return StatusResult(job_id=job_id, state="running", detail="fake: never finishes")

        if job.scenario == "engine_crash_mid_job":
            # The worker forgot this job existed — `unknown`, not `failed`:
            # a crash is not proof the job failed, only that the answer is
            # no longer available (adapter_port.py: "unknown" cousin of
            # submit_unknown).
            with self._lock:
                job.state = "unknown"
            return StatusResult(job_id=job_id, state="unknown",
                                 detail="fake: worker process disappeared mid-job")

        if job.scenario == "slow_response":
            time.sleep(self._slow_seconds)

        if job.state == "cancelled":
            return StatusResult(job_id=job_id, state="cancelled", detail="fake: cancelled")

        with self._lock:
            if job.state == "running":
                job.state = "completed"
                job.output_bytes = _payload_for(job_id, job.scenario)
                job.output_media_type = "application/octet-stream"
            state = job.state
        return StatusResult(job_id=job_id, state=state, detail="")

    # ── cancel (effect) ───────────────────────────────────────────────

    def cancel(self, job_id: str) -> CancelResult:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return CancelResult(job_id=job_id, outcome="unknown", detail="no such job")

        if job.scenario == "cancel_too_late":
            with self._lock:
                job.state = "completed"
                if not job.output_bytes:
                    job.output_bytes = _payload_for(job_id, job.scenario)
            return CancelResult(job_id=job_id, outcome="too_late",
                                 detail="fake: the job had already completed")

        if job.scenario == "cancel_races_completion":
            # The exact "Cancelación" matrix row: completion and cancel
            # arrive at once. The canonical resolution this harness checks
            # for is "cancel wins if it was requested first" — a LATE
            # completion must never silently overwrite a cancellation
            # (mirrors src/creator/adapters/ffmpeg.py's own race handling).
            with self._lock:
                if job.state == "cancelled":
                    return CancelResult(job_id=job_id, outcome="confirmed",
                                         detail="fake: already cancelled")
                job.cancel_requested = True
                job.state = "cancelled"
            return CancelResult(job_id=job_id, outcome="confirmed",
                                 detail="fake: cancel won the race")

        with self._lock:
            if job.state in ("completed", "failed"):
                return CancelResult(job_id=job_id, outcome="too_late", detail=job.state)
            job.state = "cancelled"
        return CancelResult(job_id=job_id, outcome="confirmed", detail="fake: cancelled")

    # ── collect (query + bounded local effect) ────────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return CollectResult(ok=False, detail="no such job")
        if job.state == "cancelled" and job.scenario == "cancel_races_completion":
            return CollectResult(ok=False, detail="job was cancelled; nothing to collect")
        if job.state != "completed":
            return CollectResult(ok=False, detail=f"job is {job.state}, not completed")

        os.makedirs(tmp, exist_ok=True)
        out_path = os.path.join(tmp, f"{job_id}.bin")

        if job.scenario == "disk_full":
            # Simulated by the caller wrapping this call in the
            # `disk_full()` context manager below (monkeypatches `open`) —
            # this branch just always tries to write, so the injected
            # failure is the ONLY thing that can make it fail.
            with open(out_path, "wb") as fh:
                fh.write(job.output_bytes)
            sha, size = _sha256(out_path)
            return CollectResult(ok=True, outputs=(
                CollectedOutput(path=out_path, sha256=sha, byte_size=size,
                                 media_type=job.output_media_type, valid=True),
            ))

        data = job.output_bytes or _payload_for(job_id, job.scenario)
        if job.scenario == "truncated_output":
            data = data[: max(1, len(data) // 3)]
        with open(out_path, "wb") as fh:
            fh.write(data)
        sha, size = _sha256(out_path)

        if job.scenario in ("corrupt_output", "truncated_output"):
            # This is the "collect valida hash y nunca registra salida
            # corrupta" contract point: `ok=False`, the bad file IS
            # returned (for logging/inspection) but a caller MUST NOT
            # register it as a completed artifact.
            return CollectResult(ok=False, outputs=(
                CollectedOutput(path=out_path, sha256=sha, byte_size=size,
                                 media_type=job.output_media_type, valid=False,
                                 detail=f"fake: {job.scenario}"),
            ), detail=f"output failed inspection: {job.scenario}")

        return CollectResult(ok=True, outputs=(
            CollectedOutput(path=out_path, sha256=sha, byte_size=size,
                             media_type=job.output_media_type, valid=True),
        ))

    # ── reconcile (query) ─────────────────────────────────────────────

    def reconcile(self, job_id: str) -> StatusResult:
        return self.status(job_id)


def _payload_for(job_id: str, scenario: str) -> bytes:
    return f"fake-output:{scenario}:{job_id}".encode("utf-8") * 64


def _sha256(path: str):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def corrupt_bytes_for_job(job_id: str) -> bytes:
    """What `collect()` would have written for `corrupt_output`, exposed so
    a test can assert on the exact bytes without re-deriving the format."""
    return _payload_for(job_id, "corrupt_output")


class disk_full:
    """Context manager that makes every `open(..., "wb"/"ab"/"xb")` and
    `shutil.disk_usage()` call inside it behave as if the target volume is
    full: `open()` for writing raises `OSError(errno.ENOSPC, ...)` and
    `disk_usage()` reports zero free bytes. Monkeypatches the real `open`
    builtin and `shutil.disk_usage` for the duration of the `with` block
    only, and always restores them, even on an exception inside the block —
    `08_PRUEBAS_Y_ACEPTACION.md`'s "disco lleno o descarga interrumpida"
    fault injection, without touching the actual filesystem or requiring a
    tiny loopback volume."""

    def __init__(self) -> None:
        self._real_open = None
        self._real_disk_usage = None

    def __enter__(self) -> "disk_full":
        import builtins
        import errno

        self._real_open = builtins.open
        self._real_disk_usage = shutil.disk_usage

        def fake_open(file, mode: str = "r", *args, **kwargs):
            if any(m in mode for m in ("w", "a", "x", "+")):
                raise OSError(errno.ENOSPC, "No space left on device (fake_engines.disk_full)")
            return self._real_open(file, mode, *args, **kwargs)

        import collections

        _Usage = collections.namedtuple("usage", ["total", "used", "free"])

        def fake_disk_usage(path):
            return _Usage(total=0, used=0, free=0)

        builtins.open = fake_open
        shutil.disk_usage = fake_disk_usage
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import builtins

        builtins.open = self._real_open
        shutil.disk_usage = self._real_disk_usage
