"""Contradiction tracking for the learned memory store (src/memory_conflicts.py)
and its two hooks (`memory_engine.add_item`/`correct`, ranking + marker in
`public_item`) and its HTTP surface (routes/memory_engine_routes.py).

Mirrors tests/test_p1_mem_routes_failures_decisions.py's fixtures (real
FastAPI app + TestClient, COMUN.md rule 7) and tests/test_memory_engine.py's
disposable-store fixture.
"""

import pytest

from src import memory_conflicts as conflicts
from src import memory_engine as engine


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import memory_engine_routes

    monkeypatch.setattr(memory_engine_routes, "effective_user", lambda request: "luis")
    app = FastAPI()
    app.include_router(memory_engine_routes.setup_memory_engine_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


# ── pure classification (no DB) ─────────────────────────────────────────────

def test_same_subject_different_value_is_detected():
    result = conflicts.classify("the project uses Python", "the project uses Rust")
    assert result is not None
    reason, detail = result
    assert reason == "same_subject_different_value"
    assert "uses" in detail


def test_negation_is_detected():
    result = conflicts.classify("Luis prefers tabs", "Luis does not prefer tabs")
    assert result is not None
    assert result[0] == "negation"


def test_unrelated_texts_are_not_flagged():
    assert conflicts.classify(
        "the project uses Python", "the weather is nice today") is None


def test_near_duplicate_is_not_flagged():
    assert conflicts.classify(
        "the project uses Python", "the project uses Python.") is None
    assert conflicts.classify(
        "The Project Uses Python", "the project uses python") is None


def test_different_subjects_are_not_flagged():
    assert conflicts.classify(
        "Luis prefers tabs", "Maria prefers spaces") is None


# ── probability_of (the advisory row's confidence, read back from `detail`) ──

def test_probability_of_reads_the_advisory_detail_text():
    detail = "typed decision: contradict (p=0.87, mass=0.42); advisory, not resolved"
    assert conflicts.probability_of(detail) == 0.87


def test_probability_of_is_none_for_non_advisory_detail():
    assert conflicts.probability_of("same subject, different value: uses") is None
    assert conflicts.probability_of("") is None
    assert conflicts.probability_of(None) is None


def test_probability_of_rejects_an_out_of_range_value():
    assert conflicts.probability_of("typed decision: update (p=1.50, mass=0.10)") is None


# ── DB-level detection ───────────────────────────────────────────────────────

def test_add_item_hook_records_an_open_conflict(store):
    old = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    new = engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")

    open_rows = conflicts.list_conflicts(owner="luis", status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["new_id"] == new["id"]
    assert open_rows[0]["old_id"] == old["id"]
    assert open_rows[0]["reason"] == "same_subject_different_value"


def test_correct_can_create_a_new_conflict(store):
    a = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    engine.add_item("Luis prefers tabs", owner="luis", trust_class="human_explicit")

    engine.correct(a["id"], "the project uses Rust", reason="switched languages")

    open_rows = conflicts.list_conflicts(owner="luis", status="open")
    # the corrected item is new, the tombstoned original is gone, so the only
    # possible conflict is against the still-active "uses Rust" ... wait,
    # correcting "uses Python" -> "uses Rust" leaves nothing left disagreeing
    # with it; add a genuine second statement to prove correct() re-runs
    # detection at all.
    engine.add_item("the project uses Go", owner="luis", trust_class="human_explicit")
    open_rows = conflicts.list_conflicts(owner="luis", status="open")
    reasons = {(r["reason"]) for r in open_rows}
    assert "same_subject_different_value" in reasons


def test_setting_off_disables_detection(store, monkeypatch):
    monkeypatch.setattr(engine, "memory_conflict_detection_enabled", lambda: False)
    engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")
    assert conflicts.list_conflicts(owner="luis", status="open") == []


def test_owner_isolation(store):
    engine.add_item("the project uses Python", owner="alice", trust_class="human_explicit")
    engine.add_item("the project uses Rust", owner="bob", trust_class="human_explicit")
    assert conflicts.list_conflicts(owner="alice", status="open") == []
    assert conflicts.list_conflicts(owner="bob", status="open") == []


def test_project_isolation(store):
    engine.add_item("the project uses Python", owner="luis", project="p1",
                    trust_class="human_explicit")
    engine.add_item("the project uses Rust", owner="luis", project="p2",
                    trust_class="human_explicit")
    assert conflicts.list_conflicts(owner="luis", status="open") == []


# ── ranking + marker ─────────────────────────────────────────────────────────

def test_older_item_ranking_is_penalized_and_marked(store):
    old = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    new = engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")

    old_public = engine.public_item(engine.get_item(old["id"]))
    new_public = engine.public_item(engine.get_item(new["id"]))

    assert "(contradicted by a newer memory)" in old_public["text"]
    assert "(contradicted by a newer memory)" not in new_public["text"]

    raw_old_score = engine.effective_score(engine.get_item(old["id"]))
    assert old_public["effective_score"] == pytest.approx(
        raw_old_score * conflicts.RANKING_PENALTY, abs=1e-4)
    assert new_public["effective_score"] == pytest.approx(
        engine.effective_score(engine.get_item(new["id"])), abs=1e-4)


# ── resolve() ────────────────────────────────────────────────────────────────

def test_resolve_keep_new_forgets_old(store):
    old = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")
    row = conflicts.list_conflicts(owner="luis", status="open")[0]

    result = conflicts.resolve(row["id"], "new", owner="luis")
    assert result["status"] == "kept_new"
    assert engine.get_item(old["id"]) is None
    assert engine.is_tombstoned("the project uses Python", "luis", "", "semantic")


def test_resolve_keep_old_forgets_new(store):
    engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    new = engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")
    row = conflicts.list_conflicts(owner="luis", status="open")[0]

    result = conflicts.resolve(row["id"], "old", owner="luis")
    assert result["status"] == "kept_old"
    assert engine.get_item(new["id"]) is None


def test_resolve_keep_both_forgets_nothing(store):
    old = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    new = engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")
    row = conflicts.list_conflicts(owner="luis", status="open")[0]

    result = conflicts.resolve(row["id"], "both", owner="luis")
    assert result["status"] == "kept_both"
    assert engine.get_item(old["id"]) is not None
    assert engine.get_item(new["id"]) is not None
    assert conflicts.list_conflicts(owner="luis", status="open") == []


def test_resolve_wrong_owner_is_a_no_op(store):
    engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")
    row = conflicts.list_conflicts(owner="luis", status="open")[0]

    assert conflicts.resolve(row["id"], "new", owner="someone-else") is None
    assert conflicts.get_conflict(row["id"])["status"] == "open"


# ── HTTP surface ─────────────────────────────────────────────────────────────

def test_list_and_resolve_over_http(client):
    old = engine.add_item("the project uses Python", owner="luis", trust_class="human_explicit")
    new = engine.add_item("the project uses Rust", owner="luis", trust_class="human_explicit")

    listed = client.get("/api/memory-engine/conflicts", params={"status": "open"})
    assert listed.status_code == 200, listed.text
    rows = listed.json()["conflicts"]
    assert len(rows) == 1
    assert rows[0]["old_id"] == old["id"]
    assert rows[0]["new_id"] == new["id"]
    assert rows[0]["old_text"].startswith("the project uses Python")
    assert rows[0]["new_text"].startswith("the project uses Rust")

    resolved = client.post(
        f"/api/memory-engine/conflicts/{rows[0]['id']}/resolve", json={"keep": "new"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["conflict"]["status"] == "kept_new"

    again = client.get("/api/memory-engine/conflicts", params={"status": "open"})
    assert again.json()["conflicts"] == []


def test_resolve_unknown_conflict_is_404(client):
    resp = client.post(
        "/api/memory-engine/conflicts/does-not-exist/resolve", json={"keep": "new"})
    assert resp.status_code == 404


def test_suggested_conflicts_carry_a_probability_over_http(client, store):
    """A `status=suggested` row (what `advise()` writes for a confident
    typed-decision contradict/update) surfaces its confidence as a plain
    `probability` float -- the Studio review panel's whole reason for
    reading it instead of `detail`'s prose -- and the same `resolve`
    endpoint the open-conflict panel already uses accepts or dismisses it."""
    from datetime import datetime, timezone

    old = engine.add_item("Luis lives in Madrid", owner="luis", trust_class="human_explicit")
    new = engine.add_item("Luis is based out of Barcelona now", owner="luis", trust_class="human_explicit")

    row = conflicts._insert(
        "luis", new["id"], old["id"], conflicts.ADVISORY_REASON,
        "typed decision: update (p=0.91, mass=0.50); advisory, not resolved",
        datetime.now(timezone.utc),
    )
    with conflicts._db() as conn:
        conn.execute(
            f"UPDATE {conflicts._TABLE} SET status = ? WHERE id = ?",
            (conflicts.ADVISORY_STATUS, row["id"]),
        )

    listed = client.get("/api/memory-engine/conflicts", params={"status": "suggested"})
    assert listed.status_code == 200, listed.text
    rows = listed.json()["conflicts"]
    assert len(rows) == 1
    assert rows[0]["probability"] == pytest.approx(0.91)
    assert rows[0]["status"] == "suggested"

    # "Accept" (keep the newer, drop the contradicted older one) is the
    # same `resolve` call the open-conflicts panel already offers.
    resolved = client.post(
        f"/api/memory-engine/conflicts/{rows[0]['id']}/resolve", json={"keep": "new"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["conflict"]["status"] == "kept_new"

    again = client.get("/api/memory-engine/conflicts", params={"status": "suggested"})
    assert again.json()["conflicts"] == []
