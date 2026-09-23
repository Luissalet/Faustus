"""`entities.revalidate` — the one-off cleanup of graph rows written before the
name filter and the closed relation vocabulary existed.

It only ever HIDES entities and RETRACTS model relations (never deletes),
leaves anything a person touched alone, says what it did, can be undone, and
runs once per version when driven by the maintenance task.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402

OWNER = "alice"


@pytest.fixture()
def store(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    yield tmp_path
    brain_db.use_dir(None)


def _seed():
    ada = entities.upsert_entity(OWNER, "Ada", type="person")
    labs = entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    en = entities.upsert_entity(OWNER, "En")
    todo = entities.upsert_entity(OWNER, "Todo", type="concept")
    digits = entities.upsert_entity(OWNER, "2024")
    prefixed = entities.upsert_entity(OWNER, "En Bluehaven")
    locked = entities.upsert_entity(OWNER, "Para", type="project")
    entities.update_entity(locked["id"], summary="Our project named Para.", summary_locked=True)
    target = entities.upsert_entity(OWNER, "Luego", type="project")
    dup = entities.upsert_entity(OWNER, "Luego App", type="project")
    entities.merge_entities(target["id"], dup["id"])
    good_rule = entities.add_relation(OWNER, ada["id"], "works_at", dst_id=labs["id"])
    good_llm = entities.add_relation(OWNER, ada["id"], "uses", dst_value="Python", method="llm")
    bad_llm_1 = entities.add_relation(OWNER, ada["id"], "intocable", dst_value="carpetas", method="llm")
    bad_llm_2 = entities.add_relation(OWNER, ada["id"], "has_account_named", dst_value="adita",
                                      method="llm")
    manual = entities.add_relation(OWNER, ada["id"], "mentors", dst_value="Bruno", method="manual")
    return {
        "ada": ada, "labs": labs, "en": en, "todo": todo, "digits": digits,
        "prefixed": prefixed, "locked": locked, "target": target, "dup": dup,
        "good_rule": good_rule, "good_llm": good_llm, "bad_llm_1": bad_llm_1,
        "bad_llm_2": bad_llm_2, "manual": manual,
    }


def _relation(rel_id):
    for rel in entities.list_relations(OWNER, include_retracted=True):
        if rel["id"] == rel_id:
            return rel
    return None


def test_revalidate_hides_junk_entities_and_keeps_real_or_human_ones(store):
    seed = _seed()
    me = entities.self_entity(OWNER)
    report = entities.revalidate(OWNER)

    hidden = {e["id"] for e in entities.list_entities(OWNER, include_hidden=True) if e["hidden"]}
    for key in ("en", "todo", "digits", "prefixed"):
        assert seed[key]["id"] in hidden, key
    for key in ("ada", "labs", "locked", "target"):
        assert seed[key]["id"] not in hidden, key
    assert me["id"] not in hidden
    assert set(report["hidden"]) == {seed[k]["id"] for k in ("en", "todo", "digits", "prefixed")}
    assert report["hidden_count"] == 4


def test_revalidate_retracts_only_model_relations_outside_the_vocabulary(store):
    seed = _seed()
    report = entities.revalidate(OWNER)
    assert set(report["retracted"]) == {seed["bad_llm_1"]["id"], seed["bad_llm_2"]["id"]}
    assert report["retracted_count"] == 2
    for key in ("bad_llm_1", "bad_llm_2"):
        row = _relation(seed[key]["id"])
        assert row is not None  # never deleted
        assert row["status"] == "retracted"
    for key in ("good_rule", "good_llm", "manual"):
        assert _relation(seed[key]["id"])["status"] == "active", key


def test_retracted_relations_leave_every_normal_read(store):
    seed = _seed()
    entities.revalidate(OWNER)
    ada = seed["ada"]["id"]
    ids = {r["id"] for r in entities.list_relations(OWNER, entity_id=ada)}
    assert seed["bad_llm_1"]["id"] not in ids
    ids_as_of = {r["id"] for r in entities.list_relations(OWNER, entity_id=ada,
                                                          as_of="2999-01-01T00:00:00Z")}
    assert seed["bad_llm_1"]["id"] not in ids_as_of
    prof = entities.profile(ada)
    assert all(r["rel"] not in ("intocable", "has_account_named") for r in prof["relations"])
    assert all(r["rel"] not in ("intocable", "has_account_named") for r in prof["history"])


def test_revalidate_dry_run_changes_nothing(store):
    seed = _seed()
    report = entities.revalidate(OWNER, dry_run=True)
    assert report["dry_run"] is True
    assert report["hidden_count"] == 4 and report["retracted_count"] == 2
    assert entities.get_entity(seed["en"]["id"])["hidden"] is False
    assert _relation(seed["bad_llm_1"]["id"])["status"] == "active"


def test_revalidate_is_idempotent(store):
    _seed()
    entities.revalidate(OWNER)
    again = entities.revalidate(OWNER)
    assert again["hidden_count"] == 0 and again["retracted_count"] == 0


def test_undo_revalidate_restores_everything(store):
    seed = _seed()
    entities.revalidate(OWNER)
    undone = entities.undo_revalidate(OWNER)
    assert undone["unhidden_count"] == 4 and undone["restored_count"] == 2
    assert entities.get_entity(seed["en"]["id"])["hidden"] is False
    assert _relation(seed["bad_llm_1"]["id"])["status"] == "active"


def test_revalidate_if_needed_runs_once_per_version(store, monkeypatch):
    seed = _seed()
    first = entities.revalidate_if_needed(OWNER)
    assert first is not None and first["hidden_count"] == 4
    assert entities.revalidate_if_needed(OWNER) is None  # marker stored

    # a person un-hides one of them: a later version must not hide it again
    entities.set_hidden(seed["todo"]["id"], False)
    monkeypatch.setattr(entities, "REVALIDATE_VERSION", entities.REVALIDATE_VERSION + 1)
    second = entities.revalidate_if_needed(OWNER)
    assert second is not None
    assert seed["todo"]["id"] not in second["hidden"]
    assert entities.get_entity(seed["todo"]["id"])["hidden"] is False


def test_revalidate_is_per_owner(store):
    _seed()
    other = entities.upsert_entity("bruno", "En")
    entities.revalidate(OWNER)
    assert entities.get_entity(other["id"])["hidden"] is False
    assert entities.revalidate_if_needed("bruno")["hidden"] == [other["id"]]


def test_revalidate_never_raises_on_empty_owner(store):
    report = entities.revalidate("")
    assert report["hidden_count"] == 0 and report["retracted_count"] == 0
