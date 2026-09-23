"""Typed decisions (src/typed_decision.py) and their HTTP surface
(routes/typed_decision_routes.py). The callers are covered in
tests/test_typed_decision_wiring.py.

No network: every model answer comes from an ``httpx.MockTransport`` that
plays either wire shape (OpenAI-compatible ``top_logprobs`` or native
Ollama ``logprobs``), and residency is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import typed_decision as td  # noqa: E402

LLAMA_URL = "http://127.0.0.1:8082/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "helper-3b"


# ── fakes ───────────────────────────────────────────────────────────────────

def _lp(p: float) -> float:
    return math.log(p)


def openai_body(tops, content="A"):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "logprobs": {"content": [{"token": content, "logprob": -0.01,
                                                   "top_logprobs": tops}]}}]}


def ollama_body(tops, content="A"):
    return {"message": {"role": "assistant", "content": content}, "done": True,
            "logprobs": [{"token": content, "logprob": -0.01, "top_logprobs": tops}]}


class FakeServer:
    """Records requests; answers with `responder(payload) -> (status, json)`."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.requests.append({"url": str(request.url), "payload": payload})
        status, body = self.responder(payload)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)


@pytest.fixture()
def settings(monkeypatch):
    values = {}

    def _get(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr("src.settings.get_setting", _get)
    return values


@pytest.fixture()
def endpoint(monkeypatch, settings):
    """Resolve to a loopback llama-server holding MODEL; not busy."""
    state = {"url": LLAMA_URL, "resident": [MODEL], "residency_calls": 0}

    def _resolve(prefix, *a, **k):
        return state["url"], MODEL, {}

    def _resident(url):
        state["residency_calls"] += 1
        return state["resident"]

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", _resolve)
    monkeypatch.setattr("src.background_job_guard._resident_model_names", _resident)
    monkeypatch.setattr("src.background_job_guard.model_busy", lambda url: False)
    td._reset_stats()
    yield state
    td._TRANSPORT = None
    td._reset_stats()


def serve(monkeypatch, responder):
    server = FakeServer(responder)
    monkeypatch.setattr(td, "_TRANSPORT", httpx.MockTransport(server))
    return server


BOOL = td.Field("needs_web", "Is this time-sensitive?", "bool")


def run(coro):
    return asyncio.run(coro)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_leading_space_and_punctuated_tokens_add_up_to_one_letter():
    dist, mass = td.score_top_logprobs(
        [{"token": "A", "logprob": _lp(0.6)}, {"token": " A", "logprob": _lp(0.2)},
         {"token": "B)", "logprob": _lp(0.1)}, {"token": "Billing", "logprob": _lp(0.05)},
         {"token": "a", "logprob": _lp(0.05)}],
        ["A", "B"])
    assert mass == pytest.approx(0.9)
    assert dist["A"] == pytest.approx(0.8 / 0.9)
    assert dist["B"] == pytest.approx(0.1 / 0.9)


def test_token_letter_is_case_sensitive():
    assert td.token_letter(" A") == "A"
    assert td.token_letter("(C") == "C"
    assert td.token_letter("a") is None
    assert td.token_letter("An") is None


def test_prefix_is_byte_identical_across_fields_and_question_is_last():
    context = "Ada moved to Bluehaven in March. «Cordera Labs» hired her."
    fields = [td.Field("a", "Is Ada a person?", "bool"),
              td.Field("b", "Where does Ada live?", ["Bluehaven", "Villanueva", "unknown"]),
              td.Field("c", "Kind?", ["person", "place"], descriptions=["a human", "a location"])]
    messages = [td.build_messages(context, f) for f in fields]
    systems = {m[0]["content"].encode("utf-8") for m in messages}
    assert len(systems) == 1
    prefix = td.prompt_prefix(context).encode("utf-8")
    for fld, msg in zip(fields, messages):
        user = msg[1]["content"].encode("utf-8")
        assert user.startswith(prefix)
        assert user[len(prefix):] == td.field_suffix(fld).encode("utf-8")
        assert fld.question in msg[1]["content"].split('"""')[-1]
    assert "A) person — a human" in messages[2][1]["content"]


def test_field_from_dict_validates():
    assert td.field_from_dict({"name": "x", "question": "q", "choices": "bool"}).options() == ["yes", "no"]
    assert td.field_from_dict({"name": "x", "question": "q", "choices": ["only"]}) is None
    assert td.field_from_dict({"name": "", "question": "q"}) is None
    assert td.field_from_dict({"name": "x", "question": "q", "choices": 3}) is None


def test_payload_shapes():
    msgs = td.build_messages("ctx", BOOL)
    p = td.build_payload("openai", MODEL, msgs, 2, LLAMA_URL)
    assert p["max_tokens"] == 1 and p["temperature"] == 0 and p["logprobs"] is True
    assert p["top_logprobs"] >= 5 and p["cache_prompt"] is True
    assert p["chat_template_kwargs"] == {"enable_thinking": False}
    remote = td.build_payload("openai", MODEL, msgs, 2, "https://api.example.test/v1/chat/completions")
    assert "cache_prompt" not in remote and "chat_template_kwargs" not in remote
    o = td.build_payload("ollama", MODEL, msgs, 2, OLLAMA_URL)
    assert o["think"] is False and o["options"] == {"num_predict": 1, "temperature": 0}
    assert o["logprobs"] is True and o["top_logprobs"] >= 5 and o["stream"] is False


def test_wire_detection():
    assert td.wire_for("http://127.0.0.1:8082/v1/chat/completions")[0] == "openai"
    assert td.wire_for("http://127.0.0.1:11434/api/chat") == ("ollama", "http://127.0.0.1:11434/api/chat")
    assert td.wire_for("http://127.0.0.1:11434/v1/chat/completions") == ("ollama", "http://127.0.0.1:11434/api/chat")
    assert td.wire_for("faustus-cli://x")[0] == "unsupported"


# ── decide: both wire shapes ────────────────────────────────────────────────

def test_openai_shape_logprobs(monkeypatch, endpoint):
    server = serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "A", "logprob": _lp(0.9)}, {"token": "B", "logprob": _lp(0.08)}])))
    out = run(td.decide("Who won yesterday?", [BOOL]))
    d = out["needs_web"]
    assert d.method == "logprobs" and d.value == "yes"
    assert d.confidence == pytest.approx(0.9 / 0.98, rel=1e-4)
    assert d.mass == pytest.approx(0.98, rel=1e-4)
    assert set(d.distribution) == {"yes", "no"}
    assert server.requests[0]["url"] == LLAMA_URL
    assert server.requests[0]["payload"]["max_tokens"] == 1


