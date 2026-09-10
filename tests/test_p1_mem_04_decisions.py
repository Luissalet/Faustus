"""MEM-04 — continuidad de proyecto y decisiones.

`src/memory_decisions.py` records a technical decision (with its discarded
alternatives and the artifacts it touches) as a memory_engine item —
independent of any one session or model. Three things proven here:

1. The decision and its reason survive a model swap / a brand new session —
   `list_decisions` finds it without touching any transcript.
2. Invalidating a decision keeps its row (and its reason) instead of
   deleting it — `list_decisions(include_invalidated=True)` still shows it.
3. An invalidated decision is excluded from the DEFAULT (current-truth) view.
"""

import pytest

from src import memory_engine as engine
from src import memory_decisions as decisions


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


def test_record_decision_keeps_alternatives_and_artifacts(store):
    item = decisions.record_decision(
        "Use SQLite over Postgres for the context store",
        owner="luis", project="faustus",
        alternatives=["Postgres", "DuckDB"],
        artifact_refs=["src/context_engine/store.py"],
        scope="architecture",
    )
    assert item["type"] == "decision"
    assert item["provenance"]["alternatives_discarded"] == ["Postgres", "DuckDB"]
    assert item["provenance"]["artifact_refs"] == ["src/context_engine/store.py"]
    assert item["provenance"]["decision_scope"] == "architecture"


def test_decision_survives_a_model_swap_and_a_new_session(store):
    """The acceptance criterion itself: no transcript, no session, just the
    decision and its reason, found by an entirely different session."""
    decisions.record_decision("Pin the tokenizer to cl100k_base", owner="luis",
                              project="faustus", session_id="session-with-model-A")

    # A "new session" (and implicitly a different model, since nothing here
    # is keyed by model at all) looks the project up fresh.
    found = decisions.list_decisions(owner="luis", project="faustus")
    assert any("cl100k_base" in d["text"] for d in found)


def test_invalidating_keeps_the_row_and_the_reason(store):
    item = decisions.record_decision("Use REST instead of GraphQL", owner="luis",
                                     project="faustus")
    updated = decisions.invalidate_decision(
        item["id"], "GraphQL became necessary once mobile needed partial queries",
        superseded_by="decision-xyz")
    assert updated["status"] == "deprecated"
    assert updated["provenance"]["invalidated"] is True
    assert "partial queries" in updated["provenance"]["invalidated_reason"]
    assert updated["provenance"]["superseded_by"] == "decision-xyz"

    # Still resolvable directly — never deleted.
    assert engine.get_item(item["id"]) is not None


def test_invalidated_decision_is_excluded_from_the_default_view_but_not_the_full_one(store):
    item = decisions.record_decision("Use REST instead of GraphQL", owner="luis",
                                     project="faustus")
    decisions.invalidate_decision(item["id"], "premise changed")

    current = decisions.list_decisions(owner="luis", project="faustus")
    assert all(d["id"] != item["id"] for d in current)

    full = decisions.list_decisions(owner="luis", project="faustus",
                                    include_invalidated=True)
    assert any(d["id"] == item["id"] for d in full)


def test_decisions_are_isolated_per_project(store):
    decisions.record_decision("Use SQLite", owner="luis", project="proj-a")
    found_b = decisions.list_decisions(owner="luis", project="proj-b")
    assert found_b == []


def test_invalidate_unknown_id_returns_none(store):
    assert decisions.invalidate_decision("does-not-exist", "reason") is None
