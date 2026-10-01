"""src/chat_bridges/telegram_bridge.py — talk to a Faustus agent from a Telegram chat.

An opt-in poller. It uses Telegram's Bot HTTP API with long polling
(`getUpdates` with `timeout` and `offset`), so Faustus opens only outgoing
connections and needs no public address.

* **Who may talk to it.** Only chats whose id is in `telegram_allowed_chat_ids`
  (empty = nobody). Any other chat is logged once with its id, answered once
  with that id so the owner can add it, and ignored from then on.
* **What a message can do.** It is a normal turn in a normal Faustus session
  (`src/chat_bridges/turn.py`): the text is marked as outside text, so a tool
  that changes something stops at an approval card. The bot never approves
  anything; it says what is waiting and links to the conversation in Studio.
* **One chat, one conversation.** The mapping lives in a small sqlite file;
  `/new` starts a fresh conversation, `/status` and `/help` answer at once.
  One turn at a time per chat; messages that arrive meanwhile are queued in
  order.
* **Failure.** Network errors and 5xx back off 1, 2, 5, 10, 30 s and never spin;
  a 401 disables the poller with a status message instead of retrying a token
  that is wrong.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from . import status as status_mod, text_format, turn as turn_mod
from .store import BridgeStore

logger = logging.getLogger(__name__)

# The bot token is part of every Bot API URL, and the HTTP client logs each request
# URL at INFO, which the app's root logger writes to its log file. Scrub it there.
_BOT_TOKEN_IN_URL = re.compile(r"/bot\d{3,}:[A-Za-z0-9_-]{10,}")


class _ScrubBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "/bot" in message and _BOT_TOKEN_IN_URL.search(message):
            record.msg = _BOT_TOKEN_IN_URL.sub("/bot***", message)
            record.args = ()
        return True


def _install_log_scrubber() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _ScrubBotToken) for f in target.filters):
            target.addFilter(_ScrubBotToken())


_install_log_scrubber()

#: Seconds to wait after the 1st, 2nd, 3rd… consecutive failure (the last repeats).
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
#: Long-poll window handed to `getUpdates`.
POLL_TIMEOUT_S = 25
#: Messages waiting behind a running turn, per chat.
MAX_QUEUED = 20
#: Telegram refuses photos above this; larger files go as documents.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
_TYPING_EVERY_S = 4.0

HELP_TEXT = (
    "This chat talks to your Faustus agent.\n"
    "Write a message and the agent answers it.\n\n"
    "/new - start a fresh conversation\n"
    "/status - model, conversation link and whether a turn is running\n"
    "/help - this text\n\n"
    "Anything that needs an approval waits in Studio: the bot replies with a link."
)


# ── Telegram Bot API ─────────────────────────────────────────────────────

class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: float = 0.0) -> None:
        super().__init__(f"{code}: {description}")
        self.code = int(code)
        self.description = description
        self.retry_after = float(retry_after or 0.0)


class TelegramUnauthorized(TelegramError):
    """401: the token is wrong or revoked."""


class TelegramNetworkError(Exception):
    """The request never produced an answer (refused, reset, timed out)."""


def _is_loopback(base: str) -> bool:
    host = (urlparse(base).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _cancel_requested() -> bool:
    """True when the running task has been asked to stop. A cancel that lands inside
    the HTTP client can come back out as an ordinary network error (the connection
    attempt it interrupted fails); the loops below would then carry on, and the one
    cancel request would be spent. They check this after any swallowed error."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    return bool(task is not None and cancelling is not None and cancelling())


async def _cancel_and_wait(task: Optional["asyncio.Future"], *, tries: int = 5, wait_s: float = 2.0) -> None:
    """Cancel a task and wait until it has finished, cancelling again if a cancel was
    swallowed on the way; gives up after `tries` rounds instead of hanging."""
    if task is None:
        return
    for _ in range(tries):
        if task.done():
            break
        task.cancel()
        await asyncio.wait({task}, timeout=wait_s)
    if task.done() and not task.cancelled():
        task.exception()          # mark it retrieved; the error was logged where it happened
    elif not task.done():
        logger.warning("telegram bridge: a task did not stop after %d cancel requests", tries)


