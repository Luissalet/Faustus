"""A30 (docs/spec/paridad/, Lote U5): two refiners update the same parent
version concurrently.

Trigger: two `CandidatePatch`es, proposed and evaluated against the SAME
active parent revision, are promoted from two REAL OS threads at (as close
to) the same instant — `threading.Barrier` lines them up so both call
`HarnessEvolutionService.promote_candidate` inside the same tiny window,
against `HarnessEvolutionStore.promote_cas`'s real SQLite-backed
compare-and-swap (`BEGIN IMMEDIATE` + `UPDATE ... WHERE status='active'`) —
no lock is taken in Python; the database itself has to serialize the two
writers for this to prove anything.

Expect: "Version check rejects or safely rebases stale patch; no lost
update" — exactly one thread's promotion succeeds and becomes the new
active revision; the other gets `StaleParent` naming the winner (never a
timeout, a crash, or a silently-dropped write); rebasing the loser onto the
winner and retrying (`rebase_and_retry`) succeeds and produces a SECOND new
active revision descended from the first — both changes end up applied,
neither lost.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.evolution_routes import setup_evolution_routes
from src.harness_evolution.service import HarnessEvolutionService
from src.harness_evolution.store import HarnessEvolutionStore, StaleParent

from tests.acceptance.conftest import record_evidence

SKILL_TEMPLATE = """---
name: {name}
category: demo
version: 1.0.0
permissions_backends: [local]
permissions_filesystem: workspace
---

# {name}

## When to Use

Refiner {name}'s concurrent proposal against the same parent revision.

## Verification

- Every fixture task passes.
"""


def _propose_and_evaluate(service: HarnessEvolutionService, name: str):
    patch = service.propose_candidate(
        changes={"type": "add_skill", "markdown": SKILL_TEMPLATE.format(name=name)})
    assert patch.status == "validated", patch.trace
    patch = service.evaluate_candidate(
        patch.patch_id, runner=lambda t: True,
        source_task_ids=[f"{name}_s"], held_out_task_ids=[f"{name}_h"])
    assert patch.status == "evaluated"
    return patch


def _client(store: HarnessEvolutionStore, monkeypatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import routes.evolution_routes as evolution_routes
    monkeypatch.setattr(evolution_routes, "_service",
                        lambda: HarnessEvolutionService(store=store))
    app = FastAPI()
    app.include_router(setup_evolution_routes())
    return TestClient(app)


@pytest.mark.acceptance("A30")
def test_two_threads_promoting_same_parent_one_wins_other_gets_stale_parent(
        tmp_path, monkeypatch, request):
    store = HarnessEvolutionStore(db_path=str(tmp_path / "harness.db"))
    service = HarnessEvolutionService(store=store)
    client = _client(store, monkeypatch)

    parent = service.active_revision()
    patch_a = _propose_and_evaluate(service, "refiner_a")
    patch_b = _propose_and_evaluate(service, "refiner_b")
    assert patch_a.parent_revision == parent.revision_id
    assert patch_b.parent_revision == parent.revision_id

    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def promote(label: str, patch_id: str):
        barrier.wait(timeout=5)
        try:
            revision = service.promote_candidate(patch_id, actor=label)
            outcomes[label] = ("promoted", revision)
        except StaleParent as exc:
            outcomes[label] = ("stale", exc)

    threads = [
        threading.Thread(target=promote, args=("refiner_a", patch_a.patch_id)),
        threading.Thread(target=promote, args=("refiner_b", patch_b.patch_id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a promotion thread hung — CAS deadlocked"

    kinds = sorted(kind for kind, _ in outcomes.values())
    assert kinds == ["promoted", "stale"], outcomes

    winner_label = next(label for label, (kind, _) in outcomes.items() if kind == "promoted")
    loser_label = "refiner_b" if winner_label == "refiner_a" else "refiner_a"
    _, winner_revision = outcomes[winner_label]
    _, stale_error = outcomes[loser_label]

    # No lost update: the store's active revision is EXACTLY the winner's,
    # not some third row and not still the original parent.
    active_now = client.get("/api/harness/revisions").json()
    assert active_now["active_revision_id"] == winner_revision.revision_id
    assert active_now["active_revision_id"] != parent.revision_id
    # The loser's error names the revision that actually won — a caller can
    # rebase onto it instead of guessing.
    assert stale_error.current.revision_id == winner_revision.revision_id

    winner_patch = service.get_patch(
        patch_a.patch_id if winner_label == "refiner_a" else patch_b.patch_id)
    loser_patch = service.get_patch(
        patch_a.patch_id if loser_label == "refiner_a" else patch_b.patch_id)
    assert winner_patch.status == "promoted"
    assert loser_patch.status == "evaluated", (
        "the loser must be untouched by the failed CAS, not silently marked "
        "promoted or corrupted")

    # A30's "safely rebase" option: the loser retries against the winner and
    # its change is applied too — neither refiner's work is lost.
    rebased = service.rebase_and_retry(
        loser_patch.patch_id, stale_error, runner=lambda t: True,
        source_task_ids=[f"{loser_label}_s"], held_out_task_ids=[f"{loser_label}_h"])
    assert rebased.status == "evaluated"
    assert rebased.parent_revision == winner_revision.revision_id
    second_revision = service.promote_candidate(rebased.patch_id, actor=loser_label,
                                                 canary_runs=0)
    assert second_revision.parent_id == winner_revision.revision_id
    final_active = client.get("/api/harness/revisions").json()
    assert final_active["active_revision_id"] == second_revision.revision_id
    # Both refiners' skills ended up in the final refs — no lost update.
    final_row = next(r for r in final_active["revisions"]
                     if r["revision_id"] == second_revision.revision_id)
    assert set(final_row["refs"].get("skills", {})) == {"demo.refiner-a", "demo.refiner-b"}

    record_evidence(request, winner_revision=winner_revision.revision_id,
                    second_revision=second_revision.revision_id,
                    outcomes={k: v[0] for k, v in outcomes.items()})