def test_ollama_native_shape_with_leading_space_tokens(monkeypatch, endpoint):
    endpoint["url"] = "http://127.0.0.1:11434/v1/chat/completions"
    server = serve(monkeypatch, lambda p: (200, ollama_body(
        [{"token": "B", "logprob": _lp(0.5)}, {"token": "", "logprob": _lp(0.1)},
         {"token": " B", "logprob": _lp(0.3)}, {"token": "A", "logprob": _lp(0.05)}], content="B")))
    d = run(td.decide("2+2", [BOOL]))["needs_web"]
    assert server.requests[0]["url"] == OLLAMA_URL
    assert server.requests[0]["payload"]["think"] is False
    assert d.value == "no" and d.method == "logprobs"
    assert d.mass == pytest.approx(0.85, rel=1e-4)


def test_ollama_think_flag_refused_is_retried_without_it(monkeypatch, endpoint):
    endpoint["url"] = OLLAMA_URL

    def responder(p):
        if "think" in p:
            return 400, '{"error":"model does not support thinking"}'
        return 200, ollama_body([{"token": "A", "logprob": _lp(0.95)}])

    server = serve(monkeypatch, responder)
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value == "yes" and len(server.requests) == 2


def test_missing_logprobs_falls_back_to_the_letter(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, {"choices": [{"message": {"content": " B"}}]}))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.method == "letter" and d.value == "no" and d.confidence is None
    assert td.stats()["fallbacks"] == 1


def test_garbage_is_unknown(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, {"choices": [{"message": {"content": "Well, it depends"}}]}))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.reason == "unparsed"
    serve(monkeypatch, lambda p: (200, "not json at all"))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.method == "unavailable" and d.reason == "error"


def test_low_mass_is_unknown(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "The", "logprob": _lp(0.8)}, {"token": "A", "logprob": _lp(0.15)},
         {"token": "B", "logprob": _lp(0.01)}], content="The")))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.reason == "low_mass" and d.best == "yes"
    assert d.mass == pytest.approx(0.16, rel=1e-3)


