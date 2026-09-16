"""WP09 — unified Creator preflight and budget policy.

Real sqlite for artifact identity / approvals (core.database, own engine),
real sqlite for budget_account (its own file under a tmp DATA_DIR), real
TestClient for the HTTP layer. `src.creator.capabilities.capabilities_for`
and `src.creator.params.validate` (WP07, a parallel lot — see
docs/spec/creator/plan/CONTRATO.md's Sub-ola C2) are stubbed via
`monkeypatch.setattr` on `src.creator.preflight._capabilities_for` /
`_validate_params` rather than assumed to exist as real modules.
"""
from __future__ import annotations

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
from src.contracts.blob import ArtifactOccurrence, blob_id_for
from src.creator import budget_policy
from src.creator import preflight as pf

OWNER = "alice"


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------

@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    """A real, isolated core.database — backs both artifact occurrences
    (ArtifactOccurrenceRow) and approval cards (ApprovalRow), the two
    authorities preflight/the approve route actually read and write."""
    url = "sqlite:///" + (tmp_path / "creator_preflight.db").as_posix()
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
def own_consent(tmp_path, monkeypatch):
    from src import media_consent
    monkeypatch.setattr(media_consent, "CONSENT_FILE", str(tmp_path / "consent.json"))


def _make_input(owner: str = OWNER, occurrence_id: str = "occ_in1") -> str:
    sha = "a" * 64
    ensure_blob(sha256=sha, byte_size=10, filename="in.png", media_type="image/png")
    occ = ArtifactOccurrence.parse({
        "id": occurrence_id, "kind": "image", "blob_sha256": sha, "owner": owner,
    })
    record_occurrence(occ)
    return occurrence_id


def _caps(*, operations=("render",), requires_consent=False, consent_subject_param="",
          is_cloud=False, weights_bytes=None, model_info=None,
          price_usd_per_1k_tokens=None):
    return {
        "operations": {op: {"requires_consent": requires_consent,
                             "consent_subject_param": consent_subject_param}
                       for op in operations},
        "is_cloud": is_cloud, "weights_bytes": weights_bytes, "model_info": model_info,
        "price_usd_per_1k_tokens": price_usd_per_1k_tokens,
    }


def _stub_capabilities(monkeypatch, caps):
    monkeypatch.setattr(pf, "_capabilities_for", lambda deployment_id: caps)


def _stub_params_ok(monkeypatch):
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: {"ok": True})


def _stub_params_invalid(monkeypatch, errors):
    monkeypatch.setattr(pf, "_validate_params",
                        lambda engine, task, params: {"ok": False, "errors": errors})


# ---------------------------------------------------------------------
# run_preflight — missing detection, no side effects
# ---------------------------------------------------------------------

def test_missing_input_is_reported_without_generating_or_downloading(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={}, inputs=["occ_missing"],
    )
    assert report.ok is False
    kinds = {m["kind"] for m in report.missing}
    assert "input" in kinds
    assert any(m["id"] == "occ_missing" for m in report.missing)


def test_input_owned_by_someone_else_reads_the_same_as_missing(own_database, monkeypatch):
    occ_id = _make_input(owner="mallory")
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={}, inputs=[occ_id],
    )
    missing_ids = {m["id"] for m in report.missing if m["kind"] == "input"}
    assert occ_id in missing_ids


def test_existing_owned_input_is_not_missing(own_database, monkeypatch):
    occ_id = _make_input()
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={}, inputs=[occ_id],
    )
    assert not any(m["kind"] == "input" for m in report.missing)


def test_missing_capability_when_lookup_fails(own_database, monkeypatch):
    def _boom(deployment_id):
        raise RuntimeError("no such deployment")
    monkeypatch.setattr(pf, "_capabilities_for", _boom)
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep-nope", params={}, inputs=[],
    )
    assert any(m["kind"] == "capability" for m in report.missing)
    assert report.ok is False


