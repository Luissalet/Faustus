"""src/chat_bridges/turn.py: what a Telegram message does inside Faustus.

The session manager and the agent loop are replaced by small fakes; what is
checked is what the module itself decides: the incoming text is saved as
outside text (so the tool gate arms), the answer and its tool cards are saved
the way a background follow-up saves them, a card that stops the turn is
reported and never approved, only generated images are returned, and the
account that owns the conversation is the configured one or the first admin.
"""
import asyncio
import json
import os

import pytest

from core.models import Session
from src.chat_bridges import turn as turn_mod
from src.chat_bridges.turn import TurnRequest


class FakeManager:
    def __init__(self):
        self.sessions = {}
        self.messages = []

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None, **kw):
        sess = Session(id=session_id, name=name, endpoint_url=endpoint_url, model=model, owner=owner)
        self.sessions[session_id] = sess
        return sess

    def get_session(self, sid):
        return self.sessions[sid]

    def add_message(self, sid, message):
        self.sessions[sid].history.append(message)
        self.messages.append((sid, message))


def sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


@pytest.fixture
def world(monkeypatch):
    sm = FakeManager()
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: sm)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda prefix, owner=None, **kw: ("http://models.test/v1/chat/completions", "qwen-default", {"Authorization": "Bearer k"}))
    persisted = []
    monkeypatch.setattr("routes.session_routes._persist_session_headers", lambda sid, headers: persisted.append((sid, headers)))
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [])
    busy = []
    monkeypatch.setattr(agent_runs, "mark_busy", lambda sid: busy.append(("mark", sid)))
    monkeypatch.setattr(agent_runs, "clear_busy", lambda sid: busy.append(("clear", sid)))
    sm.persisted, sm.busy = persisted, busy
    return sm


def script(monkeypatch, events, *, capture=None):
    async def fake_loop(url, model, messages, **kwargs):
        if capture is not None:
            capture.update(url=url, model=model, messages=messages, kwargs=kwargs)
        for event in events:
            if callable(event):
                await event()
            else:
                yield event if isinstance(event, str) else sse(event)
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)


def new_session(world, owner="alice"):
    sid = turn_mod.create_session(owner, "Telegram · Ada", "")
    return sid


# ── sessions ─────────────────────────────────────────────────────────────

def test_create_session_names_it_and_uses_the_default_route(world):
    sid = turn_mod.create_session("alice", "Telegram · " + "x" * 300, "")
    sess = world.sessions[sid]
    assert sess.name.startswith("Telegram · ") and len(sess.name) == 120
    assert (sess.owner, sess.model, sess.endpoint_url) == ("alice", "qwen-default", "http://models.test/v1/chat/completions")
    assert world.persisted == [(sid, {"Authorization": "Bearer k"})]
    other = turn_mod.create_session("alice", "n", " qwen-big ")
    assert world.sessions[other].model == "qwen-big"
    assert turn_mod.session_exists(sid, "alice") and not turn_mod.session_exists(sid, "mallory")
    assert not turn_mod.session_exists("nope", "alice")


def test_no_default_model_is_a_clear_error(world, monkeypatch):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", lambda *a, **k: (None, None, None))
    with pytest.raises(turn_mod.TurnUnavailable, match="no default chat model"):
        turn_mod.create_session("alice", "n", "")
    assert turn_mod.default_model_name("alice", "") == ""
    assert turn_mod.default_model_name("alice", "m") == "m"


def test_turn_backend_status_helpers(world):
    from src.chat_bridges.telegram_bridge import TurnBackend
    sid = new_session(world)
    backend = TurnBackend()
    assert backend.model_name("alice", "", sid) == "qwen-default"
    assert backend.model_name("alice", "pinned", sid) == "pinned"


# ── a normal turn ────────────────────────────────────────────────────────

