"""WP07 — multimodal capabilities and parameter contracts (MOD-03..06,
MOD-08, MOD-20, QA06).

Covers `src/creator/capabilities.py`, `src/creator/params.py`, the additive
`src/model_capabilities.py` vocabulary, and `routes/creator_capability_routes.py`
through a real FastAPI app + TestClient.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src import model_capabilities as mc
from src import model_identity as mi
from src import settings as odysseus_settings
from src.creator import capabilities as cap
from src.creator import params as pr
import routes.creator_capability_routes as ccr


# ── src/model_capabilities.py — additive vocabulary stays additive ─────────

def test_new_capability_tokens_are_additive_and_legacy_ones_are_untouched():
    # legacy tokens: same values as before, still present, still normalizable
    assert mc.normalize_capability("vision") == mc.CAP_VISION
    assert mc.normalize_capability("tool_calls") == mc.CAP_TOOL_CALL
    assert mc.CAP_IMAGE_GENERATION in mc.CAPABILITIES
    # new WP07 tokens are real members of the canonical set
    for token in (mc.CAP_VIDEO_INPUT, mc.CAP_MUSIC_GENERATION, mc.CAP_REGION_EDITING,
                  mc.CAP_CONTROLNET_CONDITIONING, mc.CAP_UPSCALING):
        assert token in mc.CAPABILITIES
    assert mc.normalize_capability("controlnet") == mc.CAP_CONTROLNET_CONDITIONING
    assert mc.normalize_capability("text_to_music") == mc.CAP_MUSIC_GENERATION
    assert mc.normalize_capability("region_edit") == mc.CAP_REGION_EDITING
    assert mc.normalize_capability("upscale") == mc.CAP_UPSCALING
    assert mc.normalize_capability("video_in") == mc.CAP_VIDEO_INPUT
    # Spanish labels exist for every new token — never a blank tooltip
    for token in (mc.CAP_VIDEO_INPUT, mc.CAP_MUSIC_GENERATION, mc.CAP_REGION_EDITING,
                  mc.CAP_CONTROLNET_CONDITIONING, mc.CAP_UPSCALING):
        assert mc.capability_label_es(token) != token


def test_new_task_tokens_do_not_collide_with_legacy_ones():
    legacy = {mc.TASK_CHAT_COMPLETIONS, mc.TASK_IMAGE_GENERATE, mc.TASK_IMAGE_EDIT,
              mc.TASK_VIDEO_GENERATE, mc.TASK_AUDIO_TRANSCRIBE, mc.TASK_AUDIO_SYNTHESIZE}
    new = {mc.TASK_IMAGE_INPAINT, mc.TASK_IMAGE_UPSCALE, mc.TASK_IMAGE_CONTROLNET,
           mc.TASK_MUSIC_GENERATE, mc.TASK_VIDEO_EDIT}
    assert legacy.isdisjoint(new)
    assert len(new) == 5  # every new token is distinct


# ── src/creator/capabilities.py ─────────────────────────────────────────────

@pytest.fixture
def identity_store(tmp_path):
    return mi.ModelIdentityStore(db_path=str(tmp_path / "model_identity.db"))


def _make_deployment(store: mi.ModelIdentityStore, suffix: str = "a") -> str:
    spec = mi.ModelSpec(
        model_spec_id=f"ollama:digest:spec-{suffix}", vendor="ollama", family="qwen3",
        model_id=f"qwen3:{suffix}", modalities_in=("text",), modalities_out=("text",),
    )
    store.upsert_model_spec(spec)
    manifest = mi.DeploymentManifest(
        deployment_id=f"dep-{suffix}", model_spec_id=spec.model_spec_id, weights_revision=f"sha256:{suffix}",
        engine=mi.EngineInfo(kind="ollama", version="0.9.0"), endpoint_id="ep-1",
        endpoint_url="http://127.0.0.1:11434",
    )
    store.upsert_deployment(manifest)
    return manifest.deployment_id


def test_unknown_axis_reports_unknown_status_not_unsupported(identity_store):
    dep_id = _make_deployment(identity_store, "a")
    profile = cap.capability_profile_for_deployment(dep_id, store=identity_store)
    # no evidence recorded for anything: every capability axis is unknown,
    # never guessed as unsupported
    for axis in cap.AXES:
        if axis == cap.AXIS_TEXT:
            continue
        assert profile.axes[axis].status == cap.STATUS_UNKNOWN
    assert set(profile.axes) == set(cap.AXES)


def test_vision_support_does_not_imply_image_generation_or_editing(identity_store):
    """WP07 closing criterion: 'no se deriva edición o generación del mero
    soporte de visión'."""
    dep_id = _make_deployment(identity_store, "b")
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_VISION_IN, level=mi.LEVEL_MEASURED,
                         source="probe", store=identity_store)
    profile = cap.capability_profile_for_deployment(dep_id, store=identity_store)
    assert profile.axes[cap.AXIS_VISION_IN].status == cap.STATUS_KNOWN
    # every other axis, including image_out and image_edit, stays unknown
    assert profile.axes[cap.AXIS_IMAGE_OUT].status == cap.STATUS_UNKNOWN
    assert profile.axes[cap.AXIS_IMAGE_EDIT].status == cap.STATUS_UNKNOWN
    assert profile.axes[cap.AXIS_REGION_EDIT].status == cap.STATUS_UNKNOWN


