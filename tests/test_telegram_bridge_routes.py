"""The Telegram bridge's surfaces around the poller: its routes, the MCP server,
the settings (schema, secret handling, live apply) and the app wiring."""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from src.chat_bridges import status as status_mod
from src.chat_bridges import telegram_bridge as tb
from src.chat_bridges.store import BridgeStore
from tests.test_telegram_bridge import TOKEN, FakeTelegram, make_config

ROOT = Path(__file__).resolve().parent.parent


# ── routes ───────────────────────────────────────────────────────────────

def _client(monkeypatch):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient
    import core.middleware as mw
    from routes.chat_bridge_routes import setup_chat_bridge_routes

    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_chat_bridge_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def idle_bridge(monkeypatch, tmp_path):
    """The process-wide bridge replaced by one on a temp store and a fixed config."""
    cfg = tb.BridgeConfig(enabled=True, token=TOKEN, allowed=frozenset({"42", "-7"}), mode="agent",
                          public_url="http://studio.test")
    store = BridgeStore(str(tmp_path / "bridge.sqlite3"))
    bridge = tb.TelegramBridge(config_loader=lambda: cfg, store=store)
    monkeypatch.setattr(tb, "_bridge", bridge)
    return bridge


def test_the_routes_are_admin_only(monkeypatch, idle_bridge):
    client = _client(monkeypatch)
    for method, path in (("get", "/api/chat-bridges/telegram/status"), ("post", "/api/chat-bridges/telegram/test")):
        assert getattr(client, method)(path).status_code == 403
        assert getattr(client, method)(path, headers={"x-user": "alice"}).status_code == 403
    assert client.get("/api/chat-bridges/telegram/status", headers={"x-user": "root"}).status_code == 200


def test_status_reports_the_state_without_the_token(monkeypatch, idle_bridge):
    idle_bridge.store.set_session("42", "sess-1", "Ada Lovelace")
    idle_bridge.store.mark_refused("99", "Mallory")
    body = _client(monkeypatch).get("/api/chat-bridges/telegram/status", headers={"x-user": "root"}).json()
    assert body["enabled"] is True and body["configured"] is True and body["running"] is False
    assert body["allowed_chats"] == ["-7", "42"] and body["mode"] == "agent"
    assert body["mapped_sessions"][0]["session_id"] == "sess-1"
    assert body["mapped_sessions"][0]["link"] == "http://studio.test/studio?s=sess-1"
    assert body["refused_chats"][0]["chat_id"] == "99"
    assert body["bot_username"] == "" and body["last_error"] == "" and body["disabled_reason"] == ""
    assert TOKEN not in json.dumps(body) and "token" not in json.dumps(body).lower()


def test_the_test_route_calls_getme_with_the_saved_token(monkeypatch, idle_bridge):
    fake = FakeTelegram()
    try:
        monkeypatch.setattr(tb, "load_config", lambda: make_config(fake))
        client = _client(monkeypatch)
        ok = client.post("/api/chat-bridges/telegram/test", headers={"x-user": "root"}).json()
        assert ok == {"ok": True, "username": "faustus_test_bot", "name": "Faustus Test", "id": 77}
        monkeypatch.setattr(tb, "load_config", lambda: make_config(fake, token="1:WRONG"))
        bad = client.post("/api/chat-bridges/telegram/test", headers={"x-user": "root"}).json()
        assert bad["ok"] is False and "401" in bad["error"] and "WRONG" not in bad["error"]
        monkeypatch.setattr(tb, "load_config", lambda: make_config(fake, token=""))
        assert client.post("/api/chat-bridges/telegram/test", headers={"x-user": "root"}).json() == {
            "ok": False, "error": "No bot token is saved."}
    finally:
        fake.stop()


def test_the_test_route_reports_a_network_failure_without_the_token(monkeypatch, idle_bridge):
    dead = FakeTelegram()
    url = dead.url
    dead.stop()
    monkeypatch.setattr(tb, "load_config", lambda: make_config(dead, api_base=url))
    out = _client(monkeypatch).post("/api/chat-bridges/telegram/test", headers={"x-user": "root"}).json()
    assert out["ok"] is False and out["error"] and TOKEN not in out["error"]


