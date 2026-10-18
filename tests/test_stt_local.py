"""Local decoding keeps clear language switches and uses context for weak short audio."""
from types import SimpleNamespace

import pytest

from services.stt.stt_service import STTService
from src import family_services


def local_service(monkeypatch, *, text="See.", language="en", probability=0.63,
                  retry="Sí.", retry_error=False):
    calls = []

    class Model:
        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            hinted = bool(kwargs.get("language"))
            if hinted and retry_error:
                raise RuntimeError("hint decoder failed")
            value = retry if hinted else text
            info = SimpleNamespace(language=kwargs.get("language") or language,
                                   language_probability=1.0 if hinted else probability)
            return iter([SimpleNamespace(start=0, end=1, text=value)]), info

    svc = STTService()
    monkeypatch.setattr(family_services, "transcribe_bytes", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(svc, "_get_whisper", lambda: Model())
    monkeypatch.setattr(svc, "_load_settings", lambda: {
        "stt_enabled": True, "stt_provider": "local", "stt_model": "small", "stt_language": ""})
    return svc, calls


def test_local_decoding_keeps_names_neutral_and_does_not_force_language(monkeypatch):
    svc, calls = local_service(monkeypatch)
    assert svc.transcribe(b"audio", language_override="auto") == "See."
    assert len(calls) == 1 and "language" not in calls[0]
    assert "initial_prompt" not in calls[0] and "hotwords" not in calls[0]
    assert calls[0]["condition_on_previous_text"] is False


def test_short_weak_detection_redecodes_audio_with_recent_language(monkeypatch):
    svc, calls = local_service(monkeypatch)
    metadata = {"language_hint": "es"}
    assert svc.transcribe(b"audio", language_override="auto", metadata=metadata) == "Sí."
    assert len(calls) == 2 and calls[1]["language"] == "es"
    assert metadata["language"] == metadata["language_hint_used"] == "es"


@pytest.mark.parametrize("text,probability,override,hint", [
    ("Yes.", 0.99, "auto", "es"),
    ("The report is ready and you can open it here.", 0.4, "auto", "es"),
    ("See.", 0.5, "en", "es"),
    ("See.", 0.5, "auto", "en"),
    ("See.", 0.5, "auto", ""),
    ("See.", 0.5, "auto", "invalid"),
    ("", 0.1, "auto", "es"),
])
def test_hint_does_not_override_clear_speech_or_explicit_choice(monkeypatch, text, probability, override, hint):
    svc, calls = local_service(monkeypatch, text=text, probability=probability, retry=text)
    metadata = {"language_hint": hint}
    assert svc.transcribe(b"audio", language_override=override, metadata=metadata) == text
    assert len(calls) == 1
    assert "language_hint_used" not in metadata


@pytest.mark.parametrize("retry,retry_error", [("", False), ("Sí.", True)])
def test_failed_context_retry_preserves_initial_transcript(monkeypatch, retry, retry_error):
    svc, calls = local_service(monkeypatch, retry=retry, retry_error=retry_error)
    metadata = {"language_hint": "es"}
    assert svc.transcribe(b"audio", language_override="auto", metadata=metadata) == "See."
    assert metadata["language"] == "en" and "language_hint_used" not in metadata
