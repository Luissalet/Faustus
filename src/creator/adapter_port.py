"""adapter_port.py — WP10: one contract every media engine speaks.

`docs/spec/creator/plan/docs/03_ARQUITECTURA_Y_CONTRATOS.md`, "Puerto de
motores": every adapter offers the same seven operations, and this module is
where that shape lives — a `Protocol` plus the plain-data types every method
takes or returns, so a route, a test harness or `production_runs.py` can hold
an `AdapterPort` without caring whether it talks to ComfyUI over HTTP or to
`ffmpeg` over `subprocess`.

**Call classification** (the ficha asks for it explicitly — which calls are
pure, which are a read of external state, which cause an effect):

* ``describe()`` — QUERY. It may probe the engine (is it reachable, what
  version), but never queues a job to find that out — MOD/06_ADAPTADORES:
  "un healthcheck no ejecuta una generación para autodeclararse listo."
* ``plan()`` — PURE. No network call, no filesystem write, no job created.
  It validates task/params/input shape and says what *would* happen.
* ``submit()`` — EFFECT. The one call that can cause an external side
  effect (a queued render, a spawned process). Takes resolved occurrence
  ids and a *controlled* `Staging`, never an arbitrary path a caller named.
* ``status()`` — QUERY. Reads external/local job state; changes nothing.
* ``cancel()`` — EFFECT. Attempts to stop a job; the answer names what
  actually happened (`requested|accepted|confirmed|too_late|unknown`) rather
  than pretending a stop request is itself a stop.
* ``collect()`` — QUERY against the engine, but a bounded LOCAL effect: it
  downloads/copies into the `tmp` directory the caller gave it, hashes and
  inspects what landed there, and returns validated bytes. It never writes
  to the artifact store itself — that is `production_runs.py`'s job, once
  it has decided the collected output is real.
* ``reconcile()`` — QUERY. Asks "what actually happened to `job_id`" without
  ever re-submitting anything. An adapter that cannot answer this honestly
  declares so in its manifest (`supports_reconcile=False`) instead of
  guessing — ADR-07: "no reintentar automáticamente mutaciones desconocidas."

None of this replaces `src/media_runs.py`, which stays the single owner of
the render lifecycle (ADR-02). `src/creator/adapters/comfyui.py` is a thin
wrapper that calls straight through to it; the ffmpeg adapter is the first
one with no pre-existing MediaRun authority of its own, and
`src/creator/production_runs.py` is where its jobs get projected onto the
SAME `media_runs` row shape rather than a second lifecycle table.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

__all__ = [
    "SUBMIT_STATES", "CANCEL_OUTCOMES", "STATUS_STATES",
    "AdapterManifest", "AdapterPlan", "Staging", "SubmitResult",
    "StatusResult", "CancelResult", "CollectedOutput", "CollectResult",
    "AdapterPort", "new_staging_dir",
]

#: What `submit()` may answer — the ficha's exact vocabulary. `accepted` is
#: an ordinary queued job; `rejected_before_queue` is a refusal the adapter
#: is SURE happened before anything reached the engine (bad params, a
#: missing binary, a graph the engine read and refused); `accepted_uncertain`
#: is the outbox case — a call that may or may not have reached the far
#: side, exactly `media_runs.STATUSES`'s `submit_unknown` under another name.
SUBMIT_STATES = ("accepted", "rejected_before_queue", "accepted_uncertain")

#: What `cancel()` may answer. "Cancelado" (ADR-07/03_ARQUITECTURA) never
#: means a publication was undone — it means one of these five things
#: happened, and the caller is told which.
CANCEL_OUTCOMES = ("requested", "accepted", "confirmed", "too_late", "unknown")

#: What `status()`/`reconcile()` may answer, deliberately the SAME strings
#: `src/media_runs.py::STATUSES` already uses for the states an adapter can
#: actually be asked about (no `submit_pending`/`pending` here — those are
#: MediaRun's own outbox bookkeeping before a job exists to ask about).
STATUS_STATES = ("queued", "running", "completed", "failed", "cancelled", "unknown")


# ── manifest ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AdapterManifest:
    """What this adapter is, right now — never a promise about a render it
    has not tried. `available=False` with a `reason` is how "ffmpeg is not
    installed" or "ComfyUI is unreachable" travels to a caller deciding
    whether to offer the button at all."""

    name: str
    engine: str
    version: str
    tasks: Tuple[str, ...]
    supports_reconcile: bool
    supports_cancel: bool
    available: bool = True
    reason: str = ""
    limits: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "engine": self.engine, "version": self.version,
            "tasks": list(self.tasks),
            "supports_reconcile": self.supports_reconcile,
            "supports_cancel": self.supports_cancel,
            "available": self.available, "reason": self.reason,
            "limits": dict(self.limits),
        }


# ── plan ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AdapterPlan:
    """What `submit()` would do, computed without doing it. `engine_plan` is
    an adapter-private payload (a rendered ComfyUI graph reference, an
    ffmpeg task descriptor) that only that SAME adapter's `submit()` is
    expected to read back — nothing outside the adapter inspects its shape."""

    ok: bool
    adapter: str
    task: str
    estimated_cost: Dict[str, Any] = field(default_factory=dict)
    missing: Tuple[str, ...] = ()
    detail: str = ""
    engine_plan: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "adapter": self.adapter, "task": self.task,
            "estimated_cost": dict(self.estimated_cost),
            "missing": list(self.missing), "detail": self.detail,
            # engine_plan is deliberately NOT serialised to callers outside
            # the process — it can carry resolved template values. Routes
            # keep it server-side and hand submit() the object, not its dict.
        }


# ── staging ─────────────────────────────────────────────────────────────

def new_staging_dir(prefix: str = "faustus-creator-stage-") -> str:
    """A bounded, process-owned temporary directory — never a path a caller
    named. Adapters materialise input occurrences here and nowhere else."""
    import tempfile
    return tempfile.mkdtemp(prefix=prefix)


@dataclass(frozen=True)
class Staging:
    """The controlled handoff `submit()` receives instead of raw paths.

    `input_paths` maps each RESOLVED occurrence id (already checked to
    belong to `owner` by whoever built this — see
    `src/creator/adapters/base.py::stage_inputs`) to a path INSIDE
    `workdir` that this occurrence's bytes were copied/hard-linked into.
    An adapter that wants a filesystem path for occurrence X reads
    `input_paths[X]`; it never resolves an occurrence id itself and never
    accepts a path from a request body."""

    owner: str
    project_id: str
    workdir: str
    input_paths: Dict[str, str] = field(default_factory=dict)


# ── submit / status / cancel ───────────────────────────────────────────

@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    state: str
    reason: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in SUBMIT_STATES:
            raise ValueError(f"submit state must be one of {SUBMIT_STATES}, got {self.state!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"job_id": self.job_id, "state": self.state,
                "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class StatusResult:
    job_id: str
    state: str
    detail: str = ""
    progress: Optional[float] = None

    def __post_init__(self) -> None:
        if self.state not in STATUS_STATES:
            raise ValueError(f"status state must be one of {STATUS_STATES}, got {self.state!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"job_id": self.job_id, "state": self.state,
                "detail": self.detail, "progress": self.progress}


@dataclass(frozen=True)
class CancelResult:
    job_id: str
    outcome: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in CANCEL_OUTCOMES:
            raise ValueError(f"cancel outcome must be one of {CANCEL_OUTCOMES}, got {self.outcome!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"job_id": self.job_id, "outcome": self.outcome, "detail": self.detail}


# ── collect ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CollectedOutput:
    """One file `collect()` pulled down, already hashed and inspected.
    `valid=False` means exactly what it says: the bytes landed but failed
    inspection (a truncated download, a file ffprobe cannot read), and the
    caller MUST NOT register it as a completed artifact."""

    path: str
    sha256: str
    byte_size: int
    media_type: str
    valid: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256,
                "byte_size": self.byte_size, "media_type": self.media_type,
                "valid": self.valid, "detail": self.detail}


@dataclass(frozen=True)
class CollectResult:
    ok: bool
    outputs: Tuple[CollectedOutput, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "outputs": [o.to_dict() for o in self.outputs],
                "detail": self.detail}


# ── the protocol itself ────────────────────────────────────────────────

@runtime_checkable
class AdapterPort(Protocol):
    """Every adapter under `src/creator/adapters/` implements this. See the
    module docstring for which calls are pure/query/effect."""

    name: str

    def describe(self) -> AdapterManifest: ...

    def plan(self, op: str, params: Mapping[str, Any],
             inputs: Sequence[str]) -> AdapterPlan: ...

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult: ...

    def status(self, job_id: str) -> StatusResult: ...

    def cancel(self, job_id: str) -> CancelResult: ...

    def collect(self, job_id: str, tmp: str) -> CollectResult: ...

    def reconcile(self, job_id: str) -> StatusResult: ...


def new_job_id(adapter_name: str) -> str:
    """One id shape shared by every adapter, so a job id's prefix alone
    says which adapter minted it — useful in logs and in tests that assert
    on the id without importing the adapter module."""
    safe = "".join(c for c in adapter_name if c.isalnum())[:12] or "job"
    return f"{safe}_{uuid.uuid4().hex[:20]}"
