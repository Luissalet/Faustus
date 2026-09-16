"""adapters/whisper.py — WP15: ASR adapter over faster-whisper (optional).

`docs/spec/creator/plan/docs/06_ADAPTADORES_MULTIMEDIA.md`, "WhisperX y
alineación": faster-whisper already ships in this repo's optional voice
stack (`services/stt/stt_service.py`, `requirements-voice.txt`) and already
gives word-level timestamps (`word_timestamps=True`) without needing
WhisperX's separate forced-alignment pass — this adapter reuses exactly
that capability instead of adding a second heavy dependency. WhisperX
itself stays an explicitly-not-installed extension point (see
``_probe_alignment_backend`` below): a caller cannot get more precision
than faster-whisper's own word timestamps until one is actually wired in.

**No weights are ever touched by importing this module or by ``describe()``/
``plan()``.** ``describe()`` only imports ``faster_whisper`` (a pure Python
import, no model download) to answer "is the engine installed at all";
loading an actual ``WhisperModel`` (which DOES fetch/READ weights) happens
only inside a queued job's worker, i.e. only after a real ``submit()``.

**Queue discipline.** Jobs run in a bounded ``ThreadPoolExecutor`` sized by
the ``creator_asr_max_concurrent`` setting (default 1 — "un motor pesado a
la vez" is the same conservative default WP30's ``resources.py`` picks for
GPU jobs); a job queued behind a full pool sits in ``queued`` state until a
worker thread is free. `submit()` never blocks the caller — it enqueues and
returns immediately, exactly like the ffmpeg adapter's queue-of-one but
asynchronous rather than synchronous (an ASR pass over real audio can run
for minutes, unlike a `trim`).

**GPU admission.** When `device` resolves to `"cuda"`, the worker calls
`src.creator.resources.admit()` (WP30) before loading the model, and always
releases in a `finally` — CPU jobs never touch `resources.admit()` at all,
matching the ficha's "respeta creator/resources.admit si el modelo ASR va a
GPU" (a CPU job has nothing to admit for).

**Diarization stays opt-in and honest.** ``describe()`` reports
``diarization_available`` from whether ``pyannote.audio`` is importable —
never whether it is actually usable, because running a real pyannote
pipeline needs a pretrained checkpoint pulled from the HuggingFace hub on
first use, and CONTRATO.md rule 6 / the ficha's "no se descargan pesos al
importar" forbid that happening as a side effect of a describe/plan/submit
call. This adapter therefore never invokes a diarization pipeline itself;
`speaker_id` assignment from real diarization is `src.creator.asr`'s
documented, currently-absent extension point (see that module's docstring)
— every cue this adapter's jobs produce carries `speaker_id: None`, never a
fabricated `speaker_1` that looks like a real turn boundary nobody computed.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult, new_job_id,
)
from src.creator.adapters.base import BaseAdapter

NAME = "whisper"
SUPPORTED_TASKS = ("transcribe",)

#: faster-whisper's own published model sizes — this list is the adapter's
#: OWN documented menu (it never asks the engine to enumerate itself, which
#: would require loading something); an unknown name is rejected by
#: `plan()` before anything is queued.
KNOWN_MODELS: Tuple[str, ...] = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v2", "large-v3",
)

#: Rough, deliberately conservative real-time factors (wall-clock seconds
#: per audio second) used ONLY to shape `plan()`'s `estimated_cost` — never
#: treated as a promise, and always labelled `"estimated_seconds"` rather
#: than a bare number a caller might mistake for a guarantee. CPU-bound
#: assumption (a `cuda` device is faster; this adapter does not special-
#: case that because a wrong-but-conservative overestimate is the safe
#: failure mode for a cost preview, an underestimate is not).
_REALTIME_FACTOR: Dict[str, float] = {
    "tiny": 0.3, "tiny.en": 0.3, "base": 0.5, "base.en": 0.5,
    "small": 0.9, "small.en": 0.9, "medium": 1.8, "medium.en": 1.8,
    "large-v2": 3.0, "large-v3": 3.0,
}

_JOBS_LOCK = threading.Lock()
#: job_id -> mutable job state. In-memory only (see module docstring on the
#: ffmpeg adapter for the same choice): an ASR job is this PROCESS's worker
#: pool, not a durable outbox — a restart loses in-flight jobs the same way
#: it loses an in-flight ffmpeg subprocess, and `describe()` never claims
#: `supports_reconcile=True` for that reason.
_JOBS: Dict[str, Dict[str, Any]] = {}

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_SIZE = 0


# ── settings (read fresh every call, same discipline as resources.py) ──────

def default_model() -> str:
    from src.settings import get_setting
    model = str(get_setting("creator_asr_default_model", "base") or "base").strip()
    return model if model in KNOWN_MODELS else "base"


def default_device() -> str:
    from src.settings import get_setting
    device = str(get_setting("creator_asr_device", "cpu") or "cpu").strip().lower()
    return device if device in ("cpu", "cuda") else "cpu"


def max_concurrent() -> int:
    from src.settings import get_setting
    try:
        value = int(get_setting("creator_asr_max_concurrent", 1) or 1)
    except (TypeError, ValueError):
        value = 1
    return value if value >= 1 else 1


def _get_executor(size: int) -> ThreadPoolExecutor:
    global _EXECUTOR, _EXECUTOR_SIZE
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None or _EXECUTOR_SIZE != size:
            if _EXECUTOR is not None:
                _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = ThreadPoolExecutor(max_workers=size, thread_name_prefix="creator-asr")
            _EXECUTOR_SIZE = size
        return _EXECUTOR


def _probe_engine() -> Tuple[bool, str, str]:
    """`(available, reason, version)` — import only, never a model load."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, ("faster-whisper is not installed; install it "
                       "(requirements-voice.txt) to enable ASR — faustus never "
                       "installs it automatically"), ""
    version = ""
    try:
        import importlib.metadata as _md
        version = _md.version("faster-whisper")
    except Exception:  # noqa: BLE001 - version is best-effort only
        version = ""
    return True, "", version


