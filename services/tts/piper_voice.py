# services/tts/piper_voice.py
"""Piper TTS — fully local, MIT-licensed neural voice synthesis.

Two runtimes, tried in this order:
  - the `piper` Python package (pip name `piper-tts`, `from piper import
    PiperVoice`) — preferred, no subprocess.
  - a `piper` binary at ``data/tts/piper/bin/piper(.exe)`` — invoked with
    ``--model <onnx> --output_file <wav>``, text piped on stdin.

Voices are ``.onnx`` + ``.onnx.json`` pairs under ``data/tts/piper/voices/``,
named ``<locale>-<voice>-<quality>`` (e.g. ``es_ES-davefx-medium``), matching
the public Piper voices repository's own naming. That name is also how the
download path on the repo is derived: ``es_ES-davefx-medium`` ->
``es/es_ES/davefx/medium/es_ES-davefx-medium.onnx``.

Nothing here is imported at module load time in a way that requires the
`piper` package or a network connection — every network call is explicit and
goes through this module's `download_voice` / `install_binary`, both of which
are admin-gated at the route layer.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

PIPER_DIR = Path(DATA_DIR) / "tts" / "piper"
VOICES_DIR = PIPER_DIR / "voices"
BIN_DIR = PIPER_DIR / "bin"

HF_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
GITHUB_RELEASES_BASE = "https://github.com/rhasspy/piper/releases/latest/download"

# Curated, recommended voices. Any other properly-named voice from the public
# repo can still be requested by name; this dict only drives the "recommended"
# list shown in Settings and fills in language/quality before the sidecar
# .onnx.json has been downloaded.
CATALOGUE: Dict[str, Dict[str, str]] = {
    "es_ES-davefx-medium": {"language": "es_ES", "quality": "medium"},
    "es_ES-sharvard-medium": {"language": "es_ES", "quality": "medium"},
    "es_ES-mls_10246-low": {"language": "es_ES", "quality": "low"},
    "es_ES-carlfm-x_low": {"language": "es_ES", "quality": "x_low"},
    "en_US-lessac-medium": {"language": "en_US", "quality": "medium"},
    "en_US-amy-medium": {"language": "en_US", "quality": "medium"},
    "en_US-ryan-high": {"language": "en_US", "quality": "high"},
    "en_GB-alan-medium": {"language": "en_GB", "quality": "medium"},
}

# One asset per platform on the engine's GitHub release page.
PIPER_RELEASE_ASSETS = {
    "nt": "piper_windows_amd64.zip",
    "posix": "piper_linux_x86_64.tar.gz",
}

# Downloads run outside outbound_fetch's small (20MB) web-content cap — a
# medium-quality voice is ~60MB and the engine binary archive is larger — so
# this module streams with httpx directly, capped generously here instead.
_MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024


def _ensure_dirs() -> None:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)


# ── Paths & naming ──────────────────────────────────────────────────────────

def _safe_voice_name(name: str) -> str:
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in name):
        raise ValueError(f"invalid Piper voice name: {name!r}")
    return name


def voice_paths(name: str) -> Tuple[Path, Path]:
    """.onnx and .onnx.json paths for `name` — never resolved outside VOICES_DIR."""
    safe = _safe_voice_name(name)
    return VOICES_DIR / f"{safe}.onnx", VOICES_DIR / f"{safe}.onnx.json"


def voice_urls(name: str) -> Tuple[str, str]:
    """Derives the HF repo download URLs from the voice name.

    ``es_ES-davefx-medium`` -> lang=es, locale=es_ES, voice=davefx, quality=medium
    -> ``.../es/es_ES/davefx/medium/es_ES-davefx-medium.onnx`` (+ .json).
    Voice ids with a dash in them (e.g. ``mls_10246``) keep working because
    only the first and last dash-separated pieces are treated as fixed.
    """
    safe = _safe_voice_name(name)
    parts = safe.split("-")
    if len(parts) < 3:
        raise ValueError(f"cannot derive a Piper voice path from {name!r} (expected locale-voice-quality)")
    locale = parts[0]
    voice = "-".join(parts[1:-1])
    quality = parts[-1]
    lang = locale.split("_")[0]
    base = f"{HF_VOICES_BASE}/{lang}/{locale}/{voice}/{quality}/{safe}"
    return f"{base}.onnx", f"{base}.onnx.json"


def binary_path() -> Path:
    return BIN_DIR / ("piper.exe" if os.name == "nt" else "piper")


# ── Runtime discovery ───────────────────────────────────────────────────────

def python_runtime_available() -> bool:
    return find_spec("piper") is not None


def binary_runtime_available() -> bool:
    try:
        return binary_path().is_file()
    except OSError:
        return False


def active_runtime() -> Optional[str]:
    """"python" | "binary" | None — python preferred when both are present."""
    if python_runtime_available():
        return "python"
    if binary_runtime_available():
        return "binary"
    return None


# ── Voice discovery ──────────────────────────────────────────────────────────

def list_voices() -> List[Dict[str, Any]]:
    """Installed voices, scanned from VOICES_DIR (name/language/quality)."""
    out: List[Dict[str, Any]] = []
    if not VOICES_DIR.exists():
        return out
    for onnx in sorted(VOICES_DIR.glob("*.onnx")):
        name = onnx.stem
        json_path = VOICES_DIR / f"{name}.onnx.json"
        language = CATALOGUE.get(name, {}).get("language", "")
        quality = CATALOGUE.get(name, {}).get("quality", "")
        if json_path.exists():
            try:
                meta = json.loads(json_path.read_text(encoding="utf-8"))
                language = (meta.get("language") or {}).get("code") or language
                quality = (meta.get("audio") or {}).get("quality") or quality
            except Exception as e:  # noqa: BLE001 — a corrupt sidecar must not break discovery
                logger.warning("Piper: could not read voice metadata for %s: %s", name, e)
        out.append({"name": name, "language": language, "quality": quality, "installed": True})
    return out


def catalogue_voices() -> List[Dict[str, Any]]:
    """The recommended catalogue, each entry flagged with whether it's installed."""
    installed = {v["name"] for v in list_voices()}
    return [
        {"name": name, "language": info["language"], "quality": info["quality"], "installed": name in installed}
        for name, info in CATALOGUE.items()
    ]


