"""tests/test_adp22_provider_policy.py — ADP-22: src/provider_policy.py's
four route invariants, `model_router.explain_event` (this lote's small
`src/model_router.py` extension), and the "auto" wiring into
`routes/chat_routes.py` (MOD-05 cabled to the turn — W1-F §2).

Everything here runs against fakes: no network, no real Ollama, no real
DB rows beyond whatever `tests/conftest.py`'s in-memory sqlite already
provides for import-time `core.database` setup. "Sin investigación
externa" per the lote contract.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import constants as constants_mod  # noqa: E402
from src import model_calibration  # noqa: E402
from src import model_router  # noqa: E402
from src import privacy_policy  # noqa: E402
from src import provider_policy  # noqa: E402
from routes import chat_routes  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# resolve_route: billing / network classification
# ---------------------------------------------------------------------------

def test_local_endpoint_is_billing_local():
    decision = provider_policy.resolve_route(
        requested_model="qwen2.5:7b",
        endpoint={"connection_id": "ep1", "base_url": "http://127.0.0.1:11434"},
    )
    assert decision.billing == provider_policy.BILLING_LOCAL
    assert decision.network == provider_policy.NETWORK_LOCAL
    assert decision.escalated is False


def test_chatgpt_subscription_base_is_billing_subscription():
    decision = provider_policy.resolve_route(
        requested_model="gpt-5",
        endpoint={"connection_id": "ep2", "base_url": "https://chatgpt.com/backend-api/codex"},
    )
    assert decision.billing == provider_policy.BILLING_SUBSCRIPTION
    assert decision.network == provider_policy.NETWORK_REMOTE


def test_copilot_base_is_billing_subscription():
    decision = provider_policy.resolve_route(
        requested_model="gpt-4o",
        endpoint={"connection_id": "ep3", "base_url": "https://api.githubcopilot.com"},
    )
    assert decision.billing == provider_policy.BILLING_SUBSCRIPTION


def test_generic_remote_endpoint_with_cost_tracked_is_api_paid():
    decision = provider_policy.resolve_route(
        requested_model="gpt-4o-mini",
        endpoint={"connection_id": "ep4", "base_url": "https://api.openai.com/v1"},
    )
    assert decision.billing == provider_policy.BILLING_API_PAID


def test_session_backed_credential_without_known_subscription_vendor_is_subscription_shaped():
    """A `provider_auth_id` means the endpoint's credential is resolved at
    call time via a session (`src.endpoint_resolver.resolve_endpoint_runtime`),
    never a bare per-token API key — even for a vendor this module has no
    dedicated subscription detector for."""
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={
            "connection_id": "ep5",
            "base_url": "https://example-enterprise.internal/v1",
            "provider_auth_id": "auth-123",
        },
    )
    assert decision.billing == provider_policy.BILLING_SUBSCRIPTION


# ---------------------------------------------------------------------------
# Invariant 1: local never escalates to remote without authorization
# ---------------------------------------------------------------------------

def test_local_only_blocks_remote_route(monkeypatch):
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_LOCAL_ONLY)
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="m",
            endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1"},
        )
    assert excinfo.value.error_class == "provider.local_escalation_blocked"


def test_local_only_allows_remote_route_with_explicit_confirmation(monkeypatch):
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_LOCAL_ONLY)
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(confirmed_remote=True),
    )
    assert decision.escalated is True
    assert decision.billing == provider_policy.BILLING_API_PAID


def test_local_only_never_blocks_a_local_route(monkeypatch):
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_LOCAL_ONLY)
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "http://127.0.0.1:11434"},
    )
    assert decision.billing == provider_policy.BILLING_LOCAL
    assert decision.escalated is False


# ---------------------------------------------------------------------------
# Invariant 2: subscription never falls to a paid API by auth failure alone
# ---------------------------------------------------------------------------

def test_subscription_auth_failure_blocks_silent_fallback_to_paid_api():
    prior = {
        "connection_id": "sub-1", "model": "gpt-5", "base_url": "https://chatgpt.com/backend-api/codex",
        "billing": provider_policy.BILLING_SUBSCRIPTION, "error_class": "401 unauthorized",
    }
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="gpt-4o-mini",
            endpoint={"connection_id": "ep-paid", "base_url": "https://api.openai.com/v1"},
            requirements=provider_policy.RouteRequirements(prior=prior),
        )
    assert excinfo.value.error_class == "provider.subscription_to_paid_blocked"


def test_subscription_auth_failure_allows_fallback_with_confirmation():
    prior = {
        "connection_id": "sub-1", "model": "gpt-5", "base_url": "https://chatgpt.com/backend-api/codex",
        "billing": provider_policy.BILLING_SUBSCRIPTION, "error_class": "401 unauthorized",
    }
    decision = provider_policy.resolve_route(
        requested_model="gpt-4o-mini",
        endpoint={"connection_id": "ep-paid", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(prior=prior, confirmed_remote=True),
    )
    assert decision.escalated is True


def test_subscription_non_auth_failure_does_not_block_fallback():
    """A non-auth failure (a 500, a timeout) on the subscription connection
    is not what this invariant guards — only an auth-shaped one is."""
    prior = {
        "connection_id": "sub-1", "model": "gpt-5", "base_url": "https://chatgpt.com/backend-api/codex",
        "billing": provider_policy.BILLING_SUBSCRIPTION, "error_class": "server_error_500",
    }
    decision = provider_policy.resolve_route(
        requested_model="gpt-4o-mini",
        endpoint={"connection_id": "ep-paid", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(prior=prior),
    )
    assert decision.escalated is False
    assert decision.billing == provider_policy.BILLING_API_PAID


# ---------------------------------------------------------------------------
# Invariant 3: a required parameter confirmed unsupported is never silent
# ---------------------------------------------------------------------------

def test_required_parameter_confirmed_unsupported_raises():
    with pytest.raises(provider_policy.ProviderPolicyError) as excinfo:
        provider_policy.resolve_route(
            requested_model="m",
            endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1", "capabilities": ["json_mode"]},
            requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
        )
    assert excinfo.value.error_class == "provider.parameter_unsupported"
    assert "tool_call" in excinfo.value.detail


def test_required_parameter_supported_does_not_raise():
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1", "capabilities": ["tool_call"]},
        requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
    )
    assert decision.model == "m"


def test_required_parameter_unknown_for_remote_endpoint_is_reported_not_raised():
    """No explicit capability evidence for a remote endpoint: never silently
    assumed supported, and never a hard failure either — just reported."""
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(required_parameters=("tool_call",)),
    )
    assert "tool_call" in decision.reason
    assert "unverified" in decision.reason


def test_required_parameter_checked_against_local_model_calibration(monkeypatch):
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


# ---------------------------------------------------------------------------
# Invariant 4: max_price is carried/documented as per-token, never a run cap
# ---------------------------------------------------------------------------

def test_max_price_per_token_is_carried_verbatim_and_documented_as_per_token():
    decision = provider_policy.resolve_route(
        requested_model="m",
        endpoint={"connection_id": "ep", "base_url": "https://openrouter.ai/api/v1"},
        requirements=provider_policy.RouteRequirements(
            max_price_per_token={"prompt": 0.000002, "completion": 0.000006},
        ),
    )
    assert decision.data_restrictions["max_price_per_token"] == {"prompt": 0.000002, "completion": 0.000006}
    lowered = decision.reason.lower()
    assert "per-token" in lowered or "per token" in lowered
    for banned in ("total cost cap", "budget cap", "run cap"):
        assert banned not in lowered


def test_max_price_per_token_absent_by_default():
    decision = provider_policy.resolve_route(
        requested_model="m", endpoint={"connection_id": "ep", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert "max_price_per_token" not in decision.data_restrictions


# ---------------------------------------------------------------------------
# fallback_scope
# ---------------------------------------------------------------------------

def test_fallback_scope_none_without_prior():
    decision = provider_policy.resolve_route(
        requested_model="m", endpoint={"connection_id": "ep", "base_url": "http://127.0.0.1:11434"},
    )
    assert decision.fallback_scope == provider_policy.FALLBACK_NONE


def test_fallback_scope_model_same_connection_different_model():
    prior = {"connection_id": "ep", "model": "m1", "base_url": "http://127.0.0.1:11434"}
    decision = provider_policy.resolve_route(
        requested_model="m2",
        endpoint={"connection_id": "ep", "base_url": "http://127.0.0.1:11434"},
        requirements=provider_policy.RouteRequirements(prior=prior),
    )
    assert decision.fallback_scope == provider_policy.FALLBACK_MODEL


def test_fallback_scope_connection_same_vendor_different_connection():
    prior = {"connection_id": "ep-a", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
    decision = provider_policy.resolve_route(
        requested_model="gpt-4o-mini",
        endpoint={"connection_id": "ep-b", "base_url": "https://api.openai.com/v1"},
        requirements=provider_policy.RouteRequirements(prior=prior),
    )
    assert decision.fallback_scope == provider_policy.FALLBACK_CONNECTION


def test_fallback_scope_provider_different_vendor():
    prior = {"connection_id": "ep-a", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
    decision = provider_policy.resolve_route(
        requested_model="claude-x",
        endpoint={"connection_id": "ep-c", "base_url": "https://api.anthropic.com"},
        requirements=provider_policy.RouteRequirements(prior=prior),
    )
    assert decision.fallback_scope == provider_policy.FALLBACK_PROVIDER


# ---------------------------------------------------------------------------
# model_router.explain_event (this lote's small model_router.py addition)
# ---------------------------------------------------------------------------

def test_explain_event_bundles_decision_and_route(monkeypatch):
    monkeypatch.setattr(model_calibration, "get_manifest", lambda k, data_dir=None: {"tested": {}, "announced": {}})
    monkeypatch.setattr(model_router, "local_speed", lambda m: None)
    decision = model_router.choose(
        model_router.Requirements(), installed=["m"], config=model_router.RouterConfig(), log=False,
    )
    route = provider_policy.resolve_route(
        requested_model="m", endpoint={"connection_id": "ep", "base_url": "http://127.0.0.1:11434"},
    )
    event = model_router.explain_event(decision, route=route)
    assert event["decision"]["model"] == "m"
    assert event["route"]["billing"] == provider_policy.BILLING_LOCAL
    assert "explain" in event


def test_explain_event_without_route_omits_it():
    decision = model_router.choose(
        model_router.Requirements(), installed=[], config=model_router.RouterConfig(), log=False,
    )
    event = model_router.explain_event(decision)
    assert "route" not in event


# ---------------------------------------------------------------------------
# Wiring into routes/chat_routes.py: `_resolve_auto_model_route` /
# `_record_model_router_outcome` — the no-regression contract every lote
# must prove for its `enabled=False` integration point.
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, model, endpoint_url):
        self.model = model
        self.endpoint_url = endpoint_url


def test_wiring_no_op_when_router_disabled(monkeypatch):
    """`enabled=False` is the stored default (no config written in this
    isolated DATA_DIR). Nothing about `sess.model` changes — the exact
    no-regression proof the contract requires for this chat_routes.py edit."""
    sess = _FakeSession(model="auto", endpoint_url="http://127.0.0.1:11434")
    monkeypatch.setattr("src.vram_admission.ollama_root", lambda url: "http://127.0.0.1:11434")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("model_router.choose() must not run when enabled=False")
    monkeypatch.setattr(model_router, "choose", _must_not_be_called)

    result = chat_routes._resolve_auto_model_route(sess, "sess-x", owner="u1")
    assert result is None
    assert sess.model == "auto"


def test_wiring_no_op_when_model_is_not_auto_or_empty(monkeypatch):
    sess = _FakeSession(model="llama3.1:8b", endpoint_url="http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "get_router_config", lambda *a, **kw: model_router.RouterConfig(enabled=True))

    def _must_not_be_called(*a, **kw):
        raise AssertionError("model_router.choose() must not run for an already-concrete requested model")
    monkeypatch.setattr(model_router, "choose", _must_not_be_called)

    result = chat_routes._resolve_auto_model_route(sess, "sess-x", owner="u1")
    assert result is None
    assert sess.model == "llama3.1:8b"


def test_wiring_no_op_when_endpoint_is_not_local_ollama(monkeypatch):
    sess = _FakeSession(model="auto", endpoint_url="https://api.openai.com/v1")
    monkeypatch.setattr(model_router, "get_router_config", lambda *a, **kw: model_router.RouterConfig(enabled=True))
    monkeypatch.setattr("src.vram_admission.ollama_root", lambda url: None)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("model_router.choose() must not run for a non-local endpoint")
    monkeypatch.setattr(model_router, "choose", _must_not_be_called)

    result = chat_routes._resolve_auto_model_route(sess, "sess-x", owner="u1")
    assert result is None
    assert sess.model == "auto"


def test_wiring_no_op_when_no_local_models_installed(monkeypatch):
    sess = _FakeSession(model="", endpoint_url="http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "get_router_config", lambda *a, **kw: model_router.RouterConfig(enabled=True))
    monkeypatch.setattr("src.vram_admission.ollama_root", lambda url: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "installed_local_models", lambda **kw: [])

    result = chat_routes._resolve_auto_model_route(sess, "sess-x", owner="u1")
    assert result is None
    assert sess.model == ""


def test_wiring_picks_local_model_and_updates_session(monkeypatch):
    sess = _FakeSession(model="auto", endpoint_url="http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "get_router_config", lambda *a, **kw: model_router.RouterConfig(enabled=True))
    monkeypatch.setattr("src.vram_admission.ollama_root", lambda url: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "installed_local_models", lambda **kw: ["qwen2.5:7b"])
    fake_decision = model_router.Decision(
        model="qwen2.5:7b", reason="mejor candidato local", alternatives=(), escalated=False, escalation=None,
    )
    monkeypatch.setattr(model_router, "choose", lambda *a, **kw: fake_decision)

    result = chat_routes._resolve_auto_model_route(sess, "sess-does-not-exist-in-db", owner="u1")
    assert result is not None
    assert result.model_router_decision.model == "qwen2.5:7b"
    assert sess.model == "qwen2.5:7b"
    assert result.route_decision is not None
    assert result.route_decision.billing == provider_policy.BILLING_LOCAL


def test_wiring_clears_literal_auto_when_nothing_qualifies(monkeypatch):
    """The literal "auto" sentinel must never reach an upstream chat
    completion call — when the router engaged but nothing qualified,
    `sess.model` is cleared so the existing, well-understood empty-model 400
    fires downstream instead of a confusing provider-side error."""
    sess = _FakeSession(model="auto", endpoint_url="http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "get_router_config", lambda *a, **kw: model_router.RouterConfig(enabled=True))
    monkeypatch.setattr("src.vram_admission.ollama_root", lambda url: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_router, "installed_local_models", lambda **kw: ["m1"])
    fake_decision = model_router.Decision(
        model=None, reason="ningún modelo local cumple los requisitos",
        alternatives=(), escalated=False, escalation=None,
    )
    monkeypatch.setattr(model_router, "choose", lambda *a, **kw: fake_decision)

    result = chat_routes._resolve_auto_model_route(sess, "sess-x", owner="u1")
    assert result is not None
    assert result.model_router_decision.model is None
    assert sess.model == ""


# ---------------------------------------------------------------------------
# _record_model_router_outcome: only writes to MOD-05's history when this
# turn's model actually came from the router.
# ---------------------------------------------------------------------------

def test_record_outcome_noop_without_auto_result(monkeypatch):
    calls = []
    monkeypatch.setattr(model_router, "record_outcome", lambda *a, **kw: calls.append((a, kw)))
    chat_routes._record_model_router_outcome(None, ok=True)
    assert calls == []


def test_record_outcome_noop_when_decision_model_is_none(monkeypatch):
    calls = []
    monkeypatch.setattr(model_router, "record_outcome", lambda *a, **kw: calls.append((a, kw)))
    decision = model_router.Decision(model=None, reason="x", alternatives=(), escalated=False, escalation=None)
    auto = chat_routes._AutoRouteResult(model_router_decision=decision, route_decision=None)
    chat_routes._record_model_router_outcome(auto, ok=True)
    assert calls == []


def test_record_outcome_writes_when_router_engaged():
    decision = model_router.Decision(model="qwen2.5:7b", reason="x", alternatives=(), escalated=False, escalation=None)
    auto = chat_routes._AutoRouteResult(model_router_decision=decision, route_decision=None)
    chat_routes._record_model_router_outcome(auto, ok=True, latency_s=2.5)
    stats = model_router.read_stats()
    assert stats["qwen2.5:7b"]["ok"] == 1
    assert stats["qwen2.5:7b"]["ewma_latency_s"] == pytest.approx(2.5)


def test_record_outcome_records_failure_with_error_class():
    decision = model_router.Decision(model="qwen2.5:7b", reason="x", alternatives=(), escalated=False, escalation=None)
    auto = chat_routes._AutoRouteResult(model_router_decision=decision, route_decision=None)
    chat_routes._record_model_router_outcome(auto, ok=False, error_class="http_500")
    stats = model_router.read_stats()
    assert stats["qwen2.5:7b"]["fail"] == 1
    assert stats["qwen2.5:7b"]["last_error_class"] == "http_500"
