"""
media_capabilities.py — what multimodal backends are actually usable, right
now, because something answered a cheap probe (MEDIA-01).

The rule this module exists to enforce: **never "available" without a probe**.
`shutil.which("ffmpeg")` finding a binary on PATH is evidence the binary
exists, not evidence it runs; `find_spec("faster_whisper")` finding the
package is evidence it imports, not evidence a model is loadable. Both are
reported as what they are — `installed` — and every entry carries `probed_at`
so a caller can see the check is fresh rather than a value cached from last
boot and never revisited.

Deliberately reuses `src.media_backends.pool.survey()` for ComfyUI instead of
writing a second HTTP probe: that module is the existing authority for "is
this engine actually there" (it is what `/api/media/engine` answers with),
and a parallel probe here would be a second, possibly disagreeing, opinion
about the same daemon.

Every probe here is bounded: a `-version` subprocess with a short timeout, an
import check, or the pool's own per-engine HTTP call. Nothing loads a model,
transcodes a file or renders anything — the batch's rule against intensive
work to "close" MEDIA-01 is enforced by construction, not by caller discipline.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from importlib.util import find_spec
from typing import Any, Dict, Optional, Tuple

from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: A probe result is cheap but not free — a `capabilities_manifest()` called
#: on every keystroke of a settings page should not shell out to ffmpeg or
#: hit every configured ComfyUI engine each time. Short enough that "just
#: installed it" is visible within the time it takes to notice.
_CACHE_SECONDS = 20.0

_cache: Dict[str, Any] = {"at": 0.0, "manifest": None}


def _run_version(binary: str, *args: str, timeout: float = 3.0
                 ) -> Tuple[bool, str, str]:
    """`(installed, version_line, detail)`. `installed` means the binary was
    found AND answered inside the timeout — a PATH entry that hangs or a
    stale symlink is not "installed" for this manifest's purposes."""
    path = shutil.which(binary)
    if not path:
        return False, "", f"{binary!r} not found on PATH"
    try:
        proc = subprocess.run([path, *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", f"{binary!r} on PATH but did not answer within {timeout}s"
    except OSError as exc:
        return False, "", f"{binary!r} on PATH but could not be run: {exc}"
    lines = (proc.stdout or proc.stderr or "").splitlines()
    return True, (lines[0].strip() if lines else ""), path


def probe_ffmpeg() -> Dict[str, Any]:
    """ffmpeg and ffprobe travel together — a pair with one missing cannot
    actually transcode or inspect anything, so `installed` requires both."""
    ffmpeg_ok, version, ffmpeg_detail = _run_version("ffmpeg", "-version")
    ffprobe_ok, _, ffprobe_detail = _run_version("ffprobe", "-version")
    installed = ffmpeg_ok and ffprobe_ok
    detail = (version if installed else
             ffmpeg_detail if not ffmpeg_ok else ffprobe_detail)
    return {"installed": installed, "version": version if ffmpeg_ok else "",
           "detail": detail, "probed_at": now_iso()}


def _importable(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        # A malformed or partially-installed package can make find_spec raise
        # instead of returning None; either way it is not usably installed.
        return False


def probe_stt() -> Dict[str, Any]:
    """`faster_whisper` — the local transcription backend `services.speech_
    runtime.capabilities()` and `routes/local_video_routes.py` already check
    for. This does the same cheap import check, never loading a model."""
    installed = _importable("faster_whisper")
    return {"installed": installed,
           "detail": "faster_whisper package importable" if installed
                     else "faster_whisper not installed", "probed_at": now_iso()}


def probe_tts_kokoro() -> Dict[str, Any]:
    """`kokoro` — this Faustus's local text-to-speech dependency
    (`services/speech_runtime.py`)."""
    installed = _importable("kokoro")
    return {"installed": installed,
           "detail": "kokoro package importable" if installed
                     else "kokoro not installed", "probed_at": now_iso()}


def probe_tts_piper() -> Dict[str, Any]:
    """`piper` — not wired into this Faustus's own TTS provider today, but
    named explicitly in MEDIA-01's scope as a voice engine to report on
    honestly rather than silently omit. Checked as a CLI binary, which is how
    piper is normally installed, alongside the Python package name in case a
    `pip install piper-tts` provided it instead."""
    binary = shutil.which("piper")
    package = _importable("piper")
    installed = bool(binary) or package
    if binary:
        detail = f"piper binary on PATH at {binary}"
    elif package:
        detail = "piper Python package importable"
    else:
        detail = "no piper binary on PATH and no piper package installed"
    return {"installed": installed, "detail": detail, "probed_at": now_iso()}


def probe_comfyui() -> Dict[str, Any]:
    """Every configured ComfyUI engine, via the pool's own probe — reused
    rather than duplicated (see module docstring). `installed` is true when
    at least one engine actually answered; every engine's own detail rides
    along so "configured two, only one answers" stays visible instead of
    collapsing to one boolean."""
    from src.media_backends import pool

    try:
        engines = pool.survey()
    except Exception as exc:  # a probe must never take the caller down with it
        logger.warning("comfyui capability probe failed", exc_info=True)
        return {"installed": False, "detail": f"probe failed: {exc}",
               "engines": [], "probed_at": now_iso()}
    ready = [e for e in engines if e.ok]
    return {"installed": bool(ready),
           "detail": (f"{len(ready)}/{len(engines)} engine(s) ready" if engines
                     else "no engine configured"),
           "engines": [e.to_dict() for e in engines], "probed_at": now_iso()}


#: name -> probe. A dict so `capabilities_manifest()` and its tests can add or
#: mock one entry without touching the others.
_PROBES = {
    "ffmpeg": probe_ffmpeg,
    "comfyui": probe_comfyui,
    "stt_faster_whisper": probe_stt,
    "tts_kokoro": probe_tts_kokoro,
    "tts_piper": probe_tts_piper,
}


def capabilities_manifest(*, force: bool = False) -> Dict[str, Any]:
    """Every backend, each with its own fresh-enough probe result.

    Cached for `_CACHE_SECONDS` so a page that polls this does not shell out
    to ffmpeg or hit ComfyUI on every render; `force=True` (or the cache
    simply having expired) always re-probes rather than ever answering from a
    value nothing measured this run."""
    now = time.monotonic()
    cached = _cache.get("manifest")
    if not force and cached is not None and now - _cache["at"] < _CACHE_SECONDS:
        return cached
    backends = {name: probe() for name, probe in _PROBES.items()}
    manifest = {"checked_at": now_iso(), "backends": backends}
    _cache["manifest"] = manifest
    _cache["at"] = now
    return manifest


__all__ = [
    "probe_ffmpeg", "probe_stt", "probe_tts_kokoro", "probe_tts_piper",
    "probe_comfyui", "capabilities_manifest",
]
