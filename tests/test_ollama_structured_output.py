"""Constrained JSON decoding for the tool-less internal passes.

The diff reviewer (src/auto_review.py) asks the model for a JSON object and
then goes looking for it inside whatever prose came back. A measured scorecard
put that at "review OK 44 %": more than half the review passes thrown away,
15 s of local GPU each.

Ollama's /api/chat takes a JSON Schema in `format` and decodes under it — a
state machine over the logits, not a request in the prompt. These tests pin
the two things that make that a guarantee rather than a hope:

  * the schema really is on the wire where it is honoured, byte for byte, and
  * it is really absent everywhere it would be silently ignored — Ollama's own
    OpenAI-compatible /v1 included, since `format` is a native-only parameter.

The second half is the point: a schema sent to an endpoint that drops it is
worse than no schema at all, because the caller then believes the answer is
guaranteed when nothing guaranteed it.
"""
import asyncio
import json

import httpx
import pytest

import src.llm_core as llm_core
from src import auto_review as ar


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OLLAMA_NATIVE = "https://ollama.com/api"
OLLAMA_V1 = "http://127.0.0.1:11434/v1"
OPENAI_COMPAT = "https://api.example-openai.test/v1"


@pytest.fixture(autouse=True)
def _clean_caches():
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()
    llm_core._ollama_caps_cache.clear()
    yield
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()
    llm_core._ollama_caps_cache.clear()


@pytest.fixture
def settings(monkeypatch):
    """Drive `local_structured_output` through the real setting reader."""
    import src.settings as settings_mod
    state = {"local_structured_output": "auto"}
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: state.get(key, default))
    return state


@pytest.fixture
def stable_ctx(monkeypatch):
    """Pin the discovered context window so payloads are comparable."""
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 32768)


class _Resp:
    is_success = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _capture_async(monkeypatch, payload=None):
    """Capture the exact async request (url, headers, JSON body)."""
    seen = {}

    async def fake_post(client, url, headers, **kwargs):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        seen["json"] = kwargs.get("json")
        seen["body"] = json.dumps(kwargs.get("json"), sort_keys=True)
        return _Resp(payload or {"message": {"content": "{}"}})

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    return seen


def _capture_sync(monkeypatch, payload=None):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request,
                              json=payload or {"message": {"content": "{}"}})

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)
    return seen


def _run(**kwargs):
    return asyncio.run(llm_core.llm_call_async(**kwargs))


# ---------------------------------------------------------------------------
# The payload builder
# ---------------------------------------------------------------------------

def test_build_ollama_payload_emits_format_with_the_exact_schema():
    payload = llm_core._build_ollama_payload(
        "codellama:7b", [{"role": "user", "content": "x"}],
        temperature=0.1, max_tokens=1200, response_schema=ar.REVIEW_SCHEMA,
    )
    assert payload["format"] == ar.REVIEW_SCHEMA
    # Not a copy that drifted: the schema goes out as written.
    assert payload["format"]["properties"]["findings"]["items"]["required"] == [
        "severity", "file", "evidence", "issue"]


def test_build_ollama_payload_has_no_format_key_by_default():
    """Every existing caller keeps the payload it had: no stray null `format`."""
    payload = llm_core._build_ollama_payload(
        "codellama:7b", [{"role": "user", "content": "x"}],
        temperature=0.1, max_tokens=1200,
    )
    assert "format" not in payload
    assert set(payload) == {"model", "messages", "stream", "options"}


def test_build_ollama_payload_never_combines_format_with_tools():
    """Ollama does not honour `format` and `tools` together across versions.
    The schema is dropped, the tool call is not put at risk."""
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    payload = llm_core._build_ollama_payload(
        "codellama:7b", [{"role": "user", "content": "x"}],
        temperature=0.1, max_tokens=1200, tools=tools,
        response_schema=ar.REVIEW_SCHEMA,
    )
    assert "format" not in payload
    assert payload["tools"]


def test_stream_llm_cannot_carry_a_schema_at_all():
    """v1 is for the tool-less passes only — the agent loop must stay out of
    it, so the streaming entry point has no way to ask for one."""
    import inspect
    assert "response_schema" not in inspect.signature(llm_core.stream_llm).parameters
    assert "response_schema" not in inspect.signature(llm_core._stream_llm_inner).parameters


# ---------------------------------------------------------------------------
# On the wire: present where Ollama honours it
# ---------------------------------------------------------------------------

