"""WP08 — Model Explorer y comparación (UX11, MOD07, MOD10, MOD11, MOD12,
MOD16, MOD17).

Covers `src/creator/model_explorer.py` and
`routes/creator_model_explorer_routes.py` through a real FastAPI app +
TestClient, real sqlite stores in tmp_path, and fakes only for GPU/network
reads (`gpu_policy.model_sizes`, `gpu_shared_memory.vram_snapshot`) — never a
real model load or a real card probe.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from src import model_calibration as calib
from src import model_capabilities as mc
from src import model_identity as mi
from src import settings as faustus_settings
from src.creator import capabilities as cap
from src.creator import model_explorer as me
import routes.creator_model_explorer_routes as cmer


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def identity_store(tmp_path):
    return mi.ModelIdentityStore(db_path=str(tmp_path / "model_identity.db"))


def _make_deployment(
    store: mi.ModelIdentityStore, suffix: str = "a", *, engine_kind: str = "ollama",
    endpoint_url: str = "http://127.0.0.1:11434",
) -> str:
    spec = mi.ModelSpec(
        model_spec_id=f"ollama:digest:spec-{suffix}", vendor="ollama", family="qwen3",
        model_id=f"qwen3:{suffix}", aliases=(f"qwen3:{suffix}",), digest=f"sha256:{suffix}",
        parameter_size="8B", quantization="Q4_K_M", license="Apache-2.0",
        modalities_in=("text",), modalities_out=("text",), context_tokens=8192,
    )
    store.upsert_model_spec(spec)
    manifest = mi.DeploymentManifest(
        deployment_id=f"dep-{suffix}", model_spec_id=spec.model_spec_id, weights_revision=f"sha256:{suffix}",
        engine=mi.EngineInfo(kind=engine_kind, version="0.9.0"), endpoint_id=f"ep-{suffix}",
        endpoint_url=endpoint_url,
    )
    store.upsert_deployment(manifest)
    return manifest.deployment_id


# ── src/creator/model_explorer.py: aggregation ──────────────────────────────

def test_entry_for_unknown_deployment_is_none(identity_store):
    assert me.entry_for_deployment("does-not-exist", store=identity_store) is None


def test_entry_aggregates_spec_deployment_and_capability_profile(identity_store):
    dep_id = _make_deployment(identity_store, "a")
    cap.record_evidence(deployment_id=dep_id, axis=cap.AXIS_VISION_IN, level=mi.LEVEL_MEASURED,
                         source="probe", store=identity_store)
    entry = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry is not None
    assert entry.deployment_id == dep_id
    assert entry.model_spec["vendor"] == "ollama"
    assert entry.deployment["deployment_id"] == dep_id
    assert entry.capability_profile["axes"][cap.AXIS_VISION_IN]["status"] == cap.STATUS_KNOWN
    # never derives image generation from vision (WP07's own rule, still true through this aggregator)
    assert entry.capability_profile["axes"][cap.AXIS_IMAGE_OUT]["status"] == cap.STATUS_UNKNOWN


def test_entry_footprint_is_unknown_for_non_ollama_engine(identity_store):
    dep_id = _make_deployment(identity_store, "b", engine_kind="comfyui", endpoint_url="http://127.0.0.1:8188")
    entry = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry.footprint["basis"] == me.BASIS_UNKNOWN
    assert entry.footprint["size_bytes"] == 0
    assert entry.footprint["reason"]


def test_entry_footprint_estimated_with_fake_gpu_policy_and_vram(identity_store, monkeypatch):
    dep_id = _make_deployment(identity_store, "c")

    fake_sizes = {"qwen3:c": 4 * 1024 ** 3}
    monkeypatch.setattr("src.gpu_policy.model_sizes", lambda base, timeout=3.0: fake_sizes)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: {
        "supported": True, "name": "Fake GPU", "total": 24 * 1024 ** 3, "used": 0,
        "free": 24 * 1024 ** 3, "count": 1, "gpus": [{"index": 0, "name": "Fake GPU",
                                                        "total": 24 * 1024 ** 3, "used": 0, "free": 24 * 1024 ** 3}],
    })

    entry = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry.footprint["basis"] == me.BASIS_ESTIMATED
    assert entry.footprint["size_bytes"] == 4 * 1024 ** 3
    assert entry.footprint["vram_state"] == "fits"


def test_entry_footprint_unknown_when_no_gpu(identity_store, monkeypatch):
    dep_id = _make_deployment(identity_store, "d")
    fake_sizes = {"qwen3:d": 4 * 1024 ** 3}
    monkeypatch.setattr("src.gpu_policy.model_sizes", lambda base, timeout=3.0: fake_sizes)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: {"supported": False, "reason": "no nvidia-smi"})

    entry = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry.footprint["size_bytes"] == 4 * 1024 ** 3
    assert entry.footprint["basis"] == me.BASIS_ESTIMATED
    assert entry.footprint["vram_state"] == ""
    assert "no nvidia-smi" in entry.footprint["reason"]


def test_legacy_calibration_present_when_on_file(identity_store, tmp_path, monkeypatch):
    monkeypatch.setattr(calib, "_default_data_dir", lambda: str(tmp_path))
    dep_id = _make_deployment(identity_store, "e")
    entry_before = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry_before.legacy_calibration is None

    key = calib.manifest_key(vendor="ollama", model_id="qwen3:e", digest="sha256:e")
    calib.save_announced(key, {"vendor": "ollama", "model_id": "qwen3:e", "capabilities": {"tools": True}})

    entry_after = me.entry_for_deployment(dep_id, store=identity_store)
    assert entry_after.legacy_calibration is not None
    assert entry_after.legacy_calibration["manifest_key"] == key
    assert entry_after.legacy_calibration["announced"]["capabilities"]["tools"] is True


def test_param_schemas_for_engine_ollama_is_empty_media_engine_is_not():
    assert me.param_schemas_for_engine("ollama") == []
    schemas = me.param_schemas_for_engine("ace_step")
    assert any(s["task"] == mc.TASK_MUSIC_GENERATE for s in schemas)


def test_list_entries_is_compact_and_filterable(identity_store):
    _make_deployment(identity_store, "f")
    rows = me.list_entries(store=identity_store)
    assert len(rows) == 1
    row = rows[0]
    assert row["vendor"] == "ollama"
    assert row["context_tokens"] == 8192
    assert "known_axes" in row and "announced_axes" in row


# ── compare(): never invents ────────────────────────────────────────────────

def test_compare_marks_unknown_deployment_ids(identity_store):
    dep_id = _make_deployment(identity_store, "g")
    table = me.compare([dep_id, "ghost-dep"], mc.TASK_CHAT_COMPLETIONS, store=identity_store)
    assert table.unknown_deployment_ids == ("ghost-dep",)
    for row in table.rows:
        assert row.cells["ghost-dep"].basis == me.BASIS_UNKNOWN
        assert row.cells["ghost-dep"].value is None


def test_compare_never_equates_missing_field_with_zero_or_false(identity_store):
    dep_id = _make_deployment(identity_store, "h")
    table = me.compare([dep_id], mc.TASK_IMAGE_GENERATE, store=identity_store)
    row_by_field = {r.field: r for r in table.rows}
    # no capability evidence recorded at all: task_support is unknown, not "unsupported"
    assert row_by_field["task_support"].cells[dep_id].basis == me.BASIS_UNKNOWN
    assert row_by_field["task_support"].cells[dep_id].value == me.BASIS_UNKNOWN
    # no footprint resolved (no GPU fake wired in this test): unknown, not 0 bytes
    assert row_by_field["footprint_bytes"].cells[dep_id].basis in (me.BASIS_UNKNOWN, me.BASIS_ESTIMATED)


def test_compare_capability_status_reflects_real_evidence(identity_store):
    dep_measured = _make_deployment(identity_store, "i")
    dep_bare = _make_deployment(identity_store, "j")
    cap.record_evidence(deployment_id=dep_measured, axis=cap.AXIS_IMAGE_OUT, level=mi.LEVEL_MEASURED,
                         source="probe", store=identity_store)

    table = me.compare([dep_measured, dep_bare], mc.TASK_IMAGE_GENERATE, store=identity_store)
    row = next(r for r in table.rows if r.field == "task_support")
    assert row.cells[dep_measured].value == cap.STATUS_KNOWN
    assert row.cells[dep_bare].value == cap.STATUS_UNKNOWN


def test_compare_does_not_equate_estimated_footprint_with_measured(identity_store, monkeypatch):
    dep_id = _make_deployment(identity_store, "k")
    monkeypatch.setattr("src.gpu_policy.model_sizes", lambda base, timeout=3.0: {"qwen3:k": 2 * 1024 ** 3})
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: {
        "supported": True, "name": "Fake GPU", "total": 8 * 1024 ** 3, "used": 0,
        "free": 8 * 1024 ** 3, "count": 1, "gpus": [{"index": 0, "name": "Fake GPU",
                                                       "total": 8 * 1024 ** 3, "used": 0, "free": 8 * 1024 ** 3}],
    })
    table = me.compare([dep_id], mc.TASK_CHAT_COMPLETIONS, store=identity_store)
    row = next(r for r in table.rows if r.field == "footprint_bytes")
    assert row.cells[dep_id].basis == me.BASIS_ESTIMATED
    assert row.cells[dep_id].basis != me.BASIS_MEASURED


def test_axis_for_task_is_partial_by_design():
    assert me.axis_for_task(mc.TASK_IMAGE_EDIT) == cap.AXIS_IMAGE_EDIT
    assert me.axis_for_task(mc.TASK_EMBEDDINGS_CREATE) is None


# ── routes/creator_model_explorer_routes.py ─────────────────────────────────

USER = {"x-user": "alice"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(faustus_settings, "get_setting",
                        lambda k, default=None: True if k == "creator_enabled" else default)

    app = FastAPI()
    app.include_router(cmer.setup_creator_model_explorer_routes())
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


def test_route_list_explorer_empty_then_populated(app_client):
    client, store = app_client
    r = client.get("/api/creator/models/explorer", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["deployments"] == []

    _make_deployment(store, "route1")
    r2 = client.get("/api/creator/models/explorer", headers=USER)
    assert r2.status_code == 200
    assert len(r2.json()["deployments"]) == 1


def test_route_explorer_entry_404_for_unknown_deployment(app_client):
    client, _ = app_client
    r = client.get("/api/creator/models/explorer/does-not-exist", headers=USER)
    assert r.status_code == 404


def test_route_explorer_entry_returns_full_ficha(app_client):
    client, store = app_client
    dep_id = _make_deployment(store, "route2")
    r = client.get(f"/api/creator/models/explorer/{dep_id}", headers=USER)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["deployment_id"] == dep_id
    assert data["model_spec"]["vendor"] == "ollama"
    assert "capability_profile" in data
    assert "footprint" in data


def test_route_compare_requires_two_to_three_ids_and_task(app_client):
    client, store = app_client
    dep_id = _make_deployment(store, "route3")

    r_too_few = client.post("/api/creator/models/compare",
                            json={"deployment_ids": [dep_id], "task": mc.TASK_CHAT_COMPLETIONS}, headers=USER)
    assert r_too_few.status_code == 400

    r_no_task = client.post("/api/creator/models/compare",
                            json={"deployment_ids": [dep_id, "dep-x"]}, headers=USER)
    assert r_no_task.status_code == 400

    r_ok = client.post("/api/creator/models/compare",
                       json={"deployment_ids": [dep_id, "dep-ghost"], "task": mc.TASK_CHAT_COMPLETIONS}, headers=USER)
    assert r_ok.status_code == 200, r_ok.text
    body = r_ok.json()
    assert body["unknown_deployment_ids"] == ["dep-ghost"]
    for row in body["rows"]:
        assert row["cells"]["dep-ghost"]["basis"] == "unknown"


def test_route_compare_marks_unknown_never_zero(app_client):
    client, store = app_client
    d1 = _make_deployment(store, "route4a")
    d2 = _make_deployment(store, "route4b")
    r = client.post("/api/creator/models/compare",
                    json={"deployment_ids": [d1, d2], "task": mc.TASK_IMAGE_GENERATE}, headers=USER)
    assert r.status_code == 200, r.text
    body = r.json()
    task_row = next(row for row in body["rows"] if row["field"] == "task_support")
    for dep_id in (d1, d2):
        cell = task_row["cells"][dep_id]
        assert cell["value"] == "unknown"
        assert cell["basis"] == "unknown"


def test_routes_404_when_creator_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "model_identity.db"
    test_store = mi.ModelIdentityStore(db_path=str(db_path))
    monkeypatch.setattr(mi, "default_store", lambda: test_store)
    monkeypatch.setattr(faustus_settings, "get_setting", lambda k, default=None: default)

    app = FastAPI()
    app.include_router(cmer.setup_creator_model_explorer_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/creator/models/explorer", headers=USER).status_code == 404
    assert client.get("/api/creator/models/explorer/dep-1", headers=USER).status_code == 404
    assert client.post("/api/creator/models/compare",
                       json={"deployment_ids": ["a", "b"], "task": "x"}, headers=USER).status_code == 404
