"""SEC-04 · audit of OCR/TTS/STT network egress against
`src/privacy_policy.py::assert_outbound`.

`src/privacy_policy.py`'s own module docstring names the auxiliaries it is
wired into (the embedding lane, ChromaDB, the compaction summarizer, the
reranker, and — as of lote 70a — OCR/vision, TTS and STT). This file is the
audit `SEC-04` asked for: it finds every remaining outbound point by name and
pins whether it calls the gate.

Telemetry (`src/scorecard.py`) turns out to have no network egress to gate at
all — it only ever reads/writes a local JSONL file (see
`test_local_telemetry_has_no_network_egress_to_gate` below), so there is
nothing to wire there.

OCR's egress point is `src/document_processor.py::analyze_image_with_vl_
result` (base64-encodes the image and POSTs it to whatever vision endpoint
is configured — admin-set or auto-detected, local or remote), which now
calls `assert_outbound("ocr_vision", ...)` per candidate endpoint before
`llm_call`. Two more of the same shape, named by the same ID text: remote
TTS/STT (`services/tts/tts_service.py::_synthesize_api`,
`services/stt/stt_service.py::_transcribe_api`), each now calling
`assert_outbound("tts"/"stt", base_url)` before its `httpx.post`.

Lote 70a closed the gap this file used to document (see the "Cambios
necesarios en ficheros ajenos" note in its own batch reports) — the three
"does not yet consult" tests below now assert the OPPOSITE of their old
name: that the call IS present. Kept as source-inspection tests (not
end-to-end — none of these are cheap to drive without a real vision/TTS/STT
endpoint), same style as before.
"""
from __future__ import annotations

import inspect

import pytest


def _source_of(obj) -> str:
    return inspect.getsource(obj)


# ---------------------------------------------------------------------------
# telemetry — audited and found to have nothing to gate
# ---------------------------------------------------------------------------

def test_local_telemetry_has_no_network_egress_to_gate():
    """`src/scorecard.py` is Faustus's only local-telemetry module (harness
    entries: tool calls, outcomes, timings). If it ever grows a network call,
    this test is the tripwire that says "audit it against assert_outbound
    before it ships" — it must never quietly start phoning out."""
    import src.scorecard as scorecard

    source = inspect.getsource(scorecard)
    network_markers = ("requests.", "httpx.", "urlopen(", "aiohttp", ".post(", ".get(\"http")
    hits = [m for m in network_markers if m in source]
    assert hits == [], (
        f"src/scorecard.py now contains what looks like network egress "
        f"({hits}) — SEC-04 requires it to consult privacy_policy."
        f"assert_outbound before any outbound call; this test must be "
        f"updated (not just deleted) once that call is added."
    )


# ---------------------------------------------------------------------------
# OCR — src/document_processor.py::analyze_image_with_vl_result
# ---------------------------------------------------------------------------

def test_ocr_vision_egress_now_consults_the_privacy_gate():
    """Lote 70a: the OCR/vision HTTP call now asks
    `privacy_policy.assert_outbound("ocr_vision", ...)` before it reaches
    `llm_call`, so `local_only` covers it."""
    from src.document_processor import analyze_image_with_vl_result

    source = _source_of(analyze_image_with_vl_result)
    assert "llm_call(" in source, "the egress point moved; re-audit this function"
    assert "assert_outbound(\"ocr_vision\"" in source
    # The call must run BEFORE llm_call, not after — a gate that only
    # fires once the request already left would be too late to matter.
    assert source.index("assert_outbound(\"ocr_vision\"") < source.index("llm_call(")


# ---------------------------------------------------------------------------
# TTS — services/tts/tts_service.py::TTSService._synthesize_api
# ---------------------------------------------------------------------------

def test_remote_tts_egress_now_consults_the_privacy_gate():
    from services.tts.tts_service import TTSService

    source = _source_of(TTSService._synthesize_api)
    assert "httpx.post(" in source, "the egress point moved; re-audit this method"
    assert "assert_outbound(\"tts\"" in source
    assert source.index("assert_outbound(\"tts\"") < source.index("httpx.post(")


# ---------------------------------------------------------------------------
# STT — services/stt/stt_service.py::STTService._transcribe_api
# ---------------------------------------------------------------------------

def test_remote_stt_egress_now_consults_the_privacy_gate():
    from services.stt.stt_service import STTService

    source = _source_of(STTService._transcribe_api)
    assert "httpx.post(" in source, "the egress point moved; re-audit this method"
    assert "assert_outbound(\"stt\"" in source
    assert source.index("assert_outbound(\"stt\"") < source.index("httpx.post(")


# ---------------------------------------------------------------------------
# The gate itself, for the "ocr_vision"/"tts"/"stt" component names the three
# call sites above now use — verified independently of any real endpoint.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("component", ["ocr_vision", "tts", "stt"])
def test_the_gate_itself_is_ready_for_these_component_names(monkeypatch, component):
    from src import privacy_policy

    monkeypatch.setattr(privacy_policy, "get_privacy_profile",
                        lambda project=None, owner=None: privacy_policy.PROFILE_LOCAL_ONLY)
    with pytest.raises(privacy_policy.PrivacyPolicyError) as exc:
        privacy_policy.assert_outbound(component, "https://api.example.com/v1")
    assert exc.value.component == component

    # A local endpoint is never blocked, regardless of component name.
    privacy_policy.assert_outbound(component, "http://127.0.0.1:8000")