def test_logprobs_without_any_allowed_letter_is_low_mass(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, openai_body([{"token": "The", "logprob": -0.01}], content="The")))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.reason == "low_mass" and d.mass == 0.0


def test_low_confidence_is_unknown_and_threshold_is_overridable(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "A", "logprob": _lp(0.55)}, {"token": "B", "logprob": _lp(0.45)}])))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.reason == "low_confidence" and d.best == "yes"
    d = run(td.decide("ctx", [BOOL], min_confidence=0))["needs_web"]
    assert d.value == "yes"


def test_not_resident_is_unavailable_without_calling_the_model(monkeypatch, endpoint):
    endpoint["resident"] = ["some-other-model:70b"]
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.method == "unavailable" and d.reason == "model_not_resident"
    assert server.requests == []
    endpoint["resident"] = None
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.reason == "residency_unknown" and server.requests == []


def test_may_load_setting_skips_the_residency_check(monkeypatch, endpoint, settings):
    endpoint["resident"] = []
    settings["typed_decisions_may_load"] = True
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value == "yes" and len(server.requests) == 1


def test_busy_runner_is_unavailable(monkeypatch, endpoint):
    monkeypatch.setattr("src.background_job_guard.model_busy", lambda url: True)
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.reason == "model_busy" and server.requests == []


def test_no_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", lambda *a, **k: (None, None, None))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.method == "unavailable" and d.reason == "no_endpoint"


def test_timeout_is_a_hard_budget(monkeypatch, endpoint):
    async def slow_post(url, payload, headers, timeout):
        await asyncio.sleep(2)
        return openai_body([{"token": "A", "logprob": -0.01}])

    monkeypatch.setattr(td, "_post", slow_post)
    fields = [BOOL, td.Field("second", "Another?", "bool")]
    import time as _t
    t0 = _t.monotonic()
    out = run(td.decide("ctx", fields, timeout_s=0.2))
    assert _t.monotonic() - t0 < 1.0
    assert all(d.method == "unavailable" and d.reason == "timeout" for d in out.values())


def test_multi_field_requests_share_the_prefix_bytes(monkeypatch, endpoint):
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    fields = [td.Field("person", "Is Ada a person?", "bool"),
              td.Field("where", "Where?", ["Bluehaven", "Villanueva"]),
              td.Field("kind", "Kind?", ["person", "project", "place"])]
    out = run(td.decide("Ada lives in Bluehaven.", fields))
    assert list(out) == ["person", "where", "kind"]
    assert len(server.requests) == 3
    systems = [r["payload"]["messages"][0]["content"] for r in server.requests]
    users = [r["payload"]["messages"][1]["content"].encode("utf-8") for r in server.requests]
    prefix = td.prompt_prefix("Ada lives in Bluehaven.").encode("utf-8")
    assert len(set(systems)) == 1
    assert all(u.startswith(prefix) for u in users)
    assert len({u[len(prefix):] for u in users}) == 3


def test_off_switch_makes_no_call(monkeypatch, endpoint, settings):
    settings["typed_decisions_enabled"] = False
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.reason == "disabled" and server.requests == [] and endpoint["residency_calls"] == 0


def test_decide_never_raises(monkeypatch, endpoint):
    def boom(*a, **k):
        raise RuntimeError("settings store exploded")

    monkeypatch.setattr(td, "_prepare", boom)
    d = run(td.decide("ctx", [BOOL]))["needs_web"]
    assert d.value is None and d.method == "unavailable"
    assert run(td.decide("ctx", [])) == {}


def test_decide_sync_inside_and_outside_a_loop(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, openai_body([{"token": "B", "logprob": -0.01}])))
    assert td.decide_sync("ctx", [BOOL])["needs_web"].value == "no"

    async def inside():
        return td.decide_sync("ctx", [BOOL])

    assert run(inside())["needs_web"].value == "no"


def test_stats_never_keep_the_context(monkeypatch, endpoint):
    serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    run(td.decide("secret context about Ada", [BOOL], caller="unit"))
    st = td.stats()
    assert st["calls"] == 1 and st["logprobs"] == 1 and st["p50_ms"] is not None
    assert "Ada" not in json.dumps(st)
    assert st["recent"][-1]["caller"] == "unit"