# ── Downloads (admin-gated at the route layer) ──────────────────────────────

def _default_fetch(url: str) -> bytes:
    """Streaming GET with no environment proxy trust, capped and logged.
    Used as the default `fetch` for download_voice/install_binary; tests
    inject a fake instead so no network call happens under pytest."""
    import httpx

    from src.privacy_policy import assert_outbound
    assert_outbound("tts_piper", url)

    chunks: List[bytes] = []
    total = 0
    with httpx.Client(trust_env=False, timeout=180, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"Piper download exceeded {_MAX_DOWNLOAD_BYTES} bytes: {url}")
                chunks.append(chunk)
    return b"".join(chunks)


def download_voice(name: str, fetch: Optional[Callable[[str], bytes]] = None) -> None:
    """Downloads the .onnx + .onnx.json pair for `name` into VOICES_DIR."""
    _ensure_dirs()
    onnx_url, json_url = voice_urls(name)
    onnx_path, json_path = voice_paths(name)
    fetcher = fetch or _default_fetch
    logger.info("Piper: downloading voice %s", name)
    onnx_bytes = fetcher(onnx_url)
    json_bytes = fetcher(json_url)
    onnx_path.write_bytes(onnx_bytes)
    json_path.write_bytes(json_bytes)
    logger.info("Piper: voice %s installed (%d bytes)", name, len(onnx_bytes))


def install_binary(fetch: Optional[Callable[[str], bytes]] = None) -> Path:
    """Downloads the platform Piper engine release archive from GitHub and
    extracts the `piper` executable (and its sibling runtime files) into
    BIN_DIR. Admin-only at the route layer; always logged."""
    _ensure_dirs()
    asset = PIPER_RELEASE_ASSETS["nt" if os.name == "nt" else "posix"]
    url = f"{GITHUB_RELEASES_BASE}/{asset}"
    fetcher = fetch or _default_fetch
    logger.warning("Piper: installing engine binary from %s", url)
    archive_bytes = fetcher(url)

    with tempfile.TemporaryDirectory(prefix="piper-install-") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / asset
        archive_path.write_bytes(archive_bytes)
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        if asset.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(extract_dir)
        else:
            import tarfile
            with tarfile.open(archive_path) as tf:
                tf.extractall(extract_dir)

        exe_name = "piper.exe" if os.name == "nt" else "piper"
        found = next((p for p in extract_dir.rglob(exe_name) if p.is_file()), None)
        if not found:
            raise RuntimeError("Piper engine archive did not contain a piper executable")

        target = binary_path()
        shutil.copy2(found, target)
        for sibling in found.parent.iterdir():
            if sibling == found:
                continue
            dest = BIN_DIR / sibling.name
            if sibling.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(sibling, dest)
            else:
                shutil.copy2(sibling, dest)
        if os.name != "nt":
            os.chmod(target, 0o755)

    logger.warning("Piper: engine binary installed at %s", target)
    return target


