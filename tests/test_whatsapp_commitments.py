"""Promises found in WhatsApp chats, offered to People's Hoard (src/whatsapp_commitments.py).

Private chats: the feature is OFF by default, only a person can turn it on (never the agent), needs the chats to be named, never sends
groups, channels, status or hidden ids without a name, and sends message text only to People's tool through the hub's proxy (never to
the event bus, never into the state file or a report). Nothing is lost to the bridge's page size, and late arrivals are still seen.
Invented people and numbers only."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import middleware
from src import settings
from src import whatsapp_bridge as wa
from src import whatsapp_commitments as wc
from src import hoard_hub

MARTA = "34600111222@s.whatsapp.net"
PEDRO = "34600333444@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
NEWS = "120363999999999999@newsletter"
LID_NAMED = "98765432101234@lid"
LID_BARE = "11223344556677@lid"


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


class FakeBridge:
    """The bridge's `/chats` and `/messages` as the real one answers them: the newest `limit` messages that pass the filter, in
    arrival order; `until` is honoured only when `paging` is True (an older bridge ignores it)."""

    def __init__(self, rows, paging=True, clock=time.time):
        self.rows = list(rows)
        self.paging = paging
        self.clock = clock
        self.message_calls = []

    def chats(self, limit=50):
        seen = {}
        for r in self.rows:
            c = seen.setdefault(r["chat"], {"jid": r["chat"], "name": r.get("chat_name") or "", "is_group": r["chat"].endswith("@g.us"), "last_ts": 0})
            c["last_ts"] = max(c["last_ts"], r["ts"])
        return list(seen.values())[:limit]

    def messages(self, chat=None, *, since_hours=24, limit=200, unread_only=False, until_ts=None):
        self.message_calls.append({"chat": chat, "limit": limit, "until_ts": until_ts})
        since = self.clock() - since_hours * 3600
        out = [r for r in self.rows if (not chat or r["chat"] == chat) and r["ts"] >= since
               and (not self.paging or not until_ts or r["ts"] <= until_ts)]
        return out[-limit:]

    def install(self, monkeypatch):
        monkeypatch.setattr(wa, "chats", self.chats)
        monkeypatch.setattr(wa, "messages", self.messages)
        return self


class FakeHub:
    """Stands in for hoard_hub._call: records every call and answers like the hub's tool proxy."""

    def __init__(self, reply=None, literal_text=""):
        self.calls = []
        self.reply = reply
        self.literal_text = literal_text

    def sent(self):
        return [m for _m, _p, b in self.calls for m in (b or {}).get("arguments", {}).get("messages", [])]

    def __call__(self, method, path, body=None, *, timeout=3.0):
        self.calls.append((method, path, body))
        if self.reply is not None:
            return self.reply
        n = len((body or {}).get("arguments", {}).get("messages", []))
        return 200, {"ok": True, "app": "people", "tool": "commitments_from_messages", "status": 200,
                     "result": {"received": n, "created": n, "queued": 0, "duplicates": 0, "linked": n, "unlinked": 0,
                                "skipped": {"seen": 0}, "commitments": [
                                    {"id": "c1", "direction": "i_owe", "text": self.literal_text or "Mandar la factura",
                                     "action": "Mandar algo", "due": "2026-10-12", "person_name": "Marta Lozano"}]}}, {}


