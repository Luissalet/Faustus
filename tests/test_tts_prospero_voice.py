"""tts_provider = "prospero": speech through the voice engines of Prospero's Hoard."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services.speech_runtime import capabilities
from services.tts import prospero_voice
from services.tts.tts_service import TTSService

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20

seen = []
reject_voice_ref = []


class _Hoard(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        if self.path == "/api/voice/engines":
            body = json.dumps({"tts": [
                {"id": "kittentts", "label": "KittenTTS", "installed": True, "reason": "ready",
                 "capabilities": {"cloning": False}},
                {"id": "xtts", "label": "XTTS", "installed": False, "reason": "pip install TTS",
                 "capabilities": {"cloning": True}}], "stt": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        seen.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        if reject_voice_ref and "voice_ref" in seen[-1][1].get("voice", {}):
            self.send_response(400)
            self.end_headers()
            return
        if self.path == "/api/voice/speak":
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.write(WAV)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def hoard():
    srv = HTTPServer(("127.0.0.1", 0), _Hoard)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    seen.clear()
    reject_voice_ref.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_only_loopback_addresses_are_accepted():
    assert prospero_voice.base_url("") == "http://127.0.0.1:8815"
    assert prospero_voice.base_url("http://localhost:9000/") == "http://localhost:9000"
    assert prospero_voice.base_url("http://[::1]:9000") == "http://[::1]:9000"
    assert prospero_voice.base_url("http://example.com:8815") == ""
    assert prospero_voice.base_url("http://192.168.1.5:8815") == ""
    assert prospero_voice.base_url("file:///etc/passwd") == ""
    assert prospero_voice.synthesize_prospero("hola", url="http://example.com") is None


def test_synthesize_sends_engine_voice_and_speed(hoard):
    audio = prospero_voice.synthesize_prospero("hello", url=hoard, engine="kittentts", voice="Luna", speed=1.25,
                                               language="en")
    assert audio == WAV
    path, body = seen[-1]
    assert path == "/api/voice/speak"
    assert body == {"text": "hello", "voice": {"engine_id": "kittentts", "voice_ref": "Luna", "speed": 1.25,
                                               "language": "en"}}


def test_generic_default_voice_is_not_forwarded(hoard):
    prospero_voice.synthesize_prospero("hi", url=hoard, voice="alloy")
    assert "voice_ref" not in seen[-1][1]["voice"]
    assert "speed" not in seen[-1][1]["voice"]


def test_engines_lists_what_the_hoard_reports(hoard):
    out = prospero_voice.engines(hoard)
    assert out["reachable"] is True
    assert [(e["id"], e["ready"], e["cloning"]) for e in out["engines"]] == [("kittentts", True, False), ("xtts", False, True)]


def test_engines_when_the_hoard_is_down():
    out = prospero_voice.engines("http://127.0.0.1:1")
    assert out["reachable"] is False and out["engines"] == []
    assert prospero_voice.engines("http://example.com")["reachable"] is False


def _service(monkeypatch, tmp_path, **settings):
    svc = TTSService(cache_dir=str(tmp_path))
    base = {"tts_enabled": True, "tts_provider": "prospero", "tts_model": "tts-1", "tts_voice": "Bella",
            "tts_speed": "1", "tts_command_template": "", "tts_prospero_url": "", "tts_prospero_engine": ""}
    base.update(settings)
    monkeypatch.setattr(TTSService, "_load_settings", lambda self: dict(base))
    return svc


def test_service_dispatches_to_the_hoard(monkeypatch, tmp_path, hoard):
    svc = _service(monkeypatch, tmp_path, tts_prospero_url=hoard, tts_prospero_engine="kittentts")
    assert svc.available is True
    assert svc.synthesize("buenas") == WAV
    assert seen[-1][1]["voice"]["engine_id"] == "kittentts"
    assert seen[-1][1]["voice"]["voice_ref"] == "Bella"
    assert svc.get_stats()["engine_id"] == "kittentts"


def test_service_unavailable_for_a_remote_address(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path, tts_prospero_url="http://example.com:8815")
    assert svc.available is False
    assert svc.synthesize("x") is None


def test_capabilities_know_the_provider_for_speech_only():
    tts = capabilities({"tts_provider": "prospero", "tts_enabled": True}, "tts")
    assert tts["provider"] == "prospero" and tts["execution"] == "local" and tts["dependency_installed"] is True
    stt = capabilities({"stt_provider": "prospero", "stt_enabled": True}, "stt")
    assert stt["provider"] == "disabled"


def test_a_voice_left_over_from_another_provider_does_not_silence_the_hoard(hoard):
    # tts_voice is shared by all providers; the Hoard answers 400 to a reference it does not know
    reject_voice_ref.append(True)
    audio = prospero_voice.synthesize_prospero("hola", url=hoard, engine="kittentts", voice="af_heart")
    assert audio == WAV
    assert "voice_ref" in seen[0][1]["voice"] and "voice_ref" not in seen[1][1]["voice"]