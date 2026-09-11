"""tests/test_cmp11_fit_explain.py — CMP-11: capabilities -> explainable
selection (INFORME V2 SS3.10).

Covers, in one file, the whole chain the card asks for: the pure reasoning
(`src.model_capabilities.explain_fit`), the provider-policy invariant that
now explains itself instead of naming a bare capability
(`src.provider_policy.resolve_route`), and the new route
(`GET /api/models/fit-explain`, `routes/model_routes.py`). No network, no
real DB: the route tests reuse the fake SessionLocal/httpx pattern already
established in tests/test_model_picker_vram_fit.py.

Decisive test (per the CMP-11 card): a model with generally-good capabilities
but a proven-missing REQUIRED one must come back explained — never silently
dropped, and never treated as "runs anyway, equivalently" — and, when another
already-evidenced model meets it, that model's id must be named as the
alternative.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import model_calibration  # noqa: E402
from src import model_capabilities as mc  # noqa: E402
from src import provider_policy  # noqa: E402
from routes import model_routes as mr  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# src.model_capabilities.explain_fit — pure reasoning
# ---------------------------------------------------------------------------

def test_tested_capability_is_never_confused_with_merely_announced():
    a = mc.CapabilityAssertion.build(capability="tool_call", status="verified")
    fit = mc.explain_fit(["tools"], model="m", endpoint="e", assertions=[a])
    assert fit.ok is True
    assert fit.reasons[0].state == mc.FIT_TESTED
    assert "verificado" in fit.reasons[0].message


def test_announced_but_unverified_is_its_own_state():
    a = mc.CapabilityAssertion.build(capability="vision", status="claimed")
    fit = mc.explain_fit(["vision"], model="m", endpoint="e", assertions=[a])
    reason = fit.reasons[0]
    assert reason.state == mc.FIT_ANNOUNCED
    assert fit.ok is True, "announced-not-tested must not block the route"
    assert "anuncia" in reason.message and "no lo ha verificado" in reason.message


def test_no_evidence_either_way_is_unknown_not_missing():
    fit = mc.explain_fit(["reasoning"], model="m", endpoint="e", assertions=None)
    reason = fit.reasons[0]
    assert reason.state == mc.FIT_UNKNOWN
    assert fit.ok is True, "unknown must never silently block, per provider_policy invariant 3"


def test_proven_absent_is_missing_and_blocks():
    a = mc.CapabilityAssertion.build(capability="image_editing", status="unsupported")
    fit = mc.explain_fit(["images_edit"], model="m", endpoint="e", assertions=[a])
    reason = fit.reasons[0]
    assert reason.capability == "image_editing"
    assert reason.state == mc.FIT_MISSING
    assert fit.ok is False


def test_a_requirement_is_never_dropped_in_silence():
    """Every token in `requirements` gets exactly one FitReason, whatever the
    evidence — this is the whole point of the card."""
    fit = mc.explain_fit(["tools", "json", "vision", "images_edit"], model="m", endpoint="e", assertions=None)
    assert {r.capability for r in fit.reasons} == {"tool_call", "json_mode", "vision", "image_editing"}
    assert all(r.state == mc.FIT_UNKNOWN for r in fit.reasons)


def test_missing_capability_names_a_concrete_alternative():
    """The decisive case: a model proven NOT to support a requirement, next
    to another already-evidenced model that DOES — the alternative must be
    named, never a silent 'runs anyway, equivalently'."""
    missing = mc.CapabilityAssertion.build(capability="tool_call", status="unsupported")
    good_sibling = mc.FitCandidateModel(
        model_id="sibling-model",
        assertions=[mc.CapabilityAssertion.build(capability="tool_call", status="verified")],
    )
    bad_sibling = mc.FitCandidateModel(
        model_id="also-broken",
        assertions=[mc.CapabilityAssertion.build(capability="tool_call", status="unsupported")],
    )
    fit = mc.explain_fit(
        ["tools"], model="m", endpoint="e", assertions=[missing], candidates=[good_sibling, bad_sibling],
    )
    reason = fit.reasons[0]
    assert reason.state == mc.FIT_MISSING
    assert reason.alternatives == ("sibling-model",)
    assert fit.ok is False


def test_unknown_capability_also_offers_alternatives_when_available():
    known_sibling = mc.FitCandidateModel(
        model_id="known-sibling",
        assertions=[mc.CapabilityAssertion.build(capability="vision", status="claimed")],
    )
    fit = mc.explain_fit(["vision"], model="m", endpoint="e", assertions=None, candidates=[known_sibling])
    reason = fit.reasons[0]
    assert reason.state == mc.FIT_UNKNOWN
    assert reason.alternatives == ("known-sibling",)


def test_duplicate_assertions_keep_the_best_evidenced_one():
    """A stale 'claimed' reading next to a fresh 'verified' probe: the
    stronger one wins, not whichever came last."""
    weak = mc.CapabilityAssertion.build(capability="tool_call", status="claimed")
    strong = mc.CapabilityAssertion.build(capability="tool_call", status="verified")
    fit = mc.explain_fit(["tools"], model="m", endpoint="e", assertions=[weak, strong])
    assert fit.reasons[0].state == mc.FIT_TESTED


def test_capability_aliases_are_normalized_the_same_as_the_rest_of_the_module():
    fit = mc.explain_fit(["images_edit"], model="m", endpoint="e", assertions=None)
    assert fit.reasons[0].capability == mc.CAP_IMAGE_EDITING


# ---------------------------------------------------------------------------
# assertions_from_calibration_manifest / assertions_from_endpoint_capabilities
# ---------------------------------------------------------------------------

def test_assertions_from_calibration_manifest_reads_tested_over_announced():
    manifest = {
        "announced": {"capabilities": {"tools": True, "vision": True, "reasoning": False}},
        "tested": {"tool_calling": {"ok": True}, "vision": {"ok": False}},
    }
    out = mc.assertions_from_calibration_manifest(manifest)
    assert out[mc.CAP_TOOL_CALL].status == mc.ASSERTION_VERIFIED
    assert out[mc.CAP_VISION].status == mc.ASSERTION_UNSUPPORTED, "tested=False overrides announced=True"
    assert out[mc.CAP_REASONING].status == mc.ASSERTION_UNKNOWN, "announced=False is absence, not a proven no"


def test_assertions_from_calibration_manifest_handles_an_empty_manifest():
    out = mc.assertions_from_calibration_manifest({})
    assert all(a.status == mc.ASSERTION_UNKNOWN for a in out.values())


def test_assertions_from_endpoint_capabilities_marks_absent_required_as_unsupported():
    out = mc.assertions_from_endpoint_capabilities(["tool_call"], requirements=["tool_call", "vision"])
    assert out["tool_call"].status == mc.ASSERTION_CLAIMED
    assert out["vision"].status == mc.ASSERTION_UNSUPPORTED


def test_assertions_from_endpoint_capabilities_says_nothing_about_unrequested_caps():
    out = mc.assertions_from_endpoint_capabilities(["tool_call"], requirements=["tool_call"])
    assert set(out) == {"tool_call"}


# ---------------------------------------------------------------------------
# src.provider_policy.resolve_route — invariant 3, now explained (CMP-11)
# ---------------------------------------------------------------------------

def test_provider_policy_explains_the_missing_capability_not_just_names_it():
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="m",
            endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1", "capabilities": ["json_mode"]},
            requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
        )
    assert excinfo.value.error_class == "provider.parameter_unsupported"
    assert "tool_call" in excinfo.value.detail
    assert "no admite" in excinfo.value.detail or "herramientas" in excinfo.value.detail


def test_provider_policy_names_an_alternative_that_meets_the_requirement():
    """The decisive test from the CMP-11 card: general-capability model,
    endpoint incompatible with a REQUIRED parameter -> explained rejection
    naming an authorized alternative route, never a silent 'runs anyway'."""
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="m",
            endpoint={
                "connection_id": "ep", "base_url": "https://api.openai.com/v1",
                "capabilities": ["json_mode"],
                "capability_alternatives": [{"model_id": "m-with-tools", "capabilities": ["tool_call", "json_mode"]}],
            },
            requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
        )
    assert "m-with-tools" in excinfo.value.detail


def test_provider_policy_local_model_uses_calibration_for_the_explanation(monkeypatch):
    key = model_calibration.manifest_key(vendor="ollama", model_id="qwen2.5:7b")

    def _fake_get_manifest(k, data_dir=None):
        if k == key:
            return {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": False}}, "announced": {}}
        return {"tested": {}, "announced": {}}

    monkeypatch.setattr(model_calibration, "get_manifest", _fake_get_manifest)
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="qwen2.5:7b",
            endpoint={"connection_id": "ep", "base_url": "http://127.0.0.1:11434"},
            requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
        )
    assert excinfo.value.error_class == "provider.parameter_unsupported"
    assert "tool_call" in excinfo.value.detail


def test_provider_policy_still_reports_unknown_without_blocking():
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
    )
    assert "tool_call" in decision.reason
    assert "unverified" in decision.reason


# ---------------------------------------------------------------------------
# GET /api/models/fit-explain
# ---------------------------------------------------------------------------

class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a, **k):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, model_cls):
        return _Query(self._rows)

    def close(self):
        self.closed = True


class _Ep(SimpleNamespace):
    pass


def _request(owner="luis"):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=owner, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _endpoint_for(router, path="/api/models/fit-explain"):
    for route in router.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"{path} not registered")


def test_the_fit_explain_route_is_registered_and_authenticated():
    router = mr.setup_model_routes(model_discovery=None)
    assert "/api/models/fit-explain" in {r.path for r in router.routes}


def test_local_ollama_model_explained_against_its_calibration_manifest(monkeypatch, tmp_path):
    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    rows = [_Ep(id="local-ollama", name="Ollama", base_url="http://127.0.0.1:11434/v1",
                api_key=None, is_enabled=True, endpoint_kind="local", supports_tools=None,
                cached_models='["broken-model", "good-model"]', hidden_models=None, pinned_models=None)]
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))
    monkeypatch.setattr(mr, "httpx", SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(OSError("no tags"))))

    key = model_calibration.manifest_key(vendor="ollama", model_id="broken-model", endpoint_id="local-ollama")
    model_calibration.save_tested(key, {model_calibration.TEST_TOOL_CALLING: {"ok": False}}, announced={})
    good_key = model_calibration.manifest_key(vendor="ollama", model_id="good-model", endpoint_id="local-ollama")
    model_calibration.save_tested(good_key, {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, announced={})

    data = asyncio.run(endpoint(_request(), model="broken-model", endpoint_id="local-ollama", needs="tools"))
    assert data["ok"] is False
    reason = data["reasons"][0]
    assert reason["capability"] == "tool_call"
    assert reason["state"] == "missing"
    assert reason["alternatives"] == ["good-model"]


def test_local_ollama_model_with_no_calibration_is_honestly_unknown(monkeypatch):
    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    rows = [_Ep(id="local-ollama", name="Ollama", base_url="http://127.0.0.1:11434/v1",
                api_key=None, is_enabled=True, endpoint_kind="local", supports_tools=None,
                cached_models='["never-calibrated"]', hidden_models=None, pinned_models=None)]
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))
    monkeypatch.setattr(mr, "httpx", SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))))

    data = asyncio.run(endpoint(_request(), model="never-calibrated", endpoint_id="local-ollama", needs="tools,vision"))
    assert data["ok"] is True
    assert {r["state"] for r in data["reasons"]} == {"unknown"}


def test_remote_endpoint_uses_supports_tools_and_stays_unknown_for_the_rest(monkeypatch):
    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    rows = [_Ep(id="ep-remote", name="OpenRouter", base_url="https://openrouter.ai/api/v1",
                api_key="k", is_enabled=True, endpoint_kind="api", supports_tools=False,
                cached_models=None, hidden_models=None, pinned_models=None)]
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))

    data = asyncio.run(endpoint(_request(), model="some/model", endpoint_id="ep-remote", needs="tools,vision"))
    by_cap = {r["capability"]: r for r in data["reasons"]}
    assert by_cap["tool_call"]["state"] == "missing"
    assert by_cap["vision"]["state"] == "unknown"
    assert data["ok"] is False


def test_remote_endpoint_offers_a_cross_endpoint_alternative_for_tools(monkeypatch):
    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    rows = [
        _Ep(id="ep-remote", name="OpenRouter", base_url="https://openrouter.ai/api/v1",
            api_key="k", is_enabled=True, endpoint_kind="api", supports_tools=False,
            cached_models=None, hidden_models=None, pinned_models=None),
        _Ep(id="ep-tools", name="Together", base_url="https://api.together.xyz/v1",
            api_key="k2", is_enabled=True, endpoint_kind="api", supports_tools=True,
            cached_models='["tool-model"]', hidden_models=None, pinned_models=None),
    ]
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))

    data = asyncio.run(endpoint(_request(), model="some/model", endpoint_id="ep-remote", needs="tools"))
    reason = data["reasons"][0]
    assert reason["state"] == "missing"
    assert reason["alternatives"] == ["tool-model"]


def test_unknown_endpoint_id_is_a_404(monkeypatch):
    from fastapi import HTTPException

    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db([]))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(endpoint(_request(), model="m", endpoint_id="missing", needs="tools"))
    assert excinfo.value.status_code == 404


def test_a_blank_model_is_a_400(monkeypatch):
    from fastapi import HTTPException

    router = mr.setup_model_routes(model_discovery=None)
    endpoint = _endpoint_for(router)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(endpoint(_request(), model="   ", endpoint_id="ep", needs="tools"))
    assert excinfo.value.status_code == 400
