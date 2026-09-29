"""Scoped probes and preserved legacy artifacts, without model/network IO."""
import pytest

from src import model_calibration as c, model_capabilities as mc
from src.creator import model_explorer
from tests.test_model_calibration import env, ADMIN, USER, ROOT


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *a, **kw: None)
    return tmp_path


def identity(endpoint="a", protocol=c.NATIVE_OLLAMA_PROTOCOL, digest="blob", model="fixture"):
    return dict(vendor="ollama", model_id=model, endpoint_id=endpoint, protocol=protocol, digest=digest)


def write(**scope):
    return c.save_scoped_tested(**identity(**scope), tested={c.TEST_TOOL_CALLING: {
        "ok": True, "tested_at": "2026-09-30T12:00:00Z", "evidence": {"fixture": True}}},
        announced={"capabilities": {"tools": True}})


@pytest.mark.parametrize("scope", [dict(endpoint="b"), dict(protocol="openai_v1"),
    dict(protocol=""), dict(endpoint=""), dict(digest="other")])
def test_other_scope_cannot_claim_verified_probe(scope):
    write()
    read = c.get_effective_manifest(**identity(**scope))
    assert read["tested"] == {} and read["evidence_scope"] == "unobserved"
    assert mc.assertions_from_calibration_manifest(read)[mc.CAP_TOOL_CALL].status != mc.ASSERTION_VERIFIED


def test_same_digest_alias_in_exact_endpoint_protocol_reuses_with_provenance():
    first = write(model="tag-a")
    second = c.get_effective_manifest(**identity(model="tag-b"))
    assert second["calibration_key"] == first["calibration_key"]
    assert second["calibration_scope"] == first["calibration_scope"]
    assert second["calibration_scope"]["digest"] == "blob"
    assert mc.assertions_from_calibration_manifest(second)[mc.CAP_TOOL_CALL].status == mc.ASSERTION_VERIFIED


@pytest.mark.parametrize("ok", [True, False])
def test_legacy_boolean_is_preserved_but_never_used_as_scoped_evidence(ok):
    key = c.manifest_key(vendor="ollama", model_id="fixture", digest="blob")
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": ok, "evidence": {"historic": True}}},
        announced={"capabilities": {"tools": True}})
    before = c.get_manifest(key)
    effective = c.get_effective_manifest(**identity())
    assert effective["tested"] == {}
    assert effective["legacy_manifest_key"] == key
    assert effective["announcement_manifest_key"] == key
    assert mc.assertions_from_calibration_manifest(effective)[mc.CAP_TOOL_CALL].status == mc.ASSERTION_CLAIMED
    write()
    assert c.get_manifest(key) == before


def test_scoped_key_without_matching_provenance_is_not_a_verified_record():
    key = c.calibration_key(**identity())
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": True}}, announced={"capabilities": {"tools": True}})
    assert c.get_effective_manifest(**identity())["tested"] == {}


@pytest.mark.parametrize("announced", [{"capabilities": {"tools": False}}, {}])
def test_exact_match_uses_its_own_announcements_even_when_empty(announced):
    key = c.manifest_key(vendor="ollama", model_id="fixture", digest="blob")
    c.save_announced(key, {"capabilities": {"tools": True}})
    c.save_scoped_tested(**identity(), tested={}, announced=announced)
    restored = c.get_effective_manifest(**identity())
    assert restored["announced"] == announced
    assert restored["announcement_manifest_key"] == restored["calibration_key"]


def test_exact_endpoint_strings_do_not_collide_under_legacy_sanitization():
    assert c.calibration_key(**identity(endpoint="a b")) != c.calibration_key(**identity(endpoint="a_b"))
    assert c.calibration_key(**identity(endpoint="A")) != c.calibration_key(**identity(endpoint="a"))


@pytest.mark.parametrize("url,expected", [("http://fixture/api/chat", c.NATIVE_OLLAMA_PROTOCOL),
    ("http://fixture/api", c.NATIVE_OLLAMA_PROTOCOL), ("http://fixture/v1", ""),
    ("http://fixture", ""), ("http://[invalid", "")])
