"""src/conversation_bridge.py: Faustus + WhatsApp conversations for an outside MCP client."""
import asyncio
import json

import pytest

from src.conversation_bridge import Bridge, BridgeError, split_key

SESSIONS = [
    {"id": "s1", "name": "Plan semanal", "last_message_at": "2026-11-16T10:00:00", "message_count": 4, "model": "m"},
    {"id": "s2", "name": "Viejo", "last_message_at": "2026-11-01T10:00:00", "message_count": 2},
]
HISTORY = {"s1": {"history": [
    {"role": "user", "content": "hola", "metadata": {"timestamp": "2026-11-16T09:59:00Z"}},
    {"role": "assistant", "content": [{"type": "text", "text": "buenas"}], "metadata": {"timestamp": "2026-11-16T10:00:00Z"}},
]}}
CHATS = {"chats": [{"jid": "34600@s.whatsapp.net", "name": "Ana", "last_ts": 1_794_825_000, "unread": 2, "last_text": "¿vienes?"}]}
WA_MSGS = {"messages": [
    {"id": "w1", "chat": "34600@s.whatsapp.net", "ts": 1_794_824_000, "from_name": "Ana", "kind": "text", "text": "hola"},
    {"id": "w2", "chat": "34600@s.whatsapp.net", "ts": 1_794_825_000, "from_me": True, "kind": "text", "text": "voy"},
]}


class FakeApi:
    def __init__(self, wa_up=True):
        self.calls = []
        self.wa_up = wa_up

    def __call__(self, method, path, params=None, json=None):
        self.calls.append((method, path, params, json))
        if path == "/api/sessions":
            return 200, SESSIONS
        if path.startswith("/api/history/"):
            return 200, HISTORY.get(path.rsplit("/", 1)[1], {"history": []})
        if path.startswith("/api/whatsapp/") and not self.wa_up:
            return 503, {"detail": "bridge not running"}
        if path == "/api/whatsapp/chats":
            return 200, CHATS
        if path == "/api/whatsapp/messages":
            return 200, WA_MSGS
        if path == "/api/agent-mailbox/send":
            return 200, {"ok": True, "wake": {"woken": True}}
        if path == "/api/whatsapp/send":
            return 200, {"id": "sent1"}
        return 404, {"detail": "nope"}


def test_split_key_accepts_prefixed_and_bare_ids():
    assert split_key("faustus:abc") == ("faustus", "abc")
    assert split_key("whatsapp:1@g.us") == ("whatsapp", "1@g.us")
    assert split_key("34600@s.whatsapp.net") == ("whatsapp", "34600@s.whatsapp.net")
    assert split_key("abc") == ("faustus", "abc")
    with pytest.raises(BridgeError):
        split_key("")


def test_list_merges_sources_newest_first_and_filters():
    b = Bridge(FakeApi())
    out = b.conversations()
    keys = [c["key"] for c in out["conversations"]]
    assert keys == ["whatsapp:34600@s.whatsapp.net", "faustus:s1", "faustus:s2"]
    assert out["conversations"][0]["unread"] == 2
    assert [c["key"] for c in b.conversations(query="plan")["conversations"]] == ["faustus:s1"]
    assert [c["source"] for c in b.conversations(source="faustus")["conversations"]] == ["faustus", "faustus"]


def test_whatsapp_down_is_reported_not_fatal():
    out = Bridge(FakeApi(wa_up=False)).conversations()
    assert {c["source"] for c in out["conversations"]} == {"faustus"}
    assert "whatsapp" in out["unavailable"]


def test_read_both_kinds():
    b = Bridge(FakeApi())
    f = b.read("faustus:s1")["messages"]
    assert [(m["role"], m["text"]) for m in f] == [("user", "hola"), ("assistant", "buenas")]
    w = b.read("whatsapp:34600@s.whatsapp.net")["messages"]
    assert [(m["role"], m["text"]) for m in w] == [("contact", "hola"), ("me", "voy")]
    assert w[0]["from"] == "Ana"


def test_poll_returns_only_messages_after_the_cursor():
    b = Bridge(FakeApi())
    cursor = 1_794_824_500_000            # after w1 and after s1's messages, before w2
    out = b.poll(cursor)
    assert [(e["conversation"], e["text"]) for e in out["events"]] == [("whatsapp:34600@s.whatsapp.net", "voy")]
    assert out["cursor"] == 1_794_825_000_000
    assert b.poll(out["cursor"])["events"] == []
    earlier = b.poll(1_794_823_000_000)   # before s1's two messages
    assert [e["text"] for e in earlier["events"]] == ["hola", "buenas", "hola", "voy"]


def test_wait_returns_on_timeout_without_events():
    clock = [1000.0]
    b = Bridge(FakeApi(), now=lambda: clock[0])

    def sleep(s):
        clock[0] += s

    out = b.wait(cursor=10**13, timeout_s=5, sleep=sleep)
    assert out["events"] == [] and out["timed_out"] is True


def test_send_routes_to_mailbox_or_whatsapp():
    api = FakeApi()
    b = Bridge(api)
    r = b.send("faustus:s1", "revisa esto")
    assert r["ok"] and api.calls[-1][1] == "/api/agent-mailbox/send"
    assert api.calls[-1][3] == {"to": "s1", "body": "revisa esto", "urgent": True, "sender": "mcp"}
    b.send("whatsapp:34600@s.whatsapp.net", "ya voy")
    assert api.calls[-1][1] == "/api/whatsapp/send" and api.calls[-1][3]["to"] == "34600@s.whatsapp.net"
    with pytest.raises(BridgeError):
        b.send("faustus:s1", "  ")


def test_http_error_becomes_bridge_error():
    def api(method, path, params=None, json=None):
        return 404, {"detail": "Session 'x' not found"}
    with pytest.raises(BridgeError, match="not found"):
        Bridge(api).read("faustus:x")


pytest.importorskip("mcp")


def test_mcp_server_lists_six_tools_and_reports_errors(monkeypatch):
    import mcp_servers.conversations_server as srv
    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert names == ["conversations_list", "conversation_get", "messages_read", "events_poll", "events_wait", "messages_send"]
    monkeypatch.setattr(srv, "_bridge", Bridge(FakeApi()))
    res = asyncio.run(srv.call_tool("conversations_list", {"source": "faustus"}))
    assert not res.isError and json.loads(res.content[0].text)["conversations"][0]["key"] == "faustus:s1"
    bad = asyncio.run(srv.call_tool("messages_send", {"key": "faustus:s1", "text": ""}))
    assert bad.isError


def test_client_config_names_the_server_script():
    from routes.conversations_mcp_routes import client_config
    cfg = client_config("http://127.0.0.1:7000/")
    entry = cfg["json"]["mcpServers"]["faustus-conversations"]
    assert entry["args"][0].endswith("mcp_servers/conversations_server.py")
    assert entry["env"]["FAUSTUS_URL"] == "http://127.0.0.1:7000"
    assert "[mcp_servers.faustus-conversations]" in cfg["codex_toml"]
