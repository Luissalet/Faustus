"""Promises found in WhatsApp chats, offered to People's Hoard (src/whatsapp_commitments.py).

Private chats: the feature is OFF by default, needs the chats to be named, never sends groups, and sends message text only to
People's tool through the hub's proxy (never to the event bus, never into the state file). Invented people and numbers only."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from src import settings
from src import whatsapp_bridge as wa
from src import whatsapp_commitments as wc
from src import hoard_hub

MARTA = "34600111222@s.whatsapp.net"
PEDRO = "34600333444@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)


def _cfg(monkeypatch, **flags):
    values = {**settings.DEFAULT_SETTINGS, **flags}
    monkeypatch.setattr(settings, "load_settings", lambda: dict(values))


def _row(id_, chat, name, text, *, from_me=False, ago=600, **extra):
    return {"id": id_, "chat": chat, "chat_name": name, "from_me": from_me, "text": text, "kind": "text",
            "ts": time.time() - ago, **extra}


ROWS = [
    _row("a1", MARTA, "Marta Lozano", "Te mando la factura el lunes", from_me=True, ago=900),
    _row("a2", MARTA, "Marta Lozano", "Te debo 30 euros de la cena", ago=800),
    _row("b1", PEDRO, "Pedro Ruiz", "me debes el libro", ago=700),
    _row("g1", GROUP, "Piso compartido", "Yo me encargo de la compra", ago=600),
    _row("a3", MARTA, "Marta Lozano", "[voice note]", ago=500, kind="audio"),
    _row("a4", MARTA, "Marta Lozano", "Te lo mando mañana", ago=400, forwarded=True),
    _row("a5", MARTA, "Marta Lozano", "mensaje borrado", ago=300, deleted=True),
]


class FakeHub:
    """Stands in for hoard_hub._call: records every call and answers like the hub's tool proxy."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def __call__(self, method, path, body=None, *, timeout=3.0):
        self.calls.append((method, path, body))
        if self.reply is not None:
            return self.reply
        n = len((body or {}).get("arguments", {}).get("messages", []))
        return 200, {"ok": True, "app": "people", "tool": "commitments_from_messages", "status": 200,
                     "result": {"received": n, "created": n, "queued": 0, "duplicates": 0, "linked": n, "unlinked": 0,
                                "skipped": {"seen": 0}, "commitments": [
                                    {"id": "c1", "direction": "i_owe", "text": "Mandar la factura", "due": "2026-10-12", "person_name": "Marta Lozano"}]}}, {}


