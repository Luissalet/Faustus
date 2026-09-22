"""Spanish typed without accents came back in Portuguese.

Found by using the app. "Prueba de envio numero dos" was answered with
"Recebido! Mensagem de teste nº 2 chegou sem problemas."

Nothing was broken. Without its accents that sentence carries exactly one
function word the detector knows -- "de" -- which four of the six languages
share, so the signal landed under the threshold and no language was pinned.
The model was left to guess, and an unaccented Spanish sentence really does
look Portuguese. People type without accents constantly, and a short first
message is the most likely one to carry no signal at all.

Silence remains the right default for an install that does not know its user.
This adds a way for one that does to say so.
"""
import pytest

from src import reply_language as rl


@pytest.fixture
def setting(monkeypatch):
    import src.settings as settings_mod
    state = {}
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: state.get(key, default))
    return state


AMBIGUOUS = [{"role": "user", "content": "Prueba de envio numero dos"}]


def test_the_message_that_started_this_really_settles_nothing():
    """If this ever starts settling a language on its own, the fallback is no
    longer what is being tested."""
    assert rl.conversation_language(AMBIGUOUS) is None


def test_without_a_setting_nothing_changes(setting):
    """An install that has not said anything keeps today's behaviour: stay
    quiet rather than impose a language on someone."""
    assert rl.fallback_language() is None
    assert rl.context_message(AMBIGUOUS) is None


def test_the_fallback_pins_the_language(setting):
    setting[rl.DEFAULT_LANGUAGE_SETTING] = "es"
    message = rl.context_message(AMBIGUOUS)
    assert message is not None
    assert message["_reply_language"] == "es"
    assert message["role"] == "user"


def test_the_conversation_still_wins_over_the_fallback(setting):
    """The setting is a last resort, never an override: someone who writes in
    English gets English back even on a Spanish install."""
    setting[rl.DEFAULT_LANGUAGE_SETTING] = "es"
    english = [{"role": "user", "content":
                "What is the difference between these two approaches and why?"}]
    assert rl.conversation_language(english) == "en"
    assert rl.context_message(english)["_reply_language"] == "en"


def test_an_unknown_code_is_ignored(setting):
    setting[rl.DEFAULT_LANGUAGE_SETTING] = "klingon"
    assert rl.fallback_language() is None


def test_the_code_is_normalised(setting):
    setting[rl.DEFAULT_LANGUAGE_SETTING] = "  ES  "
    assert rl.fallback_language() == "es"


def test_a_broken_settings_store_never_breaks_a_turn(monkeypatch):
    import src.settings as settings_mod

    def boom(*a, **k):
        raise RuntimeError("no settings")

    monkeypatch.setattr(settings_mod, "get_setting", boom)
    assert rl.fallback_language() is None
    assert rl.context_message(AMBIGUOUS) is None
