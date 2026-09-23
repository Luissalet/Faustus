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


def test_version_two_hides_lowercase_common_nouns_but_keeps_lowercase_tools(tmp_path, monkeypatch):
    from src.brain import db, entities
    db.use_dir(str(tmp_path / "brain2"))
    try:
        noun = entities.upsert_entity("alice", "chats", type="concept")
        tool = entities.upsert_entity("alice", "pytest", type="tool")
        named = entities.upsert_entity("alice", "Cordera Labs", type="organization")
        report = entities.revalidate("alice")
        assert noun["id"] in report["hidden"]
        assert tool["id"] not in report["hidden"]
        assert named["id"] not in report["hidden"]
    finally:
        db.use_dir(None)


def test_self_entity_adopts_display_name_and_folds_in_the_named_person(tmp_path, monkeypatch):
    from src.brain import db, entities
    import src.settings as settings
    db.use_dir(str(tmp_path / "brain3"))
    try:
        stray = entities.upsert_entity("alice", "Alice", type="person")
        me = entities.self_entity("alice")
        assert me["name"] == "Yo"
        real_get = settings.get_setting
        monkeypatch.setattr(settings, "get_setting",
                            lambda k, d=None: "Alice" if k == "owner_display_name" else real_get(k, d))
        me = entities.self_entity("alice")
        assert me["name"] == "Alice"
        assert entities.get_entity(stray["id"])["merged_into"] == me["id"]
        # "the user" is the same node
        assert entities.upsert_entity("alice", "User", type="person")["id"] == me["id"]
    finally:
        db.use_dir(None)


def test_self_entity_folds_in_a_stray_user_node(tmp_path):
    from src.brain import db, entities
    db.use_dir(str(tmp_path / "brain4"))
    try:
        # a row written before "user" was a self alias
        stray = entities.upsert_entity("alice", "User", type="person")
        me = entities.self_entity("alice")
        folded = entities.get_entity(stray["id"])
        # either adopted as the self node (renamed) or merged into it
        assert me["name"] == "Yo"
        assert folded["id"] == me["id"] or folded["merged_into"] == me["id"]
        assert len([e for e in entities.list_entities("alice", type="person")]) == 1
    finally:
        db.use_dir(None)


def test_same_edge_twice_is_one_edge_and_model_synonym_duplicates_retract(tmp_path):
    from src.brain import db, entities
    db.use_dir(str(tmp_path / "brain5"))
    try:
        ada = entities.upsert_entity("alice", "Ada", type="person")
        blue = entities.upsert_entity("alice", "Bluehaven", type="organization")
        r1 = entities.add_relation("alice", ada["id"], "works_at", dst_id=blue["id"],
                                   evidence=["mem:1"], method="rule")
        r2 = entities.add_relation("alice", ada["id"], "works_at", dst_id=blue["id"],
                                   evidence=["mem:2"], method="llm")
        assert r1["id"] == r2["id"]
        assert set(r2["evidence"]) == {"mem:1", "mem:2"}
        assert r2["method"] == "rule"
        # a legacy synonym row written before canonicalisation
        with entities.db() as conn:
            conn.execute("UPDATE relations SET rel = 'works_at' WHERE id = ?", (r1["id"],))
            conn.execute(
                "INSERT INTO relations (id, owner, project, src, rel, dst, dst_value, valid_from, "
                "valid_until, asserted_at, evidence, confidence, method, status, superseded_by, "
                "created_at, updated_at) VALUES ('dup','alice','',?,'works_for',?,'','','','',"
                "'[]',0.5,'llm','active','','9999','9999')", (ada["id"], blue["id"]))
        report = entities.revalidate("alice")
        assert "dup" in report["retracted"]
        live = entities.list_relations("alice", entity_id=ada["id"], include_closed=False)
        assert [r["id"] for r in live] == [r1["id"]]
    finally:
        db.use_dir(None)


def test_rule_duplicates_merge_into_one_edge_and_type_upgrades(tmp_path):
    from src.brain import db, entities
    db.use_dir(str(tmp_path / "brain6"))
    try:
        ada = entities.upsert_entity("alice", "Ada")
        assert ada["type"] == "other"
        assert entities.upsert_entity("alice", "Ada", type="person")["type"] == "person"
        blue = entities.upsert_entity("alice", "Bluehaven", type="organization")
        keep = entities.add_relation("alice", ada["id"], "works_at", dst_id=blue["id"], evidence=["mem:1"])
        with entities.db() as conn:  # a legacy duplicate written before one-edge-per-fact
            conn.execute(
                "INSERT INTO relations (id, owner, project, src, rel, dst, dst_value, valid_from, "
                "valid_until, asserted_at, evidence, confidence, method, status, superseded_by, "
                "created_at, updated_at) VALUES ('dup2','alice','',?,'works_at',?,'','','','',"
                "'[\"mem:2\"]',0.5,'rule','active','','9999','9999')", (ada["id"], blue["id"]))
        report = entities.revalidate("alice")
        assert "dup2" in report["retracted"]
        live = entities.list_relations("alice", entity_id=ada["id"], include_closed=False)
        assert [r["id"] for r in live] == [keep["id"]]
        assert set(live[0]["evidence"]) == {"mem:1", "mem:2"}
    finally:
        db.use_dir(None)
