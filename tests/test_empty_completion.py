"""A backend that answers with nothing has failed, and must say so.

Found by using the app. A local engine whose slot had gone bad answered every
request in under a second with `finish_reason` "stop", an empty content field
and six characters of punctuation in the reasoning channel:

    {"message": {"content": "", "reasoning_content": "/umd``"},
     "finish_reason": "stop", "usage": {"completion_tokens": 3}}

Three things went wrong on top of each other:

  * the reasoning channel was served as the answer, so the user got "/umd``",
  * the nothing was written to the response cache, turning a transient engine
    fault into a permanent wrong answer for that question, and
  * nothing anywhere said the engine was sick, so the turn just looked slow
    and then blank.

Restarting the engine fixed it -- the same question then answered in 3.7 s --
which is exactly the kind of thing the app should notice by itself.
"""
import httpx
import pytest

import src.llm_core as llm_core
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clean_caches():
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()
    yield
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()


# ---------------------------------------------------------------------------
# What counts as nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\n", None, "...",
                                  "``", "-- ,, ;;", "!!!"])
def test_these_are_not_answers(text):
    assert llm_completion_empty(text) is True


# ---------------------------------------------------------------------------
# The reasoning channel is a fallback, not a substitute
# ---------------------------------------------------------------------------

def test_six_characters_of_reasoning_are_not_a_reply():
    """"/umd``" has letters in it, so `empty_completion` does not catch it.
    What catches it is that it is ONE unbroken token and a short one: a model
    that puts its answer in the reasoning channel writes words with spaces
    between them."""
    assert llm_core.reasoning_as_answer("/umd``") == ""
    assert llm_core.reasoning_as_answer("aaaaaaaaaaaaaaaaaaaa") == ""


@pytest.mark.parametrize("text", [
    "Yes, already fixed.",
    "Sí, ya está arreglado.",
    "async reasoning text",
    "2 + 2 = 4",
])
def test_a_short_reply_is_still_a_reply(text):
    """Length alone was the first rule and it was too blunt: it threw away
    answers that are simply short. Several words is a reply at any length."""
    assert llm_core.reasoning_as_answer(text) == text


def test_a_long_single_token_is_allowed_through():
    """A URL or a base64 blob on its own is unusual but not malformed, and it
    is long enough not to be the six characters of a sick slot."""
    blob = "https://example.invalid/a/very/long/path/that/keeps/going/and/going"
    assert llm_core.reasoning_as_answer(blob) == blob


def test_a_real_reasoning_answer_still_comes_through():
    """Some models put the whole reply there; that fallback must survive."""
    text = ("The engine is the process that serves the model, and the model "
            "is the weights it loads. Stopping one does not delete the other.")
    assert llm_core.reasoning_as_answer(text) == text


def test_empty_reasoning_is_empty():
    assert llm_core.reasoning_as_answer(None) == ""
    assert llm_core.reasoning_as_answer("   ") == ""


@pytest.mark.parametrize("text", ["banana", "0", "No.", "sí", "Ελλάδα",
                                  "答案是", "Да", "a", "x1"])
def test_these_are_answers(text):
    """A one-character answer is still an answer, and so is one with no Latin
    letters in it -- "no letters" must not mean "no ASCII letters"."""
    assert llm_completion_empty(text) is False


@pytest.mark.parametrize("text", ["{}", "[]", '{"a": 1}'])
def test_an_empty_json_object_is_an_answer(text):
    """`{}` is a complete reply to a request that asked for JSON. The first
    version of this rule saw punctuation and threw it away, which broke the
    constrained-decoding passes -- caught by their tests, not by reasoning."""
    assert llm_completion_empty(text) is False


def llm_completion_empty(text):
    return llm_core.empty_completion(text)


# ---------------------------------------------------------------------------
# Nothing is never cached
# ---------------------------------------------------------------------------

def test_an_empty_answer_is_not_cached():
    """Caching it is what turns a sick engine into a wrong answer that
    outlives the illness."""
    llm_core._set_cached_response("k", "")
    assert llm_core._get_cached_response("k") is None


def test_punctuation_is_not_cached_either():
    llm_core._set_cached_response("k", "...")
    assert llm_core._get_cached_response("k") is None


def test_a_real_answer_is_still_cached():
    llm_core._set_cached_response("k", "banana")
    assert llm_core._get_cached_response("k") == "banana"


# ---------------------------------------------------------------------------
# The retry
# ---------------------------------------------------------------------------

def _response(content, reasoning=""):
    return httpx.Response(
        200, request=httpx.Request("POST", "http://127.0.0.1:8081/v1/chat/completions"),
        json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": content,
            "reasoning_content": reasoning}}]},
    )


def test_the_retry_also_turns_reasoning_off(monkeypatch):
    """The second way to answer with nothing is to spend the budget thinking.
    Measured on the local 27B: 37.2 s, 1,752 characters of reasoning, the
    token ceiling hit and an empty answer -- against 5.5 s and a correct one
    with reasoning off. The retry must not pay for that twice."""
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen.append(json)
        return _response("banana")

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    llm_core._retry_empty_completion(
        "http://127.0.0.1:8081/v1/chat/completions", {},
        {"model": "qwen3.8-27b-q8-llamacpp", "messages": []},
        "openai", "qwen3.8-27b-q8-llamacpp", 60)
    assert seen[0]["chat_template_kwargs"]["enable_thinking"] is False


def test_the_retry_bypasses_the_prompt_cache(monkeypatch):
    """The poisoned prompt cache is what the engine keeps reading from, so the
    retry has to ask for it to be rebuilt -- repeating the same request would
    get the same six bytes."""
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen.append(json)
        return _response("banana")

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    out = llm_core._retry_empty_completion(
        "http://127.0.0.1:8081/v1/chat/completions", {},
        {"model": "m", "messages": []}, "openai", "m", 60)
    assert out == "banana"
    assert seen[0]["cache_prompt"] is False


def test_the_original_payload_is_not_mutated(monkeypatch):
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware",
                        lambda *a, **k: _response("banana"))
    payload = {"model": "m", "messages": []}
    llm_core._retry_empty_completion("http://x/v1/chat/completions", {}, payload,
                                     "openai", "m", 60)
    assert "cache_prompt" not in payload


def test_two_empty_answers_raise_instead_of_returning_blank(monkeypatch):
    """A blank message after minutes of waiting tells the user nothing. The
    error names the engine and what clears it."""
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware",
                        lambda *a, **k: _response("", "/umd``"))
    with pytest.raises(HTTPException) as exc:
        llm_core._retry_empty_completion(
            "http://127.0.0.1:8081/v1/chat/completions", {},
            {"model": "qwen-27b", "messages": []}, "openai", "qwen-27b", 60)
    assert exc.value.status_code == 502
    message = str(exc.value.detail)
    assert "qwen-27b" in message
    assert "Local models" in message


def test_a_failed_retry_still_raises_rather_than_returning_nothing(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("engine went away")

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", boom)
    with pytest.raises(HTTPException):
        llm_core._retry_empty_completion("http://x/v1/chat/completions", {},
                                         {"model": "m", "messages": []},
                                         "openai", "m", 60)


def test_the_reasoning_channel_is_not_served_as_the_answer(monkeypatch):
    """"/umd``" in the reasoning channel is a symptom, not a reply."""
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware",
                        lambda *a, **k: _response("", "/umd``"))
    with pytest.raises(HTTPException):
        llm_core._retry_empty_completion("http://x/v1/chat/completions", {},
                                         {"model": "m", "messages": []},
                                         "openai", "m", 60)
