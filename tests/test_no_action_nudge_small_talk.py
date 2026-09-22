"""A greeting is not a shirked job.

Found by using the app, with a workspace bound. "Hola, ¿qué eres?" was
answered correctly, and the harness then told the model:

    The original request requires work in the active workspace, but your last
    response performed no tool call.

The original request was "hola". The model dutifully went round again,
produced a second greeting, and because round 1's text is kept and round 2's
appended, the user got both in one message run together mid-sentence:

    ...¿En qué te ayudo?¡Hola! Soy Faustus, tu asistente de IA que corre...

The trigger was `bool(workspace)` on its own -- a bound folder is most of the
time, so every prose-only reply looked like a job avoided.
"""
import pytest

from src.agent_loop import _conversational_turn


@pytest.mark.parametrize("text", [
    "hola",
    "Hola, ¿qué eres?",
    "gracias",
    "what can you do",
])
def test_these_turns_have_nothing_to_do(text):
    assert _conversational_turn(text) is True


@pytest.mark.parametrize("text", [
    "arregla el test que falla",
    "lee src/llm_core.py",
    "implementa el endpoint nuevo",
    "hola, arregla el bug",
])
def test_these_turns_do_have_work(text):
    """The nudge exists for exactly these: an acknowledgement is not
    completion, and taking the guard away from them would be worse than the
    bug it fixes."""
    assert _conversational_turn(text) is False


def test_empty_text_is_not_small_talk():
    """No signal is not a licence to skip the check."""
    assert _conversational_turn("") is False
    assert _conversational_turn(None) is False


def test_the_two_guards_agree():
    """The harness check and the nudge both had this bug, and both now read
    the same whitelist -- if they disagreed, a turn could be excused by one
    and punished by the other."""
    from src.agent_harness import TurnLedger
    for text in ("hola", "gracias", "arregla el bug", "lee el fichero"):
        assert _conversational_turn(text) is \
            TurnLedger(user_text=text).conversational_turn()
