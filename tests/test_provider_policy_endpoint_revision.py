"""H22 policy consumes only explicit, frozen endpoint revisions. No network."""
from types import SimpleNamespace

import pytest

from src import model_calibration as c, model_capabilities as mc, provider_policy as p


NATIVE = "http://127.0.0.1:11434/api/chat"


@pytest.fixture(autouse=True)
def temporary_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *args, **kwargs: None)


def endpoint(**changes):
    return {"connection_id": "endpoint-a", "base_url": NATIVE, "connection_revision": "revision-a", **changes}


def write(*, model="fixture", revision="revision-a", ok=True, digest="", announced=None):
    return c.save_scoped_tested(vendor="ollama", model_id=model, endpoint_id="endpoint-a",
        protocol=c.NATIVE_OLLAMA_PROTOCOL, endpoint_revision=revision, digest=digest,
        announced={} if announced is None else announced, tested={c.TEST_TOOL_CALLING: {"ok": ok}})


def fit(ep=None):
    return p._build_fit("fixture", ep if ep is not None else endpoint(), "endpoint-a", p.NETWORK_LOCAL, ("tool_call",))


def candidate_state(ep):
    candidate = p._fit_candidates_from_endpoint(ep)[0]
    return mc.explain_fit(("tool_call",), model=candidate.model_id, assertions=candidate.assertions).reasons[0].state


@pytest.mark.parametrize("ok,state", [(True, mc.FIT_TESTED), (False, mc.FIT_MISSING)])
@pytest.mark.parametrize("revision_field", ["connection_revision", "endpoint_revision"])
def test_matching_v3_revision_controls_requirements_and_alias_is_explicit(ok, state, revision_field):
    write(ok=ok)
    ep = endpoint(connection_revision=None, **{revision_field: "revision-a"}) if revision_field != "connection_revision" else endpoint()
    assert fit(ep).reasons[0].state == state
    requirements = p.RouteRequirements(required_parameters=("tool_call",))
    if ok:
        p.resolve_route(requested_model="fixture", endpoint=ep, requirements=requirements)
    else:
        with pytest.raises(p.ProviderPolicyError, match="tool_call"):
            p.resolve_route(requested_model="fixture", endpoint=ep, requirements=requirements)


@pytest.mark.parametrize("revision", [None, "", "revision-b", 42, {"revision": "revision-a"}])
@pytest.mark.parametrize("ok", [True, False])
def test_missing_invalid_or_rotated_revision_never_adopts_old_observation(revision, ok):
    write(ok=ok)
    assert fit(endpoint(connection_revision=revision)).reasons[0].state == mc.FIT_UNKNOWN
    p.resolve_route(requested_model="fixture", endpoint=endpoint(connection_revision=revision),
        requirements=p.RouteRequirements(required_parameters=("tool_call",)))


def test_canonical_revision_wins_over_alias_and_whitespace_is_normalized():
    write(ok=False)
    write(revision="revision-b", ok=True)
    assert fit(endpoint(connection_revision=" revision-a ", endpoint_revision="revision-b")).reasons[0].state == mc.FIT_MISSING
    assert fit(endpoint(connection_revision={"invalid": True}, endpoint_revision="revision-a")).reasons[0].state == mc.FIT_UNKNOWN


@pytest.mark.parametrize("ok", [True, False])
def test_revisionless_v2_remains_readable_without_becoming_current_evidence(ok):
    write(revision="", ok=ok)
    legacy_key = c.calibration_key(vendor="ollama", model_id="fixture", endpoint_id="endpoint-a",
        protocol=c.NATIVE_OLLAMA_PROTOCOL)
    assert fit().reasons[0].state == mc.FIT_UNKNOWN
    assert c.get_manifest(legacy_key)["tested"][c.TEST_TOOL_CALLING]["ok"] is ok