def test_only_explicit_native_transport_is_selected(url, expected):
    assert c.explicit_native_protocol(url) == expected


def test_explorer_selects_scoped_or_declarations_only_by_explicit_transport():
    write()
    spec = {"vendor": "ollama", "model_id": "fixture", "digest": "blob"}
    native = model_explorer.legacy_calibration_for(spec, {"endpoint_id": "a", "endpoint_url": "http://fixture/api/chat"})
    assert native["tested"][c.TEST_TOOL_CALLING]["ok"] is True
    unknown = model_explorer.legacy_calibration_for(spec, {"endpoint_id": "a", "endpoint_url": "http://fixture"})
    assert unknown is None  # No declarations on the raw legacy artifact.


def test_provider_route_writes_scoped_probes_and_second_endpoint_sees_only_declarations(env, monkeypatch):
    from routes import local_models_routes as lm
    client, fake = env
    fake.ps = [{"name": "qwen3.5:9b", "model": "qwen3.5:9b", "digest": "aaa111"}]
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda **kw: [
        {"id": ep, "name": ep, "base_url": ROOT + "/v1", "root": ROOT, "same_machine": True}
        for ep in ("a", "b")])
    response = client.post("/api/models/qwen3.5:9b/calibrate?endpoint_id=a", headers=ADMIN)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["calibration_scope"]["endpoint_id"] == "a"
    assert data["calibration_scope"]["protocol"] == c.NATIVE_OLLAMA_PROTOCOL
    assert data["tested"][c.TEST_TOOL_CALLING]["ok"] is True
    count = fake.chat_calls
    read = client.get("/api/models/qwen3.5:9b/capabilities?endpoint_id=b", headers=USER)
    assert read.status_code == 200 and read.json()["tested"] == {}
    assert read.json()["announced"]["capabilities"]["tools"] is True
    assert fake.chat_calls == count


def test_fit_reader_for_v1_keeps_legacy_evidence_out_of_verified_assertions(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from routes import model_routes as routes
    from tests.test_cmp11_fit_explain import _Ep, _Db, _endpoint_for, _request
    row = _Ep(id="a", name="Ollama", base_url=ROOT + "/v1", api_key=None,
        is_enabled=True, endpoint_kind="local", supports_tools=None,
        cached_models='["fixture"]', hidden_models=None, pinned_models=None)
    monkeypatch.setattr(routes, "SessionLocal", lambda: _Db([row]))
    monkeypatch.setattr(routes, "httpx", SimpleNamespace(get=lambda *a, **kw: (_ for _ in ()).throw(OSError("fixture no tags"))))
    key = c.manifest_key(vendor="ollama", model_id="fixture", endpoint_id="a")
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": True}}, announced={"capabilities": {"tools": True}})
    endpoint = _endpoint_for(routes.setup_model_routes(model_discovery=None))
    data = asyncio.run(endpoint(_request(), model="fixture", endpoint_id="a", needs="tools"))
    assert data["reasons"][0]["state"] != "tested"
    assert c.get_manifest(key)["tested"][c.TEST_TOOL_CALLING]["ok"] is True


def test_fit_native_chat_reader_recovers_digest_from_native_tags_root(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from routes import model_routes as routes
    from tests.test_cmp11_fit_explain import _Ep, _Db, _endpoint_for, _request
    row = _Ep(id="a", name="Ollama", base_url=ROOT + "/api/chat", api_key=None,
        is_enabled=True, endpoint_kind="local", supports_tools=None,
        cached_models='["fixture"]', hidden_models=None, pinned_models=None)
    monkeypatch.setattr(routes, "SessionLocal", lambda: _Db([row]))
    calls = []

    def tags(url, **kwargs):
        calls.append(url)
        assert url == ROOT + "/api/tags"
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": "fixture", "digest": "blob"}]})

    monkeypatch.setattr(routes, "httpx", SimpleNamespace(get=tags))
    write()
    endpoint = _endpoint_for(routes.setup_model_routes(model_discovery=None))
    data = asyncio.run(endpoint(_request(), model="fixture", endpoint_id="a", needs="tools"))
    assert calls == [ROOT + "/api/tags"]
    assert data["reasons"][0]["state"] == "tested"
