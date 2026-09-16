"""WP10 — the AdapterPort contract, ComfyUI/ffmpeg adapters, the pkgutil
registry, production_runs' MediaRun projection, and the adapter routes.

Real sqlite for artifact identity / approvals / media runs (core.database,
own engine, same pattern as tests/test_creator_preflight.py), a real ffmpeg
subprocess for the composition tests (skipped if not on PATH), and a FAKE
`src.media_runs` for the ComfyUI adapter unit tests — no real ComfyUI engine
is started or asserted ready by a mock (CONTRATO.md rule 11: "no afirmar
éxito de motores por un mock").
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from services.projects import ProjectStore
from src import approval_store, budget_account
from src.artifact_identity import ensure_blob, record_occurrence
from src.contracts.blob import ArtifactOccurrence
from src.creator import adapter_port as port
from src.creator import adapters as adapters_pkg
from src.creator import production_runs
from src.creator.adapters.base import stage_inputs
from src.creator.adapters.comfyui import ComfyUIAdapter
from src.creator.adapters.ffmpeg import FfmpegAdapter

OWNER = "alice"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ── shared fixtures (same shape as tests/test_creator_preflight.py) ───────

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "creator_wp10.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def own_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.sqlite3")


@pytest.fixture()
def own_artifact_store(tmp_path, monkeypatch):
    """Same pattern as tests/test_creator_wp03.py: real files, but never
    the repo's own `data/artifacts/store`."""
    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "artifact_store"))
    return artifact_store


def _make_input(owner: str = OWNER, occurrence_id: str = "occ_in1",
                *, path: str = "") -> str:
    """Records an occurrence AND (if `path` is given) makes it resolvable
    through the real artifact store, so `stage_inputs()` can actually
    materialise it."""
    if path:
        from src import artifact_store
        sha, size, filename, _ = artifact_store.publish_copy(
            path, artifact_store.ARTIFACT_STORE_DIR, os.path.basename(path))
        ensure_blob(sha256=sha, byte_size=size, filename=filename, media_type="video/mp4")
    else:
        sha = "a" * 64
        ensure_blob(sha256=sha, byte_size=10, filename="in.bin", media_type="application/octet-stream")
    occ = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "video" if path else "document",
        "blob_sha256": sha, "owner": owner,
    })
    record_occurrence(occ)
    return occurrence_id


@pytest.fixture()
def sample_clip(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    out = str(tmp_path / "clip.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
        "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out,
    ], check=True, capture_output=True)
    return out


# ═══════════════════════════════════════════════════════════════════════
# adapter_port — the dataclasses themselves
# ═══════════════════════════════════════════════════════════════════════

def test_submit_result_rejects_unknown_state():
    with pytest.raises(ValueError):
        port.SubmitResult(job_id="x", state="queued")  # not in SUBMIT_STATES


def test_cancel_result_rejects_unknown_outcome():
    with pytest.raises(ValueError):
        port.CancelResult(job_id="x", outcome="cancelled")  # not in CANCEL_OUTCOMES


def test_status_result_rejects_unknown_state():
    with pytest.raises(ValueError):
        port.StatusResult(job_id="x", state="accepted")  # not a STATUS_STATES value


# ═══════════════════════════════════════════════════════════════════════
# adapters.registry() — pkgutil discovery, "nada de listas que olvidar"
# ═══════════════════════════════════════════════════════════════════════

def test_registry_finds_the_two_shipped_adapters():
    reg = adapters_pkg.registry()
    assert set(reg) >= {"comfyui", "ffmpeg"}
    assert "base" not in reg  # base.py has no ADAPTER_FACTORY and is explicitly skipped


