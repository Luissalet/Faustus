"""A28 (docs/spec/paridad/, Lote U5): a promoted candidate regresses after a
dependency update.

Trigger: a candidate is proposed, validated, evaluated (source + held-out
both pass) and promoted — a real `dep_skill` fixture directory on disk is
its declared dependency, and its digest at promotion time
(`src.skills_runtime.discovery.skill_digest`, the SAME digest function
`src/workflows/skills.py` uses for ADP-25's approval pinning — not a
digest invented for this test) is recorded as `required_capabilities`.
The dependency's fixture file is then edited on disk (a real byte change,
not a stubbed "changed" flag) and `recheck_candidate` recomputes the
digest for real.

Expect: "Applicability rechecked and candidate reverted or disabled with
trace" — the recheck detects the changed digest, the harness's active
revision is reverted (through the real store, not a flag flip) back to
what `rollback_target` names, the candidate's status becomes `reverted`,
and `trace` names the exact capability whose digest moved.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.evolution_routes import setup_evolution_routes
from src.harness_evolution.service import HarnessEvolutionService
from src.harness_evolution.store import HarnessEvolutionStore
from src.skills_runtime import discovery

from tests.acceptance.conftest import record_evidence

SKILL_MD = """---
name: depends-on-dep-skill
category: demo
version: 1.0.0
permissions_backends: [local]
permissions_filesystem: workspace
---

# Depends On Dep Skill

## When to Use

Something that needs `dep_skill` to still behave the way it did when this
was promoted.

## Verification

- Both source and held-out fixture tasks pass.
"""


def _write_dep_skill(workspace, body: str) -> str:
    """Write a real `dep_skill` under `workspace/.faustus/skills/` (the
    same layout `src.skills_runtime.discovery` walks in production) and
    return its REAL digest — `skill_digest` hashes actual bytes on disk, so
    calling this twice with different `body` genuinely changes the return
    value; nothing here fakes "the dependency changed"."""
    folder = workspace / ".faustus" / "skills" / "dep_skill"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\nname: dep_skill\ncategory: demo\nversion: 1.0.0\n---\n\n"
        f"# Dep Skill\n\n## When to Use\n\n{body}\n", encoding="utf-8")
    found = next(
        f for f in discovery.discover(str(workspace)) if f.name == "dep_skill")
    return discovery.skill_digest(found)


def _client(store: HarnessEvolutionStore, monkeypatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import routes.evolution_routes as evolution_routes
    monkeypatch.setattr(evolution_routes, "_service",
                        lambda: HarnessEvolutionService(store=store))
    app = FastAPI()
    app.include_router(setup_evolution_routes())
    return TestClient(app)


@pytest.mark.acceptance("A28")
def test_dependency_digest_change_reverts_the_promoted_candidate(tmp_path, monkeypatch, request):
    digest_at_promotion = _write_dep_skill(tmp_path, "the original behaviour.")

    store = HarnessEvolutionStore(db_path=str(tmp_path / "harness.db"))
    service = HarnessEvolutionService(store=store)
    client = _client(store, monkeypatch)

    parent_before = service.active_revision()
    patch = service.propose_candidate(
        changes={"type": "add_skill", "markdown": SKILL_MD},
        required_capabilities={"dep_skill": digest_at_promotion})
    assert patch.status == "validated", patch.trace
    patch = service.evaluate_candidate(
        patch.patch_id, runner=lambda t: True,
        source_task_ids=["s1"], held_out_task_ids=["h1"])
    assert patch.status == "evaluated"

    revision = service.promote_candidate(patch.patch_id, actor="tester", canary_runs=1,
                                         canary_runner=lambda i: True)
    assert revision.status == "active"
    assert client.get("/api/harness/revisions").json()["active_revision_id"] == revision.revision_id

    promoted_patch = service.get_patch(patch.patch_id)
    assert promoted_patch.status == "promoted"
    assert promoted_patch.rollback_target == parent_before.revision_id

    # A REAL dependency update: the fixture skill's own bytes change on
    # disk, so `skill_digest` genuinely returns something different — this
    # is not a canned "changed" flag.
    new_digest = _write_dep_skill(tmp_path, "a rewritten, incompatible behaviour.")
    assert new_digest != digest_at_promotion

    def resolve_digest(name: str):
        assert name == "dep_skill"
        return new_digest

    result = service.recheck_candidate(patch.patch_id, resolve_digest=resolve_digest,
                                       actor="dependency-monitor")
    assert result.ok is False
    assert result.changed_capabilities == ["dep_skill"]
    assert "dep_skill" in result.trace

    reverted_patch = service.get_patch(patch.patch_id)
    assert reverted_patch.status == "reverted"
    assert "dep_skill" in reverted_patch.trace

    active_after = client.get("/api/harness/revisions").json()
    assert active_after["active_revision_id"] != revision.revision_id
    restored_row = next(r for r in active_after["revisions"]
                        if r["revision_id"] == active_after["active_revision_id"])
    parent_row = next(r for r in active_after["revisions"]
                      if r["revision_id"] == parent_before.revision_id)
    assert restored_row["refs"] == parent_row["refs"], (
        "the reverted revision must carry the rollback_target's refs, not the "
        "regressed candidate's")

    record_evidence(request, patch_id=patch.patch_id,
                    reverted_revision=active_after["active_revision_id"],
                    trace=result.trace)


@pytest.mark.acceptance("A28")
def test_unchanged_dependency_digest_leaves_candidate_promoted(tmp_path, request):
    """Control case: a recheck against an UNCHANGED digest must never
    revert a healthy promotion — otherwise the mechanism would be reverting
    on every recheck rather than on an actual regression."""
    digest = _write_dep_skill(tmp_path, "stable behaviour.")

    store = HarnessEvolutionStore(db_path=str(tmp_path / "harness_control.db"))
    service = HarnessEvolutionService(store=store)
    patch = service.propose_candidate(
        changes={"type": "add_skill", "markdown": SKILL_MD},
        required_capabilities={"dep_skill": digest})
    patch = service.evaluate_candidate(patch.patch_id, runner=lambda t: True,
                                       source_task_ids=["s"], held_out_task_ids=["h"])
    revision = service.promote_candidate(patch.patch_id, actor="tester", canary_runs=0)

    result = service.recheck_candidate(patch.patch_id, resolve_digest=lambda name: digest)
    assert result.ok is True
    assert service.get_patch(patch.patch_id).status == "promoted"
    assert service.active_revision().revision_id == revision.revision_id
    record_evidence(request, patch_id=patch.patch_id)
