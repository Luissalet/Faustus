"""src/chat_bridges/xchat_bridge.py: Faustus in your X self-chat (XDK and API faked)."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from src.chat_bridges import turn as turn_mod
from src.chat_bridges import xchat_bridge as xb
from src.chat_bridges.store import BridgeStore

ME = "42"
CONV = "42-42"


class FakeChat:
    """The XDK calls the bridge makes: 'encrypted' events are just their text, prefixed."""
    def __init__(self):
        self.signing = None
        self.counter = 0

    def import_keys(self, blob, version):
        self.blob = blob

    def set_identity(self, uid, version):
        self.uid = uid

    def set_cache_keys(self, on):
        pass

    def set_signing_keys(self, keys):
        self.signing = keys

    def decrypt_events(self, events, _):
        return {"conversation_keys": {"keys": {"1": b"k"}}, "messages": []}

    def decrypt_event(self, b64, keys, _):
        sender, _, text = base64.b64decode(b64).decode().partition("|")
        return {"type": "Message", "sender_id": sender, "content": {"text": text}}

    def encrypt_message(self, conv, text):
        self.counter += 1
        return SimpleNamespace(message_id=f"out{self.counter}", encrypted_content="CIPHER:" + text, encoded_event_signature="sig")

    def generate_keypairs(self):
        pk = SimpleNamespace(public_key="P", signing_public_key="S", identity_public_key_signature="I",
                             signing_public_key_signature="Z", registration_method="m")
        return SimpleNamespace(version=3, public_key=pk, generate_version=False)

    def export_keys(self):
        return b"privatekeys"


def ev(eid, sender, text):
    return {"id": eid, "sender_id": sender, "encoded_event": base64.b64encode(f"{sender}|{text}".encode()).decode()}


class FakeApi:
    def __init__(self, events):
        self.events_list = events          # newest first, as X returns them
        self.sent = []
        self.posted = []

    async def events(self, conv, token=None):
        assert conv == CONV
        return {"data": list(self.events_list), "meta": {}}

    async def public_keys(self, uid):
        return [{"public_key_version": "1", "signing_public_key": "s", "public_key": "p"}]

    async def send(self, conv, body):
        self.sent.append(body)
        return {"data": {"event_id": "e-" + body["message_id"]}}

    async def me(self):
        return {"id": ME, "username": "luis"}

    async def call(self, method, path, json_body=None, params=None):
        self.posted.append((method, path, json_body))
        return {}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    cfg = xb.XChatConfig(enabled=True, access_token="t", private_keys=base64.b64encode(b"k").decode(), user_id=ME)
    turns = []

    async def run_turn(req):
        turns.append(req)
        return turn_mod.TurnResult(text=f"respuesta a {req.text}")

    monkeypatch.setattr(turn_mod, "resolve_owner", lambda c="": "luis")
    monkeypatch.setattr(turn_mod, "session_exists", lambda sid, owner="": True)
    monkeypatch.setattr(turn_mod, "create_session", lambda owner, name, model="": "sess-x")
    monkeypatch.setattr(xb, "xdk_available", lambda: True)
    api = FakeApi([])
    chat = FakeChat()
    crypto = xb.XCrypto(chat)
    crypto.unlock(cfg.private_keys, "1", ME)
    b = xb.XChatBridge(config_loader=lambda: cfg, store=BridgeStore(str(tmp_path / "x.sqlite3")),
                       api=api, crypto=crypto, run_turn=run_turn)
    return b, api, turns


def test_first_poll_learns_the_backlog_without_answering(bridge):
    b, api, turns = bridge
    api.events_list = [ev("102", ME, "viejo 2"), ev("101", ME, "viejo 1")]
    assert asyncio.run(b.poll_once()) == 0
    assert turns == [] and b.store.get_meta("cursor") == "102"


def test_new_self_messages_become_turns_and_replies_are_not_read_back(bridge):
    b, api, turns = bridge
    api.events_list = [ev("100", ME, "antes")]
    asyncio.run(b.poll_once())
    api.events_list = [ev("103", ME, "¿qué tengo hoy?"), ev("102", "77", "alguien más"), ev("100", ME, "antes")]
    assert asyncio.run(b.poll_once()) == 1
    assert [t.text for t in turns] == ["¿qué tengo hoy?"] and turns[0].source == "xchat"
    assert api.sent[0]["encoded_message_create_event"] == "CIPHER:respuesta a ¿qué tengo hoy?"
    assert b.store.get_session(CONV)["session_id"] == "sess-x"
    # The reply comes back as an event from the same account: never a new prompt.
    api.events_list = [{**ev("out1", ME, "respuesta a ¿qué tengo hoy?")}, ev("103", ME, "¿qué tengo hoy?")]
    assert asyncio.run(b.poll_once()) == 0
    assert len(turns) == 1


def test_not_configured_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(xb, "xdk_available", lambda: False)
    b = xb.XChatBridge(config_loader=lambda: xb.XChatConfig(enabled=True), store=BridgeStore(str(tmp_path / "y.sqlite3")))
    with pytest.raises(xb.XChatError, match="token"):
        asyncio.run(b.poll_once())
    st = b.status()
    assert st["configured"] is False and any("chat_xdk" in m for m in st["missing"])
    assert "dm.read" in st["scopes_needed"]


def test_setup_saves_the_identity_before_registering_it():
    saved = []
    api = FakeApi([])
    cfg = xb.XChatConfig(access_token="t")
    out = asyncio.run(xb.setup_identity(cfg, api=api, crypto=xb.XCrypto(FakeChat()), persist=saved.append))
    assert out["registered"] and out["user_id"] == ME and out["key_version"] == "3"
    assert saved[0]["xchat_private_keys"] == base64.b64encode(b"privatekeys").decode()
    assert api.posted == [("POST", f"/2/users/{ME}/public_keys", api.posted[0][2])]
    assert api.posted[0][2]["public_key"]["signing_public_key"] == "S"
    # With an identity already saved, nothing is registered again.
    cfg2 = xb.XChatConfig(access_token="t", private_keys="abc")
    out2 = asyncio.run(xb.setup_identity(cfg2, api=FakeApi([]), persist=saved.append))
    assert out2["registered"] is False


def test_self_chat_id():
    assert xb.self_chat_id("42") == "42-42"