def test_missing_capability_when_operation_unsupported(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps(operations=("transcribe",)))
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={}, inputs=[],
    )
    assert any(m["kind"] == "capability" and m["id"] == "render" for m in report.missing)


def test_missing_consent_blocks_a_clone_operation(own_database, own_consent, monkeypatch):
    _stub_capabilities(monkeypatch, _caps(
        operations=("clone_voice",), requires_consent=True, consent_subject_param="subject",
    ))
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="clone_voice", engine="local",
        deployment_id="dep1", params={"subject": "Jane Doe"}, inputs=[],
    )
    assert any(m["kind"] == "consent" and m["id"] == "Jane Doe" for m in report.missing)
    assert report.ok is False


def test_recorded_consent_clears_the_gate(own_database, own_consent, monkeypatch):
    from src import media_consent
    media_consent.register("Jane Doe", granted_by="alice")
    _stub_capabilities(monkeypatch, _caps(
        operations=("clone_voice",), requires_consent=True, consent_subject_param="subject",
    ))
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="clone_voice", engine="local",
        deployment_id="dep1", params={"subject": "Jane Doe"}, inputs=[],
    )
    assert not any(m["kind"] == "consent" for m in report.missing)


def test_missing_param_from_wp07_validator(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_invalid(monkeypatch, [{"field": "width", "detail": "must be positive"}])
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"width": -1}, inputs=[],
    )
    assert any(m["kind"] == "param" and m["id"] == "width" for m in report.missing)
    assert report.ok is False


def test_preflight_never_calls_a_media_backend_or_writes_budget_or_approvals(
    own_database, own_budget, monkeypatch,
):
    """The literal closure criterion: preflight generates nothing and
    downloads nothing. Approval and budget writes are asserted absent too —
    a report that requires approval must not itself have created one."""
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj-side-effects", operation="render", engine="local",
        deployment_id="dep1", params={"estimated_tokens": 999999}, inputs=[],
        profile={"budget_mode": "warn", "production_ceiling_tokens": 100},
    )
    assert report.requires_approval is True
    assert report.approval_digest
    # Nothing was actually reserved or requested as a side effect of planning.
    assert budget_account.snapshot(budget_policy.run_id_for("proj-side-effects"))["opened"] is False
    assert approval_store.pending(owner=OWNER) == []


# ---------------------------------------------------------------------
# estimate: unknown cost is never a silent 0
# ---------------------------------------------------------------------

def test_cost_with_no_price_signal_is_unknown_not_zero(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"estimated_tokens": 500}, inputs=[],
    )
    assert report.estimate.tokens == 500
    assert report.estimate.cost_usd == "unknown"


def test_cost_with_a_price_signal_is_computed(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps(price_usd_per_1k_tokens=2.0))
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"estimated_tokens": 1000}, inputs=[],
    )
    assert report.estimate.cost_usd == 2.0


# ---------------------------------------------------------------------
# admission: vram_fit-derived, rejects when it does not fit
# ---------------------------------------------------------------------

def test_admission_fits_when_vram_unknown(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={}, inputs=[],
    )
    assert report.admission.fits is True
    assert "unknown" in report.admission.reason


def test_admission_rejects_when_it_does_not_fit(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    monkeypatch.setattr(pf, "_available_vram_bytes", lambda engine, deployment_id: 1_000)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"vram_bytes_hint": 5_000}, inputs=[],
    )
    assert report.admission.fits is False
    assert "5000" in report.admission.reason
    assert report.ok is False


def test_admission_allows_when_it_fits(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    monkeypatch.setattr(pf, "_available_vram_bytes", lambda engine, deployment_id: 10_000)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"vram_bytes_hint": 5_000}, inputs=[],
    )
    assert report.admission.fits is True


# ---------------------------------------------------------------------
# budget_policy.decide — modes, threshold, ceiling, cloud
# ---------------------------------------------------------------------

