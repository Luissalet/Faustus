"""WP41 — presets, evaluation and discovery.

Real sqlite `PresetStore`, real `src.harness_evolution.HarnessEvolutionService`
(its own tmp-path store), a real `TestClient` for the route layer. The only
faked pieces are the completed-run listing `discover_from_runs` reads (a
plain list of dicts, standing in for `src.media_runs.recent()`) and the
`TaskRunner` callables `promote_preset` evaluates against — per CONTRATO.md
rule 8, "fakes solo para motores externos/modelos", never for the store or
the harness itself.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.creator import params as params_mod
from src.creator import presets as presets_mod
from src.creator.presets import (
    InvalidPreset,
    Preset,
    PresetNotFound,
    PresetRevisionConflict,
    PresetStore,
    PromotionRejected,
    STATUS_RECOMMENDED,
    STATUS_RETIRED,
    STATUS_UNREVIEWED,
)
from src.harness_evolution.service import HarnessEvolutionService
from src.harness_evolution.store import HarnessEvolutionStore

ENGINE = "wp41_testengine"
TASK = "wp41.test_task"


@pytest.fixture(autouse=True, scope="module")
def _register_test_schema():
    params_mod.register_schema(params_mod.ParamSchema(
        engine=ENGINE, task=TASK,
        fields=(
            params_mod.ParamField(key="prompt", label="Prompt", type=params_mod.TYPE_STRING,
                                  required=True),
            params_mod.ParamField(key="steps", label="Steps", type=params_mod.TYPE_INTEGER,
                                  default=20, minimum=1, maximum=150),
        ),
    ))


@pytest.fixture
def store(tmp_path) -> PresetStore:
    return PresetStore(str(tmp_path / "presets.db"))


@pytest.fixture
def harness_service(tmp_path) -> HarnessEvolutionService:
    return HarnessEvolutionService(HarnessEvolutionStore(str(tmp_path / "harness.db")))


def _make(store: PresetStore, owner="alice", **overrides) -> Preset:
    params = {"prompt": "a cat", **overrides}
    return store.create(owner, ENGINE, TASK, params, origin=presets_mod.ORIGIN_USER)


# ── create() only ever stores schema-validated, normalized params ───────

def test_create_rejects_params_outside_the_schema(store):
    with pytest.raises(InvalidPreset):
        store.create("alice", ENGINE, TASK, {"prompt": "x", "not_a_field": 1})


def test_create_rejects_missing_required_field(store):
    with pytest.raises(InvalidPreset):
        store.create("alice", ENGINE, TASK, {"steps": 10})


def test_create_stores_the_normalized_result_with_defaults_filled_in(store):
    preset = _make(store)
    assert preset.params == {"prompt": "a cat", "steps": 20}
    assert preset.status == STATUS_UNREVIEWED
    assert preset.origin == presets_mod.ORIGIN_USER
    assert preset.revision == 1


# ── ADR-10: technical and artistic quality never mix ─────────────────────

def test_technical_and_artistic_are_separate_lists_never_averaged(store):
    preset = _make(store)
    record = presets_mod.EvaluationRecord(
        preset_id=preset.id, fixture_run_id="run_1",
        technical={"resolution": "512x512", "ffprobe_ok": True, "elapsed_s": 4.2},
        passed=True, notes="", evaluated_at="2026-01-01T00:00:00Z")
    preset = store.record_evaluation(
        "alice", preset.id, record, command_id="cmd_eval_1", expected_revision=preset.revision)
    preset = store.rate(
        "alice", preset.id, rating=5, comment="great output",
        command_id="cmd_rate_1", expected_revision=preset.revision)

    assert len(preset.technical) == 1 and len(preset.artistic) == 1
    assert preset.technical[0]["technical"]["resolution"] == "512x512"
    assert "rating" not in preset.technical[0]
    assert preset.artistic[0] == {"rating": 5, "comment": "great output",
                                  "rated_by": "alice", "rated_at": preset.artistic[0]["rated_at"]}
    assert "resolution" not in preset.artistic[0]
    d = preset.to_dict()
    assert set(d["quality"].keys()) == {"technical", "artistic"}
    assert "rating" not in d["quality"]["technical"][0]


def test_rate_rejects_out_of_range_rating(store):
    preset = _make(store)
    with pytest.raises(InvalidPreset):
        store.rate("alice", preset.id, rating=6, command_id="c1", expected_revision=1)
    with pytest.raises(InvalidPreset):
        store.rate("alice", preset.id, rating=0, command_id="c2", expected_revision=1)


# ── concurrency: command_id dedupe + expected_revision CAS ──────────────

def test_stale_expected_revision_is_rejected_with_current_revision(store):
    preset = _make(store)
    with pytest.raises(PresetRevisionConflict) as excinfo:
        store.rate("alice", preset.id, rating=3, command_id="c1", expected_revision=999)
    assert excinfo.value.current_revision == preset.revision


def test_same_command_id_replays_instead_of_double_applying(store):
    preset = _make(store)
    once = store.rate("alice", preset.id, rating=4, command_id="dup",
                      expected_revision=preset.revision)
    twice = store.rate("alice", preset.id, rating=4, command_id="dup",
                       expected_revision=preset.revision)
    assert once.revision == twice.revision
    assert len(twice.artistic) == 1  # not applied a second time


# ── owner scoping: "no es tuyo" == "no existe" ───────────────────────────

def test_foreign_owner_gets_not_found_not_someone_elses_data(store):
    preset = _make(store, owner="alice")
    assert store.get("bob", preset.id) is None
    with pytest.raises(PresetNotFound):
        store.rate("bob", preset.id, rating=3, command_id="c1", expected_revision=1)


# ── evaluate(): pure evidence, never a promotion ─────────────────────────

def test_evaluate_is_pure_and_never_mutates_the_store(store):
    preset = _make(store)
    record = presets_mod.evaluate(preset, {
        "run_id": "run_1", "status": "completed",
        "metrics": {"resolution": "512x512", "ffprobe_ok": True}, "elapsed_s": 3.1,
    })
    assert record.passed is True
    # nothing was written until the caller explicitly persists it
    fresh = store.get("alice", preset.id)
    assert fresh.technical == ()
    assert fresh.status == STATUS_UNREVIEWED


def test_evaluate_never_passes_on_an_empty_metrics_dict():
    preset = Preset(id="p1", owner="alice", engine=ENGINE, task=TASK, params={"prompt": "x"})
    record = presets_mod.evaluate(preset, {"run_id": "run_1", "status": "completed"})
    assert record.passed is False


def test_route_evaluate_records_evidence_but_leaves_status_unreviewed(store):
    preset = _make(store)
    preset = store.record_evaluation(
        "alice", preset.id,
        presets_mod.evaluate(preset, {"run_id": "r1", "status": "completed",
                                      "metrics": {"ffprobe_ok": True}}),
        command_id="c1", expected_revision=preset.revision)
    assert preset.status == STATUS_UNREVIEWED
    assert preset.technical[0]["passed"] is True
    assert preset.evidence[0]["kind"] == presets_mod.EVIDENCE_EVALUATION


# ── promotion: only via harness_evolution, held-out gate enforced ───────

def test_promote_succeeds_when_both_source_and_held_out_pass(store, harness_service):
    preset = _make(store)
    preset = store.record_evaluation(
        "alice", preset.id,
        presets_mod.EvaluationRecord(preset_id=preset.id, fixture_run_id="r1",
                                     technical={"ok": True}, passed=True, notes="",
                                     evaluated_at="2026-01-01T00:00:00Z"),
        command_id="seed_eval", expected_revision=preset.revision)

    promoted = presets_mod.promote_preset(
        "alice", preset.id, actor="alice", store=store, harness_service=harness_service)
    assert promoted.status == STATUS_RECOMMENDED
    assert promoted.harness_patch_id
    patch = harness_service.get_patch(promoted.harness_patch_id)
    assert patch.status == "promoted"
    assert any(e["kind"] == presets_mod.EVIDENCE_PROMOTED for e in promoted.evidence)


def test_promote_rejected_on_failed_held_out_keeps_status_and_saves_evidence(store, harness_service):
    preset = _make(store)  # no passed technical evaluation recorded -> held-out check fails

    with pytest.raises(PromotionRejected) as excinfo:
        presets_mod.promote_preset(
            "alice", preset.id, actor="alice", store=store, harness_service=harness_service)

    assert excinfo.value.evaluation.get("held_out_pass") is False
    reloaded = store.get("alice", preset.id)
    # never promoted to "recommended" — marked "rejected" (still the
    # owner's own usable preset, no permission or policy changed)
    assert reloaded.status == presets_mod.STATUS_REJECTED
    assert any(e["kind"] == presets_mod.EVIDENCE_PROMOTION_REJECTED for e in reloaded.evidence)


def test_promote_with_custom_runner_and_task_ids(store, harness_service):
    preset = _make(store)
    calls = []

    def runner(task_id: str) -> bool:
        calls.append(task_id)
        return True

    promoted = presets_mod.promote_preset(
        "alice", preset.id, actor="alice", runner=runner,
        source_task_ids=["smoke"], held_out_task_ids=["held_1", "held_2"],
        store=store, harness_service=harness_service)
    assert promoted.status == STATUS_RECOMMENDED
    assert set(calls) == {"smoke", "held_1", "held_2"}


def test_rollback_restores_previous_status_and_reverts_harness_revision(store, harness_service):
    preset = _make(store)
    preset = store.record_evaluation(
        "alice", preset.id,
        presets_mod.EvaluationRecord(preset_id=preset.id, fixture_run_id="r1",
                                     technical={"ok": True}, passed=True, notes="",
                                     evaluated_at="2026-01-01T00:00:00Z"),
        command_id="seed_eval", expected_revision=preset.revision)
    promoted = presets_mod.promote_preset(
        "alice", preset.id, actor="alice", store=store, harness_service=harness_service)
    active_after_promote = harness_service.active_revision()

    rolled_back = presets_mod.rollback_preset(
        "alice", promoted.id, actor="alice", reason="regression found",
        store=store, harness_service=harness_service)
    assert rolled_back.status == STATUS_RETIRED
    active_after_rollback = harness_service.active_revision()
    assert active_after_rollback.revision_id != active_after_promote.revision_id
    assert any(e["kind"] == presets_mod.EVIDENCE_ROLLED_BACK for e in rolled_back.evidence)


def test_rollback_refuses_a_preset_that_was_never_promoted(store, harness_service):
    preset = _make(store)
    with pytest.raises(InvalidPreset):
        presets_mod.rollback_preset("alice", preset.id, actor="alice", reason="x",
                                    store=store, harness_service=harness_service)


# ── discover_from_runs: only this owner's real, well-rated runs ─────────

def _run(run_id, owner, project_id, status="completed", engine=ENGINE, task=TASK, values=None):
    return {"id": run_id, "owner": owner, "project_id": project_id, "status": status,
            "engine": engine, "workflow": f"ffmpeg:{task}", "values": dict(values or {})}


def test_discover_proposes_only_from_this_owners_completed_well_rated_runs(store):
    runs = [
        _run("r_good", "alice", "proj1", values={"prompt": "sunset", "steps": 30}),
        _run("r_unrated", "alice", "proj1", values={"prompt": "no rating", "steps": 10}),
        _run("r_low_rated", "alice", "proj1", values={"prompt": "meh", "steps": 5}),
        _run("r_other_owner", "bob", "proj1", values={"prompt": "not mine", "steps": 10}),
        _run("r_other_project", "alice", "proj2", values={"prompt": "wrong project", "steps": 10}),
        _run("r_running", "alice", "proj1", status="running", values={"prompt": "unfinished"}),
    ]
    ratings = {"r_good": 5, "r_low_rated": 2, "r_other_owner": 5, "r_other_project": 5}

    proposed = presets_mod.discover_from_runs(
        "alice", "proj1", run_ratings=ratings, store=store, runs=runs)

    assert len(proposed) == 1
    assert proposed[0].params["prompt"] == "sunset"
    assert proposed[0].origin == presets_mod.ORIGIN_DISCOVERED
    assert proposed[0].status == STATUS_UNREVIEWED
    assert proposed[0].evidence[0]["kind"] == presets_mod.EVIDENCE_EVALUATION


def test_discover_is_idempotent_by_params_fingerprint(store):
    runs = [_run("r1", "alice", "proj1", values={"prompt": "same", "steps": 12})]
    ratings = {"r1": 5}
    first = presets_mod.discover_from_runs("alice", "proj1", run_ratings=ratings, store=store,
                                           runs=runs)
    second = presets_mod.discover_from_runs("alice", "proj1", run_ratings=ratings, store=store,
                                            runs=runs)
    assert len(first) == 1
    assert len(second) == 0  # already have a preset for this fingerprint


def test_discover_skips_runs_whose_values_do_not_validate(store):
    runs = [_run("r1", "alice", "proj1", values={"steps": 999})]  # missing required prompt
    proposed = presets_mod.discover_from_runs(
        "alice", "proj1", run_ratings={"r1": 5}, store=store, runs=runs)
    assert proposed == []


# ── routes ─────────────────────────────────────────────────────────────

@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    presets_mod.reset_default_store_for_tests()

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_preset_routes import setup_creator_preset_routes
    app = FastAPI()
    app.include_router(setup_creator_preset_routes())
    client = TestClient(app)
    try:
        yield client
    finally:
        presets_mod.reset_default_store_for_tests()


def test_routes_404_when_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_preset_routes import setup_creator_preset_routes
    app = FastAPI()
    app.include_router(setup_creator_preset_routes())
    client = TestClient(app)
    assert client.get("/api/creator/presets").status_code == 404
    assert client.post("/api/creator/presets", json={"engine": ENGINE, "task": TASK,
                                                      "params": {"prompt": "x"}}).status_code == 404
    assert client.get("/api/creator/presets/discover", params={"project_id": "p1"}).status_code == 404


def test_route_create_list_and_rate_roundtrip(route_client):
    created = route_client.post("/api/creator/presets", json={
        "engine": ENGINE, "task": TASK, "params": {"prompt": "a cat"}})
    assert created.status_code == 200
    body = created.json()
    assert body["params"] == {"prompt": "a cat", "steps": 20}
    preset_id = body["id"]

    listed = route_client.get("/api/creator/presets", params={"engine": ENGINE})
    assert listed.status_code == 200
    assert any(p["id"] == preset_id for p in listed.json()["presets"])

    rated = route_client.post(f"/api/creator/presets/{preset_id}/rate", json={
        "command_id": "c1", "expected_revision": 1, "rating": 5, "comment": "nice"})
    assert rated.status_code == 200
    assert rated.json()["quality"]["artistic"][0]["rating"] == 5


def test_route_create_rejects_invalid_params(route_client):
    resp = route_client.post("/api/creator/presets", json={
        "engine": ENGINE, "task": TASK, "params": {"unknown_field": 1}})
    assert resp.status_code == 400


def test_route_evaluate_then_promote_then_rollback(route_client):
    created = route_client.post("/api/creator/presets", json={
        "engine": ENGINE, "task": TASK, "params": {"prompt": "a cat"}})
    preset_id = created.json()["id"]

    evaluated = route_client.post(f"/api/creator/presets/{preset_id}/evaluate", json={
        "command_id": "eval1", "expected_revision": 1,
        "fixture_run": {"run_id": "r1", "status": "completed", "metrics": {"ok": True}}})
    assert evaluated.status_code == 200
    assert evaluated.json()["preset"]["status"] == STATUS_UNREVIEWED

    promoted = route_client.post(f"/api/creator/presets/{preset_id}/promote", json={})
    assert promoted.status_code == 200
    assert promoted.json()["status"] == STATUS_RECOMMENDED

    rolled_back = route_client.post(f"/api/creator/presets/{preset_id}/rollback", json={
        "reason": "test rollback"})
    assert rolled_back.status_code == 200
    assert rolled_back.json()["status"] == STATUS_RETIRED


def test_route_promote_without_evidence_is_409_with_evaluation_payload(route_client):
    created = route_client.post("/api/creator/presets", json={
        "engine": ENGINE, "task": TASK, "params": {"prompt": "a cat"}})
    preset_id = created.json()["id"]
    resp = route_client.post(f"/api/creator/presets/{preset_id}/promote", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "held_out_evaluation_failed"


def test_route_rate_stale_revision_is_409(route_client):
    created = route_client.post("/api/creator/presets", json={
        "engine": ENGINE, "task": TASK, "params": {"prompt": "a cat"}})
    preset_id = created.json()["id"]
    resp = route_client.post(f"/api/creator/presets/{preset_id}/rate", json={
        "command_id": "c1", "expected_revision": 99, "rating": 3})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "revision_conflict"


def test_route_unknown_preset_is_404(route_client):
    resp = route_client.get("/api/creator/presets", params={})
    assert resp.status_code == 200
    resp2 = route_client.post("/api/creator/presets/does_not_exist/rate", json={
        "command_id": "c1", "expected_revision": 1, "rating": 3})
    assert resp2.status_code == 404
