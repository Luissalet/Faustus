"""tests/test_w3c_estimate_followups.py — CMP-08 seguimiento (W3-C,
`CONTRATO_W3.md`).

Four things closed here, each documented as an open gap by W2-B
(`docs/api/topology.md` "Cost estimate" section, and CMP-08.md):

1. A skill can declare its own `calls_profile` in its `SKILL.md`
   (`src/contracts/skill.py::CallsProfileSpec`,
   `src/skills_runtime/bridge.py::calls_profile_from_frontmatter`), and the
   route picks it up automatically from a project's workspace
   (`routes/workflows_routes.py::_declared_calls_profiles_from_manifests`).
2. `DATA_DIR/skill_call_history.json` is actually written now
   (`src/workflows/skills.py::_record_call_history`), and the route's reader
   drops any entry that never gained a real count rather than reading a
   missing key as `0` (`_skill_call_history_from_disk`).
3. `src/workflow_cost_estimate.py::local_latency_for`/`local_latency_snapshot`
   actually compute a `local_latency` row from `gpu_policy.model_sizes`,
   `llm_core.local_speed` and `resource_admission.status` — every field
   `"unknown"` unless a real signal names it, kept OUT of `estimate()`/
   `estimate_detailed()` so both stay pure.
4. The Studio's `EstimateView.tsx` (checked by
   `studio/checks/w3c_estimate_followups.check.mjs`, via
   `test_w3c_estimate_followups_js` below) — not duplicated here in Python.

No network calls anywhere in this file.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from src.contracts import ContractError
from src.contracts.skill import CallsProfileSpec, SkillManifest
from src.contracts.workflow import WorkflowDefinition, WorkflowNode
from src.skills_runtime import bridge
from tests.test_skills_runtime import write_skill

MODEL = "local:test-7b"


def _definition(nodes, *, wf_id="wf.w3c"):
    return WorkflowDefinition(id=wf_id, version="1.0.0", title="Test", nodes=tuple(nodes))


# ── 1a. SkillManifest.calls_profile ─────────────────────────────────────────

def test_calls_profile_from_frontmatter_reads_flat_keys():
    fm = {
        "calls_profile_model_calls": 3, "calls_profile_external_ops": 1,
        "calls_profile_tokens_in": 1200, "calls_profile_tokens_out": 400,
    }
    profile = bridge.calls_profile_from_frontmatter(fm)
    assert profile == {"model_calls": 3, "external_ops": 1, "tokens_in": 1200, "tokens_out": 400}


def test_calls_profile_from_frontmatter_absent_is_none_not_zeroed():
    assert bridge.calls_profile_from_frontmatter({}) is None
    assert bridge.calls_profile_from_frontmatter({"name": "x"}) is None


def test_skill_manifest_round_trips_a_declared_calls_profile(tmp_path):
    path = write_skill(tmp_path, "research-pack", extra_lines=(
        "calls_profile_model_calls: 3", "calls_profile_external_ops: 1",
        "calls_profile_tokens_in: 1200", "calls_profile_tokens_out: 400",
    ))
    manifest = bridge.manifest_from_markdown(Path(path).read_text(encoding="utf-8"), source=path)
    assert manifest.calls_profile == CallsProfileSpec(
        model_calls=3.0, external_ops=1.0, tokens_in=1200.0, tokens_out=400.0)
    assert manifest.to_dict()["calls_profile"] == {
        "model_calls": 3.0, "external_ops": 1.0, "tokens_in": 1200.0, "tokens_out": 400.0}


def test_skill_manifest_without_calls_profile_frontmatter_is_none(tmp_path):
    path = write_skill(tmp_path, "quiet-pack")
    manifest = bridge.manifest_from_markdown(Path(path).read_text(encoding="utf-8"), source=path)
    assert manifest.calls_profile is None
    assert manifest.to_dict()["calls_profile"] is None


def test_calls_profile_spec_rejects_a_negative_count():
    with pytest.raises(ContractError):
        SkillManifest.parse({
            "id": "x", "version": "1.0.0", "title": "X",
            "calls_profile": {"model_calls": -1, "external_ops": 0, "tokens_in": 0, "tokens_out": 0},
        })


# ── 3. local_latency_for / local_latency_snapshot ───────────────────────────

def test_local_latency_for_empty_model_is_all_unknown():
    from src.workflow_cost_estimate import LOCAL_LATENCY_FIELDS, local_latency_for
    row = local_latency_for("")
    assert all(row[f] == "unknown" for f in LOCAL_LATENCY_FIELDS)


def test_local_latency_for_no_endpoint_is_all_unknown_except_maybe_generation():
    from src.workflow_cost_estimate import local_latency_for
    row = local_latency_for(MODEL)  # no endpoint_url given
    assert row["load"] == "unknown"
    assert row["queue"] == "unknown"
    assert row["memory_server"] == "unknown"
    assert row["prefill_tps"] == "unknown"
    assert "size_bytes" not in row


def test_local_latency_for_generation_tps_comes_from_llm_core(monkeypatch):
    from src import llm_core
    from src.workflow_cost_estimate import local_latency_for
    monkeypatch.setattr(llm_core, "local_speed", lambda model: 42.5 if model == MODEL else None)
    row = local_latency_for(MODEL)
    assert row["generation_tps"] == 42.5
    row_other = local_latency_for("some-other-model")
    assert row_other["generation_tps"] == "unknown"


def test_local_latency_for_load_stays_unknown_even_with_a_known_size(monkeypatch):
    from src import gpu_policy
    from src.workflow_cost_estimate import local_latency_for
    monkeypatch.setattr(gpu_policy, "model_sizes", lambda base, timeout=3.0: {MODEL: 4_900_000_000})
    row = local_latency_for(MODEL, endpoint_url="http://localhost:11434")
    assert row["load"] == "unknown"  # per the contract: size known != load time known
    assert row["size_bytes"] == 4_900_000_000


def test_local_latency_for_queue_comes_from_resource_admission(monkeypatch):
    from src import resource_admission
    from src.workflow_cost_estimate import local_latency_for
    resource_admission.reset_all()
    try:
        resource_admission.define_pool("local-ollama", "gpu", endpoints=["http://localhost:11434"],
                                       max_concurrent=2)
        row = local_latency_for(MODEL, endpoint_url="http://localhost:11434")
        assert row["queue"]["pool_id"] == "local-ollama"
        assert row["queue"]["available"] == 2
        assert row["queue"]["foreground_waiting"] == 0
    finally:
        resource_admission.reset_all()


def test_local_latency_for_queue_unknown_when_endpoint_has_no_pool(monkeypatch):
    from src import resource_admission
    from src.workflow_cost_estimate import local_latency_for
    resource_admission.reset_all()
    row = local_latency_for(MODEL, endpoint_url="http://nowhere:9999")
    assert row["queue"] == "unknown"


def test_local_latency_for_never_raises_on_a_broken_signal(monkeypatch):
    from src import llm_core
    from src.workflow_cost_estimate import local_latency_for

    def boom(model):
        raise RuntimeError("no local model table")
    monkeypatch.setattr(llm_core, "local_speed", boom)
    row = local_latency_for(MODEL, endpoint_url="http://localhost:11434")
    assert row["generation_tps"] == "unknown"


def test_local_latency_snapshot_batches_several_models(monkeypatch):
    from src import llm_core
    from src.workflow_cost_estimate import local_latency_snapshot
    monkeypatch.setattr(llm_core, "local_speed", lambda model: {"a": 10.0, "b": 20.0}.get(model))
    snap = local_latency_snapshot({"a": "http://x:1", "b": "http://x:1"})
    assert snap["a"]["generation_tps"] == 10.0
    assert snap["b"]["generation_tps"] == 20.0


# ── 2. skill_call_history.json: the writer (src/workflows/skills.py) ───────

def test_record_call_history_counts_runs_only_when_nothing_else_is_known(tmp_path, monkeypatch):
    from src.workflows import skills as skills_mod
    history_path = tmp_path / "skill_call_history.json"
    monkeypatch.setattr(skills_mod, "CALL_HISTORY_PATH", history_path)
    skills_mod._record_call_history("my.skill")
    skills_mod._record_call_history("my.skill")
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data["my.skill"] == {"runs": 2}  # no model_calls/etc — never invented


def test_record_call_history_averages_only_the_runs_that_reported_a_count(tmp_path, monkeypatch):
    from src.workflows import skills as skills_mod
    history_path = tmp_path / "skill_call_history.json"
    monkeypatch.setattr(skills_mod, "CALL_HISTORY_PATH", history_path)
    skills_mod._record_call_history("my.skill")  # run 1: nothing known
    skills_mod._record_call_history("my.skill", model_calls=2, tokens_in=1000)  # run 2
    skills_mod._record_call_history("my.skill", model_calls=4, tokens_in=2000)  # run 3
    data = json.loads(history_path.read_text(encoding="utf-8"))
    entry = data["my.skill"]
    assert entry["runs"] == 3
    assert entry["model_calls"] == pytest.approx(3.0)   # average of 2 and 4 — run 1 not counted
    assert entry["model_calls_samples"] == 2
    assert entry["tokens_in"] == pytest.approx(1500.0)
    assert "external_ops" not in entry  # never given, never guessed at 0


def test_record_call_history_ignores_an_empty_skill_id(tmp_path, monkeypatch):
    from src.workflows import skills as skills_mod
    history_path = tmp_path / "skill_call_history.json"
    monkeypatch.setattr(skills_mod, "CALL_HISTORY_PATH", history_path)
    skills_mod._record_call_history("")
    assert not history_path.exists()


# ── 2b. skill_call_history.json: the reader (routes/workflows_routes.py) ───

def test_skill_call_history_from_disk_drops_runs_only_entries(tmp_path, monkeypatch):
    from src import constants
    from routes.workflows_routes import _skill_call_history_from_disk
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    (tmp_path / "skill_call_history.json").write_text(json.dumps({
        "runs_only_pack": {"runs": 5},
        "known_pack": {"runs": 2, "model_calls": 3.0, "model_calls_samples": 2},
    }), encoding="utf-8")
    history = _skill_call_history_from_disk()
    assert "runs_only_pack" not in history
    assert history["known_pack"]["model_calls"] == 3.0


def test_skill_call_history_from_disk_missing_file_is_empty(tmp_path, monkeypatch):
    from src import constants
    from routes.workflows_routes import _skill_call_history_from_disk
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    assert _skill_call_history_from_disk() == {}


# ── 1b. routes: declared calls_profile picked up from a project's manifests ─

class _FakeProjectStore:
    def __init__(self, workspace: str):
        self._workspace = workspace

    def get(self, project_id, owner=None):
        return {"workspace": self._workspace}


def test_declared_calls_profiles_from_manifests_reads_a_projects_skill_md(tmp_path, monkeypatch):
    import services.projects as projects_mod
    from routes.workflows_routes import _declared_calls_profiles_from_manifests

    workspace = tmp_path / "proj"
    skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    write_skill(skills_dir, "research-pack", extra_lines=(
        "calls_profile_model_calls: 3", "calls_profile_external_ops: 1",
        "calls_profile_tokens_in: 1200", "calls_profile_tokens_out: 400",
    ))
    monkeypatch.setattr(projects_mod, "get_store", lambda: _FakeProjectStore(str(workspace)))

    definition = _definition([
        WorkflowNode(id="work", type="skill", config={"skill": "research-pack", "model": MODEL}),
    ])
    profiles = _declared_calls_profiles_from_manifests(definition, owner="alice", project_id="proj-1")
    assert profiles == {"research-pack": {
        "model_calls": 3.0, "external_ops": 1.0, "tokens_in": 1200.0, "tokens_out": 400.0}}


def test_declared_calls_profiles_from_manifests_needs_owner_and_project(tmp_path):
    from routes.workflows_routes import _declared_calls_profiles_from_manifests
    definition = _definition([
        WorkflowNode(id="work", type="skill", config={"skill": "research-pack", "model": MODEL}),
    ])
    assert _declared_calls_profiles_from_manifests(definition, owner="", project_id="") == {}
    assert _declared_calls_profiles_from_manifests(definition, owner="alice", project_id="") == {}


def test_declared_calls_profiles_from_manifests_skips_skills_without_one(tmp_path, monkeypatch):
    import services.projects as projects_mod
    from routes.workflows_routes import _declared_calls_profiles_from_manifests

    workspace = tmp_path / "proj"
    skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    write_skill(skills_dir, "quiet-pack")  # no calls_profile_* frontmatter
    monkeypatch.setattr(projects_mod, "get_store", lambda: _FakeProjectStore(str(workspace)))

    definition = _definition([
        WorkflowNode(id="work", type="skill", config={"skill": "quiet-pack", "model": MODEL}),
    ])
    assert _declared_calls_profiles_from_manifests(definition, owner="alice", project_id="proj-1") == {}


# ── routes: /estimate?detail=1 end to end ───────────────────────────────────

@pytest.fixture()
def workflows_client(tmp_path, monkeypatch):
    from routes.workflows_routes import setup_workflows_routes
    url = "sqlite:///" + (tmp_path / "wf_w3c.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_workflows_routes())
    yield TestClient(app)
    engine.dispose()


def test_route_estimate_detail_picks_up_a_declared_profile_when_owner_and_project_are_given(
        tmp_path, monkeypatch, workflows_client):
    import services.projects as projects_mod
    workspace = tmp_path / "proj"
    skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    write_skill(skills_dir, "research-pack", extra_lines=(
        "calls_profile_model_calls: 3", "calls_profile_external_ops: 1",
        "calls_profile_tokens_in: 1200", "calls_profile_tokens_out: 400",
    ))
    monkeypatch.setattr(projects_mod, "get_store", lambda: _FakeProjectStore(str(workspace)))

    definition = {
        "id": "wf.manifest", "version": "1.0.0", "title": "Manifest",
        "nodes": [{"id": "work", "type": "skill", "config": {"skill": "research-pack", "model": MODEL}}],
    }
    response = workflows_client.post("/api/workflows/estimate?detail=1", json={
        "definition": definition, "owner": "alice", "project_id": "proj-1",
    })
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert body["model_calls"] == {"min": 3, "max": 3}
    assert body["external_ops"] == {"min": 1, "max": 1}
    row = body["per_node"][0]
    assert row["calls_profile_source"] == "declared"


def test_route_estimate_detail_explicit_skill_calls_profiles_wins_over_the_manifest(
        tmp_path, monkeypatch, workflows_client):
    import services.projects as projects_mod
    workspace = tmp_path / "proj"
    skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    write_skill(skills_dir, "research-pack", extra_lines=("calls_profile_model_calls: 3",))
    monkeypatch.setattr(projects_mod, "get_store", lambda: _FakeProjectStore(str(workspace)))

    definition = {
        "id": "wf.manifest2", "version": "1.0.0", "title": "Manifest2",
        "nodes": [{"id": "work", "type": "skill", "config": {"skill": "research-pack", "model": MODEL}}],
    }
    response = workflows_client.post("/api/workflows/estimate?detail=1", json={
        "definition": definition, "owner": "alice", "project_id": "proj-1",
        "skill_calls_profiles": {"research-pack": {
            "model_calls": 9, "external_ops": 0, "tokens_in": 0, "tokens_out": 0}},
    })
    assert response.status_code == 200
    body = response.json()["estimate"]
    assert body["model_calls"] == {"min": 9, "max": 9}


def test_route_estimate_detail_without_owner_or_project_behaves_exactly_as_before(workflows_client):
    definition = {
        "id": "wf.plain", "version": "1.0.0", "title": "Plain",
        "nodes": [{"id": "work", "type": "skill", "config": {"skill": "mystery-pack", "model": MODEL}}],
    }
    response = workflows_client.post("/api/workflows/estimate?detail=1", json={"definition": definition})
    assert response.status_code == 200
    body = response.json()["estimate"]
    row = body["per_node"][0]
    assert row["calls_profile_source"] == "unknown"
    assert any("mystery-pack" in reason for reason in body["cost_unestimable"])


# ── JS check (studio/checks/w3c_estimate_followups.check.mjs) ──────────────

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "w3c_estimate_followups.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_w3c_estimate_followups_js():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok w3c_estimate_followups" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/screens/activity/EstimateView.tsx",
        "studio/checks/w3c_estimate_followups.check.mjs",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"
