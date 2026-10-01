"""The Telegram bridge poller, driven against a fake Bot API on localhost.

The fake speaks the few methods the bridge uses (getMe, getUpdates with
offset/timeout, sendMessage, sendPhoto, sendChatAction) over plain HTTP in a
thread, records everything it was sent and can be told to misbehave (401, 500,
reject HTML). The turn itself is replaced by a scripted backend: what is under
test is the poller, the allow-list, the queueing, the splitting and the
failure handling, not the agent loop (tests/test_telegram_bridge_turn.py).
"""
import asyncio
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.chat_bridges import telegram_bridge as tb
from src.chat_bridges.store import BridgeStore
from src.chat_bridges.turn import TurnRequest, TurnResult

TOKEN = "123456:SECRET-TOKEN-VALUE"


# ── the fake Bot API ─────────────────────────────────────────────────────

class FakeTelegram:
    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []          # pending updates
        self.sent = []             # (method, payload) in arrival order
        self.poll_offsets = []
        self.next_id = 100
        self.fail_updates = 0      # answer this many getUpdates with 500
        self.updates_status = None  # e.g. 401 for every getUpdates
        self.reject_html_marker = None
        self.me = {"id": 77, "is_bot": True, "first_name": "Faustus Test", "username": "faustus_test_bot"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass                      # the client gave up (the poller was stopped mid-poll)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                match = re.match(r"^/bot([^/]+)/(\w+)$", self.path)
                if not match or match.group(1) != TOKEN:
                    return self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                method = match.group(2)
                ctype = self.headers.get("Content-Type") or ""
                payload = {}
                if "json" in ctype:
                    payload = json.loads(raw or b"{}")
                elif "multipart" in ctype:
                    text = raw.decode("latin-1")
                    payload = {"_multipart": True,
                               "chat_id": (re.search(r'name="chat_id"\r\n\r\n([^\r]+)', text) or [None, ""])[1],
                               "filename": (re.search(r'filename="([^"]+)"', text) or [None, ""])[1],
                               "size": len(raw)}
                self.server.outer.handle(self, method, payload)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.outer = self
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def handle(self, handler, method, payload):
        if method == "getMe":
            return handler._reply(200, {"ok": True, "result": self.me})
        if method == "getUpdates":
            return self._get_updates(handler, payload)
        with self.lock:
            self.sent.append((method, payload))
        if method == "sendMessage" and payload.get("parse_mode") == "HTML" and self.reject_html_marker \
                and self.reject_html_marker in payload.get("text", ""):
            return handler._reply(400, {"ok": False, "error_code": 400,
                                        "description": "Bad Request: can't parse entities: unsupported start tag"})
        return handler._reply(200, {"ok": True, "result": {"message_id": 1}})

    def _get_updates(self, handler, payload):
        with self.lock:
            self.poll_offsets.append(payload.get("offset"))
            if self.updates_status:
                return handler._reply(self.updates_status, {"ok": False, "error_code": self.updates_status,
                                                            "description": "Unauthorized"})
            if self.fail_updates > 0:
                self.fail_updates -= 1
                return handler._reply(500, {"ok": False, "error_code": 500, "description": "Internal Server Error"})
            offset = payload.get("offset")
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
        deadline = time.time() + min(float(payload.get("timeout") or 0), 0.15)
        while True:
            with self.lock:
                batch = [u for u in self.updates if offset is None or u["update_id"] >= offset]
            if batch or time.time() >= deadline:
                return handler._reply(200, {"ok": True, "result": batch})
            time.sleep(0.01)

    # ── test helpers ────────────────────────────────────────────────────
    def push(self, chat_id, text, *, first_name="Ada", last_name="Lovelace", chat_type="private",
             title=None, is_bot=False):
        with self.lock:
            self.next_id += 1
            chat = {"id": int(chat_id), "type": chat_type}
            if title:
                chat["title"] = title
            else:
                chat.update(first_name=first_name, last_name=last_name)
            msg = {"message_id": self.next_id, "chat": chat, "date": int(time.time()),
                   "from": {"id": 5, "is_bot": is_bot, "first_name": first_name}, "text": text}
            self.updates.append({"update_id": self.next_id, "message": msg})

    def messages(self, chat_id=None, method="sendMessage"):
        with self.lock:
            rows = [p for m, p in self.sent if m == method]
        return [p for p in rows if chat_id is None or str(p.get("chat_id")) == str(chat_id)]

    def texts(self, chat_id=None):
        return [p["text"] for p in self.messages(chat_id)]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    server = FakeTelegram()
    yield server
    server.stop()


# ── the scripted turn backend ────────────────────────────────────────────

class FakeBackend(tb.TurnBackend):
    def __init__(self, answer=None):
        self.requests = []
        self.sessions = {}
        self.created = []
        self.answer = answer or (lambda req: TurnResult(text=f"echo: {req.text}", model="test-model"))
        self.running = 0
        self.max_running = 0
        self.gate = None           # asyncio.Event: a turn waits for it when set

    def resolve_owner(self, configured):
        return configured or "alice"

    def session_exists(self, session_id, owner):
        return session_id in self.sessions

    def create_session(self, owner, name, model):
        sid = f"sess-{len(self.created) + 1}"
        self.created.append({"id": sid, "owner": owner, "name": name, "model": model})
        self.sessions[sid] = name
        return sid

    def model_name(self, owner, model, session_id=""):
        return model or "test-model"

    async def run_turn(self, request: TurnRequest):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            self.requests.append(request)
            if self.gate is not None and len(self.requests) == 1:
                await self.gate.wait()
            result = self.answer(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        finally:
            self.running -= 1


def make_config(fake, **over):
    base = dict(enabled=True, token=TOKEN, allowed=frozenset({"42"}), mode="agent", model="",
                owner="", api_base=fake.url, public_url="http://studio.test")
    base.update(over)
    return tb.BridgeConfig(**base)


class Rig:
    def __init__(self, fake, tmp_path, **over):
        self.fake = fake
        self.cfg = make_config(fake, **over)
        self.store = BridgeStore(str(tmp_path / "bridge.sqlite3"))
        self.backend = FakeBackend()
        self.sleeps = []
        self.bridge = tb.TelegramBridge(config_loader=lambda: self.cfg, store=self.store, backend=self.backend,
                                        sleep=self._sleep, poll_timeout=1)

    async def _sleep(self, delay):
        self.sleeps.append(delay)
        await asyncio.sleep(0)

    def set(self, **over):
        values = dict(self.cfg.__dict__)
        values.update(over)
        self.cfg = tb.BridgeConfig(**values)

    async def __aenter__(self):
        self.bridge.start()
        return self

    async def __aexit__(self, *exc):
        await self.bridge.stop()


async def until(predicate, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# ── an allowed chat gets an answer ───────────────────────────────────────

async def test_allowed_chat_gets_an_answer_and_its_image(fake, tmp_path):
    image = tmp_path / "render.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    rig = Rig(fake, tmp_path)
    async def answer(req):
        await asyncio.sleep(0.15)                  # a real turn takes a while: the typing indicator shows
        return TurnResult(text="Here is **the** render", images=[str(image)], model="m")
    rig.backend.answer = answer
    async with rig:
        fake.push(42, "draw a cube")
        assert await until(lambda: fake.messages(42, "sendPhoto"))
        # the poller identified the bot and reports it
        assert rig.bridge.bot_username == "faustus_test_bot"
        live = rig.bridge.status()
        assert (live["running"], live["bot_username"]) == (True, "faustus_test_bot")
    assert fake.messages(42)[-1]["text"] == "Here is <b>the</b> render"
    assert fake.messages(42)[-1]["parse_mode"] == "HTML"
    photo = fake.messages(42, "sendPhoto")[0]
    assert photo["filename"] == "render.png" and photo["chat_id"] == "42"
    # the turn was a normal one: this owner, this text, the configured mode, a named session
    request = rig.backend.requests[0]
    assert (request.owner, request.text, request.mode) == ("alice", "draw a cube", "agent")
    assert rig.backend.created[0]["name"] == "Telegram · Ada Lovelace"
    assert request.session_id == "sess-1"
    # a typing indicator was shown while it ran
    assert fake.messages(42, "sendChatAction")[0]["action"] == "typing"


async def test_the_chat_uses_the_same_session_for_later_messages(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "one")
        assert await until(lambda: len(fake.texts(42)) >= 1)
        fake.push(42, "two")
        assert await until(lambda: len(fake.texts(42)) >= 2)
    assert [r.session_id for r in rig.backend.requests] == ["sess-1", "sess-1"]
    assert len(rig.backend.created) == 1
    # and the mapping survives in the store
    assert rig.store.get_session("42")["session_id"] == "sess-1"


async def test_a_deleted_session_is_replaced(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    rig.store.set_session("42", "gone", "Ada")
    async with rig:
        fake.push(42, "hello")
        assert await until(lambda: fake.texts(42))
    assert rig.backend.requests[0].session_id == "sess-1"
    assert rig.store.get_session("42")["session_id"] == "sess-1"


# ── security ─────────────────────────────────────────────────────────────

async def test_unknown_chat_is_refused_once_with_its_id(fake, tmp_path, caplog):
    rig = Rig(fake, tmp_path)
    with caplog.at_level(logging.WARNING, logger="src.chat_bridges.telegram_bridge"):
        async with rig:
            fake.push(99, "let me in", first_name="Mallory", last_name="")
            assert await until(lambda: fake.texts(99))
            fake.push(99, "hello?")
            fake.push(99, "hello??")
            fake.push(42, "marker")                       # an allowed chat after them: proves they were consumed
            assert await until(lambda: fake.texts(42))
    assert fake.texts(99) == ["This chat is not authorised. Your chat id is 99."]
    assert rig.backend.requests and all(r.text == "marker" for r in rig.backend.requests)
    ignored = [r for r in caplog.records if "ignoring chat 99" in r.getMessage()]
    assert len(ignored) == 1 and "telegram_allowed_chat_ids" in ignored[0].getMessage()
    assert rig.store.was_refused("99")
    assert rig.bridge.status()["refused_chats"][0]["chat_id"] == "99"


async def test_nobody_is_allowed_when_the_list_is_empty(fake, tmp_path):
    rig = Rig(fake, tmp_path, allowed=frozenset())
    async with rig:
        fake.push(42, "hi")
        assert await until(lambda: fake.texts(42))
    assert "not authorised" in fake.texts(42)[0]
    assert rig.backend.requests == []


async def test_messages_from_bots_and_commands_for_other_bots_are_ignored(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "bot talking", is_bot=True)
        fake.push(42, "/new@someone_elses_bot")
        fake.push(42, "/help")
        assert await until(lambda: fake.texts(42))
    assert len(fake.texts(42)) == 1 and "/new" in fake.texts(42)[0]      # only the help text
    assert rig.backend.requests == [] and rig.backend.created == []


async def test_the_allow_list_applies_without_a_restart(fake, tmp_path):
    rig = Rig(fake, tmp_path, allowed=frozenset())
    async with rig:
        fake.push(42, "first")
        assert await until(lambda: fake.texts(42))
        rig.set(allowed=frozenset({"42"}))
        fake.push(42, "second")
        assert await until(lambda: rig.backend.requests)
    assert rig.backend.requests[0].text == "second"


async def test_there_is_no_approve_command(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "/approve")
        assert await until(lambda: fake.texts(42))
    assert fake.texts(42) == ["Unknown command. Send /help."]
    assert rig.backend.requests == []
    assert "/approve" not in tb.HELP_TEXT


# ── replies ──────────────────────────────────────────────────────────────

async def test_long_answers_are_split_below_the_limit_in_order(fake, tmp_path):
    paragraphs = [f"Paragraph {i}: " + ("word " * 60) for i in range(120)]
    rig = Rig(fake, tmp_path)
    rig.backend.answer = lambda req: TurnResult(text="\n\n".join(paragraphs))
    async with rig:
        fake.push(42, "long please")
        assert await until(lambda: "Paragraph 119:" in "".join(fake.texts(42)))
    texts = fake.texts(42)
    assert len(texts) >= 3 and all(len(t) <= 4096 for t in texts)
    joined = "\n".join(texts)
    numbers = [int(n) for n in re.findall(r"Paragraph (\d+):", joined)]
    assert numbers == list(range(120))                      # nothing lost, nothing reordered


async def test_a_long_code_block_is_split_into_valid_blocks(fake, tmp_path):
    code = "```python\n" + "\n".join(f"x{i} = {i} < {i + 1}" for i in range(900)) + "\n```"
    rig = Rig(fake, tmp_path)
    rig.backend.answer = lambda req: TurnResult(text=code)
    async with rig:
        fake.push(42, "code")
        assert await until(lambda: "x899" in "".join(fake.texts(42)))
    assert len(fake.texts(42)) >= 2
    for text in fake.texts(42):
        assert len(text) <= 4096
        assert text.count("<pre>") == text.count("</pre>") >= 1


async def test_html_in_the_answer_is_escaped(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    rig.backend.answer = lambda req: TurnResult(
        text="<script>alert(1)</script> & <b onclick=x>bold?</b> `a<b` [x](https://e.com/?a=1&b=2)")
    async with rig:
        fake.push(42, "inject")
        assert await until(lambda: fake.texts(42))
    sent = fake.texts(42)[0]
    assert "<script>" not in sent and "&lt;script&gt;alert(1)&lt;/script&gt;" in sent
    assert "<b onclick" not in sent and "&lt;b onclick=x&gt;" in sent
    assert "<code>a&lt;b</code>" in sent
    assert '<a href="https://e.com/?a=1&amp;b=2">x</a>' in sent


async def test_when_the_markup_is_rejected_the_same_text_goes_out_plain(fake, tmp_path):
    fake.reject_html_marker = "BADMARKUP"
    rig = Rig(fake, tmp_path)
    rig.backend.answer = lambda req: TurnResult(text="**BADMARKUP** stays whole")
    async with rig:
        fake.push(42, "x")
        assert await until(lambda: len(fake.messages(42)) >= 2)
    first, second = fake.messages(42)[:2]
    assert first.get("parse_mode") == "HTML"
    assert second["text"] == "**BADMARKUP** stays whole" and "parse_mode" not in second


async def test_an_approval_stop_says_what_is_waiting_and_links_the_session(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    card = {"kind": "tool_approval", "question": "Run `rm -rf build` in the workspace?",
            "options": ["Allow for this task", "Allow for this chat session"]}
    rig.backend.answer = lambda req: TurnResult(text="I need to clean the build folder.", approvals=[card],
                                                stop_reason="approval")
    async with rig:
        fake.push(42, "clean up")
        assert await until(lambda: len(fake.texts(42)) >= 2)
    texts = fake.texts(42)
    assert texts[0].startswith("I need to clean")
    waiting = texts[1]
    assert "Waiting for you in Faustus" in waiting and "rm -rf build" in waiting
    assert "http://studio.test/studio?s=sess-1" in waiting
    assert "nothing was approved" in waiting
    assert "/approve" not in waiting


async def test_a_failed_turn_is_reported_not_swallowed(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    rig.backend.answer = lambda req: TurnResult(error="model endpoint refused the request")
    async with rig:
        fake.push(42, "x")
        assert await until(lambda: fake.texts(42))
    assert fake.texts(42) == ["The turn failed: model endpoint refused the request"]


async def test_a_turn_that_cannot_start_says_why(fake, tmp_path):
    from src.chat_bridges.turn import TurnUnavailable
    rig = Rig(fake, tmp_path)

    def boom(req):
        raise TurnUnavailable("no default chat model is configured")
    rig.backend.answer = boom
    async with rig:
        fake.push(42, "x")
        assert await until(lambda: fake.texts(42))
        fake.push(42, "again")                     # the worker survived
        assert await until(lambda: len(fake.texts(42)) >= 2)
    assert "no default chat model is configured" in fake.texts(42)[0]


async def test_an_unexpected_error_in_a_turn_does_not_kill_the_chat(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    calls = []

    def flaky(req):
        calls.append(req.text)
        if len(calls) == 1:
            raise RuntimeError("kaput")
        return TurnResult(text="ok")
    rig.backend.answer = flaky
    async with rig:
        fake.push(42, "first")
        assert await until(lambda: fake.texts(42))
        fake.push(42, "second")
        assert await until(lambda: "ok" in fake.texts(42))
    assert "went wrong" in fake.texts(42)[0]


# ── commands and sessions ────────────────────────────────────────────────

async def test_new_starts_a_fresh_session(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "hello")
        assert await until(lambda: len(fake.texts(42)) >= 1)
        fake.push(42, "/new")
        assert await until(lambda: len(fake.texts(42)) >= 2)
        fake.push(42, "after")
        assert await until(lambda: len(fake.texts(42)) >= 3)
    assert [c["id"] for c in rig.backend.created] == ["sess-1", "sess-2"]
    assert "New conversation started" in fake.texts(42)[1]
    assert "http://studio.test/studio?s=sess-2" in fake.texts(42)[1]
    assert [r.session_id for r in rig.backend.requests] == ["sess-1", "sess-2"]
    assert rig.store.get_session("42")["session_id"] == "sess-2"


async def test_status_and_help_answer_at_once_even_during_a_turn(fake, tmp_path):
    rig = Rig(fake, tmp_path, model="qwen-test")
    rig.backend.gate = asyncio.Event()
    async with rig:
        fake.push(42, "slow one")
        assert await until(lambda: rig.backend.requests)
        fake.push(42, "/status")
        fake.push(42, "/help")
        assert await until(lambda: len(fake.texts(42)) >= 2)
        status, help_text = fake.texts(42)[:2]
        assert "Model: qwen-test" in status and "Mode: agent" in status
        assert "Turn running: yes" in status
        assert "http://studio.test/studio?s=sess-1" in status
        assert help_text == tb.HELP_TEXT
        rig.backend.gate.set()
        assert await until(lambda: any(t.startswith("echo") for t in fake.texts(42)))
    assert rig.backend.max_running == 1


async def test_status_before_any_message_has_no_session(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "/status")
        assert await until(lambda: fake.texts(42))
    assert "none yet" in fake.texts(42)[0] and "Turn running: no" in fake.texts(42)[0]


async def test_session_names_use_the_chat_title_for_groups(fake, tmp_path):
    rig = Rig(fake, tmp_path, allowed=frozenset({"-1001"}))
    async with rig:
        fake.push(-1001, "hi all", chat_type="supergroup", title="Home lab")
        assert await until(lambda: rig.backend.created)
    assert rig.backend.created[0]["name"] == "Telegram · Home lab"


# ── queueing ─────────────────────────────────────────────────────────────

async def test_messages_during_a_turn_are_queued_in_order_one_turn_at_a_time(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    rig.backend.gate = asyncio.Event()
    async with rig:
        fake.push(42, "one")
        assert await until(lambda: rig.backend.requests)
        fake.push(42, "two")
        fake.push(42, "three")
        assert await until(lambda: len([t for t in fake.texts(42) if t.startswith("Queued")]) == 2)
        assert [r.text for r in rig.backend.requests] == ["one"]       # nothing ran in parallel
        rig.backend.gate.set()
        assert await until(lambda: len([t for t in fake.texts(42) if t.startswith("echo")]) == 3)
    assert [r.text for r in rig.backend.requests] == ["one", "two", "three"]
    answers = [t for t in fake.texts(42) if t.startswith("echo")]
    assert answers == ["echo: one", "echo: two", "echo: three"]
    assert rig.backend.max_running == 1
    acks = [t for t in fake.texts(42) if t.startswith("Queued")]
    assert acks == ["Queued: 1 ahead of this message.", "Queued: 2 ahead of this message."]


async def test_two_chats_do_not_wait_for_each_other(fake, tmp_path):
    rig = Rig(fake, tmp_path, allowed=frozenset({"42", "43"}))
    rig.backend.gate = asyncio.Event()
    async with rig:
        fake.push(42, "slow")
        assert await until(lambda: rig.backend.requests)
        fake.push(43, "fast")
        assert await until(lambda: fake.texts(43) == ["echo: fast"])
        rig.backend.gate.set()
        assert await until(lambda: "echo: slow" in fake.texts(42))
    assert rig.backend.max_running == 2


# ── failures ─────────────────────────────────────────────────────────────

async def test_a_401_disables_the_poller_with_a_clear_status(fake, tmp_path, caplog):
    fake.updates_status = 401
    rig = Rig(fake, tmp_path)
    async with rig:
        assert await until(lambda: rig.bridge.disabled_reason)
        assert await until(lambda: not rig.bridge.running)
    assert "401" in rig.bridge.disabled_reason and "token" in rig.bridge.disabled_reason.lower()
    status = rig.bridge.status()
    assert status["running"] is False and "401" in status["disabled_reason"]
    assert rig.sleeps == []                                  # it did not retry a token that is wrong
    assert len(fake.poll_offsets) == 1


async def test_a_wrong_token_at_getme_also_disables_it(fake, tmp_path):
    rig = Rig(fake, tmp_path, token="999:WRONG")
    async with rig:
        assert await until(lambda: rig.bridge.disabled_reason)
    assert "401" in rig.bridge.disabled_reason
    assert rig.bridge.running is False


async def test_network_errors_back_off_1_2_5_10_30_and_do_not_spin(tmp_path):
    # nothing listens here: every call is a connection error
    dead = FakeTelegram()
    url = dead.url
    dead.stop()
    rig = Rig.__new__(Rig)
    rig.fake = dead
    rig.cfg = make_config(dead, api_base=url)
    rig.store = BridgeStore(str(tmp_path / "bridge.sqlite3"))
    rig.backend = FakeBackend()
    rig.sleeps = []

    async def sleep(delay):
        rig.sleeps.append(delay)
        if len(rig.sleeps) >= 8:
            rig.set(enabled=False)              # the poller notices at the top of its loop and stops
        await asyncio.sleep(0)

    rig.bridge = tb.TelegramBridge(config_loader=lambda: rig.cfg, store=rig.store, backend=rig.backend,
                                   sleep=sleep, poll_timeout=1)
    rig.bridge.start()
    assert await until(lambda: len(rig.sleeps) >= 8 and not rig.bridge.running, timeout=10)
    assert rig.sleeps == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0, 30.0]
    assert rig.bridge.last_error and TOKEN not in rig.bridge.last_error
    assert rig.bridge.status()["last_error"]


async def test_server_errors_back_off_and_recovery_resets_the_ladder(fake, tmp_path):
    fake.fail_updates = 2
    rig = Rig(fake, tmp_path)
    async with rig:
        assert await until(lambda: rig.sleeps == [1.0, 2.0])
        fake.push(42, "after the outage")
        assert await until(lambda: fake.texts(42))
        assert rig.bridge.failures == 0
        fake.fail_updates = 1
        assert await until(lambda: rig.sleeps == [1.0, 2.0, 1.0])       # the ladder started again
    assert fake.texts(42) == ["echo: after the outage"]


async def test_the_poll_loop_survives_a_garbage_update(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        with fake.lock:
            fake.updates.append({"update_id": 90, "message": "not a dict"})
            fake.updates.append({"update_id": 91, "edited_message": {"chat": {"id": 42}}})
            fake.updates.append({"update_id": 92})
            fake.updates.append({"update_id": "x"})
        fake.push(42, "still alive")
        assert await until(lambda: fake.texts(42))
    assert fake.texts(42) == ["echo: still alive"]


# ── offset, lifecycle, config ────────────────────────────────────────────

async def test_the_offset_moves_past_handled_updates_and_survives_a_restart(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        fake.push(42, "one")
        assert await until(lambda: fake.texts(42))
        assert await until(lambda: any(o and o > 100 for o in fake.poll_offsets))
    saved = int(rig.store.get_meta("offset"))
    assert saved == fake.next_id + 1
    # a new poller on the same store asks for the offset it left off at
    fake.poll_offsets.clear()
    again = Rig(fake, tmp_path)
    async with again:
        assert await until(lambda: fake.poll_offsets)
    assert fake.poll_offsets[0] == saved
    assert [r.text for r in rig.backend.requests] == ["one"] and again.backend.requests == []


async def test_stop_is_clean_and_status_says_not_running(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    await rig.__aenter__()
    assert await until(lambda: rig.bridge.running)
    await rig.bridge.stop()
    assert rig.bridge.running is False and rig.bridge.status()["running"] is False
    assert rig.bridge._chats == {}


async def test_stop_while_a_turn_runs_cancels_it(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    started = asyncio.Event()
    cancelled = []

    async def slow(req):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    rig.backend.answer = slow
    await rig.__aenter__()
    fake.push(42, "long job")
    await asyncio.wait_for(started.wait(), 6)
    await rig.bridge.stop()
    assert cancelled == [True]


async def test_it_does_not_start_when_disabled_or_without_a_token(fake, tmp_path):
    for over in ({"enabled": False}, {"token": ""}):
        rig = Rig(fake, tmp_path, **over)
        rig.bridge.start()
        await asyncio.sleep(0.2)
        assert rig.bridge._task.done() and not rig.bridge.running
        await rig.bridge.stop()
    assert fake.poll_offsets == []


async def test_the_token_is_not_in_the_status_or_the_errors(fake, tmp_path):
    rig = Rig(fake, tmp_path)
    async with rig:
        assert await until(lambda: rig.bridge.bot_username)
        blob = json.dumps(rig.bridge.status())
    assert TOKEN not in blob and "SECRET-TOKEN" not in blob
    api = tb.TelegramApi(TOKEN, fake.url)
    assert TOKEN not in api._clean(f"connection failed for {fake.url}/bot{TOKEN}/getMe")


def test_load_config_reads_the_settings(monkeypatch):
    from src import settings as settings_mod
    from src.secret_storage import encrypt
    values = {
        "telegram_bridge_enabled": True,
        "telegram_bot_token": encrypt("111:abc"),
        "telegram_allowed_chat_ids": [42, " -1001 ", "", "7"],
        "telegram_agent_mode": "CHAT",
        "telegram_model": " qwen ",
        "telegram_owner": "ada",
        "telegram_api_base": "",
        "app_public_url": "https://faustus.example/",
    }
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: values.get(key, default))
    cfg = tb.load_config()
    assert cfg.enabled and cfg.configured
    assert cfg.token == "111:abc"                          # stored encrypted, read back decrypted
    assert cfg.allowed == frozenset({"42", "-1001", "7"})
    assert (cfg.mode, cfg.model, cfg.owner) == ("chat", "qwen", "ada")
    assert cfg.api_base == "https://api.telegram.org"
    assert cfg.public_url == "https://faustus.example"


def test_load_config_accepts_a_plaintext_token_and_defaults_the_link(monkeypatch):
    from src import settings as settings_mod
    values = {"telegram_bridge_enabled": True, "telegram_bot_token": "222:plain",
              "telegram_allowed_chat_ids": "42, 43", "telegram_agent_mode": "nonsense"}
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.delenv("ODYSSEUS_INTERNAL_BASE", raising=False)
    monkeypatch.setenv("APP_PORT", "7123")
    cfg = tb.load_config()
    assert cfg.token == "222:plain" and cfg.allowed == frozenset({"42", "43"}) and cfg.mode == "agent"
    assert cfg.public_url == "http://127.0.0.1:7123"


def test_off_by_default(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    cfg = tb.load_config()
    assert cfg.enabled is False and cfg.configured is False and cfg.allowed == frozenset()
