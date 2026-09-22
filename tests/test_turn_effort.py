"""Small talk must not cost 37 seconds of thinking.

The measurement that produced this module: "Dime en dos frases qué eres y qué
puedes hacer por mí" spent 37.2 s and 1,752 characters of reasoning, hit the
token ceiling and answered with nothing at all. With reasoning off: 5.5 s and
a correct answer.

Every test here is really one question: does the whitelist stay a whitelist?
A greeting that thinks costs seconds. A real request that does NOT think
costs a wrong answer. The tests below are weighted accordingly -- most of
them check that something is NOT treated as small talk.
"""
import pytest

from src import turn_effort as te


# ---------------------------------------------------------------------------
# Small talk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Hola",
    "hola, buenas",
    "Hey!",
    "Good morning",
    "gracias!",
    "Gracias, perfecto",
    "vale",
    "ok",
    "¿Qué eres?",
    "que eres exactamente",
    "What are you?",
    "who are you",
    "¿qué puedes hacer por mí?",
    "Preséntate en dos frases",
    "¿cómo estás?",
    "sí",
    "no",
    "adelante",
])
def test_this_is_small_talk(text):
    assert te.is_small_talk(text) is True


# ---------------------------------------------------------------------------
# Everything else. This is the half that matters.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "arregla los tests",
    "fix the tests",
    "¿por qué falla esto?",
    "why does this fail",
    "explica el Context Engine",
    "lee src/llm_core.py",
    "open studio/src/screens/Studio.tsx",
    "compara las dos opciones",
    "hola, ¿puedes arreglar el bug del compositor?",
    "gracias, ahora implementa la segunda parte",
    "vale, escribe el test",
    "ok pero antes analiza el diff",
    "¿qué eres capaz de hacer con un fichero .csv de 2 GB?",
    "resume esto",
    "dame un plan",
    "busca en la web qué salió esta semana",
    "```python\nprint(1)\n```",
    "mira https://example.com/docs",
    "sube la temperatura a 0.6",
    "necesito la versión v2.1",
])
def test_this_is_not_small_talk(text):
    """A greeting with work stapled to it is work. The whitelist has to lose
    every one of these arguments."""
    assert te.is_small_talk(text) is False


def test_a_long_message_is_never_small_talk():
    """Even if every word of it is polite."""
    assert te.is_small_talk("hola " * (te.MAX_SMALL_TALK_WORDS + 5)) is False


def test_an_empty_message_is_not_small_talk():
    """Nothing to recognise means no licence to skip the reasoning."""
    assert te.is_small_talk("") is False
    assert te.is_small_talk("   ") is False


def test_an_unrecognised_message_keeps_its_reasoning():
    """The default is to think. Only the whitelist buys the shortcut."""
    assert te.is_small_talk("El tiempo en Móstoles") is False
    assert te.is_small_talk("cosas varias del proyecto") is False


# ---------------------------------------------------------------------------
# Reading the turn
# ---------------------------------------------------------------------------

def test_the_last_user_message_is_the_one_that_counts():
    messages = [
        {"role": "user", "content": "arregla el bug"},
        {"role": "assistant", "content": "hecho"},
        {"role": "user", "content": "gracias"},
    ]
    assert te.last_user_text(messages) == "gracias"
    assert te.wants_reasoning(messages) is False


def test_injected_blocks_are_not_the_user_talking():
    """The date line and the reply-language line are ours, not theirs --
    the same reason src/reply_language.py skips them."""
    messages = [
        {"role": "user", "content": "arregla los tests"},
        {"role": "user", "content": "Today is Monday.", "_agent_injected": True},
    ]
    assert te.last_user_text(messages) == "arregla los tests"
    assert te.wants_reasoning(messages) is True


def test_structured_content_is_read():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "hola"},
    ]}]
    assert te.last_user_text(messages) == "hola"


def test_no_messages_means_keep_reasoning():
    assert te.wants_reasoning([]) is True
    assert te.wants_reasoning(None) is True


def test_the_wiring_reaches_a_real_call(monkeypatch):
    """The unit tests above all pass with the module wired to nothing. This
    one fails if a greeting still pays for reasoning on the wire."""
    import httpx
    import src.llm_core as llm_core

    llm_core._response_cache.clear()
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen["body"] = json
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "Soy un asistente."}}]})

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    llm_core.llm_call("https://api.example.test/v1", "qwen3.8-27b-q8",
                      [{"role": "user", "content": "hola, ¿qué eres?"}],
                      temperature=0.6, max_tokens=200)
    assert seen["body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_the_wiring_leaves_real_work_alone(monkeypatch):
    import httpx
    import src.llm_core as llm_core

    llm_core._response_cache.clear()
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen["body"] = json
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "Hecho."}}]})

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    llm_core.llm_call("https://api.example.test/v1", "qwen3.8-27b-q8",
                      [{"role": "user", "content": "arregla los tests que fallan"}],
                      temperature=0.6, max_tokens=200)
    assert "chat_template_kwargs" not in seen["body"]


def test_tools_always_keep_reasoning():
    """A turn that can act has consequences worth thinking about, and the
    whitelist was not written with tool use in mind."""
    messages = [{"role": "user", "content": "gracias"}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert te.wants_reasoning(messages, tools=tools) is True
    assert te.wants_reasoning(messages) is False
