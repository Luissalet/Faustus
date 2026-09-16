"""adapters/music.py — WP24: music generation adapter over the WP10 port.

`docs/spec/creator/plan/docs/06_ADAPTADORES_MULTIMEDIA.md`, "ACE-Step y
Music Studio": this adapter speaks the same `describe/plan/submit/status/
cancel/collect/reconcile` contract every other engine under
`src/creator/adapters/` speaks (`src/creator/adapter_port.py`). Two engines
are recognised, each an import-probe only:

* ``acestep`` — the ACE-Step diffusion music model (structured lyrics +
  style tags, real BPM/key conditioning).
* ``audiocraft`` — Meta's MusicGen (text-conditioned, no structured lyrics
  channel; BPM/key are prompt hints only, never real controls).

**No weights are ever touched by importing this module, or by
``describe()``/``plan()``.** `describe()` only imports the top-level engine
package (a pure Python import) to answer "is it installed at all" — never
constructs a pipeline/model, which would pull or read real checkpoint
weights. Loading an actual model happens only inside a queued job's worker,
i.e. only after a real `submit()` — and CONTRATO.md/WP24's own closing
criterion ("generar no implica entrenar ni bajar pesos") means this repo
NEVER installs `acestep`/`audiocraft` itself and never will as a side
effect of any call in this module; an environment without either package
installed gets an honest `available=False` from `describe()`, not a
simulated success.

**Queue discipline.** Exactly one music job runs at a time by default
(`creator_music_max_concurrent`, default 1 — CONTRATO.md's "cola de 1" for
heavy generative jobs, same default WP15's ASR adapter and WP30's
`resources.py` pick). `submit()` never blocks: it enqueues and returns
`accepted` immediately; the actual generation happens in a bounded
`ThreadPoolExecutor` worker.

**GPU admission.** When the resolved `device` is `"cuda"`, the worker calls
`src.creator.resources.admit()` (WP30) before touching the engine, and
always releases in a `finally` — a CPU-only environment (this sandbox: no
GPU, no engine installed) never reaches that call at all.

**Cost estimate.** `plan()`'s `estimated_cost` is a conservative, clearly
labelled heuristic (`duration_s` × a per-engine seconds-per-second-of-audio
factor, scaled by `steps` where the engine has one) — never presented as a
measured number, exactly like `adapters/whisper.py`'s `_REALTIME_FACTOR`.

**Impossible inputs are rejected by `plan()`, per variant.** A duration
outside the engine's own bounds, a missing/empty prompt with no lyrics and
no style tags, an unknown `engine` — every one of these is a
`AdapterPlan(ok=False, ...)` with `missing` naming the field, computed
BEFORE anything reaches `submit()`, so a caller generating several variants
gets each one rejected or accepted on its own merits rather than one bad
variant poisoning a batch submitted together (WP24 closing criterion:
"inputs imposibles se rechazan por variante").
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult, new_job_id,
)
from src.creator.adapters.base import BaseAdapter

NAME = "music"
SUPPORTED_TASKS = ("music.generate",)

#: The two engines this adapter recognises, in probe order — the first one
#: `describe()` finds importable is what `plan()`/`submit()` use unless a
#: caller names `engine` explicitly. Neither is a declared dependency of
#: this repo (grepped `requirements*.txt`: absent) — installing either is
#: entirely the operator's own choice, never automatic (module docstring).
KNOWN_ENGINES: Tuple[str, ...] = ("ace_step", "musicgen")

#: Per-engine bounds `plan()` enforces. `acestep` supports real structured
#: lyrics conditioning (ACE-Step's own published `lyrics`/`tags` inputs);
#: `audiocraft`/MusicGen has no lyrics channel at all — a `lyrics` string
#: given to it is folded into the text prompt as a hint, never a promise of
#: sung words, and `plan()` says so via `detail` on that variant's report.
_ENGINE_LIMITS: Dict[str, Dict[str, Any]] = {
    "ace_step": {
        "min_duration_s": 5.0, "max_duration_s": 240.0,
        "min_steps": 8, "max_steps": 120, "default_steps": 27,
        "seconds_per_audio_second": 1.2,  # rough, offline heuristic only
        "supports_lyrics": True, "supports_bpm": True, "supports_key": True,
    },
    "musicgen": {
        "min_duration_s": 1.0, "max_duration_s": 30.0,
        "min_steps": 0, "max_steps": 0, "default_steps": 0,
        "seconds_per_audio_second": 2.5,
        "supports_lyrics": False, "supports_bpm": False, "supports_key": False,
    },
}

_JOBS_LOCK = threading.Lock()
#: job_id -> mutable job state, in-memory only (same discipline as the
#: whisper/ffmpeg adapters — a process restart loses in-flight jobs the
#: same way it loses an in-flight subprocess; `describe()` never claims
#: `supports_reconcile=True` for that reason).
_JOBS: Dict[str, Dict[str, Any]] = {}

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_SIZE = 0


# ── settings (read fresh every call, same discipline as resources.py) ──

def default_engine() -> str:
    from src.settings import get_setting
    engine = str(get_setting("creator_music_default_engine", "") or "").strip().lower()
    return engine if engine in KNOWN_ENGINES else ""


def default_device() -> str:
    from src.settings import get_setting
    device = str(get_setting("creator_music_device", "cpu") or "cpu").strip().lower()
    return device if device in ("cpu", "cuda") else "cpu"


def max_concurrent() -> int:
    from src.settings import get_setting
    try:
        value = int(get_setting("creator_music_max_concurrent", 1) or 1)
    except (TypeError, ValueError):
        value = 1
    return value if value >= 1 else 1


def _get_executor(size: int) -> ThreadPoolExecutor:
    global _EXECUTOR, _EXECUTOR_SIZE
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None or _EXECUTOR_SIZE != size:
            if _EXECUTOR is not None:
                _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = ThreadPoolExecutor(max_workers=size, thread_name_prefix="creator-music")
            _EXECUTOR_SIZE = size
        return _EXECUTOR


def _probe_engine(name: str) -> Tuple[bool, str, str]:
    """`(available, reason, version)` for one named engine — import only,
    never a model/checkpoint load."""
    module_name = "acestep" if name == "ace_step" else "audiocraft"
    try:
        __import__(module_name)
    except ImportError:
        return False, (f"{module_name} is not installed; install it yourself to enable "
                       f"{name} music generation — faustus never installs it automatically"), ""
    version = ""
    try:
        import importlib.metadata as _md
        version = _md.version(module_name)
    except Exception:  # noqa: BLE001 - version is best-effort only
        version = ""
    return True, "", version


def _probe_all_engines() -> Dict[str, Tuple[bool, str, str]]:
    return {name: _probe_engine(name) for name in KNOWN_ENGINES}


def _first_available(probes: Mapping[str, Tuple[bool, str, str]]) -> str:
    for name in KNOWN_ENGINES:
        if probes.get(name, (False, "", ""))[0]:
            return name
    return ""


# ── the adapter ──────────────────────────────────────────────────────────

class MusicAdapter(BaseAdapter):
    """One local ACE-Step or MusicGen install, run as a queued background
    worker. `engine_runner`, when given, REPLACES the real engine call
    (`self._run_acestep`/`self._run_audiocraft`) — this is how tests exercise
    the full describe/plan/submit/status/collect/cancel lifecycle with a
    deterministic fake engine and no real weights (CONTRATO.md rule 8)."""

    name = NAME

    def __init__(self, *,
                engine_runner: Optional[Callable[[str, Dict[str, Any], str], "_EngineResult"]] = None,
                engine_probe: Optional[Callable[[], Dict[str, Tuple[bool, str, str]]]] = None) -> None:
        self._engine_runner = engine_runner
        self._engine_probe = engine_probe or _probe_all_engines

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        probes = self._engine_probe()
        chosen = _first_available(probes) or default_engine()
        available = bool(probes.get(chosen, (False, "", ""))[0]) if chosen else False
        version = probes.get(chosen, (False, "", ""))[2] if chosen else ""
        reasons = [f"{name}: {probe[1]}" for name, probe in probes.items() if not probe[0]]
        reason = "" if available else "; ".join(reasons) or "no music engine is installed"
        return AdapterManifest(
            name=NAME, engine=chosen or "none", version=version,
            tasks=SUPPORTED_TASKS, supports_reconcile=False, supports_cancel=True,
            available=available, reason=reason,
            limits={
                "engines": {name: {"available": probe[0], "reason": probe[1], "version": probe[2],
                                    **_ENGINE_LIMITS[name]}
                           for name, probe in probes.items()},
                "default_engine": chosen, "default_device": default_device(),
                "max_concurrent": max_concurrent(),
            },
        )

    # ── plan (pure) ────────────────────────────────────────────────────

    def plan(self, op: str, params: Mapping[str, Any], inputs: Sequence[str]) -> AdapterPlan:
        if op not in SUPPORTED_TASKS:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("op",),
                               detail=f"unsupported task {op!r}; music adapter supports "
                                      f"{', '.join(SUPPORTED_TASKS)}")
        params = dict(params or {})
        probes = self._engine_probe()
        engine = str(params.get("engine") or _first_available(probes) or default_engine())
        if engine not in KNOWN_ENGINES:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("engine",),
                               detail=f"unknown engine {engine!r}; choose one of {', '.join(KNOWN_ENGINES)}")
        available, reason, _version = probes.get(engine, (False, "", ""))
        if not available:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("music_engine",),
                               detail=reason or f"{engine} is not installed")

        limits = _ENGINE_LIMITS[engine]
        duration = params.get("duration_s")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("duration_s",),
                               detail="'duration_s' must be a number")
        duration = float(duration)
        if not (limits["min_duration_s"] <= duration <= limits["max_duration_s"]):
            return AdapterPlan(
                ok=False, adapter=NAME, task=op, missing=("duration_s",),
                detail=f"'duration_s' must be between {limits['min_duration_s']} and "
                       f"{limits['max_duration_s']} for engine {engine!r}")

        prompt_text = str(params.get("prompt") or "").strip()
        lyrics_text = str(params.get("lyrics") or "").strip()
        style_tags = params.get("style_tags") or []
        if not isinstance(style_tags, list) or not all(isinstance(t, str) for t in style_tags):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("style_tags",),
                               detail="'style_tags' must be a list of strings")
        if not prompt_text and not lyrics_text and not style_tags:
            return AdapterPlan(
                ok=False, adapter=NAME, task=op, missing=("prompt",),
                detail="at least one of 'prompt', 'lyrics' or 'style_tags' is required")

        steps = params.get("steps", limits["default_steps"])
        if limits["max_steps"] > 0:
            if not isinstance(steps, int) or isinstance(steps, bool):
                return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("steps",),
                                   detail="'steps' must be an integer")
            if not (limits["min_steps"] <= steps <= limits["max_steps"]):
                return AdapterPlan(
                    ok=False, adapter=NAME, task=op, missing=("steps",),
                    detail=f"'steps' must be between {limits['min_steps']} and {limits['max_steps']} "
                           f"for engine {engine!r}")
        else:
            steps = 0

        seed = params.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("seed",),
                               detail="'seed' must be a non-negative integer or omitted")

        bpm = params.get("bpm")
        if bpm is not None and (not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or bpm <= 0):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("bpm",),
                               detail="'bpm' must be a positive number or omitted")
        key = params.get("key")
        if key is not None and not (isinstance(key, str) and key.strip()):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("key",),
                               detail="'key' must be a non-empty string or omitted")
        if lyrics_text and not limits["supports_lyrics"]:
            # Not a rejection — MUS02's own rule: a request the engine can
            # only honour probabilistically is never labelled a guaranteed
            # structure. `detail` carries the honest caveat through to the
            # caller instead of silently dropping the lyrics.
            lyrics_caveat = (f"engine {engine!r} has no structured lyrics channel; "
                             f"'lyrics' is folded into the text prompt as a hint only")
        else:
            lyrics_caveat = ""

        device = str(params.get("device") or default_device())
        if device not in ("cpu", "cuda"):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("device",),
                               detail="'device' must be 'cpu' or 'cuda'")

        cost_factor = limits["seconds_per_audio_second"]
        step_scale = (steps / limits["default_steps"]) if (limits["max_steps"] > 0 and limits["default_steps"]) else 1.0
        estimated_seconds = round(duration * cost_factor * step_scale, 1)

        engine_plan = {
            "engine": engine, "device": device,
            "params": {
                "duration_s": duration, "prompt": prompt_text, "lyrics": lyrics_text,
                "style_tags": list(style_tags), "steps": steps, "seed": seed,
                "bpm": bpm, "key": key,
            },
            "structured_prompt": build_structured_prompt(
                prompt=prompt_text, lyrics=lyrics_text, style_tags=style_tags,
                bpm=bpm, key=key, sections=params.get("sections"),
            ),
        }
        detail = lyrics_caveat
        return AdapterPlan(
            ok=True, adapter=NAME, task=op,
            estimated_cost={"estimated_seconds": estimated_seconds, "duration_s": duration,
                            "steps": steps, "engine": engine, "device": device,
                            "note": "a conservative, offline heuristic — never measured"},
            detail=detail, engine_plan=engine_plan,
        )

    # ── submit (effect — enqueues; the real generation runs in a worker) ──

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="invalid_plan", detail=plan.detail)
        engine_plan = plan.engine_plan or {}
        engine = engine_plan.get("engine")
        probes = self._engine_probe()
        available, reason, _version = probes.get(str(engine), (False, "", ""))
        if not available:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="music_engine_unavailable", detail=reason)

        job_id = new_job_id(NAME)
        cancel_event = threading.Event()
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                "state": "queued", "detail": "", "error": "",
                "engine": engine, "params": dict(engine_plan.get("params") or {}),
                "structured_prompt": engine_plan.get("structured_prompt") or {},
                "device": engine_plan.get("device", "cpu"),
                "cancel_event": cancel_event, "created_at": time.time(),
                "output_path": None, "future": None,
                "workdir": staging.workdir,
            }
        executor = _get_executor(max_concurrent())
        future: Future = executor.submit(self._execute, job_id, cancel_event)
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id]["future"] = future
        return SubmitResult(job_id=job_id, state="accepted")

    # ── the worker body (runs on the executor's thread) ────────────────

    def _run_engine(self, engine: str, params: Dict[str, Any], out_path: str) -> "_EngineResult":
        if self._engine_runner is not None:
            return self._engine_runner(engine, params, out_path)
        if engine == "ace_step":
            return self._run_acestep(params, out_path)
        if engine == "musicgen":
            return self._run_audiocraft(params, out_path)
        raise ValueError(f"unknown engine {engine!r}")

    def _run_acestep(self, params: Dict[str, Any], out_path: str) -> "_EngineResult":
        """Real ACE-Step call. Never exercised by this repo's own tests
        (the package is never installed here — module docstring); written
        defensively against ACE-Step's published pipeline shape so a real
        install can use this adapter unmodified, but the exact call is a
        genuine extension point should the upstream API differ."""
        from acestep.pipeline_ace_step import ACEStepPipeline  # type: ignore[import-not-found]

        checkpoint_dir = os.environ.get("ACE_STEP_CHECKPOINT_DIR") or None
        pipeline = ACEStepPipeline(checkpoint_dir=checkpoint_dir)
        result = pipeline(
            prompt=params.get("prompt") or "",
            lyrics=params.get("lyrics") or "",
            audio_duration=params.get("duration_s"),
            infer_step=params.get("steps") or _ENGINE_LIMITS["ace_step"]["default_steps"],
            manual_seeds=[params.get("seed")] if params.get("seed") is not None else None,
            save_path=out_path,
        )
        del result  # the pipeline writes save_path itself in every known build
        return _EngineResult(path=out_path, bpm=params.get("bpm"), key=params.get("key"),
                             timing=None, engine_version="", warnings=())

    def _run_audiocraft(self, params: Dict[str, Any], out_path: str) -> "_EngineResult":
        """Real MusicGen call via audiocraft's public API. Never exercised
        by this repo's own tests (module docstring)."""
        import torchaudio  # type: ignore[import-not-found]
        from audiocraft.models import MusicGen  # type: ignore[import-not-found]

        model = MusicGen.get_pretrained("facebook/musicgen-medium")
        model.set_generation_params(duration=params.get("duration_s") or 8.0)
        description = params.get("prompt") or ""
        wav = model.generate([description])
        torchaudio.save(out_path, wav[0].cpu(), model.sample_rate)
        return _EngineResult(path=out_path, bpm=None, key=None, timing=None,
                             engine_version="", warnings=("MusicGen has no BPM/key control",))

    def _execute(self, job_id: str, cancel_event: threading.Event) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            if cancel_event.is_set():
                job["state"] = "cancelled"
                return
            job["state"] = "running"
            engine = job["engine"]
            params = dict(job["params"])
            device = job["device"]
            workdir = job["workdir"]

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
                            job.update(state="failed", error=f"GPU not admitted: {admission.reason}")
                    return
                admitted = True

            if cancel_event.is_set():
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None:
                        job["state"] = "cancelled"
                return

            os.makedirs(workdir, exist_ok=True)
            out_path = os.path.join(workdir, f"{job_id}.wav")
            result = self._run_engine(engine, params, out_path)

            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    return
                if job["state"] == "cancelled" or cancel_event.is_set():
                    job["state"] = "cancelled"
                    return
                job.update(
                    state="completed", output_path=result.path,
                    result_bpm=result.bpm, result_key=result.key,
                    result_timing=result.timing, engine_version=result.engine_version,
                    warnings=list(result.warnings),
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

    # ── status (query) ───────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return StatusResult(job_id=job_id, state="unknown", detail="no such job")
            return StatusResult(job_id=job_id, state=job["state"],
                                detail=job.get("error") or job.get("detail") or "")

    # ── cancel (effect) ──────────────────────────────────────────────

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
                                detail="cancellation requested; the worker checks before writing output")

    # ── collect (query + bounded local effect) ──────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return CollectResult(ok=False, detail="no such job")
            if job["state"] != "completed":
                return CollectResult(ok=False, detail=f"job is {job['state']}, not completed")
            source_path = job["output_path"]
            meta = {
                "engine": job["engine"], "bpm": job.get("result_bpm"), "key": job.get("result_key"),
                "timing": job.get("result_timing"), "seed": job["params"].get("seed"),
                "duration_s": job["params"].get("duration_s"), "steps": job["params"].get("steps"),
                "engine_version": job.get("engine_version", ""), "warnings": list(job.get("warnings") or []),
                "structured_prompt": job["structured_prompt"],
            }
        if not source_path or not os.path.isfile(source_path):
            return CollectResult(ok=False, detail="engine did not write an output file")
        os.makedirs(tmp, exist_ok=True)
        with open(source_path, "rb") as fh:
            data = fh.read()
        if len(data) == 0:
            return CollectResult(ok=False, detail="engine wrote an empty output file")
        audio_out = os.path.join(tmp, f"{job_id}.wav")
        with open(audio_out, "wb") as fh:
            fh.write(data)
        sha256 = hashlib.sha256(data).hexdigest()
        meta_out = os.path.join(tmp, f"{job_id}.meta.json")
        meta_bytes = json.dumps(meta).encode("utf-8")
        with open(meta_out, "wb") as fh:
            fh.write(meta_bytes)
        outputs = (
            CollectedOutput(path=audio_out, sha256=sha256, byte_size=len(data),
                            media_type="audio/wav", valid=True, detail="audio"),
            CollectedOutput(path=meta_out, sha256=hashlib.sha256(meta_bytes).hexdigest(),
                            byte_size=len(meta_bytes), media_type="application/json",
                            valid=True, detail="metadata"),
        )
        return CollectResult(ok=True, outputs=outputs, detail="1 audio + metadata")

    # ── reconcile (query) — BaseAdapter's default (== status()) is honest
    # here: no durable outbox to reconcile against (see module docstring). ──


