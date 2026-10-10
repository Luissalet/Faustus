# -*- coding: utf-8 -*-
"""A turn that reasoned and never answered keeps a durable, empty record that
Auto reads on the next turn (OBJ-58, Agora #227 r3). Unit level: the helper, the
prompt filter and what `prior_signals` makes of the row. The end-to-end flow
through the real API is `tests/test_think_mode_empty_answer_api.py`."""

from types import SimpleNamespace

import pytest

from core.models import ChatMessage, Session
from routes import chat_helpers
from routes.chat_helpers import last_finish_reason, save_empty_answer_record
from src.think_mode import decide, prior_signals

DEEP = {"mode": "deep", "requested": "deep", "source": "explicit", "budget": 16384, "effort": "high"}


@pytest.fixture
def stubbed(monkeypatch):
    import core.database as db

    monkeypatch.setattr(db, "update_session_last_accessed", lambda *a, **k: None)
    saved = []
    sess = SimpleNamespace(model="qwen3-fake", owner="o", history=[], _turn_think_mode=dict(DEEP))

    def add_message(message):
        message.metadata["_db_id"] = f"row-{len(sess.history)}"
        sess.history.append(message)

    sess.add_message = add_message
    manager = SimpleNamespace(save_sessions=lambda: saved.append(1))
    return sess, manager, saved


def test_a_reasoning_only_turn_is_saved_as_an_empty_reply_with_its_outcome(stubbed):
    sess, manager, saved = stubbed
    out = save_empty_answer_record(sess, manager, "sid", {"model": "qwen3-fake"},
                                   thinking="muy largo", finish_reason="length")
    assert out == {"id": "row-0", "reason": "length", "finish_reason": "length"}
    (row,) = sess.history
    assert row.role == "assistant" and row.content == "", "no answer text is invented"
    md = row.metadata
    assert md["empty_answer"] is True and md["empty_answer_reason"] == "length"
    assert md["finish_reason"] == "length" and md["thinking"] == "muy largo"
    assert md["think_mode"] == {"mode": "deep", "requested": "deep", "source": "explicit",
                                "budget": 16384, "effort": "high"}
    assert saved, "the session is persisted"
    assert sess._turn_think_mode is None, "the stamp belongs to one turn only"


def test_a_stop_mid_reasoning_is_marked_as_stopped(stubbed):
    sess, manager, _ = stubbed
    out = save_empty_answer_record(sess, manager, "sid", {}, thinking="a medias", stopped=True)
    assert out["reason"] == "stopped"
    md = sess.history[0].metadata
    assert md["stopped"] is True and md["finish_reason"] == "stopped" and md["empty_answer_reason"] == "stopped"


def test_reasoning_that_ended_for_another_reason_is_other(stubbed):
    sess, manager, _ = stubbed
    out = save_empty_answer_record(sess, manager, "sid", {}, thinking="x", finish_reason="stop")
    assert out["reason"] == "other"


def test_incognito_persists_nothing(stubbed):
    sess, manager, saved = stubbed
    assert save_empty_answer_record(sess, manager, "sid", {}, thinking="x", finish_reason="length",
                                    incognito=True) is None
    assert sess.history == [] and not saved


def test_a_turn_that_neither_reasoned_nor_ran_out_of_tokens_keeps_its_old_behaviour(stubbed):
    sess, manager, _ = stubbed
    assert save_empty_answer_record(sess, manager, "sid", {}, thinking="  ", finish_reason="stop") is None
    assert sess.history == []


def test_running_out_of_tokens_without_any_reasoning_is_still_recorded(stubbed):
    sess, manager, _ = stubbed
    out = save_empty_answer_record(sess, manager, "sid", {}, finish_reason="length")
    assert out and sess.history[0].metadata["empty_answer_reason"] == "length"
    assert "thinking" not in sess.history[0].metadata


def test_a_failure_is_swallowed(stubbed):
    sess, manager, _ = stubbed
    sess.add_message = lambda m: (_ for _ in ()).throw(RuntimeError("db down"))
    assert save_empty_answer_record(sess, manager, "sid", {}, thinking="x", finish_reason="length") is None


def test_the_empty_row_never_reaches_the_prompt():
    sess = Session(id="s", name="n", endpoint_url="http://x", model="m")
    sess.history = [
        ChatMessage("user", "demuestra", None),
        ChatMessage("assistant", "", {"empty_answer": True, "think_mode": DEEP}),
        ChatMessage("user", "otra vez", None),
    ]
    assert [m["role"] for m in sess.get_context_messages()] == ["user", "user"]


def test_last_finish_reason_reads_the_agent_rounds_or_the_plain_field():
    assert last_finish_reason({"harness": {"finish_reasons": ["tool_calls", None, "length"]}}) == "length"
    assert last_finish_reason({"finish_reason": "STOP"}) == "stop"
    assert last_finish_reason({"harness": {"finish_reasons": []}}) == ""
    assert last_finish_reason(None) == "" and last_finish_reason({"harness": "x"}) == ""


def test_auto_reads_the_empty_row_as_a_spent_deep_pass():
    record = ChatMessage("assistant", "", {"empty_answer": True, "think_mode": DEEP, "finish_reason": "length"})
    history = [ChatMessage("user", "demuestra con todo el rigor que hay infinitos primos 4k+3", None), record]
    sig = prior_signals([], "demuestra con todo el rigor, paso a paso, que hay infinitos primos 4k+3", history)
    assert sig["deep_without_answer"] is True and sig["empty_answer"] is True
    assert sig["prev_mode"] == "deep" and sig["prev_mode_source"] == "recorded"
    out = decide("demuestra con todo el rigor, paso a paso, que hay infinitos primos 4k+3", history_len=2, prior=sig)
    assert out["mode"] != "deep"


def test_an_empty_row_without_think_mode_still_counts_as_no_answer_but_not_as_deep():
    record = ChatMessage("assistant", "", {"empty_answer": True, "finish_reason": "length"})
    sig = prior_signals([], "", [ChatMessage("user", "q", None), record])
    assert sig["empty_answer"] is True and sig["deep_without_answer"] is False


def test_the_module_exports_what_the_routes_import():
    assert chat_helpers.save_empty_answer_record is save_empty_answer_record
