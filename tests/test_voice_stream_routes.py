"""HTTP and WebSocket surface of the streaming voice sessions."""

import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.voice_stream_routes import setup_voice_stream_routes
from services import voice_stream as vs
from tests.test_voice_stream import DONE, FakeRun, FakeRuns, FakeSTT, FakeTTS, delta, pcm


def build():
    runs = FakeRuns()
    sent = []

    async def start_turn(owner, sid, text, client_message_id):
        sent.append(text)
        runs.active[sid] = FakeRun(f"run-{len(sent)}", [delta("Vale. "), delta("Hecho."), DONE], delay=0.01)
        return runs.active[sid].run_id

    manager = vs.VoiceSessionManager(stt=FakeSTT(text="pon una alarma"), tts=FakeTTS(), runs=runs,
                                     start_turn=start_turn)
    app = FastAPI()
    app.include_router(setup_voice_stream_routes(manager))
    return app, manager, runs, sent


def test_context_language_hint_is_optional_and_can_be_cleared():
    app, manager, _, _ = build()
    with TestClient(app) as client:
        created = client.post("/api/voice/sessions", json={
            "session_id": "bilingual", "language": "auto", "language_hint": "es"}).json()
        assert created["language_hint"] == "es" and created["language"] == "auto"
        url = "/api/voice/sessions/" + created["id"]
        assert client.patch(url, json={"language_hint": ""}).json()["language_hint"] == ""
        assert client.post("/api/voice/sessions", json={
            "session_id": "other", "language_hint": "invalid"}).status_code == 422
        # The existing explicit language field still accepts other languages.
        assert client.patch(url, json={"language": "fr"}).json()["language"] == "fr"


def test_websocket_utterance_carries_context_without_pinning_session_language():
    app, manager, _, _ = build()
    seen = []
    def transcribe(audio, language, metadata):
        seen.append((language, metadata.get("language_hint")))
        metadata["language"] = "en"
        return "Yes."
    manager.transcribe = transcribe
    with TestClient(app) as client:
        created = client.post("/api/voice/sessions", json={
            "session_id": "frame-context", "language_hint": "es", "send_mode": "client", "partials": False}).json()
        with client.websocket_connect(f"/api/voice/sessions/{created['id']}/stream?token={created['token']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "utterance_start", "language_hint": "en"})
            ws.send_bytes(pcm())
            ws.send_json({"type": "utterance_end"})
            for _ in range(10):
                event = ws.receive_json()
                if event.get("type") == "transcript" and event.get("final"):
                    break
            assert event["text"] == "Yes."
        assert seen == [("", "en")]  # manager normalizes the empty automatic language to "auto"
        status = client.get("/api/voice/sessions/" + created["id"]).json()
        assert status["language"] == "auto" and status["language_hint"] == "es"


def wait_events(client, voice_id, kind, after=0, timeout=5.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        page = client.get(f"/api/voice/sessions/{voice_id}/events", params={"after": after, "timeout": 1}).json()
        for event in page["events"]:
            after = event["seq"]
            seen.append(event)
            if event["type"] == kind:
                return seen
    raise AssertionError(f"no {kind}: {[e['type'] for e in seen]}")


def test_rest_utterance_runs_the_whole_loop():
    app, manager, runs, sent = build()
    with TestClient(app) as client:
        caps = client.get("/api/voice/capabilities").json()
        assert caps["streaming"]["pcm_sample_rate"] == 16000
        created = client.post("/api/voice/sessions", json={"session_id": "sid-1"}).json()
        assert created["token"] and created["stream_url"].endswith(created["token"])
        voice_id = created["id"]
        response = client.post(f"/api/voice/sessions/{voice_id}/utterance", content=vs.pcm16_to_wav(pcm(0.8)),
                               headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 200, response.text
        assert response.json()["verdict"] == "ok" and sent == ["pon una alarma"]
        events = wait_events(client, voice_id, "speech_end")
        assert [e["text"] for e in events if e["type"] == "audio"] == ["Vale.", "Hecho."]
        with_audio = client.get(f"/api/voice/sessions/{voice_id}/events", params={"after": 0, "audio": True}).json()
        assert any(e.get("audio_b64") for e in with_audio["events"])
        assert client.post(f"/api/voice/sessions/{voice_id}/interrupt").json()["interrupted"] is True
        assert client.delete(f"/api/voice/sessions/{voice_id}").json() == {"closed": True}
        assert client.get(f"/api/voice/sessions/{voice_id}").status_code == 404


def test_websocket_streams_pcm_in_and_audio_out():
    app, manager, runs, sent = build()
    with TestClient(app) as client:
        created = client.post("/api/voice/sessions", json={"session_id": "sid-1", "partials": False}).json()
        with client.websocket_connect(f"/api/voice/sessions/{created['id']}/stream?token={created['token']}") as ws:
            ws.send_text(json.dumps({"type": "utterance_start", "sample_rate": 16000, "preroll_ms": 300}))
            for _ in range(3):
                ws.send_bytes(pcm(0.3))
            ws.send_text(json.dumps({"type": "utterance_end", "speech_end_at": time.time()}))
            kinds, audio_frames, texts = [], 0, []
            deadline = time.time() + 10
            while time.time() < deadline:
                message = ws.receive()
                if message.get("bytes") is not None:
                    audio_frames += 1
                    continue
                event = json.loads(message["text"])
                kinds.append(event["type"])
                if event["type"] == "audio":
                    texts.append(event["text"])
                if event["type"] == "speech_end":
                    break
            assert "transcript" in kinds and "sent" in kinds
            assert texts == ["Vale.", "Hecho."] and audio_frames == 2
            ws.send_text(json.dumps({"type": "played", "run_id": "run-1"}))
            ws.send_text(json.dumps({"type": "stop"}))
        assert sent == ["pon una alarma"]


def test_websocket_rejects_a_wrong_token():
    app, manager, runs, sent = build()
    with TestClient(app) as client:
        created = client.post("/api/voice/sessions", json={"session_id": "sid-1"}).json()
        try:
            with client.websocket_connect(f"/api/voice/sessions/{created['id']}/stream?token=nope") as ws:
                ws.receive_text()
            raise AssertionError("connected with a wrong token")
        except AssertionError:
            raise
        except Exception:
            pass


def test_unknown_chat_is_a_404():
    app, manager, runs, sent = build()

    def check(owner, sid):
        raise KeyError("Chat session not found")

    manager._session_check = check
    with TestClient(app) as client:
        assert client.post("/api/voice/sessions", json={"session_id": "nope"}).status_code == 404