def test_two_deployments_of_the_same_model_with_different_evidence_have_different_capabilities(identity_store):
    dep_measured = _make_deployment(identity_store, "measured")
    dep_bare = _make_deployment(identity_store, "bare")
    cap.record_evidence(deployment_id=dep_measured, axis=cap.AXIS_IMAGE_OUT, level=mi.LEVEL_MEASURED,
                         source="probe", store=identity_store)

    profile_measured = cap.capability_profile_for_deployment(dep_measured, store=identity_store)
    profile_bare = cap.capability_profile_for_deployment(dep_bare, store=identity_store)

    assert profile_measured.axes[cap.AXIS_IMAGE_OUT].status == cap.STATUS_KNOWN
    assert profile_bare.axes[cap.AXIS_IMAGE_OUT].status == cap.STATUS_UNKNOWN


def test_declared_level_reads_as_announced_and_measured_as_known(identity_store):
    dep_id = _make_deployment(identity_store, "c")
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_TTS, level=mi.LEVEL_DECLARED,
                         source="provider_docs", store=identity_store)
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_ASR, level=mi.LEVEL_PROBED,
                         source="probe", store=identity_store)
    profile = cap.capability_profile_for_deployment(dep_id, store=identity_store)
    assert profile.axes[cap.AXIS_TTS].status == cap.STATUS_ANNOUNCED
    assert profile.axes[cap.AXIS_ASR].status == cap.STATUS_KNOWN


def test_explicit_negative_evidence_reads_as_unsupported(identity_store):
    dep_id = _make_deployment(identity_store, "d")
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_MUSIC, level=mi.LEVEL_MEASURED,
                         source="probe", supported=False, store=identity_store)
    profile = cap.capability_profile_for_deployment(dep_id, store=identity_store)
    assert profile.axes[cap.AXIS_MUSIC].status == cap.STATUS_UNSUPPORTED


def test_record_evidence_rejects_the_text_axis():
    with pytest.raises(ValueError):
        cap.record_evidence(deployment_id="dep-x", axis=cap.AXIS_TEXT, level=mi.LEVEL_MEASURED, source="x")


def test_known_deployments_summary_never_resolves_or_loads(identity_store, monkeypatch):
    dep_id = _make_deployment(identity_store, "e")
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_IMAGE_OUT, level=mi.LEVEL_MEASURED,
                         source="probe", store=identity_store)

    def _boom(*a, **k):
        raise AssertionError("known_deployments_summary must not resolve over the network")

    monkeypatch.setattr(mi, "resolve_deployment", _boom)
    rows = cap.known_deployments_summary(store=identity_store)
    assert len(rows) == 1
    assert rows[0]["deployment_id"] == dep_id
    assert rows[0]["evidenced_axes"] == 1


# ── src/creator/params.py ───────────────────────────────────────────────────

def test_describe_for_ui_returns_none_for_unregistered_engine_task():
    assert pr.describe_for_ui("no_such_engine", "no_such_task") is None


def test_describe_for_ui_is_json_serializable_and_matches_registered_schema():
    schema = pr.describe_for_ui("ace_step", mc.TASK_MUSIC_GENERATE)
    assert schema is not None
    assert schema["engine"] == "ace_step"
    assert schema["task"] == mc.TASK_MUSIC_GENERATE
    keys = {f["key"] for f in schema["fields"]}
    assert {"duration_s", "guidance", "cfg", "seed"} <= keys
    import json
    json.dumps(schema)  # must not raise


def test_validate_unknown_engine_task_fails_with_target():
    result = pr.validate("nope", "nope", {})
    assert result.ok is False
    assert result.errors[0].code == pr.ERR_UNKNOWN_ENGINE_TASK


def test_validate_missing_required_field_reports_target():
    result = pr.validate("invoke", mc.TASK_IMAGE_EDIT, {"prompt": "a cat"})
    assert result.ok is False
    codes = {(e.field, e.code) for e in result.errors}
    assert ("image", pr.ERR_MISSING_REQUIRED) in codes


def test_validate_unknown_param_is_rejected_with_target():
    result = pr.validate("invoke", mc.TASK_IMAGE_EDIT, {"image": "art-1", "prompt": "x", "bogus": 1})
    assert result.ok is False
    assert any(e.field == "bogus" and e.code == pr.ERR_UNKNOWN_PARAM for e in result.errors)


