"""Tests for src/brain/entities.py — typed entities, time-windowed relations."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402
from src import memory_engine as engine  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# upsert_entity — alias/fold-aware
# ---------------------------------------------------------------------------


def test_upsert_creates_new_entity(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    assert ada["name"] == "Ada"
    assert ada["type"] == "person"
    assert ada["fold_name"] == "ada"
    assert ada["aliases"] == []


def test_upsert_returns_existing_on_same_name(store):
    a = entities.upsert_entity("alice", "Ada")
    b = entities.upsert_entity("alice", "ADA")
    assert a["id"] == b["id"]


def test_upsert_returns_existing_on_alias_match(store):
    a = entities.upsert_entity("alice", "Cordera Labs", aliases=["Cordera"])
    b = entities.upsert_entity("alice", "cordera")
    assert a["id"] == b["id"]


def test_upsert_extends_aliases_without_duplicating(store):
    a = entities.upsert_entity("alice", "Ada", aliases=["Ada Lovelace"])
    b = entities.upsert_entity("alice", "ada", aliases=["Ada Lovelace", "AL"])
    assert b["id"] == a["id"]
    assert "AL" in b["aliases"]
    assert b["aliases"].count("Ada Lovelace") == 1


def test_upsert_different_owners_are_distinct(store):
    a = entities.upsert_entity("alice", "Ada")
    b = entities.upsert_entity("bob", "Ada")
    assert a["id"] != b["id"]


def test_upsert_empty_name_raises(store):
    with pytest.raises(entities.BrainEntityError):
        entities.upsert_entity("alice", "   ")


def test_upsert_unknown_type_falls_back_to_other(store):
    e = entities.upsert_entity("alice", "Thing", type="nonsense")
    assert e["type"] == "other"


# ---------------------------------------------------------------------------
# get_entity / update_entity / set_hidden
# ---------------------------------------------------------------------------


def test_update_entity_renames_and_updates_fold_name(store):
    e = entities.upsert_entity("alice", "Ada")
    updated = entities.update_entity(e["id"], name="Ada Lovelace")
    assert updated["name"] == "Ada Lovelace"
    assert updated["fold_name"] == "ada lovelace"


def test_update_entity_rejects_bad_type(store):
    e = entities.upsert_entity("alice", "Ada")
    with pytest.raises(entities.BrainEntityError):
        entities.update_entity(e["id"], type="nope")


def test_update_entity_summary_locks_and_bumps_timestamp(store):
    e = entities.upsert_entity("alice", "Ada")
    updated = entities.update_entity(e["id"], summary="Ada is great.", summary_locked=True)
    assert updated["summary"] == "Ada is great."
    assert updated["summary_locked"] is True
    assert updated["summary_updated_at"]


def test_update_entity_unknown_id_returns_none(store):
    assert entities.update_entity("nope", name="X") is None


def test_set_hidden(store):
    e = entities.upsert_entity("alice", "Ada")
    hidden = entities.set_hidden(e["id"], True)
    assert hidden["hidden"] is True
    shown = entities.set_hidden(e["id"], False)
    assert shown["hidden"] is False


# ---------------------------------------------------------------------------
# list_entities
# ---------------------------------------------------------------------------


def test_list_entities_filters_by_type_and_query(store):
    entities.upsert_entity("alice", "Ada", type="person")
    entities.upsert_entity("alice", "Cordera Labs", type="organization")
    only_people = entities.list_entities("alice", type="person")
    assert [e["name"] for e in only_people] == ["Ada"]
    found = entities.list_entities("alice", q="cordera")
    assert [e["name"] for e in found] == ["Cordera Labs"]


def test_list_entities_excludes_hidden_by_default(store):
    e = entities.upsert_entity("alice", "Ada")
    entities.set_hidden(e["id"], True)
    assert entities.list_entities("alice") == []
    assert len(entities.list_entities("alice", include_hidden=True)) == 1


def test_list_entities_carries_mentions_and_relations(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    labs = entities.upsert_entity("alice", "Cordera Labs", type="organization")
    entities.add_mention("alice", ada["id"], "mem:abc")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=labs["id"])
    row = entities.list_entities("alice", type="person")[0]
    assert row["mentions"] == ["mem:abc"]
    assert len(row["relations"]) == 1


# ---------------------------------------------------------------------------
# add_relation — functional supersede with windows
# ---------------------------------------------------------------------------


def test_functional_relation_supersedes_previous(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    cordera = entities.upsert_entity("alice", "Cordera Labs", type="organization")
    bluehaven = entities.upsert_entity("alice", "Bluehaven", type="organization")

    first = entities.add_relation("alice", ada["id"], "works_at", dst_id=cordera["id"],
                                  valid_from="2024-01-01T00:00:00Z")
    second = entities.add_relation("alice", ada["id"], "works_at", dst_id=bluehaven["id"],
                                   valid_from="2025-03-01T00:00:00Z")

    closed = entities.list_relations("alice", entity_id=ada["id"], include_closed=True)
    old = next(r for r in closed if r["id"] == first["id"])
    assert old["status"] == "superseded"
    assert old["valid_until"] == "2025-03-01T00:00:00Z"
    assert old["superseded_by"] == second["id"]

    active = entities.list_relations("alice", entity_id=ada["id"], include_closed=False)
    assert [r["id"] for r in active] == [second["id"]]


def test_functional_relation_reasserting_same_dst_does_not_close(store):
    ada = entities.upsert_entity("alice", "Ada")
    cordera = entities.upsert_entity("alice", "Cordera Labs")
    first = entities.add_relation("alice", ada["id"], "works_at", dst_id=cordera["id"])
    second = entities.add_relation("alice", ada["id"], "works_at", dst_id=cordera["id"])
    active = entities.list_relations("alice", entity_id=ada["id"], include_closed=False)
    assert {r["id"] for r in active} == {first["id"], second["id"]}


def test_non_functional_relation_does_not_supersede(store):
    ada = entities.upsert_entity("alice", "Ada")
    entities.add_relation("alice", ada["id"], "prefers", dst_value="tabs")
    entities.add_relation("alice", ada["id"], "prefers", dst_value="spaces")
    active = entities.list_relations("alice", entity_id=ada["id"], include_closed=False)
    assert len(active) == 2
    assert all(r["status"] == "active" for r in active)


def test_add_relation_requires_dst(store):
    ada = entities.upsert_entity("alice", "Ada")
    with pytest.raises(entities.BrainEntityError):
        entities.add_relation("alice", ada["id"], "works_at")


def test_relation_vocabulary_is_folded_not_restricted(store):
    ada = entities.upsert_entity("alice", "Ada")
    rel = entities.add_relation("alice", ada["id"], "Trabaja En", dst_value="algún sitio")
    assert rel["rel"] == "trabaja_en"


# ---------------------------------------------------------------------------
# list_relations as_of
# ---------------------------------------------------------------------------


def test_list_relations_as_of_returns_the_window_that_covered_that_instant(store):
    ada = entities.upsert_entity("alice", "Ada")
    cordera = entities.upsert_entity("alice", "Cordera Labs")
    bluehaven = entities.upsert_entity("alice", "Bluehaven")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=cordera["id"],
                          valid_from="2024-01-01T00:00:00Z")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=bluehaven["id"],
                          valid_from="2025-03-01T00:00:00Z")

    at_2024 = entities.list_relations("alice", entity_id=ada["id"], as_of=_dt("2024-06-01T00:00:00Z"))
    assert [r["dst"] for r in at_2024] == [cordera["id"]]

    at_2025 = entities.list_relations("alice", entity_id=ada["id"], as_of=_dt("2025-06-01T00:00:00Z"))
    assert [r["dst"] for r in at_2025] == [bluehaven["id"]]


# ---------------------------------------------------------------------------
# profile()
# ---------------------------------------------------------------------------


def test_profile_facts_and_relations_with_as_of(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    cordera = entities.upsert_entity("alice", "Cordera Labs")
    bluehaven = entities.upsert_entity("alice", "Bluehaven")

    item = engine.add_item("Ada works at Cordera Labs", owner="alice",
                           trust_class="human_explicit",
                           valid_from="2024-01-01T00:00:00Z",
                           valid_until="2025-03-01T00:00:00Z")
    entities.add_mention("alice", ada["id"], f"mem:{item['id']}")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=cordera["id"],
                          valid_from="2024-01-01T00:00:00Z",
                          valid_until="2025-03-01T00:00:00Z")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=bluehaven["id"],
                          valid_from="2025-03-01T00:00:00Z")

    prof_now = entities.profile(ada["id"], as_of=_dt("2025-06-01T00:00:00Z"))
    assert prof_now["entity"]["name"] == "Ada"
    assert len(prof_now["facts"]) == 1
    assert prof_now["facts"][0]["valid_now"] is False  # the Cordera fact expired
    valid_at_relations = [r for r in prof_now["relations"] if r["valid_at"]]
    assert len(valid_at_relations) == 1
    assert valid_at_relations[0]["dst_name"] == "Bluehaven"
    assert len(prof_now["history"]) == 1

    prof_then = entities.profile(ada["id"], as_of=_dt("2024-06-01T00:00:00Z"))
    assert prof_then["facts"][0]["valid_now"] is True
    valid_then = [r for r in prof_then["relations"] if r["valid_at"]]
    assert valid_then[0]["dst_name"] == "Cordera Labs"


def test_profile_unknown_entity_raises(store):
    with pytest.raises(entities.BrainEntityError):
        entities.profile("nope")


# ---------------------------------------------------------------------------
# merge_entities
# ---------------------------------------------------------------------------


def test_merge_entities_repoints_relations_and_mentions(store):
    dup = entities.upsert_entity("alice", "Ada L")
    keep = entities.upsert_entity("alice", "Ada")
    labs = entities.upsert_entity("alice", "Cordera Labs")
    entities.add_relation("alice", dup["id"], "works_at", dst_id=labs["id"])
    entities.add_mention("alice", dup["id"], "mem:xyz")

    merged = entities.merge_entities(keep["id"], dup["id"])
    assert merged["id"] == keep["id"]
    assert "Ada L" in merged["aliases"]

    active = entities.list_relations("alice", entity_id=keep["id"], include_closed=False)
    assert active[0]["src"] == keep["id"]
    assert entities.sources_for(keep["id"]) == ["mem:xyz"]

    gone = entities.get_entity(dup["id"])
    assert gone["hidden"] is True
    assert gone["merged_into"] == keep["id"]


def test_merge_entities_requires_two_distinct_existing_ids(store):
    keep = entities.upsert_entity("alice", "Ada")
    with pytest.raises(entities.BrainEntityError):
        entities.merge_entities(keep["id"], keep["id"])
    with pytest.raises(entities.BrainEntityError):
        entities.merge_entities(keep["id"], "does-not-exist")


def test_upsert_after_merge_resolves_to_keep_entity(store):
    dup = entities.upsert_entity("alice", "Ada L")
    keep = entities.upsert_entity("alice", "Ada")
    entities.merge_entities(keep["id"], dup["id"])
    resolved = entities.upsert_entity("alice", "ada l")
    assert resolved["id"] == keep["id"]


# ---------------------------------------------------------------------------
# self_entity
# ---------------------------------------------------------------------------


def test_self_entity_is_idempotent_and_aliased(store):
    a = entities.self_entity("alice")
    b = entities.self_entity("alice")
    assert a["id"] == b["id"]
    assert a["type"] == "person"
    assert set(a["aliases"]) >= {"yo", "me"}


def test_self_entity_distinct_per_owner(store):
    a = entities.self_entity("alice")
    b = entities.self_entity("bob")
    assert a["id"] != b["id"]


# ---------------------------------------------------------------------------
# entities_in_text
# ---------------------------------------------------------------------------


def test_entities_in_text_matches_names_and_aliases_fold_insensitive(store):
    entities.upsert_entity("alice", "Cordera Labs", aliases=["Cordera"])
    found = entities.entities_in_text("alice", "ADA visited CORDERA yesterday")
    # "Cordera" alias matches even though only "Ada" is capitalized oddly.
    names = {e["name"] for e in found}
    assert "Cordera Labs" in names


def test_entities_in_text_prefers_longest_match(store):
    entities.upsert_entity("alice", "Ada")
    entities.upsert_entity("alice", "Ada Lovelace")
    found = entities.entities_in_text("alice", "Ada Lovelace works here")
    names = [e["name"] for e in found]
    assert "Ada Lovelace" in names


def test_entities_in_text_word_boundary_no_partial_match(store):
    entities.upsert_entity("alice", "Ada")
    found = entities.entities_in_text("alice", "Adapter pattern is useful")
    assert found == []


def test_entities_in_text_empty_text(store):
    assert entities.entities_in_text("alice", "") == []


# ---------------------------------------------------------------------------
# graph / stats
# ---------------------------------------------------------------------------


def test_graph_nodes_and_edges(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    labs = entities.upsert_entity("alice", "Cordera Labs", type="organization")
    entities.add_relation("alice", ada["id"], "works_at", dst_id=labs["id"])
    g = entities.graph("alice")
    ids = {n["id"] for n in g["nodes"]}
    assert f"ent:{ada['id']}" in ids and f"ent:{labs['id']}" in ids
    assert len(g["edges"]) == 1
    edge = g["edges"][0]
    assert edge["from"] == f"ent:{ada['id']}"
    assert edge["to"] == f"ent:{labs['id']}"
    assert edge["kind"] == "works_at"
    assert edge["valid_now"] is True


def test_graph_skips_literal_value_relations(store):
    ada = entities.upsert_entity("alice", "Ada")
    entities.add_relation("alice", ada["id"], "prefers", dst_value="tabs")
    g = entities.graph("alice")
    assert g["edges"] == []


def test_stats_counts(store):
    ada = entities.upsert_entity("alice", "Ada", type="person")
    entities.upsert_entity("alice", "Cordera Labs", type="organization")
    hidden = entities.upsert_entity("alice", "Ghost")
    entities.set_hidden(hidden["id"], True)
    entities.add_relation("alice", ada["id"], "uses", dst_value="Python")

    s = entities.stats("alice")
    assert s["entities"] == 2
    assert s["hidden"] == 1
    assert s["by_type"]["person"] == 1
    assert s["relations_active"] == 1
    assert s["relations_total"] == 1
