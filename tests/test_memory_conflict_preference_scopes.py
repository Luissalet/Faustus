"""Preference qualifiers must survive write-time contradiction detection."""
import pytest

from src import memory_conflicts as conflicts
from src import memory_engine as engine


@pytest.mark.parametrize("old,new", [
    ("Alice prefers tabs for JavaScript", "Alice prefers spaces for Python"),
    ("Alice prefers tabs for JavaScript and TypeScript", "Alice prefers pytest for Python tests"),
    ("Alice prefers quiet for work", "Alice prefers music for travel"),
    ("Alice prefiere silencio para trabajar", "Alice prefiere musica para viajar"),
])
def test_preferences_in_distinct_explicit_contexts_coexist(old, new):
    assert conflicts.classify(old, new) is None
    assert conflicts.classify(new, old) is None


@pytest.mark.parametrize("old,new", [
    ("Alice prefers tabs for Python", "Alice prefers spaces for Python"),
    ("Alice prefers tabs for JS", "Alice prefers spaces for JavaScript"),
    ("Alice prefers tabs for TS", "Alice prefers spaces for TypeScript"),
    ("Alice prefiere silencio para trabajar", "Alice prefiere musica para trabajar"),
    ("Atlas uses Python", "Atlas uses JavaScript"),
])
def test_actual_changes_in_same_context_still_conflict(old, new):
    assert conflicts.classify(old, new)[0] == "same_subject_different_value"


def test_write_path_does_not_penalize_a_different_preference_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    try:
        original = engine.add_item("Alice prefers tabs for JavaScript", owner="alice")
        engine.add_item("Alice prefers pytest for Python tests", owner="alice")
        assert conflicts.open_conflict_for(original["id"], "alice") is None
        engine.add_item("Alice prefers spaces for JavaScript", owner="alice")
        assert conflicts.open_conflict_for(original["id"], "alice") is not None
    finally:
        engine.reset_vector_store()
