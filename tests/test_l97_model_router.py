"""Lote A3 (OBJ-8, MOD-05) -- src/model_router.py + routes/model_router_routes.py.

Scoring is exercised with fake manifests (`model_calibration.get_manifest`
monkeypatched) and fake speeds (`model_router.local_speed` monkeypatched --
that name is bound into this module's namespace at import time, so patching
`src.llm_core.local_speed` directly would not be observed here) so every
assertion is deterministic: no Ollama, no network, no real calibration run.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import model_router_routes  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src import model_calibration  # noqa: E402
from src import model_router  # noqa: E402
from src import privacy_policy  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path))
    yield


def _manifest_key(model: str) -> str:
    return model_calibration.manifest_key(vendor="ollama", model_id=model)


def _fake_manifests(monkeypatch, table):
    """`table`: {model: manifest_dict}. Unknown models get the honest empty
    manifest, same as the real `get_manifest` on a never-calibrated model."""
    by_key = {_manifest_key(m): v for m, v in table.items()}

    def _get_manifest(key, *, data_dir=None):
        return by_key.get(key, {"announced": {}, "tested": {}, "degraded": [], "updated_at": ""})

    monkeypatch.setattr(model_calibration, "get_manifest", _get_manifest)


def _fake_speeds(monkeypatch, table):
    monkeypatch.setattr(model_router, "local_speed", lambda m: table.get(m))


# ---------------------------------------------------------------------------
# RouterConfig: defaults, validation, persistence
# ---------------------------------------------------------------------------
def test_default_config_is_inert():
    cfg = model_router.get_router_config()
    assert cfg.enabled is False
    assert cfg.allow_paid_escalation is False
    assert cfg.candidates == ()
    assert cfg.min_capabilities == ()


def test_update_router_config_persists_and_merges():
    model_router.update_router_config({"enabled": True, "min_capabilities": ["tool_call"]})
    cfg = model_router.get_router_config()
    assert cfg.enabled is True
    assert cfg.min_capabilities == ("tool_call",)
    # A second, unrelated patch must not clobber the first.
    model_router.update_router_config({"allow_paid_escalation": True})
    cfg2 = model_router.get_router_config()
    assert cfg2.enabled is True
    assert cfg2.allow_paid_escalation is True


@pytest.mark.parametrize(
    "patch,message_fragment",
    [
        ({"enabled": "yes"}, "bool"),
        ({"nope": True}, "unknown"),
        ({"max_latency_s": -1}, "max_latency_s"),
        ({"max_latency_s": "slow"}, "max_latency_s"),
        ({"candidates": ["", "ok"]}, "candidates"),
        ({"min_capabilities": ["telepathy"]}, "min_capabilities"),
    ],
)
def test_update_router_config_rejects_invalid_patch(patch, message_fragment):
    with pytest.raises(ValueError, match=message_fragment):
        model_router.update_router_config(patch)
    # Rejected patch must not have partially written anything.
    assert model_router.get_router_config().to_dict() == model_router.RouterConfig().to_dict()


def test_corrupt_config_file_degrades_to_defaults(tmp_path):
    path = os.path.join(str(tmp_path), "model_router_config.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    cfg = model_router.get_router_config()
    assert cfg == model_router.RouterConfig()
    assert os.path.exists(path + ".corrupt")


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------
def test_requirements_from_dict_filters_unknown_capabilities():
    req = model_router.Requirements.from_dict({"capabilities": ["tool_call", "telepathy", 5], "max_latency_s": 4})
    assert req.capabilities == ("tool_call",)
    assert req.max_latency_s == 4


# ---------------------------------------------------------------------------
# score_candidates: deterministic scoring
# ---------------------------------------------------------------------------
def test_score_candidates_deterministic(monkeypatch):
    _fake_manifests(monkeypatch, {
        "model-a": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
        "model-b": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": False}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"model-a": 40.0})
    # 2 fails then 8 oks so the running record ends with last_error_class
    # reset to None (matches the arithmetic asserted below).
    model_router.record_outcome("model-a", ok=False, error_class="timeout")
    model_router.record_outcome("model-a", ok=False, error_class="timeout")
    for _ in range(8):
        model_router.record_outcome("model-a", ok=True)

    req = model_router.Requirements(capabilities=("tool_call",))
    scored = model_router.score_candidates(req, ["model-a", "model-b"])
    by_model = {s.model: s for s in scored}

    a = by_model["model-a"]
    assert a.meets_requirements is True
    # 3.0 (tool_call proven) + 2.0 (40 tok/s -> min(40/20, 3.0)) + 1.6 (8 ok / 2 fail -> 0.8 * 2.0)
    assert a.score == pytest.approx(6.6)
    assert "tool_call probado" in a.why
    assert "40 tok/s medidos" in a.why
    assert "8 ok / 2 fallos recientes" in a.why
    assert not any("último error" in w for w in a.why)

    b = by_model["model-b"]
    assert b.meets_requirements is False
    assert b.score == pytest.approx(-1000.0)
    assert "tool_call probado y falla" in b.why
    assert "velocidad desconocida" in b.why
    assert "sin historial" in b.why

    # best-first, deterministic tiebreak by name
    assert [s.model for s in scored] == ["model-a", "model-b"]


def test_score_candidates_declared_capability_is_lower_weight_fallback(monkeypatch):
    _fake_manifests(monkeypatch, {
        "declared-only": {
            "tested": {},
            "announced": {"capabilities": {"tools": True}},
        },
    })
    _fake_speeds(monkeypatch, {})
    req = model_router.Requirements(capabilities=("tool_call",))
    [scored] = model_router.score_candidates(req, ["declared-only"])
    assert scored.meets_requirements is True
    assert "tool_call declarado (no probado)" in scored.why
    # 1.0 (declared, lower than the 3.0 a proven capability would earn)
    assert scored.score == pytest.approx(1.0)


def test_score_candidates_latency_gate(monkeypatch):
    _fake_manifests(monkeypatch, {"slow": {"tested": {}, "announced": {}}})
    _fake_speeds(monkeypatch, {})
    model_router.record_outcome("slow", ok=True, latency_s=20.0)
    req = model_router.Requirements()
    config = model_router.RouterConfig(max_latency_s=5.0)
    [scored] = model_router.score_candidates(req, ["slow"], config=config)
    assert scored.meets_requirements is False
    assert any("excede el máximo" in w for w in scored.why)


def test_min_capabilities_and_requirements_union(monkeypatch):
    _fake_manifests(monkeypatch, {
        "m": {"tested": {model_calibration.TEST_JSON_MODE: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {})
    config = model_router.RouterConfig(min_capabilities=("json_mode",))
    req = model_router.Requirements(capabilities=("tool_call",))
    [scored] = model_router.score_candidates(req, ["m"], config=config)
    # tool_call (from requirements) is neither tested nor declared -> missing
    assert scored.meets_requirements is False
    assert "json_mode probado" in scored.why
    assert "tool_call desconocido" in scored.why


# ---------------------------------------------------------------------------
# choose(): local preference, escalation, privacy
# ---------------------------------------------------------------------------
def test_choose_prefers_local_and_never_escalates_when_one_qualifies(monkeypatch):
    _fake_manifests(monkeypatch, {
        "good": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"good": 30.0})
    config = model_router.RouterConfig(allow_paid_escalation=True)  # even armed, it must not fire
    req = model_router.Requirements(capabilities=("tool_call",))
    decision = model_router.choose(req, installed=["good"], config=config, log=False)
    assert decision.model == "good"
    assert decision.escalated is False
    assert decision.escalation is None


def test_choose_no_qualifying_local_does_not_escalate_by_default(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    config = model_router.RouterConfig()  # allow_paid_escalation defaults False
    req = model_router.Requirements(capabilities=("vision",))
    decision = model_router.choose(req, installed=["unqualified"], config=config, log=False)
    assert decision.model is None
    assert decision.escalated is False
    assert decision.escalation is None
    assert "deshabilitado" in decision.reason


def test_choose_escalates_only_when_allowed_and_no_local_qualifies(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_CLOUD_ALLOWED)
    config = model_router.RouterConfig(allow_paid_escalation=True)
    req = model_router.Requirements(capabilities=("vision",))
    decision = model_router.choose(req, installed=["unqualified"], config=config, log=False)
    assert decision.model is None
    assert decision.escalated is True
    assert decision.escalation is not None
    assert decision.escalation["privacy_profile"] == privacy_policy.PROFILE_CLOUD_ALLOWED


def test_choose_never_escalates_under_local_only_privacy(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    monkeypatch.setattr(privacy_policy, "get_privacy_profile", lambda **kw: privacy_policy.PROFILE_LOCAL_ONLY)
    config = model_router.RouterConfig(allow_paid_escalation=True)
    req = model_router.Requirements(capabilities=("vision",))
    decision = model_router.choose(req, installed=["unqualified"], config=config, log=False)
    assert decision.model is None
    assert decision.escalated is False
    assert decision.escalation is None
    assert "local-only" in decision.reason


def test_choose_never_proposes_a_model_that_is_not_installed(monkeypatch):
    _fake_manifests(monkeypatch, {
        "configured-but-missing": {"tested": {}, "announced": {"capabilities": {"tools": True}}},
    })
    _fake_speeds(monkeypatch, {})
    config = model_router.RouterConfig(candidates=("configured-but-missing",))
    decision = model_router.choose(model_router.Requirements(), installed=["something-else"], config=config, log=False)
    assert decision.model != "configured-but-missing"
    assert decision.model is None
    assert all(s.model != "configured-but-missing" for s in decision.alternatives)


def test_config_enabled_flag_does_not_change_choose_behavior(monkeypatch):
    """`enabled` is the flag a wired caller checks BEFORE calling choose() at
    all (this lote leaves that caller unwired -- see docs/api/model_router.md).
    choose() itself never reads it, so behavior with enabled True/False is
    identical for the same otherwise-equal config -- this is the module's own
    no-regression guarantee in the absence of a wired integration point."""
    _fake_manifests(monkeypatch, {
        "good": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"good": 10.0})
    req = model_router.Requirements(capabilities=("tool_call",))
    off = model_router.choose(req, installed=["good"], config=model_router.RouterConfig(enabled=False), log=False)
    on = model_router.choose(req, installed=["good"], config=model_router.RouterConfig(enabled=True), log=False)
    assert off.model == on.model == "good"
    assert off.escalated == on.escalated == False


# ---------------------------------------------------------------------------
# explain()
# ---------------------------------------------------------------------------
def test_explain_variants(monkeypatch):
    _fake_manifests(monkeypatch, {
        "good": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"good": 41.0})
    req = model_router.Requirements(capabilities=("tool_call",))
    chosen = model_router.choose(req, installed=["good"], config=model_router.RouterConfig(), log=False)
    assert "Modelo elegido: good" in model_router.explain(chosen)

    none_decision = model_router.choose(
        model_router.Requirements(capabilities=("vision",)),
        installed=["good"],
        config=model_router.RouterConfig(),
        log=False,
    )
    assert model_router.explain(none_decision).startswith("Sin modelo disponible")


# ---------------------------------------------------------------------------
# record_outcome / read_stats
# ---------------------------------------------------------------------------
def test_record_outcome_ewma_and_counts():
    model_router.record_outcome("m", ok=True, latency_s=10.0)
    entry = model_router.record_outcome("m", ok=True, latency_s=20.0)
    # ewma: 10.0 (first sample, no averaging) -> 0.7*10 + 0.3*20 = 13.0
    assert entry["ewma_latency_s"] == pytest.approx(13.0)
    assert entry["ok"] == 2
    assert entry["fail"] == 0

    failed = model_router.record_outcome("m", ok=False, error_class="timeout")
    assert failed["fail"] == 1
    assert failed["last_error_class"] == "timeout"

    recovered = model_router.record_outcome("m", ok=True)
    assert recovered["last_error_class"] is None

    stats = model_router.read_stats()
    assert stats["m"]["ok"] == 3
    assert stats["m"]["fail"] == 1


def test_record_outcome_requires_model():
    with pytest.raises(ValueError):
        model_router.record_outcome("", ok=True)


# ---------------------------------------------------------------------------
# decision log: append + rotation
# ---------------------------------------------------------------------------
def test_choose_appends_to_log(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    model_router.choose(
        model_router.Requirements(), installed=[], config=model_router.RouterConfig(),
        requested="auto", session_id="sess-1",
    )
    entries = model_router.read_log(limit=10)
    assert len(entries) == 1
    assert entries[0]["session_id"] == "sess-1"
    assert entries[0]["requested"] == "auto"
    assert entries[0]["chosen"] is None
    assert "candidates" in entries[0]


def test_preview_style_choose_can_skip_logging(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    model_router.choose(model_router.Requirements(), installed=[], config=model_router.RouterConfig(), log=False)
    assert model_router.read_log(limit=10) == []


def test_log_rotation_keeps_most_recent(monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    monkeypatch.setattr(model_router, "_LOG_MAX_LINES", 3)
    for i in range(5):
        model_router.choose(
            model_router.Requirements(), installed=[], config=model_router.RouterConfig(),
            requested=f"turn-{i}",
        )
    entries = model_router.read_log(limit=10)
    assert len(entries) == 3
    # newest first
    assert [e["requested"] for e in entries] == ["turn-4", "turn-3", "turn-2"]


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(model_router_routes.setup_model_router_routes())
    return TestClient(app)


def test_get_config_route_default(client):
    resp = client.get("/api/model-router/config")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_put_config_route_round_trip(client):
    resp = client.put("/api/model-router/config", json={"enabled": True, "candidates": ["m1"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert resp.json()["candidates"] == ["m1"]
    assert client.get("/api/model-router/config").json()["candidates"] == ["m1"]


def test_put_config_route_validation_error(client):
    resp = client.put("/api/model-router/config", json={"min_capabilities": ["nope"]})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_class"] == "model_router.invalid_config"
    assert "detail" in body


def test_preview_route_does_not_write_log(client, monkeypatch):
    _fake_manifests(monkeypatch, {
        "m": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"m": 25.0})
    resp = client.post("/api/model-router/preview", json={
        "requirements": {"capabilities": ["tool_call"]},
        "installed": ["m"],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"]["model"] == "m"
    assert "Modelo elegido" in body["explain"]
    assert model_router.read_log(limit=10) == []


def test_preview_route_discovers_installed_models_when_none_given(client, monkeypatch):
    """Seen live: an empty 'installed' box answered 'no candidates installed'
    although Ollama had a dozen models. Empty now means 'ask Ollama'."""
    _fake_manifests(monkeypatch, {
        "m": {"tested": {model_calibration.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {}}},
    })
    _fake_speeds(monkeypatch, {"m": 25.0})
    monkeypatch.setattr(model_router, "installed_local_models", lambda **_kw: ["m"])
    resp = client.post("/api/model-router/preview", json={"requirements": {"capabilities": ["tool_call"]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"]["model"] == "m"
    assert resp.json()["installed"] == ["m"]


def test_installed_local_models_is_empty_when_ollama_is_unreachable(monkeypatch):
    from src import gpu_policy
    monkeypatch.setattr(gpu_policy, "model_sizes", lambda base, timeout=3.0: (_ for _ in ()).throw(RuntimeError("down")))
    assert model_router.installed_local_models() == []


def test_log_and_stats_routes(client, monkeypatch):
    _fake_manifests(monkeypatch, {})
    _fake_speeds(monkeypatch, {})
    model_router.choose(model_router.Requirements(), installed=[], config=model_router.RouterConfig(), session_id="s1")
    model_router.record_outcome("m", ok=True, latency_s=1.0)

    log_resp = client.get("/api/model-router/log?limit=5")
    assert log_resp.status_code == 200
    assert len(log_resp.json()["entries"]) == 1

    stats_resp = client.get("/api/model-router/stats")
    assert stats_resp.status_code == 200
    assert stats_resp.json()["stats"]["m"]["ok"] == 1


def test_admin_gate_blocks_put_without_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.include_router(model_router_routes.setup_model_router_routes())
    guarded_client = TestClient(app)
    resp = guarded_client.put("/api/model-router/config", json={"enabled": True})
    assert resp.status_code == 403
    resp_get = guarded_client.get("/api/model-router/config")
    assert resp_get.status_code == 403
