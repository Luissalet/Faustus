"""Speech discovery, privacy, provider pinning, and non-blocking inference."""
import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.stt_routes import setup_stt_routes
from routes.tts_routes import setup_tts_routes
from services.speech_runtime import capabilities
from services.stt.stt_service import STTService
from services.tts.tts_service import TTSService


def service(kind, **overrides):
    defaults = dict(available=True, _load_settings=lambda: {f"{kind}_enabled": True, f"{kind}_provider": "local"},
                    transcribe=lambda data, **kwargs: "Hola Faustus", synthesize=lambda text, **kwargs: b"RIFFaudio")
    return SimpleNamespace(**(defaults | overrides))


def client(kind, svc):
    app = FastAPI()
    app.include_router(setup_stt_routes(svc) if kind == "stt" else setup_tts_routes(svc))
    return TestClient(app)


@pytest.mark.parametrize("provider", [None, 123, [], {}, "nonsense"])
def test_corrupt_provider_is_disabled(provider):
    assert not capabilities({"stt_enabled": True, "stt_provider": provider}, "stt")["configured"]


def test_discovery_does_not_probe_or_claim_endpoint_health():
    result = capabilities({"stt_enabled": True, "stt_provider": "endpoint:remote"}, "stt")
    assert result["configured"] and not result["ready"]
    assert result["execution"] == "endpoint"


def test_stt_success_and_empty():
    with client("stt", service("stt")) as c:
        assert c.post("/api/stt/transcribe", files={"file": ("a.webm", b"audio")}).json() == {"text": "Hola Faustus"}
        assert c.post("/api/stt/transcribe", files={"file": ("a.webm", b"")}).status_code == 400


def test_provider_switch_rejected_before_transcription():
    calls = []
    with client("stt", service("stt", transcribe=lambda *a, **kw: calls.append(1))) as c:
        r = c.post("/api/stt/transcribe", data={"expected_provider": "endpoint:other"}, files={"file": ("a.webm", b"audio")})
        assert r.status_code == 409 and not calls


def test_synthesis_no_store_and_no_cache():
    calls = []
    def synth(text, **kwargs):
        calls.append(kwargs)
        return b"RIFFaudio"
    with client("tts", service("tts", synthesize=synth)) as c:
        for fmt in ("audio", "base64"):
            r = c.post("/api/tts/synthesize", json={"text": "Hola", "format": fmt, "use_cache": False, "expected_provider": "local"})
            assert r.status_code == 200
            assert r.headers["cache-control"] == "no-store"
        assert all(not call["use_cache"] and call["expected_provider"] == "local" for call in calls)
        assert c.post("/api/tts/synthesize", json={"text": "x" * 5001}).status_code == 422
        assert c.post("/api/tts/synthesize", json={"text": "hello", "format": "bad"}).status_code == 422


def test_tts_provider_switch_fails_closed():
    with client("tts", service("tts")) as c:
        assert c.post("/api/tts/synthesize", json={"text": "hola", "expected_provider": "endpoint:new"}).status_code == 409


def test_service_rechecks_provider_at_dispatch(monkeypatch, tmp_path):
    stt = STTService()
    tts = TTSService(cache_dir=str(tmp_path))
    for kind, svc in [("stt", stt), ("tts", tts)]:
        monkeypatch.setattr(svc, "_load_settings", lambda kind=kind: {f"{kind}_provider": "endpoint:changed"})
    with pytest.raises(ValueError):
        stt.transcribe(b"private", expected_provider="local")
    with pytest.raises(ValueError):
        tts.synthesize("private", expected_provider="local")


@pytest.mark.parametrize("override,expected", [("auto", ""), ("en", "en"), ("es", "es"), ("en-US", "en")])
def test_language_override_is_per_request(monkeypatch, override, expected):
    svc = STTService()
    monkeypatch.setattr(svc, "_load_settings", lambda: {"stt_enabled": True, "stt_provider": "local", "stt_model": "base", "stt_language": "es"})
    seen = []
    def transcribe(data, language, metadata):
        seen.append(language)
        metadata["language"] = "en"
        return "Hello Faustus"
    monkeypatch.setattr(svc, "_transcribe_local", transcribe)
    metadata = {}
    assert svc.transcribe(b"audio", language_override=override, metadata=metadata) == "Hello Faustus"
    assert seen == [expected] and metadata["language"] == "en"


def test_windows_speech_sends_text_as_data_not_code(monkeypatch):
    import base64
    import json
    from services.tts import system_voice
    if system_voice.os.name != "nt":
        pytest.skip("Windows adapter")
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(b"RIFFaudio"))
    monkeypatch.setattr(system_voice.subprocess, "run", run)
    text = 'Hola "; Remove-Item X; $(malicious) áé'
    assert system_voice.synthesize_system(text, language="es-ES") == b"RIFFaudio"
    command, kwargs = seen[0]
    assert text not in str(command)
    assert json.loads(kwargs["input"])["text"] == text
    assert kwargs["timeout"] == 90


def test_system_synthesis_bypasses_cache(monkeypatch, tmp_path):
    from services.tts import system_voice
    svc = TTSService(cache_dir=str(tmp_path))
    monkeypatch.setattr(svc, "_load_settings", lambda: {"tts_enabled": True, "tts_provider": "system", "tts_model": "", "tts_voice": "", "tts_speed": "1"})
    monkeypatch.setattr(system_voice, "synthesize_system", lambda *a: b"RIFFvoice")
    assert svc.synthesize("Hola", language="es") == b"RIFFvoice"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_inference_does_not_block_other_requests():
    started = threading.Event()
    def slow(data, **kwargs):
        started.set()
        time.sleep(0.2)
        return "hola"
    app = FastAPI()
    app.include_router(setup_stt_routes(service("stt", transcribe=slow)))
    @app.get("/ping")
    def ping():
        return {"ok": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        task = asyncio.create_task(c.post("/api/stt/transcribe", files={"file": ("a.webm", b"audio")}))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert (await c.get("/ping")).status_code == 200
        assert not task.done(), "inference blocked the event loop"
        assert (await task).status_code == 200