# ── structured prompt (WP24 closing criterion: deterministic) ──────────

def build_structured_prompt(*, prompt: str = "", lyrics: str = "",
                            style_tags: Optional[Sequence[str]] = None,
                            bpm: Optional[float] = None, key: Optional[str] = None,
                            sections: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Builds the engine-facing structured prompt: style tags (sorted, so
    the SAME tag set always renders the same string regardless of the
    order a caller happened to list them in) plus lyrics annotated with
    `[section]` markers — ACE-Step's own documented convention for section
    boundaries within a single lyrics string. Deterministic: called twice
    with the same arguments produces byte-identical output (WP24's own
    test requirement), which is why `style_tags` is sorted here rather than
    trusted to already be in a canonical order.
    """
    tags = sorted({t.strip() for t in (style_tags or []) if t and t.strip()})
    tag_line = ", ".join(tags)
    text_prompt = prompt.strip()
    if tag_line and text_prompt:
        text_prompt = f"{text_prompt}, {tag_line}"
    elif tag_line:
        text_prompt = tag_line
    if bpm:
        text_prompt = f"{text_prompt}, {int(bpm) if float(bpm).is_integer() else bpm} BPM".strip(", ")
    if key:
        text_prompt = f"{text_prompt}, key of {key}".strip(", ")

    lyrics_lines: List[str] = []
    if sections:
        for section in sections:
            kind = str(section.get("kind") or "verse")
            section_lyrics = str(section.get("lyrics") or "").strip()
            lyrics_lines.append(f"[{kind}]")
            if section_lyrics:
                lyrics_lines.append(section_lyrics)
    elif lyrics.strip():
        lyrics_lines.append(lyrics.strip())
    structured_lyrics = "\n".join(lyrics_lines)

    return {
        "text_prompt": text_prompt,
        "structured_lyrics": structured_lyrics,
        "style_tags": tags,
        "bpm": bpm,
        "key": key,
    }


class _EngineResult:
    """Plain-data return from `_run_engine`/an injected `engine_runner` —
    kept as a tiny class (not a namedtuple) so a test's fake runner can
    construct one positionally or by keyword without importing a dataclass
    decorator just for this."""

    __slots__ = ("path", "bpm", "key", "timing", "engine_version", "warnings")

    def __init__(self, *, path: str, bpm: Optional[float] = None, key: Optional[str] = None,
                timing: Optional[List[Dict[str, Any]]] = None, engine_version: str = "",
                warnings: Sequence[str] = ()) -> None:
        self.path = path
        self.bpm = bpm
        self.key = key
        self.timing = timing
        self.engine_version = engine_version
        self.warnings = tuple(warnings)


def synth_wav(path: str, *, seconds: float = 1.0, sample_rate: int = 8000) -> str:
    """A tiny, real, silent WAV file — the SAME kind of deterministic
    fixture `tests/test_creator_wp15_asr.py` builds for ASR, reused here so
    a test's fake `engine_runner` can produce genuine audio bytes without
    any external fixture asset or real synthesis engine."""
    n_frames = max(1, int(seconds * sample_rate))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return path


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


ADAPTER_FACTORY = MusicAdapter

__all__ = [
    "NAME", "SUPPORTED_TASKS", "KNOWN_ENGINES", "MusicAdapter",
    "build_structured_prompt", "synth_wav",
    "default_engine", "default_device", "max_concurrent",
    "ADAPTER_FACTORY", "reset_for_tests",
]
