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

def test_attachment_bodies_do_not_flip_english_instruction_to_spanish():
    """Silhouettes: English 'Implement it' + Spanish plan md must stay English."""
    from src.reply_language import language_of, context_message

    text = (
        "This is a plan to expand the project's capabilities. Implement it\n\n"
        "=== File: plan.md ===\n"
        "[Type: markdown, Lines: 100, Size: 1000 bytes]\n\n"
        "```markdown\n"
        "# Silhouettes — Editor de capas para fabricación\n"
        "Leer primero START_HERE.md. Después ejecutar una microtarea.\n"
        "Una tarea no se termina por decir que está implementada.\n"
        "```\n"
    )
    assert language_of(text) == "en"
    msg = context_message([user(text)])
    assert msg is not None
    assert msg["_reply_language"] == "en"
    assert "in English" in msg["content"]
    assert "attached files" in msg["content"].lower() or "Attached files" in msg["content"]


def test_reply_language_mismatch_detects_spanish_after_english_requirement():
    from src.reply_language import reply_language_mismatch, mismatch_nudge_message

    assert reply_language_mismatch("en", "Voy a continuar con la implementación del plan ahora.") == "es"
    assert reply_language_mismatch("en", "I will continue implementing the plan now.") is None
    nudge = mismatch_nudge_message("en")
    assert nudge and "wrong language" in nudge["content"]


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


@pytest.fixture
def no_default_language(monkeypatch):
    """These tests are about a turn that settles no language of its own, so
    they must not read the install's `reply_language_default` -- which exists
    precisely to answer that case and would otherwise make them pass or fail
    depending on whose machine they run on."""
    import src.settings as settings_mod
    real = settings_mod.get_setting
    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: (
            "" if key == "reply_language_default" else real(key, default)))


def test_nothing_to_read_means_no_directive(no_default_language):
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


def test_a_turn_that_settles_nothing_adds_no_message_at_all(no_default_language):
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


def test_explicit_language_request_overrides_the_language_used_to_ask():
    from src.reply_language import language_of
    assert language_of("Responde en inglés. Necesito que revises el objetivo del proyecto.") == "en"
    assert language_of("Reply in Spanish. Please check the saved project objective.") == "es"


def test_untrusted_tool_context_cannot_change_reply_language():
    from src.reply_language import conversation_language
    assert conversation_language([user(ENGLISH), {
        "role": "user", "content": SPANISH, "metadata": {"trusted": False},
    }]) == "en"


def test_continuation_reminder_rides_with_the_tool_batch_without_accumulating():
    """Refreshed five times leaves exactly one reminder, and it sits BEFORE
    the last message rather than after it -- the last thing the model reads
    has to be the step it was just given, not a standing style rule."""
    from src.reply_language import refresh_continuation, conversation_language
    original = [user(ENGLISH), {"role": "assistant", "tool_calls": [{"id": "a"}]},
                {"role": "tool", "tool_call_id": "a", "content": SPANISH}]
    messages = list(original)
    hint = context_message(messages)
    for _ in range(5):
        refresh_continuation(messages, hint)
    injected = [m for m in messages
                if m.get("_agent_injected") == "reply_language_continuity"]
    assert len(injected) == 1, "one reminder, never a growing prompt tail"
    assert "in English" in injected[0]["content"]
    assert messages[-1] == original[-1], "the last word stays with the conversation"
    assert [m for m in messages if not m.get("_agent_injected")] == original
    assert conversation_language(messages) == "en"
    refresh_continuation(messages, None)
    assert messages == original


def test_a_requested_output_language_is_recognised():
    from src.reply_language import requested_output_language as req
    assert req("Tradúceme al inglés, manteniendo el tono informal: «Oye…»") == "en"
    assert req("Translate this into Spanish please") == "es"
    assert req("Escríbelo en francés") == "fr"
    assert req("¿Qué tal el inglés de Marta?") is None
    assert req("Hola") is None


def test_a_short_spanish_order_with_di_is_not_italian():
    # "Di solo: hola A" was read as Italian ("di" listed only there) and GLM answered "Ciao!".
    from src.reply_language import language_of
    assert language_of("Di solo: hola A") == "es"
    assert language_of("dime solo hola") == "es"
    assert language_of("Dimmi la verità su questo e quello") == "it"


