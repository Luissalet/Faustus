"""Offline router lacks a deployment identity and uses declarations only."""
import pytest

from src import model_calibration as c, model_router as r


@pytest.fixture(autouse=True)
def temporary_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(r, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(r, "local_speed", lambda model: None)


def legacy(ok, declared):
    key = c.manifest_key(vendor="ollama", model_id="fixture")
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": ok}},
        announced={"capabilities": {"tools": declared}})
    return key


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("declared", [True, False])
def test_legacy_probe_has_no_proven_bonus_or_unsupported_authority(ok, declared):
    key = legacy(ok, declared)
    [scored] = r.score_candidates(r.Requirements(capabilities=("tool_call",)), ["fixture"])
    assert scored.meets_requirements is declared
    assert scored.score == (1.0 if declared else -1000.0)
    assert ("tool_call declarado (no probado)" if declared else "tool_call desconocido") in scored.why
    assert "tool_call probado" not in scored.why
    assert "tool_call probado y falla" not in scored.why
    assert c.get_manifest(key)["tested"][c.TEST_TOOL_CALLING]["ok"] is ok


def test_scoped_probe_is_not_selected_without_endpoint_context():
    c.save_scoped_tested(vendor="ollama", model_id="fixture", endpoint_id="a",
        protocol=c.NATIVE_OLLAMA_PROTOCOL,
        tested={c.TEST_TOOL_CALLING: {"ok": True}}, announced={"capabilities": {"tools": True}})
    [scored] = r.score_candidates(r.Requirements(capabilities=("tool_call",)), ["fixture"])
    assert scored.meets_requirements is False
    assert "tool_call desconocido" in scored.why


def test_speed_history_and_latency_keep_their_existing_weights(monkeypatch):
    legacy(True, True)
    monkeypatch.setattr(r, "local_speed", lambda model: 40.0)
    for _ in range(2):
        r.record_outcome("fixture", ok=False)
    for _ in range(8):
        r.record_outcome("fixture", ok=True, latency_s=20)
    [scored] = r.score_candidates(r.Requirements(capabilities=("tool_call",)), ["fixture"])
    assert scored.score == pytest.approx(4.6)  # declared 1 + speed 2 + history 1.6
    assert scored.meets_requirements is True
    [slow] = r.score_candidates(r.Requirements(capabilities=("tool_call",)), ["fixture"],
        config=r.RouterConfig(max_latency_s=5))
    assert slow.score == pytest.approx(-995.4) and slow.meets_requirements is False


@pytest.mark.parametrize("allow,local_only,escalated", [(False, False, False),
    (True, False, True), (True, True, False)])
def test_unknown_keeps_existing_escalation_and_privacy_gate(allow, local_only, escalated):
    legacy(True, False)
    decision = r.choose(r.Requirements(capabilities=("tool_call",)), installed=["fixture"],
        config=r.RouterConfig(allow_paid_escalation=allow),
        project={"privacy_profile": "local_only" if local_only else "balanced"}, log=False)
    assert decision.model is None
    assert decision.escalated is escalated
    if escalated:
        assert decision.escalation is not None


def test_declared_candidate_keeps_local_preference_despite_legacy_failed_probe():
    legacy(False, True)
    decision = r.choose(r.Requirements(capabilities=("tool_call",)), installed=["fixture"],
        config=r.RouterConfig(allow_paid_escalation=True), log=False)
    assert decision.model == "fixture" and decision.escalated is False


def test_minimum_capabilities_union_and_deterministic_tiebreak_remain():
    for model in ("b", "a"):
        c.save_announced(c.manifest_key(vendor="ollama", model_id=model),
            {"capabilities": {"tools": True, "vision": True}})
    scored = r.score_candidates(r.Requirements(capabilities=("tool_call",)), ["b", "a"],
        config=r.RouterConfig(min_capabilities=("vision",)))
    assert [item.model for item in scored] == ["a", "b"]
    assert all(item.meets_requirements and item.score == 2.0 for item in scored)


def test_legacy_json_probe_and_invented_announcement_do_not_satisfy_json_requirement():
    c.save_tested(c.manifest_key(vendor="ollama", model_id="fixture"),
        {c.TEST_JSON_MODE: {"ok": True}}, announced={"capabilities": {"json_mode": True}})
    [scored] = r.score_candidates(r.Requirements(capabilities=("json_mode",)), ["fixture"])
    assert scored.meets_requirements is False and scored.score == -1000.0
    assert "json_mode desconocido" in scored.why