class TelegramApi:
    """The few Bot API methods the bridge uses. The token is part of every URL,
    so it is scrubbed from anything that is logged or raised."""

    def __init__(self, token: str, base: str = "https://api.telegram.org", *,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.token = token
        self.base = (base or "https://api.telegram.org").rstrip("/")
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # A local test server must not be sent through an environment proxy.
            self._client = httpx.AsyncClient(trust_env=not _is_loopback(self.base))
        return self._client

    def _clean(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    async def call(self, method: str, payload: Optional[Dict[str, Any]] = None, *,
                   files: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        url = f"{self.base}/bot{self.token}/{method}"
        try:
            if files:
                response = await self._http().post(url, data=payload or {}, files=files, timeout=timeout)
            else:
                response = await self._http().post(url, json=payload or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(self._clean(f"{type(exc).__name__}: {exc}")[:300]) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code == 401:
            raise TelegramUnauthorized(401, str(body.get("description") or "Unauthorized"))
        if not body.get("ok"):
            params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
            raise TelegramError(int(body.get("error_code") or response.status_code),
                                self._clean(str(body.get("description") or f"HTTP {response.status_code}"))[:300],
                                float(params.get("retry_after") or 0))
        return body.get("result")

    async def get_me(self) -> Dict[str, Any]:
        return await self.call("getMe", timeout=15.0) or {}

    async def get_updates(self, offset: Optional[int], timeout: int) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = int(offset)
        return await self.call("getUpdates", payload, timeout=float(timeout) + 15.0) or []

    async def send_message(self, chat_id: str, text: str, *, html: bool = False) -> Any:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        return await self.call("sendMessage", payload)

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> Any:
        return await self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10.0)

    async def send_file(self, chat_id: str, path: str) -> Any:
        """A local image: `sendPhoto`, or `sendDocument` when it is too big for one."""
        file = Path(path)
        data = await asyncio.to_thread(file.read_bytes)
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if len(data) > MAX_PHOTO_BYTES:
            return await self.call("sendDocument", {"chat_id": chat_id},
                                   files={"document": (file.name, data, mime)}, timeout=120.0)
        return await self.call("sendPhoto", {"chat_id": chat_id},
                               files={"photo": (file.name, data, mime)}, timeout=120.0)


# ── configuration ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool = False
    token: str = ""
    allowed: frozenset = frozenset()
    mode: str = "agent"
    model: str = ""
    owner: str = ""
    api_base: str = "https://api.telegram.org"
    public_url: str = "http://127.0.0.1:7000"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.token)


def _decrypt_token(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        from src.secret_storage import decrypt
        return str(decrypt(raw) or "").strip()
    except Exception:  # noqa: BLE001 - an undecryptable value is no token at all
        logger.warning("telegram bridge: the saved bot token could not be read; enter it again")
        return ""


def load_config() -> BridgeConfig:
    """The current settings, read fresh (the allow-list and mode apply live)."""
    from src.settings import get_setting
    raw_ids = get_setting("telegram_allowed_chat_ids", []) or []
    if isinstance(raw_ids, str):
        raw_ids = re.split(r"[,\s]+", raw_ids)
    allowed = frozenset(str(item).strip() for item in raw_ids if str(item).strip())
    mode = str(get_setting("telegram_agent_mode", "agent") or "agent").strip().lower()
    public = str(get_setting("app_public_url", "") or "").strip().rstrip("/")
    if not public:
        from src.constants import internal_api_base
        public = internal_api_base()
    return BridgeConfig(
        enabled=bool(get_setting("telegram_bridge_enabled", False)),
        token=_decrypt_token(str(get_setting("telegram_bot_token", "") or "")),
        allowed=allowed,
        mode=mode if mode in ("agent", "chat") else "agent",
        model=str(get_setting("telegram_model", "") or "").strip(),
        owner=str(get_setting("telegram_owner", "") or "").strip(),
        api_base=str(get_setting("telegram_api_base", "") or "").strip() or "https://api.telegram.org",
        public_url=public,
    )


# ── the turn backend (replaceable in tests) ─────────────────────────────

class TurnBackend:
    """What the bridge needs from Faustus. Resolved through the `turn` module
    at call time, so a test can swap a single function or the whole object."""

    def resolve_owner(self, configured: str) -> str:
        return turn_mod.resolve_owner(configured)

    def session_exists(self, session_id: str, owner: str) -> bool:
        return turn_mod.session_exists(session_id, owner)

    def create_session(self, owner: str, name: str, model: str) -> str:
        return turn_mod.create_session(owner, name, model)

    def model_name(self, owner: str, model: str, session_id: str = "") -> str:
        if session_id and not model:
            sess = turn_mod.get_session(session_id)
            if sess is not None and getattr(sess, "model", ""):
                return str(sess.model)
        return turn_mod.default_model_name(owner, model)

    async def run_turn(self, request: turn_mod.TurnRequest) -> turn_mod.TurnResult:
        return await turn_mod.run_turn(request)


@dataclass
class _Item:
    kind: str                  # "text" | "new"
    text: str = ""


@dataclass
class _Chat:
    chat_id: str
    title: str = ""
    queue: "asyncio.Queue[_Item]" = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUED))
    task: Optional[asyncio.Task] = None
    busy: bool = False


def session_link(public_url: str, session_id: str) -> str:
    return status_mod.link(public_url, session_id)


class TelegramBridge:
    def __init__(self, *, config_loader: Callable[[], BridgeConfig] = load_config,
                 store: Optional[BridgeStore] = None, backend: Optional[TurnBackend] = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 poll_timeout: int = POLL_TIMEOUT_S,
                 api_factory: Optional[Callable[[BridgeConfig], TelegramApi]] = None) -> None:
        self._config_loader = config_loader
        self._store = store
        self._backend = backend or TurnBackend()
        self._sleep = sleep
        self._poll_timeout = poll_timeout
        self._api_factory = api_factory or (lambda cfg: TelegramApi(cfg.token, cfg.api_base))
        self._cfg = BridgeConfig()
        self._api: Optional[TelegramApi] = None
        self._task: Optional[asyncio.Task] = None
        self._chats: Dict[str, _Chat] = {}
        self._offset: Optional[int] = None
        self._warned: set = set()
        # status
        self.running = False
        self.bot_username = ""
        self.bot_name = ""
        self.last_error = ""
        self.last_error_at = 0.0
        self.disabled_reason = ""
        self.started_at = 0.0
        self.failures = 0

    # ── store ───────────────────────────────────────────────────────────
    @property
    def store(self) -> BridgeStore:
        if self._store is None:
            self._store = BridgeStore()
        return self._store

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self, supervisor: Any = None) -> bool:
        """Spawn the poller. False when it is already running."""
        if self._task is not None and not self._task.done():
            return False
        coro = self.run()
        task = None
        if supervisor is not None:
            task = supervisor.spawn(coro, name="telegram-bridge")
        else:
            task = asyncio.ensure_future(coro)
        self._task = task
        return task is not None

    async def stop(self) -> None:
        task, self._task = self._task, None
        await _cancel_and_wait(task, wait_s=5.0)      # the poller's own shutdown takes a few seconds at most
        self.running = False

    def needs_restart(self, cfg: BridgeConfig) -> bool:
        cur = self._cfg
        return (cfg.token != cur.token or cfg.api_base != cur.api_base or bool(self.disabled_reason)
                or self._task is None or self._task.done())

    # ── the poller ──────────────────────────────────────────────────────
    async def run(self) -> None:
        self.running = True
        self.started_at = time.time()
        self.disabled_reason = ""
        self.failures = 0
        self._publish()
        if self._offset is None:
            try:
                saved = self.store.get_meta("offset")
                self._offset = int(saved) if saved else None
            except Exception:  # noqa: BLE001
                self._offset = None
        try:
            while True:
                cfg = self._config_loader()
                self._cfg = cfg
                if not cfg.configured:
                    self.disabled_reason = "" if cfg.enabled is False else "No bot token is set."
                    logger.info("telegram bridge: stopped (%s)", "disabled in settings" if not cfg.enabled else "no token")
                    return
                api = self._ensure_api(cfg)
                try:
                    if not self.bot_username:
                        await self._identify(api)
                    updates = await api.get_updates(self._offset, self._poll_timeout)
                    self.failures = 0
                    self._publish()
                    # The poll may have waited ~25 s: judge what arrived by the settings
                    # as they are NOW (a chat just removed from the list must not be answered).
                    cfg = self._cfg = self._config_loader()
                    if not cfg.configured:
                        continue
                    for update in updates:
                        await self._safe_dispatch(update, cfg, api)
                except TelegramUnauthorized:
                    self.disabled_reason = ("Telegram rejected the bot token (401 Unauthorized). "
                                            "Check the token in Settings; saving it starts the bridge again.")
                    self._record_error(self.disabled_reason)
                    logger.error("telegram bridge: %s", self.disabled_reason)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the poller must outlive any single failure
                    if _cancel_requested():
                        raise asyncio.CancelledError() from None
                    self._record_error(str(exc) or type(exc).__name__)
                    delay = BACKOFF_S[min(self.failures, len(BACKOFF_S) - 1)]
                    if isinstance(exc, TelegramError) and exc.retry_after:
                        delay = max(delay, min(exc.retry_after, 60.0))
                    self.failures += 1
                    logger.warning("telegram bridge: %s; retrying in %gs", self.last_error, delay)
                    await self._sleep(delay)
        finally:
            self.running = False
            self._publish()
            await self._shutdown_workers()
            if self._api is not None:
                api, self._api = self._api, None
                try:
                    await asyncio.wait_for(api.aclose(), 5.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass

    def _ensure_api(self, cfg: BridgeConfig) -> TelegramApi:
        api = self._api
        if api is None or api.token != cfg.token or api.base != cfg.api_base.rstrip("/"):
            if api is not None:
                asyncio.ensure_future(api.aclose())
            api = self._api = self._api_factory(cfg)
            self.bot_username = ""
            self.bot_name = ""
        return api

    async def _identify(self, api: TelegramApi) -> None:
        me = await api.get_me()
        self.bot_username = str(me.get("username") or "")
        self.bot_name = str(me.get("first_name") or "")
        logger.info("telegram bridge: connected as @%s", self.bot_username or "?")
        self._publish()

    def _record_error(self, message: str) -> None:
        self.last_error = message[:300]
        self.last_error_at = time.time()
        self._publish()

    async def _shutdown_workers(self) -> None:
        chats, self._chats = list(self._chats.values()), {}
        tasks = [chat.task for chat in chats if chat.task is not None]
        if tasks:
            await asyncio.gather(*(_cancel_and_wait(task, tries=3, wait_s=1.0) for task in tasks),
                                 return_exceptions=True)

    # ── updates ─────────────────────────────────────────────────────────
    async def _safe_dispatch(self, update: Dict[str, Any], cfg: BridgeConfig, api: TelegramApi) -> None:
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            return
        # The offset moves first: a message that crashes the handler must not be
        # handed to us again on the next poll.
        self._offset = update_id + 1
        try:
            self.store.set_meta("offset", str(self._offset))
        except Exception:  # noqa: BLE001
            logger.debug("telegram bridge: could not save the offset", exc_info=True)
        try:
            await self._dispatch(update, cfg, api)
        except (asyncio.CancelledError, TelegramUnauthorized):
            raise
        except Exception:  # noqa: BLE001
            logger.warning("telegram bridge: could not handle update %s", update_id, exc_info=True)

    @staticmethod
    def _title(chat: Dict[str, Any], sender: Dict[str, Any]) -> str:
        title = str(chat.get("title") or "").strip()
        if not title:
            person = chat if chat.get("first_name") or chat.get("username") else sender
            title = " ".join(p for p in (str(person.get("first_name") or ""), str(person.get("last_name") or "")) if p).strip()
            title = title or str(person.get("username") or "")
        return (title or str(chat.get("id") or "chat"))[:80]

    async def _dispatch(self, update: Dict[str, Any], cfg: BridgeConfig, api: TelegramApi) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        if chat.get("id") is None or sender.get("is_bot"):
            return
        chat_id = str(chat["id"])
        title = self._title(chat, sender)

        if chat_id not in cfg.allowed:
            await self._refuse(api, chat_id, title)
            return

        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
            await self._say(api, chat_id, "I can only read text messages.")
            return

        state = self._chats.get(chat_id)
        if state is None:
            state = self._chats[chat_id] = _Chat(chat_id, title)
        state.title = title

        command = self._command(text)
        if command is not None:
            name, arg = command
            if name == "other":
                return
            if name in ("help", "start"):
                await self._say(api, chat_id, HELP_TEXT)
            elif name == "status":
                await self._say(api, chat_id, self._status_text(state, cfg))
            elif name == "new":
                await self._enqueue(api, state, _Item("new"))
            else:
                await self._say(api, chat_id, "Unknown command. Send /help.")
            return
        await self._enqueue(api, state, _Item("text", text))

    def _command(self, text: str):
        if not text.startswith("/"):
            return None
        head, _, rest = text.partition(" ")
        name, _, target = head[1:].partition("@")
        if target and self.bot_username and target.lower() != self.bot_username.lower():
            return ("other", rest)          # addressed to some other bot in a group
        return (name.lower(), rest.strip())

    async def _refuse(self, api: TelegramApi, chat_id: str, title: str) -> None:
        if chat_id not in self._warned:
            self._warned.add(chat_id)
            logger.warning("telegram bridge: ignoring chat %s (%s); add %s to telegram_allowed_chat_ids to allow it",
                           chat_id, re.sub(r"[\r\n]", " ", title)[:60], chat_id)
        if self.store.was_refused(chat_id):
            return
        self.store.mark_refused(chat_id, title)
        await self._say(api, chat_id, f"This chat is not authorised. Your chat id is {chat_id}.")

    async def _enqueue(self, api: TelegramApi, state: _Chat, item: _Item) -> None:
        ahead = state.queue.qsize() + (1 if state.busy else 0)
        try:
            state.queue.put_nowait(item)
        except asyncio.QueueFull:
            await self._say(api, state.chat_id, f"Too many messages are waiting ({MAX_QUEUED}). Wait for the current turn to finish.")
            return
        if state.task is None or state.task.done():
            state.task = asyncio.ensure_future(self._worker(state))
        if ahead:
            await self._say(api, state.chat_id, f"Queued: {ahead} ahead of this message.")

    # ── per-chat worker ─────────────────────────────────────────────────
    async def _worker(self, state: _Chat) -> None:
        while True:
            item = await state.queue.get()
            state.busy = True
            try:
                await self._process(state, item)
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorized:
                pass                      # the poller sees the same 401 and disables itself
            except Exception:  # noqa: BLE001
                if _cancel_requested():
                    raise asyncio.CancelledError() from None
                logger.warning("telegram bridge: turn for chat %s failed", state.chat_id, exc_info=True)
                try:
                    await self._say(self._api, state.chat_id,
                                    "Something went wrong on the Faustus side. The details are in its log.")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                state.busy = False
                state.queue.task_done()

    async def _process(self, state: _Chat, item: _Item) -> None:
        cfg, api = self._cfg, self._api
        if api is None:
            return
        chat_id = state.chat_id
        owner = self._backend.resolve_owner(cfg.owner)
        if not owner:
            await self._say(api, chat_id, "Faustus has no account to run this conversation under. Create one first.")
            return
        name = f"Telegram · {state.title or chat_id}"
        if item.kind == "new":
            try:
                sid = self._backend.create_session(owner, name, cfg.model)
            except Exception as exc:  # noqa: BLE001
                await self._say(api, chat_id, f"Could not start a new conversation: {exc}")
                return
            self.store.set_session(chat_id, sid, state.title)
            await self._say(api, chat_id, "New conversation started.\n" + session_link(cfg.public_url, sid))
            return

        try:
            sid = self._session_for(chat_id, state.title, owner, name, cfg)
        except Exception as exc:  # noqa: BLE001
            await self._say(api, chat_id, f"Faustus could not start this conversation: {exc}")
            return
        typing = asyncio.ensure_future(self._typing(api, chat_id))
        try:
            request = turn_mod.TurnRequest(owner=owner, session_id=sid, text=item.text, mode=cfg.mode, model=cfg.model)
            try:
                result = await self._backend.run_turn(request)
            except turn_mod.TurnUnavailable as exc:
                await self._say(api, chat_id, f"Faustus could not start this turn: {exc}")
                return
        finally:
            await _cancel_and_wait(typing)
        self.store.touch(chat_id)
        await self._reply(api, chat_id, result, session_link(cfg.public_url, sid))

    def _session_for(self, chat_id: str, title: str, owner: str, name: str, cfg: BridgeConfig) -> str:
        row = self.store.get_session(chat_id)
        if row and self._backend.session_exists(row["session_id"], owner):
            return row["session_id"]
        sid = self._backend.create_session(owner, name, cfg.model)
        self.store.set_session(chat_id, sid, title)
        return sid

    async def _typing(self, api: TelegramApi, chat_id: str) -> None:
        while True:
            try:
                await api.send_chat_action(chat_id)
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorized:
                return
            except Exception:  # noqa: BLE001 - the indicator is a courtesy
                if _cancel_requested():
                    raise asyncio.CancelledError() from None
            await asyncio.sleep(_TYPING_EVERY_S)

    # ── replies ─────────────────────────────────────────────────────────
    async def _reply(self, api: TelegramApi, chat_id: str, result: turn_mod.TurnResult, link: str) -> None:
        text = (result.text or "").strip()
        if text:
            await self._say_markdown(api, chat_id, text)
        for path in result.images[:turn_mod.MAX_IMAGES]:
            try:
                await api.send_file(chat_id, path)
            except TelegramUnauthorized:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram bridge: could not send an image: %s", exc)
                await self._say(api, chat_id, "The turn produced an image that could not be sent here. It is in the conversation in Studio.")
        if result.approvals:
            lines = ["Waiting for you in Faustus:"]
            for card in result.approvals[:3]:
                question = " ".join(str(card.get("question") or "").split())[:300] or "an approval"
                lines.append(f"- {question}")
                if card.get("options"):
                    lines.append("  Options: " + " / ".join(card["options"][:4]))
            lines += ["", "Answer it in Studio (nothing was approved):", link]
            await self._say(api, chat_id, "\n".join(lines))
        elif result.error:
            prefix = "The turn ended with an error" if text else "The turn failed"
            await self._say(api, chat_id, f"{prefix}: {result.error}")
        elif not text and not result.images:
            await self._say(api, chat_id, "The agent finished without writing an answer. The conversation is here:\n" + link)

    async def _say(self, api: Optional[TelegramApi], chat_id: str, text: str) -> None:
        """One plain message; a failure is logged, not raised (except a bad token)."""
        if api is None:
            return
        try:
            for part in text_format.plain_chunks(text):
                await api.send_message(chat_id, part)
        except TelegramUnauthorized:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram bridge: could not send to chat %s: %s", chat_id, exc)

    async def _say_markdown(self, api: TelegramApi, chat_id: str, markdown: str) -> None:
        for rendered, plain in text_format.render_pairs(markdown):
            try:
                try:
                    await api.send_message(chat_id, rendered, html=True)
                except TelegramError as exc:
                    if exc.code != 400 or isinstance(exc, TelegramUnauthorized):
                        raise
                    # The chat app did not accept the markup: say the same thing without it.
                    for part in text_format.plain_chunks(plain):
                        await api.send_message(chat_id, part)
            except TelegramUnauthorized:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram bridge: could not send an answer to chat %s: %s", chat_id, exc)
                return

    def _status_text(self, state: _Chat, cfg: BridgeConfig) -> str:
        row = self.store.get_session(state.chat_id)
        sid = row["session_id"] if row else ""
        try:
            owner = self._backend.resolve_owner(cfg.owner)
            model = self._backend.model_name(owner, cfg.model, sid) or "(default)"
        except Exception:  # noqa: BLE001
            model = cfg.model or "(default)"
        queued = state.queue.qsize()
        lines = [
            f"Model: {model}",
            f"Mode: {cfg.mode}",
            f"Conversation: {session_link(cfg.public_url, sid) if sid else 'none yet (send a message or /new)'}",
            f"Turn running: {'yes' if state.busy else 'no'}" + (f" ({queued} queued)" if queued else ""),
        ]
        return "\n".join(lines)

    # ── status for the route and the MCP server ─────────────────────────
    def _live(self) -> Dict[str, Any]:
        return {
            "running": bool(self.running and self._task is not None and not self._task.done()),
            "bot_username": self.bot_username, "bot_name": self.bot_name,
            "last_error": self.last_error, "last_error_at": self.last_error_at,
            "disabled_reason": self.disabled_reason, "failures_in_a_row": self.failures,
        }

    def status(self) -> Dict[str, Any]:
        cfg = self._config_loader()
        try:
            return status_mod.assemble(
                cfg, self.store, self._live(),
                busy_chats=[c.chat_id for c in self._chats.values() if c.busy],
                queued=sum(c.queue.qsize() for c in self._chats.values()))
        except Exception:  # noqa: BLE001 - a broken store must not hide the rest
            logger.debug("telegram bridge: status without the store", exc_info=True)
            return {"enabled": cfg.enabled, "configured": bool(cfg.token), **self._live(), "mode": cfg.mode,
                    "allowed_chats": sorted(cfg.allowed), "mapped_sessions": [], "refused_chats": []}

    def _publish(self) -> None:
        """Leave a snapshot for processes outside the app (the MCP server)."""
        try:
            live = self._live()
            live["running"] = self.running
            status_mod.write_snapshot(self.store, live)
        except Exception:  # noqa: BLE001
            logger.debug("telegram bridge: could not publish status", exc_info=True)


# ── process-wide instance ────────────────────────────────────────────────

_bridge: Optional[TelegramBridge] = None
_supervisor: Any = None


def get_bridge() -> TelegramBridge:
    global _bridge
    if _bridge is None:
        _bridge = TelegramBridge()
    return _bridge


def start_telegram_bridge(supervisor: Any = None) -> bool:
    """Start the poller when it is enabled and has a token (the app lifespan)."""
    global _supervisor
    if supervisor is not None:
        _supervisor = supervisor
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram bridge: settings unreadable, not starting: %s", exc)
        return False
    if not cfg.configured:
        return False
    return get_bridge().start(_supervisor)


async def stop_telegram_bridge() -> None:
    if _bridge is not None:
        await _bridge.stop()


async def apply_settings() -> Dict[str, Any]:
    """After the settings changed: start, restart or stop the poller to match."""
    bridge = get_bridge()
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram bridge: settings unreadable: %s", exc)
        return bridge.status()
    if cfg.configured:
        if bridge.needs_restart(cfg):
            await bridge.stop()
            bridge.start(_supervisor)
    elif bridge.running:
        await bridge.stop()
    return bridge.status()


async def check_token() -> Dict[str, Any]:
    """`getMe` with the saved token (the settings screen's test button)."""
    cfg = load_config()
    if not cfg.token:
        return {"ok": False, "error": "No bot token is saved."}
    api = TelegramApi(cfg.token, cfg.api_base)
    try:
        me = await api.get_me()
        return {"ok": True, "username": str(me.get("username") or ""), "name": str(me.get("first_name") or ""),
                "id": me.get("id")}
    except TelegramUnauthorized:
        return {"ok": False, "error": "Telegram rejected the token (401 Unauthorized)."}
    except (TelegramError, TelegramNetworkError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await api.aclose()
