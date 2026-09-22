"""A greeting is not a broken promise.

Found by using the app. "Hola. Dime en dos frases qué eres y qué puedes hacer
por mí" got a correct answer, which the harness then rejected:

    No action taken · no_workspace_action      round 1
    Reply rejected: intent_without_action      round 3

The model wrote a second reply, both ended up in the message, and the user
got the same paragraph twice after three rounds instead of one.

The guard is right in general and has to stay: it exists because local models
say "voy a arreglarlo" and then stop. It just must not fire on a turn where
nobody asked for anything to be done -- there, saying what you could do is
the answer, not a promise to break.
"""
import pytest

from src.agent_harness import TurnLedger


def _reasons(ledger, text):
    return ledger.check_completion(text)["reasons"]


#: A sentence the intent detector really does catch. Checked against
#: `find_intent_announcement` below rather than assumed: the first version of
#: this test used a list of capabilities, which the detector does not flag at
#: all, so it proved nothing in either direction.
ANNOUNCEMENT = "Ahora voy a revisar el fichero y corregir el error."


# ---------------------------------------------------------------------------
# Which turns are conversational
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Hola. Dime en dos frases qué eres y qué puedes hacer por mí, sin listas.",
    "¿qué eres?",
    "what can you do",
    "gracias",
])
def test_these_turns_are_conversational(text):
    assert TurnLedger(user_text=text).conversational_turn() is True


@pytest.mark.parametrize("text", [
    "arregla el bug del compositor",
    "lee src/llm_core.py y dime qué hace",
    "implementa el endpoint",
    "hola, ¿puedes arreglar los tests?",
])
def test_these_turns_are_work(text):
    assert TurnLedger(user_text=text).conversational_turn() is False


# ---------------------------------------------------------------------------
# What the guard does with them
# ---------------------------------------------------------------------------

def test_the_detector_really_catches_the_sample():
    """Guards against writing a test that passes for the wrong reason."""
    from src.agent_harness import find_intent_announcement
    assert find_intent_announcement(ANNOUNCEMENT)


def test_an_announcement_on_a_conversational_turn_is_not_rejected():
    ledger = TurnLedger(user_text="¿qué puedes hacer por mí?")
    assert "intent_without_action" not in _reasons(ledger, ANNOUNCEMENT)


def test_the_same_sentence_on_a_work_turn_is_still_rejected():
    """The guard exists because local models announce work and then stop.
    Taking it away from real requests would trade one bug for a worse one."""
    ledger = TurnLedger(user_text="arregla el bug del compositor")
    assert "intent_without_action" in _reasons(ledger, ANNOUNCEMENT)


def test_the_other_checks_still_run_on_a_conversational_turn():
    """Only the intent check is excused. A conversational turn that claims to
    have edited files is still lying."""
    ledger = TurnLedger(user_text="hola")
    reasons = _reasons(ledger, "Listo, he actualizado el fichero y guardado los cambios.")
    assert "claims_without_mutation" in reasons


def test_the_verdict_is_computed_once():
    ledger = TurnLedger(user_text="hola")
    assert ledger.conversational_turn() is True
    assert ledger._conversational is True
    assert ledger.conversational_turn() is True


def test_a_guard_never_breaks_a_turn(monkeypatch):
    """If the whitelist module cannot be imported, the guard behaves as it did
    before it existed rather than taking the turn down."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "src.turn_effort":
            raise ImportError("gone")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert TurnLedger(user_text="hola").conversational_turn() is False