@pytest.mark.parametrize("ok", [True, False])
def test_rotated_route_preserves_declarations_without_legacy_probe_shadowing(ok):
    c.save_tested(c.manifest_key(vendor="ollama", model_id="fixture"),
        {c.TEST_TOOL_CALLING: {"ok": ok}}, announced={"capabilities": {"tools": True}})
    write(ok=not ok)
    assert fit(endpoint(connection_revision="revision-b")).reasons[0].state == mc.FIT_ANNOUNCED


@pytest.mark.parametrize("digest_field", ["digest", "model_digest"])
def test_alternative_digest_alias_uses_store_observation_only_for_same_revision(digest_field):
    stored = write(model="original-tag", digest="weights", announced={})
    ep = endpoint(capability_alternatives=[{"model_id": "alias-tag", digest_field: "weights", "manifest": stored}])
    assert candidate_state(ep) == mc.FIT_TESTED
    ep["connection_revision"] = "revision-b"
    assert candidate_state(ep) == mc.FIT_UNKNOWN


@pytest.mark.parametrize("digest", [None, "other", 42, {"digest": "weights"}])
def test_alternative_digest_is_not_guessed_from_supplied_matching_scope(digest):
    stored = write(model="original-tag", digest="weights")
    ep = endpoint(capability_alternatives=[{"model_id": "alias-tag", "digest": digest, "manifest": stored}])
    assert candidate_state(ep) == mc.FIT_UNKNOWN


def test_forged_v3_scope_without_store_observation_remains_unknown():
    forged = c.get_effective_manifest(vendor="ollama", model_id="alternative", endpoint_id="endpoint-a",
        protocol=c.NATIVE_OLLAMA_PROTOCOL, endpoint_revision="revision-a")
    forged.update(evidence_scope="exact", tested={c.TEST_TOOL_CALLING: {"ok": True}})
    assert forged["calibration_scope"]["endpoint_revision"] == "revision-a"
    assert candidate_state(endpoint(capability_alternatives=[{"model_id": "alternative", "manifest": forged}])) == mc.FIT_UNKNOWN


def test_exact_store_failure_overrides_forged_alternative_success():
    stored = write(model="alternative", ok=False)
    forged = {**stored, "tested": {c.TEST_TOOL_CALLING: {"ok": True}}, "announced": {"capabilities": {"tools": True}}}
    assert candidate_state(endpoint(capability_alternatives=[{"model_id": "alternative", "manifest": forged}])) == mc.FIT_MISSING


def test_requested_model_and_alternatives_share_snapshot_when_original_endpoint_changes(monkeypatch):
    for model in ("fixture", "alternative"):
        write(model=model, ok=True)
        write(model=model, revision="revision-b", ok=False)
    ep = endpoint(capability_alternatives=[{"model_id": "alternative"}])
    real_reader = c.get_effective_manifest
    captured = []

    def read(**kwargs):
        captured.append((kwargs["model_id"], kwargs.get("endpoint_revision")))
        result = real_reader(**kwargs)
        ep["connection_revision"] = "revision-b"
        ep["base_url"] = "https://api.openai.com/v1"
        return result

    monkeypatch.setattr(c, "get_effective_manifest", read)
    assert fit(ep).reasons[0].state == mc.FIT_TESTED
    assert captured == [("alternative", "revision-a"), ("fixture", "revision-a")]


def test_orm_shaped_endpoint_supplies_revision_without_database_lookup():
    write(ok=True)
    ep = SimpleNamespace(id="endpoint-a", base_url=NATIVE, connection_revision="revision-a")
    assert fit(ep).reasons[0].state == mc.FIT_TESTED


def test_explicit_capabilities_keep_existing_semantics_independent_of_revision():
    write(ok=False)
    assert fit(endpoint(capabilities=["tool_call"])).reasons[0].state == mc.FIT_ANNOUNCED
    assert fit(endpoint(capabilities=[])).reasons[0].state == mc.FIT_MISSING