def test_the_route_is_not_part_of_the_api_token_surface():
    from core.authz import api_surface, api_token_allowed
    assert not any("chat-bridges" in key for key in api_surface())
    for method, path in (("GET", "/api/chat-bridges/telegram/status"), ("POST", "/api/chat-bridges/telegram/test")):
        allowed, reason = api_token_allowed(method, path, ["chat", "sessions", "agents:dispatch"])
        assert allowed is False and "not part of the API-token surface" in reason


def test_the_app_registers_the_router_and_the_lifecycle():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "from routes.chat_bridge_routes import setup_chat_bridge_routes" in src
    assert "app.include_router(setup_chat_bridge_routes())" in src
    assert "start_telegram_bridge(_supervisor)" in src
    assert "await stop_telegram_bridge()" in src


# ── the MCP server ───────────────────────────────────────────────────────

pytest.importorskip("mcp")


def _mcp_call(name, arguments=None):
    import mcp_servers.chat_bridges_server as srv
    return asyncio.run(srv.call_tool(name, arguments or {}))


def test_mcp_server_identity_and_tool_surface():
    import mcp_servers.chat_bridges_server as srv
    assert srv.server.name == "chat_bridges"
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["telegram_status"]
    assert tools[0].inputSchema["type"] == "object" and len(tools[0].description) > 60
    assert "Read-only" in tools[0].description


def test_mcp_status_reads_the_snapshot_the_poller_leaves(monkeypatch, tmp_path):
    cfg = tb.BridgeConfig(enabled=True, token=TOKEN, allowed=frozenset({"42"}), public_url="http://studio.test")
    store = BridgeStore(str(tmp_path / "bridge.sqlite3"))
    store.set_session("42", "sess-9", "Ada")
    status_mod.write_snapshot(store, {"running": True, "bot_username": "faustus_test_bot", "bot_name": "Faustus",
                                      "last_error": "", "last_error_at": 0.0, "disabled_reason": "",
                                      "failures_in_a_row": 0})
    monkeypatch.setattr(tb, "load_config", lambda: cfg)
    monkeypatch.setattr(status_mod, "BridgeStore", lambda *a, **k: store)
    body = json.loads(_mcp_call("telegram_status")[0].text)
    assert body["ok"] is True and body["running"] is True and body["bot_username"] == "faustus_test_bot"
    assert body["configured"] is True and body["allowed_chats"] == ["42"]
    assert body["mapped_sessions"][0]["link"] == "http://studio.test/studio?s=sess-9"
    assert TOKEN not in json.dumps(body)


def test_mcp_status_calls_a_silent_poller_not_running(monkeypatch, tmp_path):
    cfg = tb.BridgeConfig(enabled=True, token=TOKEN, allowed=frozenset(), public_url="http://studio.test")
    store = BridgeStore(str(tmp_path / "bridge.sqlite3"))
    status_mod.write_snapshot(store, {"running": True, "bot_username": "b"})
    stale = json.loads(store.get_meta("status"))
    stale["updated"] -= 600
    store.set_meta("status", json.dumps(stale))
    monkeypatch.setattr(tb, "load_config", lambda: cfg)
    monkeypatch.setattr(status_mod, "BridgeStore", lambda *a, **k: store)
    body = json.loads(_mcp_call("telegram_status")[0].text)
    assert body["running"] is False and body["snapshot_age_s"] >= 600


def test_mcp_errors_are_messages_not_exceptions(monkeypatch):
    assert "Unknown tool" in _mcp_call("nope")[0].text

    def broken(*a, **k):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(status_mod, "read_status", broken)
    assert "Error in telegram_status: RuntimeError: store unreadable" in _mcp_call("telegram_status")[0].text


def test_registered_as_a_builtin_server_and_classified_as_local():
    from src import builtin_mcp, effect_tools
    script, name = builtin_mcp._BUILTIN_SERVERS["chat_bridges"]
    assert script == "mcp_servers/chat_bridges_server.py" and (ROOT / script).is_file()
    assert "chat_bridges" in effect_tools._BUILTIN_MCP_SERVERS
    # it has no native twin, so its tool stays visible to the agent's tool retrieval
    assert "chat_bridges" not in builtin_mcp.NATIVE_TWIN_SERVERS