# ── Voice selection & synthesis ─────────────────────────────────────────────

def select_voice(voice: str, language: str = "") -> Optional[str]:
    """Language-aware pick among installed voices.

    If `voice` is installed and (no `language` given, or its language
    matches), it wins. Otherwise the first installed voice whose language
    matches `language` wins. Otherwise `voice` if installed, else the first
    installed voice, else None.
    """
    installed = list_voices()
    if not installed:
        return None
    by_name = {v["name"]: v for v in installed}
    want = (language or "").split("-")[0].lower()
    if voice and voice in by_name:
        have = (by_name[voice]["language"] or "").split("_")[0].lower()
        if not want or not have or want == have:
            return voice
    if want:
        for v in installed:
            if (v["language"] or "").split("_")[0].lower() == want:
                return v["name"]
    if voice and voice in by_name:
        return voice
    return installed[0]["name"]


def _synthesize_python(text: str, onnx_path: Path, length_scale: float) -> bytes:
    from piper import PiperVoice

    pv = PiperVoice.load(str(onnx_path))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if hasattr(pv, "synthesize_wav"):
            # Current piper-tts (>=1.3): synthesize() itself only yields audio
            # chunks; synthesize_wav() is the one that writes a WAV file, and
            # takes a SynthesisConfig rather than a length_scale kwarg.
            try:
                from piper.config import SynthesisConfig
                pv.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=length_scale))
            except ImportError:
                pv.synthesize_wav(text, wf)
        else:
            # Older piper-tts: synthesize() itself writes to the wav file.
            try:
                pv.synthesize(text, wf, length_scale=length_scale)
            except TypeError:
                pv.synthesize(text, wf)
    return buf.getvalue()


def _synthesize_binary(text: str, onnx_path: Path, length_scale: float) -> bytes:
    fd, out_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    out_path = Path(out_name)
    try:
        cmd = [
            str(binary_path()), "--model", str(onnx_path),
            "--output_file", str(out_path), "--length_scale", str(length_scale),
        ]
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=120, **kwargs)
        if result.returncode != 0:
            raise RuntimeError(f"piper binary failed: {result.stderr.decode('utf-8', 'replace')[:500]}")
        return out_path.read_bytes()
    finally:
        out_path.unlink(missing_ok=True)


def synthesize(text: str, voice: str = "", speed: float = 1.0, language: str = "") -> Optional[bytes]:
    """Synthesizes `text` to WAV bytes, or None if no runtime/voice is available."""
    runtime = active_runtime()
    if runtime is None:
        logger.warning("Piper TTS: no runtime available (install piper-tts or the engine binary)")
        return None
    chosen = select_voice(voice, language)
    if not chosen:
        logger.warning("Piper TTS: no voices installed")
        return None
    onnx_path, _ = voice_paths(chosen)
    if not onnx_path.exists():
        logger.error("Piper TTS: voice file missing at %s", onnx_path)
        return None
    length_scale = (1.0 / speed) if speed and speed > 0 else 1.0
    try:
        if runtime == "python":
            return _synthesize_python(text, onnx_path, length_scale)
        return _synthesize_binary(text, onnx_path, length_scale)
    except Exception as e:
        logger.error("Piper TTS synthesis failed: %s", e, exc_info=True)
        return None


def piper_status() -> Dict[str, Any]:
    """Extra detail folded into /api/tts/capabilities when provider == "piper"."""
    runtime = active_runtime()
    return {
        "dependency_installed": runtime is not None,
        "runtime": runtime,
        "installed_voices": list_voices(),
        "catalogue": catalogue_voices(),
    }
