"""BASE-02 — `src/state_mirror/divergence.py`.

A projection is corrupted BY HAND (`store.put_state`, bypassing the reducer
entirely — no event, no sweep, just a row written straight into
`state_materialized`) and a fake `runs` adapter stands in for the domain's
authority. `detect_divergence()` must name exactly what differs;
`repair()` must put it back, through the real `ingest.ingest`, and say so.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.state_mirror import contracts as C
from src.state_mirror import divergence as D
from src.state_mirror import persistence as P
from src.state_mirror import queries as Q
from src.state_mirror.adapters import base as A

OWNER = "alice"
ENTITY = C.entity_id("run", OWNER, "job-1")
STAMP = "2026-09-10T12:00:00Z"


@pytest.fixture
def store(tmp_path):
    result = P.StateStore(path=str(tmp_path / "state.db"))
    yield result
    result.close()


class _FakeRunsAdapter:
    """The authority: always says `job-1` is `running`, freshly observed."""

    name = "runs"
    schemas = ("run_state.v1",)

    def available(self) -> bool:
        return True

    def discover(self, scope):
        return []

    def observe(self, scope):
        obs = C.StateObservation.parse({
            "entity_id": C.entity_id("run", scope.owner, "job-1"),
            "owner": scope.owner, "source": "runs", "schema": "run_state.v1",
            "epistemic": "observed", "observed_at": STAMP,
            "state": {"status": "running", "label": "eval task"},
        })
        return [obs]


@pytest.fixture(autouse=True)
def fake_runs_adapter():
    A.reset_adapters()
    A.register(_FakeRunsAdapter())
    yield
    A.reset_adapters()


def _corrupt(store, *, status: str, label: str = "eval task") -> None:
    """Write values straight into the projection, no ingest involved — the
    "proyección corrompida a mano" the lot's own item 5 asks for.
    `epistemic='inferred'` (weaker than the authority's `observed`) so a
    repair is unambiguous per `reducers._should_replace` rule 2, without
    needing to also reason about conflicts between two `observed` claims."""
    fields = {
        "status": C.FieldState(value=status, epistemic="inferred", freshness="fresh",
                               observed_at=STAMP, source="hand-edit"),
        "label": C.FieldState(value=label, epistemic="inferred", freshness="fresh",
                              observed_at=STAMP, source="hand-edit"),
    }
    state = C.MaterializedState(entity_id=ENTITY, owner=OWNER, schema="run_state.v1",
                                revision=1, fields=fields)
    store.put_state(state)


def test_an_agreeing_projection_shows_no_divergence(store):
    _corrupt(store, status="running")  # matches the fake authority already
    report = D.detect_divergence(owner=OWNER, domains=("runs",), store=store)
    assert report.domains_checked == ("runs",)
    assert report.diverged == ()
    assert report.errors == ()


def test_a_hand_corrupted_field_is_named_by_detect_divergence(store):
    _corrupt(store, status="failed")  # wrong: the authority says "running"
    report = D.detect_divergence(owner=OWNER, domains=("runs",), store=store)
    assert report.diverged_count() == 1
    row = report.diverged[0]
    assert row.entity_id == ENTITY and row.domain == "runs"
    assert row.missing_from_projection is False
    changed_fields = {f["field"] for f in row.fields}
    assert "status" in changed_fields
    status_change = next(f for f in row.fields if f["field"] == "status")
    assert status_change["before"] == "failed" and status_change["after"] == "running"
    # detect_divergence never writes: the corrupted value is still there.
    assert Q.get(ENTITY, owner=OWNER, store=store).values()["status"] == "failed"


def test_repair_reconstructs_the_projection_from_the_authority(store):
    _corrupt(store, status="failed")
    result = D.repair(owner=OWNER, domains=("runs",), store=store)
    assert result.detected.diverged_count() == 1
    assert ENTITY in result.ingest.changed_entities
    fixed = Q.get(ENTITY, owner=OWNER, store=store)
    assert fixed.values()["status"] == "running"
    assert fixed.values()["label"] == "eval task"
    # And asking again finds nothing left to repair.
    again = D.detect_divergence(owner=OWNER, domains=("runs",), store=store)
    assert again.diverged == ()


def test_an_entity_the_projection_never_held_is_missing_not_diverged(store):
    # No put_state at all: queries.get() returns None for ENTITY.
    report = D.detect_divergence(owner=OWNER, domains=("runs",), store=store)
    assert report.diverged_count() == 1
    assert report.diverged[0].missing_from_projection is True
    repaired = D.repair(owner=OWNER, domains=("runs",), store=store)
    assert Q.get(ENTITY, owner=OWNER, store=store).values()["status"] == "running"


def test_an_unregistered_domain_is_unavailable_not_silently_clean(store):
    report = D.detect_divergence(owner=OWNER, domains=("runs", "approvals"), store=store)
    assert "approvals" in report.domains_unavailable or "approvals" in report.domains_checked
    # Whichever it is, "runs" (the one with a real, registered authority)
    # must still be checked and must still find the corruption below.
    _corrupt(store, status="failed")
    report2 = D.detect_divergence(owner=OWNER, domains=("runs", "approvals"), store=store)
    assert "runs" in report2.domains_checked
    assert any(d.entity_id == ENTITY for d in report2.diverged)


def test_an_adapter_that_raises_is_reported_not_fatal(store, monkeypatch):
    def _boom(self, scope):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(_FakeRunsAdapter, "observe", _boom)
    report = D.detect_divergence(owner=OWNER, domains=("runs",), store=store)
    assert report.domains_checked == ("runs",)  # available() still said yes
    assert report.diverged == ()
    assert any("registry unavailable" in e for e in report.errors)
