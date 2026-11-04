"""Darwin #992: a voice turn consults and feeds the Second Brain exactly like a
typed one, and its one switch is the chat's own `no_memory` flag.

Run: python -m pytest tests/test_voice_brain.py -q -p no:cacheprovider
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.voice_stream_routes import setup_voice_stream_routes
from services import voice_stream as vs
from src import voice_brain
from tests.test_voice_stream import DONE, FakeRun, FakeRuns, FakeSTT, FakeTTS, delta


def build(record):
    runs = FakeRuns()

    async def start_turn(owner, sid, text, client_message_id, **kw):
        record.append({"text": text, **kw})
        runs.active[sid] = FakeRun(f"run-{len(record)}", [delta("Vale."), DONE], delay=0.01)
        return runs.active[sid].run_id

    manager = vs.VoiceSessionManager(stt=FakeSTT(text="hola"), tts=FakeTTS(), runs=runs, start_turn=start_turn)
    app = FastAPI()
    app.include_router(setup_voice_stream_routes(manager))
    return app, manager


def test_voice_turns_use_the_brain_by_default_and_no_memory_when_switched_off():
    sent = []
    app, _ = build(sent)
    with TestClient(app) as client:
        created = client.post("/api/voice/sessions", json={"session_id": "chat-1", "read_aloud": False}).json()
        assert created["brain"] is True
        url = "/api/voice/sessions/" + created["id"]
        assert client.post(url + "/say", json={"text": "uno"}).status_code == 200
        assert client.patch(url, json={"brain": False}).json()["brain"] is False
        assert client.post(url + "/say", json={"text": "dos"}).status_code == 200
        off = client.post("/api/voice/sessions", json={"session_id": "chat-2", "brain": False}).json()
        assert off["brain"] is False
    # The default turn goes out exactly as before (four arguments, nothing extra).
    assert sent[0] == {"text": "uno"}
    assert sent[1] == {"text": "dos", "no_memory": True}


def test_a_starter_without_the_keyword_still_works_for_ordinary_turns():
    calls = []

    async def legacy(owner, sid, text, cmid):
        calls.append(text)
        return "run-1"

    manager = vs.VoiceSessionManager(stt=FakeSTT(), tts=FakeTTS(), runs=FakeRuns(), start_turn=legacy)
    assert asyncio.run(manager.start_turn(None, "s", "hola", "c1")) == "run-1"
    assert calls == ["hola"]


def test_native_starter_sends_the_chat_no_memory_flag(monkeypatch):
    seen = []
    import src.group_orchestrator as go

    def fake_native(app, owner, body):
        seen.append(body)
        return body

    class Resp:
        status_code = 200
        headers = {"X-Odysseus-Run-Id": "r1"}

    async def endpoint(request, initial_response_timeout_s=None):
        return Resp()

    monkeypatch.setattr(go, "native_request", fake_native)
    start = vs.native_turn_starter(object(), endpoint)
    assert asyncio.run(start("o", "sid", "hola", "c1")) == "r1"
    assert asyncio.run(start("o", "sid", "hola", "c2", no_memory=True)) == "r1"
    assert "no_memory" not in seen[0] and seen[1]["no_memory"] == "true"


def _settings(monkeypatch, values, prefs):
    monkeypatch.setattr(voice_brain, "_setting", lambda key, default: values.get(key, default))
    monkeypatch.setattr(voice_brain, "_prefs", lambda owner: dict(prefs))
    monkeypatch.setattr(voice_brain, "_brain_counts", lambda owner: {"notes": 2, "entities": 5, "relations": 1,
                                                                     "pending_extraction": 0})
    monkeypatch.setattr(voice_brain, "_chat_memories", lambda owner, sid: 3)


def test_link_status_reports_the_text_chat_switches(monkeypatch):
    _settings(monkeypatch, {"agent_context_engine": True}, {})
    on = voice_brain.link_status("ana", "chat-1")
    assert on["same_as_text_chat"] and on["switch"] == "no_memory"
    assert on["consulted"] == {"memory_recall": True, "brain_context_source": True, "context_engine": True}
    assert on["fed"]["memory_capture"] and on["fed"]["brain_extraction"]
    assert on["brain"]["entities"] == 5 and on["chat_memories"] == 3
    off = voice_brain.link_status("ana", "chat-1", voice_brain=False)
    assert not off["consulted"]["memory_recall"] and not off["fed"]["memory_capture"]
    # Context Engine off: memories are recalled, the brain source is not read.
    _settings(monkeypatch, {"agent_context_engine": False}, {"auto_memory": False})
    plain = voice_brain.link_status("ana", "chat-1")
    assert plain["consulted"]["memory_recall"] and not plain["consulted"]["brain_context_source"]
    assert not plain["fed"]["memory_capture"]


def test_brain_route_reflects_the_open_voice_sessions(monkeypatch):
    _settings(monkeypatch, {}, {})
    app, _ = build([])
    with TestClient(app) as client:
        assert client.get("/api/voice/brain?session_id=chat-9").json()["voice_brain"] is True
        client.post("/api/voice/sessions", json={"session_id": "chat-9", "brain": False})
        body = client.get("/api/voice/brain?session_id=chat-9").json()
        assert body["voice_brain"] is False and body["voice_sessions"] == 1
        assert client.get("/api/voice/brain?session_id=x&no_memory=true").json()["voice_brain"] is False


def test_voice_mcp_server_exposes_the_brain():
    spec = importlib.util.spec_from_file_location(
        "voice_brain_mcp", Path(__file__).resolve().parents[1] / "mcp_servers/voice_server.py")
    voice = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(voice)
    seen = []

    def handle(request):
        seen.append((request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"voice_brain": True, "id": "v1"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await voice._voice_session(client, {"action": "start", "session_id": "sid", "brain": False})
            return await voice._request(client, "GET", "/brain", params={"session_id": "sid"})

    assert asyncio.run(scenario())["voice_brain"] is True
    assert json.loads(seen[0][2])["brain"] is False
    assert seen[1][0].endswith("/api/voice/brain") and seen[1][1] == {"session_id": "sid"}
    names = {t.name for t in voice.TOOLS}
    assert "voice_brain" in names
    session = next(t for t in voice.TOOLS if t.name == "voice_session")
    assert session.inputSchema["properties"]["brain"]["type"] == "boolean"


def test_a_no_memory_turn_does_not_feed_memory_capture(monkeypatch):
    """brain=false must stop the memory extraction after the turn too, not
    only the recall before it (the link status says nothing is fed)."""
    import sys
    import types
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tests.test_kv_cache_invalidation_2927 import _install_chat_helpers_stubs
    helpers = _install_chat_helpers_stubs(monkeypatch)
    seen = {"memory": 0}
    mod = types.ModuleType("services.memory.memory_extractor")

    async def fake_extract_and_store(*a, **k):
        seen["memory"] += 1
    mod.extract_and_store = fake_extract_and_store
    monkeypatch.setitem(sys.modules, "services.memory.memory_extractor", mod)
    ep = types.ModuleType("src.task_endpoint")
    ep.resolve_task_endpoint = lambda url, model, headers, owner=None: (url, model, headers)
    monkeypatch.setitem(sys.modules, "src.task_endpoint", ep)
    queued = []
    monkeypatch.setattr(helpers, "_spawn_bg", lambda coro: queued.append(coro) or coro.close())
    monkeypatch.setattr(helpers, "needs_auto_name", lambda name: False)
    sess = SimpleNamespace(endpoint_url="u", model="m", headers={}, history=[object()] * 8, name="n")
    manager = SimpleNamespace(save_sessions=lambda: None)

    def fire(**kw):
        queued.clear()
        helpers.run_post_response_tasks(sess, manager, "s", "hola", "vale", None, {"auto_memory": True},
                                        MagicMock(), MagicMock(), None, owner="o", **kw)
        return len(queued)
    assert fire() == 1
    assert fire(no_memory=True) == 0