# ── settings ─────────────────────────────────────────────────────────────

def test_the_settings_exist_with_safe_defaults():
    from src.settings import DEFAULT_SETTINGS as d
    assert d["telegram_bridge_enabled"] is False and d["telegram_bot_token"] == ""
    assert d["telegram_allowed_chat_ids"] == [] and d["telegram_agent_mode"] == "agent"
    assert d["telegram_model"] == "" and d["telegram_owner"] == ""
    assert d["telegram_api_base"] == "https://api.telegram.org"


def test_the_schema_has_a_telegram_group_with_a_secret_token():
    from src.agent_settings_schema import GROUPS, build_schema, schema_problems
    assert schema_problems() == []
    group = next(g for g in build_schema()["groups"] if g["id"] == "chat_bridges")
    fields = {f["key"]: f for f in group["fields"]}
    assert list(fields) == ["telegram_bridge_enabled", "telegram_bot_token", "telegram_allowed_chat_ids",
                            "telegram_agent_mode", "telegram_model", "telegram_owner", "telegram_api_base"]
    assert fields["telegram_bot_token"]["type"] == "secret"
    assert fields["telegram_allowed_chat_ids"]["type"] == "list"
    assert [o["value"] for o in fields["telegram_agent_mode"]["options"]] == ["agent", "chat"]
    # off by default and nothing secret in what the schema hands out as defaults
    assert build_schema()["defaults"]["telegram_bot_token"] == ""
    assert any(g["id"] == "chat_bridges" for g in GROUPS)


def test_the_allow_list_accepts_ids_in_either_form():
    from src.agent_settings_schema import coerce_setting_value
    assert coerce_setting_value("telegram_allowed_chat_ids", "42, -1001\n7") == ["42", "-1001", "7"]
    assert coerce_setting_value("telegram_allowed_chat_ids", ["42", " 7 "]) == ["42", "7"]
    assert coerce_setting_value("telegram_bridge_enabled", "true") is True
    with pytest.raises(ValueError):
        coerce_setting_value("telegram_agent_mode", "autopilot")


def test_the_token_is_masked_on_read():
    from src.settings_scrub import is_secret_key, mask_secrets, scrub_settings
    assert is_secret_key("telegram_bot_token")
    shown = mask_secrets({"telegram_bot_token": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "telegram_model": "q"})
    assert shown["telegram_bot_token"].startswith("••••") and "123456789" not in shown["telegram_bot_token"]
    assert scrub_settings({"telegram_bot_token": "123456789:AAA"})["telegram_bot_token"] == ""


@pytest.mark.asyncio
async def test_saving_encrypts_the_token_and_applies_the_bridge(monkeypatch):
    import routes.auth_routes as auth_routes
    from src.secret_storage import decrypt
    from src.settings import DEFAULT_SETTINGS

    store = dict(DEFAULT_SETTINGS)
    applied = []
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(store))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda updated: (store.clear(), store.update(updated)))

    async def fake_apply():
        applied.append(dict(store))
    import src.chat_bridges as cb
    monkeypatch.setattr(cb, "apply_settings", fake_apply)

    class _Auth:
        def get_username_for_token(self, token):
            return "admin"

        def is_admin(self, username):
            return True

    class _Req(SimpleNamespace):
        def __init__(self, body):
            super().__init__(cookies={auth_routes.SESSION_COOKIE: "s"}, _body=body)

        async def json(self):
            return self._body

    router = auth_routes.setup_auth_routes(_Auth())
    post = next(r.endpoint for r in router.routes if r.path == "/api/auth/settings" and "POST" in r.methods)

    reply = await post(_Req({"telegram_bridge_enabled": True, "telegram_bot_token": "123:ABC-secret",
                             "telegram_allowed_chat_ids": "42, 43"}))
    assert store["telegram_bot_token"].startswith("enc:") and "ABC-secret" not in store["telegram_bot_token"]
    assert decrypt(store["telegram_bot_token"]) == "123:ABC-secret"
    assert store["telegram_allowed_chat_ids"] == ["42", "43"] and store["telegram_bridge_enabled"] is True
    assert reply["telegram_bot_token"].startswith("••••") and "ABC-secret" not in json.dumps(reply)
    assert len(applied) == 1                                    # the bridge followed the save

    saved = store["telegram_bot_token"]
    await post(_Req({"telegram_bot_token": reply["telegram_bot_token"]}))      # the mask sent back
    assert store["telegram_bot_token"] == saved
    assert len(applied) == 2                                                    # a save that names a telegram key applies
    await post(_Req({"agent_max_rounds": 9}))                                   # an unrelated save
    assert len(applied) == 2                                                    # does not touch the bridge
    await post(_Req({"telegram_bot_token": ""}))
    assert store["telegram_bot_token"] == ""