# ── route ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(endpoint, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import typed_decision_routes
    from src.auth_helpers import require_user

    monkeypatch.setattr(typed_decision_routes, "effective_user", lambda request: "ada")
    app = FastAPI()
    app.include_router(typed_decision_routes.setup_typed_decision_routes())
    app.dependency_overrides[require_user] = lambda: "ada"
    return TestClient(app)


def test_route_decides_and_reports_stats(client, monkeypatch):
    server = serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "C", "logprob": _lp(0.9)}, {"token": "A", "logprob": _lp(0.05)}], content="C")))
    r = client.post("/api/typed-decision", json={
        "context": "Cordera Labs opened an office in Villanueva.",
        "fields": [{"name": "kind", "question": "What is Cordera Labs?",
                    "choices": ["person", "place", "organization"]},
                   {"name": "yn", "question": "Is it a company?", "choices": "bool"}]})
    assert r.status_code == 200, r.text
    body = r.json()["decisions"]
    assert body["kind"]["value"] == "organization" and body["kind"]["method"] == "logprobs"
    assert body["yn"]["value"] is None  # "C" is not an allowed letter for a yes/no
    assert len(server.requests) == 2
    st = client.get("/api/typed-decision/stats").json()
    assert st["calls"] == 1 and st["fields"] == 2


@pytest.mark.parametrize("payload", [
    {"context": "", "fields": [{"name": "a", "question": "q", "choices": "bool"}]},
    {"context": "x", "fields": []},
    {"context": "x", "fields": [{"name": "a", "question": "q", "choices": ["one"]}]},
    {"context": "x", "fields": [{"name": "a", "question": "q"}, {"name": "a", "question": "q"}]},
    {"context": "x", "fields": [{"name": "a", "question": "q"}], "purpose": "whatever"},
])
def test_route_rejects_bad_input(client, payload):
    assert client.post("/api/typed-decision", json=payload).status_code == 400


def test_route_requires_a_user():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from routes import typed_decision_routes
    from src.auth_helpers import require_user

    def deny():
        raise HTTPException(401, "login required")

    app = FastAPI()
    app.include_router(typed_decision_routes.setup_typed_decision_routes())
    app.dependency_overrides[require_user] = deny
    c = TestClient(app)
    assert c.get("/api/typed-decision/stats").status_code == 401
    assert c.post("/api/typed-decision", json={"context": "x", "fields": []}).status_code == 401


# ── response shapes as the servers actually send them ───────────────────────
# Trimmed from a live probe of a loopback llama-server (3B helper) and a
# local Ollama 0.34 (27B, native /api/chat, think=false) asked the same
# three-way question ("billing / technical / sales" for a double charge).

_LIVE_LLAMA_TOP = [
    {"token": "A", "logprob": -0.004585}, {"token": "B", "logprob": -5.417728},
    {"token": "C", "logprob": -8.902763}, {"token": "Billing", "logprob": -14.813136},
    {"token": "D", "logprob": -15.104609}, {"token": "T", "logprob": -15.652663}]
_LIVE_OLLAMA_TOP = [
    {"token": "A", "logprob": -0.082591}, {"token": "", "logprob": -2.572513},
    {"token": "B", "logprob": -6.756122}, {"token": "billing", "logprob": -7.707086},
    {"token": "Billing", "logprob": -8.224601}, {"token": "<tool_call>", "logprob": -8.23153},
    {"token": " A", "logprob": -8.244203}, {"token": "C", "logprob": -8.88618},
    {"token": "\n\n", "logprob": -9.529243}, {"token": "D", "logprob": -9.867026}]


def test_live_llama_server_shape():
    fld = td.Field("dept", "department?", ["billing", "technical", "sales"])
    d = td.interpret(fld, openai_body(_LIVE_LLAMA_TOP), "openai", min_confidence=0.7, min_mass=0.5)
    assert d.value == "billing" and d.method == "logprobs"
    assert d.confidence > 0.99 and d.mass > 0.99


def test_live_ollama_native_shape():
    fld = td.Field("dept", "department?", ["billing", "technical", "sales"])
    d = td.interpret(fld, ollama_body(_LIVE_OLLAMA_TOP), "ollama", min_confidence=0.7, min_mass=0.5)
    assert d.value == "billing" and d.method == "logprobs"
    # the empty token took ~8% of the probability: mass says so, the
    # renormalised confidence does not
    assert 0.9 < d.mass < 0.93 and d.confidence > 0.99
