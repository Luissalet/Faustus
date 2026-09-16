"""WP06 — model identity vs. deployment (MOD-01, MOD-02, MOD-19).

Three layers, tested separately:

  - `src/model_identity.py` in isolation: ModelSpec/DeploymentManifest id
    determinism, the sqlite evidence store, and `resolve_deployment` against
    monkeypatched `_fetch_tags`/`_fetch_show`/`_fetch_version` — no network,
    ever, in this file (CONTRATO rule 8 / the ficha's own closing note).
  - the additive hook in `src/model_calibration.py::save_tested`: the legacy
    `ollama|digest:<digest>` manifest keeps reading exactly as before,
    whether or not `deployment_id` is passed.
  - `routes/model_identity_routes.py` through a real FastAPI app + TestClient
    (COMUN.md rule 7: no route logic tested purely in mocks), including the
    `creator_enabled` feature-flag gate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src import model_calibration as mcal
from src import model_identity as mi
from src import settings as odysseus_settings
import routes.model_identity_routes as mir


# ── ModelSpec / DeploymentManifest identity ──────────────────────────────────

def test_model_spec_id_is_shared_across_tags_pointing_at_the_same_digest():
    a = mi.model_spec_id_for(vendor="ollama", digest="sha256:aaa", model_id="qwen3.5:9b")
    b = mi.model_spec_id_for(vendor="ollama", digest="sha256:aaa", model_id="qwen3.5:latest")
    assert a == b


def test_model_spec_id_changes_when_the_digest_changes_a_repull():
    a = mi.model_spec_id_for(vendor="ollama", digest="sha256:aaa", model_id="qwen3.5:9b")
    b = mi.model_spec_id_for(vendor="ollama", digest="sha256:bbb", model_id="qwen3.5:9b")
    assert a != b


def test_deployment_id_same_config_same_id_different_config_different_id():
    base = dict(engine_kind="ollama", engine_version="0.5.1", model_digest="sha256:aaa")
    a = mi.deployment_id_for(**base, configuration={"num_ctx": 4096})
    b = mi.deployment_id_for(**base, configuration={"num_ctx": 4096})
    c = mi.deployment_id_for(**base, configuration={"num_ctx": 8192})
    assert a == b, "same digest, same engine, same effective config: one deployment"
    assert a != c, "a different num_ctx is a different deployment, on purpose"


def test_deployment_id_changes_with_engine_or_version_even_with_identical_config():
    config = {"num_ctx": 4096}
    a = mi.deployment_id_for(engine_kind="ollama", engine_version="0.5.1", model_digest="sha256:aaa", configuration=config)
    b = mi.deployment_id_for(engine_kind="llama.cpp", engine_version="0.5.1", model_digest="sha256:aaa", configuration=config)
    c = mi.deployment_id_for(engine_kind="ollama", engine_version="0.6.0", model_digest="sha256:aaa", configuration=config)
    assert len({a, b, c}) == 3


# ── resolve_deployment: monkeypatched network, never real ───────────────────

def _patch_ollama(monkeypatch, *, digest="sha256:deadbeef", version="0.5.1",
                   capabilities=("completion", "tools", "vision"), context_length=32768,
                   quantization="Q4_K_M", parameter_size="9.0B", model="qwen3.5:9b"):
    def fake_tags(root):
        return {"models": [{"name": model, "model": model, "digest": digest}]}

    def fake_show(root, name):
        return {
            "capabilities": list(capabilities),
            "details": {"family": "qwen3", "parameter_size": parameter_size,
                        "quantization_level": quantization, "families": ["qwen3"]},
            "model_info": {"general.architecture": "qwen3", "qwen3.context_length": context_length},
            "license": "Apache 2.0",
        }

    monkeypatch.setattr(mi, "_fetch_tags", fake_tags)
    monkeypatch.setattr(mi, "_fetch_show", fake_show)
    monkeypatch.setattr(mi, "_fetch_version", lambda root: version)


def test_resolve_deployment_builds_spec_and_manifest_with_no_network(monkeypatch):
    _patch_ollama(monkeypatch)
    resolution = mi.resolve_deployment("http://127.0.0.1:11434", "qwen3.5:9b", endpoint_id="ep1")
    assert resolution.model_spec.digest == "sha256:deadbeef"
    assert resolution.model_spec.vendor == "ollama"
    assert "vision" in resolution.model_spec.capabilities or "tool_call" in resolution.model_spec.capabilities
    assert resolution.deployment.model_spec_id == resolution.model_spec.model_spec_id
    assert resolution.deployment.engine.kind == "ollama"
    assert resolution.deployment.engine.version == "0.5.1"
    assert resolution.deployment.configuration.get("num_ctx") == 32768


def test_resolve_deployment_same_digest_two_endpoints_same_config_same_deployment_id(monkeypatch):
    """WP06 acceptance: same digest + same effective config -> same
    deployment_id, whichever endpoint answered — the formula is
    (engine, engine_version, model_digest, config), endpoint identity is
    NOT one of its inputs (04_MODELOS_Y_RECURSOS.md's deployment level is
    about engine+config, not about which box happens to be running it)."""
    _patch_ollama(monkeypatch)
    a = mi.resolve_deployment("http://host-a:11434", "qwen3.5:9b", endpoint_id="ep-a")
    b = mi.resolve_deployment("http://host-b:11434", "qwen3.5:9b", endpoint_id="ep-b")
    assert a.deployment.deployment_id == b.deployment.deployment_id


def test_resolve_deployment_same_digest_two_endpoints_different_config_different_deployment_id(monkeypatch):
    _patch_ollama(monkeypatch)
    import src.model_load_options as mlo

    def fake_resolve_for_request(url, model, **kw):
        return {"num_ctx": 16384} if "host-a" in url else {"num_ctx": 4096}

    monkeypatch.setattr(mlo, "resolve_for_request", fake_resolve_for_request)
    a = mi.resolve_deployment("http://host-a:11434", "qwen3.5:9b", endpoint_id="ep-a")
    b = mi.resolve_deployment("http://host-b:11434", "qwen3.5:9b", endpoint_id="ep-b")
    assert a.deployment.deployment_id != b.deployment.deployment_id


def test_resolve_deployment_requires_endpoint_and_model():
    with pytest.raises(ValueError):
        mi.resolve_deployment("", "qwen3.5:9b")
    with pytest.raises(ValueError):
        mi.resolve_deployment("http://127.0.0.1:11434", "")


def test_resolve_deployment_survives_an_unreachable_endpoint(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("nope")

    monkeypatch.setattr(mi, "_fetch_tags", boom)
    monkeypatch.setattr(mi, "_fetch_show", boom)
    monkeypatch.setattr(mi, "_fetch_version", lambda root: "")
    resolution = mi.resolve_deployment("http://127.0.0.1:11434", "qwen3.5:9b")
    assert resolution.deployment.availability == "unknown"
    assert resolution.deployment.weights_revision == "unknown"


# ── ModelIdentityStore: sqlite CAS + evidence ordering ───────────────────────

@pytest.fixture
def store(tmp_path):
    return mi.ModelIdentityStore(db_path=str(tmp_path / "model_identity.db"))


def _spec_and_manifest(digest="sha256:aaa", config=None):
    spec_id = mi.model_spec_id_for(vendor="ollama", digest=digest, model_id="qwen3.5:9b")
    spec = mi.ModelSpec(model_spec_id=spec_id, vendor="ollama", family="chat",
                        model_id="qwen3.5:9b", digest=digest)
    dep_id = mi.deployment_id_for(engine_kind="ollama", engine_version="0.5.1",
                                  model_digest=digest, configuration=config or {"num_ctx": 4096})
    manifest = mi.DeploymentManifest(
        deployment_id=dep_id, model_spec_id=spec_id, weights_revision=digest,
        engine=mi.EngineInfo(kind="ollama", version="0.5.1"), endpoint_id="ep1",
        endpoint_url="http://127.0.0.1:11434", configuration=config or {"num_ctx": 4096},
        configuration_fingerprint=mi.configuration_fingerprint(config or {"num_ctx": 4096}),
    )
    return spec, manifest


def test_store_upsert_and_list_deployments_round_trips(store):
    spec, manifest = _spec_and_manifest()
    store.upsert_model_spec(spec)
    store.upsert_deployment(manifest)
    listed = store.list_deployments()
    assert len(listed) == 1
    assert listed[0]["deployment_id"] == manifest.deployment_id
    assert store.get_model_spec(spec.model_spec_id)["digest"] == "sha256:aaa"


def test_store_resolve_capability_prefers_measured_over_probed_over_inferred_over_declared(store):
    dep_id = "dep-1"
    store.add_evidence(deployment_id=dep_id, capability="vision", level=mi.LEVEL_DECLARED, source="announced")
    store.add_evidence(deployment_id=dep_id, capability="vision", level=mi.LEVEL_INFERRED, source="heuristic")
    store.add_evidence(deployment_id=dep_id, capability="vision", level=mi.LEVEL_PROBED, source="calibration")
    best = store.resolve_capability(dep_id, "vision")
    assert best["level"] == mi.LEVEL_PROBED
    store.add_evidence(deployment_id=dep_id, capability="vision", level=mi.LEVEL_MEASURED, source="benchmark")
    best2 = store.resolve_capability(dep_id, "vision")
    assert best2["level"] == mi.LEVEL_MEASURED
    assert len(store.list_evidence(dep_id, capability="vision")) == 4


def test_store_resolve_capability_is_none_with_no_evidence_at_all(store):
    assert store.resolve_capability("dep-nope", "vision") is None


def test_store_add_evidence_rejects_an_unknown_level(store):
    with pytest.raises(ValueError):
        store.add_evidence(deployment_id="dep-1", capability="vision", level="maybe", source="x")


def test_store_legacy_unknown_evidence_is_capped_below_probed(store):
    """MOD-19: 'un probe sin identidad suficiente no se promueve a
    verificado' — asking for `measured`/`probed` on a legacy_unknown-scoped
    deployment id is silently capped at `inferred`."""
    legacy_id = mi.legacy_deployment_id("ollama|digest:oldstuff")
    assert legacy_id.startswith("legacy_unknown:")
    ev = store.add_evidence(deployment_id=legacy_id, capability="vision", level=mi.LEVEL_MEASURED, source="old probe")
    assert ev.level == mi.LEVEL_INFERRED


# ── additive hook in src/model_calibration.py::save_tested ──────────────────

def test_save_tested_legacy_key_still_reads_back_unchanged(tmp_path):
    """The legacy manifest is byte-for-byte what it always was, whether or
    not deployment_id is ever passed — the exact shape
    `tests/test_model_calibration.py::test_save_announced_then_save_tested_round_trip_and_merge`
    already locks in."""
    key = "ollama|digest:aaa111"
    announced = {"capabilities": {"tools": True, "vision": False}, "limits": {"context_tokens": 32768}}
    mcal.save_announced(key, announced, data_dir=str(tmp_path))
    tested = {mcal.TEST_JSON_MODE: {"ok": True, "tested_at": "t1", "evidence": {}}}
    manifest = mcal.save_tested(key, tested, announced=announced, data_dir=str(tmp_path))
    assert manifest["tested"][mcal.TEST_JSON_MODE]["ok"] is True
    read_back = mcal.get_manifest(key, data_dir=str(tmp_path))
    assert read_back == manifest


def test_save_tested_records_deployment_evidence_when_creator_enabled_and_deployment_id_given(tmp_path, monkeypatch):
    monkeypatch.setattr(odysseus_settings, "get_setting", lambda k, default=None: True if k == "creator_enabled" else default)
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)

    key = "ollama|digest:bbb222"
    dep_id = "dep-xyz"
    tested = {
        mcal.TEST_VISION: {"ok": True, "tested_at": "2026-01-01T00:00:00+00:00", "evidence": {}},
        mcal.TEST_JSON_MODE: {"ok": None, "tested_at": "t", "evidence": {"skipped": "time budget exceeded"}},
    }
    mcal.save_tested(key, tested, announced={}, data_dir=str(tmp_path), deployment_id=dep_id)

    evidence = test_store.list_evidence(dep_id)
    caps = {e["capability"] for e in evidence}
    assert mcal.TEST_VISION in caps
    assert mcal.TEST_JSON_MODE not in caps, "a time-budget skip observed nothing: no evidence row"
    vision_ev = next(e for e in evidence if e["capability"] == mcal.TEST_VISION)
    assert vision_ev["level"] == mi.LEVEL_PROBED
    assert vision_ev["source"] == "model_calibration.run_calibration"


def test_save_tested_falls_back_to_legacy_unknown_scope_without_a_deployment_id(tmp_path, monkeypatch):
    monkeypatch.setattr(odysseus_settings, "get_setting", lambda k, default=None: True if k == "creator_enabled" else default)
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)

    key = "ollama|digest:ccc333"
    tested = {mcal.TEST_VISION: {"ok": True, "tested_at": "t", "evidence": {}}}
    mcal.save_tested(key, tested, announced={}, data_dir=str(tmp_path))  # no deployment_id

    legacy_id = mi.legacy_deployment_id(key)
    evidence = test_store.list_evidence(legacy_id)
    assert len(evidence) == 1
    assert evidence[0]["level"] == mi.LEVEL_INFERRED, "no resolved deployment: never promoted to probed"


def test_save_tested_writes_nothing_to_model_identity_when_creator_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(odysseus_settings, "get_setting", lambda k, default=None: default)  # creator_enabled -> False
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)

    key = "ollama|digest:ddd444"
    tested = {mcal.TEST_VISION: {"ok": True, "tested_at": "t", "evidence": {}}}
    mcal.save_tested(key, tested, announced={}, data_dir=str(tmp_path), deployment_id="dep-should-not-write")

    assert test_store.list_evidence("dep-should-not-write") == []


# ── routes/model_identity_routes.py ──────────────────────────────────────────

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(odysseus_settings, "get_setting",
                        lambda k, default=None: True if k == "creator_enabled" else default)
    _patch_module_ollama(monkeypatch)

    app = FastAPI()
    app.include_router(mir.setup_model_identity_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    client = TestClient(app, raise_server_exceptions=False)
    yield client, test_store


def _patch_module_ollama(monkeypatch):
    def fake_tags(root):
        return {"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b", "digest": "sha256:routetest"}]}

    def fake_show(root, name):
        return {
            "capabilities": ["completion", "tools"],
            "details": {"family": "qwen3", "parameter_size": "9.0B", "quantization_level": "Q4_K_M"},
            "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 8192},
            "license": "Apache 2.0",
        }

    monkeypatch.setattr(mi, "_fetch_tags", fake_tags)
    monkeypatch.setattr(mi, "_fetch_show", fake_show)
    monkeypatch.setattr(mi, "_fetch_version", lambda root: "0.9.0")


ADMIN = {"x-user": "root"}
USER = {"x-user": "alice"}


def test_get_identity_returns_spec_deployment_and_evidence(app_client):
    client, store = app_client
    r = client.get("/api/models/identity", params={"endpoint": "http://127.0.0.1:11434", "model": "qwen3.5:9b"}, headers=USER)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model_spec"]["digest"] == "sha256:routetest"
    assert data["deployment"]["engine"]["version"] == "0.9.0"
    assert data["evidence"] == []
    # persisted, so GET /deployments now knows about it
    listed = store.list_deployments()
    assert len(listed) == 1


def test_get_deployments_lists_what_was_resolved(app_client):
    client, store = app_client
    client.get("/api/models/identity", params={"endpoint": "http://127.0.0.1:11434", "model": "qwen3.5:9b"}, headers=USER)
    r = client.get("/api/models/deployments", headers=USER)
    assert r.status_code == 200, r.text
    deployments = r.json()["deployments"]
    assert len(deployments) == 1
    assert deployments[0]["evidence_count"] == 0


def test_post_evidence_requires_admin(app_client):
    client, store = app_client
    r = client.post("/api/models/deployments/dep-1/evidence", json={"capability": "vision", "level": "measured"}, headers=USER)
    assert r.status_code == 403


def test_post_evidence_admin_adds_a_measured_row(app_client):
    client, store = app_client
    r = client.post(
        "/api/models/deployments/dep-1/evidence",
        json={"capability": "vision", "level": "measured", "source": "manual benchmark", "conditions": {"note": "ran by hand"}},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["evidence"]["level"] == "measured"
    assert store.resolve_capability("dep-1", "vision")["level"] == "measured"


def test_post_evidence_rejects_an_unknown_level(app_client):
    client, store = app_client
    r = client.post("/api/models/deployments/dep-1/evidence", json={"capability": "vision", "level": "vibes"}, headers=ADMIN)
    assert r.status_code == 400


def test_routes_404_when_creator_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(odysseus_settings, "get_setting", lambda k, default=None: default)  # creator_enabled -> False
    _patch_module_ollama(monkeypatch)

    app = FastAPI()
    app.include_router(mir.setup_model_identity_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    client = TestClient(app, raise_server_exceptions=False)

    r1 = client.get("/api/models/identity", params={"endpoint": "http://x:11434", "model": "m"}, headers=USER)
    assert r1.status_code == 404
    r2 = client.get("/api/models/deployments", headers=USER)
    assert r2.status_code == 404
    r3 = client.post("/api/models/deployments/dep-1/evidence", json={"capability": "vision", "level": "measured"}, headers=ADMIN)
    assert r3.status_code == 404
    assert test_store.list_deployments() == [] and test_store.list_evidence("dep-1") == []
