"""H22 route policy reads exact observations, never legacy probes. No network."""
import pytest

from src import model_calibration as c, provider_policy as p, model_capabilities as mc


@pytest.fixture(autouse=True)
def temporary_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *args, **kwargs: None)


def endpoint(**changes):
    return {"connection_id": "a", "base_url": "http://127.0.0.1:11434/api/chat", **changes}


def write(model="fixture", *, ok=True, ep="a", digest="", announced=None):
    return c.save_scoped_tested(vendor="ollama", model_id=model, endpoint_id=ep,
        protocol=c.NATIVE_OLLAMA_PROTOCOL, digest=digest,
        tested={c.TEST_TOOL_CALLING: {"ok": ok}},
        announced={"capabilities": {"tools": True}} if announced is None else announced)


def fit(ep=None):
    ep = endpoint() if ep is None else ep
    return p._build_fit("fixture", ep, ep.get("connection_id"), p.NETWORK_LOCAL, ("tool_call",))


def resolve(ep):
    return p.resolve_route(requested_model="fixture", endpoint=ep,
        requirements=p.RouteRequirements(required_parameters=("tool_call",)))


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("declared", [True, False])
def test_global_legacy_observations_never_verify_or_reject(ok, declared):
    key = c.manifest_key(vendor="ollama", model_id="fixture")
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": ok}},
        announced={"capabilities": {"tools": declared}})
    assert fit().reasons[0].state == (mc.FIT_ANNOUNCED if declared else mc.FIT_UNKNOWN)
    resolve(endpoint())
    assert c.get_manifest(key)["tested"][c.TEST_TOOL_CALLING]["ok"] is ok


@pytest.mark.parametrize("ok,state", [(True, mc.FIT_TESTED), (False, mc.FIT_MISSING)])
def test_exact_model_scope_controls_requirement(ok, state):
    write(ok=ok)
    assert fit().reasons[0].state == state
    if ok:
        resolve(endpoint())
    else:
        with pytest.raises(p.ProviderPolicyError, match="tool_call"):
            resolve(endpoint())


@pytest.mark.parametrize("ep", [endpoint(connection_id="b"), endpoint(connection_id=None),
    endpoint(base_url="http://127.0.0.1:11434/v1"), endpoint(base_url="http://127.0.0.1:11434")])
def test_context_mismatch_has_no_observation(ep):
    write(ok=False)
    assert fit(ep).reasons[0].state == mc.FIT_UNKNOWN
    resolve(ep)


@pytest.mark.parametrize("digest", [None, "different", {"digest": "blob"}, 42])
def test_digest_observation_is_not_adopted_without_matching_string(digest):
    write(digest="blob", ok=False)
    ep = endpoint(model_digest=digest)
    assert fit(ep).reasons[0].state == mc.FIT_UNKNOWN
    resolve(ep)


def test_explicit_digest_resolves_exact_scope_and_alias():
    write(model="other-tag", digest="blob", ok=False)
    assert fit(endpoint(model_digest="blob")).reasons[0].state == mc.FIT_MISSING
    with pytest.raises(p.ProviderPolicyError):
        resolve(endpoint(digest="blob"))


def test_exact_scoped_true_overrides_global_legacy_false():
    c.save_tested(c.manifest_key(vendor="ollama", model_id="fixture"),
        {c.TEST_TOOL_CALLING: {"ok": False}}, announced={})
    write(ok=True)
    assert fit().reasons[0].state == mc.FIT_TESTED
    resolve(endpoint())


