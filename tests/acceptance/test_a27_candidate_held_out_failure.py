"""A27 (docs/spec/paridad/, Lote U5): a candidate succeeds on its source
task but fails held-out tasks.

Trigger: `HarnessEvolutionService.evaluate_candidate` runs a real (fake-only
as the contract's "external process", per rule 1) task runner over the
patch's source AND held-out task ids. The runner passes every source task
and fails a held-out one.

Expect: "No promotion based solely on source success; comparison evidence
preserved" — the candidate never reaches `status="evaluated"` (only
`"evaluated_failed"`), `promote_candidate` refuses it outright (never even
attempts the CAS/canary step), and the per-task `source`/`held_out` dict —
not just a boolean — is still readable through `GET
/api/harness/candidates/{id}` after the failed evaluation, so a reviewer
can see exactly which held-out task broke it.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.evolution_routes import setup_evolution_routes
from src.harness_evolution.service import HarnessEvolutionService
from src.harness_evolution.store import HarnessEvolutionStore

from tests.acceptance.conftest import record_evidence

SKILL_MD = """---
name: source-only
category: demo
version: 1.0.0
permissions_backends: [local]
permissions_filesystem: workspace
---

# Source Only

## When to Use

Passes whatever the fixture task runner was scripted to pass.

## Verification

- Every source task in the fixture run returns pass.
"""

#: The fake "external task runner" (rule 1): deterministic, no LLM, no
#: process — a task id starting with "source_" always passes, a held-out
#: one never does. This is the fake the contract allows; everything that
#: reads its verdicts (`evaluate_candidate`, `promote_candidate`, the real
#: routes/TestClient) is real code.
def _runner(task_id: str) -> bool:
    return task_id.startswith("source_")


def _client(store: HarnessEvolutionStore, monkeypatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import routes.evolution_routes as evolution_routes
    monkeypatch.setattr(evolution_routes, "_service",
                        lambda: HarnessEvolutionService(store=store))
    app = FastAPI()
    app.include_router(setup_evolution_routes())
    return TestClient(app)


@pytest.mark.acceptance("A27")
def test_held_out_failure_blocks_promotion_and_keeps_evidence(tmp_path, monkeypatch, request):
    store = HarnessEvolutionStore(db_path=str(tmp_path / "harness.db"))
    service = HarnessEvolutionService(store=store)
    client = _client(store, monkeypatch)

    patch = service.propose_candidate(changes={"type": "add_skill", "markdown": SKILL_MD})
    assert patch.status == "validated", patch.trace

    patch = service.evaluate_candidate(
        patch.patch_id, runner=_runner,
        source_task_ids=["source_1", "source_2"],
        held_out_task_ids=["held_out_1"])
    assert patch.status == "evaluated_failed"
    assert patch.evaluation["source_pass"] is True
    assert patch.evaluation["held_out_pass"] is False
    assert patch.evaluation["held_out"] == {"held_out_1": False}

    promote = client.post(f"/api/harness/candidates/{patch.patch_id}/promote", json={})
    assert promote.status_code == 400, promote.text
    assert "evaluated" in promote.json()["detail"]

    active_after = client.get("/api/harness/revisions").json()["active_revision_id"]
    assert active_after == service.active_revision().revision_id
    # The comparison evidence is queryable after the fact, per task.
    fetched = client.get(f"/api/harness/candidates/{patch.patch_id}").json()["candidate"]
    assert fetched["status"] == "evaluated_failed"
    assert fetched["evaluation"]["source"] == {"source_1": True, "source_2": True}
    assert fetched["evaluation"]["held_out"] == {"held_out_1": False}

    record_evidence(request, patch_id=patch.patch_id,
                    evaluation=fetched["evaluation"])


@pytest.mark.acceptance("A27")
def test_all_held_out_passing_is_required_not_just_nonempty_source(tmp_path, request):
    """A candidate that passes source and ALL of its held-out tasks IS
    eligible — the negative test above is not vacuously true because the
    mechanism always refuses. This is the control case for A27's flip
    side: comparison evidence for a SUCCESSFUL held-out run is preserved
    exactly the same way, and only then does `evaluated` (not
    `evaluated_failed`) unlock `promote_candidate`."""
    store = HarnessEvolutionStore(db_path=str(tmp_path / "harness_control.db"))
    service = HarnessEvolutionService(store=store)
    patch = service.propose_candidate(changes={"type": "add_skill", "markdown": SKILL_MD})
    patch = service.evaluate_candidate(
        patch.patch_id, runner=lambda t: True,
        source_task_ids=["source_1"], held_out_task_ids=["held_out_1", "held_out_2"])
    assert patch.status == "evaluated"
    revision = service.promote_candidate(patch.patch_id, actor="tester", canary_runs=0)
    assert revision.status == "active"
    record_evidence(request, patch_id=patch.patch_id, promoted_revision=revision.revision_id)
