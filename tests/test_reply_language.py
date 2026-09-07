"""The turn says which language to answer in.

The bug these cover, from session fcffabaa: a brand-new chat, one English
message, and the model opened with "Voy a inspeccionar el código". No
memories, no AGENTS.md, no Spanish anywhere in the conversation — the whole
assembled prompt was English except for one sample sentence inside the
harness rule that says *don't* announce actions. Nothing had ever told the
model which language the user was owed.
"""

import pytest

from src.agent_harness import local_model_policy
from src.reply_language import (
    MIN_SIGNAL,
    context_message,
    conversation_language,
    language_of,
    visible_text,
)
from src.research_citations import detect_language, language_signal

ENGLISH = ("In the odysseus open source code, we have created the mode projects. "
           "We now need to add options to delete chats inside the projects.")
SPANISH = ("En el código de odysseus hemos creado el modo proyectos. Ahora hay que "
           "añadir opciones para borrar los chats de dentro de los proyectos.")


def user(text):
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------

def test_the_message_that_started_this_is_read_as_english():
    assert language_of(ENGLISH) == "en"


def test_the_same_request_in_spanish_is_read_as_spanish():
    assert language_of(SPANISH) == "es"


@pytest.mark.parametrize("text", ["Hazlo", "D:\\LocalAI\\odysseus\\src", "", "   ", "ok"])
def test_a_message_with_no_function_words_settles_nothing(text):
    """Silence beats a guess: pinning English off "Hazlo" would flip a Spanish
    conversation into English on its shortest turn."""
    assert language_of(text) is None


def test_the_score_is_what_separates_a_reading_from_the_default():
    """detect_language answers "en" for both; only the signal tells them apart."""
    assert detect_language("Hazlo") == "en"
    assert language_signal("Hazlo")[1] == 0.0
    assert detect_language(ENGLISH) == "en"
    assert language_signal(ENGLISH)[1] >= MIN_SIGNAL


def test_detect_language_still_answers_what_it_always_did():
    for text in (ENGLISH, SPANISH, "Quel est le meilleur protocole ?", ""):
        assert detect_language(text) == language_signal(text)[0]


def test_a_vision_turn_is_read_from_its_text_parts():
    content = [{"type": "image_url", "image_url": {"url": "data:..."}},
               {"type": "text", "text": SPANISH}]
    assert visible_text(content).strip() == SPANISH
    assert language_of(content) == "es"


# ---------------------------------------------------------------------------
# Which turn is read
# ---------------------------------------------------------------------------

def test_the_latest_user_turn_decides():
    assert conversation_language([user(SPANISH), user(ENGLISH)]) == "en"
    assert conversation_language([user(ENGLISH), user(SPANISH)]) == "es"


def test_a_wordless_last_turn_falls_back_to_the_one_that_spoke():
    """"Hazlo" after a Spanish request is still a Spanish conversation."""
    assert conversation_language([user(SPANISH), user("Hazlo")]) == "es"


def test_assistant_turns_never_decide():
    """The model answering in the wrong language must not entrench it."""
    convo = [user(ENGLISH), {"role": "assistant", "content": SPANISH}]
    assert conversation_language(convo) == "en"


def test_the_context_blocks_this_module_sits_beside_are_skipped():
    """The date block is English by construction and sits right before the
    user's message; reading it would pin English on every Spanish turn."""
    date_block = {"role": "user", "content": "## Current date and time\nToday is Sunday.",
                  "_agent_injected": "context"}
    assert conversation_language([user(SPANISH), date_block]) == "es"


def test_nothing_to_read_means_no_directive():
    assert conversation_language([]) is None
    assert conversation_language([user("Hazlo"), user("...")]) is None
    assert context_message([user("Hazlo")]) is None


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

def test_the_directive_is_written_in_the_language_it_asks_for():
    """A model about to answer in the wrong language reads an English sentence
    about English no better than a Spanish one."""
    english = context_message([user(ENGLISH)])["content"]
    assert "in English" in english
    assert "español" not in english

    spanish = context_message([user(SPANISH)])["content"]
    assert "en español" in spanish


def test_the_directive_is_a_user_turn_not_a_system_one():
    """Local backends key their KV-cache prefix off the system message
    byte-for-byte, and this line changes the moment the user switches."""
    assert context_message([user(ENGLISH)])["role"] == "user"


def test_the_directive_is_framed_as_context_not_as_the_user_talking():
    msg = context_message([user(ENGLISH)])
    assert msg["content"].startswith("[Runtime requirement —")
    assert msg["_agent_injected"] == "context"


# ---------------------------------------------------------------------------
# Where it lands, and what no longer seeds the drift
# ---------------------------------------------------------------------------

def test_the_harness_rules_no_longer_carry_a_spanish_sample():
    """Rule 2's second example was "voy a modificar X". The model copied the
    sample instead of obeying the "do not", which is the whole bug."""
    policy = local_model_policy()
    assert "voy a modificar" not in policy
    assert "in any language" in policy


def test_the_directive_is_the_last_thing_read_before_the_user():
    from src.agent_loop import _build_system_prompt

    built, _ = _build_system_prompt(
        [user(ENGLISH)], "test-model", None, None,
        relevant_tools={"ls", "read_file", "ask_user", "update_plan"},
        owner="admin",
    )
    assert built[-1]["content"] == ENGLISH, "the user's own words stay last"
    assert built[-2]["content"].startswith("[Runtime requirement —")
    assert "in English" in built[-2]["content"]
    assert built[-2]["_agent_injected"] == "context"


def test_a_spanish_conversation_gets_the_spanish_directive_in_place():
    from src.agent_loop import _build_system_prompt

    built, _ = _build_system_prompt(
        [user(SPANISH)], "test-model", None, None,
        relevant_tools={"ls", "read_file", "ask_user", "update_plan"},
        owner="admin",
    )
    assert "en español" in built[-2]["content"]


def test_a_turn_that_settles_nothing_adds_no_message_at_all():
    from src.agent_loop import _build_system_prompt

    built, _ = _build_system_prompt(
        [user("Hazlo")], "test-model", None, None,
        relevant_tools={"ls", "read_file", "ask_user", "update_plan"},
        owner="admin",
    )
    assert not any("[Runtime requirement — reply language]" in (m.get("content") or "") for m in built)


def test_plain_chat_context_builder_has_the_same_language_guard():
    """Chat mode bypasses agent_loop, so the shared builder must wire the
    guard itself before it copies route_messages."""
    import inspect
    from routes.chat_helpers import build_chat_context
    source = inspect.getsource(build_chat_context)
    guard = source.index("from src.reply_language import context_message")
    route_copy = source.index("route_messages = list(messages)")
    assert guard < route_copy