def test_a_pasted_link_does_not_decide_the_reply_language():
    """«x.com» carries «com», a Portuguese function word: a Spanish request with a tweet was answered in Portuguese."""
    from src import reply_language as rl
    # alone, the request is too short to settle anything (the default-language setting decides); never Portuguese
    assert rl.language_of("Descárgame este vídeo: https://x.com/NASA/status/2107095768821576019") is None
    assert rl.language_of("Descárgame este vídeo de la NASA y guárdalo en el móvil: https://x.com/NASA/status/1") == "es"
    assert rl.language_of("Can you download this video for me and keep it? https://www.youtube.com/watch?v=abc") == "en"
    assert rl.instruction_text_for_language("mira www.example.com/com/com y esto") == "mira   y esto"


@pytest.mark.parametrize('text,expected', [
    ('https://a', ' '), ('http://', 'http://'), ('www.', 'www.'),
    ('www.a://', ' '), ('foo.www.a://b', ' '),
    ('_http://a', '_http://a'), ('1http://a', '1http://a'),
    ('éhttp://a', 'éhttp://a'), ('é+http://a', 'é+ '),
    ('123+http://a', '123+ '), ('---http://a', '--- '),
    ('İ://a ı://b ſ://c K://d', '       '),
    ('中WWW.a', '中WWW.a'), ('\u0301www.a', '\u0301 '),
    ('(HTTP://a), next', '(  next'),
    ('a://b://c', ' '), ('a://\u2003www.b', 'a://\u2003 '),
    ('a://\x1cwww.b', 'a://\x1c '), ('www.a\nwww.b', ' \n '),
])
def test_url_scan_retains_boundaries_unicode_and_greedy_punctuation(text, expected):
    from src.reply_language import _strip_urls
    assert _strip_urls(text) == expected


def test_url_scan_matches_the_previous_regex_on_a_fixed_differential_corpus():
    import itertools
    import random
    import re
    from src.reply_language import _strip_urls

    previous = re.compile(r'(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+')
    # Exhaustive small delimiter strings, plus a fixed grammar that includes
    # real URL starts, nested schemes, Unicode boundaries and whitespace.
    for chars in itertools.product('a0-:/w. ', repeat=5):
        text = ''.join(chars)
        assert _strip_urls(text) == previous.sub(' ', text), repr(text)
    rng = random.Random(874)
    fragments = ['a', 'current-data-', 'http', '://', 'www.', 'WWW.', '+.-',
                 '_', '0', 'é', '中', 'İ', 'ı', 'ſ', 'K', '\u0301', '/', ':',
                 ' ', '\n', '\t', '\x1c', '\u2003', '\u00a0', '🙂', '(),']
    for _ in range(10_000):
        text = ''.join(rng.choices(fragments, k=rng.randrange(1, 50)))
        assert _strip_urls(text) == previous.sub(' ', text), repr(text)


def test_url_scan_preserves_attachment_and_media_preprocessing():
    from src.reply_language import instruction_text_for_language
    text = ('Keep this instruction https://a\n[Image attached: capture.png]\n'
            'www.example.com\n=== File: long.txt ===\n' + 'current-data-' * 1000)
    assert instruction_text_for_language(text) == 'Keep this instruction'
    assert instruction_text_for_language('[Image: capture.png]\nhttp://a') == ''


def test_large_url_inputs_finish_without_retrying_every_word_boundary():
    import json
    import subprocess
    import sys
    from pathlib import Path

    code = r'''
import json,socket,sys,time
def deny(event,args):
    if event=='socket.connect':raise AssertionError('No network in language regression')
sys.addaudithook(deny)
from src.reply_language import instruction_text_for_language
plain='current-data-'*300000
cases=[('no_url',plain,plain),('url_first','https://a '+plain,plain),
       ('url_last',plain+' https://a',plain),
       ('many_delimiters','0://-'*780000,':|')]
rows=[]
for name,text,expected in cases:
    started=time.perf_counter();actual=instruction_text_for_language(text)
    if name=='many_delimiters':assert actual==text
    else:assert actual==expected
    rows.append({'name':name,'chars':len(text),'seconds':time.perf_counter()-started})
print(json.dumps(rows))
'''
    result = subprocess.run([sys.executable, '-B', '-c', code],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 4 and all(row['chars'] >= 3_900_000 for row in rows)