def _probe_alignment_backend() -> str:
    """What produced word timestamps for a completed job — always
    faster-whisper's own decoder-level word timestamps today; a WhisperX
    forced-alignment pass is a genuine extension point this string names
    but does not implement (see module docstring)."""
    return "faster_whisper_word_timestamps"


def _probe_diarization_available() -> bool:
    """Import-only probe, exactly like `_probe_engine` — never a reason to
    run a pipeline (see module docstring)."""
    try:
        import pyannote.audio  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed, or broken; either way: unavailable
        return False
    return True


# ── the adapter ──────────────────────────────────────────────────────────

class WhisperAdapter(BaseAdapter):
    """One local faster-whisper install, run as a queued background worker.

    `model_loader`, when given, REPLACES the real `WhisperModel(...)`
    construction — this is how tests (and `src.creator.asr`'s own test
    suite) exercise the full describe/plan/submit/status/collect/cancel
    lifecycle with a deterministic fake engine and no real weights, per
    CONTRATO.md rule 8 ("fakes solo para motores externos/modelos")."""

    name = NAME

    def __init__(self, *, model_loader: Optional[Callable[[str, str], Any]] = None,
                engine_available: Optional[Callable[[], Tuple[bool, str, str]]] = None) -> None:
        self._model_loader = model_loader
        self._engine_available = engine_available or _probe_engine

    def _load_model(self, model_size: str, device: str) -> Any:
        if self._model_loader is not None:
            return self._model_loader(model_size, device)
        from faster_whisper import WhisperModel
        compute_type = "float16" if device == "cuda" else "int8"
        return WhisperModel(model_size, device=device, compute_type=compute_type)

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        available, reason, version = self._engine_available()
        return AdapterManifest(
            name=NAME, engine="whisper", version=version,
            tasks=SUPPORTED_TASKS, supports_reconcile=False, supports_cancel=True,
            available=available, reason=reason,
            limits={
                "models": list(KNOWN_MODELS), "default_model": default_model(),
                "default_device": default_device(), "max_concurrent": max_concurrent(),
                "align_words": True, "alignment_backend": _probe_alignment_backend(),
                "languages": "auto-detect (omit 'language'), or force an ISO-639-1 code",
                "diarization_available": _probe_diarization_available(),
            },
        )

    # ── plan (pure) ────────────────────────────────────────────────────

    def plan(self, op: str, params: Mapping[str, Any], inputs: Sequence[str]) -> AdapterPlan:
        if op not in SUPPORTED_TASKS:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("op",),
                               detail=f"unsupported task {op!r}; whisper adapter supports "
                                      f"{', '.join(SUPPORTED_TASKS)}")
        if len(inputs) != 1:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("inputs",),
                               detail="transcribe needs exactly one audio input")
        available, reason, _version = self._engine_available()
        if not available:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("asr_engine",), detail=reason)

        params = dict(params or {})
        language = params.get("language")
        if language is not None and not (isinstance(language, str) and language.strip()):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                               detail="'language' must be a non-empty string, or omitted for auto-detect")
        align_words = params.get("align_words", True)
        if not isinstance(align_words, bool):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                               detail="'align_words' must be a boolean")
        model = str(params.get("model") or default_model())
        if model not in KNOWN_MODELS:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                               detail=f"unknown model {model!r}; choose one of {', '.join(KNOWN_MODELS)}")
        device = str(params.get("device") or default_device())
        if device not in ("cpu", "cuda"):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                               detail="'device' must be 'cpu' or 'cuda'")

        duration = params.get("duration_seconds")
        cost: Dict[str, Any] = {"seconds": "unknown"}
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            rtf = _REALTIME_FACTOR.get(model, 2.0)
            cost = {"estimated_seconds": round(float(duration) * rtf, 1),
                    "duration_seconds": float(duration), "model": model, "device": device,
                    "note": "a conservative, offline heuristic — never measured"}

        engine_plan = {
            "input_order": list(inputs),
            "params": {"language": language, "align_words": align_words,
                      "model": model, "device": device,
                      "duration_seconds": duration if isinstance(duration, (int, float)) else None},
        }
        return AdapterPlan(ok=True, adapter=NAME, task=op, estimated_cost=cost, engine_plan=engine_plan)

    # ── submit (effect — enqueues; the real transcribe runs in a worker) ──

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="invalid_plan", detail=plan.detail)
        available, reason, _version = self._engine_available()
        if not available:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="asr_engine_unavailable", detail=reason)

        engine_plan = plan.engine_plan or {}
        order = engine_plan.get("input_order") or []
        if len(order) != 1:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="invalid_input_count",
                                detail="whisper adapter expects exactly one staged audio input")
        occurrence_id = order[0]
        try:
            source_path = staging.input_paths[occurrence_id]
        except KeyError:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="input_not_staged",
                                detail=f"occurrence {occurrence_id} was not materialised in staging")
        if not os.path.exists(source_path):
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="input_missing", detail=f"{source_path} does not exist")

        params = dict(engine_plan.get("params") or {})
        job_id = new_job_id(NAME)
        cancel_event = threading.Event()
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                "state": "queued", "detail": "", "error": "", "segments": [],
                "language": None, "language_probability": None,
                "cancel_event": cancel_event, "created_at": time.time(),
                "params": dict(params), "future": None,
            }
        executor = _get_executor(max_concurrent())
        future: Future = executor.submit(self._execute, job_id, source_path, params, cancel_event)
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id]["future"] = future
        return SubmitResult(job_id=job_id, state="accepted")

    # ── the worker body (runs on the executor's thread) ───────────────

    def _execute(self, job_id: str, source_path: str, params: Dict[str, Any],
                cancel_event: threading.Event) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            if cancel_event.is_set():
                job["state"] = "cancelled"
                return
            job["state"] = "running"

        model_size = str(params.get("model") or default_model())
        device = str(params.get("device") or default_device())
        language = params.get("language") or None
        align_words = bool(params.get("align_words", True))

        admission = None
        admitted = False
        try:
            if device == "cuda":
                from src.creator import resources
                footprint = resources.Footprint(vram_bytes=resources.DEFAULT_UNMEASURED_VRAM_BYTES)
                admission = resources.admit(footprint, device, run_id=job_id)
                if not admission.ok:
                    with _JOBS_LOCK:
                        job = _JOBS.get(job_id)
                        if job is not None:
                            job.update(state="failed",
                                      error=f"GPU not admitted: {admission.reason}")
                    return
                admitted = True

            model = self._load_model(model_size, device)
            kwargs: Dict[str, Any] = {"word_timestamps": align_words}
            if language:
                kwargs["language"] = language
            segments_iter, info = model.transcribe(source_path, **kwargs)

            segments: List[Dict[str, Any]] = []
            for seg in segments_iter:
                with _JOBS_LOCK:
                    if cancel_event.is_set():
                        job = _JOBS.get(job_id)
                        if job is not None:
                            job["state"] = "cancelled"
                        return
                words: List[Dict[str, Any]] = []
                seg_words = getattr(seg, "words", None) or []
                if align_words and seg_words:
                    for w in seg_words:
                        conf = getattr(w, "probability", None)
                        w_start = getattr(w, "start", None)
                        w_end = getattr(w, "end", None)
                        words.append({
                            "text": str(getattr(w, "word", "")).strip(),
                            # A word faster-whisper could not time keeps
                            # `None`, never a fabricated 0.0 — SUB01's own
                            # acceptance criterion ("no se inventan
                            # timestamps para completar la tabla").
                            "start": float(w_start) if isinstance(w_start, (int, float))
                                     and not isinstance(w_start, bool) else None,
                            "end": float(w_end) if isinstance(w_end, (int, float))
                                   and not isinstance(w_end, bool) else None,
                            "confidence": float(conf) if isinstance(conf, (int, float))
                                          and not isinstance(conf, bool) else None,
                        })
                confs = [w["confidence"] for w in words if w["confidence"] is not None]
                segment_confidence = round(sum(confs) / len(confs), 4) if confs else None
                text = str(getattr(seg, "text", "") or "").strip()
                if not text:
                    continue
                segments.append({
                    "start": float(getattr(seg, "start", 0.0) or 0.0),
                    "end": float(getattr(seg, "end", 0.0) or 0.0),
                    "text": text, "words": words, "confidence": segment_confidence,
                })

            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    return
                if job["state"] == "cancelled":
                    return  # a cancel that landed mid-loop already won; see above
                job.update(
                    state="completed", segments=segments,
                    language=str(getattr(info, "language", "") or language or "") or None,
                    language_probability=(
                        float(getattr(info, "language_probability", None))
                        if getattr(info, "language_probability", None) is not None else None
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - a worker exception fails the job, not the process
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.update(state="failed", error=str(exc))
        finally:
            if admitted:
                from src.creator import resources
                resources.release(job_id)

    # ── status (query) ────────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return StatusResult(job_id=job_id, state="unknown", detail="no such job")
            state = job["state"]
            detail = job.get("error") or job.get("detail") or ""
            return StatusResult(job_id=job_id, state=state, detail=detail)

    # ── cancel (effect) ───────────────────────────────────────────────

    def cancel(self, job_id: str) -> CancelResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return CancelResult(job_id=job_id, outcome="unknown", detail="no such job")
            state = job["state"]
            if state in ("completed", "failed"):
                return CancelResult(job_id=job_id, outcome="too_late", detail=state)
            if state == "cancelled":
                return CancelResult(job_id=job_id, outcome="confirmed", detail="already cancelled")
            job["cancel_event"].set()
            future: Optional[Future] = job.get("future")
            if state == "queued" and future is not None and future.cancel():
                job["state"] = "cancelled"
                return CancelResult(job_id=job_id, outcome="accepted",
                                    detail="cancelled before it started running")
            return CancelResult(job_id=job_id, outcome="requested",
                                detail="cancellation requested; the worker checks between segments")

    # ── collect (query + bounded local effect) ────────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return CollectResult(ok=False, detail="no such job")
            if job["state"] != "completed":
                return CollectResult(ok=False, detail=f"job is {job['state']}, not completed")
            payload = {
                "language": job.get("language"),
                "language_probability": job.get("language_probability"),
                "alignment_backend": _probe_alignment_backend(),
                "segments": list(job.get("segments") or []),
            }
        os.makedirs(tmp, exist_ok=True)
        out_path = os.path.join(tmp, f"{job_id}.json")
        data = json.dumps(payload).encode("utf-8")
        with open(out_path, "wb") as fh:
            fh.write(data)
        sha256 = hashlib.sha256(data).hexdigest()
        output = CollectedOutput(
            path=out_path, sha256=sha256, byte_size=len(data),
            media_type="application/json", valid=True,
            detail=f"{len(payload['segments'])} segment(s)",
        )
        return CollectResult(ok=True, outputs=(output,), detail=output.detail)

    # ── reconcile (query) — BaseAdapter's default (== status()) is honest
    # here: this adapter has no durable outbox to reconcile against (see
    # module docstring), so `supports_reconcile=False` + "asking again" is
    # all it can truthfully offer. ──


def reset_for_tests() -> None:
    """Tests only: forget every in-process job/executor this module is
    tracking, so one test's jobs never leak into the next."""
    global _EXECUTOR, _EXECUTOR_SIZE
    with _JOBS_LOCK:
        _JOBS.clear()
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False)
        _EXECUTOR = None
        _EXECUTOR_SIZE = 0


ADAPTER_FACTORY = WhisperAdapter

__all__ = ["NAME", "SUPPORTED_TASKS", "KNOWN_MODELS", "WhisperAdapter",
          "default_model", "default_device", "max_concurrent",
          "ADAPTER_FACTORY", "reset_for_tests"]