async def test_a_turn_is_saved_in_the_session_as_outside_text(world, monkeypatch):
    sid = new_session(world)
    seen = {}
    script(monkeypatch, [{"delta": "thinking..", "thinking": True}, {"type": "agent_step", "round": 1},
                         {"type": "tool_output", "tool": "web_search", "command": "q", "output": "results", "exit_code": 0},
                         {"delta": "The "}, {"delta": "answer"}, {"type": "metrics", "data": {"tokens": 5}}, "data: [DONE]\n\n"],
                capture=seen)
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="what is up?"))
    assert result.text == "The answer" and result.error == "" and result.tool_calls == 1
    assert result.stop_reason == "" and result.model == "qwen-default"
    user, assistant = [m for _, m in world.messages]
    assert (user.role, user.content) == ("user", "what is up?")
    # the marker the tool gate reads: a state-changing tool will stop at an approval card
    assert user.metadata["trusted"] is False and user.metadata["tool_gate_untrusted"] is True
    assert user.metadata["provenance_origin"] == "external" and user.metadata["source"] == "telegram"
    assert assistant.role == "assistant" and assistant.content == "The answer"
    assert assistant.metadata["tool_events"][0]["tool"] == "web_search"
    assert assistant.metadata["model"] == "qwen-default"
    # the loop saw this session's history including the new message, as this owner, on this session
    assert seen["messages"][-1]["content"] == "what is up?"
    assert seen["kwargs"]["session_id"] == sid and seen["kwargs"]["owner"] == "alice"
    assert "security_gate_bypass" not in seen["kwargs"]             # nothing opts out of the gate
    # the session was marked busy for the length of the turn only
    assert world.busy == [("mark", sid), ("clear", sid)]


async def test_a_card_that_stops_the_turn_is_reported_and_kept_for_studio(world, monkeypatch):
    sid = new_session(world)
    card = {"kind": "tool_approval", "approval_id": "ap1", "session_id": sid, "question": "Allow bash: rm -rf build?",
            "description": "This run read outside text.",
            "options": [{"label": "Allow for this task", "value": "task"}, {"label": "Deny", "value": "deny"}]}
    script(monkeypatch, [{"delta": "I will clean up."},
                         {"type": "tool_output", "tool": "bash", "command": "rm -rf build", "output": "Waiting for an exact user approval.",
                          "exit_code": None, "ask_user": card},
                         {"type": "ask_user", "data": card}])
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="clean"))
    assert result.stop_reason == "approval"
    assert result.approvals == [{"kind": "tool_approval", "question": "Allow bash: rm -rf build?",
                                 "options": ["Allow for this task", "Deny"]}]       # once, however many events carried it
    saved = world.messages[-1][1]
    assert saved.metadata["tool_events"][0]["ask_user"]["approval_id"] == "ap1"       # Studio rebuilds the card from this


async def test_only_generated_images_are_returned(world, monkeypatch):
    from src.constants import GENERATED_IMAGES_DIR
    os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)
    good = os.path.join(GENERATED_IMAGES_DIR, "abcdef0123456789.png")
    with open(good, "wb") as handle:
        handle.write(b"\x89PNG")
    try:
        sid = new_session(world)
        script(monkeypatch, [
            {"type": "generated_image", "url": "/api/generated-image/abcdef0123456789.png"},
            {"type": "generated_image", "url": "/api/generated-image/abcdef0123456789.png"},   # twice: once
            {"type": "generated_image", "url": "/api/generated-image/ffffffffffff.png"},       # missing file
            {"type": "generated_image", "url": "/api/generated-image/..%2F..%2Fetc%2Fpasswd"},
            {"type": "generated_image", "url": "/etc/passwd"},
            {"type": "generated_image", "url": "file:///etc/passwd"},
            {"delta": "done"}])
        result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="draw"))
        assert result.images == [os.path.realpath(good)] or result.images == [good]
        assert len(result.images) == 1
    finally:
        os.remove(good)


def test_local_image_path_refuses_other_files(tmp_path):
    other = tmp_path / "secret.png"
    other.write_bytes(b"x")
    assert turn_mod.local_image_path(str(other)) is None
    assert turn_mod.local_image_path("") is None
    assert turn_mod.local_image_path("/api/generated-image/notes.txt") is None


