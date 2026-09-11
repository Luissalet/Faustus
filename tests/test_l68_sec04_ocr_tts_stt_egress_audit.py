"""SEC-04 · audit of OCR/TTS/STT network egress against
`src/privacy_policy.py::assert_outbound`.

`src/privacy_policy.py`'s own module docstring already names the auxiliaries
it is wired into (the embedding lane, ChromaDB, the compaction summarizer,
the reranker — each verified by its own test) and the ones that still are
not: OCR and telemetry. This file is the audit `SEC-04` asks for: it finds
every remaining outbound point by name and pins whether it calls the gate.

Telemetry (`src/scorecard.py`) turns out to have no network egress to gate at
all — it only ever reads/writes a local JSONL file (see
`test_local_telemetry_has_no_network_egress_to_gate` below), so there is
nothing left to wire there.

OCR's egress point is `src/document_processor.py::analyze_image_with_vl_
result` (base64-encodes the image and POSTs it to whatever vision endpoint
is configured — admin-set or auto-detected, local or remote). Two more of
the same shape turned up in the audit that the ID text also names: remote
TTS/STT (`services/tts/tts_service.py::_synthesize_api`,
`services/stt/stt_service.py::_transcribe_api`, both an `httpx.post` to a
configurable `base_url`).

None of those three files are in this lot's PROPIOS list (document_processor
belongs to lot 66; the tts/stt services belong to neither this lot nor any
other named in this wave), so the gate cannot be called from here — see the
lot report's "Cambios necesarios en ficheros ajenos" for the exact diff each
needs (one `privacy_policy.assert_outbound(...)` call before the existing
`llm_call`/`httpx.post`). The tests below are the audit's receipt: they read
each function's SOURCE (not its behaviour — none of these are cheap to drive
end-to-end without a real vision/TTS/STT endpoint) and go red the moment
someone adds the call, which is exactly the signal whoever closes the
ajeno-file gap needs — delete or update the corresponding test then, don't
leave it stale.
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

def test_ocr_vision_egress_does_not_yet_consult_the_privacy_gate():
    """Documents the real, current gap: the OCR/vision HTTP call is made
    with no call to privacy_policy.assert_outbound first, so `local_only`
    does not yet cover it. See the lot report for the exact fix — this file
    is not in this lot's PROPIOS list, so the wiring cannot be added here."""
    from src.document_processor import analyze_image_with_vl_result

    source = _source_of(analyze_image_with_vl_result)
    assert "llm_call(" in source, "the egress point moved; re-audit this function"
    assert "assert_outbound" not in source and "privacy_policy" not in source, (
        "src/document_processor.py::analyze_image_with_vl_result now calls "
        "the privacy gate — this audit test is satisfied and should be "
        "deleted (the gap it documents is closed)."
    )


# ---------------------------------------------------------------------------
# TTS — services/tts/tts_service.py::TTSService._synthesize_api
# ---------------------------------------------------------------------------

def test_remote_tts_egress_does_not_yet_consult_the_privacy_gate():
    from services.tts.tts_service import TTSService

    source = _source_of(TTSService._synthesize_api)
    assert "httpx.post(" in source, "the egress point moved; re-audit this method"
    assert "assert_outbound" not in source and "privacy_policy" not in source, (
        "services/tts/tts_service.py::TTSService._synthesize_api now calls "
        "the privacy gate — this audit test is satisfied and should be "
        "deleted (the gap it documents is closed)."
    )


# ---------------------------------------------------------------------------
# STT — services/stt/stt_service.py::STTService._transcribe_api
# ---------------------------------------------------------------------------

def test_remote_stt_egress_does_not_yet_consult_the_privacy_gate():
    from services.stt.stt_service import STTService

    source = _source_of(STTService._transcribe_api)
    assert "httpx.post(" in source, "the egress point moved; re-audit this method"
    assert "assert_outbound" not in source and "privacy_policy" not in source, (
        "services/stt/stt_service.py::STTService._transcribe_api now calls "
        "the privacy gate — this audit test is satisfied and should be "
        "deleted (the gap it documents is closed)."
    )


# ---------------------------------------------------------------------------
# The gate itself works for the "ocr_vision"/"tts"/"stt" component names the
# ajeno-file diffs in the lot report use, so the fix is a one-line call away
# once someone can touch those files.
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