# ── applying settings to a live poller ───────────────────────────────────

async def test_apply_settings_starts_restarts_and_stops_the_poller(monkeypatch, tmp_path):
    fake = FakeTelegram()
    try:
        box = {"cfg": make_config(fake, enabled=False)}
        bridge = tb.TelegramBridge(config_loader=lambda: box["cfg"], store=BridgeStore(str(tmp_path / "b.sqlite3")),
                                   poll_timeout=1)
        monkeypatch.setattr(tb, "_bridge", bridge)
        monkeypatch.setattr(tb, "_supervisor", None)
        monkeypatch.setattr(tb, "load_config", lambda: box["cfg"])

        assert (await tb.apply_settings())["running"] is False       # off: nothing started
        box["cfg"] = make_config(fake)
        for _ in range(100):
            if (await tb.apply_settings())["running"]:
                break
            await asyncio.sleep(0.02)
        assert bridge.running
        for _ in range(100):
            if bridge.bot_username:
                break
            await asyncio.sleep(0.02)
        assert bridge.bot_username == "faustus_test_bot"

        first_task = bridge._task
        await tb.apply_settings()                                    # nothing changed: the same poller
        assert bridge._task is first_task

        box["cfg"] = make_config(fake, token="1:WRONG")              # a new token: restarted, and rejected
        await tb.apply_settings()
        for _ in range(100):
            if bridge.disabled_reason:
                break
            await asyncio.sleep(0.02)
        assert "401" in bridge.disabled_reason

        box["cfg"] = make_config(fake)                               # fixed again: it comes back
        await tb.apply_settings()
        for _ in range(100):
            if bridge.running and not bridge.disabled_reason:
                break
            await asyncio.sleep(0.02)
        assert bridge.running and bridge.disabled_reason == ""

        box["cfg"] = make_config(fake, enabled=False)
        assert (await tb.apply_settings())["running"] is False
    finally:
        await bridge.stop()
        fake.stop()


def test_start_only_when_enabled_and_a_token_is_saved(monkeypatch, tmp_path):
    started = []
    fake_bridge = SimpleNamespace(start=lambda supervisor=None: started.append(supervisor) or True)
    monkeypatch.setattr(tb, "_bridge", fake_bridge)
    monkeypatch.setattr(tb, "_supervisor", None)
    sup = object()
    for cfg, expected in ((tb.BridgeConfig(), False), (tb.BridgeConfig(enabled=True), False),
                          (tb.BridgeConfig(token="1:a"), False), (tb.BridgeConfig(enabled=True, token="1:a"), True)):
        monkeypatch.setattr(tb, "load_config", lambda cfg=cfg: cfg)
        assert tb.start_telegram_bridge(sup) is expected
    assert started == [sup]


def test_a_broken_settings_file_never_stops_the_app_starting(monkeypatch):
    def boom():
        raise RuntimeError("settings unreadable")
    monkeypatch.setattr(tb, "load_config", boom)
    assert tb.start_telegram_bridge() is False


async def test_the_store_survives_a_second_open(tmp_path):
    path = str(tmp_path / "s.sqlite3")
    BridgeStore(path).set_session("1", "sess", "t")
    again = BridgeStore(path)
    assert again.get_session("1")["session_id"] == "sess"
    again.mark_refused("2", "x")
    assert again.was_refused("2") and not again.was_refused("3")
    again.forget_refused("2")
    assert not again.was_refused("2")
