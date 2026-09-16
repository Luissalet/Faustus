"""adapters/tts.py — WP18: text-to-speech over the WP10 adapter port.

`services/tts/tts_service.py` and `services/tts/system_voice.py` already own
local synthesis (Kokoro-82M on GPU, and a Windows system-voice fallback) and
`services/speech_runtime.py::capabilities()` already answers the cheap
"what is configured" question for the chat/Jarvis surface. This adapter
does NOT reimplement any of that — it reaches the exact same Kokoro pipeline
and system-voice helper those modules already build, just with a per-call
`voice`/`language` instead of `TTSService`'s single settings-wide choice,
because casting (WP18's whole point) needs a different voice per speaker in
the same document, not one global voice for the process.

**Engines this build actually has.** `piper`, `edge-tts` and `chatterbox`
(mentioned in `docs/06_ADAPTADORES_MULTIMEDIA.md`, "TTS y Voice Studio") are
NOT installed in this environment — grepped, absent from every
`requirements*.txt` and unimportable. CONTRATO.md rule 6 ("dependencias
pesadas: requirements-optional.txt + import perezoso + degradación
explícita") applies here exactly as it does to the whisper adapter's
`faster_whisper`: `describe()` reports each one's real, probed
availability (reusing `src.media_capabilities.probe_tts_kokoro` /
`probe_tts_piper` rather than writing a second opinion about the same
probe), and `submit()` for an unavailable engine answers
`rejected_before_queue` — this adapter never claims to have run a motor it
cannot import.

**No motor is over-claimed as multilingual.** MOD14's acceptance is literal:
"elegir español filtra o explica por qué una variante inglesa no sirve, en
lugar de fingir soporte" — AUD10 names the exact failure mode to avoid,
"Turbo inglés" presented as multilingual. Every entry in `VOICE_CATALOG`
below declares its own `languages` tuple; `chatterbox:turbo-en` (an
English-only expressive variant, matching upstream Chatterbox's own
"Turbo" naming) declares `languages=("en",)` and `multilingual=False` by
construction — `plan()` rejects a `language` that is not in the chosen
voice's own list rather than silently trying anyway.

**Cloning is gated on live consent, checked twice.** `plan()` checks
`src.creator.consent.is_valid()` for an EARLY, honest "missing" signal (the
same `MISSING_KINDS` vocabulary `src.creator.preflight` uses); `submit()`
re-checks it FRESH right before doing anything, because a plan object is
not proof consent is still live a moment later — see
`src/creator/consent.py`'s module docstring for why a preflight-time check
alone cannot honour AUD10's "revocar bloquea trabajos aún no autorizados a
ejecutar."

**Paralinguistic tags are escaped, never spoken as literal text.** AUD02's
acceptance: "una etiqueta no soportada se escapa o bloquea y no se lee
accidentalmente como diálogo final." `_sanitize_tags()` strips any
`[tag]`/`[tag:value]` marker the chosen engine's `ENGINE_TAG_SUPPORT` entry
does not list, and reports every dropped tag in the plan's `detail` — none
of this build's runnable engines (kokoro, system) understand any tag today,
so every tag is dropped and reported, never left in the text kokoro/SAPI
would otherwise read aloud as literal bracket characters.

**Synchronous, queue-of-one** — exactly `adapters/ffmpeg.py`'s own
discipline (see that module's docstring): a short TTS utterance is fast
enough that `submit()` runs it to completion under a single process-wide
lock (never two GPU syntheses racing the same Kokoro pipeline) and returns
an already-`completed`/`failed` job; `describe()` reports
`supports_reconcile=False` for the same honest reason ffmpeg's adapter
does — a process kill mid-synthesis has nothing durable to reconcile
against.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src.creator.adapter_port import (
    AdapterManifest, AdapterPlan, CancelResult, CollectResult, CollectedOutput,
    StatusResult, Staging, SubmitResult, new_job_id,
)
from src.creator.adapters.base import BaseAdapter

NAME = "tts"
SUPPORTED_TASKS = ("tts", "voice_clone", "dub")

#: Ops that clone a voice/likeness and therefore require a live consent
#: record — the same two names `src.creator.preflight.CONSENT_OPERATIONS`
#: already uses for this adapter's engine, kept in lockstep on purpose.
CLONING_OPS: FrozenSet[str] = frozenset({"voice_clone", "dub"})

#: Rough, deliberately conservative characters-per-second used ONLY to shape
#: `plan()`'s `estimated_cost` — same discipline as the whisper adapter's
#: `_REALTIME_FACTOR`: a wrong-but-conservative UNDER-estimate of speed
#: (i.e. an OVER-estimate of seconds) is the safe failure mode for a cost
#: preview.
_CHARS_PER_SECOND = 14.0

_JOBS_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}

#: Serializes every real synthesis call — see module docstring ("queue-of-
#: one"). Not a correctness requirement for the two engines actually wired
#: here (each call is independent), but Kokoro's pipeline is a single GPU
#: resource this process should never hit from two threads at once.
_SYNTH_LOCK = threading.Lock()


class EngineUnavailable(Exception):
    """A `submit()`-time synthesis attempt hit an engine that `describe()`
    already reported as unavailable, or that failed to produce audio."""


# ── the voice catalog (this adapter's own documented menu — see module
# docstring; never asks an engine to enumerate itself) ─────────────────────

@dataclass(frozen=True)
class VoiceSpec:
    id: str
    engine: str
    label: str
    languages: Tuple[str, ...]
    gender: str
    style: str
    cloning_capable: bool = False
    multilingual: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "engine": self.engine, "label": self.label,
            "languages": list(self.languages), "gender": self.gender,
            "style": self.style, "cloning_capable": self.cloning_capable,
            "multilingual": self.multilingual,
        }


VOICE_CATALOG: Tuple[VoiceSpec, ...] = (
    # Kokoro-82M — this repo's `services/tts/tts_service.py::_KokoroPipeline`
    # hardcodes `KPipeline(lang_code="a")` (American English) today, so
    # EVERY kokoro voice here is `languages=("en",)` — never multilingual,
    # regardless of what the upstream Kokoro model card documents for other
    # `lang_code`s this repo does not wire up.
    VoiceSpec("kokoro:af_heart", "kokoro", "Heart (US English, declared female)",
              ("en",), "female (declared)", "neutral"),
    VoiceSpec("kokoro:am_adam", "kokoro", "Adam (US English, declared male)",
              ("en",), "male (declared)", "neutral"),
    # Windows SAPI, via `services/tts/system_voice.py` — the actual voice
    # roster depends entirely on what the OS has installed, so this is a
    # single documented placeholder id ("whatever matches the requested
    # language, or the OS default") rather than a fabricated fixed list.
    VoiceSpec("system:default", "system", "OS default voice (installed system voices)",
              ("und",), "unspecified", "neutral"),
    # Piper — CLI/onnx voices, not installed in this environment (see module
    # docstring). Listed as a documented, currently-unusable menu entry so a
    # caller sees the gap named rather than the engine omitted outright.
    VoiceSpec("piper:en_US-amy-medium", "piper", "Amy (US English, medium)",
              ("en",), "female (declared)", "neutral"),
    VoiceSpec("piper:es_ES-mls_10246-low", "piper", "MLS 10246 (Spanish, low)",
              ("es",), "unspecified", "neutral"),
    # edge-tts — cloud neural voices over network; not installed here either,
    # and any real call would need `src.privacy_policy.assert_outbound` the
    # same way `services/tts/tts_service.py::_synthesize_api` already gates
    # its own endpoint calls (this adapter never bypasses that gate).
    VoiceSpec("edge_tts:en-US-JennyNeural", "edge_tts", "Jenny (US English neural, cloud)",
              ("en",), "female (declared)", "neutral"),
    VoiceSpec("edge_tts:es-ES-ElviraNeural", "edge_tts", "Elvira (Spanish neural, cloud)",
              ("es",), "female (declared)", "neutral"),
    # Chatterbox — voice-cloning capable, not installed. `turbo-en` is
    # deliberately English-only (see module docstring on "Turbo inglés" /
    # AUD10); `multilingual` is the separate, real multilingual variant.
    VoiceSpec("chatterbox:turbo-en", "chatterbox", "Chatterbox Turbo (English only)",
              ("en",), "unspecified", "expressive", cloning_capable=True, multilingual=False),
    VoiceSpec("chatterbox:multilingual", "chatterbox", "Chatterbox Multilingual",
              ("en", "es", "fr", "de", "zh"), "unspecified", "expressive",
              cloning_capable=True, multilingual=True),
)

VOICE_BY_ID: Dict[str, VoiceSpec] = {v.id: v for v in VOICE_CATALOG}

#: Which paralinguistic tags each engine's OWN synthesis path actually
#: understands, per AUD02. Empty for every engine this build can run — see
#: module docstring.
ENGINE_TAG_SUPPORT: Dict[str, FrozenSet[str]] = {
    "kokoro": frozenset(),
    "system": frozenset(),
    "piper": frozenset(),
    "edge_tts": frozenset({"pause"}),  # SSML <break>, documented upstream — not runnable here
    "chatterbox": frozenset({"pause", "laughs", "emphasis"}),  # documented upstream — not runnable here
}

_TAG_RE = re.compile(r"\[(?P<name>[a-zA-Z_]+)(?::(?P<value>[^\]]*))?\]")


def _sanitize_tags(text: str, engine: str) -> Tuple[str, List[str]]:
    """Strip every `[tag]`/`[tag:value]` the engine does NOT support; return
    the safe text plus the list of raw tags that were dropped. A tag the
    engine DOES support is left verbatim for that engine's own parser — this
    function never interprets a tag itself."""
    supported = ENGINE_TAG_SUPPORT.get(engine, frozenset())
    dropped: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        name = match.group("name").lower()
        if name in supported:
            return match.group(0)
        dropped.append(match.group(0))
        return ""

    safe = _TAG_RE.sub(_sub, text)
    safe = re.sub(r"[ \t]{2,}", " ", safe).strip()
    return safe, dropped


# ── engine probes (import/PATH only — never a model load) ─────────────────

def _importable(module: str) -> bool:
    from importlib.util import find_spec
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def probe_engine(engine: str) -> Dict[str, Any]:
    """`{"installed": bool, "detail": str}` for one engine name, reusing
    `src.media_capabilities`'s existing kokoro/piper probes (CONTRATO.md
    rule 1: never a second opinion about the same import check) and adding
    the two this adapter is the first to name."""
    if engine == "kokoro":
        from src import media_capabilities
        probed = media_capabilities.probe_tts_kokoro()
        return {"installed": bool(probed.get("installed")), "detail": str(probed.get("detail") or "")}
    if engine == "piper":
        from src import media_capabilities
        probed = media_capabilities.probe_tts_piper()
        return {"installed": bool(probed.get("installed")), "detail": str(probed.get("detail") or "")}
    if engine == "system":
        installed = os.name == "nt"
        return {"installed": installed,
                "detail": "Windows System.Speech" if installed else "system voices require Windows"}
    if engine == "edge_tts":
        installed = _importable("edge_tts")
        return {"installed": installed,
                "detail": "edge_tts package importable" if installed
                          else "edge-tts not installed; install it (requirements-voice.txt) to "
                               "enable cloud neural voices — faustus never installs it automatically"}
    if engine == "chatterbox":
        installed = _importable("chatterbox")
        return {"installed": installed,
                "detail": "chatterbox package importable" if installed
                          else "chatterbox is not installed; faustus never installs it "
                               "automatically, and voice cloning stays unavailable until it is"}
    return {"installed": False, "detail": f"unknown engine {engine!r}"}


def known_engines() -> Tuple[str, ...]:
    seen: List[str] = []
    for voice in VOICE_CATALOG:
        if voice.engine not in seen:
            seen.append(voice.engine)
    return tuple(seen)


# ── real synthesis (never touched by describe()/plan(); only submit()) ────

def _synthesize_kokoro(text: str, raw_voice: str, language: str, speed: float) -> Tuple[bytes, str]:
    from services.tts.tts_service import get_tts_service
    pipeline = get_tts_service()._get_kokoro()
    if not pipeline.available:
        raise EngineUnavailable("kokoro pipeline is not available (package or CUDA missing)")
    audio = pipeline.synthesize_raw(text, raw_voice or "af_heart")
    if not audio:
        raise EngineUnavailable("kokoro synthesis produced no audio")
    return audio, "audio/wav"


def _synthesize_system(text: str, raw_voice: str, language: str, speed: float) -> Tuple[bytes, str]:
    if os.name != "nt":
        raise EngineUnavailable("system voices require Windows")
    from services.tts.system_voice import synthesize_system
    audio = synthesize_system(text, "" if raw_voice == "default" else raw_voice, language, speed)
    return audio, "audio/wav"


def _synthesize_unavailable(engine: str) -> Callable[[str, str, str, float], Tuple[bytes, str]]:
    def _raise(text: str, raw_voice: str, language: str, speed: float) -> Tuple[bytes, str]:
        reason = probe_engine(engine)["detail"]
        raise EngineUnavailable(reason)
    return _raise


_REAL_SYNTH: Dict[str, Callable[[str, str, str, float], Tuple[bytes, str]]] = {
    "kokoro": _synthesize_kokoro,
    "system": _synthesize_system,
    "piper": _synthesize_unavailable("piper"),
    "edge_tts": _synthesize_unavailable("edge_tts"),
    "chatterbox": _synthesize_unavailable("chatterbox"),
}


# ── the adapter ──────────────────────────────────────────────────────────

class TtsAdapter(BaseAdapter):
    """One TTS/voice-clone/dub adapter over whatever local voice engines
    this build actually has. `synth_fn`, when given, REPLACES `_REAL_SYNTH`
    entirely — this is how tests exercise the full describe/plan/submit/
    status/collect lifecycle with a deterministic fake engine and no real
    weights (CONTRATO.md rule 8)."""

    name = NAME

    def __init__(self, *, synth: Optional[Dict[str, Callable[[str, str, str, float], Tuple[bytes, str]]]] = None,
                probe: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        self._synth = synth if synth is not None else _REAL_SYNTH
        self._probe = probe or probe_engine

    # ── describe (query) ───────────────────────────────────────────────

    def describe(self) -> AdapterManifest:
        engines: Dict[str, Any] = {}
        any_available = False
        for engine in known_engines():
            probed = self._probe(engine)
            engines[engine] = probed
            any_available = any_available or bool(probed.get("installed"))
        return AdapterManifest(
            name=NAME, engine="tts", version="1",
            tasks=SUPPORTED_TASKS, supports_reconcile=False, supports_cancel=True,
            available=any_available,
            reason="" if any_available else "no TTS engine is installed in this build",
            limits={
                "engines": engines,
                "voices": [v.to_dict() for v in VOICE_CATALOG],
                "cloning_ops": sorted(CLONING_OPS),
                "chars_per_second_estimate": _CHARS_PER_SECOND,
            },
        )

    # ── plan (pure) ────────────────────────────────────────────────────

    def plan(self, op: str, params: Mapping[str, Any], inputs: Sequence[str]) -> AdapterPlan:
        if op not in SUPPORTED_TASKS:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("op",),
                               detail=f"unsupported task {op!r}; tts adapter supports "
                                      f"{', '.join(SUPPORTED_TASKS)}")
        params = dict(params or {})
        voice_id = str(params.get("voice_id") or "")
        voice = VOICE_BY_ID.get(voice_id)
        if voice is None:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("voice_id",),
                               detail=f"unknown voice_id {voice_id!r}; choose one of "
                                      f"{', '.join(sorted(VOICE_BY_ID))}")
        probed = self._probe(voice.engine)
        if not probed.get("installed"):
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("engine",),
                               detail=str(probed.get("detail") or f"{voice.engine} is not available"))

        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("text",),
                               detail="'text' must be a non-empty string")

        language = str(params.get("language") or (voice.languages[0] if voice.languages else "") or "")
        if voice.languages and voice.languages != ("und",) and language not in voice.languages:
            return AdapterPlan(
                ok=False, adapter=NAME, task=op, missing=("language",),
                detail=f"voice {voice_id!r} does not support language {language!r}; it supports "
                       f"{', '.join(voice.languages)} — this is never silently substituted")

        speed = params.get("speed", 1.0)
        if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed <= 0:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("params",),
                               detail="'speed' must be a positive number")

        safe_text, dropped_tags = _sanitize_tags(text, voice.engine)
        if not safe_text:
            return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("text",),
                               detail="'text' has no speakable content once unsupported tags "
                                      "are removed")

        consent_subject = ""
        if op in CLONING_OPS:
            if not voice.cloning_capable:
                return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("voice_id",),
                                   detail=f"voice {voice_id!r} does not support {op}")
            if len(inputs) != 1:
                return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("inputs",),
                                   detail=f"{op} needs exactly one reference-audio input")
            consent_subject = str(params.get("consent_subject") or "")
            if not consent_subject:
                return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("consent_subject",),
                                   detail=f"{op} clones a voice; no consent_subject was given")
            # Early, honest signal (see module docstring) — submit() is the
            # actual gate, re-checked fresh right before anything runs.
            from src.creator import consent as consent_mod
            owner_hint = str(params.get("_owner_hint") or "")
            if owner_hint and not consent_mod.is_valid(owner_hint, consent_subject, scope="clone"):
                return AdapterPlan(ok=False, adapter=NAME, task=op, missing=("consent",),
                                   detail=f"no live consent on file for {consent_subject!r}")

        seconds_estimate = round(len(safe_text) / _CHARS_PER_SECOND / max(speed, 0.1), 2)
        detail = ""
        if dropped_tags:
            detail = f"dropped unsupported tag(s): {', '.join(dropped_tags)}"

        engine_plan = {
            "op": op, "engine": voice.engine, "voice_id": voice_id, "raw_voice": voice_id.split(":", 1)[-1],
            "text": safe_text, "dropped_tags": dropped_tags, "language": language, "speed": float(speed),
            "consent_subject": consent_subject, "input_order": list(inputs),
        }
        return AdapterPlan(
            ok=True, adapter=NAME, task=op,
            estimated_cost={"estimated_seconds": seconds_estimate, "characters": len(safe_text)},
            detail=detail, engine_plan=engine_plan,
        )

    # ── submit (effect — synchronous; see module docstring) ────────────

    def submit(self, plan: AdapterPlan, staging: Staging) -> SubmitResult:
        if not plan.ok:
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="invalid_plan", detail=plan.detail)
        ep = plan.engine_plan or {}
        op = str(ep.get("op") or "")
        engine = str(ep.get("engine") or "")
        probed = self._probe(engine)
        if not probed.get("installed"):
            return SubmitResult(job_id="", state="rejected_before_queue",
                                reason="engine_unavailable",
                                detail=str(probed.get("detail") or f"{engine} is not available"))

        if op in CLONING_OPS:
            consent_subject = str(ep.get("consent_subject") or "")
            from src.creator import consent as consent_mod
            if not consent_subject or not consent_mod.is_valid(staging.owner, consent_subject, scope="clone"):
                return SubmitResult(job_id="", state="rejected_before_queue",
                                    reason="consent_missing_or_revoked_or_expired",
                                    detail=f"no live consent on file for {consent_subject!r}; "
                                           "register one with src.creator.consent.register() "
                                           "before this can render")
            order = ep.get("input_order") or []
            if len(order) != 1 or order[0] not in staging.input_paths:
                return SubmitResult(job_id="", state="rejected_before_queue",
                                    reason="input_not_staged",
                                    detail=f"{op} needs exactly one staged reference-audio input")

        job_id = new_job_id(NAME)
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "running", "output_bytes": b"", "media_type": "",
                             "detail": "", "created_at": time.time()}

        synth_fn = self._synth.get(engine)
        if synth_fn is None:
            with _JOBS_LOCK:
                _JOBS[job_id].update(state="failed", detail=f"no synthesis path for engine {engine!r}")
            return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                reason="engine_unavailable", detail=f"no synthesis path for {engine!r}")

        with _SYNTH_LOCK:
            try:
                audio_bytes, media_type = synth_fn(
                    ep.get("text", ""), ep.get("raw_voice", ""), ep.get("language", ""), float(ep.get("speed", 1.0)))
            except EngineUnavailable as exc:
                with _JOBS_LOCK:
                    _JOBS[job_id].update(state="failed", detail=str(exc))
                return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                    reason="synthesis_failed", detail=str(exc))
            except Exception as exc:  # noqa: BLE001 - a synth crash fails the job, not the process
                with _JOBS_LOCK:
                    _JOBS[job_id].update(state="failed", detail=str(exc))
                return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                    reason="synthesis_failed", detail=str(exc))

        if not audio_bytes:
            with _JOBS_LOCK:
                _JOBS[job_id].update(state="failed", detail="synthesis produced no audio")
            return SubmitResult(job_id=job_id, state="rejected_before_queue",
                                reason="synthesis_failed", detail="synthesis produced no audio")

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None and job.get("state") != "cancelled":
                job.update(state="completed", output_bytes=audio_bytes, media_type=media_type or "audio/wav")
        return SubmitResult(job_id=job_id, state="accepted")

    # ── status (query) ────────────────────────────────────────────────

    def status(self, job_id: str) -> StatusResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return StatusResult(job_id=job_id, state="unknown", detail="no such job")
            return StatusResult(job_id=job_id, state=job["state"], detail=str(job.get("detail") or ""))

    # ── cancel (effect) ───────────────────────────────────────────────

    def cancel(self, job_id: str) -> CancelResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return CancelResult(job_id=job_id, outcome="unknown", detail="no such job")
            if job["state"] in ("completed", "failed"):
                # Synchronous by construction — a cancel can only ever
                # arrive after submit() already settled the outcome.
                return CancelResult(job_id=job_id, outcome="too_late", detail=job["state"])
            if job["state"] == "cancelled":
                return CancelResult(job_id=job_id, outcome="confirmed", detail="already cancelled")
            job["state"] = "cancelled"
            return CancelResult(job_id=job_id, outcome="accepted", detail="marked cancelled")

    # ── collect (query + bounded local effect) ────────────────────────

    def collect(self, job_id: str, tmp: str) -> CollectResult:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            return CollectResult(ok=False, detail="no such job")
        if job["state"] != "completed":
            return CollectResult(ok=False, detail=f"job is {job['state']}, not completed")
        data = job["output_bytes"]
        media_type = job["media_type"] or "audio/wav"
        os.makedirs(tmp, exist_ok=True)
        ext = "wav" if "wav" in media_type else "bin"
        out_path = os.path.join(tmp, f"{job_id}.{ext}")
        with open(out_path, "wb") as fh:
            fh.write(data)
        sha256 = hashlib.sha256(data).hexdigest()
        output = CollectedOutput(path=out_path, sha256=sha256, byte_size=len(data),
                                 media_type=media_type, valid=True, detail=f"{len(data)} byte(s)")
        return CollectResult(ok=True, outputs=(output,), detail=output.detail)

    # ── reconcile (query) — BaseAdapter's default (== status()) is honest
    # here: this adapter is synchronous with no durable outbox (see module
    # docstring), matching `supports_reconcile=False`. ──


def reset_for_tests() -> None:
    """Tests only: forget every in-process job this module is tracking."""
    with _JOBS_LOCK:
        _JOBS.clear()


ADAPTER_FACTORY = TtsAdapter

__all__ = [
    "NAME", "SUPPORTED_TASKS", "CLONING_OPS", "VoiceSpec", "VOICE_CATALOG",
    "VOICE_BY_ID", "ENGINE_TAG_SUPPORT", "EngineUnavailable", "TtsAdapter",
    "probe_engine", "known_engines", "ADAPTER_FACTORY", "reset_for_tests",
]