@pytest.mark.parametrize("kind", ["legacy", "other_endpoint", "other_model", "unknown_protocol", "missing_digest"])
def test_supplied_alternative_probe_cannot_replace_missing_exact_store_observation(kind):
    manifest = (write(model="alternative", digest="blob" if kind == "missing_digest" else "")
                if kind not in ("legacy", "other_model") else {})
    ep = endpoint()
    if kind == "legacy":
        manifest = {"tested": {c.TEST_TOOL_CALLING: {"ok": True}}, "announced": {}}
    elif kind == "other_endpoint":
        ep["connection_id"] = "b"
    elif kind == "other_model":
        manifest = write(model="foreign")
    elif kind == "unknown_protocol":
        ep["base_url"] = "http://127.0.0.1:11434/v1"
    # Empty announcements ensure a wrong-scope probe cannot name an alternative.
    manifest = {**manifest, "announced": {}}
    ep["capability_alternatives"] = [{"model_id": "alternative", "manifest": manifest}]
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    result = mc.explain_fit(("tool_call",), model="alternative", assertions=candidate.assertions)
    assert result.reasons[0].state == mc.FIT_UNKNOWN


def test_exact_supplied_alternative_and_store_alternative_meet_requirement():
    write(ok=False)
    alternative = write(model="alternative", digest="blob", announced={})
    stored = write(model="stored", announced={})
    ep = endpoint(capability_alternatives=[
        {"model_id": "alternative", "digest": "blob", "manifest": alternative},
        {"model_id": "stored"}])
    with pytest.raises(p.ProviderPolicyError) as error:
        resolve(ep)
    assert "alternative" in error.value.detail and "stored" in error.value.detail


def test_alternative_declarations_and_explicit_capabilities_remain_supported():
    ep = endpoint(capability_alternatives=[
        {"model_id": "claimed", "manifest": {"tested": {c.TEST_TOOL_CALLING: {"ok": False}},
            "announced": {"capabilities": {"tools": True}}}},
        {"model_id": "explicit", "capabilities": ["tool_call"]}])
    candidates = p._fit_candidates_from_endpoint(ep)
    for candidate, state in zip(candidates, [mc.FIT_ANNOUNCED, mc.FIT_ANNOUNCED]):
        assert mc.explain_fit(("tool_call",), model=candidate.model_id,
            assertions=candidate.assertions).reasons[0].state == state


def test_remote_supplied_manifest_never_claims_scoped_probe_without_protocol():
    ep = endpoint(base_url="https://api.openai.com/v1", capability_alternatives=[
        {"model_id": "alternative", "manifest": write(model="alternative", announced={})}])
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    assert mc.explain_fit(("tool_call",), model=candidate.model_id,
        assertions=candidate.assertions).reasons[0].state == mc.FIT_UNKNOWN


def test_forged_matching_scope_without_store_record_cannot_verify_alternative():
    identity = c.get_effective_manifest(vendor="ollama", model_id="alternative", endpoint_id="a",
        protocol=c.NATIVE_OLLAMA_PROTOCOL)
    forged = {**identity, "tested": {c.TEST_TOOL_CALLING: {"ok": True}}, "announced": {}}
    assert forged["calibration_scope"] is not None and identity["evidence_scope"] == "unobserved"
    ep = endpoint(capability_alternatives=[{"model_id": "alternative", "manifest": forged}])
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    assert mc.explain_fit(("tool_call",), model=candidate.model_id,
        assertions=candidate.assertions).reasons[0].state == mc.FIT_UNKNOWN


def test_current_store_false_overrides_supplied_matching_scope_true():
    current = write(model="alternative", ok=False, announced={})
    supplied = {**current, "tested": {c.TEST_TOOL_CALLING: {"ok": True}},
        "announced": {"capabilities": {"tools": True}}}
    ep = endpoint(capability_alternatives=[{"model_id": "alternative", "manifest": supplied}])
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    assert mc.explain_fit(("tool_call",), model=candidate.model_id,
        assertions=candidate.assertions).reasons[0].state == mc.FIT_MISSING


def test_current_store_empty_announcements_override_supplied_declarations():
    write(model="alternative", ok=None, announced={})
    ep = endpoint(capability_alternatives=[{"model_id": "alternative",
        "manifest": {"announced": {"capabilities": {"tools": True}}}}])
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    assert mc.explain_fit(("tool_call",), model=candidate.model_id,
        assertions=candidate.assertions).reasons[0].state == mc.FIT_UNKNOWN