def test_registry_discovers_a_third_adapter_module_added_in_tmp(tmp_path, monkeypatch):
    """The exact closing criterion: drop a NEW module into a directory the
    package's own `__path__` is extended with, and `registry()` — with no
    list anywhere updated — finds it."""
    extra_dir = tmp_path / "extra_adapters"
    extra_dir.mkdir()
    (extra_dir / "probe.py").write_text(textwrap.dedent('''
        from src.creator.adapter_port import AdapterManifest, AdapterPlan, StatusResult, CancelResult, CollectResult, SubmitResult

        class ProbeAdapter:
            name = "probe"
            def describe(self):
                return AdapterManifest(name="probe", engine="probe", version="0",
                                        tasks=("noop",), supports_reconcile=False,
                                        supports_cancel=False)
            def plan(self, op, params, inputs):
                return AdapterPlan(ok=True, adapter="probe", task=op)
            def submit(self, plan, staging):
                return SubmitResult(job_id="probe_1", state="accepted")
            def status(self, job_id):
                return StatusResult(job_id=job_id, state="completed")
            def cancel(self, job_id):
                return CancelResult(job_id=job_id, outcome="too_late")
            def collect(self, job_id, tmp):
                return CollectResult(ok=True)
            def reconcile(self, job_id):
                return self.status(job_id)

        ADAPTER_FACTORY = ProbeAdapter
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    adapters_pkg.__path__.append(str(extra_dir))
    try:
        reg = adapters_pkg.registry()
        assert "probe" in reg
        instance = reg["probe"]()
        assert instance.describe().name == "probe"
    finally:
        adapters_pkg.__path__.remove(str(extra_dir))
        sys.modules.pop("probe", None)


def test_registry_get_unknown_name_names_what_is_registered():
    with pytest.raises(KeyError, match="comfyui"):
        adapters_pkg.get("no-such-adapter")


# ═══════════════════════════════════════════════════════════════════════
# ComfyUI adapter — wraps media_runs with a FAKE backend (no real engine)
# ═══════════════════════════════════════════════════════════════════════

class _FakeMediaRuns:
    """Stands in for `src.media_runs` — enough of its surface for the
    adapter's mapping logic, not a claim that a real ComfyUI ran anything."""

    def __init__(self):
        self.rows = {}
        self.cancel_calls = []

    def plan(self, workflow_id, inputs, check_engine=True):
        if workflow_id == "missing_model":
            return {"ok": False, "reason": "missing_requirements",
                    "detail": "no checkpoint", "missing": {"nodes": [], "models": ["sdxl.safetensors"]}}
        return {"ok": True, "workflow": workflow_id, "models": [],
                "engine": {"url": "http://fake:8188"}}

    def start(self, workflow_id, inputs, *, engine_url="", owner="", project_id=""):
        run_id = f"mrun_{workflow_id}"
        outcome = self.rows.get(workflow_id, {"status": "queued"})
        self.rows[run_id] = {**outcome, "id": run_id}
        return {"ok": outcome["status"] == "queued", "run_id": run_id,
                "status": outcome["status"], "reason": outcome.get("reason", ""),
                "detail": outcome.get("detail", "")}

    def get(self, run_id):
        return self.rows.get(run_id)

    def poll(self, run_id, collect=True):
        row = self.rows.get(run_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, "status": row["status"], "reason": row.get("reason", ""),
                "artifacts": row.get("artifacts", [])}

    def cancel(self, run_id):
        self.cancel_calls.append(run_id)
        row = self.rows.get(run_id) or {}
        if row.get("status") == "completed":
            return {"ok": False, "reason": "already_completed"}
        row["status"] = "cancelled"
        return {"ok": True, "status": "cancelled", "detail": "cancelled while running"}

    def reconcile_run(self, run_id, grace_seconds=0):
        return {"ok": True, "run_id": run_id, "changed": False}


@pytest.fixture()
def fake_media_runs(monkeypatch):
    fake = _FakeMediaRuns()
    monkeypatch.setattr("src.media_runs.plan", fake.plan)
    monkeypatch.setattr("src.media_runs.start", fake.start)
    monkeypatch.setattr("src.media_runs.get", fake.get)
    monkeypatch.setattr("src.media_runs.poll", fake.poll)
    monkeypatch.setattr("src.media_runs.cancel", fake.cancel)
    monkeypatch.setattr("src.media_runs.reconcile_run", fake.reconcile_run)
    return fake


def _staging(owner="alice", project_id="p1", workdir="/tmp"):
    return port.Staging(owner=owner, project_id=project_id, workdir=workdir, input_paths={})


def test_comfyui_plan_reflects_media_runs_plan(fake_media_runs):
    adapter = ComfyUIAdapter()
    ok_plan = adapter.plan("sdxl_txt2img", {"prompt": "a cat"}, [])
    assert ok_plan.ok is True

    bad_plan = adapter.plan("missing_model", {}, [])
    assert bad_plan.ok is False
    assert "sdxl.safetensors" in bad_plan.missing


def test_comfyui_submit_maps_queued_to_accepted(fake_media_runs):
    adapter = ComfyUIAdapter()
    fake_media_runs.rows["queued_wf"] = {"status": "queued"}
    plan = adapter.plan("queued_wf", {}, [])
    result = adapter.submit(plan, _staging())
    assert result.state == "accepted"
    assert result.job_id == "mrun_queued_wf"


def test_comfyui_submit_maps_submit_unknown_to_accepted_uncertain(fake_media_runs):
    """The ficha's acceptance criterion in the adapter's own vocabulary:
    a crash-shaped outcome between POST and response is `submit_unknown` in
    `media_runs`, and the adapter reports it as `accepted_uncertain` —
    never silently promoted to a plain `accepted`."""
    adapter = ComfyUIAdapter()
    fake_media_runs.rows["flaky_wf"] = {"status": "submit_unknown", "reason": "http_timeout"}
    plan = adapter.plan("flaky_wf", {}, [])
    result = adapter.submit(plan, _staging())
    assert result.state == "accepted_uncertain"


def test_comfyui_submit_maps_engine_refusal_to_rejected_before_queue(fake_media_runs):
    adapter = ComfyUIAdapter()
    fake_media_runs.rows["bad_wf"] = {"status": "failed", "reason": "rejected_by_engine: bad node"}
    plan = adapter.plan("bad_wf", {}, [])
    result = adapter.submit(plan, _staging())
    assert result.state == "rejected_before_queue"


def test_comfyui_cancel_after_completion_is_too_late(fake_media_runs):
    """RES08 in this adapter's vocabulary: a result that landed first (the
    render completed) is not overwritten by a later cancel — the cancel is
    told it is too late instead."""
    adapter = ComfyUIAdapter()
    fake_media_runs.rows["done_wf"] = {"status": "queued"}
    started = adapter.submit(adapter.plan("done_wf", {}, []), _staging())
    fake_media_runs.rows[started.job_id]["status"] = "completed"
    outcome = adapter.cancel(started.job_id)
    assert outcome.outcome == "too_late"


def test_comfyui_cancel_unknown_job(fake_media_runs):
    adapter = ComfyUIAdapter()
    assert adapter.cancel("mrun_nope").outcome == "unknown"


def test_comfyui_reconcile_delegates_and_reads_status(fake_media_runs):
    adapter = ComfyUIAdapter()
    fake_media_runs.rows["r_wf"] = {"status": "running"}
    started = adapter.submit(adapter.plan("r_wf", {}, []), _staging())
    result = adapter.reconcile(started.job_id)
    assert result.state == "running"


# ═══════════════════════════════════════════════════════════════════════
# ffmpeg adapter — describe()/plan() unavailable when the binary is missing
# ═══════════════════════════════════════════════════════════════════════

def test_ffmpeg_describe_reports_unavailable_without_the_binary(monkeypatch):
    monkeypatch.setattr("src.creator.adapters.ffmpeg.shutil.which", lambda name: "")
    adapter = FfmpegAdapter()
    manifest = adapter.describe()
    assert manifest.available is False
    assert "not found" in manifest.reason
    assert manifest.supports_reconcile is False


def test_ffmpeg_submit_without_binary_is_rejected_before_queue(monkeypatch):
    monkeypatch.setattr("src.creator.adapters.ffmpeg.shutil.which", lambda name: "")
    adapter = FfmpegAdapter()
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 1}, ["occ_1"])
    assert plan.ok is False
    assert "ffmpeg_binary" in plan.missing

    result = adapter.submit(plan, _staging())
    assert result.state == "rejected_before_queue"
    assert result.reason in ("invalid_plan",)


def test_ffmpeg_plan_rejects_unsupported_task():
    adapter = FfmpegAdapter()
    plan = adapter.plan("melt", {}, [])
    assert plan.ok is False
    assert "params" in plan.missing


def test_ffmpeg_plan_rejects_wrong_input_count_for_trim():
    adapter = FfmpegAdapter()
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 1}, ["a", "b"])
    assert plan.ok is False


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_ffmpeg_trim_runs_for_real_and_collect_validates_it(sample_clip, tmp_path):
    adapter = FfmpegAdapter()
    manifest = adapter.describe()
    assert manifest.available is True

    staging = port.Staging(owner=OWNER, project_id="p1", workdir=str(tmp_path / "stage"),
                            input_paths={"occ_clip": sample_clip})
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 0.5}, ["occ_clip"])
    assert plan.ok is True

    result = adapter.submit(plan, staging)
    assert result.state == "accepted"
    assert adapter.status(result.job_id).state == "completed"

    collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
    assert collected.ok is True
    assert len(collected.outputs) == 1
    output = collected.outputs[0]
    assert output.valid is True
    assert len(output.sha256) == 64
    assert output.byte_size > 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_ffmpeg_cancel_after_synchronous_completion_is_too_late(sample_clip, tmp_path):
    """Closing criterion: "cancel tardío → too_late". `submit()` is
    synchronous (see the adapter's module docstring), so by the time a
    caller can call `cancel()` the render has already produced its result —
    exactly the "resultado tardío no vence cancelación" case from the other
    direction: the late CANCEL does not undo an already-landed result."""
    adapter = FfmpegAdapter()
    staging = port.Staging(owner=OWNER, project_id="p1", workdir=str(tmp_path / "stage"),
                            input_paths={"occ_clip": sample_clip})
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 0.5}, ["occ_clip"])
    result = adapter.submit(plan, staging)
    assert result.state == "accepted"

    outcome = adapter.cancel(result.job_id)
    assert outcome.outcome == "too_late"
    # And the completed output is still there, untouched by the late cancel.
    assert adapter.status(result.job_id).state == "completed"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_ffmpeg_collect_rejects_a_corrupt_output_without_registering(sample_clip, tmp_path, monkeypatch):
    """Closing criterion: "collect valida hash y rechaza salida corrupta sin
    registrar." Truncate the produced file after a real ffmpeg run so
    ffprobe genuinely fails on it, and check `collect()` refuses it."""
    adapter = FfmpegAdapter()
    staging = port.Staging(owner=OWNER, project_id="p1", workdir=str(tmp_path / "stage"),
                            input_paths={"occ_clip": sample_clip})
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 0.5}, ["occ_clip"])
    result = adapter.submit(plan, staging)
    assert result.state == "accepted"

    # Corrupt the real output ffmpeg just wrote, in place, before collect().
    from src.creator.adapters import ffmpeg as ffmpeg_mod
    output_path = ffmpeg_mod._JOBS[result.job_id]["output_path"]
    with open(output_path, "r+b") as fh:
        fh.truncate(16)  # a 16-byte "mp4" is not a video ffprobe can read

    collected = adapter.collect(result.job_id, str(tmp_path / "collected"))
    assert collected.ok is False
    assert collected.outputs and collected.outputs[0].valid is False

    # production_runs must not register anything from a failed collect.
    settled = production_runs.link_outputs(
        result.job_id, adapter_name="ffmpeg", collected=collected,
        owner=OWNER, project_id="p1", op="trim")
    assert settled.get("artifacts") is None


# ═══════════════════════════════════════════════════════════════════════
# base.stage_inputs — the only door from an occurrence id to a path
# ═══════════════════════════════════════════════════════════════════════

def test_stage_inputs_materialises_owned_occurrences(own_database, own_artifact_store, tmp_path, sample_clip):
    occ_id = _make_input(OWNER, "occ_stage1", path=sample_clip)
    staging = stage_inputs(owner=OWNER, project_id="p1", occurrence_ids=[occ_id],
                            workdir=str(tmp_path / "stage"))
    assert occ_id in staging.input_paths
    assert os.path.isfile(staging.input_paths[occ_id])


def test_stage_inputs_refuses_someone_elses_occurrence(own_database, own_artifact_store, tmp_path, sample_clip):
    occ_id = _make_input("bob", "occ_stage2", path=sample_clip)
    from src.artifact_identity import NotTheOwner
    with pytest.raises(NotTheOwner):
        stage_inputs(owner=OWNER, project_id="p1", occurrence_ids=[occ_id],
                     workdir=str(tmp_path / "stage"))


# ═══════════════════════════════════════════════════════════════════════
# production_runs — MediaRun projection for a NON-media_runs adapter
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_production_runs_records_and_completes_an_ffmpeg_run(own_database, own_artifact_store, tmp_path, sample_clip):
    occ_id = _make_input(OWNER, "occ_prod1", path=sample_clip)
    staging = stage_inputs(owner=OWNER, project_id="p1", occurrence_ids=[occ_id],
                           workdir=str(tmp_path / "stage"))
    adapter = FfmpegAdapter()
    plan = adapter.plan("trim", {"start_seconds": 0, "duration_seconds": 0.5}, [occ_id])
    submitted = adapter.submit(plan, staging)
    assert submitted.state == "accepted"

    row = production_runs.record_submit(
        adapter_name="ffmpeg", engine="ffmpeg", op="trim", job_id=submitted.job_id,
        submit_state=submitted.state, owner=OWNER, project_id="p1", values={})
    assert row["status"] == "queued"

    collected = adapter.collect(submitted.job_id, str(tmp_path / "collected"))
    assert collected.ok is True
    settled = production_runs.link_outputs(
        submitted.job_id, adapter_name="ffmpeg", collected=collected,
        owner=OWNER, project_id="p1", op="trim", input_occurrence_ids=[occ_id])
    assert settled["status"] == "completed"
    assert len(settled["artifacts"]) == 1

    # It reuses the SAME MediaRun row/authority (ADR-02) — visible through
    # src.media_runs.get(), not a second table.
    from src import media_runs
    reread = media_runs.get(submitted.job_id)
    assert reread is not None
    assert reread["status"] == "completed"
    assert len(reread["artifact_ids"]) == 1

    # And the produced occurrence really is a derived_from child of the input.
    from src.creator import lineage as lineage_mod
    parents = lineage_mod.direct_parents(OWNER, reread["artifact_ids"][0])
    assert any(p["parent_occurrence"] == occ_id for p in parents)


def test_production_runs_never_overwrites_a_settled_run(own_database):
    row = production_runs.record_submit(
        adapter_name="ffmpeg", engine="ffmpeg", op="trim", job_id="ffjob_settled",
        submit_state="rejected_before_queue", owner=OWNER, project_id="p1", values={},
        reason="ffmpeg_unavailable")
    assert row["status"] == "failed"
    again = production_runs.mark_terminal("ffjob_settled", adapter_name="ffmpeg",
                                          status="completed", reason="should not apply")
    assert again["status"] == "failed"  # the first, real terminal status wins


# ═══════════════════════════════════════════════════════════════════════
# routes — GET /api/creator/adapters, plan, submit (403 / 409)
# ═══════════════════════════════════════════════════════════════════════

def _stub_capabilities(monkeypatch, caps):
    from src.creator import preflight as pf
    monkeypatch.setattr(pf, "_capabilities_for", lambda deployment_id: caps)


def _stub_params_ok(monkeypatch):
    from src.creator import preflight as pf
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: True)


@pytest.fixture()
def route_client(tmp_path, monkeypatch, own_database, own_budget, own_artifact_store):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    _stub_capabilities(monkeypatch, {
        "operations": {"trim": {"requires_consent": False, "consent_subject_param": ""}},
        "is_cloud": False, "weights_bytes": None, "model_info": None,
        "price_usd_per_1k_tokens": None,
    })
    _stub_params_ok(monkeypatch)

    from routes.creator_adapter_routes import setup_creator_adapter_routes
    from routes.creator_preflight_routes import setup_creator_preflight_routes
    app = FastAPI()
    app.include_router(setup_creator_adapter_routes())
    app.include_router(setup_creator_preflight_routes())
    client = TestClient(app)
    project = ps.create("HTTP Proj", owner="__odysseus_local__", scaffold_memory=False)
    return client, project["id"]


def test_route_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_adapter_routes import setup_creator_adapter_routes
    app = FastAPI()
    app.include_router(setup_creator_adapter_routes())
    client = TestClient(app)
    assert client.get("/api/creator/adapters").status_code == 404


def test_route_lists_adapters(route_client):
    client, _project_id = route_client
    resp = client.get("/api/creator/adapters")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()["adapters"]}
    assert {"comfyui", "ffmpeg"} <= names


def test_route_submit_without_approval_is_403(route_client, monkeypatch):
    """Closing criterion: "submit sin aprobación → 403" — a budget policy
    that answers `ask` and no approval on file."""
    client, project_id = route_client
    from services.projects import get_store as get_project_store
    from src.creator import profile as profile_mod
    profile_mod.set_profile(get_project_store(), "__odysseus_local__", project_id,
                            {"budget_mode": "warn", "production_ceiling_tokens": 1})

    body = {"project_id": project_id, "op": "trim",
            "params": {"start_seconds": 0, "duration_seconds": 1, "estimated_tokens": 500},
            "inputs": []}
    resp = client.post("/api/creator/adapters/ffmpeg/submit", json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "approval_required"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_route_approve_then_submit_then_second_submit_is_409(route_client, sample_clip, tmp_path):
    """Closing criterion: "aprobación consumida dos veces → 409" — exercised
    through the real HTTP submit route, not just approval_store directly."""
    client, project_id = route_client
    from services.projects import get_store as get_project_store
    from src.creator import profile as profile_mod
    profile_mod.set_profile(get_project_store(), "__odysseus_local__", project_id,
                            {"budget_mode": "warn", "production_ceiling_tokens": 1})

    occ_id = _make_input("__odysseus_local__", "occ_route1", path=sample_clip)

    body = {"project_id": project_id, "op": "trim",
            "params": {"start_seconds": 0, "duration_seconds": 0.5, "estimated_tokens": 500},
            "inputs": [occ_id]}
    pre = client.post("/api/creator/preflight", json={
        **body, "operation": "trim", "engine": "ffmpeg", "deployment_id": "ffmpeg"})
    assert pre.status_code == 200
    digest = pre.json()["approval_digest"]
    assert digest

    approve = client.post(f"/api/creator/preflight/{digest}/approve", json={
        **body, "operation": "trim", "engine": "ffmpeg", "deployment_id": "ffmpeg"})
    assert approve.status_code == 200

    first = client.post("/api/creator/adapters/ffmpeg/submit",
                        json={**body, "preflight_digest": digest})
    assert first.status_code == 200, first.text
    assert first.json()["submit"]["state"] == "accepted"
    assert first.json()["run"]["status"] == "completed"

    second = client.post("/api/creator/adapters/ffmpeg/submit",
                         json={**body, "preflight_digest": digest})
    assert second.status_code == 409
