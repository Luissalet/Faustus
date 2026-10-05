import asyncio
import json

import httpx
import pytest

from src import typed_decision as td


@pytest.fixture
def native(monkeypatch):
    settings = {**td.DEFAULTS, "typed_decision_provider": "ollaya", "typed_decisions_may_load": False}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: settings.get(key, default))
    td._reset_stats()
    return settings


def run_request(monkeypatch, responder):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "laya:multilingual"}]})
        return httpx.Response(200, json=responder(json.loads(request.content)))
    monkeypatch.setattr(td, "_TRANSPORT", httpx.MockTransport(handler))
    fields = [td.Field("needs_web", "Necesita datos actuales?"), td.Field("kind", "Tipo?", ["person", "place"])]
    return asyncio.run(td.decide("Noticias de hoy", fields)), requests


def answer(payload):
    return {"model": payload["model"], "answers": {
        name: {"type": "noul", "noul": 0.95} if q["type"] == "noul" else
              {"type": "choice", "choice": next(iter(q["criteria"])), "confidence": 0.95,
               "probabilities": dict(zip(q["criteria"], [0.95, 0.05]))}
        for name, q in payload["questions"].items()}}


def test_all_fields_one_native_call_and_existing_decision_contract(native, monkeypatch):
    decisions, requests = run_request(monkeypatch, answer)
    assert len(requests) == 2  # one residency probe, one multi-question inference
    assert requests[1].url.path == "/v1/systemone"
    assert decisions["needs_web"].value == "yes" and decisions["kind"].value == "person"
    assert decisions["kind"].method == "systemone"
    assert td.stats()["systemone"] == 2


@pytest.mark.parametrize("bad", ["NaN", -1, 2, True, 0.5])
def test_invalid_or_uncertain_native_probability_abstains(native, monkeypatch, bad):
    def malformed(payload):
        data = answer(payload)
        data["answers"]["kind"]["confidence"] = bad
        return data
    decisions, _ = run_request(monkeypatch, malformed)
    assert decisions["kind"].value is None
    assert decisions["needs_web"].value == "yes"


def test_missing_native_field_does_not_become_guessed_answer(native, monkeypatch):
    def partial(payload):
        data = answer(payload)
        del data["answers"]["kind"]
        return data
    decisions, _ = run_request(monkeypatch, partial)
    assert decisions["kind"].reason == "malformed_answer"


def test_remote_endpoint_is_never_called(native, monkeypatch):
    native["typed_decision_ollaya_url"] = "https://remote.example"
    monkeypatch.setattr(td, "_TRANSPORT", httpx.MockTransport(lambda req: pytest.fail("network must not run")))
    out = asyncio.run(td.decide("private", [td.Field("a", "A?")]))
    assert out["a"].reason == "invalid_local_endpoint"


def test_cold_model_no_implicit_load(native, monkeypatch):
    def handler(request):
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": []})
    monkeypatch.setattr(td, "_TRANSPORT", httpx.MockTransport(handler))
    out = asyncio.run(td.decide("private", [td.Field("a", "A?")]))
    assert out["a"].reason == "model_not_resident"


def test_full_call_timeout_is_bounded(native, monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    monkeypatch.setattr(td, "_TRANSPORT", httpx.MockTransport(handler))
    out = asyncio.run(td.decide("private", [td.Field("a", "A?")], timeout_s=0.01))
    assert out["a"].reason == "timeout"


@pytest.mark.parametrize('bad', [None, True, '0.95', -0.1, 1.1, float('nan'), 0.5])
def test_native_bool_probability_rejects_malformed_and_tie(native, monkeypatch, bad):
    def malformed(payload):
        data = answer(payload)
        data['answers']['needs_web']['noul'] = bad
        return data
    decisions, _ = run_request(monkeypatch, malformed)
    assert decisions['needs_web'].value is None
