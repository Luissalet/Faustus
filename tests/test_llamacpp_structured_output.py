"""Constrained JSON decoding on the other local backend: llama-server.

`tests/test_ollama_structured_output.py` pinned the same guarantee for Ollama
and, in doing so, wrote the limit into the code: anything that was not native
Ollama "gets nothing and keeps today's parsing". llama.cpp was on that list.

That mattered the day the default model moved to a managed llama-server: the
three passes that ask for a schema (the diff reviewer, the doubt pass and the
research reviewer) quietly went back to parsing prose, with nothing in the
logs to say so.

llama-server takes a JSON Schema in `response_format` on its
OpenAI-compatible surface and compiles it to a grammar, so the guarantee is
available there too -- it just travels in a different field. Measured against
the real 27B on this machine, the three regimes are:

    no schema      -> "Classification: **Bug fix request**"  (prose)
    json_object    -> {"intent": "bug_fix_request"}          (valid JSON,
                                                              invalid value)
    json_schema    -> {"intent": "code"}                     (in the set)

The middle row is why these tests check the schema and not just "is it JSON".

As in the Ollama file, the second half is the point: a schema sent to an
endpoint that drops it is worse than no schema at all, because the caller then
believes the answer is guaranteed when nothing guaranteed it.
"""
import json

import httpx
import pytest

import src.llm_core as llm_core


LLAMACPP = "http://127.0.0.1:8081/v1"
OLLAMA_NATIVE = "https://ollama.com/api"
HOSTED = "https://api.example-openai.test/v1"

SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["code", "research", "chat"]}},
    "required": ["intent"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_caches():
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()
    yield
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()


@pytest.fixture
def settings(monkeypatch):
    """Drive `local_structured_output` through the real setting reader."""
    import src.settings as settings_mod
    state = {"local_structured_output": "auto"}
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: state.get(key, default))
    return state


@pytest.fixture
def backends(monkeypatch):
    """Answer `serving_backend` from a table instead of the engine registry."""
    import src.model_backend as model_backend
    table = {LLAMACPP: "llamacpp", HOSTED: "remote"}

    def fake(base_url, *, endpoint_kind=None, probe=True):
        assert probe is False, "the hot path must not pay a network probe"
        return {"backend": table.get(str(base_url), "unknown"), "label": "x"}

    monkeypatch.setattr(model_backend, "serving_backend", fake)
    return table


# ---------------------------------------------------------------------------
# Which field carries the schema
# ---------------------------------------------------------------------------

def test_transport_is_format_for_native_ollama(settings, backends):
    assert llm_core._schema_transport(OLLAMA_NATIVE) == "format"


def test_transport_is_response_format_for_a_managed_llamacpp(settings, backends):
    assert llm_core._schema_transport(LLAMACPP) == "response_format"


def test_transport_is_none_for_a_hosted_api(settings, backends):
    """A hosted endpoint keeps the tolerant parser: we do not know what it
    would do with a schema, and guessing is what this whole gate prevents."""
    assert llm_core._schema_transport(HOSTED) is None


def test_transport_survives_a_broken_backend_lookup(monkeypatch, settings):
    """An engine registry that raises must not take the request down with it."""
    import src.model_backend as model_backend

    def boom(*a, **k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(model_backend, "serving_backend", boom)
    assert llm_core._schema_transport(LLAMACPP) is None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_llamacpp_now_resolves_a_schema(settings, backends):
    assert llm_core._resolve_response_schema(LLAMACPP, SCHEMA) == SCHEMA


def test_hosted_api_still_resolves_nothing(settings, backends):
    assert llm_core._resolve_response_schema(HOSTED, SCHEMA) is None


def test_the_off_setting_still_disables_llamacpp_too(settings, backends):
    settings["local_structured_output"] = "off"
    assert llm_core._resolve_response_schema(LLAMACPP, SCHEMA) is None


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

def test_response_format_carries_the_exact_schema(settings, backends):
    payload = {"model": "m", "messages": []}
    assert llm_core._apply_openai_response_format(payload, LLAMACPP, SCHEMA) is True
    wrapper = payload["response_format"]
    assert wrapper["type"] == "json_schema"
    assert wrapper["json_schema"]["strict"] is True
    # The schema goes out as written, not a copy that drifted.
    assert wrapper["json_schema"]["schema"] == SCHEMA
    assert wrapper["json_schema"]["schema"]["properties"]["intent"]["enum"] == [
        "code", "research", "chat"]


def test_no_response_format_key_without_a_schema(settings, backends):
    """Every existing caller keeps the payload it had: no stray null key."""
    payload = {"model": "m", "messages": []}
    assert llm_core._apply_openai_response_format(payload, LLAMACPP, None) is False
    assert "response_format" not in payload


def test_tools_exclude_the_schema(settings, backends):
    """The grammar and the tool-call decoder compete for the same output, so
    constrained decoding stays out of the agent loop -- the same rule the
    Ollama builder already enforces."""
    payload = {"model": "m", "messages": []}
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert llm_core._apply_openai_response_format(
        payload, LLAMACPP, SCHEMA, tools=tools) is False
    assert "response_format" not in payload


def test_a_hosted_api_never_receives_response_format(settings, backends):
    payload = {"model": "m", "messages": []}
    assert llm_core._apply_openai_response_format(payload, HOSTED, SCHEMA) is False
    assert "response_format" not in payload


def test_native_ollama_never_receives_response_format(settings, backends):
    """It takes `format` instead; sending both would be two grammars for one
    answer."""
    payload = {"model": "m", "messages": []}
    assert llm_core._apply_openai_response_format(payload, OLLAMA_NATIVE, SCHEMA) is False
    assert "response_format" not in payload


# ---------------------------------------------------------------------------
# End to end: the schema is really on the wire
# ---------------------------------------------------------------------------

def test_llm_call_puts_the_schema_on_the_wire_for_llamacpp(monkeypatch, settings, backends):
    """The gate and the builder agree: a real call to a managed llama-server
    carries the schema. This is the test that would have caught the silent
    regression -- the unit tests above all pass with the wiring missing."""
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        seen["url"] = url
        seen["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200, request=request,
            json={"choices": [{"message": {"content": '{"intent": "code"}'}}]},
        )

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware", fake_post)
    out = llm_core.llm_call(
        LLAMACPP, "qwen3.8-27b-q8-llamacpp",
        [{"role": "user", "content": "classify this"}],
        temperature=0.0, max_tokens=64, response_schema=SCHEMA,
    )
    body = seen["json"]
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert json.loads(out) == {"intent": "code"}