async def test_chat_mode_asks_the_model_alone(world, monkeypatch):
    sid = new_session(world)
    called = {}

    async def fake_llm(url, model, messages, headers=None, **kw):
        called.update(url=url, model=model, messages=messages, headers=headers)
        return "plain reply"

    def no_loop(*a, **k):
        raise AssertionError("chat mode must not start the agent loop")
    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", no_loop)
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi", mode="chat"))
    assert result.text == "plain reply" and result.tool_calls == 0
    assert called["messages"][-1]["content"] == "hi"
    assert [m.role for _, m in world.messages] == ["user", "assistant"]


async def test_a_model_override_runs_on_the_default_endpoint(world, monkeypatch):
    sid = new_session(world)
    seen = {}
    script(monkeypatch, [{"delta": "ok"}], capture=seen)
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi", model="qwen-big"))
    assert seen["model"] == "qwen-big" and result.model == "qwen-big"


async def test_an_error_event_keeps_the_partial_answer(world, monkeypatch):
    sid = new_session(world)
    script(monkeypatch, [{"delta": "half an ans"}, {"type": "error", "message": "model endpoint went away"}])
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi"))
    assert result.text == "half an ans" and result.error == "model endpoint went away"
    assert world.messages[-1][1].content == "half an ans"


async def test_an_exception_in_the_loop_is_a_result_not_a_crash(world, monkeypatch):
    sid = new_session(world)

    async def boom(url, model, messages, **kw):
        yield sse({"delta": "partial"})
        raise RuntimeError("kaput")
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", boom)
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi"))
    assert result.text == "partial" and "RuntimeError: kaput" in result.error
    assert world.busy[-1] == ("clear", sid)


async def test_a_turn_that_takes_too_long_is_stopped(world, monkeypatch):
    sid = new_session(world)

    async def stuck():
        await asyncio.sleep(30)
    script(monkeypatch, [{"delta": "so far"}, stuck])
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi", timeout_s=1))
    assert result.stop_reason == "timeout" and "too long" in result.error and result.text == "so far"


async def test_a_session_with_a_turn_running_in_studio_is_not_written_to(world, monkeypatch):
    sid = new_session(world)
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [sid])
    result = await turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi"))
    assert "running in Studio" in result.error
    assert world.messages == []


async def test_a_deleted_session_is_unavailable(world):
    with pytest.raises(turn_mod.TurnUnavailable, match="no longer exists"):
        await turn_mod.run_turn(TurnRequest(owner="alice", session_id="gone", text="hi"))


async def test_cancelling_a_turn_clears_the_busy_mark(world, monkeypatch):
    sid = new_session(world)
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.sleep(30)
    script(monkeypatch, [stuck])
    task = asyncio.ensure_future(turn_mod.run_turn(TurnRequest(owner="alice", session_id=sid, text="hi")))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.busy[-1] == ("clear", sid)


# ── whose conversation it is ─────────────────────────────────────────────

def _auth_file(tmp_path, monkeypatch, users):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"users": users}))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(path))
    monkeypatch.setattr("src.event_bus.AUTH_FILE", str(path))


def test_the_owner_is_the_configured_account_or_the_first_admin(tmp_path, monkeypatch):
    _auth_file(tmp_path, monkeypatch, {"bob": {"is_admin": False}, "alice": {"is_admin": True}, "carol": {"is_admin": True}})
    assert turn_mod.resolve_owner("BOB") == "bob"              # the configured one, as spelled in the account list
    assert turn_mod.resolve_owner("") == "alice"                # the first administrator
    assert turn_mod.resolve_owner("nobody") == "alice"          # an unknown name never creates an owner


def test_with_login_switched_off_the_local_owner_is_used(tmp_path, monkeypatch):
    _auth_file(tmp_path, monkeypatch, {})
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src.owner_identity import DEFAULT_LOCAL_OWNER
    assert turn_mod.resolve_owner("") == DEFAULT_LOCAL_OWNER
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert turn_mod.resolve_owner("") == ""                     # nobody to own it: the bridge says so