def test_validate_out_of_range_and_enum_errors():
    r1 = pr.validate("ace_step", mc.TASK_MUSIC_GENERATE, {"duration_s": 5})
    assert r1.ok is False
    assert any(e.field == "duration_s" and e.code == pr.ERR_OUT_OF_RANGE for e in r1.errors)

    r2 = pr.validate("chatterbox", mc.TASK_AUDIO_SYNTHESIZE, {"text": "hola", "language": "klingon"})
    assert r2.ok is False
    assert any(e.field == "language" and e.code == pr.ERR_NOT_IN_ENUM for e in r2.errors)


def test_validate_dependency_cfg_requires_guidance_true():
    # guidance explicitly off, cfg supplied anyway -> impossible combination rejected
    result = pr.validate("ace_step", mc.TASK_MUSIC_GENERATE, {"guidance": False, "cfg": 9.0})
    assert result.ok is False
    assert any(e.field == "cfg" and e.code == pr.ERR_DEPENDENCY_UNMET for e in result.errors)

    # guidance true (explicit): cfg is accepted
    ok_result = pr.validate("ace_step", mc.TASK_MUSIC_GENERATE, {"guidance": True, "cfg": 9.0})
    assert ok_result.ok is True
    assert ok_result.normalized["cfg"] == 9.0


def test_validate_dependency_strength_requires_image_present():
    # strength given but no image at all: rejected before submit
    result = pr.validate("invoke", mc.TASK_IMAGE_EDIT, {"prompt": "x", "strength": 0.8})
    assert result.ok is False
    codes = {(e.field, e.code) for e in result.errors}
    assert ("image", pr.ERR_MISSING_REQUIRED) in codes
    assert ("strength", pr.ERR_DEPENDENCY_UNMET) in codes

    # image present: strength is now valid
    ok_result = pr.validate("invoke", mc.TASK_IMAGE_EDIT, {"image": "art-1", "prompt": "x", "strength": 0.8})
    assert ok_result.ok is True
    assert ok_result.normalized["strength"] == 0.8


def test_validate_a_fully_valid_payload_normalizes_defaults():
    result = pr.validate("realesrgan", mc.TASK_IMAGE_UPSCALE, {"image": "art-1"})
    assert result.ok is True
    assert result.normalized["scale"] == 2  # default applied
    assert result.normalized["face_enhance"] is False


def test_validate_wrong_type_is_reported():
    result = pr.validate("ace_step", mc.TASK_MUSIC_GENERATE, {"duration_s": "sixty"})
    assert result.ok is False
    assert any(e.field == "duration_s" and e.code == pr.ERR_WRONG_TYPE for e in result.errors)


# ── routes/creator_capability_routes.py ─────────────────────────────────────

USER = {"x-user": "alice"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(odysseus_settings, "get_setting",
                        lambda k, default=None: True if k == "creator_enabled" else default)

    app = FastAPI()
    app.include_router(ccr.setup_creator_capability_routes())
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


def test_route_list_models_empty_then_populated(app_client):
    client, store = app_client
    r = client.get("/api/creator/capabilities/models", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["deployments"] == []

    _make_deployment(store, "route1")
    r2 = client.get("/api/creator/capabilities/models", headers=USER)
    assert r2.status_code == 200
    assert len(r2.json()["deployments"]) == 1


def test_route_capability_profile_404_for_unknown_deployment(app_client):
    client, _ = app_client
    r = client.get("/api/creator/capabilities/does-not-exist", headers=USER)
    assert r.status_code == 404


def test_route_capability_profile_returns_all_axes(app_client):
    client, store = app_client
    dep_id = _make_deployment(store, "route2")
    r = client.get(f"/api/creator/capabilities/{dep_id}", headers=USER)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["deployment_id"] == dep_id
    assert set(data["axes"]) == set(cap.AXES)


def test_route_param_schema_and_validate(app_client):
    client, _ = app_client
    r = client.get("/api/creator/params", params={"engine": "invoke", "task": mc.TASK_IMAGE_EDIT}, headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["schema"]["engine"] == "invoke"

    r2 = client.get("/api/creator/params", params={"engine": "nope", "task": "nope"}, headers=USER)
    assert r2.status_code == 404

    r3 = client.post("/api/creator/params/validate",
                     json={"engine": "invoke", "task": mc.TASK_IMAGE_EDIT, "params": {"prompt": "x"}},
                     headers=USER)
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["ok"] is False
    assert any(e["field"] == "image" for e in body["errors"])


def test_routes_404_when_creator_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(odysseus_settings, "get_setting", lambda k, default=None: default)

    app = FastAPI()
    app.include_router(ccr.setup_creator_capability_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/creator/capabilities/models", headers=USER).status_code == 404
    assert client.get("/api/creator/capabilities/dep-1", headers=USER).status_code == 404
    assert client.get("/api/creator/params", params={"engine": "invoke", "task": "x"}, headers=USER).status_code == 404
    assert client.post("/api/creator/params/validate", json={"engine": "invoke", "task": "x", "params": {}},
                       headers=USER).status_code == 404