@pytest.fixture
def world(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    FakeBridge(ROWS).install(monkeypatch)
    return hub


def test_it_is_off_by_default_and_touches_nothing(monkeypatch):
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_enabled"] is False
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_all_chats"] is False
    assert settings.DEFAULT_SETTINGS["whatsapp_commitments_chats"] == []
    _cfg(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    monkeypatch.setattr(wa, "chats", lambda *a, **k: pytest.fail("the bridge must not be read while the feature is off"))
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


def test_channels_status_broadcasts_and_nameless_hidden_ids_never_reach_people_even_with_all_chats(monkeypatch):
    rows = ROWS[:3] + [
        _row("n1", NEWS, "Noticias del barrio", "Te mando el enlace mañana"),
        _row("s1", "status@broadcast", "", "Te lo mando el lunes"),
        _row("s2", "120363000@broadcast", "Lista", "Te lo mando el lunes"),
        _row("l1", LID_BARE, "", "Te lo mando el lunes"),
        _row("l2", LID_BARE, "11223344556677", "Te lo mando el lunes"),
        _row("l3", LID_NAMED, "Carmen Vega", "Te devuelvo el cargador el viernes"),
    ]
    batch, skipped = wc.to_batch(rows, {"all_chats": True, "chats": []})
    assert {m["id"] for m in batch} == {"a1", "a2", "b1", "l3"}
    assert skipped["not_chat"] == 5 and skipped["group"] == 0
    lid = next(m for m in batch if m["id"] == "l3")
    assert "phone" not in lid and lid["name_unverified"] is True and lid["contact_name"] == "Carmen Vega"
    assert all("name_unverified" not in m for m in batch if m["id"] != "l3")
    # naming a channel or a status list does not enable it either
    named = {"all_chats": False, "chats": [NEWS, "status@broadcast", LID_BARE, "Noticias del barrio"]}
    assert wc.to_batch(rows, named)[0] == []


def test_sync_sends_message_text_only_to_peoples_tool_through_the_hub(monkeypatch, world, tmp_path):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    out = wc.sync()
    assert out["status"] == "ok" and out["offered"] == 2 and out["created"] == 2 and out["chats"] == 1
    assert out["read"] == 5, "only Marta's chat is read from the bridge"
    assert len(world.calls) == 1
    method, path, body = world.calls[0]
    assert (method, path) == ("POST", "/api/apps/people/call")
    assert body["tool"] == "commitments_from_messages" and body["arguments"]["refine"] is False
    sent = [m["text"] for m in body["arguments"]["messages"]]
    assert sent == ["Te mando la factura el lunes", "Te debo 30 euros de la cena"]
    assert "me debes el libro" not in json.dumps(body), "Pedro's chat was not enabled"
    # nothing was published anywhere else, and the state file holds counts and ids, not text
    assert all(p.startswith("/api/apps/people/call") for _m, p, _b in world.calls)
    state_files = list((tmp_path / "watchers").glob("*.json"))
    assert state_files
    blob = "".join(f.read_text(encoding="utf-8") for f in state_files)
    assert "factura" not in blob and "cena" not in blob and "last_counts" in blob
    assert wc.status()["last_status"] == "ok" and wc.status()["last_counts"]["offered"] == 2
    # what the sync reports names the commitment by its action, never by the sentence
    assert all("text" not in c for c in out["commitments"]) and out["commitments"][0]["action"] == "Mandar algo"


def test_a_busy_group_or_other_chats_cannot_push_the_enabled_chats_out_of_the_page(monkeypatch):
    """Repro of the review: 200 messages of the enabled chat in the last 20 h, then 450 newer group messages. The old single query
    for all chats returned the newest 500 of everything and 150 of the enabled chat were never seen."""
    now = time.time()
    rows = [_row(f"m{i}", MARTA, "Marta Lozano", f"Te mando el informe {i} el lunes", ago=20 * 3600 - i * 60) for i in range(200)]
    rows += [_row(f"g{i}", GROUP, "Piso compartido", f"Yo llevo la tarta {i}", ago=3600 - i) for i in range(450)]
    rows += [_row(f"p{i}", PEDRO, "Pedro Ruiz", f"me debes {i} euros", ago=1800 - i) for i in range(300)]
    bridge = FakeBridge(rows).install(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    out = wc.sync(now=now)
    assert out["status"] == "ok" and out["read"] == 200 and out["offered"] == 200 and out["truncated"] == 0
    assert {m["id"] for m in hub.sent()} == {f"m{i}" for i in range(200)}
    assert {c["chat"] for c in bridge.message_calls} == {MARTA}, "only the enabled chat is asked for"


def test_a_chat_with_more_messages_than_a_page_is_paged_back_with_until(monkeypatch):
    rows = [_row(f"m{i}", MARTA, "Marta Lozano", f"Te mando el informe {i} el lunes", ago=3 * 3600 - i) for i in range(1300)]
    bridge = FakeBridge(rows).install(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    out = wc.sync()
    assert out["status"] == "ok" and out["offered"] == 1300 and out["truncated"] == 0
    assert len(bridge.message_calls) >= 3 and bridge.message_calls[0]["until_ts"] is None and bridge.message_calls[1]["until_ts"]
    assert len({m["id"] for m in hub.sent()}) == 1300
    assert max(len((b or {}).get("arguments", {}).get("messages", [])) for _m, _p, b in hub.calls) <= wc.BATCH_SIZE
    assert wc.status()["cursor_ts"] is not None


def test_a_bridge_that_cannot_page_is_reported_and_the_run_is_not_marked_complete(monkeypatch):
    rows = [_row(f"m{i}", MARTA, "Marta Lozano", f"Te mando el informe {i} el lunes", ago=3 * 3600 - i) for i in range(700)]
    FakeBridge(rows, paging=False).install(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    out = wc.sync()
    assert out["status"] == "ok" and out["truncated"] == 1 and out["offered"] == wc.PAGE_LIMIT
    assert wc.status()["cursor_ts"] is None, "the time of the run does not advance while a chat was cut"
    assert "se reintentará" in wc.summary_line(out)
    again = wc.sync()
    assert again["offered"] == 0 and again["already_offered"] == wc.PAGE_LIMIT, "what was read is not offered twice"


def test_a_message_that_arrives_late_with_an_old_timestamp_is_still_offered(monkeypatch):
    t0 = time.time()
    clock = {"now": t0}
    rows = [_row("a1", MARTA, "Marta Lozano", "Te mando la factura el lunes", ago=600)]
    bridge = FakeBridge(rows, clock=lambda: clock["now"]).install(monkeypatch)
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    assert wc.sync(now=t0)["offered"] == 1
    # three hours later a history sync delivers a message written ten hours before the last run
    clock["now"] = t0 + 3 * 3600
    bridge.rows.append(_row("late1", MARTA, "Marta Lozano", "Te devuelvo el libro el viernes", ago=0, ts=t0 - 10 * 3600))
    bridge.rows.append(_row("a2", MARTA, "Marta Lozano", "Te pago mañana", ago=0, ts=t0 + 3600))
    second = wc.sync(now=t0 + 3 * 3600)
    assert second["offered"] == 2 and second["already_offered"] == 1
    assert {m["id"] for m in hub.sent()} == {"a1", "late1", "a2"}
    assert [m["id"] for m in hub.sent()].count("a1") == 1, "the overlap does not repeat a message"
    third = wc.sync(now=t0 + 4 * 3600)
    assert third["offered"] == 0 and len(hub.calls) == 2


def test_the_window_reaches_back_before_the_last_run_and_is_capped(monkeypatch, world):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    t0 = time.time()
    first = wc.sync(now=t0)
    assert first["window_hours"] == wc.FIRST_WINDOW_HOURS
    assert world.calls[0][2]["arguments"]["refine"] is False
    later = wc.sync(now=t0 + 2 * 3600)
    assert later["window_hours"] == pytest.approx(2 + wc.OVERLAP_HOURS, abs=0.01)
    FakeBridge(ROWS + [_row("r1", MARTA, "Marta Lozano", "Te pago mañana", ago=0, ts=t0 + 7000)]).install(monkeypatch)
    assert wc.sync(refine=True, now=t0 + 2 * 3600)["status"] == "ok"
    assert world.calls[-1][2]["arguments"]["refine"] is True
    # a long pause is never replayed in full
    far = wc.sync(now=t0 + 60 * 24 * 3600)
    assert far["window_hours"] == wc.MAX_WINDOW_HOURS


def test_with_the_model_pass_unread_messages_are_offered_again(monkeypatch):
    FakeBridge(ROWS).install(monkeypatch)
    replies = iter([(200, {"ok": True, "result": {"created": 0, "queued": 0, "skipped": {"seen": 0}, "retry": 2, "commitments": []}}, {}),
                    (200, {"ok": True, "result": {"created": 0, "queued": 1, "skipped": {"seen": 0}, "retry": 0, "commitments": []}}, {})])
    hub = FakeHub()
    seen = []

    def call(method, path, body=None, *, timeout=3.0):
        hub.calls.append((method, path, body))
        seen.append(len(body["arguments"]["messages"]))
        return next(replies)
    monkeypatch.setattr(hoard_hub, "_call", call)
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    assert wc.sync(refine=True)["offered"] == 2
    assert wc.sync(refine=True)["offered"] == 2, "People kept them unread (hub down): they come back"
    assert wc.sync(refine=True)["offered"] == 0
    assert seen == [2, 2]


@pytest.mark.parametrize("reply,expected", [
    (None, "hub_down"),
    ((401, {"error": "bad token"}, {}), "hub_refused"),
    ((200, {"ok": False, "status": 404, "error": "Herramienta desconocida."}, {}), "people_outdated"),
    ((200, {"ok": False, "status": None, "error": "not reachable"}, {}), "people_down"),
    ((200, {"ok": False, "status": 400, "error": "boom"}, {}), "people_error"),
])
def test_failures_are_reported_and_nothing_is_marked_as_offered(monkeypatch, reply, expected):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    FakeBridge(ROWS).install(monkeypatch)
    monkeypatch.setattr(hoard_hub, "_call", lambda *a, **k: reply if reply is not None else (None, None, {}))
    out = wc.sync()
    assert out["status"] == expected and out["created"] == 0
    assert wc.status()["cursor_ts"] is None and wc.status()["last_status"] == expected
    assert wc.summary_line(out).startswith("Compromisos de WhatsApp:")
    # the same messages are offered again once People answers
    hub = FakeHub()
    monkeypatch.setattr(hoard_hub, "_call", hub)
    assert wc.sync()["offered"] == 3


def test_a_bridge_that_is_down_is_a_status_not_a_crash(monkeypatch):
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_all_chats=True)
    monkeypatch.setattr(hoard_hub, "_call", FakeHub())

    def boom(*a, **k):
        raise wa.BridgeError("bridge not running")
    monkeypatch.setattr(wa, "chats", boom)
    monkeypatch.setattr(wa, "messages", boom)
    assert wc.sync()["status"] == "bridge_down"
    # one chat the bridge refuses does not hide the others, and the run is not marked complete
    FakeBridge(ROWS).install(monkeypatch)
    real = wa.messages

    def picky(chat=None, **kw):
        if chat == PEDRO:
            raise wa.BridgeError("409 ambiguous")
        return real(chat, **kw)
    monkeypatch.setattr(wa, "messages", picky)
    out = wc.sync()
    assert out["status"] == "ok" and out["chat_errors"] == 1 and out["offered"] == 2
    assert wc.status()["cursor_ts"] is None


def test_the_scheduled_action_is_quiet_when_off_and_reports_without_quoting_anyone(monkeypatch):
    from src.builtin_actions import TaskNoop
    from src.watchers import WATCH_ACTIONS, WATCH_ACTION_INFO
    assert "whatsapp_commitments" in WATCH_ACTIONS and "whatsapp_commitments" in WATCH_ACTION_INFO
    action = WATCH_ACTIONS["whatsapp_commitments"]
    sentinel = "ZXQ-SECRETO-17 te lo mando el lunes"
    hub = FakeHub(literal_text=sentinel)
    monkeypatch.setattr(hoard_hub, "_call", hub)
    FakeBridge(ROWS + [_row("z1", MARTA, "Marta Lozano", sentinel)]).install(monkeypatch)
    _cfg(monkeypatch)
    with pytest.raises(TaskNoop):
        asyncio.run(action("admin", prompt=""))
    assert hub.calls == []
    _cfg(monkeypatch, whatsapp_commitments_enabled=True, whatsapp_commitments_chats=["Marta Lozano"])
    text, ok = asyncio.run(action("admin", prompt=""))
    assert ok and "3 compromisos nuevos" in text and "Le debes a Marta Lozano: Mandar algo (para el 2026-10-12)" in text
    for word in ("ZXQ", "SECRETO", "factura", "cena", "Te mando", "te lo mando"):
        assert word not in text, f"the report must not repeat the message ({word})"
    # nothing new the second time: quiet
    with pytest.raises(TaskNoop):
        asyncio.run(action("admin", prompt=""))


def test_routes_exist_and_changing_the_config_or_syncing_is_human_only(monkeypatch):
    from routes.whatsapp_routes import setup_whatsapp_routes, CommitmentsConfigBody, CommitmentsSyncBody
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
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


# --- the agent cannot switch the feature on by any other road ------------------

KEYS = sorted(settings.HUMAN_ONLY_SETTINGS)


def test_the_three_keys_are_the_human_only_set():
    assert KEYS == ["whatsapp_commitments_all_chats", "whatsapp_commitments_chats", "whatsapp_commitments_enabled"]
    assert set(KEYS) <= set(settings.DEFAULT_SETTINGS)


def test_manage_settings_cannot_set_reset_or_delete_them(monkeypatch):
    from src.agent_tools.admin_tools import do_manage_settings
    store = {}
    monkeypatch.setattr(settings, "load_settings", lambda: dict(store))
    monkeypatch.setattr(settings, "save_settings", lambda s: store.update(s))
    for key in KEYS:
        value = [] if key.endswith("chats") else True
        for action in ("set", "reset", "delete"):
            out = asyncio.run(do_manage_settings(json.dumps({"action": action, "key": key, "value": value})))
            assert out.get("exit_code") == 1 and "person" in out["error"], (key, action, out)
    # the review's repro, verbatim
    out = asyncio.run(do_manage_settings('{"action":"set","key":"whatsapp_commitments_enabled","value":true}'))
    assert out.get("exit_code") == 1 and "Set " not in out.get("response", "")
    assert store == {}
    ok = asyncio.run(do_manage_settings(json.dumps({"action": "set", "key": "agent_input_token_budget", "value": 8000})))
    assert ok.get("exit_code") == 0 and store["agent_input_token_budget"] == 8000


@pytest.fixture()
def settings_post(monkeypatch):
    from unittest.mock import MagicMock
    import routes.auth_routes as auth_routes
    stored = dict(settings.DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(stored))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda new: stored.clear() or stored.update(new))
    auth_manager = MagicMock()
    auth_manager.get_username_for_token.return_value = "luis"
    auth_manager.is_admin.return_value = True
    router = auth_routes.setup_auth_routes(auth_manager)
    post = next(r.endpoint for r in router.routes
                if getattr(r, "path", "") == "/api/auth/settings" and "POST" in getattr(r, "methods", ()))

    async def call(body, headers=None):
        async def js():
            return body
        request = SimpleNamespace(cookies={}, headers=headers or {}, state=SimpleNamespace(),
                                  client=SimpleNamespace(host="127.0.0.1"), json=js)
        return await post(request)
    return SimpleNamespace(stored=stored, call=lambda b, h=None: asyncio.run(call(b, h)))


def test_the_settings_api_refuses_them_from_the_agent_channel_but_not_from_a_person(settings_post):
    tool = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}
    for body in ({"whatsapp_commitments_enabled": True}, {"whatsapp_commitments_all_chats": True},
                 {"whatsapp_commitments_chats": ["Marta Lozano"]}):
        with pytest.raises(HTTPException) as err:
            settings_post.call(body, tool)
        assert err.value.status_code == 403
    assert settings_post.stored["whatsapp_commitments_enabled"] is False
    assert settings_post.stored["whatsapp_commitments_chats"] == []
    # a whole-form echo of the unchanged values, and other settings, still pass
    settings_post.call({"whatsapp_commitments_enabled": False, "whatsapp_commitments_chats": [], "agent_max_rounds": 7}, tool)
    # a person (no internal token) can
    settings_post.call({"whatsapp_commitments_enabled": True})
    assert settings_post.stored["whatsapp_commitments_enabled"] is True


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
