"""Cheap speech discovery: never download/load a model merely to open settings."""
from importlib.util import find_spec
import os


def capabilities(settings: dict, kind: str) -> dict:
    provider = settings.get(f"{kind}_provider", "disabled")
    if not isinstance(provider, str) or not settings.get(f"{kind}_enabled", kind == "tts"):
        provider = "disabled"
    if provider not in ("disabled", "browser", "local", "system") and not provider.startswith("endpoint:"):
        provider = "disabled"
    dependency = "faster_whisper" if kind == "stt" else "kokoro"
    installed = provider != "local" or find_spec(dependency) is not None
    if provider == "system":
        installed = os.name == "nt" and kind == "tts"
    return {
        "provider": provider,
        "configured": provider != "disabled",
        "dependency_installed": installed,
        "ready": False,  # discovery is not a successful inference/health probe
        "execution": "local" if provider in ("local", "system") else "browser" if provider == "browser" else "endpoint" if provider.startswith("endpoint:") else "disabled",
        "model": settings.get(f"{kind}_model", ""),
        "language": settings.get("stt_language", ""),
        "voice": settings.get("tts_voice", ""),
        "streaming": False,
        "audio_retention": "temporary-input" if kind == "stt" else "request-controlled-cache",
    }
