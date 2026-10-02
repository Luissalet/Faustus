"""Local speech to text with faster-whisper: one :class:`Transcriber` instead of six copies.

::

    from hoard_link.media.stt import Transcriber

    stt = Transcriber(size="small")                    # models under $HOARD_HOME/models/whisper; nothing is loaded yet
    result = stt.transcribe("meeting.wav", language=None, progress=print, cancel=event)
    result.text, result.language, result.segments      # segments: [{start_s, end_s, text, words: [{start_s, end_s, word, p}]}]

What it does that the copies did only in part:

* **GPU lease.** Before a CUDA load it asks the hub for the model's VRAM (:mod:`hoard_link.lease`); the lease covers the load and the
  first inference. On a lease timeout / refusal, a load failure, or a CUDA runtime error in the middle of an inference (the
  ``cublas64_12.dll is not found`` of a Windows machine without the pip wheels) it reloads on the **CPU** and carries on.
* **Windows CUDA libraries.** :func:`cuda_dll_dirs` registers the ``nvidia/*/bin`` folders of the pip wheels before the first load.
* **State machine.** ``idle -> downloading | loading -> ready | error``, shown by :meth:`Transcriber.info`.
* **Hallucination filter.** :func:`clean_segments` drops what Whisper invents on silence ("Thanks for watching", "Subtítulos por la
  comunidad de Amara.org"), segments the decoder itself doubts (no-speech / log-probability / compression thresholds), collapses
  word loops ("the the the the") and consecutive duplicates.
* **Cancellation and progress** per segment; the model and ``numpy`` are imported lazily, so importing this module needs only the
  standard library (a missing ``faster_whisper`` raises :func:`hoard_link.errors.missing_dependency`).

Environment: ``HOARD_GPU_LEASE=0`` (no leasing), ``HOARD_WHISPER_VRAM_MB``, ``HOARD_LEASE_TIMEOUT_S`` (the old ``SCRIBE_*`` names still work).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import sys
import sysconfig
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from ..errors import Unavailable, missing_dependency
from ..launch import hoard_home
from ..proc import Cancelled

__all__ = [
    "Transcriber", "Transcript", "clean_segments", "CleanStats", "is_hallucination", "collapse_ngram_loops", "normalize",
    "cuda_dll_dirs", "cuda_available", "model_present", "model_repo", "whisper_vram_mb", "default_models_dir", "available",
    "NO_SPEECH_MAX", "LOGPROB_MIN", "COMPRESSION_MAX",
]

log = logging.getLogger("hoard_link.media.stt")

NO_SPEECH_MAX = 0.6
LOGPROB_MIN = -1.0
COMPRESSION_MAX = 2.4
DUPLICATE_SIMILARITY = 0.88
MIN_LOOP_REPEATS = 4
MAX_NGRAM = 4

VRAM_MB_BY_SIZE = {
    "tiny": 1024, "base": 1536, "small": 2048, "medium": 5120, "large": 6144, "large-v1": 6144, "large-v2": 6144, "large-v3": 6144,
    "turbo": 6144, "large-v3-turbo": 6144,
}
DEFAULT_VRAM_MB = 2048
DEFAULT_LEASE_TIMEOUT_S = 120.0
LEASE_STATES = ("waiting", "granted", "fallback_cpu", "disabled")


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def default_models_dir() -> Path:
    """``$HOARD_HOME/models/whisper``."""
    return hoard_home() / "models" / "whisper"


def gpu_leasing_enabled() -> bool:
    return _env("HOARD_GPU_LEASE", "SCRIBE_GPU_LEASE") != "0"


def whisper_vram_mb(size: str) -> int:
    """VRAM to lease for a model size (``HOARD_WHISPER_VRAM_MB`` overrides)."""
    raw = _env("HOARD_WHISPER_VRAM_MB", "SCRIBE_WHISPER_VRAM_MB")
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("HOARD_WHISPER_VRAM_MB=%r is not an integer; ignoring it", raw)
    return VRAM_MB_BY_SIZE.get(size.replace("distil-", ""), DEFAULT_VRAM_MB)


def lease_timeout_s() -> float:
    raw = _env("HOARD_LEASE_TIMEOUT_S", "SCRIBE_LEASE_TIMEOUT_S")
    if not raw:
        return DEFAULT_LEASE_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        log.warning("HOARD_LEASE_TIMEOUT_S=%r is not a number; using %s", raw, DEFAULT_LEASE_TIMEOUT_S)
        return DEFAULT_LEASE_TIMEOUT_S


# ---------------------------------------------------------------- CUDA / model files --

_DLL_REGISTERED: list[str] = []
_DLL_DONE = False


def cuda_dll_dirs(*, roots: Optional[Iterable["str | Path"]] = None, platform: Optional[str] = None) -> list[str]:
    """Windows: the ``nvidia/<lib>/bin`` folders of the pip-installed CUDA wheels (cuBLAS, cuDNN), put on the DLL search path once.

    CTranslate2 loads those libraries at the first inference and only finds them through that path; without them the model loads and then
    fails with ``cublas64_12.dll is not found``. Returns the folders (``[]`` off Windows). ``roots`` / ``platform`` are for tests.
    """
    global _DLL_DONE
    if not (platform or sys.platform).startswith("win"):
        return []
    if roots is None:
        import site

        roots_list: list[Path] = [Path(p) for p in sysconfig.get_paths().values()]
        try:
            roots_list += [Path(p) for p in site.getsitepackages()]
            roots_list.append(Path(site.getusersitepackages()))
        except Exception:  # noqa: BLE001 - a virtualenv without the site helpers
            pass
    else:
        roots_list = [Path(r) for r in roots]
    found: list[str] = []
    for root in roots_list:
        base = root / "nvidia"
        if not base.is_dir():
            continue
        for lib in sorted(base.iterdir()):
            bin_dir = lib / "bin"
            if bin_dir.is_dir() and str(bin_dir) not in found:
                found.append(str(bin_dir))
    if _DLL_DONE and not roots:
        return list(_DLL_REGISTERED)
    for folder in found:
        try:
            os.add_dll_directory(folder)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join([*found, os.environ.get("PATH", "")])
        log.info("CUDA DLL folders: %s", found)
    if roots is None:
        _DLL_DONE = True
        _DLL_REGISTERED[:] = found
    return found


def available() -> bool:
    """True when ``faster_whisper`` can be imported."""
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


def cuda_available() -> bool:
    try:
        cuda_dll_dirs()
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - no CTranslate2, no driver
        return False


def _looks_like_cuda_error(error: BaseException) -> bool:
    text = str(error).lower()
    return any(k in text for k in ("cublas", "cudnn", "cuda", "cudart", "device-side"))


_CUDA_LIBRARY_FAILURE = ""


def _remember_missing_cuda_library(error: BaseException) -> None:
    # A native runtime may hang when it is initialized again after a missing DLL.
    # Only remember missing libraries, never transient GPU contention or OOM.
    global _CUDA_LIBRARY_FAILURE
    text = str(error).lower()
    if _looks_like_cuda_error(error) and any(s in text for s in ("not found", "cannot be loaded", "could not load", "cannot open shared object")):
        _CUDA_LIBRARY_FAILURE = str(error)


def resolve_device(device: str) -> str:
    return ("cuda" if not _CUDA_LIBRARY_FAILURE and cuda_available() else "cpu") if device == "auto" else device


def resolve_compute(compute: str, device: str) -> str:
    return compute if compute != "auto" else ("float16" if device == "cuda" else "int8")


def model_repo(size: str) -> str:
    base = size.replace("distil-", "")
    return f"Systran/{'distil-whisper' if size.startswith('distil-') else 'faster-whisper'}-{base}"


def model_present(models_dir: "str | Path", size: str) -> bool:
    """True when the hub cache under ``models_dir`` already holds ``model.bin`` for this size."""
    folder = Path(models_dir) / ("models--" + model_repo(size).replace("/", "--"))
    return folder.is_dir() and any(folder.glob("snapshots/*/model.bin"))


def _load_whisper(size: str, device: str, compute: str, download_root: str) -> Any:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise missing_dependency("faster_whisper", "speech to text", pip_name="faster-whisper") from None
    return WhisperModel(size, device=device, compute_type=compute, download_root=download_root)


def _default_lease(vram_mb: int, purpose: str, owner: str, timeout_s: float) -> Any:
    from ..lease import lease

    return lease(vram_mb=vram_mb, purpose=purpose, owner=owner, timeout_s=timeout_s)


# ---------------------------------------------------------------- the hallucination filter --

_EXACT = (
    # Spanish
    "subtítulos realizados por la comunidad de amara.org", "subtitulado por la comunidad de amara.org",
    "subtítulos por la comunidad de amara.org", "subtítulos creados por la comunidad de amara.org", "gracias por ver el vídeo",
    "gracias por ver el video", "gracias por ver", "gracias por su atención", "suscríbete", "suscríbete al canal",
    "no olvides suscribirte", "dale like y suscríbete", "hasta la próxima", "nos vemos en el próximo vídeo",
    "este es el canal de subtítulos en español",
    # English
    "thank you for watching", "thanks for watching", "thank you so much for watching", "please subscribe", "subscribe",
    "like and subscribe", "don't forget to subscribe", "subscribe to my channel", "see you in the next video", "see you next time",
    "subtitles by the amara.org community", "amara.org",
)
#: one-word segments Whisper emits on noise; dropped only as a whole segment (and not at all with ``deny_weak=False``)
_WEAK = ("you", "so")
_PREFIXES = (
    "subtítulos realizados por", "subtítulos por", "subtitulado por", "subtítulos creados por", "este es el canal de subtítulos",
    "subtitles by", "captions by", "transcribed by", "transcripción por", "gracias por ver", "thank you for watching",
    "thanks for watching",
)
_MUSIC = re.compile(r"^[\[\(\{]?\s*(?:m[uú]sica|music|silencio|silence|applause|aplausos)\s*[\]\)\}]?$")
_NOTES = re.compile(r"^[♪♫♩♬\s]+$")


def normalize(text: str) -> str:
    """Lowercase, accents and punctuation stripped, whitespace collapsed: the form every comparison here uses."""
    plain = unicodedata.normalize("NFKD", str(text or "").lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", plain, flags=re.UNICODE)).strip()


_DENY = frozenset(normalize(p) for p in _EXACT)
_DENY_WEAK = frozenset(normalize(p) for p in _WEAK)
_DENY_PREFIXES = tuple(normalize(p) for p in _PREFIXES)


def is_hallucination(text: str, *, extra_phrases: Iterable[str] = (), deny_weak: bool = True) -> bool:
    """True when ``text`` as a whole is one of Whisper's known silence artifacts: an empty string, a music marker, a listed
    phrase (``extra_phrases`` adds more), a ``Subtitles by …`` credit, or anything naming ``amara.org``."""
    raw = str(text or "").strip()
    if not raw or _NOTES.match(raw):
        return True
    norm = normalize(raw)
    if not norm or _MUSIC.match(norm):
        return True
    if norm in _DENY or (deny_weak and norm in _DENY_WEAK) or "amara org" in norm:
        return True
    if norm in {normalize(p) for p in extra_phrases}:
        return True
    return any(norm.startswith(p) for p in _DENY_PREFIXES)


def collapse_ngram_loops(text: str) -> "tuple[str, int]":
    """Collapse an immediate loop of a word or a short phrase (up to 4 words, repeated at least 4 times) to one occurrence:
    ``"the the the the the"`` -> ``"the"``. Returns ``(text, loops_collapsed)``. Three repeats ("no no no") are left alone."""
    words = str(text or "").split()
    n_words = len(words)
    if n_words < MIN_LOOP_REPEATS:
        return str(text or ""), 0
    lowered = [w.lower().strip(".,!?;:") for w in words]
    out: list[str] = []
    collapsed = 0
    i = 0
    while i < n_words:
        matched = False
        for n in range(MAX_NGRAM, 0, -1):
            if i + n > n_words:
                continue
            phrase = lowered[i:i + n]
            if not any(phrase):
                continue
            repeats, j = 1, i + n
            while j + n <= n_words and lowered[j:j + n] == phrase:
                repeats += 1
                j += n
            if repeats >= MIN_LOOP_REPEATS:
                out.extend(words[i:i + n])
                collapsed += repeats - 1
                i = j
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return " ".join(out), collapsed


@dataclass
class CleanStats:
    segments_in: int = 0
    kept: int = 0
    dropped_no_speech: int = 0
    dropped_logprob: int = 0
    dropped_compression: int = 0
    dropped_hallucination: int = 0
    removed_duplicate: int = 0
    loops_collapsed: int = 0
    dropped_texts: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.dropped_no_speech + self.dropped_logprob + self.dropped_compression + self.dropped_hallucination

    def as_dict(self) -> dict[str, Any]:
        return {"segments_in": self.segments_in, "kept": self.kept, "dropped": self.dropped, "dropped_no_speech": self.dropped_no_speech,
                "dropped_logprob": self.dropped_logprob, "dropped_compression": self.dropped_compression,
                "dropped_hallucination": self.dropped_hallucination, "removed_duplicate": self.removed_duplicate,
                "loops_collapsed": self.loops_collapsed, "dropped_texts": list(self.dropped_texts)}


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= DUPLICATE_SIMILARITY


def clean_segments(segments: Iterable[Mapping[str, Any]], *, extra_phrases: Iterable[str] = (), stats: Optional[CleanStats] = None,
                   no_speech_max: float = NO_SPEECH_MAX, logprob_min: float = LOGPROB_MIN, compression_max: float = COMPRESSION_MAX,
                   collapse_loops: bool = True, collapse_duplicates: bool = True, deny_weak: bool = True) -> list[dict[str, Any]]:
    """Segments (``{start_s, end_s, text, words?, no_speech_prob?, avg_logprob?, compression_ratio?}``) without Whisper's artifacts.

    In order, per segment: dropped when the decoder doubted it (``no_speech_prob`` above ``no_speech_max``, ``avg_logprob`` below
    ``logprob_min``, ``compression_ratio`` above ``compression_max``); word loops collapsed (its ``words`` are cleared, they no longer
    match the text); dropped when the whole text is a known hallucination (:func:`is_hallucination`). Then runs of consecutive
    identical or near-identical segments become one (it keeps the first text and ends where the run ends). ``stats`` (a
    :class:`CleanStats`) accumulates what was removed. Returns new dicts; the input is not modified.
    """
    st = stats if stats is not None else CleanStats()
    extra = tuple(extra_phrases)
    kept: list[dict[str, Any]] = []

    def drop(counter: str, text: str) -> None:
        setattr(st, counter, getattr(st, counter) + 1)
        st.dropped_texts = (st.dropped_texts + [text[:80]])[-20:]

    for seg in segments:
        st.segments_in += 1
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        nsp, lp, cr = seg.get("no_speech_prob"), seg.get("avg_logprob"), seg.get("compression_ratio")
        if nsp is not None and nsp > no_speech_max:
            drop("dropped_no_speech", text)
            continue
        if lp is not None and lp < logprob_min:
            drop("dropped_logprob", text)
            continue
        if cr is not None and cr > compression_max:
            drop("dropped_compression", text)
            continue
        item = dict(seg)
        if collapse_loops:
            collapsed, loops = collapse_ngram_loops(text)
            if loops:
                st.loops_collapsed += loops
                text = collapsed.strip()
                item["words"] = []
        if is_hallucination(text, extra_phrases=extra, deny_weak=deny_weak):
            drop("dropped_hallucination", text)
            continue
        item["text"] = text
        if collapse_duplicates and kept and _similar(normalize(kept[-1]["text"]), normalize(text)):
            if item.get("end_s") is not None:
                kept[-1]["end_s"] = item["end_s"]
            st.removed_duplicate += 1
            continue
        kept.append(item)
    st.kept += len(kept)
    return kept


# ---------------------------------------------------------------- the transcriber --

@dataclass
class Transcript:
    language: str
    duration_s: float
    text: str
    segments: list[dict[str, Any]]
    language_probability: float = 0.0
    model: str = ""
    device: str = ""
    note: str = ""            # e.g. "The GPU failed (...); transcribed on the CPU."
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"language": self.language, "language_probability": self.language_probability, "duration_s": self.duration_s,
                "text": self.text, "segments": self.segments, "model": self.model, "device": self.device, "note": self.note,
                "stats": self.stats}


def _to_float32(audio: Any) -> Any:
    """16 kHz mono float32 samples from an int16 / float array (numpy is imported only here)."""
    import numpy as np  # type: ignore

    arr = np.asarray(audio)
    if arr.dtype == np.int16:
        return arr.astype(np.float32) / 32768.0
    return arr.astype(np.float32, copy=False)


def _r(value: Any, digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


class Transcriber:
    """faster-whisper behind a lock: lazy load, GPU lease, CPU fallback, cancellation. See the module docstring.

    ``model_factory(size, device, compute, download_root)`` and ``lease_factory(vram_mb, purpose, owner, timeout_s)`` are injectable
    (tests pass fakes; the defaults import ``faster_whisper`` and :mod:`hoard_link.lease` lazily). ``lease=False`` never leases.
    """

    name = "faster-whisper"

    def __init__(self, models_dir: "str | Path | None" = None, size: str = "small", device: str = "auto", compute: str = "auto",
                 lease: bool = True, *, owner: str = "hoard", model_factory: Optional[Callable[..., Any]] = None,
                 lease_factory: Optional[Callable[..., Any]] = None):
        self.models_dir = Path(models_dir) if models_dir else default_models_dir()
        self.size, self.device_setting, self.compute_setting = size, device, compute
        self.use_lease = lease
        self.owner = owner
        self._model_factory = model_factory or _load_whisper
        self._lease_factory = lease_factory or _default_lease
        self.model: Any = None
        self.loaded_key: Optional[tuple[str, str, str]] = None   # the settings the model was loaded for (not the device it ended up on)
        self.device_used = ""      # "cuda" / "cpu": where it really runs (cpu after a lease timeout or a CUDA failure)
        self.state = "idle"        # idle | downloading | loading | ready | error
        self.error = ""
        self.last_ms: Optional[int] = None
        self.cpu_fallback_reason = ""
        self.lease_state = "disabled"
        self._lock = threading.RLock()
        self._active_lease: Any = None
        self._lease_pending_release = False

    # -- configuration ---------------------------------------------------
    def reconfigure(self, size: Optional[str] = None, device: Optional[str] = None, compute: Optional[str] = None) -> None:
        with self._lock:
            self.size = size or self.size
            self.device_setting = device or self.device_setting
            self.compute_setting = compute or self.compute_setting
            if self.loaded_key != self._key():
                self._release_lease()
                self.model, self.loaded_key, self.state, self.device_used = None, None, "idle", ""
                self.lease_state = "disabled"

    def _key(self) -> tuple[str, str, str]:
        device = resolve_device(self.device_setting)
        return self.size, device, resolve_compute(self.compute_setting, device)

    def close(self) -> None:
        """Drop the model (and any lease) so its memory can be reused."""
        with self._lock:
            self._release_lease()
            self.model, self.loaded_key, self.state, self.device_used = None, None, "idle", ""

    # -- leasing ---------------------------------------------------------
    def _release_lease(self) -> None:
        held, self._active_lease = self._active_lease, None
        self._lease_pending_release = False
        if held is not None:
            try:
                held.release()
            except Exception as error:  # noqa: BLE001 - best effort
                log.warning("could not release the GPU lease: %s", error)

    def ensure_loaded(self) -> None:
        """Load the model if the current settings changed (downloading it first when it is not on disk). Safe to call repeatedly."""
        with self._lock:
            key = self._key()
            if self.model is not None and self.loaded_key == key:
                return
            self._release_lease()
            cuda_dll_dirs()
            size, device, compute = key
            if self.device_setting == "auto" and _CUDA_LIBRARY_FAILURE:
                self.cpu_fallback_reason = _CUDA_LIBRARY_FAILURE
            self.state = "loading" if model_present(self.models_dir, size) else "downloading"
            self.error = ""
            self.models_dir.mkdir(parents=True, exist_ok=True)
            taken: Any = None
            if device != "cuda" or not self.use_lease or not gpu_leasing_enabled():
                self.lease_state = "disabled"
            else:
                self.lease_state = "waiting"
                try:
                    taken = self._lease_factory(whisper_vram_mb(size), f"whisper {size}", self.owner, lease_timeout_s())
                    taken = taken.acquire() or taken
                    self.lease_state = "granted"
                except Exception as error:  # noqa: BLE001 - LeaseTimeout / LeaseError (or a broken hub): the CPU still works
                    log.warning("GPU lease unavailable (%s); loading on the CPU", error)
                    self.lease_state = "fallback_cpu"
                    self.cpu_fallback_reason = f"GPU lease unavailable: {error}"
                    taken = None
                    device = "cpu"
                    compute = resolve_compute(self.compute_setting, "cpu")
            try:
                started = time.time()
                self.model = self._model_factory(size, device, compute, str(self.models_dir))
                self.loaded_key, self.state, self.device_used = key, "ready", device
                log.info("whisper %s loaded on %s/%s in %.1fs", size, device, compute, time.time() - started)
                if taken is not None:
                    self._active_lease, self._lease_pending_release = taken, True
            except BaseException as error:
                if taken is not None:
                    self._active_lease = taken
                    self._release_lease()
                if device == "cuda" and isinstance(error, Exception) and not isinstance(error, Unavailable):
                    _remember_missing_cuda_library(error)
                    log.warning("CUDA load failed (%s); falling back to the CPU", error)
                    self.cpu_fallback_reason = str(error)
                    try:
                        compute = resolve_compute(self.compute_setting, "cpu")
                        self.model = self._model_factory(size, "cpu", compute, str(self.models_dir))
                        self.loaded_key, self.state, self.device_used = key, "ready", "cpu"
                        return
                    except Exception as inner:  # noqa: BLE001
                        error = inner
                self.model, self.loaded_key, self.state, self.error, self.device_used = None, None, "error", str(error), ""
                raise error

    # -- work ------------------------------------------------------------
    def _decode(self, audio: Any, language: Optional[str], word_timestamps: bool, vad: bool, initial_prompt: str, beam_size: int) -> Any:
        return self.model.transcribe(
            audio, language=None if language in (None, "", "auto") else language, beam_size=beam_size, vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 500} if vad else None, word_timestamps=word_timestamps,
            condition_on_previous_text=False, initial_prompt=initial_prompt or None,
            no_speech_threshold=NO_SPEECH_MAX, log_prob_threshold=LOGPROB_MIN, compression_ratio_threshold=COMPRESSION_MAX)

    def _consume(self, audio: Any, language: Optional[str], word_timestamps: bool, vad: bool, initial_prompt: str, beam_size: int,
                 progress: Optional[Callable[[float], None]], cancel: Optional[threading.Event]) -> "tuple[list[dict[str, Any]], Any]":
        """Iterate the lazy segment generator here: an inference error (a missing cuBLAS) only shows up while iterating."""
        raw, info = self._decode(audio, language, word_timestamps, vad, initial_prompt, beam_size)
        total = float(getattr(info, "duration", 0.0) or 0.0)
        out: list[dict[str, Any]] = []
        for s in raw:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Transcription cancelled.", partial=out)
            text = str(getattr(s, "text", "") or "").strip()
            if text:
                out.append({
                    "start_s": _r(s.start, 2), "end_s": _r(s.end, 2), "text": text,
                    "avg_logprob": _r(getattr(s, "avg_logprob", None), 3), "no_speech_prob": _r(getattr(s, "no_speech_prob", None), 3),
                    "compression_ratio": _r(getattr(s, "compression_ratio", None), 3),
                    "words": [{"start_s": _r(w.start, 2), "end_s": _r(w.end, 2), "word": w.word, "p": _r(getattr(w, "probability", 0.0) or 0.0, 3)}
                              for w in (getattr(s, "words", None) or [])],
                })
            if progress and total > 0:
                progress(max(0.0, min(0.99, float(s.end) / total)))
        return out, info

    def transcribe(self, audio: Any, *, language: Optional[str] = None, word_timestamps: bool = True, vad: bool = True,
                   initial_prompt: str = "", progress: Optional[Callable[[float], None]] = None,
                   cancel: Optional[threading.Event] = None, beam_size: int = 5, clean: bool = True,
                   extra_phrases: Iterable[str] = ()) -> Transcript:
        """Transcribe a file path or a 16 kHz mono sample array (int16 or float32).

        ``language`` ``None`` / ``"auto"`` detects it; ``initial_prompt`` primes names and jargon; ``progress(fraction)`` is called per
        segment; setting ``cancel`` raises :class:`hoard_link.proc.Cancelled` (its ``partial`` holds the segments so far). With
        ``clean=True`` (default) :func:`clean_segments` runs on the result and ``Transcript.stats`` says what it removed.
        """
        if not isinstance(audio, (str, os.PathLike)):
            audio = _to_float32(audio)
            if len(audio) == 0:
                return Transcript(language or "", 0.0, "", [], model=self.size)
        else:
            audio = os.fspath(audio)
        self.ensure_loaded()
        started = time.time()
        note = ""
        with self._lock:
            try:
                try:
                    segs, info = self._consume(audio, language, word_timestamps, vad, initial_prompt, beam_size, progress, cancel)
                except Cancelled:
                    raise
                except Exception as error:  # noqa: BLE001
                    # A CUDA library missing at inference time (not at load time): reload on the CPU instead of leaving the session stuck.
                    if self.device_used == "cuda" and _looks_like_cuda_error(error):
                        _remember_missing_cuda_library(error)
                        log.warning("CUDA inference failed (%s); reloading on the CPU", error)
                        self.cpu_fallback_reason = str(error)
                        self.device_setting = "cpu"
                        self._release_lease()
                        self.model, self.loaded_key = None, None
                        self.ensure_loaded()
                        note = f"The GPU failed ({str(error)[:200]}); transcribed on the CPU."
                        segs, info = self._consume(audio, language, word_timestamps, vad, initial_prompt, beam_size, progress, cancel)
                    else:
                        raise
            finally:
                if self._lease_pending_release:  # the lease covers the load and this first inference
                    self._release_lease()
            device = self.device_used
        stats: dict[str, Any] = {}
        if clean:
            st = CleanStats()
            segs = clean_segments(segs, extra_phrases=extra_phrases, stats=st)
            stats = st.as_dict()
        self.last_ms = int((time.time() - started) * 1000)
        if progress:
            progress(1.0)
        return Transcript(
            language=str(getattr(info, "language", "") or language or ""), duration_s=round(float(getattr(info, "duration", 0.0) or 0.0), 3),
            text=" ".join(s["text"] for s in segs).strip(), segments=segs,
            language_probability=round(float(getattr(info, "language_probability", 0.0) or 0.0), 3), model=self.size, device=device,
            note=note or (f"Loaded on the CPU: {self.cpu_fallback_reason[:200]}" if self.cpu_fallback_reason and device == "cpu" else ""),
            stats=stats)

    # -- status ----------------------------------------------------------
    def info(self) -> dict[str, Any]:
        size, device, compute = self._key()
        return {
            "name": self.name, "model": size, "device": device, "device_used": self.device_used, "compute": compute, "cuda": cuda_available(),
            "loaded": self.model is not None and self.loaded_key == (size, device, compute),
            "download": "ready" if model_present(self.models_dir, size) else ("downloading" if self.state == "downloading" else "missing"),
            "state": self.state, "error": self.error, "cpu_fallback_reason": self.cpu_fallback_reason, "last_ms": self.last_ms,
            "models_dir": str(self.models_dir), "gpu_lease": self.lease_state, "available": available(),
        }
