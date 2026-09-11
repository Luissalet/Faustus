"""Lote 70a, punto A.7 — end-to-end proof that OCR/vision, TTS and STT
actually consult `src.privacy_policy.assert_outbound` before their outbound
call, complementing `tests/test_l68_sec04_ocr_tts_stt_egress_audit.py`'s
source-level audit (which only proves the call is present in the source,
not that it is actually reached with the right arguments and actually
blocks). Same two-part contract every other auxiliary's own test proves
(`tests/test_privacy_policy_auxiliaries.py`, `tests/test_rerank_privacy_policy.py`):
the policy is actually consulted (not a coincidental failure), and a block
degrades the same way any other failure of that call already does.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import privacy_policy as pp


def _blocked(monkeypatch, calls):
    def fake_assert_outbound(component, destination, **kwargs):
        calls.append((component, destination))
        raise pp.PrivacyPolicyError(
            component, destination, pp.PROFILE_LOCAL_ONLY,
            pp.ErrorInfo(code="privacy.blocked_outbound", message="blocked"),
        )
    monkeypatch.setattr(pp, "assert_outbound", fake_assert_outbound)


# ── OCR/vision ───────────────────────────────────────────────────────────

def test_blocked_vision_never_calls_llm_call(monkeypatch, tmp_path):
    from src import document_processor as dp

    calls = []
    _blocked(monkeypatch, calls)

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "gpt-4o"})
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda configured, owner=None: (
        "http://vision.example.com/v1/chat/completions", "gpt-4o", {},
    ))
    from src import endpoint_resolver
    monkeypatch.setattr(endpoint_resolver, "resolve_vision_fallback_candidates", lambda owner=None: [])

    def _forbidden_llm_call(*a, **k):
        raise AssertionError("llm_call must not be reached when the privacy gate blocks")
    monkeypatch.setattr(dp, "llm_call", _forbidden_llm_call)

    image = tmp_path / "image.png"
    image.write_bytes(b"not-a-real-png-but-good-enough")

    result = dp.analyze_image_with_vl_result(str(image), owner="alice")

    assert calls == [("ocr_vision", "http://vision.example.com/v1/chat/completions")]
    # Degrades exactly like an unavailable VL model already did before this
    # gate existed — no crash, a plain marker text back to the caller.
    assert result == {"text": "[VL model unavailable - image not analyzed]", "model": ""}


def test_unblocked_vision_still_calls_llm_call(monkeypatch, tmp_path):
    """The gate must not get in the way when the profile allows the call."""
    from src import document_processor as dp

    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "gpt-4o"})
    monkeypatch.setattr(dp, "_resolve_vl_model", lambda configured, owner=None: (
        "http://vision.example.com/v1/chat/completions", "gpt-4o", {},
    ))
    from src import endpoint_resolver
    monkeypatch.setattr(endpoint_resolver, "resolve_vision_fallback_candidates", lambda owner=None: [])
    monkeypatch.setattr(dp, "llm_call", lambda *a, **k: "a description")

    image = tmp_path / "image.png"
    image.write_bytes(b"not-a-real-png-but-good-enough")

    result = dp.analyze_image_with_vl_result(str(image), owner="alice")
    assert result == {"text": "a description", "model": "gpt-4o"}


# ── TTS ──────────────────────────────────────────────────────────────────

class _Endpoint:
    def __init__(self, base_url, api_key=""):
        self.base_url = base_url
        self.api_key = api_key


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _Db:
    def __init__(self, row):
        self._row = row

    def query(self, *a, **k):
        return _Query(self._row)

    def close(self):
        return None


def test_blocked_tts_never_opens_the_socket(monkeypatch, tmp_path):
    from services.tts.tts_service import TTSService
    import src.database as database

    calls = []
    _blocked(monkeypatch, calls)
    monkeypatch.setattr(database, "SessionLocal", lambda: _Db(_Endpoint("https://tts.example.com/v1")))

    def _forbidden_post(*a, **k):
        raise AssertionError("httpx.post must not be reached when the privacy gate blocks")
    import services.tts.tts_service as tts_mod
    monkeypatch.setattr(tts_mod.httpx, "post", _forbidden_post)

    service = TTSService(cache_dir=str(tmp_path))
    result = service._synthesize_api("hello", "ep1", "tts-1", "alloy")

    assert calls == [("tts", "https://tts.example.com/v1")]
    assert result is None


# ── STT ──────────────────────────────────────────────────────────────────

def test_blocked_stt_never_opens_the_socket(monkeypatch, tmp_path):
    from services.stt.stt_service import STTService
    import src.database as database

    calls = []
    _blocked(monkeypatch, calls)
    monkeypatch.setattr(database, "SessionLocal", lambda: _Db(_Endpoint("https://stt.example.com/v1")))

    def _forbidden_post(*a, **k):
        raise AssertionError("httpx.post must not be reached when the privacy gate blocks")
    import services.stt.stt_service as stt_mod
    monkeypatch.setattr(stt_mod.httpx, "post", _forbidden_post)

    service = STTService()
    result = service._transcribe_api(b"fake-audio-bytes", "ep1", "whisper-1")

    assert calls == [("stt", "https://stt.example.com/v1")]
    assert result is None