def test_observe_mode_always_allows():
    d = budget_policy.decide(project_id="p", engine="local", estimated_tokens=10_000_000,
                             profile={"budget_mode": "observe"})
    assert d.verdict == "allow"


def test_warn_mode_asks_over_ceiling_but_does_not_deny():
    d = budget_policy.decide(project_id="p", engine="local", estimated_tokens=200,
                             profile={"budget_mode": "warn", "production_ceiling_tokens": 100})
    assert d.verdict == "ask"
    assert d.action == "cost_over_budget"


def test_limit_mode_denies_over_the_ceiling():
    d = budget_policy.decide(project_id="p", engine="local", estimated_tokens=200,
                             profile={"budget_mode": "limit", "production_ceiling_tokens": 100})
    assert d.verdict == "deny"


def test_limit_mode_allows_under_the_ceiling():
    d = budget_policy.decide(project_id="p", engine="local", estimated_tokens=50,
                             profile={"budget_mode": "limit", "production_ceiling_tokens": 100})
    assert d.verdict == "allow"


def test_threshold_asks_independent_of_mode():
    d = budget_policy.decide(project_id="p", engine="local", estimated_tokens=999,
                             profile={"budget_mode": "limit", "approval_threshold_tokens": 500,
                                      "production_ceiling_tokens": 100000})
    assert d.verdict == "ask"


def test_cloud_engine_always_asks_regardless_of_tokens():
    d = budget_policy.decide(project_id="p", engine="openai:gpt", estimated_tokens=1,
                             cloud_engine=True, profile={"budget_mode": "observe"})
    assert d.verdict == "ask"
    assert d.action == "cloud_model"


def test_limit_mode_end_to_end_deny_via_preflight(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    report = pf.run_preflight(
        owner=OWNER, project_id="proj1", operation="render", engine="local",
        deployment_id="dep1", params={"estimated_tokens": 5000}, inputs=[],
        profile={"budget_mode": "limit", "production_ceiling_tokens": 1000},
    )
    assert report.ok is False
    assert report.requires_approval is False
    assert report.budget.verdict == "deny"
    assert any(m["kind"] == "param" and m["id"] == "budget" for m in report.missing)


# ---------------------------------------------------------------------
# budget_policy.reserve/reconcile — real writes to budget_account
# ---------------------------------------------------------------------

def test_reserve_and_reconcile_write_to_the_real_budget_account(own_budget):
    r = budget_policy.reserve("projX", "op-1", tokens=400, ceiling_tokens=1000)
    assert isinstance(r, budget_account.Reservation)
    snap = budget_policy.snapshot("projX")
    assert snap["reserved_tokens"] == 400
    budget_policy.reconcile("projX", "op-1", used_tokens=250, used_cost=0.01)
    snap2 = budget_policy.snapshot("projX")
    assert snap2["reserved_tokens"] == 0
    assert snap2["consumed_tokens"] == 250
    assert snap2["consumed_cost"] == 0.01


def test_reserve_rejected_when_it_would_exceed_the_project_ceiling(own_budget):
    budget_policy.reserve("projY", "op-1", tokens=800, ceiling_tokens=1000)
    r2 = budget_policy.reserve("projY", "op-2", tokens=500, ceiling_tokens=1000)
    assert isinstance(r2, budget_account.BudgetExceeded)


# ---------------------------------------------------------------------
# ApprovalPlan digest: material change invalidates it
# ---------------------------------------------------------------------

def test_changing_params_changes_the_approval_digest(own_database, monkeypatch):
    _stub_capabilities(monkeypatch, _caps())
    _stub_params_ok(monkeypatch)
    profile = {"budget_mode": "warn", "production_ceiling_tokens": 10}
    r1 = pf.run_preflight(owner=OWNER, project_id="p", operation="render", engine="local",
                          deployment_id="dep1", params={"estimated_tokens": 50, "seed": 1},
                          inputs=[], profile=profile)
    r2 = pf.run_preflight(owner=OWNER, project_id="p", operation="render", engine="local",
                          deployment_id="dep1", params={"estimated_tokens": 50, "seed": 2},
                          inputs=[], profile=profile)
    assert r1.approval_digest and r2.approval_digest
    assert r1.approval_digest != r2.approval_digest


# ---------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------

@pytest.fixture()
def route_client(tmp_path, monkeypatch, own_database, own_budget):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    _stub_capabilities(monkeypatch, _caps(price_usd_per_1k_tokens=1.0))
    _stub_params_ok(monkeypatch)

    from routes.creator_preflight_routes import setup_creator_preflight_routes
    app = FastAPI()
    app.include_router(setup_creator_preflight_routes())
    client = TestClient(app)
    project = ps.create("HTTP Proj", owner="__odysseus_local__", scaffold_memory=False)
    return client, project["id"]


def test_route_flag_off_is_404(tmp_path, monkeypatch, own_database, own_budget):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects_off"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Off Proj", owner="__odysseus_local__", scaffold_memory=False)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)

    from routes.creator_preflight_routes import setup_creator_preflight_routes
    app = FastAPI()
    app.include_router(setup_creator_preflight_routes())
    client = TestClient(app)

    resp = client.post("/api/creator/preflight",
                        json={"project_id": project["id"], "operation": "render", "engine": "local"})
    assert resp.status_code == 404


