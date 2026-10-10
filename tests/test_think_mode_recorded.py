# -*- coding: utf-8 -*-
"""Auto reads what the previous turn really ran with, and stops asking for deep
after a deep pass that ended without an answer (OBJ-58, Agora #227)."""

from types import SimpleNamespace

from core.models import ChatMessage
from routes.chat_helpers import save_assistant_response
from src.think_mode import decide, prior_signals


def _saved(*items):
    """Session history as saved: ChatMessage objects with metadata."""
    return [ChatMessage(role, content, metadata=meta) for role, content, meta in items]


def test_a_mode_picked_by_hand_counts_as_the_previous_mode():
    # The rule alone would call "hola" fast; the saved reply says it ran deep.
    history = _saved(("user", "hola", None),
                     ("assistant", "¡Hola!", {"think_mode": {"mode": "deep", "source": "explicit"}}),
                     ("user", "no, eso está mal", None))
    sig = prior_signals([{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola!"},
                         {"role": "user", "content": "no, eso está mal"}], "no, eso está mal", history)
    assert sig["prev_mode"] == "deep" and sig["prev_mode_source"] == "recorded"
    # A correction after a recorded full pass goes deep, not just think.
    assert decide("no, eso está mal", history_len=2, prior=sig)["mode"] == "deep"


def test_a_low_effort_think_is_recorded_as_light():
    history = _saved(("user", "x", None),
                     ("assistant", "y", {"think_mode": {"mode": "think", "effort": "low", "source": "rule"}}))
    assert prior_signals([], "", history)["prev_mode"] == "light"


def test_without_a_record_the_rule_still_decides():
    history = _saved(("user", "hola", None), ("assistant", "¡Hola!", None))
    sig = prior_signals([{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola!"}], "", history)
    assert sig["prev_mode"] == "fast" and sig["prev_mode_source"] == "rule"


def test_deep_that_spent_its_budget_without_an_answer_is_not_asked_again():
    history = _saved(
        ("user", "demuestra que hay infinitos primos de la forma 4k+3", None),
        ("assistant", "<think>muy largo…", {"think_mode": {"mode": "deep", "source": "rule"}, "thinking": "muy largo"}),
        ("user", "demuestra paso a paso, con todo el rigor, que hay infinitos primos 4k+3", None))
    current = "demuestra paso a paso, con todo el rigor, que hay infinitos primos 4k+3"
    sig = prior_signals([], current, history)
    assert sig["deep_without_answer"] is True and sig["empty_answer"] is True
    base = decide(current, history_len=2)
    out = decide(current, history_len=2, prior=sig)
    if base["mode"] == "deep":
        assert out["mode"] == "think" and "deep_spent_without_answer" in out["reasons"]
    assert out["mode"] != "deep"
    # a correction afterwards does not escalate back to deep either
    assert decide("no, eso está mal", history_len=3, prior=sig)["mode"] != "deep"


def test_a_deep_turn_that_answered_does_not_block_deep():
    history = _saved(("user", "q", None),
                     ("assistant", "La respuesta es 42.", {"think_mode": {"mode": "deep"}, "thinking": "…"}))
    assert prior_signals([], "", history)["deep_without_answer"] is False


def test_the_saved_reply_carries_the_mode_the_turn_ran_with():
    added = []
    sess = SimpleNamespace(model="m", owner="o", history=[], add_message=lambda m: added.append(m),
                           _turn_think_mode={"mode": "think", "requested": "auto", "source": "rule",
                                             "budget": 1024, "effort": "low"})
    manager = SimpleNamespace(save_session=lambda *a, **k: None, update_session=lambda *a, **k: None)
    try:
        save_assistant_response(sess, manager, "sid", "Hecho.", {"model": "m"})
    except Exception:
        pass  # persistence details vary; the message object is what matters here
    msgs = added or [m for m in sess.history if getattr(m, "role", "") == "assistant"]
    assert msgs, "the assistant message was not built"
    meta = msgs[-1].metadata or {}
    assert meta["think_mode"] == {"mode": "think", "requested": "auto", "source": "rule", "budget": 1024, "effort": "low"}
    assert sess._turn_think_mode is None, "the record belongs to one turn only"
