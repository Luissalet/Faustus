"""Tests for src/typed_choice.py (typed choice decisions) and its wiring:

* pure helpers offline: build_prompt, build_grammar, score_from_top_logprobs
  (incl. leading-space tokens and renormalisation over missing options),
  parse_letter;
* typed_choice() against a fake httpx client: logprobs path, fallback to
  generated when the server returns no logprobs, error when the letter can't
  be parsed;
* POST /api/typed-choice requires admin;
* the verify_claim tool wiring (src/tool_execution.py): typed_choice is only
  used when `typed_choice_logprobs` is on, and falls back to the unsettled
  deterministic verdict when the typed-choice call errors.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import middleware                                        # noqa: E402
from src import typed_choice as tc                                  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_prompt_lists_lettered_options_and_context():
    prompt = tc.build_prompt(
        "Which category?", ["billing", "technical support"], context="Message: hi",
    )
    assert "Message: hi" in prompt
    assert "Which category?" in prompt
    assert "A) billing" in prompt
    assert "B) technical support" in prompt
    assert prompt.strip().endswith("Answer with the letter only.")


def test_build_prompt_without_context_omits_it():
    prompt = tc.build_prompt("Yes or no?", ["yes", "no"])
    assert "A) yes" in prompt and "B) no" in prompt
    assert prompt.count("\n\n") >= 1


def test_letters_for_caps_at_26():
    assert tc.letters_for(3) == ["A", "B", "C"]
    assert len(tc.letters_for(30)) == 26
    assert tc.letters_for(30)[-1] == "Z"


def test_build_grammar_is_a_letter_alternation():
    grammar = tc.build_grammar(["A", "B", "C"])
    assert grammar == 'root ::= "A" | "B" | "C"'


def test_score_from_top_logprobs_softmax_renormalises():
    top_logprobs = [
        {"token": "A", "logprob": -0.1},
        {"token": "B", "logprob": -2.0},
        {"token": "C", "logprob": -5.0},  # not one of the offered letters
    ]
    probs = tc.score_from_top_logprobs(top_logprobs, ["A", "B"])
    assert set(probs) == {"A", "B"}
    assert probs["A"] > probs["B"]
    assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_score_from_top_logprobs_strips_leading_space_tokens():
    # llama.cpp / BPE tokenizers often emit the option letter with a leading
    # space from the word boundary (" A" rather than "A").
    top_logprobs = [
        {"token": " A", "logprob": -0.2},
        {"token": " B", "logprob": -1.5},
    ]
    probs = tc.score_from_top_logprobs(top_logprobs, ["A", "B"])
    assert probs["A"] > 0
    assert probs["B"] > 0
    assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_score_from_top_logprobs_case_sensitive():
    # lowercase "a" must NOT be folded into letter "A".
    top_logprobs = [{"token": "a", "logprob": -0.1}]
    probs = tc.score_from_top_logprobs(top_logprobs, ["A", "B"])
    assert probs == {"A": 0.0, "B": 0.0}


def test_score_from_top_logprobs_missing_option_gets_zero():
    top_logprobs = [{"token": "A", "logprob": -0.1}]
    probs = tc.score_from_top_logprobs(top_logprobs, ["A", "B", "C"])
    assert probs["A"] == 1.0
    assert probs["B"] == 0.0
    assert probs["C"] == 0.0


def test_score_from_top_logprobs_empty_input_is_all_zero():
    probs = tc.score_from_top_logprobs([], ["A", "B"])
    assert probs == {"A": 0.0, "B": 0.0}


def test_parse_letter_exact_and_embedded():
    assert tc.parse_letter("A", ["A", "B"]) == "A"
    assert tc.parse_letter("The answer is B.", ["A", "B"]) == "B"
    assert tc.parse_letter("b", ["A", "B"]) == "B"
    assert tc.parse_letter("", ["A", "B"]) is None
    assert tc.parse_letter("nothing useful here", ["A", "B"]) is None


# ---------------------------------------------------------------------------
# typed_choice() / generated_choice() against a fake httpx client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._payload)


def _patch_endpoint(monkeypatch, payload):
    """Patch out the HTTP client and the local-model gate so typed_choice()
    talks to a fake server, with no real network or engine-swap machinery
    involved."""
    from src import llm_core
    from contextlib import asynccontextmanager

    fake_client = _FakeClient(payload)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: fake_client)

    @asynccontextmanager
    async def _noop_slot(*args, **kwargs):
        yield

    monkeypatch.setattr(llm_core, "_local_model_slot", _noop_slot)
    return fake_client


@pytest.mark.asyncio
async def test_typed_choice_uses_logprobs_when_present(monkeypatch):
    payload = {
        "choices": [{
            "message": {"content": "A"},
            "logprobs": {"content": [{
                "token": "A", "logprob": -0.05,
                "top_logprobs": [
                    {"token": "A", "logprob": -0.05},
                    {"token": "B", "logprob": -3.0},
                ],
            }]},
        }],
    }
    fake_client = _patch_endpoint(monkeypatch, payload)
    result = await tc.typed_choice(
        "Is Paris the capital of France?", ["yes", "no"],
        url="http://127.0.0.1:8082", model="local-model",
    )
    assert result.get("error") is None
    assert result["choice"] == "yes"
    assert result["letter"] == "A"
    assert result["method"] == "logprobs"
    assert result["probability_status"] == tc.PROBABILITY_STATUS
    assert result["probabilities"]["yes"] > result["probabilities"]["no"]
    assert abs(sum(result["probabilities"].values()) - 1.0) < 1e-9
    assert result["margin"] > 0
    # the request actually carried a grammar + logprobs request.
    body = fake_client.calls[0]["json"]
    assert body["logprobs"] is True
    assert body["top_logprobs"] == 20
    assert body["max_tokens"] == 1
    assert 'root ::=' in body["grammar"]
    assert fake_client.calls[0]["url"] == "http://127.0.0.1:8082/v1/chat/completions"


@pytest.mark.asyncio
async def test_typed_choice_falls_back_to_generated_without_logprobs(monkeypatch):
    payload = {"choices": [{"message": {"content": "B"}, "logprobs": None}]}
    _patch_endpoint(monkeypatch, payload)
    result = await tc.typed_choice(
        "Which one?", ["alpha", "beta"], url="http://127.0.0.1:8082", model="local-model",
    )
    assert result.get("error") is None
    assert result["method"] == "generated"
    assert result["choice"] == "beta"
    assert result["probabilities"]["beta"] == 1.0
    assert result["probabilities"]["alpha"] == 0.0


@pytest.mark.asyncio
async def test_typed_choice_errors_when_letter_unparseable(monkeypatch):
    payload = {"choices": [{"message": {"content": "sorry cannot answer clearly!!"}, "logprobs": None}]}
    _patch_endpoint(monkeypatch, payload)
    result = await tc.typed_choice(
        "Which one?", ["alpha", "beta"], url="http://127.0.0.1:8082", model="local-model",
    )
    assert result.get("error")
    assert result["choice"] is None


@pytest.mark.asyncio
async def test_typed_choice_never_raises_on_transport_failure(monkeypatch):
    from src import llm_core
    from contextlib import asynccontextmanager

    class _BoomClient:
        async def post(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _BoomClient())

    @asynccontextmanager
    async def _noop_slot(*args, **kwargs):
        yield

    monkeypatch.setattr(llm_core, "_local_model_slot", _noop_slot)
    result = await tc.typed_choice(
        "Which one?", ["alpha", "beta"], url="http://127.0.0.1:8082", model="local-model",
    )
    assert result.get("error")
    assert "connection refused" in result["error"]


def test_typed_choice_raises_on_empty_options():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(tc.typed_choice("Q?", []))


@pytest.mark.asyncio
async def test_typed_choice_without_endpoint_returns_error(monkeypatch):
    monkeypatch.setattr(tc, "_default_endpoint", lambda url, model: (None, None, {}))
    result = await tc.typed_choice("Q?", ["a", "b"])
    assert result.get("error")


@pytest.mark.asyncio
async def test_generated_choice_baseline(monkeypatch):
    payload = {"choices": [{"message": {"content": "A) alpha"}}]}
    _patch_endpoint(monkeypatch, payload)
    result = await tc.generated_choice(
        "Which one?", ["alpha", "beta"], url="http://127.0.0.1:8082", model="local-model",
    )
    assert result["method"] == "generated"
    assert result["choice"] == "alpha"


# ---------------------------------------------------------------------------
# HTTP surface — POST /api/typed-choice is admin-only
# ---------------------------------------------------------------------------

from routes.typed_choice_routes import setup_typed_choice_routes            # noqa: E402


@pytest.fixture
def tc_client(monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_typed_choice_routes())
    return TestClient(app)


@pytest.fixture
def tc_locked(monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(setup_typed_choice_routes())
    return TestClient(app, raise_server_exceptions=False)


def test_route_requires_admin(tc_locked):
    resp = tc_locked.post("/api/typed-choice", json={"question": "Q?", "options": ["a", "b"]})
    assert resp.status_code == 403


def test_route_calls_typed_choice_when_authorised(tc_client, monkeypatch):
    async def fake_typed_choice(question, options, **kwargs):
        assert question == "Q?"
        assert options == ["a", "b"]
        return {"choice": "a", "index": 0, "letter": "A", "probabilities": {"a": 1.0, "b": 0.0},
                "margin": 1.0, "method": "logprobs", "probability_status": tc.PROBABILITY_STATUS,
                "prompt_sha256": "x", "model": "m", "elapsed_ms": 1.0}

    import routes.typed_choice_routes as routes_mod
    monkeypatch.setattr(routes_mod.typed_choice, "typed_choice", fake_typed_choice)
    resp = tc_client.post("/api/typed-choice", json={"question": "Q?", "options": ["a", "b"]})
    assert resp.status_code == 200
    assert resp.json()["choice"] == "a"


def test_route_rejects_empty_options(tc_client):
    resp = tc_client.post("/api/typed-choice", json={"question": "Q?", "options": []})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# verify_claim tool wiring (src/tool_execution.py)
# ---------------------------------------------------------------------------

from src import tool_execution                                              # noqa: E402
from src import settings as settings_mod                                    # noqa: E402


def test_typed_choice_logprobs_default_is_off():
    assert settings_mod.DEFAULT_SETTINGS["typed_choice_logprobs"] is False


@pytest.mark.asyncio
async def test_verify_claim_setting_off_never_calls_typed_choice(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setitem(settings_mod.DEFAULT_SETTINGS, "typed_choice_logprobs", False)
    settings_mod._invalidate_caches()

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("typed_choice must not be called when the setting is off")

    monkeypatch.setattr("src.typed_choice.typed_choice", boom)
    result = await tool_execution._verify_claim_with_optional_judge(
        "the sky is blue", "completely unrelated text about oceans",
    )
    assert called["n"] == 0
    assert result["layer"] is None  # nothing deterministic settled it, no judge used


@pytest.mark.asyncio
async def test_verify_claim_setting_on_uses_typed_choice_when_unsettled(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setitem(settings_mod.DEFAULT_SETTINGS, "typed_choice_logprobs", True)
    settings_mod._invalidate_caches()

    async def fake_typed_choice(question, options, **kwargs):
        assert options == ["supported", "not supported"]
        return {"choice": "supported", "index": 0, "letter": "A",
                "probabilities": {"supported": 0.9, "not supported": 0.1},
                "margin": 0.8, "method": "logprobs",
                "probability_status": tc.PROBABILITY_STATUS,
                "prompt_sha256": "x", "model": "m", "elapsed_ms": 1.0}

    monkeypatch.setattr("src.typed_choice.typed_choice", fake_typed_choice)
    result = await tool_execution._verify_claim_with_optional_judge(
        "this claim has no lexical overlap", "totally different unrelated words here",
    )
    assert result["layer"] == 5
    assert result["model_judgement"] is True
    assert result["judgement"]["supported"] is True


@pytest.mark.asyncio
async def test_verify_claim_falls_back_when_typed_choice_errors(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setitem(settings_mod.DEFAULT_SETTINGS, "typed_choice_logprobs", True)
    settings_mod._invalidate_caches()

    async def fake_typed_choice(question, options, **kwargs):
        return {"error": "no local model endpoint configured", "choice": None}

    monkeypatch.setattr("src.typed_choice.typed_choice", fake_typed_choice)
    result = await tool_execution._verify_claim_with_optional_judge(
        "this claim has no lexical overlap", "totally different unrelated words here",
    )
    # falls back to the unsettled deterministic verdict — no judgement.
    assert result["layer"] is None
    assert result["judgement"] is None


@pytest.mark.asyncio
async def test_verify_claim_setting_on_but_already_settled_skips_typed_choice(monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setitem(settings_mod.DEFAULT_SETTINGS, "typed_choice_logprobs", True)
    settings_mod._invalidate_caches()

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("typed_choice must not be called when a deterministic layer settles it")

    monkeypatch.setattr("src.typed_choice.typed_choice", boom)
    # exact substring -> layer 1, settled deterministically, no judge needed.
    result = await tool_execution._verify_claim_with_optional_judge(
        "the sky is blue", "Everyone agrees the sky is blue today.",
    )
    assert called["n"] == 0
    assert result["layer"] == 1