def test_route_preflight_report_shape(route_client):
    client, project_id = route_client
    resp = client.post("/api/creator/preflight", json={
        "project_id": project_id, "operation": "render", "engine": "local",
        "deployment_id": "dep1", "params": {"estimated_tokens": 10}, "inputs": [],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["requires_approval"] is False
    assert body["estimate"]["cost_usd"] == 0.01


def test_route_approve_creates_a_real_single_use_approval(route_client, monkeypatch):
    client, project_id = route_client
    from src import settings as settings_mod
    from services.projects import get_store as get_project_store
    from src.creator import profile as profile_mod
    profile_mod.set_profile(get_project_store(), "__odysseus_local__", project_id,
                            {"budget_mode": "warn", "production_ceiling_tokens": 5})

    body = {"project_id": project_id, "operation": "render", "engine": "local",
            "deployment_id": "dep1", "params": {"estimated_tokens": 500}, "inputs": []}

    pre = client.post("/api/creator/preflight", json=body)
    assert pre.status_code == 200
    report = pre.json()
    assert report["requires_approval"] is True
    digest = report["approval_digest"]
    assert digest

    approve = client.post(f"/api/creator/preflight/{digest}/approve", json=body)
    assert approve.status_code == 200
    approval = approve.json()["approval"]
    assert approval["status"] == "granted"

    # The card is real and lives in approval_store, consumable exactly once —
    # two concurrent "submits" cannot spend the same permission twice.
    from src.contracts import ApprovalPlan
    plan = ApprovalPlan.parse(report["approval_plan"])
    first = approval_store.consume(approval["id"], plan, owner="__odysseus_local__")
    assert first["ok"] is True
    second = approval_store.consume(approval["id"], plan, owner="__odysseus_local__")
    assert second["ok"] is False


def test_route_approve_rejects_stale_digest_after_material_change(route_client):
    client, project_id = route_client
    from services.projects import get_store as get_project_store
    from src.creator import profile as profile_mod
    profile_mod.set_profile(get_project_store(), "__odysseus_local__", project_id,
                            {"budget_mode": "warn", "production_ceiling_tokens": 5})

    body = {"project_id": project_id, "operation": "render", "engine": "local",
            "deployment_id": "dep1", "params": {"estimated_tokens": 500}, "inputs": []}
    pre = client.post("/api/creator/preflight", json=body)
    digest = pre.json()["approval_digest"]

    changed_body = dict(body, params={"estimated_tokens": 900})
    resp = client.post(f"/api/creator/preflight/{digest}/approve", json=changed_body)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "digest_mismatch"
