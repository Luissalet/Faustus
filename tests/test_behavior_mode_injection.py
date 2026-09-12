"""CONTRATO_MODOS Lote A — injection tests.

Two layers:

1. `src.chat_processor.ChatProcessor.build_context_preface` directly — the
   ordering guarantee itself (mode block strictly between the preset and
   `UNTRUSTED_CONTEXT_POLICY`) lives there, so it is tested there, on the
   real function, no fakes.
2. `routes.chat_helpers.build_chat_context` — that resolution actually
   happens on a real turn (request > session > global default > "default"),
   reaches `chat_processor.build_context_preface` as `behavior_mode_block`,
   survives agent mode / incognito, and a request for an unknown mode id
   never raises. Same fake-preprocessing harness `tests/test_chat_helpers.py`
   uses, so a real model is never called.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import routes.chat_helpers as chat_helpers
from routes.chat_helpers import build_chat_context, PreprocessedMessage, PresetInfo
from src.chat_processor import ChatProcessor
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY


class _Memory:
    def load(self, owner=None):
        return []


class _Docs:
    rag_manager = None


def _processor():
    return ChatProcessor(memory_manager=_Memory(), personal_docs_manager=_Docs())


# ── Layer 1: ChatProcessor.build_context_preface ordering ──────────────── #

def test_adversarial_block_sits_between_preset_and_policy():
    from src import behavior_modes
    mode = behavior_modes.get_mode("adversarial")
    block = behavior_modes.system_block(mode)

    preface, _, _ = _processor().build_context_preface(
        message="hello",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=False,
        preset_system_prompt="PRESET TEXT",
        behavior_mode_block=block,
    )
    roles_content = [(m["role"], m["content"]) for m in preface[:3]]
    assert roles_content[0] == ("system", "PRESET TEXT")
    assert roles_content[1][0] == "system"
    assert "Behaviour mode" in roles_content[1][1]
    assert roles_content[2] == ("system", UNTRUSTED_CONTEXT_POLICY)


def test_default_mode_adds_no_block():
    from src import behavior_modes
    mode = behavior_modes.get_mode("default")
    block = behavior_modes.system_block(mode)
    assert block is None

    preface, _, _ = _processor().build_context_preface(
        message="hello",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=False,
        preset_system_prompt="PRESET TEXT",
        behavior_mode_block=block,
    )
    assert [m["content"] for m in preface[:2]] == ["PRESET TEXT", UNTRUSTED_CONTEXT_POLICY]


def test_no_preset_mode_block_still_precedes_policy():
    preface, _, _ = _processor().build_context_preface(
        message="hello",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=False,
        behavior_mode_block='Behaviour mode "Terse":\nAnswer first.',
    )
    assert preface[0]["content"].startswith('Behaviour mode "Terse"')
    assert preface[1]["content"] == UNTRUSTED_CONTEXT_POLICY


def test_absent_behavior_mode_block_kwarg_is_backward_compatible():
    """Existing callers that never pass `behavior_mode_block` at all must
    behave exactly as before this contract."""
    preface, _, _ = _processor().build_context_preface(
        message="hello", session=SimpleNamespace(), use_rag=False, use_memory=False,
    )
    assert preface[0]["content"] == UNTRUSTED_CONTEXT_POLICY


# ── Layer 2: build_chat_context resolves + threads it through ──────────── #

def _harness(monkeypatch, *, session_behavior_mode=None):
    """Same fake-everything harness `tests/test_chat_helpers.py`'s
    `_build_context_owner_probe` uses, extended to capture whatever
    `chat_processor.build_context_preface` was called with."""
    captured: dict = {}

    async def fake_preprocess(chat_handler, message, att_ids, sess, **kwargs):
        return PreprocessedMessage(
            enhanced_message=message, user_content=message, text_for_context=message,
            youtube_transcripts=[], attachment_meta=[],
        )

    def fake_extract_preset(chat_handler, preset_id):
        return PresetInfo(temperature=0.7, max_tokens=1024, system_prompt=None, character_name=None)

    def fake_add_user_message(sess, chat_handler, preprocessed, incognito=False):
        sess.messages.append({"role": "user", "content": preprocessed.user_content})

    def fake_load_prefs(owner):
        return {"memory_enabled": True, "skills_enabled": True}

    def fake_build_context_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    async def fake_maybe_compact(sess, endpoint_url, model, messages, headers, owner=None):
        return messages, 8192, False

    monkeypatch.setattr(chat_helpers, "preprocess", fake_preprocess)
    monkeypatch.setattr(chat_helpers, "extract_preset", fake_extract_preset)
    monkeypatch.setattr(chat_helpers, "add_user_message", fake_add_user_message)
    monkeypatch.setattr(chat_helpers, "load_prefs_for_user", fake_load_prefs)
    monkeypatch.setattr(chat_helpers, "_normalize_model_id_from_cache", lambda sess: None)
    monkeypatch.setattr(chat_helpers, "normalize_model_id", lambda endpoint_url, model, **kwargs: None)
    monkeypatch.setattr(chat_helpers, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(chat_helpers, "trim_for_context", lambda messages, context_length: messages)
    monkeypatch.setattr(chat_helpers, "get_session_behavior_mode", lambda session_id: session_behavior_mode)
    monkeypatch.setattr(chat_helpers, "effective_user", lambda request: "alice")

    import src.user_time as user_time
    monkeypatch.setattr(
        user_time, "current_datetime_context_message",
        lambda now_utc=None: {"role": "user", "content": "[Context - current date/time]"},
        raising=False,
    )

    sess = SimpleNamespace(
        endpoint_url="http://model.local/v1/chat/completions",
        model="test-model", headers={}, history=[], messages=[],
    )
    sess.get_context_messages = lambda: list(sess.messages)
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice", api_token=False))
    return sess, request, captured


@pytest.mark.asyncio
async def test_requested_mode_resolves_and_reaches_preface_kwargs(monkeypatch):
    sess, request, captured = _harness(monkeypatch)

    def fake_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=SimpleNamespace(),
        chat_processor=SimpleNamespace(build_context_preface=fake_preface),
        message="hello", session_id="s1", behavior_mode="adversarial",
    )
    assert ctx.behavior_mode == "adversarial"
    assert captured["kwargs"]["behavior_mode_block"] is not None
    assert 'Behaviour mode "Adversarial"' in captured["kwargs"]["behavior_mode_block"]


@pytest.mark.asyncio
async def test_default_mode_sends_no_block(monkeypatch):
    sess, request, captured = _harness(monkeypatch)

    def fake_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=SimpleNamespace(),
        chat_processor=SimpleNamespace(build_context_preface=fake_preface),
        message="hello", session_id="s1",
    )
    assert ctx.behavior_mode == "default"
    assert captured["kwargs"]["behavior_mode_block"] is None


@pytest.mark.asyncio
async def test_session_mode_used_when_no_request_override(monkeypatch):
    sess, request, captured = _harness(monkeypatch, session_behavior_mode="terse")

    def fake_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=SimpleNamespace(),
        chat_processor=SimpleNamespace(build_context_preface=fake_preface),
        message="hello", session_id="s1",
    )
    assert ctx.behavior_mode == "terse"
    assert captured["kwargs"]["behavior_mode_block"].startswith('Behaviour mode "Terse"')


@pytest.mark.asyncio
async def test_agent_mode_and_incognito_still_get_the_block(monkeypatch):
    sess, request, captured = _harness(monkeypatch)

    def fake_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=SimpleNamespace(),
        chat_processor=SimpleNamespace(build_context_preface=fake_preface),
        message="hello", session_id="s1", behavior_mode="adversarial",
        agent_mode=True, incognito=True,
    )
    assert ctx.behavior_mode == "adversarial"
    assert captured["kwargs"]["behavior_mode_block"] is not None


@pytest.mark.asyncio
async def test_unknown_mode_id_never_breaks_the_turn(monkeypatch):
    sess, request, captured = _harness(monkeypatch)

    def fake_preface(**kwargs):
        captured["kwargs"] = kwargs
        return [], [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=SimpleNamespace(),
        chat_processor=SimpleNamespace(build_context_preface=fake_preface),
        message="hello", session_id="s1", behavior_mode="not_a_real_mode_xyz",
    )
    # No exception, and it fell all the way through to "default".
    assert ctx.behavior_mode == "default"
    assert captured["kwargs"]["behavior_mode_block"] is None


# ── metadata.behavior_mode / metadata.mode_check on the saved reply ────── #

def _fake_session(owner=None):
    added = []
    sess = SimpleNamespace(owner=owner, history=added, add_message=lambda m: added.append(m))
    return sess, added


def test_save_assistant_response_stamps_behavior_mode_and_mode_check():
    from routes.chat_helpers import save_assistant_response

    sess, added = _fake_session()
    save_assistant_response(
        sess, SimpleNamespace(save_sessions=lambda: None), "sess-1",
        "Great question, absolutely.", {},
        behavior_mode="adversarial",
    )
    assert len(added) == 1
    md = added[0].metadata
    assert md["behavior_mode"] == "adversarial"
    assert "mode_check" in md
    assert md["mode_check"]["checked"]
    assert any(v["rule"] == "forbidden_phrases" for v in md["mode_check"]["violations"])


def test_save_assistant_response_default_mode_stamps_nothing():
    from routes.chat_helpers import save_assistant_response

    sess, added = _fake_session()
    save_assistant_response(
        sess, SimpleNamespace(save_sessions=lambda: None), "sess-1",
        "Whatever the reply is.", {},
        behavior_mode="default",
    )
    md = added[0].metadata
    assert "behavior_mode" not in md
    assert "mode_check" not in md


def test_save_assistant_response_mode_with_no_checks_stamps_id_but_not_mode_check():
    from routes.chat_helpers import save_assistant_response

    sess, added = _fake_session()
    save_assistant_response(
        sess, SimpleNamespace(save_sessions=lambda: None), "sess-1",
        "A perfectly ordinary reply.", {},
        behavior_mode="editor",  # editor.json ships with checks: {}
    )
    md = added[0].metadata
    assert md["behavior_mode"] == "editor"
    assert "mode_check" not in md


def test_save_assistant_response_unknown_behavior_mode_never_raises():
    from routes.chat_helpers import save_assistant_response

    sess, added = _fake_session()
    save_assistant_response(
        sess, SimpleNamespace(save_sessions=lambda: None), "sess-1",
        "Some reply.", {},
        behavior_mode="not_a_real_mode_xyz",
    )
    # Stamped verbatim (it's the id the turn actually resolved to elsewhere);
    # this call just could not find it to run checks against, and didn't blow up.
    md = added[0].metadata
    assert md["behavior_mode"] == "not_a_real_mode_xyz"
    assert "mode_check" not in md