def test_native_ollama_request_carries_the_schema(settings, stable_ctx, monkeypatch):
    seen = _capture_async(monkeypatch)
    _run(url=OLLAMA_NATIVE, model="gpt-oss:120b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["json"]["format"] == ar.REVIEW_SCHEMA


def test_sync_llm_call_also_carries_the_schema(settings, stable_ctx, monkeypatch):
    seen = _capture_sync(monkeypatch)
    llm_core.llm_call(OLLAMA_NATIVE, "gpt-oss:120b",
                      [{"role": "user", "content": "review this"}],
                      response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["json"]["format"] == ar.REVIEW_SCHEMA


def test_local_ollama_v1_is_rerouted_to_api_chat_to_carry_the_schema(
        settings, stable_ctx, monkeypatch):
    """`format` is native-only. A local Ollama configured as /v1 is moved to
    /api/chat on the same server — but only after /api/show has answered for
    this model, which is the proof that :11434 really is Ollama."""
    monkeypatch.setattr(llm_core, "_ollama_model_caps",
                        lambda url, model: frozenset({"completion", "tools"}))
    seen = _capture_async(monkeypatch)
    _run(url=OLLAMA_V1, model="codellama:7b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["json"]["format"] == ar.REVIEW_SCHEMA
    # Native payload shape, and no `think` for a model without the capability.
    assert seen["json"]["stream"] is False and "think" not in seen["json"]


def test_reroute_keeps_the_thinking_suppression_it_replaces(
        settings, stable_ctx, monkeypatch):
    """qwen3.5:9b on /v1 is already moved to /api/chat for think=false. The
    schema rides along on the request that reroute produced — it must not undo
    the suppression that made the reviewer answer at all."""
    monkeypatch.setattr(llm_core, "_ollama_model_caps",
                        lambda url, model: frozenset({"completion", "thinking"}))
    seen = _capture_async(monkeypatch)
    _run(url=OLLAMA_V1, model="qwen3.5:9b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["json"]["format"] == ar.REVIEW_SCHEMA
    assert seen["json"]["think"] is False


# ---------------------------------------------------------------------------
# On the wire: absent everywhere it would be ignored
# ---------------------------------------------------------------------------

def test_openai_compatible_endpoint_gets_no_schema(settings, stable_ctx, monkeypatch):
    """A plain OpenAI-compatible server has no `format`. Sending one would be
    ignored — and inventing a `response_format` for it is a different feature."""
    seen = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": "{}"}}]})
    _run(url=OPENAI_COMPAT, model="gpt-4o-mini",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "https://api.example-openai.test/v1/chat/completions"
    assert "format" not in seen["json"]
    assert "response_format" not in seen["json"]


def test_anthropic_endpoint_gets_no_schema(settings, stable_ctx, monkeypatch):
    seen = _capture_async(
        monkeypatch, payload={"content": [{"type": "text", "text": "{}"}]})
    _run(url="https://api.anthropic.com/v1", model="claude-sonnet-4",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert "format" not in seen["json"]


def test_local_v1_without_a_native_api_keeps_v1_and_sends_no_schema(
        settings, stable_ctx, monkeypatch):
    """/api/show did not answer: whatever is on :11434 is not Ollama (llama.cpp,
    vLLM, a proxy). Do not move the request and do not pretend it is
    constrained — the caller's tolerant parse is what protects the turn."""
    monkeypatch.setattr(llm_core, "_ollama_model_caps", lambda url, model: None)
    seen = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": "{}"}}]})
    _run(url=OLLAMA_V1, model="codellama:7b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert "format" not in seen["json"]


def test_a_non_ollama_local_port_is_never_rerouted(settings, stable_ctx, monkeypatch):
    """llama.cpp on :8080 matches the local-Ollama host test but has no
    /api/chat. Same rule as the `think` reroute: default port only."""
    monkeypatch.setattr(llm_core, "_ollama_model_caps",
                        lambda url, model: frozenset({"completion"}))
    seen = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": "{}"}}]})
    _run(url="http://127.0.0.1:8080/v1", model="codellama:7b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert "format" not in seen["json"]


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def test_setting_off_restores_the_previous_request_byte_for_byte(
        settings, stable_ctx, monkeypatch):
    """`local_structured_output: off` is a real off switch: same URL, same
    headers, same body bytes as the call that never knew about schemas."""
    monkeypatch.setattr(llm_core, "_ollama_model_caps",
                        lambda url, model: frozenset({"completion", "tools"}))
    messages = [{"role": "user", "content": "review this"}]

    before = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": "{}"}}]})
    _run(url=OLLAMA_V1, model="codellama:7b", messages=list(messages))
    baseline = dict(before)

    settings["local_structured_output"] = "off"
    llm_core._response_cache.clear()
    after = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": "{}"}}]})
    _run(url=OLLAMA_V1, model="codellama:7b", messages=list(messages),
         response_schema=ar.REVIEW_SCHEMA)

    assert after["url"] == baseline["url"]
    assert after["headers"] == baseline["headers"]
    assert after["body"] == baseline["body"]
    assert "format" not in after["json"]

    # …and the switch is not vacuous: with it back on, the very same call
    # moves to /api/chat and carries the schema.
    settings["local_structured_output"] = "auto"
    llm_core._response_cache.clear()
    on = _capture_async(monkeypatch)
    _run(url=OLLAMA_V1, model="codellama:7b", messages=list(messages),
         response_schema=ar.REVIEW_SCHEMA)
    assert on["url"] != baseline["url"] and on["body"] != baseline["body"]
    assert on["json"]["format"] == ar.REVIEW_SCHEMA


def test_setting_off_also_strips_the_schema_on_the_native_route(
        settings, stable_ctx, monkeypatch):
    settings["local_structured_output"] = "off"
    seen = _capture_async(monkeypatch)
    _run(url=OLLAMA_NATIVE, model="gpt-oss:120b",
         messages=[{"role": "user", "content": "review this"}],
         response_schema=ar.REVIEW_SCHEMA)
    assert "format" not in seen["json"]


def test_setting_auto_is_the_default_and_unknown_values_do_not_disable_it(settings):
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["local_structured_output"] == "auto"
    for value in ("auto", "AUTO", "on", "", None):
        settings["local_structured_output"] = value
        assert llm_core._structured_output_enabled() is True, value
    for value in ("off", "OFF", "false", "0", "no", "disabled"):
        settings["local_structured_output"] = value
        assert llm_core._structured_output_enabled() is False, value


def test_a_constrained_answer_does_not_share_a_cache_entry_with_a_free_one(stable_ctx):
    args = (OLLAMA_NATIVE, "gpt-oss:120b", [{"role": "user", "content": "x"}], 0.1, 1200)
    plain = llm_core._get_cache_key(*args)
    constrained = llm_core._get_cache_key(*args, response_schema=ar.REVIEW_SCHEMA)
    assert plain != constrained
    # A key computed without a schema keeps the exact digest it always had.
    assert llm_core._get_cache_key(*args, response_schema=None) == plain


# ---------------------------------------------------------------------------
# The contract: what auto_review produces validates against its own schema
# ---------------------------------------------------------------------------

def _check_required(instance, schema, path="$"):
    """Minimal, dependency-free check of the required keys and value kinds the
    review schema declares. `jsonschema` is only a transitive dependency here,
    so the contract test must stand on its own."""
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(instance, dict), f"{path}: expected object"
        for key in schema.get("required", []):
            assert key in instance, f"{path}: missing required key {key!r}"
        for key, sub in (schema.get("properties") or {}).items():
            if key in instance:
                _check_required(instance[key], sub, f"{path}.{key}")
    elif kind == "array":
        assert isinstance(instance, list), f"{path}: expected array"
        for i, item in enumerate(instance):
            _check_required(item, schema.get("items") or {}, f"{path}[{i}]")
    elif kind == "string":
        assert isinstance(instance, str), f"{path}: expected string"
        if schema.get("enum"):
            assert instance in schema["enum"], f"{path}: {instance!r} not in enum"
    elif isinstance(kind, list):
        ok = ("null" in kind and instance is None) or \
             ("integer" in kind and isinstance(instance, int) and not isinstance(instance, bool))
        assert ok, f"{path}: {instance!r} does not match {kind}"


def _review_answer():
    """Exactly the object REVIEW_SCHEMA describes — what a constrained decode
    is guaranteed to produce."""
    return json.dumps({
        "verdict": "issues",
        "summary": "add() multiplies instead of adding",
        "findings": [
            {"severity": "error", "file": "src/calc.py", "line": 2,
             "evidence": "return a * b", "issue": "the request asks for a sum"},
            {"severity": "warning", "file": "src/calc.py", "line": None,
             "evidence": "print('debug')", "issue": "leftover debug print"},
        ],
    })


def _fake_review(monkeypatch, answer):
    seen = {}

    async def _fake(url, model, messages, **kwargs):
        seen.update(kwargs, url=url, model=model)
        return answer
    monkeypatch.setattr(llm_core, "llm_call_async", _fake, raising=False)
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, files, sha: {
        "diff": "-    return a - b\n+    return a * b\n+    print('debug')\n",
        "source": "checkpoint", "truncated": False})
    return seen


def test_review_turn_sends_its_own_schema(tmp_path, monkeypatch):
    seen = _fake_review(monkeypatch, _review_answer())
    asyncio.run(ar.review_turn(workspace=str(tmp_path), changed=["src/calc.py"],
                               checkpoint_sha="abc", user_text="fix add",
                               endpoint_url=OLLAMA_V1, model="qwen3.5:9b"))
    assert seen["response_schema"] is ar.REVIEW_SCHEMA


def test_review_output_validates_against_the_review_schema(tmp_path, monkeypatch):
    _fake_review(monkeypatch, _review_answer())
    res = asyncio.run(ar.review_turn(workspace=str(tmp_path), changed=["src/calc.py"],
                                     checkpoint_sha="abc", user_text="fix add",
                                     endpoint_url=OLLAMA_V1, model="qwen3.5:9b"))
    answer = {k: res[k] for k in ("verdict", "summary", "findings")}
    _check_required(answer, ar.REVIEW_SCHEMA)
    # Nothing was lost on the way through _parse / ground_findings.
    assert answer["verdict"] == "issues"
    assert [f["evidence"] for f in answer["findings"]] == ["return a * b", "print('debug')"]


def test_review_output_validates_with_jsonschema_when_available(tmp_path, monkeypatch):
    """Cross-check with a real validator where one happens to be installed.
    Skipped rather than depended on: this feature adds no dependency."""
    jsonschema = pytest.importorskip("jsonschema")
    _fake_review(monkeypatch, _review_answer())
    res = asyncio.run(ar.review_turn(workspace=str(tmp_path), changed=["src/calc.py"],
                                     checkpoint_sha="abc", user_text="fix add",
                                     endpoint_url=OLLAMA_V1, model="qwen3.5:9b"))
    jsonschema.validate({k: res[k] for k in ("verdict", "summary", "findings")},
                        ar.REVIEW_SCHEMA)
    jsonschema.Draft7Validator.check_schema(ar.REVIEW_SCHEMA)


def test_evidence_is_a_precondition_and_ground_findings_is_still_the_net():
    """The schema makes an evidence-less finding unsayable; it cannot make the
    evidence true. ground_findings stays, and stays load-bearing."""
    item = ar.REVIEW_SCHEMA["properties"]["findings"]["items"]
    assert "evidence" in item["required"]
    diff = "-    return a - b\n+    return a * b\n"
    res = ar.ground_findings([
        # Schema-legal (evidence is present) but invented: still demoted.
        {"severity": "error", "file": "x.py", "line": 9,
         "evidence": "<button id='refresh'>", "issue": "invented"},
        {"severity": "error", "file": "x.py", "line": 2,
         "evidence": "return a * b", "issue": "real"},
    ], diff, user_text="fix add")
    assert [(f["severity"], bool(f["grounded"])) for f in res["findings"]] == [
        ("warning", False), ("error", True)]
    assert res["ungrounded"] == 1


# ---------------------------------------------------------------------------
# An endpoint without the capability keeps working the way it always did
# ---------------------------------------------------------------------------

def test_review_on_an_endpoint_without_the_capability_still_parses_prose(
        settings, stable_ctx, tmp_path, monkeypatch):
    """No schema goes out, the model answers with prose around the JSON, and
    the tolerant parser does exactly what it did before this feature."""
    prose = ("Sure, here is my review:\n```json\n" + _review_answer() +
             "\n```\nHope that helps!")
    seen = _capture_async(
        monkeypatch, payload={"choices": [{"message": {"content": prose}}]})
    monkeypatch.setattr(ar, "turn_diff", lambda workspace, files, sha: {
        "diff": "-    return a - b\n+    return a * b\n+    print('debug')\n",
        "source": "checkpoint", "truncated": False})
    res = asyncio.run(ar.review_turn(workspace=str(tmp_path), changed=["src/calc.py"],
                                     checkpoint_sha="abc", user_text="fix add",
                                     endpoint_url=OPENAI_COMPAT, model="gpt-4o-mini"))
    assert "format" not in seen["json"]
    assert res["verdict"] == "issues"
    assert res["findings"][0]["issue"] == "the request asks for a sum"
    assert res["findings"][0]["grounded"] is True
