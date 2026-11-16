"""src/chat_bridges/xchat_bridge.py — talk to Faustus in your X direct-message chat with yourself.

X's direct messages (X Chat) are end-to-end encrypted: bodies are encrypted and
decrypted locally with X's Chat XDK (the ``chat_xdk`` Python package, a native
binding), and X only routes ciphertext. This bridge works like WhatsApp's
"message yourself" chat: you write in your own self-chat on X, Faustus runs a
normal turn in a Faustus conversation (src/chat_bridges/turn.py) and answers in
the same chat. Only the self-chat is read; your other conversations are never
treated as prompts.

What it needs, all of it the owner's (Ajustes › Chat de X):

* an X developer app with OAuth 2.0 user context and X Chat API access on its
  plan, and a user access token for your account with ``dm.read dm.write
  users.read tweet.read`` (+ ``offline.access`` for a refresh token, which also
  needs the app's client id);
* ``chat_xdk`` installed in Faustus's Python;
* an encryption identity for Faustus on the account: ``POST /api/chat-bridges/xchat/setup``
  generates it with the XDK, registers its public keys on the account
  (``POST /2/users/{id}/public_keys``, a few writes per day) and keeps the
  private-key blob encrypted in the settings.

Then ``xchat_bridge_enabled`` starts the poller (every ``xchat_poll_seconds``).
Each message you type in the self-chat becomes a turn; the reply is encrypted,
signed and posted back, and its id is remembered so it is never read as a prompt.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import turn as turn_mod
from .store import BridgeStore, default_path

logger = logging.getLogger(__name__)

API_BASE = "https://api.x.com"
EVENT_FIELDS = "conversation_id,created_at,encoded_event,id,sender_id"
SCOPES = "dm.read dm.write users.read tweet.read offline.access"
MIN_POLL_S = 2.0
MAX_SENT_IDS = 500


class XChatError(RuntimeError):
    pass


# ── settings ────────────────────────────────────────────────────────────

def _dec(raw: Any) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    try:
        from src.secret_storage import decrypt
        return str(decrypt(raw) or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable credential is no credential
        return ""


@dataclass
class XChatConfig:
    enabled: bool = False
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    user_id: str = ""
    private_keys: str = ""
    key_version: str = "1"
    poll_s: float = 10.0
    mode: str = "agent"
    model: str = ""
    owner: str = ""
    api_base: str = API_BASE

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.access_token and self.private_keys)

    def missing(self) -> List[str]:
        out = []
        if not self.access_token:
            out.append("xchat_access_token (token de usuario OAuth 2.0 de tu app de X)")
        if not self.private_keys:
            out.append("identidad de cifrado (POST /api/chat-bridges/xchat/setup)")
        if not xdk_available():
            out.append("el paquete chat_xdk en el Python de Faustus")
        return out


def load_config() -> XChatConfig:
    from src.settings import get_setting
    mode = str(get_setting("xchat_agent_mode", "agent") or "agent").strip().lower()
    try:
        poll = max(MIN_POLL_S, float(get_setting("xchat_poll_seconds", 10) or 10))
    except (TypeError, ValueError):
        poll = 10.0
    return XChatConfig(
        enabled=bool(get_setting("xchat_bridge_enabled", False)),
        access_token=_dec(get_setting("xchat_access_token", "")),
        refresh_token=_dec(get_setting("xchat_refresh_token", "")),
        client_id=str(get_setting("xchat_client_id", "") or "").strip(),
        user_id=str(get_setting("xchat_user_id", "") or "").strip(),
        private_keys=_dec(get_setting("xchat_private_keys", "")),
        key_version=str(get_setting("xchat_key_version", "1") or "1").strip(),
        poll_s=poll, mode=mode if mode in ("agent", "chat") else "agent",
        model=str(get_setting("xchat_model", "") or "").strip(),
        owner=str(get_setting("xchat_owner", "") or "").strip(),
        api_base=str(get_setting("xchat_api_base", "") or "").strip() or API_BASE,
    )


def _save_settings(patch: Dict[str, Any]) -> None:
    from src.secret_storage import encrypt
    from src.settings import update_settings
    secret = {"xchat_access_token", "xchat_refresh_token", "xchat_private_keys"}
    update_settings({k: (encrypt(v) if k in secret and v else v) for k, v in patch.items()})


# ── the Chat XDK (optional native package) ──────────────────────────────

def xdk_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("chat_xdk") is not None
    except (ImportError, ValueError):
        return False


def _as_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return dict(obj)
    except Exception:  # noqa: BLE001
        return {}


class XCrypto:
    """The few XDK calls this bridge uses; ``chat`` is injectable for tests."""

    def __init__(self, chat: Any = None):
        if chat is None:
            try:
                import chat_xdk  # type: ignore[import-not-found]
            except ImportError as exc:
                raise XChatError("X Chat needs the chat_xdk package installed in Faustus's Python") from exc
            chat = chat_xdk.Chat()
        self.chat = chat

    def unlock(self, private_keys_b64: str, version: str, user_id: str) -> None:
        self.chat.import_keys(base64.b64decode(private_keys_b64.strip()), version=version)
        self.chat.set_identity(str(user_id), version)
        self.chat.set_cache_keys(True)

    def set_signing_keys(self, keys: List[Dict[str, str]]) -> None:
        self.chat.set_signing_keys(keys)

    def absorb(self, events_b64: List[str]) -> Dict[str, Any]:
        result = self.chat.decrypt_events(events_b64, None) or {}
        return (result.get("conversation_keys") or {}).get("keys") or {}

    def decrypt(self, event_b64: str, keys: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return _as_dict(self.chat.decrypt_event(event_b64, keys or None, None))

    def encrypt(self, conversation_id: str, text: str) -> Dict[str, str]:
        p = self.chat.encrypt_message(str(conversation_id), text)
        return {"message_id": p.message_id, "encoded_message_create_event": p.encrypted_content,
                "encoded_message_event_signature": p.encoded_event_signature}

    def new_identity(self) -> Dict[str, Any]:
        reg = self.chat.generate_keypairs()
        version = str(getattr(reg, "version", None) or "1")
        pk = reg.public_key
        body = {"public_key": {"public_key": pk.public_key, "signing_public_key": pk.signing_public_key,
                               "identity_public_key_signature": pk.identity_public_key_signature,
                               "signing_public_key_signature": pk.signing_public_key_signature,
                               "registration_method": pk.registration_method},
                "version": version, "generate_version": bool(getattr(reg, "generate_version", False))}
        blob = base64.b64encode(bytes(self.chat.export_keys() or b"")).decode("ascii")
        return {"registration": body, "version": version, "private_keys_b64": blob}


# ── the X API ───────────────────────────────────────────────────────────

class XApi:
    def __init__(self, cfg: XChatConfig, *, client: Optional[httpx.AsyncClient] = None,
                 persist: Callable[[Dict[str, Any]], None] = _save_settings):
        self.cfg = cfg
        self._client = client
        self._persist = persist

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _refresh(self) -> None:
        if not (self.cfg.refresh_token and self.cfg.client_id):
            raise XChatError("X access token expired and there is no refresh token + client id to renew it")
        r = await self._http().post(f"{self.cfg.api_base}/2/oauth2/token", data={
            "grant_type": "refresh_token", "refresh_token": self.cfg.refresh_token, "client_id": self.cfg.client_id})
        if r.status_code >= 400:
            raise XChatError(f"X token refresh refused ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self.cfg.access_token = data.get("access_token") or ""
        self.cfg.refresh_token = data.get("refresh_token") or self.cfg.refresh_token
        self._persist({"xchat_access_token": self.cfg.access_token, "xchat_refresh_token": self.cfg.refresh_token})

    async def call(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                   json_body: Any = None) -> Dict[str, Any]:
        for attempt in (0, 1):
            r = await self._http().request(method, self.cfg.api_base + path, params=params, json=json_body,
                                           headers={"Authorization": f"Bearer {self.cfg.access_token}"})
            if r.status_code == 401 and attempt == 0 and self.cfg.refresh_token:
                await self._refresh()
                continue
            break
        if r.status_code >= 400:
            raise XChatError(f"X {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def me(self) -> Dict[str, Any]:
        return (await self.call("GET", "/2/users/me")).get("data") or {}

    async def public_keys(self, user_id: str) -> List[Dict[str, Any]]:
        return (await self.call("GET", f"/2/users/{user_id}/public_keys")).get("data") or []

    async def events(self, conversation_id: str, token: Optional[str] = None) -> Dict[str, Any]:
        params = {"max_results": 50, "chat_message_event.fields": EVENT_FIELDS}
        if token:
            params["pagination_token"] = token
        return await self.call("GET", f"/2/chat/conversations/{conversation_id}/events", params=params)

    async def send(self, conversation_id: str, body: Dict[str, str]) -> Dict[str, Any]:
        return await self.call("POST", f"/2/chat/conversations/{conversation_id}/messages", json_body=body)


def self_chat_id(user_id: str) -> str:
    return f"{user_id}-{user_id}"


def _newer(a: str, b: str) -> bool:
    try:
        return int(a) > int(b)
    except (TypeError, ValueError):
        return str(a) > str(b)


# ── setup: Faustus's encryption identity on the account ─────────────────

async def setup_identity(cfg: Optional[XChatConfig] = None, *, api: Optional[XApi] = None,
                         crypto: Optional[XCrypto] = None, persist: Callable = _save_settings) -> Dict[str, Any]:
    """Find the account, generate Faustus's E2EE identity and register its public keys.
    Idempotent while a saved identity exists (it is reused, nothing is registered again)."""
    cfg = cfg or load_config()
    if not cfg.access_token:
        raise XChatError("save an X user access token first (Ajustes › Chat de X)")
    api = api or XApi(cfg, persist=persist)
    me = await api.me()
    user_id = str(me.get("id") or "")
    if not user_id:
        raise XChatError("X did not say which account the token belongs to (GET /2/users/me)")
    if cfg.private_keys:
        persist({"xchat_user_id": user_id})
        return {"ok": True, "user_id": user_id, "username": me.get("username"), "registered": False,
                "note": "identity already saved; reused"}
    crypto = crypto or XCrypto()
    ident = crypto.new_identity()
    # Saved before the network call: a refused or interrupted registration resumes the same identity.
    persist({"xchat_private_keys": ident["private_keys_b64"], "xchat_key_version": ident["version"],
             "xchat_user_id": user_id})
    await api.call("POST", f"/2/users/{user_id}/public_keys", json_body=ident["registration"])
    return {"ok": True, "user_id": user_id, "username": me.get("username"), "registered": True,
            "key_version": ident["version"]}


# ── the bridge ──────────────────────────────────────────────────────────

class XChatBridge:
    def __init__(self, *, config_loader: Callable[[], XChatConfig] = load_config,
                 store: Optional[BridgeStore] = None, api: Optional[XApi] = None,
                 crypto: Optional[XCrypto] = None, run_turn: Optional[Callable] = None,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self._load = config_loader
        self._store = store
        self._api = api
        self._crypto = crypto
        self._run_turn = run_turn or turn_mod.run_turn
        self._sleep = sleep
        self._task: Optional[asyncio.Future] = None
        self._keys: Dict[str, Any] = {}
        self._signing: List[Dict[str, str]] = []
        self._known: set = set()
        self.cfg = XChatConfig()
        self.running = False
        self.last_error = ""
        self.last_poll = 0.0
        self.handled = 0

    @property
    def store(self) -> BridgeStore:
        if self._store is None:
            self._store = BridgeStore(default_path("xchat.sqlite3"))
        return self._store

    # lifecycle
    def start(self, supervisor: Any = None) -> bool:
        if self._task is not None and not self._task.done():
            return False
        coro = self.run()
        self._task = supervisor.spawn(coro, name="xchat-bridge") if supervisor is not None else asyncio.ensure_future(coro)
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.running = False

    async def run(self) -> None:
        self.running = True
        try:
            while True:
                try:
                    await self.poll_once()
                    self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad poll never ends the bridge
                    self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    logger.warning("xchat bridge: %s", self.last_error)
                await self._sleep(self.cfg.poll_s or 10.0)
        finally:
            self.running = False

    # one poll
    def _ensure(self) -> None:
        cfg = self._load()
        if not cfg.configured:
            raise XChatError("not configured: falta " + ", ".join(cfg.missing() or ["activarlo"]))
        if not cfg.user_id:
            raise XChatError("no account id yet: run POST /api/chat-bridges/xchat/setup")
        self.cfg = cfg
        if self._api is None:
            self._api = XApi(cfg)
        if self._crypto is None:
            self._crypto = XCrypto()
            self._crypto.unlock(cfg.private_keys, cfg.key_version, cfg.user_id)

    async def _signing_keys_for(self, senders: set) -> None:
        new = senders - self._known
        for uid in new:
            for pk in await self._api.public_keys(uid):
                self._signing.append({"user_id": uid, "public_key_version": str(pk.get("public_key_version") or ""),
                                      "public_key": pk.get("signing_public_key") or "",
                                      "identity_public_key": pk.get("public_key") or "",
                                      "identity_public_key_signature": pk.get("identity_public_key_signature") or ""})
            self._known.add(uid)
        if new and self._signing:
            self._crypto.set_signing_keys(self._signing)

    def _sent_ids(self) -> List[str]:
        raw = self.store.get_meta("sent_ids", "") or ""
        return [x for x in raw.split(",") if x]

    def _remember_sent(self, message_id: str) -> None:
        ids = (self._sent_ids() + [message_id])[-MAX_SENT_IDS:]
        self.store.set_meta("sent_ids", ",".join(ids))

    async def poll_once(self) -> int:
        """Read the self-chat once; answer every new message typed there. Returns how many."""
        self._ensure()
        self.last_poll = time.time()
        me = self.cfg.user_id
        conv = self_chat_id(me)
        cursor = self.store.get_meta("cursor", "") or ""
        collected: List[Dict[str, Any]] = []
        key_events: List[str] = []
        token = None
        for _ in range(10):
            page = await self._api.events(conv, token)
            meta = page.get("meta") or {}
            key_events += [k for k in meta.get("conversation_key_events") or [] if k and k not in key_events]
            reached = False
            for item in page.get("data") or []:
                eid = str(item.get("id") or "")
                if cursor and eid and not _newer(eid, cursor):
                    reached = True
                    break
                collected.append(item)
            token = meta.get("next_token")
            if reached or not token:
                break
        collected.reverse()                          # oldest first
        await self._signing_keys_for({me})
        if key_events:
            self._keys.update(self._crypto.absorb(key_events))
        if not collected:
            return 0
        if not cursor:
            # First run: learn the keys from the backlog, answer nothing old.
            self._keys.update(self._crypto.absorb([e["encoded_event"] for e in collected if e.get("encoded_event")]))
            self.store.set_meta("cursor", str(collected[-1].get("id") or ""))
            return 0
        sent = set(self._sent_ids())
        handled = 0
        for item in collected:
            eid = str(item.get("id") or "")
            try:
                if item.get("encoded_event"):
                    event = self._crypto.decrypt(item["encoded_event"], self._keys)
                    if event.get("type") == "KeyChange":
                        self._keys.update(self._crypto.absorb([item["encoded_event"]]))
                    elif event.get("type") in ("Message", "MessageEdit"):
                        mid = str(event.get("id") or eid)
                        text = ((event.get("content") or {}) if isinstance(event.get("content"), dict) else {}).get("text") or ""
                        sender = str(event.get("sender_id") or item.get("sender_id") or "")
                        if sender == me and mid not in sent and eid not in sent and text.strip():
                            await self._answer(conv, text)
                            handled += 1
            finally:
                if eid:
                    self.store.set_meta("cursor", eid)
        self.handled += handled
        return handled

    async def _answer(self, conv: str, text: str) -> None:
        owner = turn_mod.resolve_owner(self.cfg.owner)
        if not owner:
            raise XChatError("Faustus has no account to run this conversation under")
        row = self.store.get_session(conv)
        sid = row["session_id"] if row and turn_mod.session_exists(row["session_id"], owner) else ""
        if not sid:
            sid = turn_mod.create_session(owner, "X · chat propio", self.cfg.model)
            self.store.set_session(conv, sid, "X · chat propio")
        result = await self._run_turn(turn_mod.TurnRequest(owner=owner, session_id=sid, text=text,
                                                           mode=self.cfg.mode, model=self.cfg.model, source="xchat"))
        reply = (result.text or "").strip() or (f"(sin respuesta: {result.error})" if result.error else "(sin respuesta)")
        if result.approvals:
            reply += "\n\nHay una aprobación pendiente en Faustus Studio para seguir."
        body = self._crypto.encrypt(conv, reply[:9000])
        self._remember_sent(str(body.get("message_id") or ""))
        sent = await self._api.send(conv, body)
        sent_id = str(((sent or {}).get("data") or {}).get("event_id") or ((sent or {}).get("data") or {}).get("id") or "")
        if sent_id:
            self._remember_sent(sent_id)
        self.store.touch(conv)

    def status(self) -> Dict[str, Any]:
        try:
            cfg = self._load()
        except Exception:  # noqa: BLE001
            cfg = XChatConfig()
        rows = []
        try:
            rows = self.store.list_sessions()
        except Exception:  # noqa: BLE001
            pass
        return {"enabled": cfg.enabled, "running": self.running, "configured": cfg.configured,
                "missing": cfg.missing(), "xdk_installed": xdk_available(), "user_id": cfg.user_id,
                "self_chat": self_chat_id(cfg.user_id) if cfg.user_id else "", "mode": cfg.mode,
                "poll_seconds": cfg.poll_s, "last_poll": self.last_poll, "last_error": self.last_error,
                "messages_answered": self.handled,
                "conversations": [{"chat": r["chat_id"], "session_id": r["session_id"]} for r in rows],
                "scopes_needed": SCOPES}


_bridge: Optional[XChatBridge] = None
_supervisor: Any = None


def get_bridge() -> XChatBridge:
    global _bridge
    if _bridge is None:
        _bridge = XChatBridge()
    return _bridge


def start_xchat_bridge(supervisor: Any = None) -> bool:
    global _supervisor
    if supervisor is not None:
        _supervisor = supervisor
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return False
    if not (cfg.configured and cfg.user_id and xdk_available()):
        return False
    return get_bridge().start(_supervisor)


async def stop_xchat_bridge() -> None:
    if _bridge is not None:
        await _bridge.stop()


async def apply_settings() -> Dict[str, Any]:
    bridge = get_bridge()
    await bridge.stop()
    bridge._api = None
    bridge._crypto = None
    start_xchat_bridge(_supervisor)
    return bridge.status()