@pytest.fixture
def world(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    monkeypatch.setattr(wa, "messages", lambda chat=None, **kw: list(ROWS))
    return hub


def test_it_is_off_by_default_and_touches_nothing(monkeypatch):
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_enabled"] is False
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_all_chats"] is False
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_chats"] == []
    _cfg(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    monkeypatch.setattr(wa, "messages", lambda *a, **k: pytest.fail("the bridge must not be read while the feature is off"))
    out = wc.sync()
    assert out["status"] == "off"
    assert hub.calls == []
    # the switch alone is not enough: a chat has to be named (or all one-to-one chats chosen)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True)
    assert wc.sync()["status"] == "no_chats"
    assert hub.calls == []


def test_only_the_chats_the_person_enabled_are_offered_and_groups_never(monkeypatch):
    cfg = {"enabled": True, "all_chats": False, "chats": ["marta lozano"]}
    batch, skipped = wc.to_batch(ROWS, cfg)
    assert [m["id"] for m in batch] == ["a1", "a2"]
    assert skipped["group"] == 1 and skipped["not_enabled"] == 1 and skipped["not_text"] >= 2 and skipped["forwarded"] == 1
    first = batch[0]
    assert first == {"id": "a1", "chat_id": MARTA, "contact_name": "Marta Lozano", "sender": "me",
                     "text": "Te mando la factura el lunes", "timestamp": first["timestamp"], "phone": "+34600111222"}
    assert first["timestamp"].endswith("+00:00") and batch[1]["sender"] == "them"
    # by JID and by phone number (any way of writing it), case-insensitive on the name
    assert [m["id"] for m in wc.to_batch(ROWS, {"all_chats": False, "chats": [PEDRO]})[0]] == ["b1"]
    assert [m["id"] for m in wc.to_batch(ROWS, {"all_chats": False, "chats": ["+34 600 333 444"]})[0]] == ["b1"]
    assert [m["id"] for m in wc.to_batch(ROWS, {"all_chats": False, "chats": ["PEDRO RUIZ"]})[0]] == ["b1"]
    assert wc.to_batch(ROWS, {"all_chats": False, "chats": ["Marta"]})[0] == [], "a partial name does not enable a chat"
    # every one-to-one chat on: still not the group
    everyone, skip2 = wc.to_batch(ROWS, {"all_chats": True, "chats": []})
    assert {m["id"] for m in everyone} == {"a1", "a2", "b1"} and skip2["group"] == 1


def test_sync_sends_message_text_only_to_peoples_tool_through_the_hub(monkeypatch, world, tmp_path):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    out = wc.sync()
    assert out["status"] == "ok" and out["offered"] == 2 and out["created"] == 2 and out["read"] == len(ROWS)
    assert len(world.calls) == 1
    method, path, body = world.calls[0]
    assert (method, path) == ("POST", "/api/apps/people/call")
    assert body["tool"] == "commitments_from_messages" and body["arguments"]["refine"] is False
    sent = [m["text"] for m in body["arguments"]["messages"]]
    assert sent == ["Te mando la factura el lunes", "Te debo 30 euros de la cena"]
    assert "me debes el libro" not in json.dumps(body), "Pedro's chat was not enabled"
    # nothing was published anywhere else, and the state file holds counts, not text
    assert all(p.startswith("/api/apps/people/call") for _m, p, _b in world.calls)
    state_files = list((tmp_path / "watchers").glob("*.json"))
    assert state_files
    blob = "".join(f.read_text(encoding="utf-8") for f in state_files)
    assert "factura" not in blob and "cena" not in blob and "last_counts" in blob
    assert wc.status()["last_status"] == "ok" and wc.status()["last_counts"]["offered"] == 2


def test_the_second_run_looks_back_from_the_cursor_and_refine_is_off_unless_asked(monkeypatch, world):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    seen = []
    monkeypatch.setattr(wa, "messages", lambda chat=None, **kw: seen.append(kw) or list(ROWS))
    first = wc.sync(now=time.time())
    assert first["window_hours"] == wc.FIRST_WINDOW_HOURS
    assert world.calls[0][2]["arguments"]["refine"] is False
    later = wc.sync(now=time.time() + 2 * 3600)
    assert 2.0 < later["window_hours"] < 4.5 and abs(seen[1]["since_hours"] - later["window_hours"]) < 0.01
    assert wc.sync(refine=True)["status"] == "ok" and world.calls[-1][2]["arguments"]["refine"] is True
    # a long pause is never replayed in full
    far = wc.sync(now=time.time() + 30 * 24 * 3600)
    assert far["window_hours"] == wc.MAX_WINDOW_HOURS


@pytest.mark.parametrize("reply,expected", [
    (None, "hub_down"),
    ((401, {"error": "bad token"}, {}), "hub_refused"),
    ((200, {"ok": False, "status": 404, "error": "Herramienta desconocida."}, {}), "people_outdated"),
    ((200, {"ok": False, "status": None, "error": "not reachable"}, {}), "people_down"),
    ((200, {"ok": False, "status": 400, "error": "boom"}, {}), "people_error"),
])
def test_failures_are_reported_and_the_cursor_does_not_move(monkeypatch, reply, expected):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    monkeypatch.setattr(wa, "messages", lambda chat=None, **kw: list(ROWS))
    monkeypatch.setattr(hoard_hub, "_call", lambda *a, **k: reply if reply is not None else (None, None, {}))
    out = wc.sync()
    assert out["status"] == expected and out["created"] == 0
    assert wc.status()["cursor_ts"] is None and wc.status()["last_status"] == expected
    assert wc.summary_line(out).startswith("Compromisos de WhatsApp:")


def test_a_bridge_that_is_down_is_a_status_not_a_crash(monkeypatch):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    monkeypatch.setattr(hoard_hub, "_call", FakeHub())

    def boom(*a, **k):
        raise wa.BridgeError("bridge not running")
    monkeypatch.setattr(wa, "messages", boom)
    assert wc.sync()["status"] == "bridge_down"


def test_the_scheduled_action_is_quiet_when_off_and_says_what_was_recorded_when_on(monkeypatch, world):
    from src.builtin_actions import TaskNoop
    from src.watchers import WATCH_ACTIONS, WATCH_ACTION_INFO
    assert "whatsapp_commitments" in WATCH_ACTIONS and "whatsapp_commitments" in WATCH_ACTION_INFO
    action = WATCH_ACTIONS["whatsapp_commitments"]
    _cfg(monkeypatch)
    with pytest.raises(TaskNoop):
        asyncio.run(action("admin", prompt=""))
    assert world.calls == []
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    text, ok = asyncio.run(action("admin", prompt=""))
    assert ok and "2 compromisos nuevos" in text and "Le debes a Marta Lozano: Mandar la factura (para el 2026-10-12)" in text
    assert "Te mando la factura" not in text, "the report never repeats the message text"


def test_routes_exist_and_changing_the_config_or_syncing_is_human_only(monkeypatch):
    from routes.whatsapp_routes import setup_whatsapp_routes, CommitmentsConfigBody, CommitmentsSyncBody
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    from fastapi import HTTPException
    router = setup_whatsapp_routes()
    routes = {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}
    for key in [("GET", "/api/whatsapp/commitments"), ("POST", "/api/whatsapp/commitments/config"), ("POST", "/api/whatsapp/commitments/sync")]:
        assert key in routes

    class Req:
        headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        state = type("S", (), {"current_user": "admin", "is_admin": True})()
        client = type("C", (), {"host": "127.0.0.1"})()

    for key, body in [(("POST", "/api/whatsapp/commitments/config"), CommitmentsConfigBody(enabled=True)),
                      (("POST", "/api/whatsapp/commitments/sync"), CommitmentsSyncBody())]:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes[key](body, Req()))
        assert exc.value.status_code == 403, "the agent's internal token can never turn private chats over to another app"


def test_update_config_normalises_and_validates(monkeypatch):
    captured = {}

    def fake_update(patch):
        captured.update(patch)
        return {"settings": {}, "revision": 1}
    monkeypatch.setattr(settings, "update_settings", fake_update)
    _cfg(monkeypatch, **{"whatsapp_commitments_enabled": True, "whatsapp_commitments_chats": ["Marta Lozano"]})
    wc.update_config(enabled=True, chats=[" Marta Lozano ", "marta lozano", "", "+34 600 333 444"])
    assert captured == {"whatsapp_commitments_enabled": True, "whatsapp_commitments_chats": ["Marta Lozano", "+34 600 333 444"]}
    for key in captured:
        settings._validate_patch({key: captured[key]})
