"""Regression tests for supersede chronology (review 1, finding 9) and the
never-truncated saved window behind `unsupersede` (review 1, MINOR).

A historical fact added after the current one ("Ada works at Cordera Labs
since 2019" stored after "Ada works at Bluehaven since 2024") must never
close the current fact, and no supersede may ever write a ``valid_until``
earlier than the item's own ``valid_from`` — in `memory_conflicts` and in
`entities.add_relation` alike.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_conflicts as conflicts  # noqa: E402
from src import memory_engine as engine  # noqa: E402
from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402

NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()
    engine.clear_injected()


def _p(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _window_ok(item) -> bool:
    return not item.get("valid_until") or _p(item["valid_until"]) >= _p(item["valid_from"])


# ---------------------------------------------------------------------------
# memory_conflicts
# ---------------------------------------------------------------------------


def test_historical_fact_added_later_never_closes_the_current_one(store):
    current = engine.add_item("Ada works at Bluehaven since 2024", owner="luis",
                              trust_class="human_explicit", now=NOW)
    past = engine.add_item("Ada works at Cordera Labs since 2019", owner="luis",
                           trust_class="human_explicit", now=LATER)
    cur = engine.get_item(current["id"])
    old = engine.get_item(past["id"])

    assert cur["valid_until"] == ""
    assert engine.is_valid_now(cur, LATER)
    # the historical fact is the one closed, where the current one starts
    assert old["valid_until"] == cur["valid_from"] == "2024-01-01T00:00:00Z"
    assert _window_ok(cur) and _window_ok(old)
    assert not engine.is_valid_now(old, LATER)

    rows = conflicts.list_conflicts(owner="luis", status="superseded")
    assert len(rows) == 1
    hits = engine.search("Ada works at", owner="luis", now=LATER, as_of=LATER, touch_hits=False)
    assert [h["id"] for h in hits] == [current["id"]]
    between = engine.search("Ada works at", owner="luis", now=LATER,
                            as_of=datetime(2020, 6, 1, tzinfo=timezone.utc), touch_hits=False)
    assert [h["id"] for h in between] == [past["id"]]


def test_chronological_order_still_closes_the_older_fact(store):
    old = engine.add_item("Ada works at Cordera Labs since 2019", owner="luis",
                          trust_class="human_explicit", now=NOW)
    new = engine.add_item("Ada works at Bluehaven since 2024", owner="luis",
                          trust_class="human_explicit", now=LATER)
    assert engine.get_item(old["id"])["valid_until"] == "2024-01-01T00:00:00Z"
    assert engine.get_item(new["id"])["valid_until"] == ""


def test_ambiguous_order_leaves_the_conflict_open_and_closes_nothing(store):
    # stored today with no date, then a dated fact that starts BEFORE it was
    # stored: which one is current is unknowable -> a human decides.
    undated = engine.add_item("Ada works at Bluehaven", owner="luis",
                              trust_class="human_explicit", now=NOW)
    dated = engine.add_item("Ada works at Cordera Labs since 2019", owner="luis",
                            trust_class="human_explicit", now=LATER)
    assert engine.get_item(undated["id"])["valid_until"] == ""
    assert engine.get_item(dated["id"])["valid_until"] == ""
    assert len(conflicts.list_conflicts(owner="luis", status="open")) == 1
    assert conflicts.list_conflicts(owner="luis", status="superseded") == []


def test_supersede_never_extends_or_rewrites_an_earlier_end(store):
    old = engine.add_item("Ada works at Cordera Labs entre 2019 y 2021", owner="luis",
                          trust_class="human_explicit", now=NOW)
    engine.add_item("Ada works at Bluehaven since 2024", owner="luis",
                    trust_class="human_explicit", now=LATER)
    assert engine.get_item(old["id"])["valid_until"] == "2021-12-31T23:59:59Z"


def test_unsupersede_restores_the_new_item_it_closed(store):
    current = engine.add_item("Ada works at Bluehaven since 2024", owner="luis",
                              trust_class="human_explicit", now=NOW)
    past = engine.add_item("Ada works at Cordera Labs since 2019", owner="luis",
                           trust_class="human_explicit", now=LATER)
    row = conflicts.list_conflicts(owner="luis", status="superseded")[0]
    conflicts.unsupersede(row["id"])
    assert engine.get_item(past["id"])["valid_until"] == ""
    assert engine.get_item(current["id"])["valid_until"] == ""


def test_manual_superseded_resolution_never_inverts_a_window(store, monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: False if key == "memory_temporal_supersede" else default,
    )
    current = engine.add_item("Ada works at Bluehaven since 2024", owner="luis",
                              trust_class="human_explicit", now=NOW)
    past = engine.add_item("Ada works at Cordera Labs since 2019", owner="luis",
                           trust_class="human_explicit", now=LATER)
    row = conflicts.list_conflicts(owner="luis", status="open")[0]
    conflicts.resolve(row["id"], "superseded")
    for item_id in (current["id"], past["id"]):
        assert _window_ok(engine.get_item(item_id))
    assert engine.get_item(current["id"])["valid_until"] == ""


def test_saved_previous_window_survives_a_long_detail(store, monkeypatch):
    # A long conflict detail used to be cut to 500 chars AFTER the saved
    # valid_until was appended to it, so unsupersede restored "" instead.
    monkeypatch.setattr(conflicts, "classify",
                        lambda a, b: ("same_subject_different_value", "x" * 900))
    monkeypatch.setattr(conflicts, "_is_supersedable_pair", lambda a, b: True)
    old = engine.add_item("Ada works at Cordera Labs", owner="luis", trust_class="human_explicit",
                          now=NOW, valid_until="2030-01-01T00:00:00Z")
    engine.add_item("Ada works at Bluehaven", owner="luis", trust_class="human_explicit",
                    now=LATER)
    assert engine.get_item(old["id"])["valid_until"] == engine._iso(LATER)
    row = conflicts.list_conflicts(owner="luis", status="superseded")[0]
    conflicts.unsupersede(row["id"])
    assert engine.get_item(old["id"])["valid_until"] == "2030-01-01T00:00:00Z"


def test_unsupersede_still_reads_rows_written_with_the_legacy_detail_marker(store):
    old = engine.add_item("Ada works at Cordera Labs", owner="luis", trust_class="human_explicit",
                          now=NOW)
    new = engine.add_item("Ada works at Bluehaven", owner="luis", trust_class="human_explicit",
                          now=LATER)
    row = conflicts.list_conflicts(owner="luis", status="superseded")[0]
    with conflicts._db() as conn:
        conn.execute(f"UPDATE {conflicts._TABLE} SET supersede = '', detail = ? WHERE id = ?",
                     ("legacy detail" + conflicts._PREV_MARK + "2031-05-05T00:00:00Z", row["id"]))
    conflicts.unsupersede(row["id"])
    assert engine.get_item(old["id"])["valid_until"] == "2031-05-05T00:00:00Z"
    assert conflicts.get_conflict(row["id"])["detail"] == "legacy detail"
    assert engine.get_item(new["id"])["valid_until"] == ""


# ---------------------------------------------------------------------------
# entities.add_relation
# ---------------------------------------------------------------------------


def test_relation_historical_assertion_never_closes_the_current_one(store):
    ada = entities.upsert_entity("luis", "Ada", type="person")
    blue = entities.upsert_entity("luis", "Bluehaven", type="organization")
    cord = entities.upsert_entity("luis", "Cordera Labs", type="organization")
    current = entities.add_relation("luis", ada["id"], "works_at", dst_id=blue["id"],
                                    valid_from="2024-01-01T00:00:00Z")
    past = entities.add_relation("luis", ada["id"], "works_at", dst_id=cord["id"],
                                 valid_from="2019-01-01T00:00:00Z")
    rels = {r["id"]: r for r in entities.list_relations("luis", entity_id=ada["id"])}
    assert rels[current["id"]]["status"] == "active"
    assert rels[current["id"]]["valid_until"] == ""
    assert rels[past["id"]]["status"] == "superseded"
    assert rels[past["id"]]["valid_until"] == "2024-01-01T00:00:00Z"
    assert rels[past["id"]]["superseded_by"] == current["id"]
    for rel in rels.values():
        assert not rel["valid_until"] or rel["valid_until"] >= rel["valid_from"]
    now_rels = entities.list_relations("luis", entity_id=ada["id"], as_of=LATER)
    assert [r["dst"] for r in now_rels] == [blue["id"]]
    then = entities.list_relations("luis", entity_id=ada["id"],
                                   as_of=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert [r["dst"] for r in then] == [cord["id"]]


def test_relation_already_ended_is_not_rewritten(store):
    ada = entities.upsert_entity("luis", "Ada", type="person")
    blue = entities.upsert_entity("luis", "Bluehaven", type="organization")
    cord = entities.upsert_entity("luis", "Cordera Labs", type="organization")
    old = entities.add_relation("luis", ada["id"], "works_at", dst_id=cord["id"],
                                valid_from="2019-01-01T00:00:00Z",
                                valid_until="2021-12-31T23:59:59Z")
    entities.add_relation("luis", ada["id"], "works_at", dst_id=blue["id"],
                          valid_from="2024-01-01T00:00:00Z")
    rel = next(r for r in entities.list_relations("luis", entity_id=ada["id"])
               if r["id"] == old["id"])
    assert rel["valid_until"] == "2021-12-31T23:59:59Z"


def test_relation_same_start_is_ambiguous_and_closes_nothing(store):
    ada = entities.upsert_entity("luis", "Ada", type="person")
    blue = entities.upsert_entity("luis", "Bluehaven", type="organization")
    cord = entities.upsert_entity("luis", "Cordera Labs", type="organization")
    entities.add_relation("luis", ada["id"], "works_at", dst_id=blue["id"],
                          valid_from="2024-01-01T00:00:00Z")
    entities.add_relation("luis", ada["id"], "works_at", dst_id=cord["id"],
                          valid_from="2024-01-01T00:00:00Z")
    active = entities.list_relations("luis", entity_id=ada["id"], include_closed=False)
    assert len(active) == 2
    assert all(r["valid_until"] == "" for r in active)
