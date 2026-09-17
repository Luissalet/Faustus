"""WhatsApp bridge (src/whatsapp_bridge.py, src/whatsapp_tools.py,
routes/whatsapp_routes.py): the Python side against a fake bridge; the Node
bridge itself is exercised by bridges/whatsapp/check.mjs."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src import whatsapp_bridge as wa
from src import whatsapp_tools as wt


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)


class FakeBridge:
    def __init__(self):
        now = time.time()
        self.contacts = [{"jid": "34600000001@s.whatsapp.net", "name": "Ana Pérez", "phone": "34600000001"},
                         {"jid": "34600000002@s.whatsapp.net", "name": "Ana López", "phone": "34600000002"},
                         {"jid": "34600000003@s.whatsapp.net", "name": "Mamá", "phone": "34600000003"}]
        self.messages = [
            {"id": "m1", "chat": "34600000001@s.whatsapp.net", "chat_name": "Ana Pérez", "from": "34600000001@s.whatsapp.net",
             "from_name": "Ana Pérez", "from_me": False, "ts": now - 3600, "text": "¿Nos vemos el sábado?", "kind": "text", "unread": True},
            {"id": "m2", "chat": "34600000003@s.whatsapp.net", "chat_name": "Mamá", "from": "me", "from_name": "me",
             "from_me": True, "ts": now - 1800, "text": "Ya he llegado", "kind": "text", "unread": False},
        ]
        self.sent = []

    def get(self, path, params=None):
        params = params or {}
        if path == "/status":
            return {"status": "connected", "me": {"jid": "34600000000@s.whatsapp.net", "name": "Yo"}, "counts": {"messages": 2}}
        if path == "/chats":
            return [{"jid": "34600000001@s.whatsapp.net", "name": "Ana Pérez", "is_group": False, "last_ts": time.time(), "last_text": "¿Nos vemos?", "unread": 1}]
        if path == "/contacts":
            q = (params.get("q") or "").lower()
            return [c for c in self.contacts if q in c["name"].lower()]
        if path == "/messages":
            rows = self.messages
            if params.get("chat"):
                r = self.resolve(params["chat"])
                if "ambiguous" in r:
                    raise wa.BridgeError("ambiguous: Ana Pérez, Ana López")
                rows = [m for m in rows if m["chat"] == r["jid"]]
            if params.get("unread"):
                rows = [m for m in rows if m["unread"]]
            return [m for m in rows if m["ts"] >= params.get("since", 0)]
        raise wa.BridgeError("not found")

    def resolve(self, to):
        cands = [c for c in self.contacts if to.lower() in c["name"].lower()]
        exact = [c for c in cands if c["name"].lower() == to.lower()]
        if len(exact) == 1:
            return exact[0]
        if len(cands) == 1:
            return cands[0]
        if cands:
            return {"ambiguous": cands}
        return {"error": "no contact"}

    def post(self, path, body):
        if path == "/send":
            r = self.resolve(body["to"])
            if "ambiguous" in r:
                raise wa.BridgeError("ambiguous — which one? " + ", ".join(f"{c['name']} ({c['jid']})" for c in r["ambiguous"]))
            if "error" in r:
                raise wa.BridgeError(r["error"])
            self.sent.append((r["jid"], body["text"]))
            return {"ok": True, "to": r["name"], "jid": r["jid"], "id": "s1"}
        if path == "/resolve":
            return self.resolve(body["to"])
        if path == "/mark-read":
            return {"ok": True}
        raise wa.BridgeError("not found")


@pytest.fixture
def bridge(monkeypatch):
    fb = FakeBridge()
    monkeypatch.setattr(wa, "_get", fb.get)
    monkeypatch.setattr(wa, "_post", fb.post)
    monkeypatch.setattr(wa, "probe", lambda timeout=3.0: fb.get("/status"))
    return fb


def test_token_is_created_once_and_private():
    a, b = wa.token(), wa.token()
    assert a == b and len(a) >= 40


def test_read_tool_returns_a_transcript_marked_as_data(bridge):
    out = wt.read({"action": "messages", "hours": 24})
    assert out["exit_code"] == 0 and out["messages"] == 2 and out["unread"] == 1
    assert "DATA" in out["note"] and "Ana Pérez: ¿Nos vemos el sábado?" in out["transcript"] and "yo: Ya he llegado" in out["transcript"]
    only = wt.read({"chat": "Mamá"})
    assert only["messages"] == 1 and "Mamá" in only["summary_line"]
    unread = wt.read({"unread_only": True})
    assert unread["messages"] == 1
    assert wt.read({"action": "chats"})["chats"][0]["name"] == "Ana Pérez"
    assert [c["name"] for c in wt.read({"action": "contacts", "query": "ana"})["contacts"]] == ["Ana Pérez", "Ana López"]
    assert wt.read({"action": "status"})["status"] == "connected"


def test_send_tool_resolves_names_and_reports_ambiguity(bridge):
    out = wt.send({"to": "Mamá", "text": "Llego en 10 min"})
    assert out["sent"] and out["to"] == "Mamá" and bridge.sent == [("34600000003@s.whatsapp.net", "Llego en 10 min")]
    amb = wt.send({"to": "Ana", "text": "hola"})
    assert amb["exit_code"] == 1 and "ambiguous" in amb["error"] and "Ana López" in amb["error"] and "ask the user" in amb["hint"]
    assert wt.send({"to": "", "text": "x"})["exit_code"] == 1
    assert wt.send({"to": "Nadie", "text": "x"})["error"] == "no contact"


def test_ambiguous_chat_on_read_is_reported_not_guessed(bridge):
    out = wt.read({"chat": "Ana"})
    assert out["exit_code"] == 1 and "ambiguous" in out["error"]


def test_digest_action_summarises_with_the_local_model(bridge, monkeypatch):
    from src import watchers

    async def _sum(system, user, owner):
        assert "DATA" in system and "Ana Pérez" in user
        return "Ana Pérez pregunta si os veis el sábado."

    monkeypatch.setattr(watchers, "_summarise", _sum)
    text, ok = asyncio.run(wt.action_whatsapp_digest("admin", prompt='{"hours": 24}'))
    assert ok and "2 mensajes, 1 sin leer" in text and "Ana Pérez pregunta" in text
    from src.builtin_actions import BUILTIN_ACTIONS
    assert "whatsapp_digest" in BUILTIN_ACTIONS


def test_status_when_the_bridge_is_down(monkeypatch):
    monkeypatch.setattr(wa, "probe", lambda timeout=3.0: None)
    st = wa.status()
    assert st["status"] == "stopped" and "installed" in st and st["port"] == wa.DEFAULT_PORT


def test_routes_send_and_start_are_human_only():
    from routes.whatsapp_routes import setup_whatsapp_routes, SendBody
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    from fastapi import HTTPException
    router = setup_whatsapp_routes()
    routes = {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}
    assert ("POST", "/api/whatsapp/send") in routes and ("GET", "/api/whatsapp/status") in routes

    class Req:
        headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        state = type("S", (), {"current_user": "admin", "is_admin": True})()
        client = type("C", (), {"host": "127.0.0.1"})()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes[("POST", "/api/whatsapp/send")](SendBody(to="x", text="y"), Req()))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        asyncio.run(routes[("POST", "/api/whatsapp/start")](Req()))
    assert exc2.value.status_code == 403


def test_tools_are_registered():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src import tool_capabilities as tc
    names = {t["function"]["name"] for t in FUNCTION_TOOL_SCHEMAS}
    assert {"whatsapp_read", "whatsapp_send"} <= names and {"whatsapp_read", "whatsapp_send"} <= set(TOOL_TAGS)
    assert tc.ToolEffect.EXTERNAL_SIDE_EFFECT in tc.capabilities_for_tool("whatsapp_send").effects
    assert tc.ToolEffect.READ_PRIVATE in tc.capabilities_for_tool("whatsapp_read").effects


# ---------------------------------------------------------------------------
# voice notes, media, history (the second wave)
# ---------------------------------------------------------------------------

def test_voice_notes_are_transcribed_once_and_read_as_text(bridge, monkeypatch):
    now = time.time()
    bridge.messages.append({"id": "v1", "chat": "34600000001@s.whatsapp.net", "chat_name": "Ana Pérez",
                            "from": "34600000001@s.whatsapp.net", "from_name": "Ana Pérez", "from_me": False,
                            "ts": now - 600, "text": "", "kind": "audio", "unread": True,
                            "media": "v1.ogg", "mime": "audio/ogg; codecs=opus", "seconds": 72, "voice": True})
    calls = []

    class Svc:
        available = True

        def transcribe(self, audio, **kw):
            calls.append(audio)
            return "  llego a las nueve  "

    import types, sys
    fake = types.ModuleType("services.stt")
    fake.get_stt_service = lambda: Svc()
    monkeypatch.setitem(sys.modules, "services.stt", fake)
    monkeypatch.setattr(wa, "media", lambda name: (b"OGG", "audio/ogg"))

    out = wt.read({"hours": 2})
    assert out["voice_notes"] == 1 and out["voice_notes_transcribed"] == 1
    assert "[voice note 1:12] llego a las nueve" in out["transcript"]
    # second read hits the cache: the speech service is not called again
    wt.read({"hours": 2})
    assert len(calls) == 1
    assert wa.cached_transcript("v1") == "llego a las nueve"
    # opting out leaves the note untranscribed but visible
    bridge.messages[-1].pop("transcript", None)   # the fake hands out the same dicts
    out = wt.read({"hours": 2, "transcribe_audio": False})
    assert "[voice note 1:12] (not transcribed)" in out["transcript"]


def test_voice_note_without_a_speech_provider_stays_untranscribed(bridge, monkeypatch):
    bridge.messages.append({"id": "v2", "chat": "34600000001@s.whatsapp.net", "from": "34600000001@s.whatsapp.net",
                            "from_name": "Ana Pérez", "from_me": False, "ts": time.time() - 60, "text": "",
                            "kind": "audio", "unread": True, "media": "v2.ogg"})
    import types, sys

    class Svc:
        available = False

    fake = types.ModuleType("services.stt")
    fake.get_stt_service = lambda: Svc()
    monkeypatch.setitem(sys.modules, "services.stt", fake)
    out = wt.read({"hours": 1})
    assert out["voice_notes"] == 1 and out["voice_notes_transcribed"] == 0
    assert wa.cached_transcript("v2") is None


def test_media_names_are_validated_and_history_is_a_post(bridge, monkeypatch):
    assert wa.media("../token") is None
    assert wa.media("a/b.ogg") is None
    seen = {}
    monkeypatch.setattr(wa, "_post", lambda path, body: seen.update({path: body}) or {"ok": True})
    wa.history("Ana Pérez", 80)
    assert seen == {"/history": {"chat": "Ana Pérez", "count": 80}}


def test_media_routes_are_admin_reads_and_transcribe_exists():
    from routes import whatsapp_routes
    router = whatsapp_routes.setup_whatsapp_routes()
    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert ("/api/whatsapp/avatar", ("GET",)) in paths
    assert ("/api/whatsapp/media/{name}", ("GET",)) in paths
    assert ("/api/whatsapp/history", ("POST",)) in paths
    assert ("/api/whatsapp/transcribe", ("POST",)) in paths


# ---------------------------------------------------------------------------
# wave 3: replies, attachments, reactions, search, Ask Faustus
# ---------------------------------------------------------------------------

def test_send_tool_quotes_and_attaches_workspace_files(bridge, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(wa, "_post", lambda path, body: seen.update({path: body}) or {"ok": True, "to": "Ana Pérez", "jid": "x", "id": "s9"})
    out = wt.send({"to": "Ana Pérez", "text": "vale", "reply_to": "m1"})
    assert out["sent"] and seen["/send"]["quote"] == "m1" and "media" not in seen["/send"]
    pdf = tmp_path / "informe.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = wt.send({"to": "Ana Pérez", "text": "el informe", "attachment": str(pdf)}, resolve_path=lambda p: str(pdf))
    assert out["sent"] and out["attachment"] == "informe.pdf"
    media = seen["/send"]["media"]
    assert media["mime"] == "application/pdf" and media["filename"] == "informe.pdf" and media["voice"] is False
    # a path the resolver refuses is reported, never sent
    def refuse(p):
        raise ValueError("outside the allowed roots")
    out = wt.send({"to": "Ana", "attachment": "/etc/passwd"}, resolve_path=refuse)
    assert out["exit_code"] == 1 and "attachment" in out["error"]


def test_voice_attachment_falls_back_when_ffmpeg_is_missing(monkeypatch):
    seen = {}
    monkeypatch.setattr(wa, "_post", lambda path, body: seen.update({path: body}) or {"ok": True})
    monkeypatch.setattr(wa, "_to_ogg_opus", lambda data: None)
    wa.send_file("Ana", b"webm", "audio/webm", filename="v.webm", voice=True)
    assert seen["/send"]["media"]["voice"] is False and seen["/send"]["media"]["mime"] == "audio/webm"
    monkeypatch.setattr(wa, "_to_ogg_opus", lambda data: b"OggS")
    wa.send_file("Ana", b"webm", "audio/webm", filename="v.webm", voice=True)
    assert seen["/send"]["media"]["voice"] is True and seen["/send"]["media"]["mime"].startswith("audio/ogg")


def test_react_tool_and_search_action(bridge, monkeypatch):
    seen = {}
    monkeypatch.setattr(wa, "_post", lambda path, body: seen.update({path: body}) or {"ok": True})
    out = wt.react({"message_id": "m1", "emoji": "❤️"})
    assert out["reacted"] and seen["/react"] == {"id": "m1", "emoji": "❤️"}
    assert wt.react({})["exit_code"] == 1
    monkeypatch.setattr(wa, "_get", lambda path, params=None: list(reversed(bridge.messages)) if path == "/search" else bridge.get(path, params))
    out = wt.read({"action": "search", "query": "sábado"})
    assert out["matches"] == 2 and "¿Nos vemos el sábado?" in out["transcript"] and "DATA" in out["note"]
    assert wt.read({"action": "search"})["exit_code"] == 1


def test_assist_never_sends_and_marks_the_transcript_as_data(bridge, monkeypatch):
    prompts = []

    async def fake_summarise(system, user, owner, *, foreground=False):
        prompts.append((system, user))
        assert foreground, "a person is waiting on assist: it must skip the interactive-quiet gate"
        return "Ana pregunta por el sábado."

    import src.watchers as watchers
    monkeypatch.setattr(watchers, "_summarise", fake_summarise)
    out = asyncio.run(wt.assist("admin", "Ana Pérez", "draft_reply", "que sí, a las 8"))
    assert out["text"] and out["messages"] == 1 and not bridge.sent
    system, user = prompts[0]
    assert "DATA" in system and "que sí, a las 8" in user and "¿Nos vemos el sábado?" in user
    with pytest.raises(ValueError):
        asyncio.run(wt.assist("admin", "Ana Pérez", "delete_everything"))


def test_wave3_routes_exist_and_side_effects_are_human_only():
    from routes import whatsapp_routes
    router = whatsapp_routes.setup_whatsapp_routes()
    paths = {r.path for r in router.routes}
    for p in ("/react", "/delete", "/forward", "/edit", "/typing", "/subscribe", "/search", "/upload", "/assist"):
        assert f"/api/whatsapp{p}" in paths
    import inspect
    src = inspect.getsource(whatsapp_routes)
    for name in ("react", "delete", "forward", "edit", "upload"):
        body = src.split(f"async def {name}(")[1].split("async def")[0]
        assert "require_human(request)" in body, name
